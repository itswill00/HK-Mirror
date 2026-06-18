import asyncio
import logging
from pyrogram import Client, idle

from config import config
from helpers.network import network

logger = logging.getLogger("MirrorBot")

# Initialize client with plugins loading from "modules" directory
app = Client(
    "simple_mirror_bot",
    api_id=config.api_id,
    api_hash=config.api_hash,
    bot_token=config.bot_token,
    workdir="/root/simple-mirror-bot",
    plugins=dict(root="modules")
)

# Clean shutdown lifecycle
async def shutdown() -> None:
    logger.info("Shutting down resources...")
    await network.close()

if __name__ == "__main__":
    async def main():
        logger.info("🤖 Starting Modular Enterprise Mirror Bot...")
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
