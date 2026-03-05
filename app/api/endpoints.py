from datetime import datetime, timezone

from fastapi import APIRouter, status, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import HttpUrl
from app.models.metadata import URLRequest
from app.services.scraper import fetch_url_metadata
from app.db.crud import (
    get_metadata_by_url,
    create_metadata_record,
    record_fetch_failure,
    FETCH_FAILED_STATUS,
    mark_scrape_in_flight,
    clear_scrape_in_flight,
)
from app.utils.url_normalization import normalize_url
from app.core.rate_limit import rate_limiter
from typing import Any, Dict
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


async def background_scrape_and_store(url: str):
    """
    The background worker logic that runs independently of the request-response cycle.
    On failure (SSL, 403, timeout, etc.), persists a fetch-failure record so GET
    can return 503 with the error until the retry window passes.
    """
    canonical_url = normalize_url(url)

    try:
        logger.info("Background scrape started for url=%s", canonical_url)
        metadata = await fetch_url_metadata(canonical_url)
        await create_metadata_record(metadata)
        logger.info("Background scrape completed for url=%s", canonical_url)
    except Exception as exc:
        logger.exception("Background task failed for url=%s", canonical_url)
        try:
            await record_fetch_failure(canonical_url, str(exc))
        except Exception:  # pragma: no cover
            logger.exception("Failed to record fetch failure for url=%s", canonical_url)
    finally:
        # Always clear the in-flight flag so future GETs can schedule new work.
        try:
            await clear_scrape_in_flight(canonical_url)
        except Exception:  # pragma: no cover
            logger.exception("Failed to clear in-flight flag for url=%s", canonical_url)


@router.post(
    "/metadata",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limiter)],
)
async def create_metadata(request: URLRequest):
    """
    1. POST Endpoint: Create a metadata record for a given URL immediately.
    On fetch failure (SSL, 403, etc.), records the failure so GET returns 503
    until the retry window passes, then re-raises so the client gets the error.
    """
    raw_url = str(request.url)
    url_str = normalize_url(raw_url)
    logger.info("Received POST /metadata for url=%s (normalized from %s)", url_str, raw_url)

    try:
        metadata = await fetch_url_metadata(url_str)
    except HTTPException as exc:
        try:
            detail = getattr(exc, "detail", str(exc))
            await record_fetch_failure(url_str, str(detail))
        except Exception:  # pragma: no cover
            logger.exception("Failed to record fetch failure for url=%s", url_str)
        raise
    except Exception as exc:
        try:
            await record_fetch_failure(url_str, str(exc))
        except Exception:  # pragma: no cover
            logger.exception("Failed to record fetch failure for url=%s", url_str)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    try:
        saved_document = await create_metadata_record(metadata)
    except Exception as exc:  # pragma: no cover - safety net
        logger.exception("Database error while creating metadata for url=%s", url_str)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable. Please try again later.",
        ) from exc

    return {"message": "Metadata created successfully", "data": saved_document}


@router.get(
    "/metadata",
    dependencies=[Depends(rate_limiter)],
)
async def retrieve_metadata(
    url: HttpUrl,
):
    """
    2. GET Endpoint: Retrieve metadata or trigger background collection.
    """
    # Normalize URL to the same canonical form used in POST
    raw_url = str(url)
    url_str = normalize_url(raw_url)
    logger.info("Received GET /metadata for url=%s (normalized from %s)", url_str, raw_url)

    # Inventory Check
    try:
        existing_data = await get_metadata_by_url(url_str)
    except Exception as exc:  # pragma: no cover - safety net
        logger.exception("Database error while retrieving metadata for url=%s", url_str)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable. Please try again later.",
        ) from exc

    if existing_data:
        if existing_data.get("status") == FETCH_FAILED_STATUS:
            # Previous fetch failed; respect retry window
            retry_after = existing_data.get("retry_after")
            if retry_after is not None:
                now = datetime.now(timezone.utc)
                # Mongo may return naive UTC; ensure we can compare
                if getattr(retry_after, "tzinfo", None) is None:
                    retry_after = retry_after.replace(tzinfo=timezone.utc)
                if now < retry_after:
                    error_message = existing_data.get("error_message", "Unknown error")
                    logger.info(
                        "Returning 503 for url=%s (fetch failed, retry window active)",
                        url_str,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail=f"Metadata fetch failed: {error_message}. Retry later.",
                    )
            # Retry window passed; schedule again if no other worker is already scraping.
            acquired = await mark_scrape_in_flight(url_str)
            if acquired:
                logger.info(
                    "Retry window passed for url=%s; background task scheduled",
                    url_str,
                )
                # Background work is now delegated to a worker (e.g. Celery or in-process task).
                # For now we reuse the same coroutine directly; this is compatible with BackgroundTasks
                # or external orchestration.
                # In this version, we still trigger it asynchronously from the API process.
                # (Will be wired to Celery in a separate step.)
                from asyncio import create_task

                create_task(background_scrape_and_store(url_str))
            else:
                logger.info(
                    "Retry window passed for url=%s but scrape already in-flight; not scheduling another",
                    url_str,
                )
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={
                    "message": f"Metadata for {url_str} not found. A background task has been initiated to fetch it."
                },
            )
        # Success document (has metadata)
        logger.info("Cache hit for url=%s", url_str)
        return {"data": existing_data}

    # Conditional Inventory Update: Cache miss occurs
    acquired = await mark_scrape_in_flight(url_str)
    if acquired:
        logger.info("Cache miss for url=%s; background task scheduled", url_str)
        from asyncio import create_task

        create_task(background_scrape_and_store(url_str))
    else:
        logger.info(
            "Cache miss for url=%s but scrape already in-flight; not scheduling another",
            url_str,
        )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "message": f"Metadata for {url_str} not found. A background task has been initiated to fetch it."
        },
    )

@router.get("/health")
async def health_check():
    return {"status": "ok"}