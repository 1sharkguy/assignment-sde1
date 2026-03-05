from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from app.db.mongodb import connect_to_mongo, close_mongo_connection
from app.db.crud import setup_database_indexes
from app.services.scraper import init_http_client, close_http_client
from app.api.endpoints import router as metadata_router
import logging


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Connect to DB, ensure indexes, and initialize shared HTTP client
    logger.info("Application startup initiated")
    await connect_to_mongo()
    await setup_database_indexes()
    await init_http_client()
    logger.info("Application startup completed")
    yield
    # Shutdown: Close shared HTTP client and DB
    logger.info("Application shutdown initiated")
    await close_http_client()
    await close_mongo_connection()
    logger.info("Application shutdown completed")


app = FastAPI(title="HTTP Metadata Inventory API", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """
    Catch-all handler to ensure unexpected errors are logged and returned gracefully.
    """
    logger.exception(
        "Unhandled error while processing %s %s", request.method, request.url.path
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. Please try again later."},
    )


# Register the endpoints
app.include_router(metadata_router, prefix="/api/v1")