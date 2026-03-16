import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import os as _os
DB_PATH = Path(_os.environ.get("DB_PATH", str(Path(__file__).parent.parent / "data" / "trades.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist, migrate schema if needed."""
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

    # ── user_tokens: one row per (user, bot) ─────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            bot_key TEXT NOT NULL DEFAULT 'legacy_bot',
            oanda_account_id TEXT NOT NULL,
            oanda_access_token TEXT NOT NULL,
            oanda_account_type TEXT NOT NULL DEFAULT 'practice',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            UNIQUE(user_id, bot_key)
        )
    """)

    # ── Migration: add bot_key column if this is an old single-token table ───
    cursor.execute("PRAGMA table_info(user_tokens)")
    cols = [row[1] for row in cursor.fetchall()]
    if "bot_key" not in cols:
        cursor.execute("ALTER TABLE user_tokens RENAME TO _user_tokens_old")
        cursor.execute("""
            CREATE TABLE user_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                bot_key TEXT NOT NULL DEFAULT 'legacy_bot',
                oanda_account_id TEXT NOT NULL,
                oanda_access_token TEXT NOT NULL,
                oanda_account_type TEXT NOT NULL DEFAULT 'practice',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id),
                UNIQUE(user_id, bot_key)
            )
        """)
        cursor.execute("""
            INSERT INTO user_tokens
                (user_id, bot_key, oanda_account_id, oanda_access_token,
                 oanda_account_type, created_at, updated_at)
            SELECT user_id, 'legacy_bot', oanda_account_id, oanda_access_token,
                   oanda_account_type, created_at, updated_at
            FROM _user_tokens_old
        """)
        cursor.execute("DROP TABLE _user_tokens_old")

    # ── strategy_state: persists enabled/disabled across deploys ─────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS strategy_state (
            name       TEXT PRIMARY KEY,
            enabled    INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
    """)

    # ── open_trades: live positions written by runner ────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS open_trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_key   TEXT    NOT NULL UNIQUE,
            strategy    TEXT    NOT NULL,
            instrument  TEXT    NOT NULL,
            direction   INTEGER NOT NULL,
            units       REAL    NOT NULL,
            entry_price REAL    NOT NULL,
            entry_time  TEXT    NOT NULL
        )
    """)

    # ── Migration: add learner columns to trades ─────────────────────────────
    cursor.execute("PRAGMA table_info(trades)")
    trade_cols = [row[1] for row in cursor.fetchall()]
    if "strategy_name" not in trade_cols:
        cursor.execute("ALTER TABLE trades ADD COLUMN strategy_name TEXT")
    if "entry_metadata" not in trade_cols:
        cursor.execute("ALTER TABLE trades ADD COLUMN entry_metadata TEXT")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy_name)"
    )

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


def seed_users(users: list[tuple[str, str]]):
    """Upsert users from (email, password_hash) tuples."""
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
# OANDA token helpers — per (user_id, bot_key)
# bot_key values: 'legacy_bot' | 'stat_arb' | 'momentum' | 'vol_premium'
# ---------------------------------------------------------------------------

ALL_BOT_KEYS = ["legacy_bot", "stat_arb", "momentum", "vol_premium", "crypto"]


def get_user_token(user_id: int, bot_key: str = "legacy_bot") -> dict | None:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM user_tokens WHERE user_id = ? AND bot_key = ?",
        (user_id, bot_key),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_user_tokens(user_id: int) -> dict[str, dict]:
    """Return a dict keyed by bot_key for every configured bot."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM user_tokens WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return {row["bot_key"]: dict(row) for row in rows}


def upsert_user_token(user_id: int, bot_key: str,
                      account_id: str, access_token: str,
                      account_type: str = "practice") -> None:
    now = datetime.utcnow().isoformat()
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO user_tokens
               (user_id, bot_key, oanda_account_id, oanda_access_token,
                oanda_account_type, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id, bot_key) DO UPDATE SET
               oanda_account_id   = excluded.oanda_account_id,
               oanda_access_token = excluded.oanda_access_token,
               oanda_account_type = excluded.oanda_account_type,
               updated_at         = excluded.updated_at""",
        (user_id, bot_key, account_id, access_token, account_type, now, now),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Open-trade helpers (live positions written by runner subprocess)
# ---------------------------------------------------------------------------

def upsert_open_trade(
    trade_key: str,
    strategy: str,
    instrument: str,
    direction: int,
    units: float,
    entry_price: float,
    entry_time: str,
) -> None:
    conn = _connect()
    conn.execute(
        """INSERT INTO open_trades
               (trade_key, strategy, instrument, direction, units, entry_price, entry_time)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(trade_key) DO UPDATE SET
               entry_price = excluded.entry_price,
               entry_time  = excluded.entry_time""",
        (trade_key, strategy, instrument, direction, units, entry_price, entry_time),
    )
    conn.commit()
    conn.close()


def delete_open_trade(trade_key: str) -> None:
    conn = _connect()
    conn.execute("DELETE FROM open_trades WHERE trade_key = ?", (trade_key,))
    conn.commit()
    conn.close()


def record_closed_trade(
    instrument: str,
    direction: int,
    units: float,
    entry_price: float,
    exit_price: float,
    entry_time: str,
    exit_time: str,
    exit_reason: str,
    raw_pl: float,
    strategy_name: str = "",
) -> None:
    """Write a manually-closed trade to the trades table."""
    if entry_price <= 0 or exit_price <= 0:
        return
    pl_points = (exit_price - entry_price) * direction
    conn = _connect()
    conn.execute(
        """INSERT INTO trades
               (entry_time, exit_time, instrument, direction, entry_units,
                entry_price, exit_price, exit_reason,
                pl_points, pl_R, raw_pl,
                bar_length, momentum, threshold_k, per_trade_sl, per_trade_tp, trailing_mode,
                strategy_name, entry_metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (entry_time, exit_time, instrument, direction, int(units),
         entry_price, exit_price, exit_reason,
         pl_points, 0.0, raw_pl,
         None, None, None, None, None, None,
         strategy_name or None, None),
    )
    conn.commit()
    conn.close()


def get_open_trades() -> list[dict]:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM open_trades ORDER BY entry_time DESC")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Strategy state helpers
# ---------------------------------------------------------------------------

_ALL_STRATEGIES = ["stat_arb", "momentum", "vol_premium", "crypto"]


def get_strategy_states() -> dict:
    """Return {name: {"enabled": bool}} for all strategies."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT name, enabled FROM strategy_state")
    rows = {row["name"]: {"enabled": bool(row["enabled"])} for row in cursor.fetchall()}
    conn.close()
    result = {s: {"enabled": False} for s in _ALL_STRATEGIES}
    result.update(rows)
    return result


def upsert_strategy_state(name: str, enabled: bool) -> None:
    now = datetime.utcnow().isoformat()
    conn = _connect()
    conn.execute(
        """INSERT INTO strategy_state (name, enabled, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at""",
        (name, int(enabled), now),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Learner helpers
# ---------------------------------------------------------------------------

def get_trades_for_learner(strategy_name: str, limit: int = 200) -> list[dict]:
    """Return closed trades for a strategy that have entry_metadata stored."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT raw_pl, entry_metadata FROM trades
           WHERE strategy_name = ? AND entry_metadata IS NOT NULL
           ORDER BY id DESC LIMIT ?""",
        (strategy_name, limit),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


if __name__ == "__main__":
    init_db()
    print("Database initialized!")
