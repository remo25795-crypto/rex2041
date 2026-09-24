import importlib.util
import logging
import sqlite3
from datetime import time as dt_time, timedelta, timezone

from telegram.error import InvalidToken, NetworkError, TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from config import (
    BOT_TOKEN,
    DB_FILE,
    MAX_CONCURRENT_PROCESSING,
    TELEGRAM_BOOTSTRAP_RETRIES,
    TELEGRAM_CONNECT_TIMEOUT,
    TELEGRAM_POOL_TIMEOUT,
    TELEGRAM_PROXY_URL,
    TELEGRAM_READ_TIMEOUT,
    TELEGRAM_WRITE_TIMEOUT,
)
from db import get_user_count, init_db
from file_processing import cancel_process_text_handler
from handlers.admin import (
    reset_daily_limits,
    send_daily_report,
    send_daily_cookies_to_admins,   # NEW import
)
from handlers.user import (
    handle_callback_query,
    handle_cookies,
    handle_document,
    start,
)

logger = logging.getLogger(__name__)


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Add it to your environment before starting the bot.")

    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(TELEGRAM_CONNECT_TIMEOUT)
        .read_timeout(TELEGRAM_READ_TIMEOUT)
        .write_timeout(TELEGRAM_WRITE_TIMEOUT)
        .pool_timeout(TELEGRAM_POOL_TIMEOUT)
        .get_updates_connect_timeout(TELEGRAM_CONNECT_TIMEOUT)
        .get_updates_read_timeout(TELEGRAM_READ_TIMEOUT)
        .get_updates_write_timeout(TELEGRAM_WRITE_TIMEOUT)
        .get_updates_pool_timeout(TELEGRAM_POOL_TIMEOUT)
    )
    if TELEGRAM_PROXY_URL:
        builder = builder.proxy(TELEGRAM_PROXY_URL).get_updates_proxy(TELEGRAM_PROXY_URL)

    application = builder.build()

    application.add_handler(MessageHandler(filters.Regex(r'^❌ Cancel Process$'), cancel_process_text_handler))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_cookies))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    return application


def schedule_jobs(application: Application) -> None:
    if importlib.util.find_spec("apscheduler") is None:
        logger.warning(
            "Scheduled jobs disabled: run `python -m pip install -r requirements.txt` "
            "to install python-telegram-bot[job-queue]."
        )
        return

    job_queue = application.job_queue
    if not job_queue:
        logger.warning("Scheduled jobs disabled: JobQueue is unavailable.")
        return

    ist = timezone(timedelta(hours=5, minutes=30))
    job_queue.run_daily(reset_daily_limits, time=dt_time(0, 0, tzinfo=ist))
    job_queue.run_daily(send_daily_report, time=dt_time(22, 0, tzinfo=ist))
    # NEW: schedule daily cookies zip at midnight
    job_queue.run_daily(send_daily_cookies_to_admins, time=dt_time(0, 0, tzinfo=ist))
    logger.info("Scheduled daily limit reset, stats report, and daily cookies jobs")


def main() -> None:
    init_db()
    try:
        user_count = get_user_count()
    except sqlite3.Error as exc:
        logger.error("Error getting user count: %s", exc)
        user_count = 0

    logger.info("Bot starting with %s users in database", user_count)
    logger.info("Database location: %s", DB_FILE)
    logger.info("Concurrent processing enabled with %s workers", MAX_CONCURRENT_PROCESSING)

    application = build_application()

    schedule_jobs(application)

    try:
        application.run_polling(
            bootstrap_retries=TELEGRAM_BOOTSTRAP_RETRIES,
            timeout=int(TELEGRAM_READ_TIMEOUT),
        )
    except InvalidToken as exc:
        logger.error("Telegram rejected BOT_TOKEN. Set a valid BOT_TOKEN and restart.")
        raise SystemExit(1) from exc
    except (TimedOut, NetworkError) as exc:
        logger.error("Could not connect to Telegram: %s", exc)
        logger.error(
            "Check your internet/VPN/firewall. If Telegram is blocked on this network, "
            "set TELEGRAM_PROXY_URL, for example socks5://user:pass@host:port."
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()