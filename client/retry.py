"""Shared retry-with-backoff helper for HTTP clients."""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

RETRY_DELAYS = [10, 30, 60]
FINAL_WAIT = 180

RETRYABLE_ERRORS = (httpx.HTTPStatusError, httpx.ReadTimeout, httpx.ConnectError, RuntimeError)


async def retry_with_backoff(
    fn: Callable[[], Coroutine[None, None, T]],
    *,
    max_retries: int = 4,
    label: str = "request",
) -> T:
    """Retry an async callable with exponential backoff + 180s final wait.

    Retries on httpx HTTP/timeout/connect errors.
    Schedule: attempt 1, wait 10s, attempt 2, wait 30s, attempt 3, wait 60s, attempt 4,
              then wait 180s, final attempt.
    """
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return await fn()
        except RETRYABLE_ERRORS as e:
            last_error = e
            if attempt >= max_retries:
                break
            delay = RETRY_DELAYS[attempt - 1] if attempt - 1 < len(RETRY_DELAYS) else RETRY_DELAYS[-1]
            logger.error("%s failed (attempt %d/%d, wait %ds): %s", label, attempt, max_retries, delay, e)
            await asyncio.sleep(delay)

    # All attempts exhausted — final long wait
    logger.warning("%s failed %d times, waiting %ds for final attempt: %s", label, max_retries, FINAL_WAIT, last_error)
    await asyncio.sleep(FINAL_WAIT)
    try:
        return await fn()
    except RETRYABLE_ERRORS:
        raise last_error  # type: ignore[misc]
