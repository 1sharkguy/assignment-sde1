import pytest
from fastapi.testclient import TestClient
from app.main import app

@pytest.fixture
def client():
    """
    Creates a FastAPI TestClient instance. 
    This allows us to make requests to our API without spinning up a real server.
    """
    return TestClient(app)

@pytest.fixture
def mock_url():
    return "https://example.com"

@pytest.fixture
def mock_metadata_doc(mock_url):
    return {
        "url": mock_url,
        "headers": {"content-type": "text/html"},
        "cookies": {"session": "12345"},
        "page_source": "<html><body>Mocked Data</body></html>",
        "created_at": "2026-03-04T10:00:00Z"
    }