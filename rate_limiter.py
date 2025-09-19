"""
Core rate limiting algorithms and logic
"""
import asyncio
import time
import json
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, and_, or_, func
from database import (
    RateLimitRecord, RateLimitConfig, ClientStatus, 
    RateLimitEvent, db_manager
)

class RateLimitResult:
    """Result of rate limit check"""
    def __init__(self, allowed: bool, remaining: int, reset_time: datetime, 
                 retry_after: Optional[int] = None, details: Optional[Dict] = None):
        self.allowed = allowed
        self.remaining = remaining
        self.reset_time = reset_time
        self.retry_after = retry_after
        self.details = details or {}

class BaseRateLimiter(ABC):
    """Abstract base class for rate limiting algorithms"""
    
    def __init__(self, requests_per_window: int, window_seconds: int):
        self.requests_per_window = requests_per_window
        self.window_seconds = window_seconds
    
    @abstractmethod
    async def check_rate_limit(self, client_id: str, endpoint: str, 
                             session: AsyncSession) -> RateLimitResult:
        """Check if request should be allowed"""
        pass
    
    @abstractmethod
    async def record_request(self, client_id: str, endpoint: str, 
                           session: AsyncSession) -> None:
        """Record a successful request"""
        pass

class SlidingWindowRateLimiter(BaseRateLimiter):
    """Sliding window rate limiter - most accurate but more resource intensive"""
    
    async def check_rate_limit(self, client_id: str, endpoint: str, 
                             session: AsyncSession) -> RateLimitResult:
        now = datetime.utcnow()
        window_start = now - timedelta(seconds=self.window_seconds)
        
        # Count requests in the sliding window
        result = await session.execute(
            select(func.count(RateLimitRecord.id))
            .where(
                and_(
                    RateLimitRecord.client_id == client_id,
                    RateLimitRecord.endpoint == endpoint,
                    RateLimitRecord.timestamp >= window_start
                )
            )
        )
        current_requests = result.scalar() or 0
        
        allowed = current_requests < self.requests_per_window
        remaining = max(0, self.requests_per_window - current_requests)
        reset_time = now + timedelta(seconds=self.window_seconds)
        
        if not allowed:
            # Find the oldest request in window to calculate retry_after
            oldest_result = await session.execute(
                select(RateLimitRecord.timestamp)
                .where(
                    and_(
                        RateLimitRecord.client_id == client_id,
                        RateLimitRecord.endpoint == endpoint,
                        RateLimitRecord.timestamp >= window_start
                    )
                )
                .order_by(RateLimitRecord.timestamp.asc())
                .limit(1)
            )
            oldest_request = oldest_result.scalar()
            if oldest_request:
                retry_after = int((oldest_request + timedelta(seconds=self.window_seconds) - now).total_seconds())
            else:
                retry_after = self.window_seconds
        else:
            retry_after = None
        
        return RateLimitResult(
            allowed=allowed,
            remaining=remaining,
            reset_time=reset_time,
            retry_after=retry_after,
            details={"algorithm": "sliding_window", "current_requests": current_requests}
        )
    
    async def record_request(self, client_id: str, endpoint: str, 
                           session: AsyncSession) -> None:
        now = datetime.utcnow()
        record = RateLimitRecord(
            client_id=client_id,
            endpoint=endpoint,
            timestamp=now,
            window_start=now - timedelta(seconds=self.window_seconds),
            window_end=now
        )
        session.add(record)

class FixedWindowRateLimiter(BaseRateLimiter):
    """Fixed window rate limiter - simpler and more efficient"""
    
    async def check_rate_limit(self, client_id: str, endpoint: str, 
                             session: AsyncSession) -> RateLimitResult:
        now = datetime.utcnow()
        # Calculate current window start (aligned to window boundaries)
        window_start = datetime(
            now.year, now.month, now.day, now.hour,
            (now.minute // (self.window_seconds // 60)) * (self.window_seconds // 60)
        )
        window_end = window_start + timedelta(seconds=self.window_seconds)
        
        # Get or create window record
        result = await session.execute(
            select(RateLimitRecord)
            .where(
                and_(
                    RateLimitRecord.client_id == client_id,
                    RateLimitRecord.endpoint == endpoint,
                    RateLimitRecord.window_start == window_start
                )
            )
        )
        record = result.scalar_one_or_none()
        
        if record:
            current_requests = record.request_count
        else:
            current_requests = 0
        
        allowed = current_requests < self.requests_per_window
        remaining = max(0, self.requests_per_window - current_requests)
        reset_time = window_end
        retry_after = int((window_end - now).total_seconds()) if not allowed else None
        
        return RateLimitResult(
            allowed=allowed,
            remaining=remaining,
            reset_time=reset_time,
            retry_after=retry_after,
            details={"algorithm": "fixed_window", "current_requests": current_requests}
        )
    
    async def record_request(self, client_id: str, endpoint: str, 
                           session: AsyncSession) -> None:
        now = datetime.utcnow()
        window_start = datetime(
            now.year, now.month, now.day, now.hour,
            (now.minute // (self.window_seconds // 60)) * (self.window_seconds // 60)
        )
        window_end = window_start + timedelta(seconds=self.window_seconds)
        
        # Update or create window record
        result = await session.execute(
            select(RateLimitRecord)
            .where(
                and_(
                    RateLimitRecord.client_id == client_id,
                    RateLimitRecord.endpoint == endpoint,
                    RateLimitRecord.window_start == window_start
                )
            )
        )
        record = result.scalar_one_or_none()
        
        if record:
            record.request_count += 1
            record.timestamp = now
        else:
            record = RateLimitRecord(
                client_id=client_id,
                endpoint=endpoint,
                timestamp=now,
                request_count=1,
                window_start=window_start,
                window_end=window_end
            )
            session.add(record)

class TokenBucketRateLimiter(BaseRateLimiter):
    """Token bucket rate limiter - allows bursts but maintains average rate"""
    
    def __init__(self, requests_per_window: int, window_seconds: int, 
                 burst_limit: Optional[int] = None, refill_rate: Optional[float] = None):
        super().__init__(requests_per_window, window_seconds)
        self.burst_limit = burst_limit or requests_per_window
        self.refill_rate = refill_rate or (requests_per_window / window_seconds)
    
    async def check_rate_limit(self, client_id: str, endpoint: str, 
                             session: AsyncSession) -> RateLimitResult:
        now = datetime.utcnow()
        
        # Get or create client status
        result = await session.execute(
            select(ClientStatus)
            .where(ClientStatus.client_id == client_id)
        )
        client_status = result.scalar_one_or_none()
        
        if not client_status:
            client_status = ClientStatus(
                client_id=client_id,
                current_tokens=float(self.burst_limit),
                last_refill=now
            )
            session.add(client_status)
        else:
            # Refill tokens based on time elapsed
            time_elapsed = (now - client_status.last_refill).total_seconds()
            tokens_to_add = time_elapsed * self.refill_rate
            client_status.current_tokens = min(
                self.burst_limit,
                client_status.current_tokens + tokens_to_add
            )
            client_status.last_refill = now
        
        allowed = client_status.current_tokens >= 1.0
        remaining = int(client_status.current_tokens)
        
        # Calculate when next token will be available
        if not allowed:
            seconds_until_token = (1.0 - client_status.current_tokens) / self.refill_rate
            reset_time = now + timedelta(seconds=seconds_until_token)
            retry_after = int(seconds_until_token)
        else:
            reset_time = now + timedelta(seconds=1.0 / self.refill_rate)
            retry_after = None
        
        return RateLimitResult(
            allowed=allowed,
            remaining=remaining,
            reset_time=reset_time,
            retry_after=retry_after,
            details={
                "algorithm": "token_bucket",
                "current_tokens": client_status.current_tokens,
                "burst_limit": self.burst_limit,
                "refill_rate": self.refill_rate
            }
        )
    
    async def record_request(self, client_id: str, endpoint: str, 
                           session: AsyncSession) -> None:
        # Consume one token
        result = await session.execute(
            select(ClientStatus)
            .where(ClientStatus.client_id == client_id)
        )
        client_status = result.scalar_one_or_none()
        
        if client_status and client_status.current_tokens >= 1.0:
            client_status.current_tokens -= 1.0
            client_status.last_request = datetime.utcnow()
            client_status.total_requests += 1

class RateLimiterManager:
    """Main rate limiter manager"""
    
    def __init__(self):
        self.limiters: Dict[str, BaseRateLimiter] = {}
        self.configs: Dict[str, RateLimitConfig] = {}
    
    async def initialize(self):
        """Initialize rate limiter with database configs"""
        async with db_manager.async_session() as session:
            result = await session.execute(select(RateLimitConfig))
            configs = result.scalars().all()
            
            for config in configs:
                self.configs[config.endpoint] = config
                self.limiters[config.endpoint] = self._create_limiter(config)
    
    def _create_limiter(self, config: RateLimitConfig) -> BaseRateLimiter:
        """Create appropriate limiter based on algorithm"""
        if config.algorithm == "sliding_window":
            return SlidingWindowRateLimiter(
                config.requests_per_window,
                config.window_seconds
            )
        elif config.algorithm == "fixed_window":
            return FixedWindowRateLimiter(
                config.requests_per_window,
                config.window_seconds
            )
        elif config.algorithm == "token_bucket":
            return TokenBucketRateLimiter(
                config.requests_per_window,
                config.window_seconds,
                config.burst_limit,
                config.refill_rate
            )
        else:
            # Default to sliding window
            return SlidingWindowRateLimiter(
                config.requests_per_window,
                config.window_seconds
            )
    
    async def check_rate_limit(self, client_id: str, endpoint: str) -> RateLimitResult:
        """Check rate limit for client and endpoint"""
        async with db_manager.async_session() as session:
            # Get or create default config if not exists
            if endpoint not in self.configs:
                await self._ensure_config_exists(endpoint, session)
            
            limiter = self.limiters.get(endpoint)
            if not limiter:
                # Create default limiter
                limiter = SlidingWindowRateLimiter(100, 3600)
                self.limiters[endpoint] = limiter
            
            result = await limiter.check_rate_limit(client_id, endpoint, session)
            
            # Log event
            await self._log_event(client_id, endpoint, "request", session, {
                "allowed": result.allowed,
                "remaining": result.remaining,
                "details": result.details
            })
            
            if result.allowed:
                await limiter.record_request(client_id, endpoint, session)
            
            await session.commit()
            return result
    
    async def _ensure_config_exists(self, endpoint: str, session: AsyncSession):
        """Ensure configuration exists for endpoint"""
        result = await session.execute(
            select(RateLimitConfig)
            .where(RateLimitConfig.endpoint == endpoint)
        )
        config = result.scalar_one_or_none()
        
        if not config:
            config = RateLimitConfig(
                endpoint=endpoint,
                requests_per_window=100,
                window_seconds=3600,
                algorithm="sliding_window"
            )
            session.add(config)
            await session.flush()
            
            self.configs[endpoint] = config
            self.limiters[endpoint] = self._create_limiter(config)
    
    async def _log_event(self, client_id: str, endpoint: str, event_type: str, 
                        session: AsyncSession, details: Dict[str, Any]):
        """Log rate limit event"""
        event = RateLimitEvent(
            client_id=client_id,
            endpoint=endpoint,
            event_type=event_type,
            details=json.dumps(details)
        )
        session.add(event)
    
    async def update_config(self, endpoint: str, **kwargs) -> bool:
        """Update rate limit configuration"""
        async with db_manager.async_session() as session:
            result = await session.execute(
                select(RateLimitConfig)
                .where(RateLimitConfig.endpoint == endpoint)
            )
            config = result.scalar_one_or_none()
            
            if not config:
                return False
            
            # Update config
            for key, value in kwargs.items():
                if hasattr(config, key):
                    setattr(config, key, value)
            
            config.updated_at = datetime.utcnow()
            
            # Update in-memory config and limiter
            self.configs[endpoint] = config
            self.limiters[endpoint] = self._create_limiter(config)
            
            await session.commit()
            return True
    
    async def get_client_stats(self, client_id: str) -> Dict[str, Any]:
        """Get client statistics"""
        async with db_manager.async_session() as session:
            # Get client status
            status_result = await session.execute(
                select(ClientStatus)
                .where(ClientStatus.client_id == client_id)
            )
            client_status = status_result.scalar_one_or_none()
            
            # Get recent events
            events_result = await session.execute(
                select(RateLimitEvent)
                .where(RateLimitEvent.client_id == client_id)
                .order_by(RateLimitEvent.timestamp.desc())
                .limit(10)
            )
            recent_events = events_result.scalars().all()
            
            return {
                "client_id": client_id,
                "status": {
                    "last_request": client_status.last_request if client_status else None,
                    "total_requests": client_status.total_requests if client_status else 0,
                    "blocked_until": client_status.blocked_until if client_status else None,
                    "current_tokens": client_status.current_tokens if client_status else None,
                    "is_active": client_status.is_active if client_status else True
                },
                "recent_events": [
                    {
                        "endpoint": event.endpoint,
                        "event_type": event.event_type,
                        "timestamp": event.timestamp,
                        "details": json.loads(event.details) if event.details else {}
                    }
                    for event in recent_events
                ]
            }

# Global rate limiter manager
rate_limiter_manager = RateLimiterManager()
