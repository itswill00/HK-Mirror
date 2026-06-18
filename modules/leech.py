import os
import time
import logging
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery

from config import config, DOWNLOAD_DIR, THUMBNAIL_DIR
from database import db
from helpers.decorators import verified_only
from helpers.trackers import active_tasks, active_tasks_meta, cooldowns
from helpers.ui import (
    extract_url, get_cancel_keyboard, UI, leech_upload_progress
)
from helpers.network import network, resolve_direct_link, get_link_size, split_file

logger = logging.getLogger("MirrorBot.Leech")

# Command: Leech
@Client.on_message(filters.command("leech"))
@verified_only
async def leech_cmd(client: Client, message: Message):
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
        await message.reply_text("Link missing. Example: `/leech https://link.com/file.zip` or reply to a message containing a link.")
        return

    # Resolve user mention
    try:
        user = await client.get_users(user_id)
        user_mention = f"@{user.username}" if user.username else f"[{user.first_name}](tg://user?id={user.id})"
        user_mention += f" (ID: `{user.id}`)"
    except Exception:
        user_mention = f"ID: `{user_id}`"

    status_msg = await message.reply_text(f"**Processing, please wait...**\n• **User:** {user_mention}")
    
    file_path = None
    task_id = f"{status_msg.chat.id}_{status_msg.id}"
    
    try:
        # Register task
        active_tasks[task_id] = asyncio.current_task()
        active_tasks_meta[task_id] = {
            "user_id": user_id,
            "user_mention": user_mention,
            "file_path": None,
            "status_msg": status_msg
        }

        # Resolve direct link dynamically
        await status_msg.edit_text(f"**Resolving link...**\n• **User:** {user_mention}", reply_markup=get_cancel_keyboard(task_id, url))
        url = await resolve_direct_link(url)

        # Pre-check file size
        await status_msg.edit_text(f"**Checking file size...**\n• **User:** {user_mention}", reply_markup=get_cancel_keyboard(task_id, url))
        filesize_pre = await get_link_size(url)
        if filesize_pre is not None:
            if filesize_pre > 10 * 1024 * 1024 * 1024:
                raise Exception("File size exceeds the maximum limit of 10 GB!")
            if user_id != config.owner_id:
                user_data = await db.users.find_one({"_id": user_id})
                quota_left = user_data.get("quota_left", 10 * 1024 * 1024 * 1024) if user_data else 10 * 1024 * 1024 * 1024
                if filesize_pre > quota_left:
                    raise Exception(f"Your remaining quota ({UI.human_size(quota_left)}) is not enough for this file ({UI.human_size(filesize_pre)})!")

        # Step 1: Download
        await status_msg.edit_text(f"**Downloading file...**\n• **User:** {user_mention}", reply_markup=get_cancel_keyboard(task_id, url))
        file_path = await network.download(url, DOWNLOAD_DIR, status_msg, user_id=user_id, task_id=task_id)
        filename = os.path.basename(file_path)
        filesize = os.path.getsize(file_path)
        
        # Retrieve custom thumbnail if exists
        thumb_path = os.path.join(THUMBNAIL_DIR, f"{user_id}.jpg")
        thumb = thumb_path if os.path.exists(thumb_path) else None
        
        # Step 2: Upload to Telegram
        await status_msg.edit_text(f"**Uploading to Telegram...**\n• **User:** {user_mention}", reply_markup=get_cancel_keyboard(task_id, url))
        
        max_upload_size = 1999 * 1024 * 1024  # 1.99 GB
        if filesize > max_upload_size:
            await status_msg.edit_text(f"**Large file detected ({UI.human_size(filesize)}).**\nSplitting file into parts...\n• **User:** {user_mention}", reply_markup=get_cancel_keyboard(task_id, url))
            split_files = await split_file(file_path, max_upload_size)
            
            total_parts = len(split_files)
            for idx, part_path in enumerate(split_files, 1):
                part_size = os.path.getsize(part_path)
                part_name = os.path.basename(part_path)
                await status_msg.edit_text(f"**Sending Part {idx}/{total_parts}...**\n`{part_name}`\n• **User:** {user_mention}", reply_markup=get_cancel_keyboard(task_id, url))
                
                start_time = time.time()
                await client.send_document(
                    chat_id=message.chat.id,
                    document=part_path,
                    file_name=part_name,
                    caption=f"**Part {idx}/{total_parts}**\n\n• **Filename:** `{filename}`\n• **Part Size:** `{UI.human_size(part_size)}`\n• **Total Size:** `{UI.human_size(filesize)}`\n• **User:** {user_mention}",
                    progress=leech_upload_progress,
                    progress_args=(status_msg, start_time, part_name, task_id, url),
                    thumb=thumb
                )
                # Remove part file
                os.remove(part_path)
                
            await status_msg.delete()
        else:
            # Send single file
            start_time = time.time()
            await client.send_document(
                chat_id=message.chat.id,
                document=file_path,
                file_name=filename,
                caption=f"**Leech Completed**\n\n• **Filename:** `{filename}`\n• **Size:** `{UI.human_size(filesize)}`\n• **User:** {user_mention}",
                progress=leech_upload_progress,
                progress_args=(status_msg, start_time, filename, task_id, url),
                thumb=thumb
            )
            await status_msg.delete()

        # Deduct quota from MongoDB
        if user_id != config.owner_id:
            await db.users.update_one({"_id": user_id}, {"$inc": {"quota_left": -filesize}})
            logger.info(f"Deducted {filesize} bytes from user {user_id}'s quota.")
        
    except asyncio.CancelledError:
        logger.info(f"Task {task_id} was cancelled by user.")
        try:
            user_mention = active_tasks_meta.get(task_id, {}).get("user_mention", f"ID: `{user_id}`")
            await status_msg.edit_text(f"**Leech process cancelled by user.**\n• **User:** {user_mention}")
        except Exception:
            pass
        raise
    except Exception as e:
        logger.error(f"Error executing leech command: {e}", exc_info=True)
        try:
            user_mention = active_tasks_meta.get(task_id, {}).get("user_mention", f"ID: `{user_id}`")
            await status_msg.edit_text(f"**An error occurred:**\n`{str(e)}`\n• **User:** {user_mention}")
        except Exception:
            pass
    finally:
        # Cleanup task tracking
        active_tasks.pop(task_id, None)
        active_tasks_meta.pop(task_id, None)
        
        # Cleanup files
        if file_path:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    logger.info(f"Cleaned up local file: {file_path}")
                except Exception as e:
                    logger.warning(f"Failed to delete {file_path}: {e}")
            
            # Clean split parts
            file_dir = os.path.dirname(file_path)
            base_name = os.path.basename(file_path)
            if os.path.exists(file_dir):
                for f in os.listdir(file_dir):
                    if f.startswith(base_name + "."):
                        try:
                            os.remove(os.path.join(file_dir, f))
                            logger.info(f"Cleaned up split part on task exit: {f}")
                        except Exception:
                            pass

# Command: Quota
@Client.on_message(filters.command("quota"))
@verified_only
async def quota_cmd(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return
        
    if user_id == config.owner_id:
        await message.reply_text("**You are the Owner!** Quota: `Unlimited`")
        return
        
    user_data = await db.users.find_one({"_id": user_id})
    quota_left = user_data.get("quota_left", 10 * 1024 * 1024 * 1024) if user_data else 10 * 1024 * 1024 * 1024
    
    await message.reply_text(
        f"**Your Quota Information:**\n\n"
        f"• **Remaining Quota:** `{UI.human_size(quota_left)}` of `10.00 GB`\n\n"
        f"Your quota will be deducted automatically upon successful mirror or leech."
    )

# Command: Set Thumbnail
@Client.on_message(filters.command("setthumb"))
@verified_only
async def setthumb_cmd(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return
        
    # Check if user replied to a photo, or if the message itself has a photo
    photo = None
    if message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo
    elif message.photo:
        photo = message.photo
        
    if not photo:
        await message.reply_text(
            "**How to Set a Custom Thumbnail:**\n\n"
            "Send an image with the caption `/setthumb` or reply to an existing image with `/setthumb`."
        )
        return
        
    status_msg = await message.reply_text("**Processing thumbnail...**")
    
    try:
        thumb_path = os.path.join(THUMBNAIL_DIR, f"{user_id}.jpg")
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
            
        await client.download_media(
            message=photo,
            file_name=thumb_path
        )
        
        await db.users.update_one(
            {"_id": user_id},
            {"$set": {"has_thumbnail": True}}
        )
        
        await status_msg.edit_text("**Custom thumbnail saved successfully!** This thumbnail will be used for files sent via `/leech`.")
    except Exception as e:
        logger.error(f"Error setting thumbnail: {e}", exc_info=True)
        await status_msg.edit_text(f"**Failed to save thumbnail:**\n`{str(e)}`")

# Command: Delete Thumbnail
@Client.on_message(filters.command("delthumb"))
@verified_only
async def delthumb_cmd(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return
        
    thumb_path = os.path.join(THUMBNAIL_DIR, f"{user_id}.jpg")
    if os.path.exists(thumb_path):
        try:
            os.remove(thumb_path)
            await db.users.update_one(
                {"_id": user_id},
                {"$set": {"has_thumbnail": False}}
            )
            await message.reply_text("**Custom thumbnail deleted successfully!**")
        except Exception as e:
            await message.reply_text(f"**Failed to delete thumbnail:**\n`{str(e)}`")
    else:
        await message.reply_text("**You do not have a custom thumbnail set.**")

# Callback Query: Cancel Task
@Client.on_callback_query(filters.regex(r"^cancel:"))
async def cancel_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data.split(":")
    if len(data) < 2:
        return
        
    task_id = data[1]
    user_id = callback_query.from_user.id
    
    meta = active_tasks_meta.get(task_id)
    if not meta:
        await callback_query.answer("Task not found or already completed!", show_alert=True)
        return
        
    if user_id != meta["user_id"] and user_id != config.owner_id:
        await callback_query.answer("You do not have permission to cancel this task!", show_alert=True)
        return
        
    task = active_tasks.get(task_id)
    if task:
        task.cancel()
        await callback_query.answer("Cancelling task...")
        try:
            await meta["status_msg"].edit_text("**Task is being cancelled...**")
        except Exception:
            pass
    else:
        await callback_query.answer("Task is not active.", show_alert=True)
