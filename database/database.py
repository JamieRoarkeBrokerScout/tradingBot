import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "database" / "trades.db"
DB_PATH.parent.mkdir(exist_ok=True)


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = _connect()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_time TEXT NOT NULL,
            exit_time TEXT NOT NULL,
            instrument TEXT NOT NULL,
            direction INTEGER NOT NULL,
            entry_units INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL NOT NULL,
            exit_reason TEXT NOT NULL,
            pl_points REAL NOT NULL,
            pl_R REAL NOT NULL,
            raw_pl REAL NOT NULL,
            bar_length TEXT,
            momentum INTEGER,
            threshold_k REAL,
            per_trade_sl REAL,
            per_trade_tp REAL,
            trailing_mode TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            oanda_account_id TEXT NOT NULL,
            oanda_access_token TEXT NOT NULL,
            oanda_account_type TEXT NOT NULL DEFAULT 'practice',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

def get_user_by_email(email: str) -> dict | None:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def create_user(email: str, password_hash: str) -> int:
    now = datetime.utcnow().isoformat()
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
        (email, password_hash, now),
    )
    user_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return user_id


def seed_users(users: list[tuple[str, str]]):
    """
    Upsert users from a list of (email, password_hash) tuples.
    Always updates the password_hash so a fresh hash from startup is stored.
    """
    now = datetime.utcnow().isoformat()
    conn = _connect()
    cursor = conn.cursor()
    for email, password_hash in users:
        cursor.execute(
            """INSERT INTO users (email, password_hash, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(email) DO UPDATE SET password_hash = excluded.password_hash""",
            (email, password_hash, now),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

SESSION_TTL_HOURS = 24 * 7  # 1 week


def create_session(user_id: int, email: str) -> str:
    token = str(uuid.uuid4())
    now = datetime.utcnow()
    expires_at = (now + timedelta(hours=SESSION_TTL_HOURS)).isoformat()
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO user_sessions (token, user_id, email, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (token, user_id, email, now.isoformat(), expires_at),
    )
    conn.commit()
    conn.close()
    return token


def get_session(token: str) -> dict | None:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM user_sessions WHERE token = ? AND expires_at > ?",
        (token, datetime.utcnow().isoformat()),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def delete_session(token: str):
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM user_sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# OANDA token helpers (per user_id integer FK)
# ---------------------------------------------------------------------------

def get_user_token(user_id: int) -> dict | None:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM user_tokens WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def upsert_user_token(user_id: int, account_id: str, access_token: str, account_type: str = "practice"):
    now = datetime.utcnow().isoformat()
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM user_tokens WHERE user_id = ?", (user_id,)
    )
    if cursor.fetchone():
        cursor.execute(
            "UPDATE user_tokens SET oanda_account_id=?, oanda_access_token=?, oanda_account_type=?, updated_at=? WHERE user_id=?",
            (account_id, access_token, account_type, now, user_id),
        )
    else:
        cursor.execute(
            "INSERT INTO user_tokens (user_id, oanda_account_id, oanda_access_token, oanda_account_type, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (user_id, account_id, access_token, account_type, now, now),
        )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("Database initialized!")
