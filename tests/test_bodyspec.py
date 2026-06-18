"""Tests for src/bodyspec.py.

Network calls (authenticate / list_results / get_composition) are intercepted
by pytest-httpx (httpx_mock fixture). The pure helpers (nearest_result,
build_body_comp, _extract_login_action, _code_from_location, _to_date) are
exercised directly.
"""
from __future__ import annotations

from datetime import date

import pytest

from src import bodyspec
from src.bodyspec import (
    BodyspecAuthError,
    _code_from_location,
    _extract_login_action,
    _to_date,
    authenticate,
    body_comp_from_record,
    build_body_comp,
    build_scan_cache,
    build_scan_record,
    get_composition,
    list_results,
    nearest_result,
)

TOKEN = "bearer-token-123"

_LOGIN_ACTION = (
    "https://auth.bodyspec.com/realms/bodyspec/login-actions/authenticate"
    "?session_code=SC&execution=EX&tab_id=TI"
)


def _login_page(login_action: str = _LOGIN_ACTION) -> str:
    """Minimal stand-in for the Keycloakify login page — just enough of the
    kcContext blob for _extract_login_action to find the form URL."""
    return (
        '<!DOCTYPE html><html><head><script> const kcContext = {'
        '"pageId": "login", "url": {"loginAction": "%s"} };'
        '</script></head><body><div id="root"></div></body></html>'
    ) % login_action


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestExtractLoginAction:
    def test_plain_url(self):
        html = _login_page("https://auth.bodyspec.com/x/authenticate?a=1")
        assert _extract_login_action(html) == "https://auth.bodyspec.com/x/authenticate?a=1"

    def test_json_escaped_slashes(self):
        # Keycloak's JSON often escapes forward slashes as \/.
        raw = "https:\\/\\/auth.bodyspec.com\\/x\\/authenticate?a=1"
        html = _login_page(raw)
        assert _extract_login_action(html) == "https://auth.bodyspec.com/x/authenticate?a=1"

    def test_missing_returns_none(self):
        assert _extract_login_action("<html>no kccontext here</html>") is None


class TestCodeFromLocation:
    def test_extracts_code(self):
        loc = "https://app.bodyspec.com/callback?code=abc123&state=xyz"
        assert _code_from_location(loc) == "abc123"

    def test_no_code_returns_none(self):
        assert _code_from_location("https://app.bodyspec.com/callback?state=xyz") is None

    def test_empty_returns_none(self):
        assert _code_from_location("") is None


class TestToDate:
    def test_iso_date(self):
        assert _to_date("2026-04-15") == date(2026, 4, 15)

    def test_iso_datetime_z(self):
        assert _to_date("2026-04-15T13:30:00Z") == date(2026, 4, 15)

    def test_iso_datetime_offset(self):
        assert _to_date("2026-04-15T13:30:00+00:00") == date(2026, 4, 15)

    def test_none_and_blank(self):
        assert _to_date(None) is None
        assert _to_date("") is None

    def test_garbage(self):
        assert _to_date("not-a-date") is None


# ---------------------------------------------------------------------------
# nearest_result
# ---------------------------------------------------------------------------

class TestNearestResult:
    _SCANS = [
        {"result_id": "old", "start_time": "2026-01-01T09:00:00Z"},
        {"result_id": "mid", "start_time": "2026-04-10T09:00:00Z"},
        {"result_id": "new", "start_time": "2026-06-01T09:00:00Z"},
    ]

    def test_picks_closest_within_window(self):
        got = nearest_result(self._SCANS, "2026-04-15", max_gap_days=90)
        assert got["result_id"] == "mid"

    def test_returns_none_when_all_outside_window(self):
        # Purchase far from every scan.
        got = nearest_result(self._SCANS, "2025-06-01", max_gap_days=90)
        assert got is None

    def test_returns_none_for_empty_scan_list(self):
        assert nearest_result([], "2026-04-15") is None

    def test_returns_none_for_unparseable_target(self):
        assert nearest_result(self._SCANS, "garbage") is None

    def test_skips_scans_with_bad_start_time(self):
        scans = [{"result_id": "bad", "start_time": None},
                 {"result_id": "good", "start_time": "2026-04-12T09:00:00Z"}]
        got = nearest_result(scans, "2026-04-15", max_gap_days=90)
        assert got["result_id"] == "good"

    def test_boundary_exactly_at_max_gap_is_kept(self):
        scans = [{"result_id": "edge", "start_time": "2026-01-15T00:00:00Z"}]
        # 2026-01-15 -> 2026-04-15 is 90 days exactly.
        assert nearest_result(scans, "2026-04-15", max_gap_days=90)["result_id"] == "edge"
        # One day tighter rejects it.
        assert nearest_result(scans, "2026-04-15", max_gap_days=89) is None


# ---------------------------------------------------------------------------
# build_body_comp
# ---------------------------------------------------------------------------

_COMPOSITION = {
    "result_id": "R1",
    "section_name": "total_body",
    "total": {
        "fat_mass_kg": 14.111,
        "lean_mass_kg": 58.04,
        "bone_mass_kg": 3.11,
        "total_mass_kg": 75.26,
        "tissue_fat_pct": 17.91,
        "region_fat_pct": 18.42,
    },
    "regions": {
        "left_arm": {
            "fat_mass_kg": 1.14, "lean_mass_kg": 2.04, "bone_mass_kg": 0.13,
            "total_mass_kg": 3.31, "tissue_fat_pct": 35.92, "region_fat_pct": 34.5,
        },
        "trunk": {
            "fat_mass_kg": 7.22, "lean_mass_kg": 17.32, "bone_mass_kg": 0.57,
            "total_mass_kg": 25.11, "tissue_fat_pct": 29.42, "region_fat_pct": 28.75,
        },
    },
    "android_gynoid_ratio": 0.913,
}


class TestBuildBodyComp:
    def test_field_mapping(self):
        bc = build_body_comp(_COMPOSITION, "2026-04-10T09:00:00Z", "2026-04-15", "purchase")
        assert bc["result_id"] == "R1"
        assert bc["scan_date"] == "2026-04-10"
        assert bc["matched_to"] == "purchase"
        assert bc["matched_date"] == "2026-04-15"
        assert bc["weight_kg"] == 75.26
        assert bc["body_fat_pct"] == 18.42
        assert bc["tissue_fat_pct"] == 17.91
        assert bc["lean_mass_kg"] == 58.04
        assert bc["fat_mass_kg"] == 14.11           # rounded to 2dp
        assert bc["bone_mass_kg"] == 3.11
        assert bc["android_gynoid_ratio"] == 0.91   # rounded to 2dp
        # Full per-region breakdown — every metric, not just lean mass.
        assert bc["regions"] == {
            "left_arm": {
                "fat_mass_kg": 1.14, "lean_mass_kg": 2.04, "bone_mass_kg": 0.13,
                "total_mass_kg": 3.31, "tissue_fat_pct": 35.92, "region_fat_pct": 34.5,
            },
            "trunk": {
                "fat_mass_kg": 7.22, "lean_mass_kg": 17.32, "bone_mass_kg": 0.57,
                "total_mass_kg": 25.11, "tissue_fat_pct": 29.42, "region_fat_pct": 28.75,
            },
        }
        assert "fetched_at" in bc

    def test_days_from_event_sign_scan_before(self):
        # scan 5 days BEFORE purchase -> negative
        bc = build_body_comp(_COMPOSITION, "2026-04-10", "2026-04-15", "purchase")
        assert bc["days_from_event"] == -5

    def test_days_from_event_sign_scan_after(self):
        bc = build_body_comp(_COMPOSITION, "2026-04-20", "2026-04-15", "fit_review")
        assert bc["days_from_event"] == 5
        assert bc["matched_to"] == "fit_review"

    def test_missing_totals_are_none_not_crash(self):
        bc = build_body_comp({"result_id": "R2", "regions": {}}, "2026-04-10", "2026-04-15", "purchase")
        assert bc["weight_kg"] is None
        assert bc["regions"] == {}

    def test_partial_region_fills_missing_metrics_with_none(self):
        comp = {"result_id": "R3", "total": {}, "regions": {"trunk": {"lean_mass_kg": 17.3}}}
        bc = build_body_comp(comp, "2026-04-10", "2026-04-15", "purchase")
        assert bc["regions"]["trunk"] == {
            "fat_mass_kg": None, "lean_mass_kg": 17.3, "bone_mass_kg": None,
            "total_mass_kg": None, "tissue_fat_pct": None, "region_fat_pct": None,
        }


# ---------------------------------------------------------------------------
# authenticate (scripted Keycloak login)
# ---------------------------------------------------------------------------

class TestAuthenticate:
    def test_happy_path(self, httpx_mock):
        httpx_mock.add_response(method="GET", text=_login_page())
        httpx_mock.add_response(
            method="POST", url=_LOGIN_ACTION, status_code=302,
            headers={"location": f"{bodyspec.REDIRECT_URI}?code=AUTHCODE&state=x"},
        )
        httpx_mock.add_response(
            method="POST", url=bodyspec._TOKEN_ENDPOINT,
            json={"access_token": "tok123", "token_type": "Bearer"},
        )
        assert authenticate("me@example.com", "hunter2") == "tok123"

        # The credentials were posted to the loginAction URL.
        login_req = next(
            r for r in httpx_mock.get_requests()
            if r.method == "POST" and "login-actions" in str(r.url)
        )
        body = login_req.read().decode()
        assert "username=me%40example.com" in body
        assert "password=hunter2" in body

    def test_bad_credentials_raise(self, httpx_mock):
        # Wrong creds → Keycloak re-renders the form (200, no redirect).
        httpx_mock.add_response(method="GET", text=_login_page())
        httpx_mock.add_response(method="POST", url=_LOGIN_ACTION, status_code=200,
                                text=_login_page())
        with pytest.raises(BodyspecAuthError):
            authenticate("me@example.com", "wrong")

    def test_redirect_without_code_raises(self, httpx_mock):
        httpx_mock.add_response(method="GET", text=_login_page())
        httpx_mock.add_response(
            method="POST", url=_LOGIN_ACTION, status_code=302,
            headers={"location": f"{bodyspec.REDIRECT_URI}?state=x"},
        )
        with pytest.raises(BodyspecAuthError):
            authenticate("me@example.com", "hunter2")

    def test_unparseable_login_page_raises(self, httpx_mock):
        httpx_mock.add_response(method="GET", text="<html>no kccontext</html>")
        with pytest.raises(BodyspecAuthError):
            authenticate("me@example.com", "hunter2")

    def test_blank_credentials_raise_without_network(self):
        # No httpx_mock responses registered — must fail before any request.
        with pytest.raises(BodyspecAuthError):
            authenticate("", "")


# ---------------------------------------------------------------------------
# list_results (pagination)
# ---------------------------------------------------------------------------

class TestListResults:
    def test_single_page(self, httpx_mock):
        httpx_mock.add_response(
            method="GET",
            json={
                "results": [
                    {"result_id": "a", "start_time": "2026-01-01T09:00:00Z"},
                    {"result_id": "b", "start_time": "2026-04-01T09:00:00Z"},
                ],
                "pagination": {"page": 1, "has_more": False},
            },
        )
        out = list_results(TOKEN)
        assert [r["result_id"] for r in out] == ["a", "b"]

    def test_paginates_until_has_more_false(self, httpx_mock):
        httpx_mock.add_response(
            method="GET",
            json={
                "results": [{"result_id": "a", "start_time": "2026-01-01T09:00:00Z"}],
                "pagination": {"page": 1, "has_more": True},
            },
        )
        httpx_mock.add_response(
            method="GET",
            json={
                "results": [{"result_id": "b", "start_time": "2026-04-01T09:00:00Z"}],
                "pagination": {"page": 2, "has_more": False},
            },
        )
        out = list_results(TOKEN)
        assert [r["result_id"] for r in out] == ["a", "b"]
        # Bearer token went out on the request.
        assert httpx_mock.get_requests()[0].headers["Authorization"] == f"Bearer {TOKEN}"


# ---------------------------------------------------------------------------
# get_composition
# ---------------------------------------------------------------------------

class TestGetComposition:
    def test_returns_payload(self, httpx_mock):
        httpx_mock.add_response(method="GET", json=_COMPOSITION)
        got = get_composition(TOKEN, "R1")
        assert got["total"]["total_mass_kg"] == 75.26
        req = httpx_mock.get_requests()[0]
        assert "/results/R1/dexa/composition" in str(req.url)


# ---------------------------------------------------------------------------
# build_scan_record / body_comp_from_record (the cached-scan split)
# ---------------------------------------------------------------------------

class TestBuildScanRecord:
    def test_scan_intrinsic_fields_only(self):
        rec = build_scan_record(_COMPOSITION, "2026-04-10T09:00:00Z")
        assert rec["result_id"] == "R1"
        assert rec["scan_date"] == "2026-04-10"
        assert rec["start_time"] == "2026-04-10T09:00:00Z"
        assert rec["weight_kg"] == 75.26
        assert rec["body_fat_pct"] == 18.42
        assert rec["android_gynoid_ratio"] == 0.91
        assert rec["regions"]["trunk"]["lean_mass_kg"] == 17.32
        # No per-item match fields live on the cached record.
        for k in ("matched_to", "matched_date", "days_from_event", "fetched_at"):
            assert k not in rec

    def test_result_id_override_wins(self):
        # The cache passes the known id from list_results even if the composition
        # payload omits it.
        comp = {k: v for k, v in _COMPOSITION.items() if k != "result_id"}
        rec = build_scan_record(comp, "2026-04-10", result_id="RX")
        assert rec["result_id"] == "RX"

    def test_start_time_from_date_object(self):
        rec = build_scan_record(_COMPOSITION, date(2026, 4, 10))
        assert rec["scan_date"] == "2026-04-10"
        assert rec["start_time"] == "2026-04-10"


class TestBodyCompFromRecord:
    def test_adds_match_fields_with_signed_days(self):
        rec = build_scan_record(_COMPOSITION, "2026-04-10T09:00:00Z")
        bc = body_comp_from_record(rec, "2026-04-15", "fit_review")
        assert bc["matched_to"] == "fit_review"
        assert bc["matched_date"] == "2026-04-15"
        assert bc["days_from_event"] == -5      # scan 5d before the event
        assert bc["weight_kg"] == 75.26
        assert bc["regions"]["trunk"]["lean_mass_kg"] == 17.32
        assert "fetched_at" in bc


# ---------------------------------------------------------------------------
# build_scan_cache (list_results + get_composition → pre-shaped records)
# ---------------------------------------------------------------------------

class TestBuildScanCache:
    def test_shapes_every_scan_and_stamps_refreshed_at(self, httpx_mock):
        httpx_mock.add_response(
            method="GET",
            json={
                "results": [
                    {"result_id": "a", "start_time": "2026-01-01T09:00:00Z"},
                    {"result_id": "b", "start_time": "2026-04-01T09:00:00Z"},
                ],
                "pagination": {"page": 1, "has_more": False},
            },
        )
        # One composition GET per scan (a, then b).
        httpx_mock.add_response(method="GET", json=dict(_COMPOSITION, result_id="a"))
        httpx_mock.add_response(method="GET", json=dict(_COMPOSITION, result_id="b"))

        cache = build_scan_cache(TOKEN)
        assert "refreshed_at" in cache
        assert [s["result_id"] for s in cache["scans"]] == ["a", "b"]
        # Records carry start_time so nearest_result can match them later.
        assert cache["scans"][0]["start_time"] == "2026-01-01T09:00:00Z"
        assert cache["scans"][1]["weight_kg"] == 75.26

    def test_nearest_result_matches_cached_records(self, httpx_mock):
        httpx_mock.add_response(
            method="GET",
            json={
                "results": [
                    {"result_id": "jan", "start_time": "2026-01-02T09:00:00Z"},
                    {"result_id": "jun", "start_time": "2026-06-02T09:00:00Z"},
                ],
                "pagination": {"has_more": False},
            },
        )
        httpx_mock.add_response(method="GET", json=dict(_COMPOSITION, result_id="jan"))
        httpx_mock.add_response(method="GET", json=dict(_COMPOSITION, result_id="jun"))
        records = build_scan_cache(TOKEN)["scans"]

        near = nearest_result(records, "2026-06-01", max_gap_days=90)
        assert near["result_id"] == "jun"
        assert nearest_result(records, "2026-03-15", max_gap_days=30) is None
