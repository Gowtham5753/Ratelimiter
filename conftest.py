import pytest
from database import reset_database

@pytest.fixture(autouse=True)
def setup_db():
    reset_database()
