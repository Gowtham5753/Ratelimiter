"""
Database models and configuration for the FastAPI Rate Limiter
"""
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List
from sqlalchemy import Column, Integer, String, DateTime, Float, Boolean, Text, Index
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func
from sqlalchemy.orm import sessionmaker
import aiosqlite
import json

Base = declarative_base()

class RateLimitRecord(Base):
    """Rate limit tracking record"""
    __tablename__ = "rate_limit_records"
    
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(String(255), nullable=False, index=True)
    endpoint = Column(String(255), nullable=False, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    request_count = Column(Integer, default=1, nullable=False)
    window_start = Column(DateTime, nullable=False)
    window_end = Column(DateTime, nullable=False)
    is_blocked = Column(Boolean, default=False, nullable=False)
    
    # Composite index for efficient queries
    __table_args__ = (
        Index('idx_client_endpoint_window', 'client_id', 'endpoint', 'window_start'),
        Index('idx_timestamp', 'timestamp'),
    )

class RateLimitConfig(Base):
    """Rate limit configuration per endpoint"""
    __tablename__ = "rate_limit_configs"
    
    id = Column(Integer, primary_key=True, index=True)
    endpoint = Column(String(255), nullable=False, unique=True, index=True)
    requests_per_window = Column(Integer, nullable=False, default=100)
    window_seconds = Column(Integer, nullable=False, default=3600)  # 1 hour default
    algorithm = Column(String(50), nullable=False, default="sliding_window")  # sliding_window, token_bucket, fixed_window
    burst_limit = Column(Integer, nullable=True)  # For token bucket
    refill_rate = Column(Float, nullable=True)  # For token bucket
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

class ClientStatus(Base):
    """Real-time client status for sync"""
    __tablename__ = "client_status"
    
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(String(255), nullable=False, unique=True, index=True)
    last_request = Column(DateTime, default=datetime.utcnow, nullable=False)
    total_requests = Column(Integer, default=0, nullable=False)
    blocked_until = Column(DateTime, nullable=True)
    current_tokens = Column(Float, nullable=True)  # For token bucket algorithm
    last_refill = Column(DateTime, nullable=True)  # For token bucket algorithm
    metadata = Column(Text, nullable=True)  # JSON metadata
    is_active = Column(Boolean, default=True, nullable=False)

class RateLimitEvent(Base):
    """Event log for real-time sync and monitoring"""
    __tablename__ = "rate_limit_events"
    
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(String(255), nullable=False, index=True)
    endpoint = Column(String(255), nullable=False, index=True)
    event_type = Column(String(50), nullable=False)  # request, block, unblock, config_change
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    details = Column(Text, nullable=True)  # JSON details
    
    __table_args__ = (
        Index('idx_client_event_time', 'client_id', 'event_type', 'timestamp'),
    )

class DatabaseManager:
    """Async database manager"""
    
    def __init__(self, database_url: str = "sqlite+aiosqlite:///./rate_limiter.db"):
        self.database_url = database_url
        self.engine = create_async_engine(
            database_url,
            echo=False,
            pool_pre_ping=True,
            pool_recycle=300,
        )
        self.async_session = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False
        )
    
    async def create_tables(self):
        """Create all tables"""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    
    async def get_session(self) -> AsyncSession:
        """Get async database session"""
        async with self.async_session() as session:
            yield session
    
    async def cleanup_old_records(self, days: int = 7):
        """Clean up old rate limit records"""
        cutoff_date = datetime.utcnow() - timedelta(days=days)
        async with self.async_session() as session:
            # Clean old rate limit records
            await session.execute(
                f"DELETE FROM rate_limit_records WHERE timestamp < '{cutoff_date}'"
            )
            # Clean old events
            await session.execute(
                f"DELETE FROM rate_limit_events WHERE timestamp < '{cutoff_date}'"
            )
            await session.commit()

# Global database manager instance
db_manager = DatabaseManager()
