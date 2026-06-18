import os
import time
import uuid
import logging
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery

from config import config, DOWNLOAD_DIR
from database import db
from helpers.decorators import verified_only
from helpers.trackers import active_tasks, active_tasks_meta, cooldowns, mirror_queries
from helpers.ui import (
    extract_url, get_cancel_keyboard, edit_message_with_style,
    edit_styled_message, send_styled_message, UI
)
from helpers.network import network, resolve_direct_link, get_link_size

logger = logging.getLogger("MirrorBot.Mirror")

# Shared Mirror Worker (Downloads, then Uploads to target service)
async def run_mirror_worker(client: Client, status_msg: Message, url: str, user_id: int, service: str):
    file_path = None
    task_id = f"{status_msg.chat.id}_{status_msg.id}"
    
    try:
        # Resolve user mention
        try:
            user = await client.get_users(user_id)
            user_mention = f"@{user.username}" if user.username else f"[{user.first_name}](tg://user?id={user.id})"
            user_mention += f" (ID: `{user.id}`)"
        except Exception:
            user_mention = f"ID: `{user_id}`"

        # Register task
        active_tasks[task_id] = asyncio.current_task()
        active_tasks_meta[task_id] = {
            "user_id": user_id,
            "user_mention": user_mention,
            "file_path": None,
            "status_msg": status_msg
        }

        # Resolve direct link dynamically
        await edit_message_with_style(status_msg, f"**Resolving link...**\n• **User:** {user_mention}", reply_markup=get_cancel_keyboard(task_id, url))
        url = await resolve_direct_link(url)

        # Pre-check file size
        await edit_message_with_style(status_msg, f"**Checking file size...**\n• **User:** {user_mention}", reply_markup=get_cancel_keyboard(task_id, url))
        filesize_pre = await get_link_size(url)
        if filesize_pre is not None:
            if filesize_pre > 10 * 1024 * 1024 * 1024:
                raise Exception("File size exceeds the 10 GB limit!")
            if user_id != config.owner_id:
                user_data = await db.users.find_one({"_id": user_id})
                quota_left = user_data.get("quota_left", 10 * 1024 * 1024 * 1024) if user_data else 10 * 1024 * 1024 * 1024
                if filesize_pre > quota_left:
                    raise Exception(f"Your remaining quota ({UI.human_size(quota_left)}) is not enough for this file ({UI.human_size(filesize_pre)})!")

        # Step 1: Download
        await edit_message_with_style(status_msg, f"**Downloading file...**\n• **User:** {user_mention}", reply_markup=get_cancel_keyboard(task_id, url))
        file_path = await network.download(url, DOWNLOAD_DIR, status_msg, user_id=user_id, task_id=task_id)
        filename = os.path.basename(file_path)
        if not file_path or not os.path.exists(file_path):
            raise FileNotFoundError("Downloaded file not found on server.")
        filesize = os.path.getsize(file_path)
        
        # Step 2: Upload
        if service == "gofile":
            await edit_message_with_style(status_msg, f"**Uploading to Gofile...**\n• **User:** {user_mention}", reply_markup=get_cancel_keyboard(task_id, url))
            link = await network.upload_gofile(file_path, config.gofile_key, status_msg, task_id, url)
            btn_text = "Gofile"
            btn_style = "success"
        elif service == "pixeldrain":
            await edit_message_with_style(status_msg, f"**Uploading to Pixeldrain...**\n• **User:** {user_mention}", reply_markup=get_cancel_keyboard(task_id, url))
            link = await network.upload_pixeldrain(file_path, config.pixeldrain_key, status_msg, task_id, url)
            btn_text = "Pixeldrain"
            btn_style = "primary"
        else:
            raise ValueError(f"Unknown mirror service: {service}")
            
        # Deduct quota from MongoDB
        if user_id != config.owner_id:
            await db.users.update_one({"_id": user_id}, {"$inc": {"quota_left": -filesize}})
            logger.info(f"Deducted {filesize} bytes from user {user_id}'s quota.")

        # Finished successfully
        success_text = (
            f"**File successfully mirrored**\n\n"
            f"• **Filename:** `{filename}`\n"
            f"• **Size:** `{UI.human_size(filesize)}`\n"
            f"• **User:** {user_mention}"
        )
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": btn_text, "url": link, "style": btn_style}
                ]
            ]
        }
        await edit_message_with_style(status_msg, success_text, reply_markup=reply_markup)
        
    except asyncio.CancelledError:
        logger.info(f"Task {task_id} was cancelled by user.")
        try:
            user_mention = active_tasks_meta.get(task_id, {}).get("user_mention", f"ID: `{user_id}`")
            await edit_message_with_style(status_msg, f"**Mirroring process cancelled by user.**\n• **User:** {user_mention}")
        except Exception:
            pass
        raise
    except Exception as e:
        logger.error(f"Error executing {service} mirror worker: {e}", exc_info=True)
        try:
            user_mention = active_tasks_meta.get(task_id, {}).get("user_mention", f"ID: `{user_id}`")
            await edit_message_with_style(status_msg, f"**An error occurred:**\n`{str(e)}`\n• **User:** {user_mention}")
        except Exception:
            pass
    finally:
        # Cleanup task tracking
        active_tasks.pop(task_id, None)
        active_tasks_meta.pop(task_id, None)
        
        # Cleanup local file
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"Cleaned up local file: {file_path}")
            except Exception as e:
                logger.warning(f"Failed to delete {file_path}: {e}")

# Command: Gofile Mirror
@Client.on_message(filters.command(["gofile", "gf"]))
@verified_only
async def gofile_cmd(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return

    # Anti-spam Cooldown Check (30 seconds)
    now = time.time()
    last_time = cooldowns.get(user_id, 0.0)
    if now - last_time < 30.0 and user_id != config.owner_id:
        seconds_left = int(30.0 - (now - last_time))
        await message.reply_text(f"Please wait `{seconds_left}` seconds before trying again.")
        return
    cooldowns[user_id] = now

    url = extract_url(message)
    if not url:
        await message.reply_text("Link missing. Example: `/gf https://link.com/file.zip` or reply to a message containing a link.")
        return
        
    # Resolve user details for the initial text
    try:
        user = message.from_user
        user_mention = f"@{user.username}" if user.username else f"[{user.first_name}](tg://user?id={user.id})"
        user_mention += f" (ID: `{user.id}`)"
    except Exception:
        user_mention = f"ID: `{user_id}`"

    status_msg = await message.reply_text(f"**Processing, please wait...**\n• **User:** {user_mention}")
    await run_mirror_worker(client, status_msg, url, user_id, "gofile")

# Command: Pixeldrain Mirror
@Client.on_message(filters.command(["pixeldrain", "pd"]))
@verified_only
async def pixeldrain_cmd(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return

    # Anti-spam Cooldown Check (30 seconds)
    now = time.time()
    last_time = cooldowns.get(user_id, 0.0)
    if now - last_time < 30.0 and user_id != config.owner_id:
        seconds_left = int(30.0 - (now - last_time))
        await message.reply_text(f"Please wait `{seconds_left}` seconds before trying again.")
        return
    cooldowns[user_id] = now

    url = extract_url(message)
    if not url:
        await message.reply_text("Link missing. Example: `/pd https://link.com/file.zip` or reply to a message containing a link.")
        return
        
    # Resolve user details for the initial text
    try:
        user = message.from_user
        user_mention = f"@{user.username}" if user.username else f"[{user.first_name}](tg://user?id={user.id})"
        user_mention += f" (ID: `{user.id}`)"
    except Exception:
        user_mention = f"ID: `{user_id}`"

    status_msg = await message.reply_text(f"**Processing, please wait...**\n• **User:** {user_mention}")
    await run_mirror_worker(client, status_msg, url, user_id, "pixeldrain")

# Command: Mirror Choice Trigger (asks user where to mirror)
@Client.on_message(filters.command("mirror"))
@verified_only
async def mirror_cmd(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return

    url = extract_url(message)
    if not url:
        await message.reply_text(
            "Link missing.\n"
            "Example: `/mirror https://link.com/file.zip` or reply to a message containing a link."
        )
        return

    # Prune old cache entries (older than 10 minutes)
    now_time = time.time()
    expired = [k for k, v in mirror_queries.items() if now_time - v["timestamp"] > 600.0]
    for k in expired:
        mirror_queries.pop(k, None)

    # Generate a unique query ID for caching
    query_id = str(uuid.uuid4())[:8]
    mirror_queries[query_id] = {
        "url": url,
        "user_id": user_id,
        "timestamp": now_time
    }
    
    # Inline buttons with colored styles
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "Gofile", "callback_data": f"mirror_choice:gofile:{query_id}", "style": "success"},
                {"text": "Pixeldrain", "callback_data": f"mirror_choice:pixeldrain:{query_id}", "style": "primary"}
            ],
            [
                {"text": "Cancel", "callback_data": f"mirror_choice:cancel:{query_id}", "style": "danger"}
            ]
        ]
    }
    
    await send_styled_message(
        chat_id=message.chat.id,
        text=(
            f"**Select Mirror Service**\n\n"
            f"Where would you like to upload the file?\n"
            f"• **Link:** `{url}`"
        ),
        reply_markup=reply_markup,
        reply_to_message_id=message.id
    )

# Callback Query: Mirror Choice Processing
@Client.on_callback_query(filters.regex(r"^mirror_choice:"))
async def mirror_choice_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data.split(":")
    if len(data) < 3:
        return
        
    choice = data[1]
    query_id = data[2]
    clicker_user_id = callback_query.from_user.id
    
    query = mirror_queries.get(query_id)
    if not query:
        await callback_query.answer("Session expired or not found!", show_alert=True)
        try:
            await callback_query.message.delete()
        except Exception:
            pass
        return
        
    if clicker_user_id != query["user_id"] and clicker_user_id != config.owner_id:
        await callback_query.answer("This button is not for you!", show_alert=True)
        return
        
    url = query["url"]
    mirror_queries.pop(query_id, None)
    
    if choice == "cancel":
        await callback_query.answer("Process cancelled.")
        try:
            await callback_query.message.delete()
        except Exception:
            pass
        return
        
    await callback_query.answer(f"Processing upload to {choice.capitalize()}...")
    
    # Resolve user details for the initial text
    try:
        user = callback_query.from_user
        user_mention = f"@{user.username}" if user.username else f"[{user.first_name}](tg://user?id={user.id})"
        user_mention += f" (ID: `{user.id}`)"
    except Exception:
        user_mention = f"ID: `{clicker_user_id}`"

    # Edit message to start progress updates
    status_msg = await edit_styled_message(
        chat_id=callback_query.message.chat.id,
        message_id=callback_query.message.id,
        text=f"**Processing, please wait...**\n• **User:** {user_mention}"
    )
    if not status_msg:
        status_msg = callback_query.message
        await status_msg.edit_text(f"**Processing, please wait...**\n• **User:** {user_mention}")
        
    asyncio.create_task(run_mirror_worker(client, status_msg, url, clicker_user_id, choice))
