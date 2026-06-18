import os
import time
import logging
import io
import asyncio
import aiohttp
import random
import uuid
import json
from typing import Optional, Tuple, Dict, Any, List, Union
from random import choice
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from motor.motor_asyncio import AsyncIOMotorClient

# ---------------------------------------------------------
# 1. LOGGING SYSTEM SETUP
# ---------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("/root/simple-mirror-bot/bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("MirrorBot")

# ---------------------------------------------------------
# 2. CONFIGURATION LOADER
# ---------------------------------------------------------
class Config:
    def __init__(self) -> None:
        load_dotenv("/root/simple-mirror-bot/config.env")
        self.bot_token: str = self._get_required("BOT_TOKEN")
        self.api_id: int = int(self._get_required("API_ID"))
        self.api_hash: str = self._get_required("API_HASH")
        self.owner_id: int = int(self._get_required("OWNER_ID"))
        self.database_url: str = self._get_required("DATABASE_URL")
        self.gofile_key: str = os.getenv("GOFILE_API_KEY", "")
        self.pixeldrain_key: str = os.getenv("PIXELDRAIN_API_KEY", "")
        
        # Parse authorized chats
        auth_chats_raw = os.getenv("AUTHORIZED_CHATS", "")
        self.auth_chats: List[int] = [
            int(x.strip()) for x in auth_chats_raw.split() if x.strip()
        ]
        if self.owner_id not in self.auth_chats:
            self.auth_chats.append(self.owner_id)

    @staticmethod
    def _get_required(var_name: str) -> str:
        val = os.getenv(var_name)
        if not val:
            logger.critical(f"Missing required environment variable: {var_name}")
            raise ValueError(f"Required configuration '{var_name}' is not set.")
        return val

try:
    config = Config()
except Exception as e:
    logger.critical(f"Configuration initialization failed: {e}")
    raise

# Initialize bot client
app = Client(
    "simple_mirror_bot",
    api_id=config.api_id,
    api_hash=config.api_hash,
    bot_token=config.bot_token,
    workdir="/root/simple-mirror-bot"
)

DOWNLOAD_DIR = "/root/simple-mirror-bot/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------------------------------------------------------
# 3. DATABASE MANAGER
# ---------------------------------------------------------
class DatabaseManager:
    def __init__(self, uri: str) -> None:
        self.client: AsyncIOMotorClient = AsyncIOMotorClient(uri)
        self.db = self.client["simple_mirror_bot"]
        self.users = self.db["users"]
        self.pending = self.db["pending_verifications"]
        logger.info("Connected to MongoDB cluster successfully.")

    async def is_verified(self, user_id: int) -> bool:
        if user_id == config.owner_id or user_id in config.auth_chats:
            return True
        user = await self.users.find_one({"_id": user_id})
        return user is not None and user.get("verified", False)

    async def create_challenge(self, user_id: int) -> Tuple[int, int, List[str]]:
        a = random.randint(2, 9)
        b = random.randint(2, 9)
        answer = a + b
        
        incorrect = set()
        while len(incorrect) < 3:
            wrong = answer + random.choice([-3, -2, -1, 1, 2, 3, 4])
            if wrong > 0 and wrong != answer:
                incorrect.add(wrong)
                
        options = list(incorrect) + [answer]
        random.shuffle(options)
        options_str = [str(x) for x in options]
        
        await self.pending.update_one(
            {"_id": user_id},
            {
                "$set": {
                    "answer": str(answer),
                    "created_at": time.time(),
                    "a": a,
                    "b": b,
                    "options": options_str
                }
            },
            upsert=True
        )
        logger.info(f"Generated verification challenge for user {user_id}: {a} + {b} = {answer} | Options: {options_str}")
        return a, b, options_str

    async def verify_user(self, user_id: int, username: str) -> None:
        await self.users.update_one(
            {"_id": user_id},
            {
                "$set": {
                    "verified": True,
                    "username": username,
                    "verified_at": time.time()
                },
                "$setOnInsert": {
                    "quota_left": 10 * 1024 * 1024 * 1024  # 10 GB
                }
            },
            upsert=True
        )
        logger.info(f"User {user_id} (@{username}) successfully verified with 10GB quota.")

db = DatabaseManager(config.database_url)

# Global trackers for active tasks (cancellation)
active_tasks: Dict[str, asyncio.Task] = {}
active_tasks_meta: Dict[str, Dict[str, Any]] = {}
THUMBNAIL_DIR = "/root/simple-mirror-bot/thumbnails"
os.makedirs(THUMBNAIL_DIR, exist_ok=True)

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

# Helper: Split large files
async def split_file(file_path: str, max_size: int) -> List[str]:
    def _split():
        parts = []
        chunk_size = 10 * 1024 * 1024  # 10MB chunk
        file_dir = os.path.dirname(file_path)
        base_name = os.path.basename(file_path)
        
        part_idx = 1
        current_written = 0
        out_file = None
        
        try:
            with open(file_path, 'rb') as infile:
                while True:
                    chunk = infile.read(chunk_size)
                    if not chunk:
                        break
                        
                    if out_file is None:
                        part_name = f"{base_name}.{part_idx:03d}"
                        part_path = os.path.join(file_dir, part_name)
                        out_file = open(part_path, 'wb')
                        parts.append(part_path)
                        
                    out_file.write(chunk)
                    current_written += len(chunk)
                    
                    if current_written >= max_size:
                        out_file.close()
                        out_file = None
                        current_written = 0
                        part_idx += 1
        finally:
            if out_file:
                out_file.close()
        return parts

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _split)

# Helper: Progress file wrapper for uploads
class progress_file_wrapper(io.IOBase):
    def __init__(self, file_path, callback, *args):
        super().__init__()
        self.file_path = file_path
        self.file_size = os.path.getsize(file_path)
        self.file = open(file_path, 'rb')
        self.callback = callback
        self.args = args
        self.uploaded = 0
        self.loop = asyncio.get_running_loop()

    def read(self, size=-1):
        chunk = self.file.read(size)
        if chunk:
            self.uploaded += len(chunk)
            asyncio.run_coroutine_threadsafe(
                self.callback(self.uploaded, self.file_size, *self.args),
                self.loop
            )
        return chunk

    def seek(self, offset, whence=io.SEEK_SET):
        self.file.seek(offset, whence)
        if offset == 0 and whence == io.SEEK_SET:
            self.uploaded = 0

    def tell(self):
        return self.file.tell()

    def close(self):
        self.file.close()

    def readable(self):
        return True

    def seekable(self):
        return True

    def writable(self):
        return False

# Helper: Upload progress callback
async def upload_progress_callback(current: int, total: int, status_msg: Message, start_time: float, filename: str, task_id: str, url: str) -> None:
    now = time.time()
    if not hasattr(upload_progress_callback, "last_edit"):
        upload_progress_callback.last_edit = 0.0
    if now - upload_progress_callback.last_edit > 4.0:
        txt = UI.build_progress_text("Uploading file", filename, current, total, start_time)
        try:
            markup = get_cancel_keyboard(task_id, url) if task_id else None
            await edit_message_with_style(status_msg, txt, reply_markup=markup)
        except Exception:
            pass
        upload_progress_callback.last_edit = now

# ---------------------------------------------------------
# 4. ENTERPRISE USER INTERFACE & UTILITIES
# ---------------------------------------------------------
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
        start_time: float
    ) -> str:
        bar, pct = UI.progress_bar(current, total)
        now = time.time()
        elapsed = now - start_time
        speed = current / elapsed if elapsed > 0 else 0.0
        eta = (total - current) / speed if speed > 0 and total > 0 else 0.0
        eta_str = time.strftime('%H:%M:%S', time.gmtime(eta)) if eta > 0 else "00:00:00"
        
        return (
            f"**{title}...**\n\n"
            f"• **File:** `{filename}`\n"
            f"• **Progress:** `[{bar}]` **{pct:.1f}%**\n"
            f"• **Speed:** `{UI.human_size(speed)}/s` | **ETA:** `{eta_str}`\n"
            f"• **Size:** `{UI.human_size(current)}` / `{UI.human_size(total)}`"
        )

# ---------------------------------------------------------
# 5. RETRY-ABLE NETWORK CORE (UPLOADS & DOWNLOADS)
# ---------------------------------------------------------
class NetworkCore:
    def __init__(self) -> None:
        self.session: Optional[aiohttp.ClientSession] = None

    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            # Configure connection pool for stability
            connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
            self.session = aiohttp.ClientSession(connector=connector)
        return self.session

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("Shared ClientSession closed.")

    async def download(self, url: str, dest_dir: str, status_msg: Message, user_id: int = 0, task_id: str = "") -> str:
        session = await self.get_session()
        logger.info(f"Initiating download for user {user_id}: {url}")
        
        # Get user quota
        quota_left = 10 * 1024 * 1024 * 1024
        if user_id and user_id != config.owner_id:
            user_data = await db.users.find_one({"_id": user_id})
            if user_data:
                quota_left = user_data.get("quota_left", 10 * 1024 * 1024 * 1024)
        
        async with session.get(url, timeout=3600) as resp:
            if resp.status != 200:
                raise Exception(f"Failed to fetch file. HTTP Status: {resp.status}")
            
            content_type = resp.headers.get('content-type', '')
            preview_bytes = b''
            if 'text/html' in content_type:
                preview_bytes = await resp.content.read(10240)
                preview_text = preview_bytes.decode('utf-8', errors='ignore')
                if "Quota exceeded" in preview_text or "Too many users have viewed" in preview_text:
                    raise Exception("Google Drive quota exceeded for this file.")
                if "Access denied" in preview_text or "Access to this file is denied" in preview_text:
                    raise Exception("Access denied. The Google Drive file is private or restricted.")
                if "does not exist" in preview_text:
                    raise Exception("The requested Google Drive file does not exist or has been deleted.")
                if "Virus scan warning" in preview_text:
                    raise Exception("Google Drive virus scan confirmation required.")
            
            total_size = int(resp.headers.get('content-length', 0))
            if 'text/html' in content_type and total_size == 0:
                total_size = len(preview_bytes)
            
            # Abort early if content-length exceeds quota/limit
            if total_size > 10 * 1024 * 1024 * 1024:
                raise Exception("File size exceeds the maximum limit of 10 GB!")
            if user_id and user_id != config.owner_id and total_size > quota_left:
                raise Exception(f"File size exceeds your remaining quota ({UI.human_size(quota_left)})!")
            
            # Extract filename securely
            filename = ""
            cd = resp.headers.get('content-disposition', '')
            if 'filename=' in cd:
                parts = cd.split('filename=')
                if len(parts) > 1:
                    filename = parts[1].strip('"\'')
            if not filename:
                filename = url.split('/')[-1].split('?')[0]
            if not filename:
                filename = "downloaded_file"
            
            # Sanitize path to prevent traversal
            filename = "".join(c for c in filename if c.isalnum() or c in "._- ")
            dest_path = os.path.join(dest_dir, filename)
            
            if task_id and task_id in active_tasks_meta:
                active_tasks_meta[task_id]["file_path"] = dest_path
            
            current_size = 0
            start_time = time.time()
            last_edit = start_time
            
            with open(dest_path, 'wb') as f:
                if preview_bytes:
                    f.write(preview_bytes)
                    current_size += len(preview_bytes)
                
                async for chunk in resp.content.iter_chunked(512 * 1024): # 512KB chunks
                    f.write(chunk)
                    current_size += len(chunk)
                    
                    # Safety checks during download iteration (if content-length was missing)
                    if current_size > 10 * 1024 * 1024 * 1024:
                        raise Exception("File size exceeds the maximum limit of 10 GB!")
                    if user_id and user_id != config.owner_id and current_size > quota_left:
                        raise Exception(f"File size exceeds your remaining quota ({UI.human_size(quota_left)})!")
                    
                    now = time.time()
                    if now - last_edit > 4.0: # Prevent spam / API flood limits
                        txt = UI.build_progress_text(
                            "Downloading file", filename, current_size, total_size, start_time
                        )
                        try:
                            markup = get_cancel_keyboard(task_id, url) if task_id else None
                            await edit_message_with_style(status_msg, txt, reply_markup=markup)
                        except Exception:
                            pass
                        last_edit = now
            logger.info(f"Download complete: {dest_path} ({UI.human_size(total_size)})")
            return dest_path

    async def upload_gofile(self, file_path: str, token: str = "", status_msg: Optional[Message] = None, task_id: str = "", source_url: str = "") -> str:
        session = await self.get_session()
        logger.info(f"Uploading {file_path} to Gofile...")
        
        # Robust server selection
        server = "store1"
        try:
            async with session.get("https://api.gofile.io/servers", timeout=10) as resp:
                res = await resp.json()
                if res.get("status") == "ok":
                    server = choice(res["data"]["servers"])["name"]
        except Exception as e:
            logger.warning(f"Error fetching best gofile server: {e}. Defaulting to store1.")
            
        url = f"https://{server}.gofile.io/contents/uploadfile"
        filename = os.path.basename(file_path)
            
        # Perform request with retry
        for attempt in range(1, 4):
            try:
                data = aiohttp.FormData()
                if token:
                    data.add_field("token", token)
                
                start_time = time.time()
                if status_msg:
                    f_wrapped = progress_file_wrapper(
                        file_path, upload_progress_callback, status_msg, start_time, filename, task_id, source_url
                    )
                    data.add_field('file', f_wrapped, filename=filename)
                else:
                    with open(file_path, 'rb') as f:
                        data.add_field('file', f, filename=filename)
                        
                async with session.post(url, data=data, timeout=1800) as resp:
                    res = await resp.json()
                    if res.get("status") == "ok":
                        download_page = res["data"]["downloadPage"]
                        logger.info(f"Uploaded successfully to Gofile: {download_page}")
                        return download_page
                    raise Exception(f"Gofile error: {res}")
            except Exception as e:
                logger.error(f"Gofile upload attempt {attempt} failed: {e}")
                if attempt == 3:
                    raise
                await asyncio.sleep(2 * attempt)
        raise Exception("Gofile upload failed after multiple retries.")

    async def upload_pixeldrain(self, file_path: str, api_key: str = "", status_msg: Optional[Message] = None, task_id: str = "", source_url: str = "") -> str:
        session = await self.get_session()
        logger.info(f"Uploading {file_path} to Pixeldrain...")
        
        url = "https://pixeldrain.com/api/file"
        auth = aiohttp.BasicAuth("", api_key) if api_key else None
        filename = os.path.basename(file_path)
        
        for attempt in range(1, 4):
            try:
                data = aiohttp.FormData()
                start_time = time.time()
                if status_msg:
                    f_wrapped = progress_file_wrapper(
                        file_path, upload_progress_callback, status_msg, start_time, filename, task_id, source_url
                    )
                    data.add_field('file', f_wrapped, filename=filename)
                else:
                    with open(file_path, 'rb') as f:
                        data.add_field('file', f, filename=filename)
                        
                async with session.post(url, data=data, auth=auth, timeout=1800) as resp:
                    res = await resp.json()
                    if res.get("success"):
                        file_id = res["id"]
                        url_result = f"https://pixeldrain.com/u/{file_id}"
                        logger.info(f"Uploaded successfully to Pixeldrain: {url_result}")
                        return url_result
                    raise Exception(f"Pixeldrain error: {res}")
            except Exception as e:
                logger.error(f"Pixeldrain upload attempt {attempt} failed: {e}")
                if attempt == 3:
                    raise
                await asyncio.sleep(2 * attempt)
        raise Exception("Pixeldrain upload failed after multiple retries.")

network = NetworkCore()

# Cooldowns storage for anti-spam (user_id: last_command_time)
cooldowns: Dict[int, float] = {}

# Global cache for temporary mirror query URLs (query_id: {url, user_id, timestamp})
mirror_queries: Dict[str, Dict[str, Any]] = {}

# Helper: Serialize pyrogram InlineKeyboardMarkup to a Bot API dictionary with styles
def serialize_keyboard(markup: Optional[InlineKeyboardMarkup]) -> Optional[dict]:
    if not markup:
        return None
    rows = []
    for row in markup.inline_keyboard:
        row_btns = []
        for btn in row:
            btn_dict = {"text": btn.text}
            if btn.callback_data:
                btn_dict["callback_data"] = btn.callback_data
            if btn.url:
                btn_dict["url"] = btn.url
            
            # Map button text or callback data to styles
            cb_data = btn.callback_data or ""
            if cb_data.startswith("cancel:") or cb_data.startswith("mirror_choice:cancel:"):
                btn_dict["style"] = "danger"
            elif cb_data == "user_quota" or cb_data.startswith("admin:"):
                btn_dict["style"] = "primary"
            elif cb_data.startswith("verify:"):
                btn_dict["style"] = "success"
                
            row_btns.append(btn_dict)
        rows.append(row_btns)
    return {"inline_keyboard": rows}

# Helper: Deserialize a Bot API keyboard dictionary back to pyrogram InlineKeyboardMarkup
def deserialize_keyboard(reply_markup: Union[dict, InlineKeyboardMarkup, None]) -> Optional[InlineKeyboardMarkup]:
    if not reply_markup:
        return None
    if isinstance(reply_markup, InlineKeyboardMarkup):
        return reply_markup
        
    rows = []
    inline_keyboard = reply_markup.get("inline_keyboard", [])
    for row in inline_keyboard:
        row_btns = []
        for btn in row:
            row_btns.append(InlineKeyboardButton(
                text=btn.get("text", ""),
                callback_data=btn.get("callback_data"),
                url=btn.get("url")
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
        session = await network.get_session()
        async with session.post(url, json=payload) as resp:
            if resp.status == 200:
                res = await resp.json()
                if res.get("ok"):
                    raw_msg = res["result"]
                    from pyrogram.types import Chat
                    from pyrogram.enums import ChatType
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
        session = await network.get_session()
        async with session.post(url, json=payload) as resp:
            if resp.status == 200:
                res = await resp.json()
                if res.get("ok"):
                    if isinstance(res["result"], bool):
                        return None
                    raw_msg = res["result"]
                    from pyrogram.types import Chat
                    from pyrogram.enums import ChatType
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
# Helper: Resolve sharing links (Google Drive, Dropbox, Mediafire, Pixeldrain) to direct links
async def resolve_direct_link(url: str) -> str:
    import re
    import urllib.parse
    
    session = await network.get_session()
    
    # Follow redirects for shorteners or generic redirects first
    if not any(domain in url for domain in ["drive.google.com", "drive.usercontent.google.com", "dropbox.com", "pixeldrain.com", "mediafire.com"]):
        try:
            async with session.head(url, allow_redirects=True, timeout=10) as resp:
                url = str(resp.url)
        except Exception as e:
            logger.warning(f"Error following redirection: {e}")

    # Google Drive
    gdrive_match = re.search(r'(?:drive\.google\.com|drive\.usercontent\.google\.com)/(?:file/d/|open\?id=|download\?id=)([a-zA-Z0-9_-]+)', url)
    if gdrive_match:
        file_id = gdrive_match.group(1)
        uc_url = f"https://docs.google.com/uc?export=download&id={file_id}"
        try:
            async with session.get(uc_url, timeout=15) as resp:
                html = await resp.text()
                if "Quota exceeded" in html or "Too many users have viewed" in html:
                    raise Exception("Google Drive quota exceeded for this file.")
                
                # Check for virus warning page and confirmation form
                form_match = re.search(r'<form\s+[^>]*action="([^"]+)"[^>]*>(.*?)</form>', html, re.DOTALL | re.IGNORECASE)
                if form_match:
                    action = form_match.group(1)
                    form_body = form_match.group(2)
                    inputs = re.findall(r'<input\s+[^>]+>', form_body, re.IGNORECASE)
                    params = {}
                    for inp in inputs:
                        name_m = re.search(r'name="([^"]+)"', inp, re.IGNORECASE)
                        value_m = re.search(r'value="([^"]*)"', inp, re.IGNORECASE)
                        if name_m and value_m:
                            params[name_m.group(1)] = value_m.group(1)
                    if params:
                        return f"{action}?{urllib.parse.urlencode(params)}"
        except Exception as e:
            logger.warning(f"Error resolving GDrive confirmation link: {e}")
            raise e
        return f"https://docs.google.com/uc?export=download&id={file_id}"

    # Dropbox
    if 'dropbox.com' in url:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        qs['dl'] = ['1']
        new_query = urllib.parse.urlencode(qs, doseq=True)
        netloc = parsed.netloc.replace('www.dropbox.com', 'dl.dropboxusercontent.com')
        return urllib.parse.urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, new_query, parsed.fragment))

    # Pixeldrain
    pixeldrain_match = re.search(r'pixeldrain\.com/u/([a-zA-Z0-9_-]+)', url)
    if pixeldrain_match:
        file_id = pixeldrain_match.group(1)
        return f"https://pixeldrain.com/api/file/{file_id}"

    # MediaFire
    if 'mediafire.com' in url:
        try:
            async with session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    match = re.search(r'aria-label="Download file"\s+href="([^"]+)"', html)
                    if match:
                        return match.group(1)
                    match = re.search(r'https?://download[0-9]*\.mediafire\.com/[^\s\'"]+', html)
                    if match:
                        return match.group(0)
                    match = re.search(r'href="((?:https?://)?download[^"]+mediafire[^"]+)"', html, re.IGNORECASE)
                    if match:
                        return match.group(1)
        except Exception as e:
            logger.error(f"Error scraping Mediafire link: {e}")

    return url

# Helper: Fetch file size from link using HEAD or GET
async def get_link_size(url: str) -> Optional[int]:
    try:
        session = await network.get_session()
        async with session.head(url, allow_redirects=True, timeout=10) as resp:
            if resp.status == 200:
                size = resp.headers.get('content-length')
                if size:
                    return int(size)
        async with session.get(url, allow_redirects=True, timeout=10) as resp:
            if resp.status == 200:
                size = resp.headers.get('content-length')
                if size:
                    return int(size)
    except Exception as e:
        logger.error(f"Error checking link size: {e}")
    return None

# ---------------------------------------------------------
# 6. HANDLERS AND CONTROLLERS
# ---------------------------------------------------------
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

# Command: Start
@app.on_message(filters.command("start"))
@verified_only
async def start_cmd(client: Client, message: Message):
    welcome_text = (
        "**Welcome to Mirror Bot**\n\n"
        "Use the following commands to mirror or leech files:\n"
        "• `/gf <link>` or `/gofile <link>` — Mirror to **Gofile**\n"
        "• `/pd <link>` or `/pixeldrain <link>` — Mirror to **Pixeldrain**\n"
        "• `/leech <link>` — Download file and send it directly to your Telegram chat.\n\n"
        "This bot is free and ad-free."
    )
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "Check Quota", "callback_data": "user_quota", "style": "primary"},
                {"text": "Bot Owner", "url": f"tg://user?id={config.owner_id}", "style": "success"}
            ]
        ]
    }
    await send_styled_message(
        chat_id=message.chat.id,
        text=welcome_text,
        reply_markup=reply_markup,
        reply_to_message_id=message.id
    )

# Command: Help
@app.on_message(filters.command("help"))
@verified_only
async def help_cmd(client: Client, message: Message):
    help_text = (
        "**Mirror Bot Guide**\n\n"
        "This bot allows you to download files from direct links and mirror them to cloud storage or leech them directly to Telegram.\n\n"
        "**Mirror Commands (Upload to Cloud):**\n"
        "• `/mirror <link>` — Select where to mirror (Gofile or Pixeldrain)\n"
        "• `/gf <link>` or `/gofile <link>` — Mirror directly to **Gofile**\n"
        "• `/pd <link>` or `/pixeldrain <link>` — Mirror directly to **Pixeldrain**\n\n"
        "**Leech Commands (Send to Telegram):**\n"
        "• `/leech <link>` — Download and send the file directly to this chat\n\n"
        "**Thumbnail Management (Leech Only):**\n"
        "• Reply to any photo with `/setthumb` to set a custom thumbnail\n"
        "• Send `/delthumb` to delete your custom thumbnail\n\n"
        "**Quota & Account:**\n"
        "• `/quota` — Check your remaining daily quota\n\n"
        "**Tip:**\n"
        "You can also reply to any message containing a link with any of the commands above."
    )
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "Check Quota", "callback_data": "user_quota", "style": "primary"},
                {"text": "Bot Owner", "url": f"tg://user?id={config.owner_id}", "style": "success"}
            ]
        ]
    }
    await send_styled_message(
        chat_id=message.chat.id,
        text=help_text,
        reply_markup=reply_markup,
        reply_to_message_id=message.id
    )

# Callback Query: Verification
@app.on_callback_query(filters.regex(r"^verify:"))
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
@app.on_callback_query(filters.regex(r"^user_quota$"))
async def user_quota_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id == config.owner_id:
        await callback_query.answer("Your Quota: Unlimited!", show_alert=True)
        return
        
    user_data = await db.users.find_one({"_id": user_id})
    quota_left = user_data.get("quota_left", 10 * 1024 * 1024 * 1024) if user_data else 10 * 1024 * 1024 * 1024
    readable = UI.human_size(quota_left)
    await callback_query.answer(f"Your Remaining Quota: {readable} of 10.00 GB", show_alert=True)

# Shared Mirror Worker (Downloads, then Uploads to target service)
async def run_mirror_worker(client: Client, status_msg: Message, url: str, user_id: int, service: str):
    file_path = None
    task_id = f"{status_msg.chat.id}_{status_msg.id}"
    
    try:
        # Register task
        active_tasks[task_id] = asyncio.current_task()
        active_tasks_meta[task_id] = {
            "user_id": user_id,
            "file_path": None,
            "status_msg": status_msg
        }

        # Resolve direct link dynamically
        await edit_message_with_style(status_msg, "**Resolving link...**", reply_markup=get_cancel_keyboard(task_id, url))
        url = await resolve_direct_link(url)

        # Pre-check file size
        await edit_message_with_style(status_msg, "**Checking file size...**", reply_markup=get_cancel_keyboard(task_id, url))
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
        await edit_message_with_style(status_msg, "**Downloading file...**", reply_markup=get_cancel_keyboard(task_id, url))
        file_path = await network.download(url, DOWNLOAD_DIR, status_msg, user_id=user_id, task_id=task_id)
        filename = os.path.basename(file_path)
        if not file_path or not os.path.exists(file_path):
            raise FileNotFoundError("Downloaded file not found on server.")
        filesize = os.path.getsize(file_path)
        
        # Step 2: Upload
        if service == "gofile":
            await edit_message_with_style(status_msg, "**Uploading to Gofile...**", reply_markup=get_cancel_keyboard(task_id, url))
            link = await network.upload_gofile(file_path, config.gofile_key, status_msg, task_id, url)
            btn_text = "Gofile"
            btn_style = "success"
        elif service == "pixeldrain":
            await edit_message_with_style(status_msg, "**Uploading to Pixeldrain...**", reply_markup=get_cancel_keyboard(task_id, url))
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
            f"• **Size:** `{UI.human_size(filesize)}`"
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
            await edit_message_with_style(status_msg, "**Mirroring process cancelled by user.**")
        except Exception:
            pass
        raise
    except Exception as e:
        logger.error(f"Error executing {service} mirror worker: {e}", exc_info=True)
        try:
            await edit_message_with_style(status_msg, f"**An error occurred:**\n`{str(e)}`")
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
@app.on_message(filters.command(["gofile", "gf"]))
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
        
    status_msg = await message.reply_text("**Processing, please wait...**")
    await run_mirror_worker(client, status_msg, url, user_id, "gofile")

# Command: Pixeldrain Mirror
@app.on_message(filters.command(["pixeldrain", "pd"]))
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
        
    status_msg = await message.reply_text("**Processing, please wait...**")
    await run_mirror_worker(client, status_msg, url, user_id, "pixeldrain")

# Command: Mirror Choice Trigger (asks user where to mirror)
@app.on_message(filters.command("mirror"))
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
@app.on_callback_query(filters.regex(r"^mirror_choice:"))
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
    
    # Edit message to start progress updates
    status_msg = await edit_styled_message(
        chat_id=callback_query.message.chat.id,
        message_id=callback_query.message.id,
        text="**Processing, please wait...**"
    )
    if not status_msg:
        status_msg = callback_query.message
        await status_msg.edit_text("**Processing, please wait...**")
        
    asyncio.create_task(run_mirror_worker(client, status_msg, url, clicker_user_id, choice))

# Callback progress for telegram leeching
async def leech_upload_progress(current: int, total: int, message: Message, start_time: float, filename: str, task_id: str = "", url: str = "") -> None:
    now = time.time()
    if not hasattr(leech_upload_progress, "last_edit"):
        leech_upload_progress.last_edit = 0.0
    if now - leech_upload_progress.last_edit > 4.0:
        txt = UI.build_progress_text("Uploading to Telegram", filename, current, total, start_time)
        try:
            markup = get_cancel_keyboard(task_id, url) if task_id else None
            await edit_message_with_style(message, txt, reply_markup=markup)
        except Exception:
            pass
        leech_upload_progress.last_edit = now

# Command: Leech
@app.on_message(filters.command("leech"))
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
    status_msg = await message.reply_text("**Processing, please wait...**")
    
    file_path = None
    task_id = f"{status_msg.chat.id}_{status_msg.id}"
    
    try:
        # Register task
        active_tasks[task_id] = asyncio.current_task()
        active_tasks_meta[task_id] = {
            "user_id": user_id,
            "file_path": None,
            "status_msg": status_msg
        }

        # Resolve direct link dynamically
        await status_msg.edit_text("**Resolving link...**", reply_markup=get_cancel_keyboard(task_id, url))
        url = await resolve_direct_link(url)

        # Pre-check file size
        await status_msg.edit_text("**Checking file size...**", reply_markup=get_cancel_keyboard(task_id, url))
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
        await status_msg.edit_text("**Downloading file...**", reply_markup=get_cancel_keyboard(task_id, url))
        file_path = await network.download(url, DOWNLOAD_DIR, status_msg, user_id=user_id, task_id=task_id)
        filename = os.path.basename(file_path)
        filesize = os.path.getsize(file_path)
        
        # Retrieve custom thumbnail if exists
        thumb_path = os.path.join(THUMBNAIL_DIR, f"{user_id}.jpg")
        thumb = thumb_path if os.path.exists(thumb_path) else None
        
        # Step 2: Upload to Telegram
        await status_msg.edit_text(f"**Uploading to Telegram...**", reply_markup=get_cancel_keyboard(task_id, url))
        
        max_upload_size = 1999 * 1024 * 1024  # 1.99 GB
        if filesize > max_upload_size:
            await status_msg.edit_text(f"**Large file detected ({UI.human_size(filesize)}).**\nSplitting file into parts...", reply_markup=get_cancel_keyboard(task_id, url))
            split_files = await split_file(file_path, max_upload_size)
            
            total_parts = len(split_files)
            for idx, part_path in enumerate(split_files, 1):
                part_size = os.path.getsize(part_path)
                part_name = os.path.basename(part_path)
                await status_msg.edit_text(f"**Sending Part {idx}/{total_parts}...**\n`{part_name}`", reply_markup=get_cancel_keyboard(task_id, url))
                
                start_time = time.time()
                await client.send_document(
                    chat_id=message.chat.id,
                    document=part_path,
                    file_name=part_name,
                    caption=f"**Part {idx}/{total_parts}**\n\n• **Filename:** `{filename}`\n• **Part Size:** `{UI.human_size(part_size)}`\n• **Total Size:** `{UI.human_size(filesize)}`",
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
                caption=f"**Leech Completed**\n\n• **Filename:** `{filename}`\n• **Size:** `{UI.human_size(filesize)}`",
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
            await status_msg.edit_text("**Leech process cancelled by user.**")
        except Exception:
            pass
        raise
    except Exception as e:
        logger.error(f"Error executing leech command: {e}", exc_info=True)
        try:
            await status_msg.edit_text(f"**An error occurred:**\n`{str(e)}`")
        except Exception:
            pass
    finally:
        # Cleanup task tracking
        active_tasks.pop(task_id, None)
        meta = active_tasks_meta.pop(task_id, None)
        
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
@app.on_message(filters.command("quota"))
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
@app.on_message(filters.command("setthumb"))
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
@app.on_message(filters.command("delthumb"))
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
@app.on_callback_query(filters.regex(r"^cancel:"))
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

# Command: Ping (Owner Only)
@app.on_message(filters.command("ping") & filters.user(config.owner_id))
async def ping_cmd(client: Client, message: Message):
    start_time = time.time()
    status_msg = await message.reply_text("**Pinging...**")
    latency = (time.time() - start_time) * 1000
    await status_msg.edit_text(
        f"**Pong!**\nLatency: `{latency:.2f} ms`"
    )

# Command: Speedtest (Owner Only)
@app.on_message(filters.command("speedtest") & filters.user(config.owner_id))
async def speedtest_cmd(client: Client, message: Message):
    status_msg = await message.reply_text("**Running Speedtest...**\nPlease wait, this process takes about 15-30 seconds.")
    
    try:
        process = await asyncio.create_subprocess_exec(
            '/usr/local/bin/speedtest-cli', '--simple',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0:
            result = stdout.decode().strip()
            lines = result.split('\n')
            ping_val = "N/A"
            down_val = "N/A"
            up_val = "N/A"
            for line in lines:
                if line.startswith("Ping:"):
                    ping_val = line.split(":", 1)[1].strip()
                elif line.startswith("Download:"):
                    down_val = line.split(":", 1)[1].strip()
                elif line.startswith("Upload:"):
                    up_val = line.split(":", 1)[1].strip()
            
            response = (
                f"**Speedtest Results**\n\n"
                f"• **Ping:** `{ping_val}`\n"
                f"• **Download:** `{down_val}`\n"
                f"• **Upload:** `{up_val}`"
            )
            await status_msg.edit_text(response)
        else:
            err = stderr.decode().strip()
            await status_msg.edit_text(f"**Speedtest failed with code {process.returncode}:**\n`{err}`")
    except Exception as e:
        logger.error(f"Error running speedtest: {e}", exc_info=True)
        await status_msg.edit_text(f"**Speedtest error:**\n`{str(e)}`")

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

# Command: Admin Dashboard
@app.on_message(filters.command("admin") & filters.user(config.owner_id))
async def admin_cmd(client: Client, message: Message):
    await message.reply_text(
        "**Admin Control Panel**\n\n"
        "Welcome Owner! Please select a management option below:",
        reply_markup=get_admin_keyboard()
    )

# Command: Add Quota
@app.on_message(filters.command("addquota") & filters.user(config.owner_id))
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
@app.on_message(filters.command("deluser") & filters.user(config.owner_id))
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
@app.on_callback_query(filters.regex(r"^admin:"))
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
@app.on_message(filters.command("user") & filters.user(config.owner_id))
async def user_manager_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Use: `/user <user_id>` or `/user <@username>`")
        return
        
    query_str = message.command[1].strip()
    await show_user_management(message, query_str)

# Callback Query: User Management Actions
@app.on_callback_query(filters.regex(r"^usermg:"))
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
@app.on_message(filters.command("setquota") & filters.user(config.owner_id))
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
@app.on_message(filters.command("broadcast") & filters.user(config.owner_id))
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

# Clean shutdown lifecycle
async def shutdown() -> None:
    logger.info("Shutting down resources...")
    await network.close()

if __name__ == "__main__":
    from pyrogram import idle
    
    async def main():
        logger.info("🤖 Starting Enterprise Mirror Bot Core...")
        await app.start()
        
        # Set bot commands
        try:
            from pyrogram.types import BotCommand
            commands = [
                BotCommand("start", "Start the bot and verify"),
                BotCommand("help", "Bot usage guide"),
                BotCommand("mirror", "Mirror file to cloud (Gofile / Pixeldrain)"),
                BotCommand("leech", "Download and send file directly to Telegram"),
                BotCommand("gf", "Mirror directly to Gofile"),
                BotCommand("pd", "Mirror directly to Pixeldrain"),
                BotCommand("setthumb", "Set custom thumbnail for leeching"),
                BotCommand("delthumb", "Delete custom thumbnail"),
                BotCommand("quota", "Check your remaining daily quota")
            ]
            await app.set_bot_commands(commands)
            logger.info("✅ Bot commands registered successfully.")
        except Exception as e:
            logger.error(f"❌ Failed to set bot commands: {e}")
            
        await idle()
        await app.stop()
        
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.run_until_complete(shutdown())
