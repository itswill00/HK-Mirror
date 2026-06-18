# HK-Mirror

An enterprise-grade, high-performance Telegram bot built using Pyrogram (Pyrofork) to mirror files from direct download links directly to Gofile and Pixeldrain, or leech them directly to Telegram.

## Features

- **Multi-Service Mirroring:** Mirror files directly to Gofile or Pixeldrain.
- **Telegram Leeching:** Download and send files directly into your Telegram chat.
- **Large File Handling:** Automatically splits files exceeding the 2GB Telegram limit.
- **Dynamic Direct Link Resolution:** Automatically detects and converts standard browser sharing links (like Google Drive, Dropbox, Mediafire, Pixeldrain) into direct download links. It also bypasses Google Drive's "Virus scan warning" page and handles quota limit errors.
- **Verification System:** Built-in verification challenge (math puzzle) to prevent spam.
- **Quota Management:** Manage user bandwidth quotas with an interactive admin control panel.
- **Custom Thumbnails:** Set custom thumbnails for leeched files.
- **Owner Utilities:** Features speed testing, latency ping, user management, and broadcast features.

## Commands

### User Commands
- `/start` - Start the bot and verify.
- `/help` - View usage guide.
- `/quota` - Check remaining daily quota.
- `/mirror <link>` - Choose where to mirror a file (Gofile/Pixeldrain).
- `/gf <link>` - Mirror directly to Gofile.
- `/pd <link>` - Mirror directly to Pixeldrain.
- `/leech <link>` - Leech files directly to Telegram.
- `/setthumb` - Set a custom thumbnail (by replying to a photo).
- `/delthumb` - Delete custom thumbnail.

### Admin Commands (Owner Only)
- `/admin` - Access admin control panel.
- `/user <user_id>` - Access user management detail dashboard.
- `/addquota <user_id> <amount_in_gb>` - Add daily bandwidth quota to a user.
- `/setquota <user_id> <amount_in_gb>` - Set a specific daily bandwidth quota for a user.
- `/deluser <user_id>` - Remove user access.
- `/broadcast <message>` - Broadcast a message to all verified users.
- `/ping` - Check bot response latency.
- `/speedtest` - Run speed test.

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/itswill00/HK-Mirror.git
   cd HK-Mirror
   ```

2. Set up virtual environment and install dependencies:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. Configure environment variables by copying `config.env.example` to `config.env` and filling in the values:
   ```bash
   cp config.env.example config.env
   nano config.env
   ```

4. Run the bot:
   ```bash
   python3 bot.py
   ```
