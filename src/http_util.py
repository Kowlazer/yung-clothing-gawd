"""Shared HTTP throttling + retry helpers.

A thread-safe ``RateLimiter`` (minimum inter-request gap across all threads) and
a ``Retry-After``-aware retry wrapper, used by both the product-price extractor
(``src/extract.py``) and the homepage sale-check fetcher (``src/claude_fuzzy.py``)
so neither hammers shared platform infrastructure — Shopify rate-limits by source
IP across every store it hosts, so concurrent / bursty requests from one runner
trip a platform-level 429 even though each store is a different domain.
"""

from __future__ import annotations

import email.utils
import logging
import threading
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

# Retry policy for rate-limit / transient-unavailable responses. We honor the
# server's Retry-After header when present (adaptive — no guessing at the
# threshold), falling back to exponential backoff otherwise.
_RETRY_STATUSES = frozenset({429, 503})
DEFAULT_MAX_RETRIES = 3   # extra attempts after the first GET (up to 4 total)
_BACKOFF_BASE = 1.0       # seconds; doubles each attempt: 1, 2, 4 ...
MAX_BACKOFF = 20.0        # cap on any single wait (also caps a huge Retry-After)


class RateLimiter:
    """Thread-safe minimum inter-request delay across all threads.

    Ensures at least ``interval`` seconds between successive acquisitions so
    concurrent (or rapid sequential) callers don't hammer shared platform
    infrastructure. The lock is held across the sleep on purpose: that
    serialises acquirers, giving a strict ``interval`` gap between request
    *starts* rather than a thundering-herd wake-up.
    """

    def __init__(self, interval: float) -> None:
        self._lock = threading.Lock()
        self._last = 0.0
        self._interval = interval

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._interval - (now - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Translate a ``Retry-After`` header into seconds to wait.

    Accepts both RFC 7231 forms — delta-seconds (a bare integer) or an
    HTTP-date — and returns the seconds to wait (never negative), or ``None``
    when the header is absent or unparseable (caller then uses backoff).
    """
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (dt - now).total_seconds())


def get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    sleep=time.sleep,
    **get_kwargs,
) -> httpx.Response:
    """GET ``url``, retrying on 429/503 while honoring ``Retry-After``.

    On a retry status, waits the server-instructed ``Retry-After`` (or an
    exponential backoff when the header is absent/invalid, capped at
    ``MAX_BACKOFF``), up to ``max_retries`` extra attempts, then returns the
    final response — which the caller still classifies, so a persistent 429
    becomes ``rate_limited``. Network-level exceptions propagate unchanged (the
    caller owns the try/except), so this only adapts to a server that
    explicitly told us to slow down. ``get_kwargs`` are forwarded to every
    ``client.get`` (e.g. per-call ``headers`` / ``timeout`` / ``follow_redirects``).
    """
    resp = client.get(url, **get_kwargs)
    for attempt in range(max_retries):
        if resp.status_code not in _RETRY_STATUSES:
            return resp
        wait = parse_retry_after(resp.headers.get("Retry-After"))
        if wait is None:
            wait = _BACKOFF_BASE * (2 ** attempt)
        wait = min(wait, MAX_BACKOFF)
        log.info("http_util: %s -> %s, backing off %.1fs (retry %d/%d)",
                 url, resp.status_code, wait, attempt + 1, max_retries)
        sleep(wait)
        resp = client.get(url, **get_kwargs)
    return resp
