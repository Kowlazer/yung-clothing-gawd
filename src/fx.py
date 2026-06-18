"""Fetch and cache USD-base FX rates for currency conversion at digest time.

Source: open.er-api.com — free, keyless, ~160 currencies. Rates are quoted as
"1 USD = X target currency", so converting FROM a non-USD currency TO USD is
amount / rates[currency].

Cache schema (stored as fx_rates.json in the Gist):
    {
        "fetched_at": "2026-05-17T14:00:00Z",
        "base": "USD",
        "rates": {"USD": 1.0, "CAD": 1.37, "EUR": 0.92, ...}
    }

Failure policy: if the FX API is unreachable AND we have a stale cache, fall
back to stale rates rather than failing the run (a slightly drifted CAD->USD
conversion is far better than blocking the digest). If there's no cache at
all, conversion silently no-ops and prices render in their native currency.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

import httpx

log = logging.getLogger(__name__)

_FX_API_URL = "https://open.er-api.com/v6/latest/USD"
_TIMEOUT = 10.0
_DEFAULT_MAX_AGE_HOURS = 23.0


def fetch_rates(url: str = _FX_API_URL) -> dict | None:
    """GET the FX API and return the rates dict {currency: rate_from_usd}.

    Returns None on any error (network, HTTP, parse, missing 'rates' key, or
    a non-success 'result' field). Callers handle the None case.
    """
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(url)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("fx: fetch failed: %s", e)
        return None

    if body.get("result") != "success":
        log.warning("fx: API returned non-success result: %s", body.get("result"))
        return None
    rates = body.get("rates")
    if not isinstance(rates, dict) or "USD" not in rates:
        log.warning("fx: response missing valid 'rates' dict")
        return None
    return rates


def convert_to_usd(amount: float | None, currency: str | None, rates: dict | None) -> float | None:
    """Convert `amount` in `currency` to USD using `rates`. Returns None if
    conversion isn't possible (missing rate, unknown currency, no amount).
    USD amounts pass through unchanged."""
    if amount is None:
        return None
    if not currency or currency == "USD":
        return float(amount)
    if not rates:
        return None
    rate = rates.get(currency)
    if not rate:
        return None
    return float(amount) / float(rate)


def _is_fresh(cache: dict, now: datetime, max_age_hours: float) -> bool:
    fetched_at = cache.get("fetched_at")
    if not fetched_at:
        return False
    try:
        ts = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    age_hours = (now - ts).total_seconds() / 3600.0
    return age_hours < max_age_hours


def get_rates(
    cache: dict,
    fetcher: Callable[[], dict | None] = fetch_rates,
    now: datetime | None = None,
    max_age_hours: float = _DEFAULT_MAX_AGE_HOURS,
) -> tuple[dict | None, dict]:
    """Resolve which rates to use this run and what to persist to the Gist.

    Returns (rates_for_use, cache_to_persist):
        rates_for_use   — rates dict to apply right now, or None if nothing
                          is available (no cache and the fetch failed)
        cache_to_persist — the cache dict to write back: a fresh cache if we
                          successfully fetched, otherwise the unchanged input
                          cache (which may be empty)

    Logic:
        - cache exists and is fresh → return cached rates, don't refetch
        - else attempt a fetch
            - on success → return new rates and a new cache entry with now()
            - on failure with stale cache → return stale rates and keep cache
            - on failure with no cache → return (None, {})
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if cache and _is_fresh(cache, now, max_age_hours):
        return cache.get("rates"), cache

    fresh = fetcher()
    if fresh is not None:
        new_cache = {
            "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "base": "USD",
            "rates": fresh,
        }
        return fresh, new_cache

    if cache and cache.get("rates"):
        log.warning("fx: fetch failed, falling back to stale cache from %s",
                    cache.get("fetched_at"))
        return cache.get("rates"), cache

    log.warning("fx: fetch failed and no cache available; conversion disabled")
    return None, cache or {}
