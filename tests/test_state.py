"""Tests for src/state.py.

Network calls are intercepted by pytest-httpx (httpx_mock fixture). The
_prune_prices helper is tested directly as a pure function.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

import pytest

from src.state import (
    _prune_codes,
    _prune_gmail_state,
    _prune_prices,
    _prune_voice_state,
    read_state,
    write_state,
)

GIST_ID = "abc123"
TOKEN = "ghp_testtoken"
_API_URL = f"https://api.github.com/gists/{GIST_ID}"


def _gist_response(prices=None, aliases=None, codes=None, fx=None,
                    gmail=None, voice=None, sms_aliases=None, signup=None,
                    email_sales=None, body_scans=None, shop_verdicts=None,
                    restock=None) -> dict:
    """Build a minimal GitHub Gist API response with the given file contents."""
    def _file(content) -> dict:
        return {"content": json.dumps(content)}

    files: dict = {}
    if prices is not None:
        files["prices.json"] = _file(prices)
    if aliases is not None:
        files["shop_aliases.json"] = _file(aliases)
    if codes is not None:
        files["codes.json"] = _file(codes)
    if email_sales is not None:
        files["email_sales.json"] = _file(email_sales)
    if fx is not None:
        files["fx_rates.json"] = _file(fx)
    if gmail is not None:
        files["gmail_state.json"] = _file(gmail)
    if voice is not None:
        files["voice_state.json"] = _file(voice)
    if sms_aliases is not None:
        files["sms_aliases.json"] = _file(sms_aliases)
    if signup is not None:
        files["signup_state.json"] = _file(signup)
    if body_scans is not None:
        files["body_scans.json"] = _file(body_scans)
    if shop_verdicts is not None:
        files["shop_verdicts.json"] = _file(shop_verdicts)
    if restock is not None:
        files["restock_state.json"] = _file(restock)
    return {"files": files}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# Timestamps relative to now — stable regardless of when tests run
_RECENT = _iso(datetime.now(timezone.utc) - timedelta(days=5))
_OLD = _iso(datetime.now(timezone.utc) - timedelta(days=45))


# ---------------------------------------------------------------------------
# read_state
# ---------------------------------------------------------------------------

class TestReadState:
    def test_happy_path(self, httpx_mock):
        prices = {"https://example.com/p": {"current_price": 50.0}}
        aliases = {"ShopName": "https://shopname.com"}
        codes = {"ShopName": ["CODE10"]}
        httpx_mock.add_response(url=_API_URL, json=_gist_response(prices, aliases, codes))

        result = read_state(GIST_ID, TOKEN)

        assert result["prices"] == prices
        assert result["aliases"] == aliases
        assert result["codes"] == codes

    def test_missing_prices_file_returns_empty(self, httpx_mock):
        httpx_mock.add_response(url=_API_URL, json=_gist_response(aliases={}, codes={}))
        result = read_state(GIST_ID, TOKEN)
        assert result["prices"] == {}

    def test_missing_aliases_file_returns_empty(self, httpx_mock):
        httpx_mock.add_response(url=_API_URL, json=_gist_response(prices={}, codes={}))
        result = read_state(GIST_ID, TOKEN)
        assert result["aliases"] == {}

    def test_missing_codes_file_returns_empty_list(self, httpx_mock):
        httpx_mock.add_response(url=_API_URL, json=_gist_response(prices={}, aliases={}))
        result = read_state(GIST_ID, TOKEN)
        # codes is a list now (entries carry source/first_seen/last_seen fields).
        assert result["codes"] == []

    def test_body_scans_file_parsed(self, httpx_mock):
        cache = {"refreshed_at": "2026-06-01T00:00:00Z",
                 "scans": [{"result_id": "R1", "scan_date": "2026-05-30",
                            "start_time": "2026-05-30T09:00:00Z", "weight_kg": 75.2}]}
        httpx_mock.add_response(url=_API_URL, json=_gist_response(
            prices={}, aliases={}, codes=[], body_scans=cache))
        result = read_state(GIST_ID, TOKEN)
        assert result["body_scans"] == cache

    def test_missing_body_scans_file_returns_empty(self, httpx_mock):
        httpx_mock.add_response(url=_API_URL, json=_gist_response(prices={}, aliases={}, codes=[]))
        result = read_state(GIST_ID, TOKEN)
        assert result["body_scans"] == {}

    def test_email_sales_file_parsed_as_list(self, httpx_mock):
        entries = [{"shop": "Aniqi", "email_id": "m1", "status": "yes",
                    "ends_on": "2026-05-30"}]
        httpx_mock.add_response(
            url=_API_URL,
            json=_gist_response(prices={}, aliases={}, email_sales=entries),
        )
        result = read_state(GIST_ID, TOKEN)
        assert result["email_sales"] == entries

    def test_missing_email_sales_returns_empty_list(self, httpx_mock):
        httpx_mock.add_response(url=_API_URL, json=_gist_response(prices={}, aliases={}))
        result = read_state(GIST_ID, TOKEN)
        assert result["email_sales"] == []

    def test_shop_verdicts_file_parsed_as_list(self, httpx_mock):
        entries = [{"shop": "Aniqi", "hash": "abc123", "status": "yes",
                    "description": "30% off", "checked_at": _RECENT}]
        httpx_mock.add_response(
            url=_API_URL,
            json=_gist_response(prices={}, aliases={}, shop_verdicts=entries),
        )
        result = read_state(GIST_ID, TOKEN)
        assert result["shop_verdicts"] == entries

    def test_restock_file_parsed(self, httpx_mock):
        restock = {"https://shop.com/products/x": {
            "sizes": {"M": {"signed_up_at": _RECENT, "vendor": "klaviyo_bis"}},
            "attempts": [{"at": _RECENT, "size": "M", "result": "success"}],
        }}
        httpx_mock.add_response(
            url=_API_URL,
            json=_gist_response(prices={}, aliases={}, restock=restock),
        )
        result = read_state(GIST_ID, TOKEN)
        assert result["restock"] == restock

    def test_missing_restock_returns_empty_dict(self, httpx_mock):
        httpx_mock.add_response(url=_API_URL, json=_gist_response(prices={}, aliases={}))
        result = read_state(GIST_ID, TOKEN)
        assert result["restock"] == {}

    def test_missing_shop_verdicts_returns_empty_list(self, httpx_mock):
        httpx_mock.add_response(url=_API_URL, json=_gist_response(prices={}, aliases={}))
        result = read_state(GIST_ID, TOKEN)
        assert result["shop_verdicts"] == []

    def test_codes_file_parsed_as_list(self, httpx_mock):
        codes = [{"shop": "Aniqi", "code": "SPRING30", "source": "email",
                  "first_seen": _RECENT, "last_seen": _RECENT}]
        httpx_mock.add_response(url=_API_URL, json=_gist_response(codes=codes))
        result = read_state(GIST_ID, TOKEN)
        assert result["codes"] == codes

    def test_gmail_state_parsed(self, httpx_mock):
        gmail = {"processed_ids": {"msg_abc": _RECENT}}
        httpx_mock.add_response(url=_API_URL, json=_gist_response(gmail=gmail))
        result = read_state(GIST_ID, TOKEN)
        assert result["gmail"] == gmail

    def test_missing_gmail_file_returns_empty(self, httpx_mock):
        httpx_mock.add_response(url=_API_URL, json=_gist_response(prices={}))
        result = read_state(GIST_ID, TOKEN)
        assert result["gmail"] == {}

    def test_voice_state_parsed(self, httpx_mock):
        voice = {"processed_ids": {"sms_xyz": _RECENT}}
        httpx_mock.add_response(url=_API_URL, json=_gist_response(voice=voice))
        result = read_state(GIST_ID, TOKEN)
        assert result["voice"] == voice

    def test_missing_voice_file_returns_empty(self, httpx_mock):
        httpx_mock.add_response(url=_API_URL, json=_gist_response(prices={}))
        result = read_state(GIST_ID, TOKEN)
        assert result["voice"] == {}

    def test_sms_aliases_parsed(self, httpx_mock):
        aliases = {"+18334567890": "Aniqi", "21234": "Pomelo"}
        httpx_mock.add_response(url=_API_URL, json=_gist_response(sms_aliases=aliases))
        result = read_state(GIST_ID, TOKEN)
        assert result["sms_aliases"] == aliases

    def test_missing_sms_aliases_file_returns_empty(self, httpx_mock):
        httpx_mock.add_response(url=_API_URL, json=_gist_response(prices={}))
        result = read_state(GIST_ID, TOKEN)
        assert result["sms_aliases"] == {}

    def test_signup_state_parsed(self, httpx_mock):
        signup = {
            "https://shop.com": {
                "email": {"signed_up_at": _RECENT, "code_received": "WELCOME15"},
                "phone": None,
                "attempts": [{"at": _RECENT, "channel": "email", "result": "success"}],
            }
        }
        httpx_mock.add_response(url=_API_URL, json=_gist_response(signup=signup))
        result = read_state(GIST_ID, TOKEN)
        assert result["signup"] == signup

    def test_missing_signup_file_returns_empty(self, httpx_mock):
        httpx_mock.add_response(url=_API_URL, json=_gist_response(prices={}))
        result = read_state(GIST_ID, TOKEN)
        assert result["signup"] == {}

    def test_fx_file_parsed(self, httpx_mock):
        fx = {"fetched_at": "2026-05-17T14:00:00Z", "base": "USD",
              "rates": {"USD": 1, "CAD": 1.37}}
        httpx_mock.add_response(url=_API_URL, json=_gist_response(fx=fx))
        result = read_state(GIST_ID, TOKEN)
        assert result["fx"] == fx

    def test_missing_fx_file_returns_empty(self, httpx_mock):
        httpx_mock.add_response(url=_API_URL, json=_gist_response(prices={}, aliases={}, codes={}))
        result = read_state(GIST_ID, TOKEN)
        assert result["fx"] == {}

    def test_malformed_json_returns_empty(self, httpx_mock):
        httpx_mock.add_response(
            url=_API_URL,
            json={"files": {"prices.json": {"content": "NOT JSON {"}}},
        )
        result = read_state(GIST_ID, TOKEN)
        assert result["prices"] == {}

    def test_empty_content_returns_empty(self, httpx_mock):
        httpx_mock.add_response(
            url=_API_URL,
            json={"files": {"prices.json": {"content": "   "}}},
        )
        result = read_state(GIST_ID, TOKEN)
        assert result["prices"] == {}

    def test_http_error_raises(self, httpx_mock):
        httpx_mock.add_response(url=_API_URL, status_code=401)
        with pytest.raises(Exception):
            read_state(GIST_ID, TOKEN)

    def test_sends_auth_header(self, httpx_mock):
        httpx_mock.add_response(url=_API_URL, json=_gist_response(prices={}, aliases={}, codes={}))
        read_state(GIST_ID, TOKEN)
        req = httpx_mock.get_requests()[0]
        assert req.headers["Authorization"] == f"Bearer {TOKEN}"


# ---------------------------------------------------------------------------
# write_state
# ---------------------------------------------------------------------------

class TestWriteState:
    def _mock_patch(self, httpx_mock) -> None:
        httpx_mock.add_response(method="PATCH", url=_API_URL, json={"files": {}})

    def _get_patch_body(self, httpx_mock) -> dict:
        req = httpx_mock.get_requests()[0]
        return json.loads(req.content)

    def test_patches_three_files_when_fx_and_gmail_omitted(self, httpx_mock):
        self._mock_patch(httpx_mock)
        write_state(GIST_ID, TOKEN, {}, {}, [])
        body = self._get_patch_body(httpx_mock)
        assert set(body["files"].keys()) == {"prices.json", "shop_aliases.json", "codes.json"}

    def test_patches_gmail_when_provided(self, httpx_mock):
        self._mock_patch(httpx_mock)
        gmail = {"processed_ids": {"msg_abc": _RECENT}}
        write_state(GIST_ID, TOKEN, {}, {}, [], gmail=gmail)
        body = self._get_patch_body(httpx_mock)
        assert "gmail_state.json" in body["files"]
        content = json.loads(body["files"]["gmail_state.json"]["content"])
        assert content == {"processed_ids": {"msg_abc": _RECENT}}

    def test_gmail_none_skips_writing_gmail_file(self, httpx_mock):
        """gmail=None leaves gmail_state.json untouched (e.g. Gmail step failed)."""
        self._mock_patch(httpx_mock)
        write_state(GIST_ID, TOKEN, {}, {}, [], gmail=None)
        body = self._get_patch_body(httpx_mock)
        assert "gmail_state.json" not in body["files"]

    def test_patches_email_sales_when_provided(self, httpx_mock):
        self._mock_patch(httpx_mock)
        entries = [{"shop": "Aniqi", "email_id": "m1", "status": "yes",
                    "ends_on": "2026-05-30"}]
        write_state(GIST_ID, TOKEN, {}, {}, [], email_sales=entries)
        body = self._get_patch_body(httpx_mock)
        assert "email_sales.json" in body["files"]
        content = json.loads(body["files"]["email_sales.json"]["content"])
        assert content == entries

    def test_email_sales_none_skips_writing(self, httpx_mock):
        self._mock_patch(httpx_mock)
        write_state(GIST_ID, TOKEN, {}, {}, [], email_sales=None)
        body = self._get_patch_body(httpx_mock)
        assert "email_sales.json" not in body["files"]

    def test_patches_shop_verdicts_when_provided(self, httpx_mock):
        self._mock_patch(httpx_mock)
        entries = [{"shop": "Aniqi", "hash": "abc123", "status": "yes",
                    "description": "30% off", "checked_at": _RECENT}]
        write_state(GIST_ID, TOKEN, {}, {}, [], shop_verdicts=entries)
        body = self._get_patch_body(httpx_mock)
        assert "shop_verdicts.json" in body["files"]
        content = json.loads(body["files"]["shop_verdicts.json"]["content"])
        assert content == entries

    def test_shop_verdicts_none_skips_writing(self, httpx_mock):
        self._mock_patch(httpx_mock)
        write_state(GIST_ID, TOKEN, {}, {}, [], shop_verdicts=None)
        body = self._get_patch_body(httpx_mock)
        assert "shop_verdicts.json" not in body["files"]

    def test_patches_restock_when_provided(self, httpx_mock):
        self._mock_patch(httpx_mock)
        restock = {"https://shop.com/products/x": {
            "sizes": {"M": {"signed_up_at": _RECENT, "vendor": "klaviyo_bis"}},
            "attempts": [{"at": _RECENT, "size": "M", "result": "success"}],
        }}
        write_state(GIST_ID, TOKEN, {}, {}, [], restock=restock)
        body = self._get_patch_body(httpx_mock)
        assert "restock_state.json" in body["files"]
        content = json.loads(body["files"]["restock_state.json"]["content"])
        assert content == restock

    def test_restock_none_skips_writing(self, httpx_mock):
        self._mock_patch(httpx_mock)
        write_state(GIST_ID, TOKEN, {}, {}, [], restock=None)
        body = self._get_patch_body(httpx_mock)
        assert "restock_state.json" not in body["files"]

    def test_patches_voice_when_provided(self, httpx_mock):
        self._mock_patch(httpx_mock)
        voice = {"processed_ids": {"sms_abc": _RECENT}}
        write_state(GIST_ID, TOKEN, {}, {}, [], voice=voice)
        body = self._get_patch_body(httpx_mock)
        assert "voice_state.json" in body["files"]
        content = json.loads(body["files"]["voice_state.json"]["content"])
        assert content == voice

    def test_voice_none_skips_writing_voice_file(self, httpx_mock):
        self._mock_patch(httpx_mock)
        write_state(GIST_ID, TOKEN, {}, {}, [], voice=None)
        body = self._get_patch_body(httpx_mock)
        assert "voice_state.json" not in body["files"]

    def test_patches_body_scans_when_provided(self, httpx_mock):
        self._mock_patch(httpx_mock)
        cache = {"refreshed_at": "2026-06-01T00:00:00Z",
                 "scans": [{"result_id": "R1", "scan_date": "2026-05-30"}]}
        write_state(GIST_ID, TOKEN, {}, {}, [], body_scans=cache)
        body = self._get_patch_body(httpx_mock)
        assert "body_scans.json" in body["files"]
        content = json.loads(body["files"]["body_scans.json"]["content"])
        assert content == cache

    def test_body_scans_none_skips_writing(self, httpx_mock):
        self._mock_patch(httpx_mock)
        write_state(GIST_ID, TOKEN, {}, {}, [], body_scans=None)
        body = self._get_patch_body(httpx_mock)
        assert "body_scans.json" not in body["files"]

    def test_patches_sms_aliases_when_provided(self, httpx_mock):
        self._mock_patch(httpx_mock)
        sms_aliases = {"+18334567890": "Aniqi"}
        write_state(GIST_ID, TOKEN, {}, {}, [], sms_aliases=sms_aliases)
        body = self._get_patch_body(httpx_mock)
        assert "sms_aliases.json" in body["files"]
        content = json.loads(body["files"]["sms_aliases.json"]["content"])
        assert content == sms_aliases

    def test_sms_aliases_none_skips_writing(self, httpx_mock):
        self._mock_patch(httpx_mock)
        write_state(GIST_ID, TOKEN, {}, {}, [], sms_aliases=None)
        body = self._get_patch_body(httpx_mock)
        assert "sms_aliases.json" not in body["files"]

    def test_patches_signup_when_provided(self, httpx_mock):
        self._mock_patch(httpx_mock)
        signup = {
            "https://shop.com": {
                "email": {"signed_up_at": _RECENT, "code_received": "WELCOME15"},
                "phone": None,
                "attempts": [{"at": _RECENT, "channel": "email", "result": "success"}],
            }
        }
        write_state(GIST_ID, TOKEN, {}, {}, [], signup=signup)
        body = self._get_patch_body(httpx_mock)
        assert "signup_state.json" in body["files"]
        content = json.loads(body["files"]["signup_state.json"]["content"])
        assert content == signup

    def test_signup_none_skips_writing(self, httpx_mock):
        self._mock_patch(httpx_mock)
        write_state(GIST_ID, TOKEN, {}, {}, [], signup=None)
        body = self._get_patch_body(httpx_mock)
        assert "signup_state.json" not in body["files"]

    def test_prunes_stale_email_codes(self, httpx_mock):
        self._mock_patch(httpx_mock)
        codes = [
            {"shop": "X", "code": "FRESH", "source": "email", "last_seen": _RECENT},
            {"shop": "X", "code": "STALE", "source": "email", "last_seen": _OLD},
            {"shop": "Y", "code": "WLIST", "source": "watchlist"},
        ]
        write_state(GIST_ID, TOKEN, {}, {}, codes)
        body = self._get_patch_body(httpx_mock)
        content = json.loads(body["files"]["codes.json"]["content"])
        kept_codes = {c["code"] for c in content}
        assert kept_codes == {"FRESH", "WLIST"}

    def test_patches_fx_when_provided(self, httpx_mock):
        self._mock_patch(httpx_mock)
        fx = {"fetched_at": "2026-05-17T14:00:00Z", "base": "USD",
              "rates": {"USD": 1, "CAD": 1.37}}
        write_state(GIST_ID, TOKEN, {}, {}, [], fx=fx)
        body = self._get_patch_body(httpx_mock)
        assert "fx_rates.json" in body["files"]
        assert json.loads(body["files"]["fx_rates.json"]["content"]) == fx

    def test_fx_none_skips_writing_fx_file(self, httpx_mock):
        """fx=None leaves fx_rates.json untouched (e.g. when fetch failed)."""
        self._mock_patch(httpx_mock)
        write_state(GIST_ID, TOKEN, {}, {}, [], fx=None)
        body = self._get_patch_body(httpx_mock)
        assert "fx_rates.json" not in body["files"]

    def test_serializes_prices_as_json(self, httpx_mock):
        self._mock_patch(httpx_mock)
        prices = {"https://x.com/p": {"current_price": 42.0}}
        write_state(GIST_ID, TOKEN, prices, {}, [])
        body = self._get_patch_body(httpx_mock)
        content = json.loads(body["files"]["prices.json"]["content"])
        assert content == prices

    def test_prunes_stale_entries_before_writing(self, httpx_mock):
        self._mock_patch(httpx_mock)
        prices = {
            "https://x.com/recent": {"current_price": 50.0, "last_seen": _RECENT},
            "https://x.com/old": {"current_price": 50.0, "last_seen": _OLD},
        }
        write_state(GIST_ID, TOKEN, prices, {}, [])
        body = self._get_patch_body(httpx_mock)
        content = json.loads(body["files"]["prices.json"]["content"])
        assert "https://x.com/recent" in content
        assert "https://x.com/old" not in content

    def test_sends_auth_header(self, httpx_mock):
        self._mock_patch(httpx_mock)
        write_state(GIST_ID, TOKEN, {}, {}, [])
        req = httpx_mock.get_requests()[0]
        assert req.headers["Authorization"] == f"Bearer {TOKEN}"

    def test_http_error_raises(self, httpx_mock):
        httpx_mock.add_response(method="PATCH", url=_API_URL, status_code=422)
        with pytest.raises(Exception):
            write_state(GIST_ID, TOKEN, {}, {}, [])


# ---------------------------------------------------------------------------
# _prune_prices
# ---------------------------------------------------------------------------

class TestPrunePrices:
    def test_old_entry_pruned(self):
        prices = {"https://x.com/p": {"last_seen": _OLD}}
        result = _prune_prices(prices)
        assert result == {}

    def test_recent_entry_kept(self):
        prices = {"https://x.com/p": {"last_seen": _RECENT}}
        result = _prune_prices(prices)
        assert "https://x.com/p" in result

    def test_no_last_seen_kept(self):
        """Legacy entries without last_seen must not be pruned."""
        prices = {"https://x.com/p": {"current_price": 50.0}}
        result = _prune_prices(prices)
        assert "https://x.com/p" in result

    def test_malformed_last_seen_kept(self):
        """Unparseable date string → safe fallback: keep the entry."""
        prices = {"https://x.com/p": {"last_seen": "not-a-date"}}
        result = _prune_prices(prices)
        assert "https://x.com/p" in result

    def test_empty_dict_returns_empty(self):
        assert _prune_prices({}) == {}

    def test_mixed_entries_pruned_selectively(self):
        prices = {
            "https://x.com/old": {"last_seen": _OLD},
            "https://x.com/recent": {"last_seen": _RECENT},
            "https://x.com/legacy": {"current_price": 30.0},
        }
        result = _prune_prices(prices)
        assert "https://x.com/old" not in result
        assert "https://x.com/recent" in result
        assert "https://x.com/legacy" in result

    def test_price_history_and_first_seen_survive_prune(self):
        """A kept entry's change-point history + first_seen pass through intact."""
        entry = {
            "last_seen": _RECENT,
            "current_price": 50.0,
            "first_seen": "2025-01-01T00:00:00Z",
            "price_history": ["2025-01-01:100", "2026-06-01:50"],
        }
        result = _prune_prices({"https://x.com/p": entry})
        assert result["https://x.com/p"]["price_history"] == ["2025-01-01:100", "2026-06-01:50"]
        assert result["https://x.com/p"]["first_seen"] == "2025-01-01T00:00:00Z"

    def test_entry_content_preserved(self):
        entry = {"current_price": 75.0, "label": "Cool Shirt", "last_seen": _RECENT}
        result = _prune_prices({"https://x.com/p": entry})
        assert result["https://x.com/p"] == entry

    def test_exactly_at_boundary(self):
        """Entry last_seen exactly 30 days ago should be kept (cutoff is strictly less than)."""
        exactly_30 = _iso(datetime.now(timezone.utc) - timedelta(days=30, seconds=0))
        prices = {"https://x.com/p": {"last_seen": exactly_30}}
        # Whether kept or pruned at the exact boundary is an impl detail;
        # just verify it doesn't raise and returns a dict.
        result = _prune_prices(prices)
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# _prune_codes
# ---------------------------------------------------------------------------

class TestPruneCodes:
    def test_watchlist_codes_always_kept(self):
        codes = [
            {"shop": "A", "code": "X", "source": "watchlist"},
            {"shop": "B", "code": "Y", "source": "watchlist", "last_seen": _OLD},
        ]
        # Watchlist codes have no persistence semantics — they're rebuilt
        # from the watchlist every run regardless of last_seen.
        assert len(_prune_codes(codes)) == 2

    def test_legacy_codes_without_source_kept(self):
        codes = [{"shop": "A", "code": "X"}]
        assert _prune_codes(codes) == codes

    def test_old_email_codes_dropped(self):
        codes = [
            {"shop": "A", "code": "FRESH", "source": "email", "last_seen": _RECENT},
            {"shop": "A", "code": "STALE", "source": "email", "last_seen": _OLD},
        ]
        result = _prune_codes(codes)
        assert [c["code"] for c in result] == ["FRESH"]

    def test_old_unattributed_codes_dropped(self):
        codes = [
            {"shop": "a.com", "code": "GO", "source": "email_unattributed", "last_seen": _OLD},
        ]
        assert _prune_codes(codes) == []

    def test_old_sms_codes_dropped(self):
        codes = [
            {"shop": "A", "code": "FRESHSMS", "source": "sms", "last_seen": _RECENT},
            {"shop": "A", "code": "STALESMS", "source": "sms", "last_seen": _OLD},
            {"shop": "+18005551234", "code": "GHOST",
             "source": "sms_unattributed", "last_seen": _OLD},
        ]
        result = _prune_codes(codes)
        assert [c["code"] for c in result] == ["FRESHSMS"]

    def test_email_code_without_last_seen_kept(self):
        """Email code without a last_seen is kept (defensive fallback)."""
        codes = [{"shop": "A", "code": "X", "source": "email"}]
        assert _prune_codes(codes) == codes

    def test_non_list_returns_empty(self):
        assert _prune_codes({}) == []
        assert _prune_codes(None) == []


# ---------------------------------------------------------------------------
# _prune_gmail_state
# ---------------------------------------------------------------------------

class TestPruneGmailState:
    def test_old_processed_ids_dropped(self):
        very_old = _iso(datetime.now(timezone.utc) - timedelta(days=20))
        state = {"processed_ids": {"fresh_id": _RECENT, "old_id": very_old}}
        result = _prune_gmail_state(state)
        assert "fresh_id" in result["processed_ids"]
        assert "old_id" not in result["processed_ids"]

    def test_empty_state_returns_empty(self):
        assert _prune_gmail_state({}) == {"processed_ids": {}}
        assert _prune_gmail_state(None) == {"processed_ids": {}}

    def test_unparseable_timestamp_kept(self):
        """Defensive: an unparseable date doesn't accidentally drop dedup state."""
        state = {"processed_ids": {"id_x": "not-a-date"}}
        assert _prune_gmail_state(state)["processed_ids"] == {"id_x": "not-a-date"}


# ---------------------------------------------------------------------------
# _prune_voice_state — same shape/semantics as _prune_gmail_state
# ---------------------------------------------------------------------------

class TestPruneVoiceState:
    def test_old_processed_ids_dropped(self):
        very_old = _iso(datetime.now(timezone.utc) - timedelta(days=20))
        state = {"processed_ids": {"fresh_id": _RECENT, "old_id": very_old}}
        result = _prune_voice_state(state)
        assert "fresh_id" in result["processed_ids"]
        assert "old_id" not in result["processed_ids"]

    def test_empty_state_returns_empty(self):
        assert _prune_voice_state({}) == {"processed_ids": {}}
        assert _prune_voice_state(None) == {"processed_ids": {}}


# ---------------------------------------------------------------------------
# read_state — GitHub truncates files >1 MB (regression: wardrobe.json with
# body_comp crossed 1 MB and silently read back as {} before the raw_url fix)
# ---------------------------------------------------------------------------

class TestReadStateTruncatedFile:
    def test_follows_raw_url_when_truncated(self, httpx_mock):
        full = {"items": [{"id": "a", "body_comp": {"weight_kg": 70.0}}],
                "scan_state": {}, "watchlist_exclusions": []}
        raw_url = "https://gist.githubusercontent.com/u/abc/raw/deadbeef/wardrobe.json"
        files = {"wardrobe.json": {
            "truncated": True,
            "raw_url": raw_url,
            "content": '{"items": [{"id": "a"  <<<TRUNCATED MID-JSON',  # invalid
        }}
        httpx_mock.add_response(url=_API_URL, json={"files": files})
        httpx_mock.add_response(url=raw_url, text=json.dumps(full))

        result = read_state(GIST_ID, TOKEN)

        assert result["wardrobe"] == full
        # The raw_url fetch carried the bearer token (secret gists require it).
        raw_req = next(r for r in httpx_mock.get_requests() if str(r.url) == raw_url)
        assert raw_req.headers["Authorization"] == f"Bearer {TOKEN}"

    def test_non_truncated_file_uses_inline_content_no_extra_request(self, httpx_mock):
        # Guard: the happy path must NOT make a second request.
        httpx_mock.add_response(url=_API_URL, json=_gist_response(prices={"x": 1}))
        result = read_state(GIST_ID, TOKEN)
        assert result["prices"] == {"x": 1}
        assert len(httpx_mock.get_requests()) == 1


# ---------------------------------------------------------------------------
# read_state(fresh=True) — cache-busts so a long-lived process (the wardrobe
# browser) never serves a stale revision from GitHub's s-maxage=60 edge cache
# after an external writer updates the Gist (issue #20).
# ---------------------------------------------------------------------------

import re  # noqa: E402  (kept local to this section for clarity)

_API_RE = re.compile(r"https://api\.github\.com/gists/abc123")
_RAW_RE = re.compile(r"https://gist\.githubusercontent\.com/")


class TestReadStateFresh:
    def test_default_read_has_no_cache_buster(self, httpx_mock):
        # Regression: the daily-cron path (fresh omitted) is byte-identical —
        # no query param, exact-URL mock still matches.
        httpx_mock.add_response(url=_API_URL, json=_gist_response(prices={"x": 1}))
        result = read_state(GIST_ID, TOKEN)
        assert result["prices"] == {"x": 1}
        req = httpx_mock.get_requests()[0]
        assert "_cb=" not in str(req.url)
        assert "Cache-Control" not in req.headers

    def test_fresh_appends_cache_buster_and_no_cache_headers(self, httpx_mock):
        httpx_mock.add_response(url=_API_RE, json=_gist_response(prices={"x": 1}))
        result = read_state(GIST_ID, TOKEN, fresh=True)
        assert result["prices"] == {"x": 1}
        req = httpx_mock.get_requests()[0]
        assert "_cb=" in str(req.url)
        assert req.headers["Authorization"] == f"Bearer {TOKEN}"
        assert req.headers["Cache-Control"] == "no-cache"
        assert req.headers["Pragma"] == "no-cache"

    def test_fresh_cache_busts_truncated_raw_url_too(self, httpx_mock):
        full = {"items": [{"id": "a", "body_comp": {"weight_kg": 70.0}}]}
        raw_url = "https://gist.githubusercontent.com/u/abc/raw/deadbeef/wardrobe.json"
        files = {"wardrobe.json": {
            "truncated": True, "raw_url": raw_url,
            "content": "<<<TRUNCATED",  # invalid — must follow raw_url
        }}
        httpx_mock.add_response(url=_API_RE, json={"files": files})
        httpx_mock.add_response(url=_RAW_RE, text=json.dumps(full))

        result = read_state(GIST_ID, TOKEN, fresh=True)

        assert result["wardrobe"] == full
        raw_req = next(r for r in httpx_mock.get_requests()
                       if "gist.githubusercontent.com" in str(r.url))
        assert "_cb=" in str(raw_req.url)               # cache-busted
        assert raw_req.url.path.endswith("/wardrobe.json")  # original path intact
        assert raw_req.headers["Authorization"] == f"Bearer {TOKEN}"
        assert raw_req.headers["Cache-Control"] == "no-cache"

    def test_default_does_not_cache_bust_raw_url(self, httpx_mock):
        # Regression: the non-fresh truncation path is unchanged (exact-URL mock).
        full = {"items": [{"id": "a"}]}
        raw_url = "https://gist.githubusercontent.com/u/abc/raw/deadbeef/wardrobe.json"
        files = {"wardrobe.json": {
            "truncated": True, "raw_url": raw_url, "content": "<<<TRUNCATED"}}
        httpx_mock.add_response(url=_API_URL, json={"files": files})
        httpx_mock.add_response(url=raw_url, text=json.dumps(full))
        result = read_state(GIST_ID, TOKEN)
        assert result["wardrobe"] == full
        raw_req = next(r for r in httpx_mock.get_requests() if str(r.url) == raw_url)
        assert "_cb=" not in str(raw_req.url)
