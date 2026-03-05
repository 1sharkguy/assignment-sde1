from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """
    Central application configuration.

    Values are read from environment variables, falling back to safe defaults
    when not explicitly provided. A local `.env` file is also loaded when
    present (see Config below).
    """

    PROJECT_NAME: str = "HTTP Metadata Inventory"
    # Default to localhost for local testing outside Docker
    MONGODB_URL: str = "mongodb://localhost:27017"
    DATABASE_NAME: str = "metadata_db"

    # Mongo collection and retry behaviour
    COLLECTION_NAME: str = "url_metadata"
    DEFAULT_RETRY_AFTER_SECONDS: int = 300  # 5 minutes

    # Simple in-process rate limiting (per IP, per minute)
    RATE_LIMIT_REQUESTS_PER_MINUTE: int = 60

    # # Celery / task queue configuration
    # CELERY_BROKER_URL: str = "redis://redis:6379/0"
    # CELERY_RESULT_BACKEND: str = "redis://redis:6379/0"
    # CELERY_SCRAPE_RATE_LIMIT: str = "20/m"

    class ConfigDict:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Instantiate the settings so they can be imported across the app
settings = Settings()