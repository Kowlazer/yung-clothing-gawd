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


@pytest.fixture(autouse=True)
def _reset_circuit_breaker():
    """Clear the process-global host circuit breaker between tests.

    ``http_util._BREAKER`` accumulates per-host throttle counts across a run;
    without a reset, a test that trips it (two persistent 429s to one host)
    would silently short-circuit a later test's fetch to that same host.
    """
    from src import http_util
    http_util._BREAKER.reset()
    yield


@pytest.fixture(autouse=True)
def _no_browser_fallback(monkeypatch):
    """Keep the browser-render fallback out of every test by default.

    ``extract()``'s blocked-recovery ladder now ends in a real headless
    Chromium launch (``src/browser_fetch.py``). Any test that forces a
    blocked response without stubbing that rung would otherwise launch an
    actual browser against a fake URL — slow, flaky, and wrong. Tests that
    exercise the rung re-enable it and monkeypatch
    ``browser_fetch.fetch_rendered_html`` explicitly.
    """
    from src import extract
    monkeypatch.setattr(extract, "_BROWSER_FALLBACK_ENABLED", False)
