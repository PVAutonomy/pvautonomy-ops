"""Generic async retry helper + OTA failure classification (EPIC-006-A5).

Provides ``retry_async()`` for wrapping flaky async operations with
configurable backoff, and ``is_hard_ota_failure()`` to distinguish
transient (retryable) from permanent OTA errors.

Ref: WORK-ITEM-EPIC006-A5-OTA-ROBUSTNESS-CACHE-CLEANUP_UPDATED.md
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

# Substrings in OTAError messages that indicate a hard (non-retryable) failure.
_HARD_OTA_SUBSTRINGS = (
    "authentication",
    "auth",
    "Unsupported OTA version",
    "MD5 auth",
    "Firmware file is empty",
    "requires OTA password",
)


def is_hard_ota_failure(exc: Exception) -> bool:
    """Return True if the exception represents a non-retryable OTA error.

    Hard failures: auth errors, version mismatch, empty firmware.
    Soft failures (retryable): timeout, connection reset, network error.
    """
    msg = str(exc).lower()
    for substr in _HARD_OTA_SUBSTRINGS:
        if substr.lower() in msg:
            return True
    return False


async def retry_async(
    fn: Callable[..., Awaitable[T]],
    *args: object,
    retries: int = 3,
    delays: tuple[float, ...] = (0, 10, 30),
    retry_on: tuple[type[Exception], ...] = (Exception,),
    no_retry_on: tuple[type[Exception], ...] = (),
    on_retry: Callable[[int, Exception], Awaitable[None]] | None = None,
    **kwargs: object,
) -> T:
    """Call *fn* with retry and exponential-ish backoff.

    Args:
        fn: Async callable to invoke.
        retries: Maximum number of attempts (including the first).
        delays: Per-attempt delay in seconds before each retry.
            Index 0 = delay before attempt 2, etc.  Padded with the
            last value if fewer entries than retries-1.
        retry_on: Exception types that trigger a retry.
        no_retry_on: Exception types that must NOT be retried (takes
            precedence over *retry_on*).
        on_retry: Optional async callback ``(attempt, exc)`` called
            before each retry sleep.

    Returns:
        The result of *fn* on success.

    Raises:
        The last exception if all attempts are exhausted, or a
        ``no_retry_on`` exception immediately.
    """
    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc

            # Check no-retry list first (takes precedence)
            if no_retry_on and isinstance(exc, no_retry_on):
                _LOGGER.debug(
                    "Attempt %d/%d failed (no-retry): %s", attempt, retries, exc
                )
                raise

            # Check if retryable
            if not isinstance(exc, retry_on):
                _LOGGER.debug(
                    "Attempt %d/%d failed (not retryable): %s",
                    attempt, retries, exc,
                )
                raise

            # Last attempt — don't retry
            if attempt >= retries:
                _LOGGER.warning(
                    "All %d attempts exhausted. Last error: %s", retries, exc
                )
                raise

            # Compute delay
            delay_idx = attempt - 1  # 0-based: delay before attempt 2, 3, …
            if delay_idx < len(delays):
                delay = delays[delay_idx]
            else:
                delay = delays[-1] if delays else 0

            _LOGGER.info(
                "Attempt %d/%d failed: %s — retrying in %.0fs",
                attempt, retries, exc, delay,
            )

            if on_retry:
                await on_retry(attempt, exc)

            if delay > 0:
                await asyncio.sleep(delay)

    # Should not reach here, but just in case
    if last_exc:
        raise last_exc
    raise RuntimeError("retry_async: no attempts executed")
