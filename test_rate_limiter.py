import pytest
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_create_and_get_config():
    # Create config
    response = client.post("/config", json={"key": "test_key", "capacity": 5, "refill_rate": 1.0})
    assert response.status_code == 200
    assert response.json()["message"] == "Config created successfully"

    # Get config
    response = client.get("/config/test_key")
    assert response.status_code == 200
    data = response.json()
    assert data["key"] == "test_key"
    assert data["capacity"] == 5
    assert data["refill_rate"] == 1.0

def test_rate_limit_allowance():
    client.post("/config", json={"key": "limit_key", "capacity": 2, "refill_rate": 1.0})
    
    # First 2 requests should pass
    assert client.get("/allow/limit_key").json()["allowed"] == True
    assert client.get("/allow/limit_key").json()["allowed"] == True
    # Third request should fail
    assert client.get("/allow/limit_key").json()["allowed"] == False
