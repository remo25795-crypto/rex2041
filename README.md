# Public Cookie Bot

A powerful Telegram bot for checking Netflix cookies, extracting account metadata, and providing premium login links. Built with `python-telegram-bot`, SQLite, and a robust Netflix API client.

---

## Features

- **Single Cookie Check** – Paste a cookie string, get instant login links and full account info (background fetching).
- **File Upload Support** – Upload `.txt`, `.zip`, or `.rar` files containing multiple cookies. The bot deduplicates records and offers multiple processing modes.
- **Quick Check** – Fast validation of cookies (active/hold/invalid) without full account metadata.
- **Full Check** – Detailed account info (plan, region, billing, profiles, viewing activity) for each valid cookie, delivered as a file.
- **One-by-One Check** – Send each active cookie as a separate message with its login buttons and account details.
- **Reset Link Support** – Paste a Netflix reset link (with `nftoken=`), the bot extracts a new token, loads the account session, and displays login buttons + account info.
- **Bulk Reset Links** – Upload a file with multiple reset links; they are processed in bulk.
- **Daily Cookie Logging** – Every valid cookie is logged (deduplicated) to daily text files. Admins receive a ZIP archive of all daily logs at midnight.
- **Admin Privacy Toggle** – When `SEND_COOKIE_TO_ADMIN=false`, admins receive only a summary (user, status, timestamp) – no cookie or account details are forwarded.
- **Admin Dashboard** – Health, stats, user management, premium/ban toggles, broadcast messages, and database backups.
- **Scheduled Jobs** – Daily limit reset at midnight IST, daily stats report, and daily cookie archive delivery.
- **Premium User Tiers** – Higher daily limits for premium users.
- **Channel Membership Requirement** – Users must join a specified Telegram channel to use the bot.
- **Concurrent Processing** – Up to 150 concurrent workers (configurable) with a queue for large file jobs.
- **Cache** – Caches successful cookie results for 30 minutes to reduce repeated requests.

---

## Project Structure

```text
.
├── bot.py                 # Entry point, handler registration, job scheduling
├── config.py              # Environment variables, limits, paths, global settings
├── db.py                  # SQLite database models and queries
├── netflix_client.py      # Cookie parsing, Netflix API client, account extraction
├── file_processing.py     # Upload validation, archive parsing, batch processing, cancellation state
├── handlers/
│   ├── user.py            # User commands, cookie handling, callback routing
│   └── admin.py           # Admin dashboard, reports, backups, premium/ban management
├── premium_emoji.py       # Telegram premium emoji helpers and inline button styling
├── requirements.txt       # Python dependencies
├── Dockerfile             # Docker build instructions
├── .env.example           # Environment variable template
└── README.md              # This file
```

---

## Installation

### 1. Prerequisites

- Python 3.11 or higher
- pip (Python package manager)
- (Optional) Docker or Railway for cloud deployment

### 2. Clone the Repository

```bash
git clone https://github.com/yourusername/Public-coookie-bot.git
cd Public-coookie-bot
```

### 3. Create and Activate a Virtual Environment

```bash
python -m venv venv
source venv/bin/activate      # On Linux/macOS
venv\Scripts\activate         # On Windows
```

### 4. Install Dependencies

```bash
pip install -r requirements.txt
```

> **Note for Windows:** Use `python -m pip install -r requirements.txt` to ensure the correct Python environment is used.

### 5. Configure Environment Variables

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Edit `.env` with your values. At minimum, set:

```dotenv
BOT_TOKEN=your_telegram_bot_token
ADMIN_IDS=123456789,987654321   # comma-separated admin user IDs
CHANNEL_USERNAME=your_channel
SEND_COOKIE_TO_ADMIN=true       # or false
```

All available variables are documented below.

### 6. Initialize the Database

The bot will automatically create the SQLite database (`netflix_bot.db`) on first run.

### 7. Run the Bot

```bash
python bot.py
```

---

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `BOT_TOKEN` | **Required** – Your Telegram bot token from @BotFather. | *(empty)* |
| `ADMIN_IDS` | **Required** – Comma-separated Telegram user IDs of admins. | `1325407917` |
| `CHANNEL_USERNAME` | Channel username (without `@`) users must join to use the bot. | `Sais_tool` |
| `CHANNEL_URL` | Join URL for the channel. | `https://t.me/<CHANNEL_USERNAME>` |
| `SEND_COOKIE_TO_ADMIN` | If `true`, admins receive the user's cookie and full account details. If `false`, only a summary (user, status, timestamp) is sent. | `true` |
| `TELEGRAM_CONNECT_TIMEOUT` | Connection timeout (seconds). | `30` |
| `TELEGRAM_READ_TIMEOUT` | Read timeout (seconds). | `30` |
| `TELEGRAM_WRITE_TIMEOUT` | Write timeout (seconds). | `30` |
| `TELEGRAM_POOL_TIMEOUT` | Pool timeout (seconds). | `30` |
| `TELEGRAM_BOOTSTRAP_RETRIES` | Number of retries on startup. | `0` |
| `TELEGRAM_PROXY_URL` | Optional proxy URL (HTTP/SOCKS). Example: `socks5://user:pass@host:port`. | *(empty)* |
| `DATA_DIR` | Persistent data directory. Defaults to `RAILWAY_VOLUME_DIR` or current directory. | *(current directory)* |
| `USER_DAILY_LIMIT` | Single-cookie checks per day for regular users. | `15` |
| `USER_FILE_DAILY_LIMIT` | File checks per day for regular users. | `3` |
| `PREMIUM_DAILY_LIMIT` | Single-cookie checks per day for premium users. | `100000` |
| `PREMIUM_FILE_DAILY_LIMIT` | File checks per day for premium users. | `500` |
| `MAX_CONCURRENT_PROCESSING` | Maximum concurrent cookie-checking workers. | `20` |
| `CACHE_TTL_SECONDS` | Cache duration for successful cookie results. | `1800` |
| `MAX_UPLOAD_BYTES` | Maximum file size for uploads (bytes). | `15728640` (15 MB) |
| `MAX_ARCHIVE_FILES` | Maximum number of text files inside an archive. | `25` |
| `MAX_ARCHIVE_ENTRY_BYTES` | Maximum uncompressed size per archive entry (bytes). | `5242880` (5 MB) |
| `MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES` | Maximum total uncompressed text size from an archive (bytes). | `10485760` (10 MB) |
| `LOG_LEVEL` | Logging level (`DEBUG`, `INFO`, `WARNING`, etc.). | `INFO` |

---

## Usage

### User Commands

- **Start the bot** – Send `/start` or press the Start button.
- **Single Cookie Check** – Paste a cookie string (e.g., `NetflixId=...; SecureNetflixId=...`) directly in the chat. The bot will:
  - Validate the cookie and generate **PC / MOBILE / TV** login buttons (using `nftoken`).
  - Fetch full account info in the background and edit the message with the details.
- **File Upload** – Send a `.txt`, `.zip`, or `.rar` file. The bot will:
  - Parse the content, deduplicate records, and display a file summary.
  - Offer three processing modes:
    - **Quick Check** – Shows only the status (Active/Hold/Invalid) for each cookie, delivered as a file.
    - **Full Results File** – Includes full account info (plan, region, billing, profiles, etc.) for each valid cookie, delivered as a file.
    - **Send Active One By One** – Sends each active cookie as a separate message with its own login buttons and full account info.
- **Reset Link** – Paste a Netflix reset link containing `nftoken=...`. The bot follows the link, extracts a new token, and presents login buttons + account info.
- **My Limits** – View your daily usage and remaining limits.
- **Report Issue** – Send a message to admins (support).
- **Request Premium** – Notify admins that you want premium access.
- **Help** – Show quick usage instructions.

### Admin Commands (Accessible via Admin Dashboard)

Admins see a special **Admin Dashboard** button in the main menu. The dashboard provides:

- **Health** – Database status, pending files, queue size, worker usage.
- **Stats** – Total users, premium/banned counts, daily checks, active tasks.
- **Users** – Paginated list of all users. Click a user to manage them.
- **Search User** – Search by user ID or username.
- **Premium Users** – List all premium users.
- **Broadcast** – Send a message to all bot users (preview before sending).
- **Backup DB** – Download a backup of the SQLite database.

### User Management Panel

When viewing a specific user, admins can:

- Add/Remove premium status
- Ban/Unban the user
- View check history summary (active/hold/expired breakdown)
- View recent checks

---

## Scheduled Jobs

The bot runs three scheduled tasks daily (IST timezone):

| Time  | Task |
|-------|------|
| 00:00 | Reset daily usage limits for all users. |
| 00:00 | Send a ZIP archive of all daily cookie logs to all admins (then deletes the log files). |
| 22:00 | Send a daily stats report to admins (summary of all user activity). |

---

## Data and Privacy

- **User Data** – User IDs, usernames, usage counters, premium/ban status, check history, and pending upload metadata are stored in SQLite.
- **Cookie Privacy** – By default (`SEND_COOKIE_TO_ADMIN=true`), admins receive the user's cookie and full account details when a check is performed. This helps with support and quality monitoring.
- **Privacy Toggle** – Set `SEND_COOKIE_TO_ADMIN=false` in your environment to prevent any cookie or account detail from being forwarded to admins. Admins will only see a summary: user, status, and timestamp.
- **Daily Cookies** – Every valid cookie is logged (deduplicated) to daily text files. These logs are stored in `DATA_DIR/daily_cookies/` and are automatically zipped and sent to admins at midnight, then deleted. This helps build a collection of working cookies without manual intervention.
- **User Content** – Uploaded files are stored temporarily in `DATA_DIR/pending_uploads/` and deleted after processing.
- **Cache** – Successful cookie results are cached in memory for 30 minutes to reduce repeated API calls.

---

## Deployment

### Local

```bash
python bot.py
```

### Docker

Build and run:

```bash
docker build -t public-cookie-bot .
docker run -d --env-file .env -v /path/to/data:/data public-cookie-bot
```

### Railway

1. Connect your GitHub repository to Railway.
2. Set the required environment variables (`BOT_TOKEN`, `ADMIN_IDS`, etc.) in the Railway dashboard.
3. Use `python bot.py` as the start command.
4. Mount a persistent volume at `/data` to keep the database and daily logs.

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Bot doesn't start | Check that `BOT_TOKEN` is set correctly and that the bot has permission to send messages. |
| "Cannot join channel" | Ensure the bot is an admin in the channel and has the necessary permissions. |
| "No valid records found" | Your cookie or file might not contain valid Netflix cookies. Ensure the format is supported (plain header string, Netscape, JSON, or combo lines). |
| Slow processing | Increase `MAX_CONCURRENT_PROCESSING` if you have enough system resources. |
| Database errors | Ensure the `DATA_DIR` is writable. On Railway, mount a persistent volume. |

---

## License

This project is provided as-is for educational and personal use. Modify and distribute as you see fit.

---

## Credits

- Built with [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- Uses `requests`, `rarfile`, `apscheduler`, and `sqlite3`
- Premium emojis provided by Telegram custom emoji packs

---

Enjoy managing your Netflix cookies with ease!
