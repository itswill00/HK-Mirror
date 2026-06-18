import time
import logging
from typing import Union, Optional, List, Tuple
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ChatType
from config import config
from helpers.trackers import active_tasks_meta

logger = logging.getLogger("MirrorBot.UI")

class UI:
    @staticmethod
    def human_size(size: float) -> str:
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024.0:
                return f"{size:.2f} {unit}"
            size /= 1024.0
        return f"{size:.2f} PB"

    @staticmethod
    def progress_bar(current: int, total: int) -> Tuple[str, float]:
        percentage = (current / total) * 100.0 if total > 0 else 0.0
        completed = int(percentage // 10)
        # Friendly and clean progress indicators
        bar = "■" * completed + "□" * (10 - completed)
        return bar, percentage

    @staticmethod
    def build_progress_text(
        title: str,
        filename: str,
        current: int,
        total: int,
        start_time: float,
        user_mention: str = ""
    ) -> str:
        bar, pct = UI.progress_bar(current, total)
        now = time.time()
        elapsed = now - start_time
        speed = current / elapsed if elapsed > 0 else 0.0
        eta = (total - current) / speed if speed > 0 and total > 0 else 0.0
        eta_str = time.strftime('%H:%M:%S', time.gmtime(eta)) if eta > 0 else "00:00:00"
        
        txt = (
            f"**{title}...**\n\n"
            f"• **File:** `{filename}`\n"
            f"• **Progress:** `[{bar}]` **{pct:.1f}%**\n"
            f"• **Speed:** `{UI.human_size(speed)}/s` | **ETA:** `{eta_str}`\n"
            f"• **Size:** `{UI.human_size(current)}` / `{UI.human_size(total)}`"
        )
        if user_mention:
            txt += f"\n• **User:** {user_mention}"
        return txt

# Helper: Build Cancel Inline Keyboard (supporting source link)
def get_cancel_keyboard(task_id: str, url: str = "") -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton("Cancel", callback_data=f"cancel:{task_id}")
    ]
    if url and (url.startswith("http://") or url.startswith("https://")):
        buttons.append(InlineKeyboardButton("Open Link", url=url))
    return InlineKeyboardMarkup([buttons])

# Helper: Build Mirror Success Inline Keyboard
def get_mirror_success_keyboard(gofile_url: str, pixeldrain_url: str) -> InlineKeyboardMarkup:
    buttons = []
    if gofile_url:
        buttons.append(InlineKeyboardButton("Gofile", url=gofile_url))
    if pixeldrain_url:
        buttons.append(InlineKeyboardButton("Pixeldrain", url=pixeldrain_url))
    return InlineKeyboardMarkup([buttons])

# Helper: Get Verification Inline Keyboard
def get_verify_keyboard(user_id: int, options: List[str]) -> dict:
    styles = ["primary", "success", "danger", "primary"]
    rows = []
    row = []
    for idx, opt in enumerate(options):
        row.append({
            "text": opt,
            "callback_data": f"verify:{opt}:{user_id}",
            "style": styles[idx % len(styles)]
        })
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}

# Helper: Get Admin Menu Keyboard
def get_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("List Users", callback_data="admin:users"),
            InlineKeyboardButton("Stats", callback_data="admin:stats")
        ],
        [
            InlineKeyboardButton("Add Quota", callback_data="admin:addquota"),
            InlineKeyboardButton("Delete User", callback_data="admin:deluser")
        ]
    ])

# Helper: Serialize pyrogram keyboard to HTTP Bot API JSON
def serialize_keyboard(markup: Optional[InlineKeyboardMarkup]) -> Optional[dict]:
    if not markup:
        return None
    serialized_rows = []
    for row in markup.inline_keyboard:
        row_btns = []
        for btn in row:
            btn_dict = {"text": btn.text}
            if btn.url:
                btn_dict["url"] = btn.url
            elif btn.callback_data:
                btn_dict["callback_data"] = btn.callback_data
            
            # Extract Telegram custom stylings safely if applicable
            style = getattr(btn, "style", None)
            if style:
                btn_dict["style"] = style
            row_btns.append(btn_dict)
        serialized_rows.append(row_btns)
    return {"inline_keyboard": serialized_rows}

# Helper: Deserialize JSON keyboard to standard Pyrogram object
def deserialize_keyboard(reply_markup: Union[dict, InlineKeyboardMarkup, None]) -> Optional[InlineKeyboardMarkup]:
    if not reply_markup:
        return None
    if isinstance(reply_markup, InlineKeyboardMarkup):
        return reply_markup
    
    rows = []
    for row in reply_markup.get("inline_keyboard", []):
        row_btns = []
        for btn in row:
            cb_data = btn.get("callback_data")
            # Exclude dangerous cancel button callbacks to avoid crash loop
            if cb_data and (cb_data.startswith("cancel:") or cb_data.startswith("mirror_choice:cancel:")):
                continue
            row_btns.append(InlineKeyboardButton(
                text=btn.get("text"),
                url=btn.get("url"),
                callback_data=cb_data
            ))
        rows.append(row_btns)
    return InlineKeyboardMarkup(rows)

# Helper: Send message using HTTP Bot API (supports styled inline buttons)
async def send_styled_message(chat_id: Union[int, str], text: str, reply_markup: Union[dict, InlineKeyboardMarkup] = None, reply_to_message_id: int = None) -> Optional[Message]:
    if isinstance(reply_markup, InlineKeyboardMarkup):
        reply_markup = serialize_keyboard(reply_markup)
        
    url = f"https://api.telegram.org/bot{config.bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id

    try:
        from helpers.network import network
        session = await network.get_session()
        async with session.post(url, json=payload) as resp:
            if resp.status == 200:
                res = await resp.json()
                if res.get("ok"):
                    raw_msg = res["result"]
                    from pyrogram.types import Chat
                    from bot import app
                    chat_data = raw_msg.get("chat", {})
                    chat_type_str = chat_data.get("type", "supergroup").upper()
                    chat_type = getattr(ChatType, chat_type_str, ChatType.SUPERGROUP)
                    chat = Chat(id=chat_data.get("id"), type=chat_type)
                    return Message(
                        client=app,
                        id=raw_msg.get("message_id"),
                        chat=chat,
                        text=raw_msg.get("text", "")
                    )
            else:
                body = await resp.text()
                logger.error(f"HTTP Bot API sendMessage failed: {resp.status} - {body}")
    except Exception as e:
        logger.error(f"Error in send_styled_message: {e}", exc_info=True)
        
    # Fallback to standard Pyrogram send_message
    try:
        from bot import app
        fallback_markup = deserialize_keyboard(reply_markup)
        return await app.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=fallback_markup,
            reply_to_message_id=reply_to_message_id
        )
    except Exception as ex:
        logger.error(f"Fallback send_message also failed: {ex}")
    return None

# Helper: Edit message text using HTTP Bot API (supports styled inline buttons)
async def edit_styled_message(chat_id: Union[int, str], message_id: int, text: str, reply_markup: Union[dict, InlineKeyboardMarkup] = None) -> Optional[Message]:
    if isinstance(reply_markup, InlineKeyboardMarkup):
        reply_markup = serialize_keyboard(reply_markup)
        
    url = f"https://api.telegram.org/bot{config.bot_token}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        from helpers.network import network
        session = await network.get_session()
        async with session.post(url, json=payload) as resp:
            if resp.status == 200:
                res = await resp.json()
                if res.get("ok"):
                    if isinstance(res["result"], bool):
                        return None
                    raw_msg = res["result"]
                    from pyrogram.types import Chat
                    from bot import app
                    chat_data = raw_msg.get("chat", {})
                    chat_type_str = chat_data.get("type", "supergroup").upper()
                    chat_type = getattr(ChatType, chat_type_str, ChatType.SUPERGROUP)
                    chat = Chat(id=chat_data.get("id"), type=chat_type)
                    return Message(
                        client=app,
                        id=raw_msg.get("message_id"),
                        chat=chat,
                        text=raw_msg.get("text", "")
                    )
            else:
                body = await resp.text()
                logger.error(f"HTTP Bot API editMessageText failed: {resp.status} - {body}")
    except Exception as e:
        logger.error(f"Error in edit_styled_message: {e}", exc_info=True)
    return None

# Helper: Edit message text with automatic styling and standard Pyrogram fallback
async def edit_message_with_style(message: Message, text: str, reply_markup: Union[dict, InlineKeyboardMarkup] = None) -> Optional[Message]:
    try:
        new_msg = await edit_styled_message(
            chat_id=message.chat.id,
            message_id=message.id,
            text=text,
            reply_markup=reply_markup
        )
        if new_msg:
            try:
                message.text = new_msg.text
                message.reply_markup = new_msg.reply_markup
            except Exception:
                pass
            return new_msg
    except Exception as e:
        logger.error(f"Failed to edit via HTTP Bot API: {e}")
    
    # Fallback to standard Pyrogram edit (deserializing keyboard safely)
    fallback_markup = deserialize_keyboard(reply_markup)
    return await message.edit_text(text, reply_markup=fallback_markup)

# Helper: Extract URL from a message (args or reply)
def extract_url(message: Message) -> str:
    # 1. Check message arguments
    if len(message.command) > 1:
        potential_url = message.text.split(None, 1)[1].strip()
        if potential_url.startswith("http://") or potential_url.startswith("https://"):
            return potential_url
            
    # 2. Check if replying to a message
    if message.reply_to_message:
        replied = message.reply_to_message
        text = replied.text or replied.caption
        if text:
            # Look for urls in text entities
            entities = replied.entities or replied.caption_entities
            if entities:
                for entity in entities:
                    if entity.type == "url":
                        offset = entity.offset
                        length = entity.length
                        url = text[offset:offset+length].strip()
                        if url.startswith("http://") or url.startswith("https://"):
                            return url
                    elif entity.type == "text_link":
                        if entity.url and (entity.url.startswith("http://") or entity.url.startswith("https://")):
                            return entity.url
            # Fallback check raw text/caption words
            for word in text.split():
                if word.startswith("http://") or word.startswith("https://"):
                    return word
                    
    return ""

# Callback progress for gofile/pixeldrain uploads
async def upload_progress_callback(current: int, total: int, status_msg: Message, start_time: float, filename: str, task_id: str, url: str) -> None:
    now = time.time()
    if not hasattr(upload_progress_callback, "last_edit"):
        upload_progress_callback.last_edit = 0.0
    if now - upload_progress_callback.last_edit > 4.0:
        user_mention = ""
        if task_id and task_id in active_tasks_meta:
            user_mention = active_tasks_meta[task_id].get("user_mention", "")
        txt = UI.build_progress_text("Uploading file", filename, current, total, start_time, user_mention)
        try:
            markup = get_cancel_keyboard(task_id, url) if task_id else None
            await edit_message_with_style(status_msg, txt, reply_markup=markup)
        except Exception:
            pass
        upload_progress_callback.last_edit = now

# Callback progress for telegram leeching
async def leech_upload_progress(current: int, total: int, message: Message, start_time: float, filename: str, task_id: str = "", url: str = "") -> None:
    now = time.time()
    if not hasattr(leech_upload_progress, "last_edit"):
        leech_upload_progress.last_edit = 0.0
    if now - leech_upload_progress.last_edit > 4.0:
        user_mention = ""
        if task_id and task_id in active_tasks_meta:
            user_mention = active_tasks_meta[task_id].get("user_mention", "")
        txt = UI.build_progress_text("Uploading to Telegram", filename, current, total, start_time, user_mention)
        try:
            markup = get_cancel_keyboard(task_id, url) if task_id else None
            await edit_message_with_style(message, txt, reply_markup=markup)
        except Exception:
            pass
        leech_upload_progress.last_edit = now
