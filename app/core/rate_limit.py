from time import time
from typing import Dict, Tuple

import asyncio
from fastapi import Request, HTTPException, status

from app.core.config import settings


_lock = asyncio.Lock()
_ip_windows: Dict[str, Tuple[float, int]] = {}


async def rate_limiter(request: Request) -> None:
    """
    Very simple fixed-window rate limiter.

    Limits each client IP to RATE_LIMIT_REQUESTS_PER_MINUTE requests per 60-second
    window. Exceeds return HTTP 429.
    """
    client = request.client
    ip = client.host if client and client.host else "unknown"

    now = time()
    window_seconds = 60.0
    max_requests = settings.RATE_LIMIT_REQUESTS_PER_MINUTE

    async with _lock:
        window_start, count = _ip_windows.get(ip, (now, 0))

        # Start a new window if the current one has expired
        if now - window_start >= window_seconds:
            window_start, count = now, 0

        if count >= max_requests:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too Many Requests. Please slow down.",
            )

        _ip_windows[ip] = (window_start, count + 1)

