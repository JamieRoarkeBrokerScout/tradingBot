import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "database" / "trades.db"
DB_PATH.parent.mkdir(exist_ok=True)

def init_db():
    """Create tables if they don't exist"""
    conn = sqlite3.connect(DB_PATH)
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
    
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("Database initialized!")