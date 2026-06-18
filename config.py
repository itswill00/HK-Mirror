import os
import time
import logging
from typing import List
from dotenv import load_dotenv

# Set up logging early so it is shared
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

DOWNLOAD_DIR = "/root/simple-mirror-bot/downloads"
THUMBNAIL_DIR = "/root/simple-mirror-bot/thumbnails"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(THUMBNAIL_DIR, exist_ok=True)

bot_start_time = time.time()
