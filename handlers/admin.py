import asyncio
import html
import logging
import os
import re
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from premium_emoji import pe, premium_button
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from config import ADMIN_IDS, DAILY_COOKIES_DIR, DB_FILE
from db import (
    add_premium_user,
    ban_user,
    get_all_user_ids,
    get_all_user_stats,
    get_pending_file_counts,
    get_premium_users,
    get_user_checks,
    get_user_history_summary,
    get_user_limits,
    get_user_usage,
    init_db,
    remove_premium_user,
    reset_daily_usage,
    unban_user,
)
from file_processing import get_active_processing_count, get_file_queue_snapshot

logger = logging.getLogger(__name__)


async def admin_dashboard_callback(query, context):
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Not authorized.", show_alert=True)
        return

    stats = get_all_user_stats()
    total_users = len(stats)
    premium_users = sum(1 for item in stats if item.get('is_premium'))
    banned_users = sum(1 for item in stats if item.get('is_banned'))
    queue = get_file_queue_snapshot()
    msg = (
        f"{pe('crown')} <b><i>PREMIUM ADMIN CONTROL CENTER</i></b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{pe('users')} <b>Total Users:</b> <code>{total_users}</code>\n"
        f"{pe('star')} <b>Premium Users:</b> <code>{premium_users}</code>\n"
        f"{pe('stop')} <b>Banned Users:</b> <code>{banned_users}</code>\n\n"
        f"{pe('folder')} <b>Large Jobs:</b> <code>{queue['active']} active / {queue['queued']} queued</code>\n"
        f"{pe('flash')} <b>Cookie Workers:</b> <code>{queue['cookie_workers_busy']} busy</code>\n\n"
        f"{pe('lock')} <i>Admin tools are locked to authorized users only.</i>\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    keyboard = [
        [premium_button("Health", callback_data="admin_health", emoji="doctor", style="success"), premium_button("Stats", callback_data="admin_stats", emoji="chart", style="primary")],
        [premium_button("Search User", callback_data="admin_search_user", emoji="search_alt", style="primary"), premium_button("Users", callback_data="list_users_page_0", emoji="users", style="primary")],
        [premium_button("Broadcast", callback_data="admin_broadcast", emoji="announcement", style="danger"), premium_button("Premium", callback_data="admin_premium_users", emoji="star", style="success")],
        [premium_button("Backup DB", callback_data="admin_backup", emoji="disk", style="primary")],
        [premium_button("Main Menu", callback_data="back_to_start", emoji="home", style="primary")],
    ]
    await query.edit_message_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_health_callback(query, context):
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Not authorized.", show_alert=True)
        return

    queue = get_file_queue_snapshot()
    pending = get_pending_file_counts()
    db_exists = DB_FILE.exists()
    db_size = DB_FILE.stat().st_size if db_exists else 0
    msg = (
        f"{pe('doctor')} <b><i>SYSTEM HEALTH CHECK</i></b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{pe('disk')} <b>Database:</b> <code>{'OK' if db_exists else 'Missing'}</code>\n"
        f"{pe('folder')} <b>DB Size:</b> <code>{db_size} bytes</code>\n"
        f"{pe('document')} <b>Pending Files:</b> <code>{sum(pending.values()) if pending else 0}</code>\n"
        f"{pe('receipt')} <b>Pending By Status:</b> <code>{html.escape(str(pending or {}))}</code>\n"
        f"{pe('rocket')} <b>Large Jobs:</b> <code>{queue['active']} active / {queue['queued']} queued</code>\n"
        f"{pe('tools')} <b>Large Worker Limit:</b> <code>{queue['large_worker_limit']}</code>\n"
        f"{pe('flash')} <b>Cookie Workers Busy:</b> <code>{get_active_processing_count()}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    keyboard = [[premium_button("Dashboard", callback_data="admin_dashboard", emoji="left", style="primary")]]
    await query.edit_message_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_search_prompt_callback(query, context):
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Not authorized.", show_alert=True)
        return
    context.user_data["admin_mode"] = "search_user"
    keyboard = [[InlineKeyboardButton("⬅️ Dashboard", callback_data="admin_dashboard")]]
    await query.edit_message_text(
        "🔎 <b>Search User</b>\n\nSend a user ID or username.",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def send_backup(context: ContextTypes.DEFAULT_TYPE, chat_id_to_notify: int | None = None) -> None:
    recipients = [chat_id_to_notify] if chat_id_to_notify else ADMIN_IDS
    for admin_id in recipients:
        try:
            with open(DB_FILE, 'rb') as handle:
                await context.bot.send_document(
                    chat_id=admin_id,
                    document=handle,
                    caption=f"SQLite Database Backup {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    filename="netflix_bot.db",
                )
        except (OSError, TelegramError) as exc:
            logger.error("Failed to send database backup to %s: %s", admin_id, exc)


async def send_daily_cookies_to_admins(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Zips all daily cookie files from DAILY_COOKIES_DIR and sends them to admins,
    then deletes the files.
    """
    logger.info("Preparing daily cookies for admins...")
    if not DAILY_COOKIES_DIR.exists():
        return

    # Collect all cookie files (one per day)
    cookie_files = list(DAILY_COOKIES_DIR.glob("cookies_*.txt"))
    if not cookie_files:
        logger.info("No daily cookie files to send.")
        return

    # Create a zip in memory
    zip_buffer = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
    try:
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for file_path in cookie_files:
                zip_file.write(file_path, arcname=file_path.name)
        zip_buffer.close()

        # Send the zip to all admins
        caption = f"📦 Daily Cookies Collection - {datetime.now().strftime('%Y-%m-%d')}"
        for admin_id in ADMIN_IDS:
            try:
                with open(zip_buffer.name, 'rb') as zip_doc:
                    await context.bot.send_document(
                        chat_id=admin_id,
                        document=zip_doc,
                        caption=caption,
                        filename="daily_cookies.zip"
                    )
            except TelegramError as e:
                logger.error("Failed to send daily cookies to admin %s: %s", admin_id, e)

        # After successful sending, delete the cookie files
        for file_path in cookie_files:
            try:
                os.unlink(file_path)
                logger.debug("Deleted daily cookie file: %s", file_path)
            except OSError as e:
                logger.error("Failed to delete %s: %s", file_path, e)

    except (OSError, zipfile.BadZipFile) as e:
        logger.error("Error creating daily cookies zip: %s", e)
    finally:
        # Clean up the temporary zip file
        if os.path.exists(zip_buffer.name):
            os.unlink(zip_buffer.name)


async def show_user_management(query, context, target_user_id):
    usage, files, is_premium, is_banned = get_user_usage(target_user_id)
    limit, file_limit = get_user_limits(target_user_id)
    
    try:
        user_info = await context.bot.get_chat(target_user_id)
        safe_username = html.escape(user_info.username) if user_info.username else html.escape(user_info.first_name)
        username_str = f"@{safe_username}" if user_info.username else safe_username
        user_display = f"{username_str} (ID: {target_user_id})"
    except TelegramError:
        user_display = f"ID: {target_user_id}"
    
    status = "⭐ Premium" if is_premium else "👤 Regular"
    ban_status = "🔴 BANNED" if is_banned else "🟢 Active"
    
    msg = (f"<b>User Management</b>\n\n"
           f"<b>User:</b> {user_display}\n"
           f"<b>Status:</b> {status}\n"
           f"<b>Account:</b> {ban_status}\n"
           f"<b>Single Checks:</b> {usage}/{'∞' if limit == float('inf') else limit}\n"
           f"<b>File Checks:</b> {files}/{'∞' if file_limit == float('inf') else file_limit}")
    
    keyboard = []
    
    if is_premium:
        keyboard.append([InlineKeyboardButton("➖ Remove Premium", callback_data=f"manage_user_{target_user_id}_remove_premium")])
    else:
        keyboard.append([InlineKeyboardButton("➕ Add Premium", callback_data=f"manage_user_{target_user_id}_add_premium")])
    
    if is_banned:
        keyboard.append([InlineKeyboardButton("🔓 Unban User", callback_data=f"manage_user_{target_user_id}_unban")])
    else:
        keyboard.append([InlineKeyboardButton("🔒 Ban User", callback_data=f"manage_user_{target_user_id}_ban")])
    
    keyboard.append([
        InlineKeyboardButton("📊 History Summary", callback_data=f"manage_user_{target_user_id}_history"),
        InlineKeyboardButton("🧾 Recent Checks", callback_data=f"manage_user_{target_user_id}_view_checks"),
    ])
    
    keyboard.append([InlineKeyboardButton("⬅️ Back to List", callback_data="list_users_page_0")])
    keyboard.append([InlineKeyboardButton("🏠 Back to Start", callback_data="back_to_start")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        await query.edit_message_text(msg, parse_mode='HTML', reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            logger.error(f"Error updating user management view: {e}")
            pass


async def handle_user_management(query, context, target_user_id, action):
    try:
        target_user = await context.bot.get_chat(target_user_id)
        username = target_user.username or target_user.first_name
    except TelegramError:
        username = "Unknown"
    
    if action == "add_premium":
        if add_premium_user(target_user_id, username, query.from_user.id):
            await query.answer("✅ User added to premium!")
            await send_backup(context)
        else:
            await query.answer("❌ User is already premium!")
    
    elif action == "remove_premium":
        if remove_premium_user(target_user_id):
            await query.answer("✅ User removed from premium!")
            await send_backup(context)
        else:
            await query.answer("❌ User was not premium!")
    
    elif action == "ban":
        if ban_user(target_user_id):
            await query.answer("✅ User banned!")
            await send_backup(context)
        else:
            await query.answer("❌ Failed to ban user!")
    
    elif action == "unban":
        if unban_user(target_user_id):
            await query.answer("✅ User unbanned!")
            await send_backup(context)
        else:
            await query.answer("❌ Failed to unban user!")
    
    elif action == "view_checks":
        checks = get_user_checks(target_user_id)
        if checks:
            check_count = len(checks)
            last_check = checks[0]['timestamp'] if checks else "Never"
            msg = f"<b>User Checks for {target_user_id}</b>\n\nTotal Checks: {check_count}\nLast Check: {last_check}\n\nRecent checks:\n"
            for i, check in enumerate(checks[:5]):
                c_type = html.escape(str(check['cookie_type']))
                msg += f"{i+1}. {check['timestamp']} - {c_type}\n"
            
            await query.edit_message_text(msg, parse_mode='HTML')
            return
        else:
            await query.answer("❌ No checks found for this user!")
            return

    elif action == "history":
        summary = get_user_history_summary(target_user_id)
        msg = (
            f"<b>User History Summary</b>\n\n"
            f"<b>User ID:</b> <code>{target_user_id}</code>\n"
            f"<b>Total Saved Checks:</b> {summary['total_checks']}\n"
            f"<b>Last Check:</b> {html.escape(str(summary['last_check']))}\n"
            f"<b>Today:</b> {summary['usage_count']} single | {summary['file_checks_count']} files\n"
            f"<b>Status:</b> {'Banned' if summary['is_banned'] else 'Premium' if summary['is_premium'] else 'Regular'}\n\n"
            f"🟢 Active: {summary['active']}\n"
            f"🟡 On Hold: {summary['hold']}\n"
            f"🔴 Expired: {summary['expired']}\n"
            f"❓ Unknown: {summary['unknown']}"
        )
        keyboard = [[InlineKeyboardButton("⬅️ User", callback_data=f"user_info_{target_user_id}")]]
        await query.edit_message_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    await show_user_management(query, context, target_user_id)


async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Generating daily report...")
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        users = get_all_user_stats()
        today_checks = [
            check for check in get_user_checks()
            if str(check.get('timestamp', '')).startswith(today)
        ]
        active_user_ids = {
            int(user['user_id'])
            for user in users
            if user.get('usage_count', 0) > 0 or user.get('file_checks_count', 0) > 0
        }
        active_user_ids.update(int(check['user_id']) for check in today_checks)

        if not active_user_ids:
            msg = f"📊 <b>Daily Report ({today})</b>\n\nNo activity recorded today."
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(chat_id=admin_id, text=msg, parse_mode='HTML')
                except TelegramError as exc:
                    logger.warning("Failed to send empty daily report to %s: %s", admin_id, exc)
            return

        users_by_id = {int(user['user_id']): user for user in users}
        report_lines = [f"📊 <b>Daily Report ({today})</b>\n"]
        total_checks_system = 0

        for user_id in sorted(active_user_ids):
            user_usage = users_by_id.get(user_id, {})
            single_count = user_usage.get('usage_count', 0)
            file_count = user_usage.get('file_checks_count', 0)
            u_checks = [c for c in today_checks if c['user_id'] == user_id]
            active_hits = 0
            hold_hits = 0
            invalid_hits = 0
            username = u_checks[0].get('username', 'Unknown') if u_checks else user_usage.get('username', 'Unknown')
            safe_username = html.escape(str(username))

            for c in u_checks:
                info = c.get('account_info', '')
                if "Status: Active" in info or "Status = Active" in info:
                    active_hits += 1
                elif "Status: On Hold" in info or "Status: Hold" in info or "Status = Hold" in info:
                    hold_hits += 1
                else:
                    invalid_hits += 1
            
            line = (f"👤 {safe_username} (<code>{user_id}</code>)\n"
                   f"   • Usage: {single_count} single | {file_count} files\n"
                   f"   • Results: ✅ {active_hits} | ⚠️ {hold_hits} | ❌ {invalid_hits}\n")
            report_lines.append(line)
            total_checks_system += len(u_checks)

        report_lines.append(f"<b>Total System Checks Processed:</b> {total_checks_system}")
        
        full_report = "\n".join(report_lines)
        
        if len(full_report) > 4000:
            parts = [full_report[i:i+4000] for i in range(0, len(full_report), 4000)]
            for part in parts:
                for admin_id in ADMIN_IDS:
                    try:
                        await context.bot.send_message(chat_id=admin_id, text=part, parse_mode='HTML')
                    except TelegramError as exc:
                        logger.warning("Failed to send daily report part to %s: %s", admin_id, exc)
        else:
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(chat_id=admin_id, text=full_report, parse_mode='HTML')
                except TelegramError as exc:
                    logger.warning("Failed to send daily report to %s: %s", admin_id, exc)

    except (sqlite3.Error, KeyError, TypeError, ValueError) as e:
        logger.error(f"Failed to build daily report: {e}")


async def admin_stats_callback(query, context):
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Not authorized.", show_alert=True)
        return

    stats = get_all_user_stats()
    total_users = len(stats)
    total_checks = sum(s['usage_count'] for s in stats)
    total_files = sum(s['file_checks_count'] for s in stats)
    banned_users = sum(1 for s in stats if s['is_banned'])
    premium_users = sum(1 for s in stats if s['is_premium'])
    active_tasks = get_active_processing_count()

    msg = (
        f"{pe('chart')} <b><i>LIVE BOT STATISTICS</i></b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{pe('users')} <b>Total Users:</b> <code>{total_users}</code>\n"
        f"{pe('star')} <b>Premium Users:</b> <code>{premium_users}</code>\n"
        f"{pe('stop')} <b>Banned Users:</b> <code>{banned_users}</code>\n\n"
        f"{pe('search')} <b>Single Checks Today:</b> <code>{total_checks}</code>\n"
        f"{pe('folder')} <b>File Checks Today:</b> <code>{total_files}</code>\n"
        f"{pe('rocket')} <b>Active Processing Tasks:</b> <code>{active_tasks}</code>\n"
        f"{pe('lock')} <b>DB:</b> <code>SQLite</code>\n\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    keyboard = [[premium_button("Dashboard", callback_data="admin_dashboard", emoji="left", style="primary")]]
    await query.edit_message_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_premium_users_callback(query, context):
    users = get_premium_users()
    if not users:
        await query.edit_message_text(
            f"{pe('star')} <b><i>PREMIUM USERS</i></b>\n\nNo premium users found.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[premium_button("Dashboard", callback_data="admin_dashboard", emoji="left", style="primary")]]),
        )
        return
    
    lines = [f"{pe('star')} <b><i>PREMIUM USERS</i></b>", "━━━━━━━━━━━━━━━━━━"]
    for u in users:
        safe_username = html.escape(str(u.get('username', 'Unknown')))
        lines.append(f"- ID: <code>{u['user_id']}</code> | User: @{safe_username}")
    msg = "\n".join(lines)
    
    keyboard = [
        [premium_button("Dashboard", callback_data="admin_dashboard", emoji="left", style="primary")]
    ]
    
    await query.edit_message_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("You are not authorized to use this command.")
        return
    
    await update.message.reply_text("🔄 Generating and sending backup...")
    await send_backup(context, chat_id_to_notify=user_id)


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    if admin_id not in ADMIN_IDS:
        await update.message.reply_text("You are not authorized to use this command.")
        return

    message_to_broadcast = " ".join(context.args)
    if not message_to_broadcast:
        await update.message.reply_text("Use Admin Dashboard > Broadcast.")
        return

    user_ids = get_all_user_ids()
    await update.message.reply_text(f"Starting broadcast to {len(user_ids)} users. This may take a while.")

    success_count = 0
    fail_count = 0
    for user_id in user_ids:
        try:
            await context.bot.send_message(chat_id=user_id, text=message_to_broadcast)
            success_count += 1
        except TelegramError as e:
            fail_count += 1
            logger.error(f"Failed to send broadcast to {user_id}: {e}")
        await asyncio.sleep(0.05)

    await update.message.reply_text(
        f"📢 Broadcast complete!\n\n"
        f"✅ Sent successfully to {success_count} users.\n"
        f"❌ Failed for {fail_count} users (they may have blocked the bot)."
    )


async def admin_list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    await list_users_paged(update, context, page=0)


async def list_users_paged(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    user_ids = get_all_user_ids()
    users_per_page = 10
    start_index = page * users_per_page
    end_index = start_index + users_per_page
    
    paginated_users = user_ids[start_index:end_index]
    
    keyboard = []
    for user_id in paginated_users:
        try:
            user = await context.bot.get_chat(user_id)
            user_display = f"{user.first_name} (@{user.username})" if user.username else user.first_name
            
            _, _, is_premium, is_banned = get_user_usage(user_id)
            status_indicator = "⭐" if is_premium else "👤"
            status_indicator = "🔴" if is_banned else status_indicator
            
            keyboard.append([InlineKeyboardButton(f"{status_indicator} {user_display}", callback_data=f"user_info_{user_id}")])
        except TelegramError:
            _, _, is_premium, is_banned = get_user_usage(user_id)
            status_indicator = "⭐" if is_premium else "👤"
            status_indicator = "🔴" if is_banned else status_indicator
            keyboard.append([InlineKeyboardButton(f"{status_indicator} ID: {user_id}", callback_data=f"user_info_{user_id}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"list_users_page_{page-1}"))
    if end_index < len(user_ids):
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"list_users_page_{page+1}"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    keyboard.append([InlineKeyboardButton("🏠 Back to Start", callback_data="back_to_start")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    message_text = f"👥 **Users List** (Page {page + 1}/{ -(-len(user_ids) // users_per_page) })\nTotal Users: {len(user_ids)}"
    
    if hasattr(update, 'callback_query') and update.callback_query:
        await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode='Markdown')


async def admin_user_checks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        target_user_id = int(context.args[0])
        checks = get_user_checks(target_user_id)
        if not checks:
            await update.message.reply_text("No checks found for this user.")
            return
        
        check_count = len(checks)
        last_check = checks[0]['timestamp'] if checks else "Never"
        
        msg = (f"<b>User Checks for {target_user_id}</b>\n\n"
               f"Total Checks: {check_count}\n"
               f"Last Check: {last_check}\n\n"
               f"Recent checks:\n")
        
        for i, check in enumerate(checks[:5]):
            c_type = html.escape(str(check['cookie_type']))
            msg += f"{i+1}. {check['timestamp']} - {c_type}\n"
        
        if check_count > 5:
            msg += f"\n... and {check_count - 5} more checks"
            
        await update.message.reply_text(msg, parse_mode='HTML')
    except (IndexError, ValueError):
        await update.message.reply_text("Use Admin Dashboard > Search User > Recent Checks.")


async def add_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        target_user_id = int(context.args[0])
        try:
            target_user = await context.bot.get_chat(target_user_id)
            username = target_user.username or target_user.first_name
        except TelegramError:
            username = "Unknown"
            
        if add_premium_user(target_user_id, username, update.effective_user.id):
            await update.message.reply_text(f"✅ User {target_user_id} is now premium.")
            logger.info(f"Premium user added: {target_user_id}. Triggering backup.")
            await send_backup(context)
        else:
            await update.message.reply_text("User is already premium.")
    except (IndexError, ValueError):
        await update.message.reply_text("Use Admin Dashboard > Search User > Add Premium.")


async def remove_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        target_user_id = int(context.args[0])
        if remove_premium_user(target_user_id):
            await update.message.reply_text(f"✅ User {target_user_id} is no longer premium.")
            await send_backup(context)
        else:
            await update.message.reply_text("User was not premium.")
    except (IndexError, ValueError):
        await update.message.reply_text("Use Admin Dashboard > Search User > Remove Premium.")


async def ban_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        target_user_id = int(context.args[0])
        if ban_user(target_user_id):
            await update.message.reply_text(f"✅ User {target_user_id} has been banned.")
            await send_backup(context)
        else:
            await update.message.reply_text("❌ Failed to ban user.")
    except (IndexError, ValueError):
        await update.message.reply_text("Use Admin Dashboard > Search User > Ban User.")


async def unban_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        target_user_id = int(context.args[0])
        if unban_user(target_user_id):
            await update.message.reply_text(f"✅ User {target_user_id} has been unbanned.")
            await send_backup(context)
        else:
            await update.message.reply_text("❌ Failed to unban user.")
    except (IndexError, ValueError):
        await update.message.reply_text("Use Admin Dashboard > Search User > Unban User.")


async def reset_daily_limits(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Resetting daily limits for IST timezone...")
    try:
        reset_daily_usage()
        logger.info("Daily limits reset complete.")
    except sqlite3.Error as exc:
        logger.error(f"Failed to reset daily limits: {exc}")


async def wipe_database(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Wiping database file...")
    try:
        if os.path.exists(DB_FILE):
            os.remove(DB_FILE)
            logger.info(f"Successfully deleted database file: {DB_FILE}")
        
        init_db()
        logger.info("Database has been wiped and re-initialized.")
        
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text="✅ The database has been automatically wiped for the day.")
            except TelegramError as e:
                logger.error(f"Failed to send database wipe notification to admin {admin_id}: {e}")

    except (OSError, sqlite3.Error) as e:
        logger.error(f"An error occurred while wiping the database: {e}")


# ---- NEW: Daily cookies command and purge ----
async def daily_cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the daily cookie log file for a given date (YYYY-MM-DD)."""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("You are not authorized to use this command.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /daily_cookies 2025-01-15")
        return
    date_str = args[0].strip()
    # Validate date format
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        await update.message.reply_text("Invalid date format. Use YYYY-MM-DD.")
        return
    log_file = DAILY_COOKIES_DIR / f"cookies_{date_str}.txt"
    if not log_file.exists():
        await update.message.reply_text(f"No log found for {date_str}.")
        return
    with open(log_file, 'rb') as f:
        await context.bot.send_document(chat_id=user_id, document=f, filename=log_file.name)


def purge_old_cookie_logs():
    """Delete daily cookie log files older than 30 days."""
    now = datetime.now()
    for file in DAILY_COOKIES_DIR.glob("cookies_*.txt"):
        # extract date from filename
        match = re.search(r'cookies_(\d{4}-\d{2}-\d{2})\.txt', file.name)
        if match:
            file_date = datetime.strptime(match.group(1), '%Y-%m-%d')
            if (now - file_date).days > 30:
                try:
                    os.remove(file)
                    logger.info(f"Purged old cookie log: {file.name}")
                except OSError as e:
                    logger.error(f"Failed to purge {file}: {e}")