from dataclasses import dataclass
from datetime import datetime
import sqlite3

DB_NAME = "rate_limiter.db"

# ------------------------
# Data classes
# ------------------------
@dataclass
class RateLimitConfig:
    key: str
    capacity: int
    refill_rate: float

@dataclass
class RateLimitRecord:
    key: str
    tokens: float
    last_refill: datetime

# ------------------------
# DB helpers
# ------------------------
def get_connection():
    return sqlite3.connect(DB_NAME)

def reset_database():
    conn = get_connection()
    cursor = conn.cursor()
    
    # Drop old tables
    cursor.execute("DROP TABLE IF EXISTS rate_limit_configs")
    cursor.execute("DROP TABLE IF EXISTS rate_limit_states")
    
    # Create tables
    cursor.execute("""
    CREATE TABLE rate_limit_configs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT UNIQUE NOT NULL,
        capacity INTEGER NOT NULL,
        refill_rate REAL NOT NULL
    )
    """)
    
    cursor.execute("""
    CREATE TABLE rate_limit_states (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT NOT NULL,
        tokens REAL NOT NULL,
        last_refill TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    conn.commit()
    conn.close()
    print("✅ Database reset complete")
