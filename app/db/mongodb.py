from motor.motor_asyncio import AsyncIOMotorClient
from app.core.config import settings
import asyncio
from pymongo.errors import PyMongoError
import logging


class Database:
    client: AsyncIOMotorClient = None


db = Database()
logger = logging.getLogger(__name__)


async def connect_to_mongo(retries: int = 5, delay: float = 1.0):
    """
    Initializes the database connection and waits until MongoDB is ready.
    Retries a few times to tolerate startup delays.
    """
    db.client = AsyncIOMotorClient(settings.MONGODB_URL)

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            await db.client.admin.command("ping")
            logger.info("Connected to MongoDB at %s", settings.MONGODB_URL)
            return
        except PyMongoError as exc:
            last_error = exc
            logger.warning(
                "MongoDB not ready (attempt %s/%s): %s", attempt, retries, exc
            )
            await asyncio.sleep(delay)

    # If we exhausted all retries, surface the last error so the app fails fast.
    logger.error("Unable to connect to MongoDB after %s attempts", retries)
    raise last_error if last_error is not None else RuntimeError("Unable to connect to MongoDB")

async def close_mongo_connection():
    """Closes the database connection cleanly."""
    if db.client:
        db.client.close()
        logger.info("Closed MongoDB connection")

def get_database():
    """Utility to retrieve the specific database instance."""
    return db.client[settings.DATABASE_NAME]