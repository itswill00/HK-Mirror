from pyrogram import Client
from pyrogram.types import Message
from database import db
from helpers.ui import send_styled_message, get_verify_keyboard

# Filter: Verified check decorator
def verified_only(func):
    async def wrapper(client: Client, message: Message):
        user_id = message.from_user.id if message.from_user else None
        if not user_id:
            return
        
        # Bypass or verified
        if await db.is_verified(user_id):
            return await func(client, message)
        
        # Check active verification challenge
        pending = await db.pending.find_one({"_id": user_id})
        if pending and "options" in pending:
            a = pending["a"]
            b = pending["b"]
            options = pending["options"]
        else:
            a, b, options = await db.create_challenge(user_id)
            
        await send_styled_message(
            chat_id=message.chat.id,
            text=(
                f"**Please verify you are human**\n\n"
                f"To prevent spam, please solve this simple math challenge:\n\n"
                f"**{a} + {b} = ?**\n\n"
                f"Select the correct answer below:"
            ),
            reply_markup=get_verify_keyboard(user_id, options),
            reply_to_message_id=message.id
        )
    return wrapper
