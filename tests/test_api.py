from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import status, HTTPException

from app.db.crud import FETCH_FAILED_STATUS
from app.utils.url_normalization import normalize_url


# --- POST Endpoint Tests ---


@patch("app.api.endpoints.create_metadata_record")
@patch("app.api.endpoints.fetch_url_metadata")
def test_create_metadata_success(
    mock_fetch, mock_create, client, mock_url, mock_metadata_doc
):
    # Arrange: Normalize URL the same way as the application
    normalized_url = normalize_url(mock_url)
    mock_metadata_doc["url"] = normalized_url
    mock_fetch.return_value = mock_metadata_doc
    mock_create.return_value = mock_metadata_doc

    # Act: Make a POST request to the API
    response = client.post("/api/v1/metadata", json={"url": mock_url})

    # Assert: Verify the response matches our expectations
    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["data"]["url"] == normalized_url

    # Verify our mocked functions were actually called with the normalized URL
    mock_fetch.assert_called_once_with(normalized_url)
    mock_create.assert_called_once()


# --- GET Endpoint Tests ---


@patch("app.api.endpoints.get_metadata_by_url")
def test_get_metadata_cache_hit(mock_get, client, mock_url, mock_metadata_doc):
    """Test the Immediate Resolution workflow when data exists."""
    # Arrange: Simulate the database finding the record with normalized URL
    normalized_url = normalize_url(mock_url)
    mock_metadata_doc["url"] = normalized_url
    mock_get.return_value = mock_metadata_doc

    # Act: Make a GET request
    response = client.get(f"/api/v1/metadata?url={mock_url}")

    # Assert: We should get a 200 OK and the full dataset
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["data"]["url"] == normalized_url
    mock_get.assert_called_once_with(normalized_url)


@patch("app.api.endpoints.get_metadata_by_url")
def test_get_metadata_cache_miss_triggers_background_task(
    mock_get, client, mock_url
):
    """Test the Conditional Inventory Update workflow when data is missing."""
    # Arrange: Simulate the database returning nothing
    mock_get.return_value = None

    # Act: Make a GET request
    response = client.get(f"/api/v1/metadata?url={mock_url}")

    # Assert: We must receive a 202 Accepted status
    normalized_url = normalize_url(mock_url)
    assert response.status_code == status.HTTP_202_ACCEPTED
    assert "A background task has been initiated" in response.json()["message"]
    mock_get.assert_called_once_with(normalized_url)


# --- Error Handling Tests ---


@patch("app.api.endpoints.create_metadata_record")
@patch("app.api.endpoints.fetch_url_metadata")
def test_create_metadata_db_error_returns_503(
    mock_fetch, mock_create, client, mock_url, mock_metadata_doc
):
    """If the database write fails, the API should surface a 503."""
    normalized_url = normalize_url(mock_url)
    mock_metadata_doc["url"] = normalized_url
    mock_fetch.return_value = mock_metadata_doc
    mock_create.side_effect = Exception("database down")

    response = client.post("/api/v1/metadata", json={"url": mock_url})

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json()["detail"] == "Database unavailable. Please try again later."


@patch("app.api.endpoints.get_metadata_by_url")
def test_get_metadata_db_error_returns_503(mock_get, client, mock_url):
    """If the database read fails, the API should surface a 503."""
    mock_get.side_effect = Exception("database down")

    response = client.get(f"/api/v1/metadata?url={mock_url}")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json()["detail"] == "Database unavailable. Please try again later."


@patch("app.api.endpoints.fetch_url_metadata")
def test_create_metadata_http_error_propagates(mock_fetch, client, mock_url):
    """HTTP errors from the scraper should be propagated as-is to the client."""
    mock_fetch.side_effect = HTTPException(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        detail="Request timed out.",
    )

    response = client.post("/api/v1/metadata", json={"url": mock_url})

    assert response.status_code == status.HTTP_504_GATEWAY_TIMEOUT
    assert response.json()["detail"] == "Request timed out."


@patch("app.api.endpoints.record_fetch_failure")
@patch("app.api.endpoints.fetch_url_metadata")
def test_create_metadata_403_records_failure_and_reraises(
    mock_fetch, mock_record_failure, client, mock_url
):
    """POST records fetch failure (e.g. 403) so GET can return 503, then re-raises."""
    mock_fetch.side_effect = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="HTTP Error 403 for https://chatgpt.com/",
    )

    response = client.post("/api/v1/metadata", json={"url": mock_url})

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert "403" in response.json()["detail"]
    mock_record_failure.assert_called_once()
    call_args = mock_record_failure.call_args[0]
    assert normalize_url(mock_url) in call_args[0]
    assert "403" in call_args[1]


@patch("app.api.endpoints.get_metadata_by_url")
def test_get_metadata_returns_503_when_recent_fetch_failed(mock_get, client, mock_url):
    """GET returns 503 with error message when a recent failure record exists."""
    normalized_url = normalize_url(mock_url)
    mock_get.return_value = {
        "url": normalized_url,
        "status": FETCH_FAILED_STATUS,
        "error_message": "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed",
        "failed_at": datetime.now(timezone.utc),
        "retry_after": datetime.now(timezone.utc) + timedelta(minutes=5),
    }

    response = client.get(f"/api/v1/metadata?url={mock_url}")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert "Metadata fetch failed" in response.json()["detail"]
    assert "CERTIFICATE_VERIFY_FAILED" in response.json()["detail"]


@patch("app.api.endpoints.get_metadata_by_url")
def test_get_metadata_schedules_retry_after_failure_window_passed(
    mock_get, client, mock_url
):
    """GET returns 202 and schedules background task when failure retry window has passed."""
    normalized_url = normalize_url(mock_url)
    mock_get.return_value = {
        "url": normalized_url,
        "status": FETCH_FAILED_STATUS,
        "error_message": "HTTP Error 403",
        "failed_at": datetime.now(timezone.utc) - timedelta(minutes=10),
        "retry_after": datetime.now(timezone.utc) - timedelta(minutes=5),
    }

    response = client.get(f"/api/v1/metadata?url={mock_url}")

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert "background task has been initiated" in response.json()["message"]


def test_normalize_url_collapses_trailing_slashes():
    original = "https://linkedin.com//"
    normalized = normalize_url(original)
    assert normalized == "https://linkedin.com/"
