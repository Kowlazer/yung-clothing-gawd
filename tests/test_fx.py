"""Tests for src/fx.py.

fetch_rates is exercised against httpx_mock; convert_to_usd and get_rates are
tested as pure functions with injectable fetchers and clocks.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import httpx
import pytest

from src.fx import _FX_API_URL, convert_to_usd, fetch_rates, get_rates


_NOW = datetime(2026, 5, 17, 14, 0, 0, tzinfo=timezone.utc)


def _success_body(rates: dict | None = None) -> dict:
    return {
        "result": "success",
        "base_code": "USD",
        "time_last_update_unix": 1747000000,
        "rates": rates or {"USD": 1, "CAD": 1.37, "EUR": 0.92, "GBP": 0.79},
    }


# ---------------------------------------------------------------------------
# fetch_rates
# ---------------------------------------------------------------------------

class TestFetchRates:
    def test_success_returns_rates_dict(self, httpx_mock):
        httpx_mock.add_response(url=_FX_API_URL, json=_success_body())
        rates = fetch_rates()
        assert rates is not None
        assert rates["USD"] == 1
        assert rates["CAD"] == 1.37

    def test_http_error_returns_none(self, httpx_mock):
        httpx_mock.add_response(url=_FX_API_URL, status_code=500)
        assert fetch_rates() is None

    def test_network_error_returns_none(self, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectError("boom"))
        assert fetch_rates() is None

    def test_non_success_result_returns_none(self, httpx_mock):
        httpx_mock.add_response(url=_FX_API_URL, json={"result": "error", "rates": {}})
        assert fetch_rates() is None

    def test_missing_rates_key_returns_none(self, httpx_mock):
        httpx_mock.add_response(url=_FX_API_URL, json={"result": "success"})
        assert fetch_rates() is None

    def test_rates_missing_usd_returns_none(self, httpx_mock):
        """USD must be present as the base — defensive guard against weird shapes."""
        httpx_mock.add_response(url=_FX_API_URL, json={"result": "success", "rates": {"CAD": 1.37}})
        assert fetch_rates() is None


# ---------------------------------------------------------------------------
# convert_to_usd
# ---------------------------------------------------------------------------

class TestConvertToUsd:
    def test_usd_passes_through(self):
        assert convert_to_usd(50.0, "USD", {"USD": 1, "CAD": 1.37}) == 50.0

    def test_cad_to_usd(self):
        """$45 CAD with rate 1.37 → $32.85 USD."""
        result = convert_to_usd(45.0, "CAD", {"USD": 1, "CAD": 1.37})
        assert result is not None
        assert abs(result - 32.85) < 0.01

    def test_none_amount_returns_none(self):
        assert convert_to_usd(None, "CAD", {"USD": 1, "CAD": 1.37}) is None

    def test_missing_currency_treated_as_usd(self):
        assert convert_to_usd(50.0, None, {"USD": 1, "CAD": 1.37}) == 50.0

    def test_no_rates_returns_none_for_non_usd(self):
        """Non-USD without rates can't convert — return None so the caller falls back."""
        assert convert_to_usd(50.0, "CAD", None) is None

    def test_no_rates_still_passes_usd_through(self):
        """USD doesn't need rates at all — always returns the amount."""
        assert convert_to_usd(50.0, "USD", None) == 50.0

    def test_unknown_currency_returns_none(self):
        assert convert_to_usd(50.0, "XYZ", {"USD": 1, "CAD": 1.37}) is None


# ---------------------------------------------------------------------------
# get_rates (cache freshness orchestration)
# ---------------------------------------------------------------------------

def _fresh_cache(hours_old: float = 1.0) -> dict:
    fetched = _NOW - timedelta(hours=hours_old)
    return {
        "fetched_at": fetched.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base": "USD",
        "rates": {"USD": 1, "CAD": 1.30, "EUR": 0.90},
    }


class TestGetRates:
    def test_fresh_cache_used_without_fetch(self):
        cache = _fresh_cache(hours_old=2.0)
        called = []
        def fetcher():
            called.append(True)
            return {"USD": 1, "CAD": 999}
        rates, new_cache = get_rates(cache, fetcher=fetcher, now=_NOW)
        assert rates == cache["rates"]
        assert new_cache == cache
        assert called == [], "fetch should not have been called"

    def test_stale_cache_triggers_refresh(self):
        cache = _fresh_cache(hours_old=25.0)  # > 23h default
        def fetcher():
            return {"USD": 1, "CAD": 1.40}
        rates, new_cache = get_rates(cache, fetcher=fetcher, now=_NOW)
        assert rates == {"USD": 1, "CAD": 1.40}
        assert new_cache["rates"] == {"USD": 1, "CAD": 1.40}
        assert new_cache["fetched_at"].startswith("2026-05-17T14")

    def test_no_cache_triggers_fetch(self):
        def fetcher():
            return {"USD": 1, "CAD": 1.40}
        rates, new_cache = get_rates({}, fetcher=fetcher, now=_NOW)
        assert rates == {"USD": 1, "CAD": 1.40}
        assert new_cache["rates"] == {"USD": 1, "CAD": 1.40}

    def test_fetch_failure_falls_back_to_stale_cache(self):
        """Daily run: if FX API is down, use the day-old cache rather than no conversion."""
        cache = _fresh_cache(hours_old=25.0)
        def fetcher():
            return None
        rates, new_cache = get_rates(cache, fetcher=fetcher, now=_NOW)
        assert rates == cache["rates"]  # stale rates, still better than nothing
        assert new_cache == cache  # cache unchanged

    def test_fetch_failure_no_cache_returns_none(self):
        def fetcher():
            return None
        rates, new_cache = get_rates({}, fetcher=fetcher, now=_NOW)
        assert rates is None
        assert new_cache == {}

    def test_malformed_cache_timestamp_treated_as_stale(self):
        cache = {"fetched_at": "garbage", "rates": {"USD": 1, "CAD": 1.30}}
        def fetcher():
            return {"USD": 1, "CAD": 1.40}
        rates, new_cache = get_rates(cache, fetcher=fetcher, now=_NOW)
        assert rates == {"USD": 1, "CAD": 1.40}  # forced refresh

    def test_cache_missing_fetched_at_treated_as_stale(self):
        cache = {"rates": {"USD": 1, "CAD": 1.30}}
        def fetcher():
            return {"USD": 1, "CAD": 1.40}
        rates, _ = get_rates(cache, fetcher=fetcher, now=_NOW)
        assert rates == {"USD": 1, "CAD": 1.40}

    def test_freshness_boundary_at_max_age(self):
        """Exactly at the max_age boundary, treat as stale (refetch)."""
        cache = _fresh_cache(hours_old=23.0)
        def fetcher():
            return {"USD": 1, "CAD": 1.40}
        rates, _ = get_rates(cache, fetcher=fetcher, now=_NOW)
        assert rates == {"USD": 1, "CAD": 1.40}
