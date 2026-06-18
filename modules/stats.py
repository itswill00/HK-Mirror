import os
import time
import sys
import logging
import platform
import psutil
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config import config, bot_start_time, DOWNLOAD_DIR
from helpers.decorators import verified_only
from helpers.trackers import active_tasks
from helpers.ui import UI, send_styled_message, edit_styled_message

logger = logging.getLogger("MirrorBot.Stats")

def get_readable_time(seconds: float) -> str:
    periods = [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]
    result = ""
    for period_name, period_seconds in periods:
        if seconds >= period_seconds:
            period_value, seconds = divmod(seconds, period_seconds)
            result += f"{int(period_value)}{period_name} "
    return result.strip() or "0s"

def get_progress_bar_string(pct: float) -> str:
    p = min(max(pct, 0.0), 100.0)
    completed = int(p // 8)
    bar = "■" * completed + "□" * (12 - completed)
    return f"[{bar}]"

# Command: Stats
@Client.on_message(filters.command("stats"))
@verified_only
async def stats_cmd(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return
        
    reply_markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Bot Stats", callback_data=f"stats:stbot:{user_id}"),
            InlineKeyboardButton("OS Stats", callback_data=f"stats:stsys:{user_id}")
        ],
        [
            InlineKeyboardButton("Close", callback_data=f"stats:close:{user_id}")
        ]
    ])
    
    await send_styled_message(
        chat_id=message.chat.id,
        text="⌬ **Bot & OS Statistics!**\n\nSelect a stats category below:",
        reply_markup=reply_markup,
        reply_to_message_id=message.id
    )

# Callback Query: Stats Actions
@Client.on_callback_query(filters.regex(r"^stats:"))
async def stats_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data.split(":")
    if len(data) < 3:
        return
        
    action = data[1]
    target_user_id = int(data[2])
    clicker_user_id = callback_query.from_user.id
    
    if clicker_user_id != target_user_id and clicker_user_id != config.owner_id:
        await callback_query.answer("This button is not for you!", show_alert=True)
        return
        
    if action == "close":
        await callback_query.answer("Closing stats...")
        try:
            await callback_query.message.delete()
        except Exception:
            pass
        return
        
    await callback_query.answer("Fetching metrics...")
    
    reply_markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Bot Stats", callback_data=f"stats:stbot:{target_user_id}"),
            InlineKeyboardButton("OS Stats", callback_data=f"stats:stsys:{target_user_id}")
        ],
        [
            InlineKeyboardButton("Close", callback_data=f"stats:close:{target_user_id}")
        ]
    ])
    
    if action == "stbot":
        # Disk usage info
        total, used, free = psutil.disk_usage('/')[:3]
        disk_pct = psutil.disk_usage('/').percent
        
        # Memory info
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        
        # Disk I/O info
        try:
            disk_io = psutil.disk_io_counters()
            disk_read = UI.human_size(disk_io.read_bytes)
            disk_write = UI.human_size(disk_io.write_bytes)
        except Exception:
            disk_read = "Access Denied"
            disk_write = "Access Denied"
            
        bot_uptime = get_readable_time(time.time() - bot_start_time)
        active_tasks_count = len(active_tasks)
        
        msg = (
            f"⌬ **BOT STATISTICS :**\n"
            f"• **Bot Uptime :** `{bot_uptime}`\n"
            f"• **Active Tasks :** `{active_tasks_count}`\n\n"
            f"┎ **RAM ( MEMORY ) :**\n"
            f"┃ {get_progress_bar_string(memory.percent)} `{memory.percent:.1f}%`\n"
            f"┖ **Used:** `{UI.human_size(memory.used)}` | **Free:** `{UI.human_size(memory.available)}` | **Total:** `{UI.human_size(memory.total)}`\n\n"
            f"┎ **SWAP MEMORY :**\n"
            f"┃ {get_progress_bar_string(swap.percent)} `{swap.percent:.1f}%`\n"
            f"┖ **Used:** `{UI.human_size(swap.used)}` | **Free:** `{UI.human_size(swap.free)}` | **Total:** `{UI.human_size(swap.total)}`\n\n"
            f"┎ **DISK :**\n"
            f"┃ {get_progress_bar_string(disk_pct)} `{disk_pct:.1f}%`\n"
            f"┃ **Total Disk Read :** `{disk_read}`\n"
            f"┃ **Total Disk Write :** `{disk_write}`\n"
            f"┖ **Used:** `{UI.human_size(used)}` | **Free:** `{UI.human_size(free)}` | **Total:** `{UI.human_size(total)}`"
        )
        
        await edit_styled_message(
            chat_id=callback_query.message.chat.id,
            message_id=callback_query.message.id,
            text=msg,
            reply_markup=reply_markup
        )
        
    elif action == "stsys":
        cpu_usage = psutil.cpu_percent(interval=0.1)
        
        # System uptime
        sys_uptime = get_readable_time(time.time() - psutil.boot_time())
        os_version = platform.release()
        os_arch = platform.platform()
        
        # Net I/O
        try:
            net_io = psutil.net_io_counters()
            up_data = UI.human_size(net_io.bytes_sent)
            dl_data = UI.human_size(net_io.bytes_recv)
            tl_data = UI.human_size(net_io.bytes_sent + net_io.bytes_recv)
            pkt_sent = f"{net_io.packets_sent / 1000:.1f}k"
            pkt_recv = f"{net_io.packets_recv / 1000:.1f}k"
        except Exception:
            up_data = dl_data = tl_data = pkt_sent = pkt_recv = "N/A"
            
        # CPU Freq
        try:
            cpu_f = psutil.cpu_freq()
            cpu_freq_str = f"{cpu_f.current / 1000:.2f} GHz" if cpu_f else "Access Denied"
        except Exception:
            cpu_freq_str = "Access Denied"
            
        # Load Average
        try:
            sys_load = ", ".join(f"{x:.2f}" for x in os.getloadavg())
        except Exception:
            sys_load = "N/A"
            
        p_cores = psutil.cpu_count(logical=False)
        total_cores = psutil.cpu_count(logical=True)
        v_cores = total_cores - p_cores
        
        # Usable CPUs count
        try:
            cpu_use = len(os.sched_getaffinity(0))
        except AttributeError:
            cpu_use = total_cores
            
        msg = (
            f"⌬ **OS SYSTEM :**\n"
            f"• **OS Uptime :** `{sys_uptime}`\n"
            f"• **OS Version :** `{os_version}`\n"
            f"• **OS Arch :** `{os_arch}`\n\n"
            f"⌬ **NETWORK STATS :**\n"
            f"• **Upload Data:** `{up_data}`\n"
            f"• **Download Data:** `{dl_data}`\n"
            f"• **Pkts Sent:** `{pkt_sent}`\n"
            f"• **Pkts Received:** `{pkt_recv}`\n"
            f"• **Total I/O Data:** `{tl_data}`\n\n"
            f"┎ **CPU :**\n"
            f"┃ {get_progress_bar_string(cpu_usage)} `{cpu_usage:.1f}%`\n"
            f"• **CPU Frequency :** `{cpu_freq_str}`\n"
            f"• **System Avg Load :** `{sys_load}`\n"
            f"• **P-Core(s) :** `{p_cores}` | **V-Core(s) :** `{v_cores}`\n"
            f"• **Total Core(s) :** `{total_cores}`\n"
            f"┖ **Usable CPU(s) :** `{cpu_use}`"
        )
        
        await edit_styled_message(
            chat_id=callback_query.message.chat.id,
            message_id=callback_query.message.id,
            text=msg,
            reply_markup=reply_markup
        )
