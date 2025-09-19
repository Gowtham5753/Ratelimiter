"""
FastAPI Rate Limiter with Real-time Sync
Production-ready rate limiting service with multiple algorithms and real-time monitoring
"""
import asyncio
import json
import time
from datetime import datetime
from typing import Dict, List, Optional, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from database import db_manager, RateLimitConfig
from rate_limiter import rate_limiter_manager, RateLimitResult


# Pydantic models for API
class RateLimitConfigCreate(BaseModel):
    endpoint: str = Field(..., description="API endpoint path")
    requests_per_window: int = Field(100, description="Number of requests allowed per window")
    window_seconds: int = Field(3600, description="Window duration in seconds")
    algorithm: str = Field("sliding_window", description="Rate limiting algorithm")
    burst_limit: Optional[int] = Field(None, description="Burst limit for token bucket")
    refill_rate: Optional[float] = Field(None, description="Refill rate for token bucket")
    is_active: bool = Field(True, description="Whether the rate limit is active")

class RateLimitConfigUpdate(BaseModel):
    requests_per_window: Optional[int] = None
    window_seconds: Optional[int] = None
    algorithm: Optional[str] = None
    burst_limit: Optional[int] = None
    refill_rate: Optional[float] = None
    is_active: Optional[bool] = None

class RateLimitStatus(BaseModel):
    allowed: bool
    remaining: int
    reset_time: datetime
    retry_after: Optional[int] = None
    details: Dict[str, Any] = {}

class ClientStats(BaseModel):
    client_id: str
    status: Dict[str, Any]
    recent_events: List[Dict[str, Any]]


# WebSocket connection manager for real-time sync
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.client_connections: Dict[str, List[WebSocket]] = {}
    
    async def connect(self, websocket: WebSocket, client_id: Optional[str] = None):
        await websocket.accept()
        self.active_connections.append(websocket)
        if client_id:
            if client_id not in self.client_connections:
                self.client_connections[client_id] = []
            self.client_connections[client_id].append(websocket)
    
    def disconnect(self, websocket: WebSocket, client_id: Optional[str] = None):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        if client_id and client_id in self.client_connections:
            if websocket in self.client_connections[client_id]:
                self.client_connections[client_id].remove(websocket)
            if not self.client_connections[client_id]:
                del self.client_connections[client_id]
    
    async def send_personal_message(self, message: str, websocket: WebSocket):
        try:
            await websocket.send_text(message)
        except:
            pass
    
    async def broadcast(self, message: str):
        for connection in self.active_connections.copy():
            try:
                await connection.send_text(message)
            except:
                self.active_connections.remove(connection)
    
    async def send_to_client(self, message: str, client_id: str):
        if client_id in self.client_connections:
            for connection in self.client_connections[client_id].copy():
                try:
                    await connection.send_text(message)
                except:
                    self.client_connections[client_id].remove(connection)


# Global connection manager
manager = ConnectionManager()


# Startup and shutdown events
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await db_manager.create_tables()
    await rate_limiter_manager.initialize()
    
    # Start cleanup task
    cleanup_task = asyncio.create_task(periodic_cleanup())
    
    yield
    
    # Shutdown
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass


async def periodic_cleanup():
    """Periodic cleanup of old records"""
    while True:
        try:
            await asyncio.sleep(3600)  # Run every hour
            await db_manager.cleanup_old_records(days=7)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Cleanup error: {e}")


# Create FastAPI app
app = FastAPI(
    title="FastAPI Rate Limiter",
    description="Production-ready rate limiter with real-time sync capabilities",
    version="1.0.0",
    lifespan=lifespan
)

# Templates
templates = Jinja2Templates(directory="templates")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Rate limiting middleware
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    # Skip rate limiting for admin endpoints and WebSocket
    if request.url.path.startswith("/admin") or request.url.path.startswith("/ws"):
        response = await call_next(request)
        return response
    
    # Get client identifier (IP address or custom header)
    client_id = request.headers.get("X-Client-ID") or request.client.host
    endpoint = request.url.path
    
    # Check rate limit
    result = await rate_limiter_manager.check_rate_limit(client_id, endpoint)
    
    if not result.allowed:
        # Broadcast rate limit event
        await manager.broadcast(json.dumps({
            "type": "rate_limit_exceeded",
            "client_id": client_id,
            "endpoint": endpoint,
            "timestamp": datetime.utcnow().isoformat(),
            "retry_after": result.retry_after
        }))
        
        return JSONResponse(
            status_code=429,
            content={
                "error": "Rate limit exceeded",
                "retry_after": result.retry_after,
                "reset_time": result.reset_time.isoformat()
            },
            headers={
                "X-RateLimit-Limit": str(rate_limiter_manager.configs.get(endpoint, {}).get("requests_per_window", 100)),
                "X-RateLimit-Remaining": str(result.remaining),
                "X-RateLimit-Reset": str(int(result.reset_time.timestamp())),
                "Retry-After": str(result.retry_after) if result.retry_after else "60"
            }
        )
    
    # Process request
    response = await call_next(request)
    
    # Add rate limit headers
    response.headers["X-RateLimit-Limit"] = str(rate_limiter_manager.configs.get(endpoint, {}).get("requests_per_window", 100))
    response.headers["X-RateLimit-Remaining"] = str(result.remaining)
    response.headers["X-RateLimit-Reset"] = str(int(result.reset_time.timestamp()))
    
    # Broadcast successful request event
    await manager.broadcast(json.dumps({
        "type": "request_processed",
        "client_id": client_id,
        "endpoint": endpoint,
        "timestamp": datetime.utcnow().isoformat(),
        "remaining": result.remaining
    }))
    
    return response


# API Routes
@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "FastAPI Rate Limiter Service",
        "version": "1.0.0",
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "database": "connected"
    }

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Rate limiter dashboard"""
    return templates.TemplateResponse("dashboard.html", {"request": request})

# Protected demo endpoints
@app.get("/api/data")
async def get_data():
    """Demo protected endpoint"""
    return {
        "data": "This is protected data",
        "timestamp": datetime.utcnow().isoformat()
    }

@app.post("/api/submit")
async def submit_data(data: Dict[str, Any]):
    """Demo protected POST endpoint"""
    return {
        "message": "Data submitted successfully",
        "received_data": data,
        "timestamp": datetime.utcnow().isoformat()
    }

# Admin endpoints (not rate limited)
@app.get("/admin/configs", response_model=List[Dict[str, Any]])
async def get_rate_limit_configs():
    """Get all rate limit configurations"""
    async with db_manager.async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(RateLimitConfig))
        configs = result.scalars().all()
        
        return [
            {
                "id": config.id,
                "endpoint": config.endpoint,
                "requests_per_window": config.requests_per_window,
                "window_seconds": config.window_seconds,
                "algorithm": config.algorithm,
                "burst_limit": config.burst_limit,
                "refill_rate": config.refill_rate,
                "is_active": config.is_active,
                "created_at": config.created_at,
                "updated_at": config.updated_at
            }
            for config in configs
        ]

@app.post("/admin/configs")
async def create_rate_limit_config(config: RateLimitConfigCreate):
    """Create new rate limit configuration"""
    async with db_manager.async_session() as session:
        db_config = RateLimitConfig(**config.dict())
        session.add(db_config)
        await session.commit()
        await session.refresh(db_config)
        
        # Update in-memory config
        rate_limiter_manager.configs[config.endpoint] = db_config
        rate_limiter_manager.limiters[config.endpoint] = rate_limiter_manager._create_limiter(db_config)
        
        return {"message": "Configuration created successfully", "id": db_config.id}

@app.put("/admin/configs/{endpoint:path}")
async def update_rate_limit_config(endpoint: str, config: RateLimitConfigUpdate):
    """Update rate limit configuration"""
    update_data = {k: v for k, v in config.dict().items() if v is not None}
    success = await rate_limiter_manager.update_config(endpoint, **update_data)
    
    if not success:
        raise HTTPException(status_code=404, detail="Configuration not found")
    
    return {"message": "Configuration updated successfully"}

@app.get("/admin/stats/{client_id}", response_model=ClientStats)
async def get_client_stats(client_id: str):
    """Get client statistics"""
    stats = await rate_limiter_manager.get_client_stats(client_id)
    return ClientStats(**stats)

@app.get("/admin/stats")
async def get_global_stats():
    """Get global statistics"""
    async with db_manager.async_session() as session:
        from sqlalchemy import select, func
        from database import RateLimitRecord, RateLimitEvent
        
        # Get total requests in last 24 hours
        from datetime import timedelta
        yesterday = datetime.utcnow() - timedelta(days=1)
        
        requests_result = await session.execute(
            select(func.count(RateLimitRecord.id))
            .where(RateLimitRecord.timestamp >= yesterday)
        )
        total_requests = requests_result.scalar() or 0
        
        # Get blocked requests
        blocked_result = await session.execute(
            select(func.count(RateLimitEvent.id))
            .where(
                RateLimitEvent.event_type == "request",
                RateLimitEvent.timestamp >= yesterday,
                RateLimitEvent.details.like('%"allowed": false%')
            )
        )
        blocked_requests = blocked_result.scalar() or 0
        
        return {
            "total_requests_24h": total_requests,
            "blocked_requests_24h": blocked_requests,
            "success_rate": (total_requests - blocked_requests) / max(total_requests, 1) * 100,
            "active_configs": len(rate_limiter_manager.configs),
            "timestamp": datetime.utcnow().isoformat()
        }

# WebSocket endpoint for real-time sync
@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """WebSocket endpoint for real-time rate limit monitoring"""
    await manager.connect(websocket, client_id)
    try:
        while True:
            # Send periodic updates
            stats = await rate_limiter_manager.get_client_stats(client_id)
            await manager.send_personal_message(json.dumps({
                "type": "client_stats",
                "data": stats,
                "timestamp": datetime.utcnow().isoformat()
            }), websocket)
            
            await asyncio.sleep(5)  # Send updates every 5 seconds
            
    except WebSocketDisconnect:
        manager.disconnect(websocket, client_id)

@app.websocket("/ws/global")
async def global_websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for global rate limit monitoring"""
    await manager.connect(websocket)
    try:
        while True:
            # Send global stats
            stats = await get_global_stats()
            await manager.send_personal_message(json.dumps({
                "type": "global_stats",
                "data": stats,
                "timestamp": datetime.utcnow().isoformat()
            }), websocket)
            
            await asyncio.sleep(10)  # Send updates every 10 seconds
            
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# Server-Sent Events endpoint for real-time updates
@app.get("/events/{client_id}")
async def stream_events(client_id: str):
    """Server-Sent Events endpoint for real-time updates"""
    async def event_generator():
        while True:
            try:
                stats = await rate_limiter_manager.get_client_stats(client_id)
                yield f"data: {json.dumps(stats)}\n\n"
                await asyncio.sleep(5)
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                break
    
    return StreamingResponse(
        event_generator(),
        media_type="text/plain",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Content-Type": "text/event-stream"
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
