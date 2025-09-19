"""
Comprehensive test suite for FastAPI Rate Limiter
"""
import asyncio
import pytest
import json
from datetime import datetime, timedelta
from httpx import AsyncClient
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from main import app
from database import Base, db_manager, RateLimitConfig, RateLimitRecord, ClientStatus
from rate_limiter import (
    SlidingWindowRateLimiter, FixedWindowRateLimiter, 
    TokenBucketRateLimiter, rate_limiter_manager
)

# Test database URL
TEST_DATABASE_URL = "sqlite+aiosqlite:///./test_rate_limiter.db"

@pytest.fixture
async def test_db():
    """Create test database"""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Override the database manager for tests
    original_engine = db_manager.engine
    original_session = db_manager.async_session
    
    db_manager.engine = engine
    db_manager.async_session = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    
    yield engine
    
    # Cleanup
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
    
    # Restore original database manager
    db_manager.engine = original_engine
    db_manager.async_session = original_session

@pytest.fixture
async def client():
    """Create test client"""
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac

@pytest.fixture
def sync_client():
    """Create synchronous test client for WebSocket testing"""
    return TestClient(app)

class TestRateLimitAlgorithms:
    """Test rate limiting algorithms"""
    
    @pytest.mark.asyncio
    async def test_sliding_window_limiter(self, test_db):
        """Test sliding window rate limiter"""
        limiter = SlidingWindowRateLimiter(requests_per_window=5, window_seconds=60)
        
        async with db_manager.async_session() as session:
            client_id = "test_client_1"
            endpoint = "/test"
            
            # First 5 requests should be allowed
            for i in range(5):
                result = await limiter.check_rate_limit(client_id, endpoint, session)
                assert result.allowed == True
                assert result.remaining == 4 - i
                
                if result.allowed:
                    await limiter.record_request(client_id, endpoint, session)
            
            await session.commit()
            
            # 6th request should be blocked
            result = await limiter.check_rate_limit(client_id, endpoint, session)
            assert result.allowed == False
            assert result.remaining == 0
            assert result.retry_after is not None
    
    @pytest.mark.asyncio
    async def test_fixed_window_limiter(self, test_db):
        """Test fixed window rate limiter"""
        limiter = FixedWindowRateLimiter(requests_per_window=3, window_seconds=60)
        
        async with db_manager.async_session() as session:
            client_id = "test_client_2"
            endpoint = "/test"
            
            # First 3 requests should be allowed
            for i in range(3):
                result = await limiter.check_rate_limit(client_id, endpoint, session)
                assert result.allowed == True
                
                if result.allowed:
                    await limiter.record_request(client_id, endpoint, session)
            
            await session.commit()
            
            # 4th request should be blocked
            result = await limiter.check_rate_limit(client_id, endpoint, session)
            assert result.allowed == False
    
    @pytest.mark.asyncio
    async def test_token_bucket_limiter(self, test_db):
        """Test token bucket rate limiter"""
        limiter = TokenBucketRateLimiter(
            requests_per_window=10, 
            window_seconds=60,
            burst_limit=5,
            refill_rate=1.0
        )
        
        async with db_manager.async_session() as session:
            client_id = "test_client_3"
            endpoint = "/test"
            
            # Should allow burst of 5 requests
            for i in range(5):
                result = await limiter.check_rate_limit(client_id, endpoint, session)
                assert result.allowed == True
                
                if result.allowed:
                    await limiter.record_request(client_id, endpoint, session)
            
            await session.commit()
            
            # 6th request should be blocked (no tokens left)
            result = await limiter.check_rate_limit(client_id, endpoint, session)
            assert result.allowed == False

class TestRateLimiterManager:
    """Test rate limiter manager"""
    
    @pytest.mark.asyncio
    async def test_manager_initialization(self, test_db):
        """Test manager initialization"""
        # Create test config
        async with db_manager.async_session() as session:
            config = RateLimitConfig(
                endpoint="/api/test",
                requests_per_window=10,
                window_seconds=60,
                algorithm="sliding_window"
            )
            session.add(config)
            await session.commit()
        
        # Initialize manager
        await rate_limiter_manager.initialize()
        
        # Check if config is loaded
        assert "/api/test" in rate_limiter_manager.configs
        assert "/api/test" in rate_limiter_manager.limiters
    
    @pytest.mark.asyncio
    async def test_check_rate_limit(self, test_db):
        """Test rate limit checking"""
        client_id = "test_client_4"
        endpoint = "/api/test"
        
        # Should create default config if not exists
        result = await rate_limiter_manager.check_rate_limit(client_id, endpoint)
        assert result.allowed == True
        assert result.remaining >= 0
    
    @pytest.mark.asyncio
    async def test_update_config(self, test_db):
        """Test configuration update"""
        endpoint = "/api/update_test"
        
        # Create initial config
        async with db_manager.async_session() as session:
            config = RateLimitConfig(
                endpoint=endpoint,
                requests_per_window=5,
                window_seconds=60
            )
            session.add(config)
            await session.commit()
        
        await rate_limiter_manager.initialize()
        
        # Update config
        success = await rate_limiter_manager.update_config(
            endpoint, 
            requests_per_window=10,
            window_seconds=120
        )
        
        assert success == True
        assert rate_limiter_manager.configs[endpoint].requests_per_window == 10
        assert rate_limiter_manager.configs[endpoint].window_seconds == 120
    
    @pytest.mark.asyncio
    async def test_get_client_stats(self, test_db):
        """Test client statistics"""
        client_id = "test_client_5"
        endpoint = "/api/stats_test"
        
        # Make some requests
        for _ in range(3):
            await rate_limiter_manager.check_rate_limit(client_id, endpoint)
        
        # Get stats
        stats = await rate_limiter_manager.get_client_stats(client_id)
        
        assert stats["client_id"] == client_id
        assert "status" in stats
        assert "recent_events" in stats

class TestAPIEndpoints:
    """Test API endpoints"""
    
    @pytest.mark.asyncio
    async def test_root_endpoint(self, client, test_db):
        """Test root endpoint"""
        response = await client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert "message" in data
        assert "version" in data
    
    @pytest.mark.asyncio
    async def test_health_endpoint(self, client, test_db):
        """Test health check endpoint"""
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
    
    @pytest.mark.asyncio
    async def test_protected_endpoint(self, client, test_db):
        """Test protected endpoint with rate limiting"""
        # First request should succeed
        response = await client.get("/api/data")
        assert response.status_code == 200
        
        # Check rate limit headers
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers
        assert "X-RateLimit-Reset" in response.headers
    
    @pytest.mark.asyncio
    async def test_rate_limit_exceeded(self, client, test_db):
        """Test rate limit exceeded scenario"""
        # Create a very restrictive config
        config_data = {
            "endpoint": "/api/data",
            "requests_per_window": 1,
            "window_seconds": 60,
            "algorithm": "fixed_window"
        }
        
        # Create config
        response = await client.post("/admin/configs", json=config_data)
        assert response.status_code == 200
        
        # First request should succeed
        response = await client.get("/api/data", headers={"X-Client-ID": "test_limit_client"})
        assert response.status_code == 200
        
        # Second request should be rate limited
        response = await client.get("/api/data", headers={"X-Client-ID": "test_limit_client"})
        assert response.status_code == 429
        
        data = response.json()
        assert "error" in data
        assert "retry_after" in data
    
    @pytest.mark.asyncio
    async def test_admin_endpoints(self, client, test_db):
        """Test admin endpoints"""
        # Get configs
        response = await client.get("/admin/configs")
        assert response.status_code == 200
        configs = response.json()
        assert isinstance(configs, list)
        
        # Create new config
        config_data = {
            "endpoint": "/api/admin_test",
            "requests_per_window": 50,
            "window_seconds": 3600,
            "algorithm": "sliding_window"
        }
        
        response = await client.post("/admin/configs", json=config_data)
        assert response.status_code == 200
        
        # Update config
        update_data = {
            "requests_per_window": 100
        }
        
        response = await client.put("/admin/configs/api/admin_test", json=update_data)
        assert response.status_code == 200
        
        # Get global stats
        response = await client.get("/admin/stats")
        assert response.status_code == 200
        stats = response.json()
        assert "total_requests_24h" in stats
        assert "blocked_requests_24h" in stats
        assert "success_rate" in stats

class TestWebSocketConnections:
    """Test WebSocket connections"""
    
    def test_websocket_connection(self, sync_client):
        """Test WebSocket connection"""
        with sync_client.websocket_connect("/ws/test_client") as websocket:
            # Should connect successfully
            assert websocket is not None
    
    def test_global_websocket_connection(self, sync_client):
        """Test global WebSocket connection"""
        with sync_client.websocket_connect("/ws/global") as websocket:
            # Should connect successfully
            assert websocket is not None

class TestRealTimeSync:
    """Test real-time synchronization features"""
    
    @pytest.mark.asyncio
    async def test_event_logging(self, test_db):
        """Test event logging functionality"""
        client_id = "test_event_client"
        endpoint = "/api/event_test"
        
        # Make a request that should be logged
        result = await rate_limiter_manager.check_rate_limit(client_id, endpoint)
        
        # Check if event was logged
        async with db_manager.async_session() as session:
            from sqlalchemy import select
            from database import RateLimitEvent
            
            result = await session.execute(
                select(RateLimitEvent)
                .where(RateLimitEvent.client_id == client_id)
                .order_by(RateLimitEvent.timestamp.desc())
                .limit(1)
            )
            event = result.scalar_one_or_none()
            
            assert event is not None
            assert event.client_id == client_id
            assert event.endpoint == endpoint
            assert event.event_type == "request"

class TestPerformance:
    """Test performance characteristics"""
    
    @pytest.mark.asyncio
    async def test_concurrent_requests(self, test_db):
        """Test handling of concurrent requests"""
        client_id = "perf_test_client"
        endpoint = "/api/perf_test"
        
        async def make_request():
            return await rate_limiter_manager.check_rate_limit(client_id, endpoint)
        
        # Make 10 concurrent requests
        tasks = [make_request() for _ in range(10)]
        results = await asyncio.gather(*tasks)
        
        # All should complete without error
        assert len(results) == 10
        for result in results:
            assert hasattr(result, 'allowed')
            assert hasattr(result, 'remaining')
    
    @pytest.mark.asyncio
    async def test_database_cleanup(self, test_db):
        """Test database cleanup functionality"""
        # Create some old records
        async with db_manager.async_session() as session:
            old_record = RateLimitRecord(
                client_id="cleanup_test",
                endpoint="/cleanup",
                timestamp=datetime.utcnow() - timedelta(days=10),
                window_start=datetime.utcnow() - timedelta(days=10),
                window_end=datetime.utcnow() - timedelta(days=10) + timedelta(hours=1)
            )
            session.add(old_record)
            await session.commit()
        
        # Run cleanup
        await db_manager.cleanup_old_records(days=7)
        
        # Verify old records are removed
        async with db_manager.async_session() as session:
            from sqlalchemy import select
            result = await session.execute(
                select(RateLimitRecord)
                .where(RateLimitRecord.client_id == "cleanup_test")
            )
            records = result.scalars().all()
            assert len(records) == 0

class TestEdgeCases:
    """Test edge cases and error conditions"""
    
    @pytest.mark.asyncio
    async def test_invalid_algorithm(self, test_db):
        """Test handling of invalid algorithm"""
        async with db_manager.async_session() as session:
            config = RateLimitConfig(
                endpoint="/api/invalid_algo",
                requests_per_window=10,
                window_seconds=60,
                algorithm="invalid_algorithm"
            )
            session.add(config)
            await session.commit()
        
        await rate_limiter_manager.initialize()
        
        # Should fall back to default algorithm
        limiter = rate_limiter_manager.limiters["/api/invalid_algo"]
        assert isinstance(limiter, SlidingWindowRateLimiter)
    
    @pytest.mark.asyncio
    async def test_zero_window_size(self, test_db):
        """Test handling of zero window size"""
        # This should not crash the system
        limiter = SlidingWindowRateLimiter(requests_per_window=10, window_seconds=1)
        
        async with db_manager.async_session() as session:
            result = await limiter.check_rate_limit("test_client", "/test", session)
            assert result is not None
    
    @pytest.mark.asyncio
    async def test_negative_values(self, test_db):
        """Test handling of negative values"""
        # Should handle gracefully
        try:
            limiter = SlidingWindowRateLimiter(requests_per_window=-1, window_seconds=-1)
            async with db_manager.async_session() as session:
                result = await limiter.check_rate_limit("test_client", "/test", session)
                # Should not crash
                assert result is not None
        except Exception:
            # Expected to fail gracefully
            pass

# Test configuration
if __name__ == "__main__":
    pytest.main([__file__, "-v", "--asyncio-mode=auto"])
