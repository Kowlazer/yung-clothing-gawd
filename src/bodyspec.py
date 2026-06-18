"""BodySpec DEXA API client — authenticate and pull body-composition scans.

Manual / wardrobe-only — not part of the daily cron. Used by
``src/order_scan.py --backfill-bodycomp`` to stamp each purchased item with the
body-composition metrics from the DEXA scan closest in time to its purchase.

Why this module looks the way it does
--------------------------------------
BodySpec's public API (``https://app.bodyspec.com``, OpenAPI at ``/openapi.json``)
is JWT-bearer protected. Tokens come from Keycloak (``auth.bodyspec.com``, realm
``bodyspec``) via the public client ``bodyspec-api-ext-v1``. That client only
permits the **authorization-code + PKCE** flow — the Resource-Owner Password
grant is disabled (``"Client not allowed for direct access grants"``). So there
is no one-shot username/password token endpoint to call.

``authenticate`` therefore scripts the browser login by hand with plain
``httpx`` (no headless browser needed):

  1. Build a PKCE verifier/challenge (S256).
  2. ``GET`` the Keycloak authorize endpoint. Keycloak sets session cookies and
     returns the login page. The page is a Keycloakify (React) theme, so the
     form ``action`` isn't a plain ``<form>`` — it's embedded in a ``kcContext``
     JSON blob as ``"loginAction": "..."``. We extract that.
  3. ``POST`` ``username``/``password`` to that ``loginAction`` URL (cookies from
     step 2 ride along on the same client). Bad creds re-render the form (200);
     good creds ``302`` to ``redirect_uri?code=...`` — we read the code straight
     out of the ``Location`` header, no callback server required.
  4. Exchange the code (+ PKCE verifier) at the token endpoint for a bearer
     ``access_token``.

The access token is short-lived; this module re-authenticates per process. The
backfill is a manual, infrequent command, so a fresh login each run is fine.

Pure helpers ``nearest_result`` and ``build_body_comp`` carry no I/O so they're
unit-testable and reusable at fit-review time later (match against
``reviewed_at`` instead of ``purchased_at``).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Endpoints / client config
# ---------------------------------------------------------------------------

API_BASE = "https://app.bodyspec.com"
_AUTH_REALM = "https://auth.bodyspec.com/realms/bodyspec/protocol/openid-connect"
_AUTH_ENDPOINT = f"{_AUTH_REALM}/auth"
_TOKEN_ENDPOINT = f"{_AUTH_REALM}/token"
CLIENT_ID = "bodyspec-api-ext-v1"
# Any https://app.bodyspec.com/* path is a registered redirect for the client;
# we never serve it — we only read the ?code= off the 302 Location header.
REDIRECT_URI = "https://app.bodyspec.com/callback"

_TIMEOUT = 30.0
_RESULTS_PAGE_SIZE = 100

_LOGIN_ACTION_RE = re.compile(r'"loginAction"\s*:\s*"((?:[^"\\]|\\.)*)"')


class BodyspecError(RuntimeError):
    """Base class for BodySpec client failures."""


class BodyspecAuthError(BodyspecError):
    """Raised when the scripted Keycloak login can't produce a token —
    almost always bad ``BODYSPEC_USERNAME`` / ``BODYSPEC_PASSWORD``, or a
    change in Keycloak's login page that broke the scrape."""


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _pkce_pair() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` for a PKCE S256 exchange."""
    verifier = _b64url(os.urandom(40))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _extract_login_action(html: str) -> str | None:
    """Pull the form-POST URL out of the Keycloakify login page.

    The value sits in the page's ``kcContext`` JSON as
    ``"loginAction": "https:\\/\\/auth.bodyspec.com/realms/.../authenticate?..."``.
    We decode it as a JSON string so any escapes (``\\/``, ``\\u0026``) resolve.
    """
    m = _LOGIN_ACTION_RE.search(html)
    if not m:
        return None
    try:
        return json.loads('"' + m.group(1) + '"')
    except json.JSONDecodeError:
        return None


def _code_from_location(location: str) -> str | None:
    """Read the ``code`` query param out of a redirect ``Location`` header."""
    if not location:
        return None
    return (parse_qs(urlparse(location).query).get("code") or [None])[0]


def authenticate(username: str, password: str, *, scope: str = "openid") -> str:
    """Log in via scripted Keycloak auth-code + PKCE; return a bearer token.

    Raises ``BodyspecAuthError`` if credentials are missing/wrong or the login
    page can't be parsed.
    """
    if not (username or "").strip() or not (password or "").strip():
        raise BodyspecAuthError(
            "BODYSPEC_USERNAME / BODYSPEC_PASSWORD are required for the "
            "BodySpec backfill but were empty"
        )

    verifier, challenge = _pkce_pair()
    with httpx.Client(follow_redirects=False, timeout=_TIMEOUT) as client:
        # 1+2. Authorize request → cookies + login page.
        resp = client.get(
            _AUTH_ENDPOINT,
            params={
                "client_id": CLIENT_ID,
                "response_type": "code",
                "scope": scope,
                "redirect_uri": REDIRECT_URI,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": _b64url(os.urandom(8)),
            },
        )
        resp.raise_for_status()
        login_action = _extract_login_action(resp.text)
        if not login_action:
            raise BodyspecAuthError(
                "could not locate the Keycloak loginAction in the login page "
                "(the login form may have changed)"
            )

        # 3. Submit credentials. Good creds → 302 to redirect_uri?code=...
        login = client.post(
            login_action,
            data={"username": username, "password": password, "credentialId": ""},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if login.status_code not in (302, 303):
            raise BodyspecAuthError(
                "BodySpec login was rejected — check BODYSPEC_USERNAME / "
                f"BODYSPEC_PASSWORD (Keycloak returned {login.status_code}, "
                "expected a redirect)"
            )
        code = _code_from_location(login.headers.get("location", ""))
        if not code:
            raise BodyspecAuthError(
                "BodySpec login redirect carried no authorization code "
                f"(location: {login.headers.get('location', '')!r})"
            )

        # 4. Exchange the code for an access token.
        token_resp = client.post(
            _TOKEN_ENDPOINT,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": CLIENT_ID,
                "code_verifier": verifier,
            },
        )
        token_resp.raise_for_status()
        token = token_resp.json().get("access_token")
        if not token:
            raise BodyspecAuthError("token endpoint returned no access_token")
        log.info("bodyspec: authenticated as %s", username)
        return token


# ---------------------------------------------------------------------------
# API reads
# ---------------------------------------------------------------------------

def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def list_results(token: str) -> list[dict]:
    """Return every DEXA scan result as ``[{"result_id", "start_time"}, ...]``.

    Paginates ``GET /api/v1/users/me/results/`` until ``pagination.has_more``
    is false. Newest-first ordering is whatever the API returns — callers match
    by date, not position.
    """
    out: list[dict] = []
    page = 1
    with httpx.Client(timeout=_TIMEOUT) as client:
        while True:
            resp = client.get(
                f"{API_BASE}/api/v1/users/me/results/",
                params={"page": page, "page_size": _RESULTS_PAGE_SIZE},
                headers=_auth_headers(token),
            )
            resp.raise_for_status()
            payload = resp.json()
            for r in payload.get("results") or []:
                out.append({
                    "result_id": r.get("result_id"),
                    "start_time": r.get("start_time"),
                })
            pagination = payload.get("pagination") or {}
            if not pagination.get("has_more"):
                break
            page += 1
    log.info("bodyspec: fetched %d scan result(s)", len(out))
    return out


def get_composition(token: str, result_id: str) -> dict:
    """Return the raw DEXA composition payload for one scan.

    Shape: ``{total: BodyRegion, regions: {region: BodyRegion}, android_gynoid_ratio}``
    where BodyRegion has ``fat_mass_kg``/``lean_mass_kg``/``bone_mass_kg``/
    ``total_mass_kg``/``tissue_fat_pct``/``region_fat_pct``.
    """
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.get(
            f"{API_BASE}/api/v1/users/me/results/{result_id}/dexa/composition",
            headers=_auth_headers(token),
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Pure helpers (no I/O — reused at fit-review time later)
# ---------------------------------------------------------------------------

def _to_date(value) -> date | None:
    """Coerce an ISO date or datetime string (or date/datetime) to a date."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None


def nearest_result(
    results: list[dict],
    target_date,
    max_gap_days: int = 90,
) -> dict | None:
    """Return the scan whose ``start_time`` is closest to ``target_date``.

    Returns ``None`` when there are no scans, when ``target_date`` is
    unparseable, or when even the closest scan is more than ``max_gap_days``
    away (so callers leave ``body_comp`` unset rather than attaching a scan
    from a wholly different body state).
    """
    target = _to_date(target_date)
    if target is None:
        return None
    best: dict | None = None
    best_gap: int | None = None
    for r in results or []:
        scan_date = _to_date(r.get("start_time"))
        if scan_date is None:
            continue
        gap = abs((scan_date - target).days)
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best = r
    if best is None or best_gap is None or best_gap > max_gap_days:
        return None
    return best


def _round(value, ndigits: int = 2):
    return round(value, ndigits) if isinstance(value, (int, float)) else value


# Every metric the API reports for a body region (and for the whole-body
# ``total``). Regional fat matters for fit as much as regional lean, so we keep
# the full per-region breakdown rather than lean mass alone.
_REGION_FIELDS = (
    "fat_mass_kg", "lean_mass_kg", "bone_mass_kg",
    "total_mass_kg", "tissue_fat_pct", "region_fat_pct",
)


def _region_metrics(region: dict) -> dict:
    """All six BodyRegion metrics, rounded, with missing values left as None."""
    return {f: _round(region.get(f)) for f in _REGION_FIELDS}


def _start_time_iso(scan_start_time) -> str | None:
    """Coerce a scan's start_time (str/date/datetime) to an ISO string for the
    cache — preserved verbatim so ``nearest_result`` can match against it."""
    if isinstance(scan_start_time, str):
        return scan_start_time or None
    if isinstance(scan_start_time, (date, datetime)):
        return scan_start_time.isoformat()
    return None


def build_scan_record(composition: dict, scan_start_time, result_id=None) -> dict:
    """Pre-shape one DEXA scan into the unit stored in ``body_scans.json``.

    Carries everything that depends only on the scan itself — the whole-body
    ``total`` (a few fields renamed for readability; ``weight_kg`` is the scanned
    ``total_mass_kg``, ``body_fat_pct`` the total-region fat %) plus the **full
    per-region breakdown** under ``regions`` (fat/lean/bone/total mass + both
    fat-% figures for each of left_arm/right_arm/left_leg/right_leg/trunk/
    android/gynoid) — but *not* the per-item match fields. Those are added later
    by :func:`body_comp_from_record` against a specific purchase/review date.

    Splitting the scan-intrinsic data from the per-match data is what lets one
    cached scan serve many items, and lets the cron, the CLI backfill, and the
    Apps Script web form all build byte-identical ``body_comp`` blocks. ``start_time``
    is kept on the record so :func:`nearest_result` can match cached scans by date.
    """
    total = composition.get("total") or {}
    regions = composition.get("regions") or {}
    regions_out = {
        name: _region_metrics(region)
        for name, region in regions.items()
        if isinstance(region, dict)
    }
    scan_date = _to_date(scan_start_time)
    return {
        "result_id": result_id if result_id is not None else composition.get("result_id"),
        "scan_date": scan_date.isoformat() if scan_date else None,
        "start_time": _start_time_iso(scan_start_time),
        # Whole-body totals (renamed for readability; this is the full `total`).
        "weight_kg": _round(total.get("total_mass_kg")),
        "body_fat_pct": _round(total.get("region_fat_pct")),
        "tissue_fat_pct": _round(total.get("tissue_fat_pct")),
        "lean_mass_kg": _round(total.get("lean_mass_kg")),
        "fat_mass_kg": _round(total.get("fat_mass_kg")),
        "bone_mass_kg": _round(total.get("bone_mass_kg")),
        "android_gynoid_ratio": _round(composition.get("android_gynoid_ratio")),
        # Full per-region breakdown (all six metrics per region).
        "regions": regions_out,
    }


def body_comp_from_record(record: dict, target_date, matched_to: str) -> dict:
    """Attach this item's match provenance to a cached scan record.

    Produces exactly the schema :func:`build_body_comp` returns: the scan
    record's metrics + ``matched_to`` / ``matched_date`` / ``days_from_event``
    for *this* item's purchase or review date, plus a fresh ``fetched_at``.

    ``matched_to`` is ``"purchase"`` (matched against ``purchased_at``) or
    ``"fit_review"`` (matched against ``reviewed_at``). ``days_from_event`` is
    signed: negative means the scan predates the event.
    """
    scan_date = _to_date(record.get("scan_date") or record.get("start_time"))
    target = _to_date(target_date)
    days_from_event = (
        (scan_date - target).days
        if scan_date is not None and target is not None
        else None
    )
    return {
        "result_id": record.get("result_id"),
        "scan_date": scan_date.isoformat() if scan_date else None,
        "matched_to": matched_to,
        "matched_date": target.isoformat() if target else None,
        "days_from_event": days_from_event,
        "weight_kg": record.get("weight_kg"),
        "body_fat_pct": record.get("body_fat_pct"),
        "tissue_fat_pct": record.get("tissue_fat_pct"),
        "lean_mass_kg": record.get("lean_mass_kg"),
        "fat_mass_kg": record.get("fat_mass_kg"),
        "bone_mass_kg": record.get("bone_mass_kg"),
        "android_gynoid_ratio": record.get("android_gynoid_ratio"),
        "regions": record.get("regions") or {},
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def build_body_comp(
    composition: dict,
    scan_start_time,
    target_date,
    matched_to: str,
) -> dict:
    """Shape a stored ``body_comp`` block straight from a raw composition payload.

    Thin composition of :func:`build_scan_record` (scan-intrinsic shaping) and
    :func:`body_comp_from_record` (per-item match fields) so there is one source
    of truth for the block's schema. Used by the live-fetch backfill path; the
    cached path calls ``body_comp_from_record`` on a stored record directly.
    """
    return body_comp_from_record(
        build_scan_record(composition, scan_start_time), target_date, matched_to,
    )


def build_scan_cache(token: str) -> dict:
    """Pull every DEXA scan + composition and pre-shape them for ``body_scans.json``.

    Fetches each scan's composition once. Returns
    ``{"refreshed_at": ISO, "scans": [build_scan_record(...), ...]}`` — the cache
    the daily cron writes to the Gist and the web form / CLI read back to match a
    body state to a review/purchase date without re-hitting BodySpec. Any scan or
    composition fetch error propagates so the caller keeps the prior cache intact
    rather than persisting a partial one missing the newest scan.
    """
    results = list_results(token)
    scans = [
        build_scan_record(
            get_composition(token, r.get("result_id")),
            r.get("start_time"),
            result_id=r.get("result_id"),
        )
        for r in results
    ]
    log.info("bodyspec: built scan cache with %d scan(s)", len(scans))
    return {
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "scans": scans,
    }
