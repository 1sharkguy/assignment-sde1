from datetime import datetime, timedelta, timezone

from app.db.mongodb import get_database
from app.models.metadata import MetadataDocument
from app.core.config import settings
from pymongo import IndexModel, ASCENDING
import logging


COLLECTION_NAME = settings.COLLECTION_NAME
FETCH_FAILED_STATUS = "fetch_failed"
DEFAULT_RETRY_AFTER_SECONDS = settings.DEFAULT_RETRY_AFTER_SECONDS
logger = logging.getLogger(__name__)


async def setup_database_indexes():
    """Creates a unique index on the 'url' field to optimize lookups."""
    database = get_database()
    collection = database[COLLECTION_NAME]

    index = IndexModel([("url", ASCENDING)], unique=True)
    await collection.create_indexes([index])
    logger.info("Ensured unique index on 'url' for collection=%s", COLLECTION_NAME)


async def get_metadata_by_url(url: str):
    """Retrieves the metadata document for a specific URL."""
    database = get_database()
    collection = database[COLLECTION_NAME]

    document = await collection.find_one({"url": url})
    if document:
        document.pop("_id", None)
        logger.debug("Found metadata document for url=%s", url)
    else:
        logger.debug("No metadata document found for url=%s", url)
    return document


async def create_metadata_record(metadata: MetadataDocument):
    """Upserts a metadata document for the given URL and clears any failure state."""
    database = get_database()
    collection = database[COLLECTION_NAME]

    document = metadata.model_dump()

    await collection.update_one(
        {"url": document["url"]},
        {
            "$set": {
                **document,
                "status": "success",
            },
            "$unset": {
                "error_message": "",
                "failed_at": "",
                "retry_after": "",
                "in_flight": "",
                "in_flight_since": "",
            },
        },
        upsert=True,
    )
    logger.info("Upserted metadata document for url=%s", document["url"])
    return document


async def record_fetch_failure(
    url: str,
    error_message: str,
    retry_after_seconds: int = DEFAULT_RETRY_AFTER_SECONDS,
) -> None:
    """
    Record a failed metadata fetch for the given URL so GET can return 503
    until the retry window passes, avoiding repeated background task failures.
    """
    database = get_database()
    collection = database[COLLECTION_NAME]
    now = datetime.now(timezone.utc)
    retry_after = now + timedelta(seconds=retry_after_seconds)
    update_doc = {
        "status": FETCH_FAILED_STATUS,
        "error_message": error_message,
        "failed_at": now,
        "retry_after": retry_after,
    }
    await collection.update_one(
        {"url": url},
        {"$set": update_doc},
        upsert=True,
    )
    logger.info(
        "Recorded fetch failure for url=%s (retry after %s)",
        url,
        retry_after,
    )


async def mark_scrape_in_flight(url: str) -> bool:
    """
    Mark a scrape as in-flight for the given URL.

    Returns True if this call acquired the in-flight flag and a new background
    scrape should be scheduled, or False if another worker is already handling it.
    """
    database = get_database()
    collection = database[COLLECTION_NAME]
    now = datetime.now(timezone.utc)

    result = await collection.update_one(
        {"url": url, "in_flight": {"$ne": True}},
        {
            "$set": {
                "in_flight": True,
                "in_flight_since": now,
            }
        },
        upsert=True,
    )

    # We "own" the in-flight flag if we either inserted a new doc
    # or modified an existing one that previously was not in-flight.
    return bool(result.upserted_id) or (result.matched_count > 0 and result.modified_count > 0)


async def clear_scrape_in_flight(url: str) -> None:
    """
    Clear the in-flight flag once a background scrape has completed.
    """
    database = get_database()
    collection = database[COLLECTION_NAME]
    await collection.update_one(
        {"url": url},
        {"$unset": {"in_flight": "", "in_flight_since": ""}},
    )