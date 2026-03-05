import httpx
from fastapi import HTTPException
from app.models.metadata import MetadataDocument
from datetime import datetime, timezone
from typing import Optional
import logging


_http_client: Optional[httpx.AsyncClient] = None
logger = logging.getLogger(__name__)


async def init_http_client():
    """
    Initialize a shared AsyncClient for outbound HTTP calls.
    """
    global _http_client
    if _http_client is None:
        limits = httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
        )
        _http_client = httpx.AsyncClient(
            timeout=10.0,
            limits=limits,
            follow_redirects=True,
        )
        logger.info("Initialized shared HTTP client with connection limits")


async def close_http_client():
    """
    Close the shared AsyncClient, releasing connection pool resources.
    """
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None
        logger.info("Closed shared HTTP client")


async def fetch_url_metadata(url: str, client: Optional[httpx.AsyncClient] = None):
    """
    Asynchronously fetches headers, cookies, and page source for a given URL
    using a shared AsyncClient by default.
    """
    http_client = client or _http_client
    if http_client is None:
        # Defensive fallback: should not happen when init_http_client is called at startup.
        raise RuntimeError("HTTP client is not initialized")

    try:
        logger.debug("Fetching metadata for url=%s", url)
        response = await http_client.get(url)
        response.raise_for_status()  # Raise an exception for 4xx/5xx errors

        # Extract the required data points
        headers = dict(response.headers)
        cookies = dict(response.cookies)
        page_source = response.text

        # Return data formatted to our Pydantic model
        result = MetadataDocument(
            url=url,
            headers=headers,
            cookies=cookies,
            page_source=page_source,
            created_at=datetime.now(timezone.utc),
        )
        logger.debug("Fetched metadata successfully for url=%s", url)
        return result

    except httpx.TimeoutException:
        logger.warning("Request to %s timed out", url)
        raise HTTPException(status_code=504, detail=f"Request to {url} timed out.")
    except httpx.RequestError as exc:
        logger.warning("Request error for url=%s: %s", url, exc)
        raise HTTPException(status_code=400, detail=f"Error requesting {url}: {str(exc)}")
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "HTTP status error for url=%s: status=%s", url, exc.response.status_code
        )
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"HTTP Error {exc.response.status_code} for {url}",
        )