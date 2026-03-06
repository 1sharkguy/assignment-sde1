## HTTP Metadata Inventory Service

A small backend service that inventories HTTP response metadata (headers, cookies, page source) for arbitrary URLs.  
The service exposes a REST API built with FastAPI, persists data in MongoDB, and runs via Docker Compose.

It was implemented for the **SDE Backend Engineering Hiring Challenge** and is designed to demonstrate:

- **Correct background-worker behavior** on cache misses (no self-HTTP calls).
- **Resilient handling** of external HTTP and database issues.
- **Clear separation of concerns** (API, business logic, persistence, infrastructure).
- **Efficient I/O handling** via a shared `httpx.AsyncClient`.

---

## Features

- **POST `/api/v1/metadata`**: Fetch metadata for a URL and upsert it into MongoDB.
- **GET `/api/v1/metadata`**: Return cached metadata if present; otherwise trigger background scraping and immediately respond with `202 Accepted`.
- **GET `/api/v1/health`**: Simple health check for the API.
- **Basic per-IP rate limiting**: Simple in-process limiter to protect the service from abuse.
- **URL canonicalization**: Robust normalization of URLs (scheme/host case, default ports, duplicate slashes, query ordering) so logically identical URLs map to a single cache/database key.
- **Async FastAPI** with background tasks for non-blocking cache-miss handling.
- **MongoDB persistence** with a unique index on `url` for efficient lookups.
- **Resilience**:
  - Retry and readiness checks when connecting to MongoDB.
  - Graceful error handling for HTTP timeouts, invalid URLs, and DB failures.
- **Fetch failure handling**: When a metadata fetch fails (e.g. SSL certificate errors in Docker, or 403 from the target site), the failure is persisted with a retry window (default 5 minutes). GET returns `503` with the error message until the window passes, then schedules a new background attempt. POST records the failure and re-raises the error to the client (403, 504, 400, etc.).
- **Containerized** with Docker & Docker Compose, running the API.
- **Configurable** via environment variables (`.env` / `.env.example`).
- **Tests** written using `pytest` and `pytest-asyncio`.

---

## Tech Stack

- **Language**: Python 3.11
- **Framework**: FastAPI
- **Database**: MongoDB (via Motor async driver)
- **HTTP client**: `httpx.AsyncClient` (shared, connection-pooled)
- **Containerization**: Docker, Docker Compose
- **Config**: `pydantic-settings`
- **Testing**: `pytest`, `pytest-asyncio`

---

## High-Level Architecture

### Logical Layers

- **API Layer (`app/api`)**
  - `endpoints.py` defines all HTTP routes:
    - `POST /api/v1/metadata`
    - `GET /api/v1/metadata`
    - `GET /api/v1/health`
  - Responsible only for:
    - Request validation (via Pydantic models).
    - Mapping domain outcomes to HTTP responses and status codes.
    - Scheduling background tasks.

- **Domain / Business Logic Layer (`app/services`, `app/models`)**
  - `app/services/scraper.py`
    - Encapsulates fetching metadata for a URL:
      - Issues HTTP request with shared `httpx.AsyncClient`.
      - Extracts headers, cookies, and page source.
      - Constructs `MetadataDocument` domain model.
    - Handles HTTP-related errors and maps them to proper `HTTPException`s.
  - `app/models/metadata.py`
    - `URLRequest`: request body model for the POST endpoint.
    - `MetadataDocument`: Pydantic model representing stored metadata.

- **Persistence Layer (`app/db`)**
  - `mongodb.py`:
    - Manages a process-wide `AsyncIOMotorClient`:
      - `connect_to_mongo` with retry and `ping`-based readiness check.
      - `close_mongo_connection`.
      - `get_database` helper.
  - `crud.py`:
    - `setup_database_indexes`: ensures a unique index on `url`.
    - `get_metadata_by_url`: retrieves metadata by normalized URL (success or failure record).
    - `create_metadata_record`: upserts metadata documents and clears any previous failure state.
    - `record_fetch_failure`: stores a fetch-failure record (url, error_message, retry_after) so GET can return 503 until the retry window passes, without discarding any previously stored successful metadata fields.

- **Infrastructure / Configuration (`app/main.py`, `app/core`, Docker files)**
  - `app/main.py`:
    - Configures logging globally.
    - Defines FastAPI app with a `lifespan` context manager:
      - On startup:
        - Connect to MongoDB.
        - Set up indexes.
        - Initialize shared HTTP client.
      - On shutdown:
        - Close HTTP client and MongoDB connection.
    - Registers a global exception handler for unhandled errors.
  - `app/core/config.py`:
    - `Settings` (via `pydantic-settings`) loads:
      - `PROJECT_NAME`
      - `MONGODB_URL`
      - `DATABASE_NAME`
      - `COLLECTION_NAME`
      - `DEFAULT_RETRY_AFTER_SECONDS` (retry window for failed fetches)
      - `RATE_LIMIT_REQUESTS_PER_MINUTE` (simple per-IP rate limit)
    - Uses `.env` and environment variables with sensible defaults.
  - `Dockerfile` / `docker-compose.yml`:
    - API container + MongoDB container.
    - MongoDB `healthcheck` for readiness.
    - Volumes for data and tests.

### Background Worker Flow (Cache Miss)

1. **Client calls** `GET /api/v1/metadata?url=<URL>`.
2. FastAPI validates `url` as `HttpUrl`, then the service canonicalizes it using `normalize_url` (e.g., collapses duplicate slashes, lowercases host, removes default ports, and preserves a trailing slash for non-root paths when present).
3. API calls `get_metadata_by_url(canonical_url)`:
   - If **success document exists** (cached metadata):
     - Return `200 OK` with cached metadata (**Immediate Resolution**).
   - If **failure document exists** (e.g. previous fetch failed with SSL/403):
     - If still inside the **retry window** (`now < retry_after`): return `503 Service Unavailable` with the stored error message; do *not* schedule a new task.
     - If **retry window has passed**: attempt to mark the URL as `in_flight` and, if successful, schedule `background_scrape_and_store(canonical_url)` and return `202 Accepted`. If another worker already holds the `in_flight` flag, the service simply returns `202 Accepted` without scheduling a duplicate scrape.
   - If **no document exists**:
     - Attempt to mark the URL as `in_flight`. If successful, schedule `background_scrape_and_store(canonical_url)`; if not, another request has already scheduled it.
     - Immediately return `202 Accepted` with a message (no blocking wait; no self-HTTP call).
4. `background_scrape_and_store`:
   - Calls `fetch_url_metadata(canonical_url)` (shared `httpx.AsyncClient`).
   - On success: calls `create_metadata_record(metadata)` to upsert into MongoDB and clear any previous failure state.
   - On failure (SSL, 403, timeout, etc.): calls `record_fetch_failure(canonical_url, error_message)` so the next GET can return 503 until the retry window passes.
   - In all cases, clears the `in_flight` flag so future GETs can schedule new work after the retry window.
   - Logs the outcome for observability.

Subsequent GETs for the same URL will hit the cached metadata and return `200 OK`, or return `503` with the last error until the retry window passes.

---

## Error Handling & Resilience

### Invalid/Unreachable URLs & HTTP Errors

- The scraper (`fetch_url_metadata`) handles:
  - **Timeouts** (`httpx.TimeoutException`) → `504 Gateway Timeout`.
  - **Request errors** (`httpx.RequestError`) → `400 Bad Request` with details (e.g. `SSL: CERTIFICATE_VERIFY_FAILED` in Docker).
  - **Non-2xx HTTP status codes** (`httpx.HTTPStatusError`) → passes through corresponding status code (e.g. `403 Forbidden`, `404`, `500`).
- **POST**: These errors are raised as FastAPI `HTTPException` and propagated back to the client. The failure is also persisted via `record_fetch_failure` so that a subsequent GET does not repeatedly schedule a failing background task.
- **GET (cache miss)**: A background task is scheduled. If that task fails (SSL, 403, timeout), the failure is persisted. The next GET for the same URL will see the failure record and return **503** with the stored error message until a retry window (default 5 minutes) passes, then a new background attempt is scheduled.
- Tests (see `tests/test_api.py`) verify timeouts, 503 on recent failure, and 202 after the retry window.

### URL Normalization & Canonicalization

- All incoming URLs (for both POST and GET) are passed through a dedicated `normalize_url` helper.
- Canonicalization rules:
  - Trim surrounding whitespace.
  - Lowercase scheme and host; strip a trailing dot in the host.
  - Drop default ports (`http:80`, `https:443`).
  - Normalize path: ensure a leading slash, collapse duplicate slashes, resolve `.`/`..`, and preserve a trailing slash for non-root paths when present.
  - Canonicalize the query string by parsing, sorting parameters, and re-encoding.
  - Drop URL fragments (the `#...` portion).
- This ensures that logically equivalent URLs such as `https://linkedin.com`, `https://linkedin.com/`, and `https://linkedin.com//` all map to the same canonical key (e.g., `https://linkedin.com/`) and therefore the same MongoDB document and cache entry.

### Fetch failure persistence & retry window (edge cases)

- **SSL / TLS errors** (e.g. `CERTIFICATE_VERIFY_FAILED`): Treated like any other fetch failure; the error is stored and GET returns 503 until the retry window passes.
- **403 Forbidden** (e.g. target site blocks the scraper): POST returns 403 and records the failure; GET returns 503 with the error until the window passes, then schedules a new attempt.
- **Timeouts / connection errors**: Same behavior; failure is recorded and GET returns 503 with the message until retry.
- **Retry window**: Default 5 minutes (`DEFAULT_RETRY_AFTER_SECONDS` in `app/db/crud.py`). After that, the next GET for the same URL schedules a new background task (202) instead of returning 503.

### Database Connectivity & Startup

- `connect_to_mongo`:
  - Creates `AsyncIOMotorClient` with `MONGODB_URL`.
  - Tries multiple times to `ping` the MongoDB server.
  - Logs successes and failures.
  - Raises a clear error if it cannot connect after all retries.
- `setup_database_indexes`:
  - Ensures a unique index on `url` to prevent duplicates and keep lookups efficient.
- `docker-compose.yml`:
  - Defines a `healthcheck` for the MongoDB service using `mongosh db.adminCommand('ping')`.
  - Helps ensure Mongo is ready to accept traffic during startup.

### Graceful Error Responses

- **Database errors** during read/write:
  - Wrapped in `try/except` in the API layer.
  - On failure, return:
    - `503 Service Unavailable`
    - `{"detail": "Database unavailable. Please try again later."}`
- **Fetch failures (GET)**:
  - When a background fetch has recently failed (SSL, 403, timeout, etc.), GET returns:
    - `503 Service Unavailable`
    - `{"detail": "Metadata fetch failed: <error_message>. Retry later."}`
  - After the retry window (default 5 minutes), the next GET schedules a new background task and returns `202 Accepted`.
- **Fetch failures (POST)**:
  - POST returns the appropriate HTTP status from the scraper (e.g. `403`, `504`, `400`) and persists the failure so GET respects the same retry window.
- **Rate limiting (GET & POST)**:
  - Both `POST /metadata` and `GET /metadata` are protected by a very simple in-process, per-IP rate limiter.
  - If a client exceeds `RATE_LIMIT_REQUESTS_PER_MINUTE` requests within a 60-second window, the service returns:
    - `429 Too Many Requests`
    - `{"detail": "Too Many Requests. Please slow down."}`
- **Global exception handler** in `app/main.py`:
  - Catches any other unhandled exceptions.
  - Logs the error with stack trace.
  - Returns a generic `500 Internal Server Error` with a safe message.

---

## Project Structure

```text
app/
  api/
    endpoints.py        # FastAPI routes
  core/
    config.py           # Settings (pydantic-settings)
  db/
    mongodb.py          # Mongo client + connection management
    crud.py             # DB operations & index setup
  models/
    metadata.py         # Pydantic models (URLRequest, MetadataDocument)
  services/
    scraper.py          # HTTP scraping & metadata building
  utils/
    url_normalization.py  # URL canonicalization helper used by the API
  main.py               # FastAPI app, lifespan, logging, exception handler

tests/
  conftest.py           # Fixtures (test client, mocked data, etc.)
  test_api.py           # API tests

Dockerfile
docker-compose.yml
requirements.txt
.env.example
.env (local, not committed)
```

---

## Getting Started

### 1. Prerequisites

- **With Docker (recommended)**:
  - Docker
  - Docker Compose

- **Without Docker**:
  - Python 3.11
  - Local MongoDB running (default: `mongodb://localhost:27017`).

---

### 2. Configuration

Copy the example env file and adjust values as needed:

```bash
cp .env.example .env
```

`.env` controls:

- `PROJECT_NAME` – Service name (used by FastAPI).
- `MONGODB_URL` – MongoDB connection string (e.g. `mongodb://mongodb:27017` for Docker Compose, or `mongodb://localhost:27017` locally).
- `DATABASE_NAME` – Database name (e.g. `metadata_db`).

`app/core/config.py` reads these via `pydantic-settings`.

---

### 3. Running with Docker Compose

Ensure your `.env` has `MONGODB_URL=mongodb://mongodb:27017` (the Docker network hostname). Then from the project root:

```bash
docker compose up --build
```

This will:

- Build and run the API container.
- Run MongoDB with a healthcheck.
- Expose the API on `http://localhost:8000`.

To stop:

```bash
docker compose down
```

---

### 4. Running Locally (without Docker)

1. Create and activate a virtual environment (recommended).
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Ensure MongoDB is running and reachable at `MONGODB_URL` (default: `mongodb://localhost:27017`).
4. Start the API:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

---

## API Usage

Base URL (local): `http://localhost:8000`

### 1. POST `/api/v1/metadata`

Trigger metadata scraping and store/update it in the inventory.

- **Request body**:

```json
{
  "url": "https://example.com"
}
```

- **Response (201 Created)**:

```json
{
  "message": "Metadata created successfully",
  "data": {
    "url": "https://example.com/",
    "headers": { "...": "..." },
    "cookies": { "...": "..." },
    "page_source": "<!doctype html>...",
    "created_at": "2026-03-04T10:00:00Z"
  }
}
```

- **Example `curl`**:

```bash
curl -X POST "http://localhost:8000/api/v1/metadata" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

### 2. GET `/api/v1/metadata?url=<URL>`

Retrieve cached metadata or trigger background scraping if missing.

- **Cache Hit (200 OK)**:

```json
{
  "data": {
    "url": "https://example.com/",
    "headers": { "...": "..." },
    "cookies": { "...": "..." },
    "page_source": "<!doctype html>...",
    "created_at": "2026-03-04T10:00:00Z"
  }
}
```

- **Cache Miss (202 Accepted)**:

```json
{
  "message": "Metadata for https://example.com/ not found. A background task has been initiated to fetch it."
}
```

- **Example `curl`**:

```bash
curl "http://localhost:8000/api/v1/metadata?url=https://example.com"
```

### 3. GET `/api/v1/health`

Simple health check for the API.

- **Example**:

```bash
curl "http://localhost:8000/api/v1/health"
```

- **Response**:

```json
{
  "status": "ok"
}
```

---

## Testing

Tests focus on:

- Happy path for POST & GET.
- Cache hit vs cache miss behavior.
- Error handling for DB read/write failures.
- Propagation of HTTP scraper errors (e.g. timeouts).
- URL normalization behavior, ensuring that visually different but logically equivalent URLs resolve to the same canonical key.
- **Fetch failure handling**: POST records failure and re-raises (e.g. 403); GET returns 503 when a recent failure exists, and 202 when the retry window has passed.

### Running tests inside Docker

With the stack up (or using `docker compose run`):

```bash
docker compose exec api pytest
```

### Running tests locally

Assuming dependencies are installed and MongoDB is running:

```bash
pytest
```

---

## Design & Implementation Notes (for reviewers)

- **Shared `httpx.AsyncClient`**:
  - Created once at startup (`init_http_client`), closed on shutdown (`close_http_client`).
  - Used by `fetch_url_metadata` for all outbound HTTP calls.
  - Reduces connection overhead and demonstrates efficient I/O handling.

- **Background Worker Pattern**:
  - GET `/metadata` does not call the service itself or block on scraping.
  - Schedules in-process background work via `asyncio.create_task` to trigger `background_scrape_and_store`.
  - Satisfies the rubric’s constraint: internal orchestration without external self-HTTP calls.

- **Resilience**:
  - MongoDB readiness checks and retries at startup.
  - Docker Compose `healthcheck` for MongoDB.
  - Explicit 503 responses for DB failures.
  - Specific error mapping for external HTTP issues.

- **Separation of Concerns**:
  - API code does not know how HTTP scraping or DB internals work; it calls clearly named functions (`fetch_url_metadata`, `get_metadata_by_url`, `create_metadata_record`).
  - Business logic (scraping) and persistence code (CRUD) are isolated and testable.

- **Security & Ops**:
  - `.env` is not committed; `.env.example` documents required variables.
  - Dependencies are pinned in `requirements.txt` for reproducible builds.

