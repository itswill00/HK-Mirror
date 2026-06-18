import os
import time
import asyncio
import logging
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config import config, DOWNLOAD_DIR
from database import db
from helpers.trackers import active_tasks, active_tasks_meta
from helpers.ui import UI, get_admin_keyboard

logger = logging.getLogger("MirrorBot.Admin")

# Command: Admin Dashboard
@Client.on_message(filters.command("admin") & filters.user(config.owner_id))
async def admin_cmd(client: Client, message: Message):
    await message.reply_text(
        "**Admin Control Panel**\n\n"
        "Welcome Owner! Please select a management option below:",
        reply_markup=get_admin_keyboard()
    )

# Command: Add Quota
@Client.on_message(filters.command("addquota") & filters.user(config.owner_id))
async def addquota_cmd(client: Client, message: Message):
    if len(message.command) < 3:
        await message.reply_text("Invalid format! Use: `/addquota <user_id> <quota_in_gb>`")
        return
        
    try:
        target_id = int(message.command[1].strip())
        gb_to_add = float(message.command[2].strip())
    except ValueError:
        await message.reply_text("User ID must be an integer and GB must be a number/decimal!")
        return
        
    bytes_to_add = int(gb_to_add * 1024 * 1024 * 1024)
    
    user = await db.users.find_one({"_id": target_id})
    if not user:
        await message.reply_text(f"User with ID `{target_id}` was not found in the database.")
        return
        
    await db.users.update_one(
        {"_id": target_id},
        {"$inc": {"quota_left": bytes_to_add}}
    )
    
    new_quota = user.get("quota_left", 0) + bytes_to_add
    await message.reply_text(
        f"**Quota Added Successfully!**\n\n"
        f"• **User:** `{target_id}`\n"
        f"• **Added:** `{gb_to_add} GB`\n"
        f"• **Current Quota:** `{UI.human_size(new_quota)}`"
    )

# Command: Delete User
@Client.on_message(filters.command("deluser") & filters.user(config.owner_id))
async def deluser_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Invalid format! Use: `/deluser <user_id>`")
        return
        
    try:
        target_id = int(message.command[1].strip())
    except ValueError:
        await message.reply_text("User ID must be a number!")
        return
        
    user = await db.users.find_one({"_id": target_id})
    if not user:
        await message.reply_text(f"User with ID `{target_id}` not found.")
        return
        
    await db.users.delete_one({"_id": target_id})
    await message.reply_text(f"User with ID `{target_id}` deleted successfully from the database.")

# Callback Query: Admin Actions
@Client.on_callback_query(filters.regex(r"^admin:"))
async def admin_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id != config.owner_id:
        await callback_query.answer("You are not the Owner!", show_alert=True)
        return
        
    action = callback_query.data.split(":")[1]
    
    if action == "main":
        await callback_query.message.edit_text(
            "**Admin Control Panel**\n\n"
            "Welcome Owner! Please select a management option below:",
            reply_markup=get_admin_keyboard()
        )
    elif action == "users":
        users_cursor = db.users.find({"verified": True})
        users_list = await users_cursor.to_list(length=100)
        
        text = "**Verified Users List:**\n\n"
        if not users_list:
            text += "No verified users found."
        else:
            for idx, u in enumerate(users_list, 1):
                username = f"@{u['username']}" if u.get("username") else "No Username"
                quota = UI.human_size(u.get("quota_left", 0))
                text += f"{idx}. {username} (ID: `{u['_id']}`) - Quota: `{quota}`\n"
                
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Back", callback_data="admin:main")]
        ])
        await callback_query.message.edit_text(text, reply_markup=keyboard)
        
    elif action == "stats":
        total_users = await db.users.count_documents({"verified": True})
        total_pending = await db.pending.count_documents({})
        
        downloads_size = 0
        for root, dirs, files in os.walk(DOWNLOAD_DIR):
            for f in files:
                fp = os.path.join(root, f)
                if os.path.exists(fp):
                    downloads_size += os.path.getsize(fp)
                    
        text = (
            f"**Mirror Bot Statistics:**\n\n"
            f"• **Verified Users:** `{total_users}`\n"
            f"• **Pending Verifications:** `{total_pending}`\n"
            f"• **Server Storage:** `{UI.human_size(downloads_size)}`\n"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Clean", callback_data="admin:clean_storage"),
                InlineKeyboardButton("Back", callback_data="admin:main")
            ]
        ])
        await callback_query.message.edit_text(text, reply_markup=keyboard)
        
    elif action == "clean_storage":
        # Get active file paths to exclude them from deletion
        active_paths = set()
        for meta in active_tasks_meta.values():
            file_path = meta.get("file_path")
            if file_path:
                active_paths.add(os.path.abspath(file_path))
                # Also exclude possible split parts
                file_dir = os.path.dirname(file_path)
                base_name = os.path.basename(file_path)
                if os.path.exists(file_dir):
                    for f in os.listdir(file_dir):
                        if f.startswith(base_name + "."):
                            active_paths.add(os.path.abspath(os.path.join(file_dir, f)))
                            
        cleaned_count = 0
        cleaned_size = 0
        
        for root, dirs, files in os.walk(DOWNLOAD_DIR):
            for f in files:
                fp = os.path.abspath(os.path.join(root, f))
                if fp not in active_paths:
                    try:
                        sz = os.path.getsize(fp)
                        os.remove(fp)
                        cleaned_count += 1
                        cleaned_size += sz
                    except Exception:
                        pass
                        
        await callback_query.answer(
            f"Successfully cleaned {cleaned_count} unused files ({UI.human_size(cleaned_size)})!",
            show_alert=True
        )
        
        # Refresh the stats view
        total_users = await db.users.count_documents({"verified": True})
        total_pending = await db.pending.count_documents({})
        
        downloads_size = 0
        for root, dirs, files in os.walk(DOWNLOAD_DIR):
            for f in files:
                fp = os.path.join(root, f)
                if os.path.exists(fp):
                    downloads_size += os.path.getsize(fp)
                    
        text = (
            f"**Mirror Bot Statistics:**\n\n"
            f"• **Verified Users:** `{total_users}`\n"
            f"• **Pending Verifications:** `{total_pending}`\n"
            f"• **Server Storage:** `{UI.human_size(downloads_size)}`\n"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Clean", callback_data="admin:clean_storage"),
                InlineKeyboardButton("Back", callback_data="admin:main")
            ]
        ])
        await callback_query.message.edit_text(text, reply_markup=keyboard)
        
    elif action == "addquota":
        text = (
            "**Add User Quota**\n\n"
            "To add quota to a user, send the following command:\n"
            "`/addquota <user_id> <amount_in_gb>`\n\n"
            "**Example:** `/addquota 12345678 10` (adds 10 GB)"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Back", callback_data="admin:main")]
        ])
        await callback_query.message.edit_text(text, reply_markup=keyboard)
        
    elif action == "deluser":
        text = (
            "**Delete User**\n\n"
            "To remove user access, send the following command:\n"
            "`/deluser <user_id>`\n\n"
            "**Example:** `/deluser 12345678`"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Back", callback_data="admin:main")]
        ])
        await callback_query.message.edit_text(text, reply_markup=keyboard)

# Helper: Show User Management Detail Screen
async def show_user_management(target_msg, query_str: str, edit: bool = False):
    user = None
    if query_str.isdigit():
        user_id = int(query_str)
        user = await db.users.find_one({"_id": user_id})
    else:
        username = query_str.lstrip("@").lower()
        user = await db.users.find_one({"username": {"$regex": f"^{username}$", "$options": "i"}})
        
    if not user:
        txt = f"**User '{query_str}' not found in the database.**"
        if edit:
            await target_msg.edit_text(txt)
        else:
            await target_msg.reply_text(txt)
        return
        
    user_id = user["_id"]
    username = user.get("username", "No Username")
    quota_left = user.get("quota_left", 10 * 1024 * 1024 * 1024)
    verified = user.get("verified", False)
    
    # Check if user has active tasks
    active_task_id = ""
    for tid, meta in active_tasks_meta.items():
        if meta.get("user_id") == user_id:
            active_task_id = tid
            break
            
    status_emoji = "Verified" if verified else "Banned/Unverified"
    task_status = f"Active (`{active_task_id}`)" if active_task_id else "None"
    
    text = (
        f"**User Management**\n\n"
        f"• **User ID:** `{user_id}`\n"
        f"• **Username:** @{username}\n"
        f"• **Status:** {status_emoji}\n"
        f"• **Remaining Quota:** `{UI.human_size(quota_left)}`\n"
        f"• **Active Task:** {task_status}\n"
    )
    
    buttons = [
        [
            InlineKeyboardButton("+10 GB", callback_data=f"usermg:add:10:{user_id}"),
            InlineKeyboardButton("+50 GB", callback_data=f"usermg:add:50:{user_id}")
        ],
        [
            InlineKeyboardButton("Reset 10 GB", callback_data=f"usermg:reset:10:{user_id}"),
            InlineKeyboardButton(
                "Unban" if not verified else "Block", 
                callback_data=f"usermg:toggle_ban:{user_id}"
            )
        ]
    ]
    
    if active_task_id:
        buttons.append([
            InlineKeyboardButton("Stop Task", callback_data=f"usermg:kill_task:{active_task_id}:{user_id}")
        ])
        
    buttons.append([
        InlineKeyboardButton("Close", callback_data="usermg:close")
    ])
    
    reply_markup = InlineKeyboardMarkup(buttons)
    
    if edit:
        await target_msg.edit_text(text, reply_markup=reply_markup)
    else:
        await target_msg.reply_text(text, reply_markup=reply_markup)

# Command: User Manager (Owner Only)
@Client.on_message(filters.command("user") & filters.user(config.owner_id))
async def user_manager_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Use: `/user <user_id>` or `/user <@username>`")
        return
        
    query_str = message.command[1].strip()
    await show_user_management(message, query_str)

# Callback Query: User Management Actions
@Client.on_callback_query(filters.regex(r"^usermg:"))
async def user_management_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id != config.owner_id:
        await callback_query.answer("Access denied!", show_alert=True)
        return
        
    data = callback_query.data.split(":")
    action = data[1]
    
    if action == "close":
        await callback_query.message.delete()
        return
        
    target_id = int(data[-1])
    
    user = await db.users.find_one({"_id": target_id})
    if not user:
        await callback_query.answer("User not found!", show_alert=True)
        return
        
    if action == "add":
        gb = float(data[2])
        bytes_to_add = int(gb * 1024 * 1024 * 1024)
        await db.users.update_one({"_id": target_id}, {"$inc": {"quota_left": bytes_to_add}})
        await callback_query.answer(f"Added {gb} GB!", show_alert=True)
        
    elif action == "reset":
        gb = float(data[2])
        bytes_to_set = int(gb * 1024 * 1024 * 1024)
        await db.users.update_one({"_id": target_id}, {"$set": {"quota_left": bytes_to_set}})
        await callback_query.answer(f"Quota reset to {gb} GB!", show_alert=True)
        
    elif action == "toggle_ban":
        current_status = user.get("verified", False)
        new_status = not current_status
        await db.users.update_one({"_id": target_id}, {"$set": {"verified": new_status}})
        msg = "User unbanned/verified!" if new_status else "User blocked!"
        await callback_query.answer(f"{msg}", show_alert=True)
        
    elif action == "kill_task":
        task_id = data[2]
        task = active_tasks.get(task_id)
        if task:
            task.cancel()
            await callback_query.answer("Task successfully forced stopped!", show_alert=True)
        else:
            await callback_query.answer("Task is no longer active or already completed.", show_alert=True)
            
    await show_user_management(callback_query.message, str(target_id), edit=True)

# Command: Set Quota (Owner Only)
@Client.on_message(filters.command("setquota") & filters.user(config.owner_id))
async def setquota_cmd(client: Client, message: Message):
    if len(message.command) < 3:
        await message.reply_text("Invalid format! Use: `/setquota <user_id> <quota_in_gb>`")
        return
        
    try:
        target_id = int(message.command[1].strip())
        gb_to_set = float(message.command[2].strip())
    except ValueError:
        await message.reply_text("User ID must be an integer and GB must be a number/decimal!")
        return
        
    bytes_to_set = int(gb_to_set * 1024 * 1024 * 1024)
    
    user = await db.users.find_one({"_id": target_id})
    if not user:
        await message.reply_text(f"User with ID `{target_id}` not found in the database.")
        return
        
    await db.users.update_one(
        {"_id": target_id},
        {"$set": {"quota_left": bytes_to_set}}
    )
    
    await message.reply_text(
        f"**Quota Configured Successfully!**\n\n"
        f"• **User:** `{target_id}`\n"
        f"• **New Quota:** `{gb_to_set} GB` (`{UI.human_size(bytes_to_set)}`)"
    )

# Command: Broadcast (Owner Only)
@Client.on_message(filters.command("broadcast") & filters.user(config.owner_id))
async def broadcast_cmd(client: Client, message: Message):
    if not message.reply_to_message and len(message.command) < 2:
        await message.reply_text("Use: `/broadcast <message>` or reply to a message you want to broadcast with `/broadcast`")
        return
        
    status_msg = await message.reply_text("**Starting message broadcast...**")
    
    users_cursor = db.users.find({"verified": True})
    users_list = await users_cursor.to_list(length=2000)
    
    success = 0
    failed = 0
    total = len(users_list)
    
    last_update = time.time()
    
    for idx, u in enumerate(users_list, 1):
        user_id = u["_id"]
        if user_id == config.owner_id:
            continue
        try:
            if message.reply_to_message:
                await message.reply_to_message.copy(chat_id=user_id)
            else:
                text = message.text.split(None, 1)[1]
                await client.send_message(chat_id=user_id, text=text)
            success += 1
        except Exception:
            failed += 1
            
        now = time.time()
        if now - last_update > 4.0:
            try:
                await status_msg.edit_text(f"**Sending Broadcast...**\n• Progress: `{idx}/{total}`\n• Success: `{success}`\n• Failed: `{failed}`")
            except Exception:
                pass
            last_update = now
            
        await asyncio.sleep(0.05)
        
    await status_msg.edit_text(
        f"**Broadcast Completed**\n\n"
        f"• **Total Users:** `{total}`\n"
        f"• **Successfully Sent:** `{success}`\n"
        f"• **Failed/Blocked:** `{failed}`"
    )
