import time
import asyncio
import logging
from pyrogram import Client, filters
from pyrogram.types import Message

from config import config
from helpers.decorators import verified_only

logger = logging.getLogger("MirrorBot.Start")

# Command: Start
@Client.on_message(filters.command("start"))
@verified_only
async def start_cmd(client: Client, message: Message):
    welcome_text = (
        "🤖 **Welcome to Enterprise Mirror & Leech Bot**\n\n"
        "This bot can download files from direct links and upload them to Gofile, Pixeldrain, or Leech them directly back to Telegram!\n\n"
        "• To begin, send a command with a download link.\n"
        "• Use `/help` to see all available commands and operations."
    )
    
    # Simple verification dashboard shortcut buttons
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "My Quota", "callback_data": "user_quota"}
            ]
        ]
    }
    
    from helpers.ui import send_styled_message
    await send_styled_message(
        chat_id=message.chat.id,
        text=welcome_text,
        reply_markup=reply_markup,
        reply_to_message_id=message.id
    )

# Command: Help
@Client.on_message(filters.command("help"))
@verified_only
async def help_cmd(client: Client, message: Message):
    help_text = (
        "📖 **Mirror Bot Usage Guide**\n\n"
        "This bot allows you to download files from direct links and mirror them to cloud storage or leech them directly to Telegram.\n\n"
        "**Mirror Commands (Cloud Upload):**\n"
        "• `/mirror <link>` — Select where to upload the file dynamically via interactive menus\n"
        "• `/gf <link>` or `/gofile <link>` — Directly mirror the file to Gofile\n"
        "• `/pd <link>` or `/pixeldrain <link>` — Directly mirror the file to Pixeldrain\n\n"
        "**Leech Commands (Send to Telegram):**\n"
        "• `/leech <link>` — Download and send the file directly to this chat\n\n"
        "**Thumbnail Management (Leech Only):**\n"
        "• Send a photo with `/setthumb` in caption, or reply to a photo with `/setthumb` to configure custom leech thumbnails\n"
        "• `/delthumb` — Delete your custom leech thumbnail\n\n"
        "**Information Commands:**\n"
        "• `/quota` — Check remaining daily quota (Default limit: `10 GB`)\n"
        "• `/stats` — Check OS and Bot performance statistics\n"
        "• `/help` — Display this documentation"
    )
    
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "My Quota", "callback_data": "user_quota"}
            ]
        ]
    }
    
    from helpers.ui import send_styled_message
    await send_styled_message(
        chat_id=message.chat.id,
        text=help_text,
        reply_markup=reply_markup,
        reply_to_message_id=message.id
    )

# Command: Ping (Owner Only)
@Client.on_message(filters.command("ping") & filters.user(config.owner_id))
async def ping_cmd(client: Client, message: Message):
    start_time = time.time()
    status_msg = await message.reply_text("**Pinging...**")
    latency = (time.time() - start_time) * 1000
    await status_msg.edit_text(
        f"**Pong!**\nLatency: `{latency:.2f} ms`"
    )

# Command: Speedtest (Owner Only)
@Client.on_message(filters.command("speedtest") & filters.user(config.owner_id))
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
