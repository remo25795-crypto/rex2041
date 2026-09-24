import logging
import sqlite3
from datetime import datetime

from config import (
    ADMIN_IDS,
    DB_FILE,
    PREMIUM_DAILY_LIMIT,
    PREMIUM_FILE_DAILY_LIMIT,
    USER_DAILY_LIMIT,
    USER_FILE_DAILY_LIMIT,
)

logger = logging.getLogger(__name__)

PENDING_FILES: dict[str, dict] = {}
NEXT_FILE_ID = 1


def init_db():
    """Initialize the SQLite database."""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Users table
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            usage_count INTEGER DEFAULT 0,
            file_checks_count INTEGER DEFAULT 0,
            last_reset_date TEXT,
            is_premium INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            username TEXT
        )''')
        
        # Checks history table
        c.execute('''CREATE TABLE IF NOT EXISTS user_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            timestamp TEXT,
            account_info TEXT,
            cookie_type TEXT
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS check_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            timestamp TEXT,
            category TEXT
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS pending_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            file_path TEXT,
            timestamp TEXT,
            source_label TEXT,
            total_records INTEGER DEFAULT 0,
            unique_records INTEGER DEFAULT 0,
            duplicate_records INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            original_filename TEXT
        )''')
        
        # Premium users table (for tracking who added them)
        c.execute('''CREATE TABLE IF NOT EXISTS premium_log (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            added_date TEXT,
            added_by INTEGER
        )''')
        
        conn.commit()
        conn.close()
        logger.info("SQLite database initialized successfully")
        return {"user_usage": {}} # Dummy return for compatibility
    except sqlite3.Error as e:
        logger.error(f"Error initializing SQLite database: {e}")
        return {"user_usage": {}}

def get_user_usage(user_id):
    """Get user usage from SQLite"""
    if user_id in ADMIN_IDS:
        return 0, 0, True, False
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    today = datetime.now().strftime('%Y-%m-%d')
    
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    
    if not row:
        # Create new user
        c.execute("INSERT INTO users (user_id, usage_count, file_checks_count, last_reset_date, is_premium, is_banned) VALUES (?, 0, 0, ?, 0, 0)", (user_id, today))
        conn.commit()
        conn.close()
        return 0, 0, False, False
    
    usage_count = row[1]
    file_checks_count = row[2]
    last_reset_date = row[3]
    is_premium = bool(row[4])
    is_banned = bool(row[5])
    
    # Reset daily counts if needed
    if last_reset_date != today:
        c.execute("UPDATE users SET usage_count=0, file_checks_count=0, last_reset_date=? WHERE user_id=?", (today, user_id))
        conn.commit()
        usage_count = 0
        file_checks_count = 0
    
    conn.close()
    return (usage_count, file_checks_count, is_premium, is_banned)

def is_user_banned(user_id):
    """Check if user is banned"""
    if user_id in ADMIN_IDS:
        return False
    _, _, _, is_banned = get_user_usage(user_id)
    return is_banned

def ban_user(user_id):
    """Ban a user"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,))
        if c.rowcount == 0:
            today = datetime.now().strftime('%Y-%m-%d')
            c.execute("INSERT INTO users (user_id, is_banned, last_reset_date) VALUES (?, 1, ?)", (user_id, today))
        conn.commit()
        conn.close()
        logger.info(f"User banned: {user_id}")
        return True
    except sqlite3.Error as e:
        logger.error(f"Failed to ban user: {e}")
        return False

def unban_user(user_id):
    """Unban a user"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()
        logger.info(f"User unbanned: {user_id}")
        return True
    except sqlite3.Error as e:
        logger.error(f"Failed to unban user: {e}")
        return False

def get_user_limits(user_id):
    """Get user limits based on their status"""
    if user_id in ADMIN_IDS:
        return float('inf'), float('inf')  # No limits for admin
    
    _, _, is_premium, is_banned = get_user_usage(user_id)
    
    if is_banned:
        return 0, 0
    
    return (PREMIUM_DAILY_LIMIT, PREMIUM_FILE_DAILY_LIMIT) if is_premium else (USER_DAILY_LIMIT, USER_FILE_DAILY_LIMIT)

def increment_user_usage(user_id):
    """Increment user usage count"""
    if user_id in ADMIN_IDS: 
        return
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    today = datetime.now().strftime('%Y-%m-%d')
    
    c.execute("UPDATE users SET usage_count = usage_count + 1, last_reset_date = ? WHERE user_id = ?", (today, user_id))
    if c.rowcount == 0:
         c.execute("INSERT INTO users (user_id, usage_count, file_checks_count, last_reset_date) VALUES (?, 1, 0, ?)", (user_id, today))
    
    conn.commit()
    conn.close()

def increment_file_checks(user_id):
    """Increment user file checks count"""
    if user_id in ADMIN_IDS: 
        return
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    today = datetime.now().strftime('%Y-%m-%d')
    
    c.execute("UPDATE users SET file_checks_count = file_checks_count + 1, last_reset_date = ? WHERE user_id = ?", (today, user_id))
    if c.rowcount == 0:
         c.execute("INSERT INTO users (user_id, usage_count, file_checks_count, last_reset_date) VALUES (?, 0, 1, ?)", (user_id, today))
    
    conn.commit()
    conn.close()

def save_user_check(user_id, username, account_info, cookie_type):
    """Save user check to database"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute("INSERT INTO user_checks (user_id, username, timestamp, account_info, cookie_type) VALUES (?, ?, ?, ?, ?)",
                  (user_id, username, timestamp, account_info, cookie_type))
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.error(f"Failed to save user check: {e}")


def upsert_user_profile(user_id, username):
    """Keep username searchable without changing usage counters."""
    if user_id in ADMIN_IDS:
        return
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        today = datetime.now().strftime('%Y-%m-%d')
        c.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
        if c.rowcount == 0:
            c.execute(
                "INSERT INTO users (user_id, usage_count, file_checks_count, last_reset_date, is_premium, is_banned, username) VALUES (?, 0, 0, ?, 0, 0, ?)",
                (user_id, today, username),
            )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.error(f"Failed to update user profile: {e}")

def save_pending_file(
    user_id,
    file_path,
    source_label="Upload",
    total_records=0,
    unique_records=0,
    duplicate_records=0,
    original_filename=None,
):
    """Persist a pending upload so callback buttons survive bot restarts."""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute(
            """INSERT INTO pending_files
               (user_id, file_path, timestamp, source_label, total_records, unique_records, duplicate_records, status, original_filename)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (user_id, str(file_path), timestamp, source_label, total_records, unique_records, duplicate_records, original_filename),
        )
        file_id = int(c.lastrowid)
        conn.commit()
        conn.close()
        return file_id
    except sqlite3.Error as e:
        logger.error(f"Failed to save pending file in DB: {e}")

    global NEXT_FILE_ID
    file_id = NEXT_FILE_ID
    NEXT_FILE_ID += 1
    PENDING_FILES[str(file_id)] = {
        'user_id': user_id,
        'file_path': str(file_path),
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'source_label': source_label,
        'total_records': total_records,
        'unique_records': unique_records,
        'duplicate_records': duplicate_records,
        'status': 'pending',
        'original_filename': original_filename,
    }
    return file_id


def get_pending_file_record(file_id):
    """Return pending file metadata without deleting it."""
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM pending_files WHERE id=? AND status != 'done'", (int(file_id),))
        row = c.fetchone()
        conn.close()
        if row:
            return dict(row)
    except (sqlite3.Error, ValueError) as e:
        logger.error(f"Failed to get pending file record: {e}")

    file_data = PENDING_FILES.get(str(file_id))
    return dict(file_data) if file_data else None


def mark_pending_file_status(file_id, status):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE pending_files SET status=? WHERE id=?", (status, int(file_id)))
        conn.commit()
        conn.close()
    except (sqlite3.Error, ValueError) as e:
        logger.error(f"Failed to update pending file status: {e}")
    if str(file_id) in PENDING_FILES:
        PENDING_FILES[str(file_id)]["status"] = status


def remove_pending_file(file_id):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM pending_files WHERE id=?", (int(file_id),))
        conn.commit()
        conn.close()
    except (sqlite3.Error, ValueError) as e:
        logger.error(f"Failed to remove pending file: {e}")
    PENDING_FILES.pop(str(file_id), None)


def get_pending_file(file_id):
    """Get pending file by ID, keeping the persistent record until processing finishes."""
    file_data = get_pending_file_record(file_id)
    if file_data:
        return (int(file_data['user_id']), str(file_data['file_path']))
    return None

def get_user_checks(user_id=None):
    """Get user checks from database, optionally filtered by user_id"""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    if user_id:
        c.execute("SELECT * FROM user_checks WHERE user_id=? ORDER BY id DESC", (user_id,))
    else:
        c.execute("SELECT * FROM user_checks ORDER BY id DESC")
        
    rows = c.fetchall()
    checks = [dict(row) for row in rows]
    conn.close()
    return checks


def _normalize_check_category(value: str) -> str:
    text = (value or "").lower()
    if text in {"active", "current", "current_member"}:
        return "active"
    if text in {"hold", "on hold"}:
        return "hold"
    if text in {"expired", "inactive", "cancelled", "invalid", "failed", "error"}:
        return "expired"
    if "on hold" in text or "category = on hold" in text or "status = hold" in text or "status: hold" in text:
        return "hold"
    if "category:</b> active" in text or "category = active" in text or "status:</b> active" in text or "status = active" in text:
        return "active"
    if any(marker in text for marker in ("expired", "inactive", "cancelled", "failed", "invalid")):
        return "expired"
    return "unknown"


def _classify_check_result(account_info: str) -> str:
    return _normalize_check_category(account_info)


def record_check_metric(user_id, category: str):
    """Record one check outcome for public aggregate stats."""
    try:
        normalized = _normalize_check_category(category)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "INSERT INTO check_metrics (user_id, timestamp, category) VALUES (?, ?, ?)",
            (user_id, timestamp, normalized),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.error(f"Failed to record check metric: {e}")


def get_public_stats():
    """Return community-safe daily stats without exposing user details."""
    today = datetime.now().strftime('%Y-%m-%d')
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("SELECT * FROM users")
    users = [dict(row) for row in c.fetchall()]
    c.execute("SELECT * FROM user_checks WHERE timestamp LIKE ?", (f"{today}%",))
    checks = [dict(row) for row in c.fetchall()]
    c.execute("SELECT * FROM check_metrics WHERE timestamp LIKE ?", (f"{today}%",))
    metrics = [dict(row) for row in c.fetchall()]
    conn.close()

    active_user_ids = {
        int(user["user_id"])
        for user in users
        if int(user.get("usage_count") or 0) > 0 or int(user.get("file_checks_count") or 0) > 0
    }
    active_user_ids.update(int(check["user_id"]) for check in checks if check.get("user_id") is not None)
    active_user_ids.update(int(metric["user_id"]) for metric in metrics if metric.get("user_id") is not None)

    category_counts = {"active": 0, "hold": 0, "expired": 0, "unknown": 0}
    source_rows = metrics or checks
    for check in source_rows:
        if metrics:
            category = _normalize_check_category(str(check.get("category") or ""))
        else:
            category = _classify_check_result(str(check.get("account_info") or ""))
        category_counts[category] += 1

    classified_total = sum(category_counts.values())
    successful = category_counts["active"] + category_counts["hold"]
    success_rate = round((successful / classified_total) * 100, 1) if classified_total else 0.0

    return {
        "date": today,
        "total_checks_today": len(source_rows),
        "active_users_today": len(active_user_ids),
        "success_rate": success_rate,
        "active": category_counts["active"],
        "on_hold": category_counts["hold"],
        "expired": category_counts["expired"],
        "unknown": category_counts["unknown"],
    }

def get_all_user_stats():
    """Get statistics for all users"""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users")
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_all_user_ids():
    """Gets all user IDs from the database for broadcasting."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    ids = [row[0] for row in c.fetchall()]
    conn.close()
    return ids


def search_users(query, limit=10):
    """Search users by numeric ID or username from users/check history."""
    pattern = f"%{query.strip().lstrip('@')}%"
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    rows = []
    if query.strip().isdigit():
        c.execute("SELECT * FROM users WHERE CAST(user_id AS TEXT) LIKE ? LIMIT ?", (pattern, limit))
        rows.extend(dict(row) for row in c.fetchall())
    c.execute(
        """
        SELECT u.*
        FROM users u
        LEFT JOIN user_checks c ON c.user_id = u.user_id
        WHERE COALESCE(u.username, '') LIKE ? OR COALESCE(c.username, '') LIKE ?
        GROUP BY u.user_id
        LIMIT ?
        """,
        (pattern, pattern, limit),
    )
    seen = {int(row["user_id"]) for row in rows}
    for row in c.fetchall():
        data = dict(row)
        if int(data["user_id"]) not in seen:
            rows.append(data)
            seen.add(int(data["user_id"]))
    conn.close()
    return rows[:limit]


def get_pending_file_counts():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT status, COUNT(*) FROM pending_files GROUP BY status")
        rows = {str(status): int(count) for status, count in c.fetchall()}
        conn.close()
        return rows
    except sqlite3.Error as e:
        logger.error(f"Failed to get pending file counts: {e}")
        return {}


def get_user_history_summary(user_id):
    checks = get_user_checks(user_id)
    usage, files, is_premium, is_banned = get_user_usage(user_id)
    category_counts = {"active": 0, "hold": 0, "expired": 0, "unknown": 0}
    for check in checks:
        category_counts[_classify_check_result(str(check.get("account_info") or ""))] += 1
    return {
        "total_checks": len(checks),
        "last_check": checks[0]["timestamp"] if checks else "Never",
        "usage_count": usage,
        "file_checks_count": files,
        "is_premium": is_premium,
        "is_banned": is_banned,
        **category_counts,
    }

def get_premium_users():
    """Get all premium users"""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM premium_log")
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def add_premium_user(user_id, username, added_by):
    """Add a user to premium list"""
    if is_premium_user(user_id):
        return False
        
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Update users table
        c.execute("UPDATE users SET is_premium=1 WHERE user_id=?", (user_id,))
        if c.rowcount == 0:
            today = datetime.now().strftime('%Y-%m-%d')
            c.execute("INSERT INTO users (user_id, is_premium, last_reset_date) VALUES (?, 1, ?)", (user_id, today))
            
        # Log to premium_log
        added_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute("INSERT OR REPLACE INTO premium_log (user_id, username, added_date, added_by) VALUES (?, ?, ?, ?)",
                  (user_id, username, added_date, added_by))
                  
        conn.commit()
        conn.close()
        logger.info(f"Added premium user: {user_id} ({username})")
        return True
    except sqlite3.Error as e:
        logger.error(f"Failed to add premium user: {e}")
        return False

def remove_premium_user(user_id):
    """Remove a user from premium list"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE users SET is_premium=0 WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM premium_log WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()
        logger.info(f"Removed premium user: {user_id}")
        return True
    except sqlite3.Error as e:
        logger.error(f"Failed to remove premium user: {e}")
        return False

def is_premium_user(user_id):
    """Check if a user is premium"""
    _, _, is_premium, _ = get_user_usage(user_id)
    return is_premium


def reset_daily_usage() -> None:
    today = datetime.now().strftime('%Y-%m-%d')
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "UPDATE users SET usage_count=0, file_checks_count=0, last_reset_date=?",
            (today,),
        )


def get_user_count() -> int:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.execute("SELECT count(*) FROM users")
        return int(cursor.fetchone()[0])