from database import get_connection, RateLimitConfig, RateLimitRecord
from datetime import datetime, timedelta

class RateLimiter:
    def __init__(self, key: str):
        self.key = key
        self.conn = get_connection()
        self.cursor = self.conn.cursor()
        self.config = self.get_config()
        if not self.config:
            raise ValueError(f"No config found for key {key}")

    def get_config(self):
        self.cursor.execute(
            "SELECT key, capacity, refill_rate FROM rate_limit_configs WHERE key=?",
            (self.key,)
        )
        row = self.cursor.fetchone()
        if row:
            return RateLimitConfig(*row)
        return None

    def get_state(self):
        self.cursor.execute(
            "SELECT key, tokens, last_refill FROM rate_limit_states WHERE key=?",
            (self.key,)
        )
        row = self.cursor.fetchone()
        if row:
            key, tokens, last_refill = row
            return RateLimitRecord(key, tokens, datetime.fromisoformat(last_refill))
        else:
            # Initialize state if it doesn't exist
            state = RateLimitRecord(self.key, self.config.capacity, datetime.now())
            self.cursor.execute(
                "INSERT INTO rate_limit_states (key, tokens, last_refill) VALUES (?, ?, ?)",
                (state.key, state.tokens, state.last_refill.isoformat())
            )
            self.conn.commit()
            return state

    def allow_request(self):
        state = self.get_state()
        now = datetime.now()
        delta = (now - state.last_refill).total_seconds()
        # Refill tokens
        state.tokens = min(
            self.config.capacity, 
            state.tokens + delta * self.config.refill_rate
        )
        if state.tokens >= 1:
            state.tokens -= 1
            state.last_refill = now
            # Update DB
            self.cursor.execute(
                "UPDATE rate_limit_states SET tokens=?, last_refill=? WHERE key=?",
                (state.tokens, state.last_refill.isoformat(), self.key)
            )
            self.conn.commit()
            return True
        else:
            return False
