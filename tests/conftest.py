"""Shared pytest fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _fast_rate_limiters():
    """Zero the production RateLimiter intervals so tests never really sleep.

    The product extractor and the homepage fetcher each hold a module-level
    ``RateLimiter`` singleton (5 s / 2 s gaps in prod). Tests that exercise the
    real fetch paths through ``httpx_mock`` would otherwise block on those
    sleeps. Zeroing the interval makes ``acquire()`` a no-op. A dedicated
    RateLimiter unit test constructs its own instance with a real interval, so
    its timing behaviour is still covered.
    """
    from src import claude_fuzzy, main
    main._SHOPIFY_LIMITER._interval = 0.0
    claude_fuzzy._HOMEPAGE_LIMITER._interval = 0.0
    yield
