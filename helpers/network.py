import os
import io
import time
import re
import logging
import asyncio
import aiohttp
import urllib.parse
from random import choice
from typing import Optional, List, Tuple, Dict, Any
from pyrogram.types import Message

from config import config, DOWNLOAD_DIR
from database import db
from helpers.trackers import active_tasks_meta
from helpers.ui import UI, upload_progress_callback

logger = logging.getLogger("MirrorBot.Network")

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

# Helper: Resolve sharing links (Google Drive, Dropbox, Mediafire, Pixeldrain) to direct links
async def resolve_direct_link(url: str) -> str:
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

class NetworkCore:
    def __init__(self) -> None:
        self.session: Optional[aiohttp.ClientSession] = None

    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
            logger.info("Shared ClientSession created.")
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
                        user_mention = ""
                        if task_id and task_id in active_tasks_meta:
                            user_mention = active_tasks_meta[task_id].get("user_mention", "")
                        txt = UI.build_progress_text(
                            "Downloading file", filename, current_size, total_size, start_time, user_mention
                        )
                        try:
                            from helpers.ui import edit_message_with_style, get_cancel_keyboard
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
