import logging
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery

from config import config
from database import db
from helpers.ui import get_verify_keyboard, edit_styled_message, UI

logger = logging.getLogger("MirrorBot.Verify")

# Callback Query: Verification
@Client.on_callback_query(filters.regex(r"^verify:"))
async def verify_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data.split(":")
    if len(data) < 3:
        return
        
    opt = data[1]
    target_user_id = int(data[2])
    clicker_user_id = callback_query.from_user.id
    
    if clicker_user_id != target_user_id:
        await callback_query.answer("This verification challenge is not yours!", show_alert=True)
        return
        
    if await db.is_verified(clicker_user_id):
        await callback_query.answer("You are already verified!")
        await callback_query.message.edit_text("You are already verified!")
        return
        
    pending = await db.pending.find_one({"_id": clicker_user_id})
    if not pending:
        await callback_query.answer("Challenge expired. Please try again.", show_alert=True)
        return
        
    if opt == pending["answer"]:
        username = callback_query.from_user.username or f"user_{clicker_user_id}"
        await db.verify_user(clicker_user_id, username)
        await callback_query.answer("Verification successful!", show_alert=True)
        await callback_query.message.edit_text(
            "**Verification Successful!**\n\n"
            "You can now use the following commands:\n"
            "• `/gf <link>` or `/gofile <link>` — Mirror to Gofile\n"
            "• `/pd <link>` or `/pixeldrain <link>` — Mirror to Pixeldrain\n"
            "• `/leech <link>` — Leech directly to Telegram"
        )
        await db.pending.delete_one({"_id": clicker_user_id})
    else:
        # Wrong answer, regenerate challenge
        a, b, new_options = await db.create_challenge(clicker_user_id)
        await callback_query.answer("Incorrect answer. Please try again!", show_alert=True)
        await edit_styled_message(
            chat_id=callback_query.message.chat.id,
            message_id=callback_query.message.id,
            text=(
                f"**Please verify you are human**\n\n"
                f"To prevent spam, please solve this simple math challenge:\n\n"
                f"**{a} + {b} = ?**\n\n"
                f"Select the correct answer below:"
            ),
            reply_markup=get_verify_keyboard(clicker_user_id, new_options)
        )

# Callback Query: User Quota
@Client.on_callback_query(filters.regex(r"^user_quota$"))
async def user_quota_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id == config.owner_id:
        await callback_query.answer("Your Quota: Unlimited!", show_alert=True)
        return
        
    user_data = await db.users.find_one({"_id": user_id})
    quota_left = user_data.get("quota_left", 10 * 1024 * 1024 * 1024) if user_data else 10 * 1024 * 1024 * 1024
    readable = UI.human_size(quota_left)
    await callback_query.answer(f"Your Remaining Quota: {readable} of 10.00 GB", show_alert=True)
