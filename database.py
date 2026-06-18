import time
import random
import logging
from typing import Tuple, List
from motor.motor_asyncio import AsyncIOMotorClient
from config import config

logger = logging.getLogger("MirrorBot.Database")

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
