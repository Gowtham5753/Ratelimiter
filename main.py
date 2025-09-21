from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from database import get_connection, reset_database
from rate_limiter import RateLimiter
from contextlib import asynccontextmanager
from datetime import datetime
# ------------------------
# Lifespan for startup/shutdown
# ------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: reset DB
    reset_database()
    yield
    # Shutdown: nothing for now
    # Could close DB connections here if needed

# ------------------------
# FastAPI app
# ------------------------
app = FastAPI(title="Rate Limiter API", lifespan=lifespan)

# ------------------------
# Pydantic model for config input
# ------------------------
class RateLimitConfigInput(BaseModel):
    key: str
    capacity: int
    refill_rate: float

# ------------------------
# API endpoints
# ------------------------
@app.post("/config")
def create_config(config: RateLimitConfigInput):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO rate_limit_configs (key, capacity, refill_rate) VALUES (?, ?, ?)",
            (config.key, config.capacity, config.refill_rate)
        )
        conn.commit()
        return {"message": "Config created successfully"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()

@app.get("/config/{key}")
def get_config(key: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT key, capacity, refill_rate FROM rate_limit_configs WHERE key=?",
        (key,)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"key": row[0], "capacity": row[1], "refill_rate": row[2]}
    raise HTTPException(status_code=404, detail="Config not found")

@app.get("/allow/{key}")
def allow_request(key: str):
    try:
        limiter = RateLimiter(key)
        allowed = limiter.allow_request()
        return {"allowed": allowed}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "FastAPI Rate Limiter Service",
        "version": "1.0.0",
        "timestamp": datetime.utcnow().isoformat()
    }