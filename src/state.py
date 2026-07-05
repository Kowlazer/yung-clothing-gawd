"""Read and write state to GitHub Gist.

Gist files (14):
  * prices.json       — per-URL price history, pruned after 30 days
  * shop_aliases.json — known shop name → homepage URL
  * codes.json        — promo codes (source: "watchlist" | "email" |
                        "email_unattributed" | "sms" | "sms_unattributed")
  * email_sales.json  — persisted email/SMS sale announcements (status "yes",
                        with resolved starts_on/ends_on) so advance + multi-day
                        sales keep showing until they end. Pruned by date /
                        undated TTL in src/email_sales.py.
  * fx_rates.json     — cached FX rate snapshot
  * gmail_state.json  — processed_ids dedupe for the Gmail Promotions tab
  * voice_state.json  — processed_ids dedupe for the GV-forward SMS pipeline
  * sms_aliases.json  — sender phone number → canonical shop name
                        (parallel to shop_aliases for SMS attribution)
  * signup_state.json — newsletter-signup record per shop homepage:
                        {url: {email: {signed_up_at, code_received} | null,
                               phone: {signed_up_at} | null,
                               attempts: [{at, channel, result}, ...]}}
  * wardrobe.json     — purchased-item catalogue from Gmail order/shipping
                        scans (src/order_scan.py). Four sections:
                        {items: [...], scan_state: {last_scanned_at,
                        processed_email_ids}, watchlist_exclusions: [...],
                        shop_fit_notes: {shop: note}}.
                        Items and processed_email_ids are NOT pruned —
                        scans are infrequent and dedupe must survive years.
  * body_scans.json   — cached BodySpec DEXA scans, pre-shaped for matching:
                        {refreshed_at, scans: [build_scan_record(...), ...]}.
                        Refreshed ~weekly by the daily cron (age-gated; see
                        main._maybe_refresh_body_scans) so the fit-feedback web
                        form / CLI backfill can match a body state to a review/
                        purchase date without re-hitting BodySpec. Not pruned.
  * shop_verdicts.json — per-shop homepage sale-verdict cache (cost lever #3):
                        [{shop, hash, status, description, checked_at}, ...].
                        A homepage whose sale-signal hash matches a still-fresh
                        cached entry is reused instead of re-judged by Claude.
                        Owned by src/shop_verdicts.py; pruned there by age.
  * restock_state.json — restock-notification signup record per product URL:
                        {url: {sizes: {size|"__product__":
                               {signed_up_at, vendor} | null},
                               attempts: [{at, size, result, vendor}, ...]}}.
                        Written by the manual src/restock_signup.py command.
  * shadow_runs.json  — shadow A/B verdict-diff log (cost lever #5, issue
                        #16): {runs: [{at, primary_model, shadow_model,
                        summary, disagreements, primary_usage,
                        shadow_usage}, ...]}. Appended only while the
                        SHADOW_MODEL env var is set; owned + pruned by
                        src/shadow_compare.py; reviewed via
                        `python -m src.shadow_report`.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

_GIST_API = "https://api.github.com/gists"
_GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
# Belt-and-suspenders request headers asking any intermediary cache to
# revalidate. Paired with the cache-buster query param (the reliable lever —
# Fastly may ignore client Cache-Control on requests) for ``fresh=True`` reads.
_NO_CACHE_HEADERS = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
_PRUNE_DAYS = 30           # prices + email/sms-sourced codes
_GMAIL_PRUNE_DAYS = 14     # processed-email-id dedupe window
_VOICE_PRUNE_DAYS = 14     # processed-sms-id dedupe window
_TIMEOUT = 15.0


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", **_GITHUB_HEADERS}


def _cache_bust(url: str) -> str:
    """Append a unique query param so a shared edge cache can't serve a stale copy.

    The Gist-API metadata GET returns ``Cache-Control: private, max-age=60,
    s-maxage=60`` — that ``s-maxage`` lets GitHub's Fastly edge serve a cached
    ``/gists/{id}`` response (and thus a stale file revision / ``raw_url``) for
    up to a minute after an external writer updates the Gist. A long-lived
    process (the wardrobe browser) can keep hitting that warm edge entry over a
    keep-alive connection and so keep serving pre-write data even across a
    Refresh, while a freshly-started process reads correctly (issue #20). A
    unique query string is a unique cache key → guaranteed miss → fresh origin
    read. Harmless: GitHub ignores the extra param (verified 200 + correct body
    on both the API and the raw CDN).
    """
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_cb={time.time_ns()}"


def _file_content(
    f: dict, client: httpx.Client | None, token: str | None, *, fresh: bool = False,
) -> str:
    """Full text content of one Gist file.

    GitHub truncates a file's inline ``content`` once it exceeds 1 MB and sets
    ``truncated: true`` with a ``raw_url`` pointing at the full bytes. We follow
    that so large state files read back intact rather than as truncated
    (malformed) JSON. ``wardrobe.json`` crossed 1 MB once items carry the full
    BodySpec ``body_comp`` block (see src/bodyspec.py), so without this the whole
    wardrobe would silently read back as ``{}`` — a data-loss trap for the next
    read-modify-write. The secret-gist ``raw_url`` requires the bearer token.

    ``fresh=True`` cache-busts the ``raw_url`` fetch too (see ``_cache_bust``).
    The ``raw_url`` is SHA-content-addressed so it's immutable per revision, but
    busting it costs nothing and defends against an unversioned URL / odd cache.
    """
    if f.get("truncated") and f.get("raw_url") and client is not None:
        url = f["raw_url"]
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if fresh:
            url = _cache_bust(url)
            headers = {**headers, **_NO_CACHE_HEADERS}
        resp = client.get(url, headers=headers or None)
        resp.raise_for_status()
        return resp.text
    return f.get("content") or ""


def _parse_gist_file(
    files: dict, name: str, default: Any = None,
    *, client: httpx.Client | None = None, token: str | None = None,
    fresh: bool = False,
) -> Any:
    """Parse one file out of a Gist payload. Returns ``default`` (or ``{}``)
    when the file is missing, empty, or malformed JSON. Follows ``raw_url`` for
    files GitHub truncated (>1 MB) when a ``client``/``token`` are supplied."""
    if default is None:
        default = {}
    f = files.get(name)
    if not f:
        return default
    content = _file_content(f, client, token, fresh=fresh)
    if not content.strip():
        return default
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        log.warning("state: malformed JSON in gist file %s — treating as default", name)
        return default


def read_state(gist_id: str, token: str, *, fresh: bool = False) -> dict:
    """Fetch the Gist and return all state blobs as a dict.

    ``fresh=True`` cache-busts the reads so a long-lived process never serves a
    stale revision from GitHub's edge cache (issue #20 — see ``_cache_bust``).
    The daily cron leaves it ``False`` (a fresh process per run; never affected),
    so its behaviour is unchanged; the wardrobe browser passes ``True``.
    """
    with httpx.Client(timeout=_TIMEOUT) as client:
        url = f"{_GIST_API}/{gist_id}"
        headers = _auth_headers(token)
        if fresh:
            url = _cache_bust(url)
            headers = {**headers, **_NO_CACHE_HEADERS}
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        files = resp.json().get("files", {})
        # Keep the client open: truncated (>1 MB) files are re-fetched via raw_url.
        opts = {"client": client, "token": token, "fresh": fresh}
        return {
            "prices": _parse_gist_file(files, "prices.json", default={}, **opts),
            "aliases": _parse_gist_file(files, "shop_aliases.json", default={}, **opts),
            "codes": _parse_gist_file(files, "codes.json", default=[], **opts),
            "email_sales": _parse_gist_file(files, "email_sales.json", default=[], **opts),
            "fx": _parse_gist_file(files, "fx_rates.json", default={}, **opts),
            "gmail": _parse_gist_file(files, "gmail_state.json", default={}, **opts),
            "voice": _parse_gist_file(files, "voice_state.json", default={}, **opts),
            "sms_aliases": _parse_gist_file(files, "sms_aliases.json", default={}, **opts),
            "signup": _parse_gist_file(files, "signup_state.json", default={}, **opts),
            "wardrobe": _parse_gist_file(files, "wardrobe.json", default={}, **opts),
            "body_scans": _parse_gist_file(files, "body_scans.json", default={}, **opts),
            "shop_verdicts": _parse_gist_file(files, "shop_verdicts.json", default=[], **opts),
            "restock": _parse_gist_file(files, "restock_state.json", default={}, **opts),
            "shadow_runs": _parse_gist_file(files, "shadow_runs.json", default={}, **opts),
        }


def _prune_prices(prices: dict) -> dict:
    """Drop entries whose last_seen is older than _PRUNE_DAYS days."""
    cutoff = datetime.now(timezone.utc).timestamp() - _PRUNE_DAYS * 86400
    result = {}
    for url, entry in prices.items():
        last_seen = entry.get("last_seen")
        if last_seen:
            try:
                ts = datetime.fromisoformat(last_seen.replace("Z", "+00:00")).timestamp()
                if ts < cutoff:
                    log.info("state: pruning stale entry %s (last_seen=%s)", url, last_seen)
                    continue
            except (ValueError, AttributeError):
                pass  # keep entries with unparseable dates (safe fallback)
        result[url] = entry
    return result


_PRUNABLE_CODE_SOURCES = frozenset({
    "email", "email_unattributed", "sms", "sms_unattributed",
})


def _prune_codes(codes: Any) -> list:
    """Drop email/sms-sourced codes whose last_seen is older than _PRUNE_DAYS days.

    Watchlist-sourced codes are passed through untouched — they are rebuilt
    fresh from the watchlist text on every run. Legacy entries without a
    ``source`` field are treated as ``"watchlist"`` and kept.
    """
    if not isinstance(codes, list):
        return codes or []
    cutoff = datetime.now(timezone.utc).timestamp() - _PRUNE_DAYS * 86400
    out: list = []
    for entry in codes:
        if not isinstance(entry, dict):
            out.append(entry)
            continue
        source = entry.get("source", "watchlist")
        if source in _PRUNABLE_CODE_SOURCES:
            last_seen = entry.get("last_seen")
            if last_seen:
                try:
                    ts = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00")).timestamp()
                    if ts < cutoff:
                        log.info(
                            "state: pruning stale %s code %s (last_seen=%s)",
                            source, entry.get("code"), last_seen,
                        )
                        continue
                except (ValueError, AttributeError):
                    pass
        out.append(entry)
    return out


def _prune_processed_ids_state(state: Any, prune_days: int) -> dict:
    """Drop processed_ids whose seen_at is older than ``prune_days`` days.

    Shared by gmail_state and voice_state — both follow the same
    ``{processed_ids: {id: seen_at_iso}}`` shape.
    """
    if not isinstance(state, dict):
        return {"processed_ids": {}}
    cutoff = datetime.now(timezone.utc).timestamp() - prune_days * 86400
    pids = state.get("processed_ids") or {}
    out_pids: dict[str, str] = {}
    for eid, seen_at in pids.items():
        try:
            ts = datetime.fromisoformat(str(seen_at).replace("Z", "+00:00")).timestamp()
            if ts >= cutoff:
                out_pids[eid] = seen_at
        except (ValueError, AttributeError):
            # Unparseable timestamp — keep the entry (safer than losing dedup).
            out_pids[eid] = seen_at
    return {"processed_ids": out_pids}


def _prune_gmail_state(state: Any) -> dict:
    return _prune_processed_ids_state(state, _GMAIL_PRUNE_DAYS)


def _prune_voice_state(state: Any) -> dict:
    return _prune_processed_ids_state(state, _VOICE_PRUNE_DAYS)


def write_state(
    gist_id: str,
    token: str,
    prices: dict,
    aliases: dict,
    codes: Any,
    fx: dict | None = None,
    gmail: dict | None = None,
    voice: dict | None = None,
    sms_aliases: dict | None = None,
    signup: dict | None = None,
    wardrobe: dict | None = None,
    email_sales: list | None = None,
    body_scans: dict | None = None,
    shop_verdicts: list | None = None,
    restock: dict | None = None,
    shadow_runs: dict | None = None,
) -> None:
    """Prune stale entries, then update Gist files in one PATCH.

    ``fx``, ``gmail``, ``voice``, ``sms_aliases``, ``signup``, ``wardrobe``,
    ``email_sales``, ``body_scans``, ``shop_verdicts``, ``restock``, and
    ``shadow_runs`` are
    optional — pass None to leave the corresponding file untouched (e.g. when
    the FX fetch failed
    and the cache shouldn't be overwritten, when Gmail/Voice/signup was
    skipped/failed this run, when the wardrobe scan didn't run, when the
    BodySpec scan cache wasn't refreshed this run, or when no shadow A/B call
    was made).

    ``email_sales`` and ``shop_verdicts`` are expected already pruned by the
    caller (via ``src/email_sales.py`` / ``src/shop_verdicts.py``, which own the
    date logic) — written verbatim.
    """
    prices = _prune_prices(prices)
    codes = _prune_codes(codes)
    files = {
        "prices.json": {"content": json.dumps(prices, indent=2)},
        "shop_aliases.json": {"content": json.dumps(aliases, indent=2)},
        "codes.json": {"content": json.dumps(codes, indent=2)},
    }
    if email_sales is not None:
        files["email_sales.json"] = {"content": json.dumps(email_sales, indent=2)}
    if fx is not None:
        files["fx_rates.json"] = {"content": json.dumps(fx, indent=2)}
    if gmail is not None:
        gmail = _prune_gmail_state(gmail)
        files["gmail_state.json"] = {"content": json.dumps(gmail, indent=2)}
    if voice is not None:
        voice = _prune_voice_state(voice)
        files["voice_state.json"] = {"content": json.dumps(voice, indent=2)}
    if sms_aliases is not None:
        files["sms_aliases.json"] = {"content": json.dumps(sms_aliases, indent=2)}
    if signup is not None:
        files["signup_state.json"] = {"content": json.dumps(signup, indent=2)}
    if wardrobe is not None:
        files["wardrobe.json"] = {"content": json.dumps(wardrobe, indent=2)}
    if body_scans is not None:
        files["body_scans.json"] = {"content": json.dumps(body_scans, indent=2)}
    if shop_verdicts is not None:
        files["shop_verdicts.json"] = {"content": json.dumps(shop_verdicts, indent=2)}
    if restock is not None:
        files["restock_state.json"] = {"content": json.dumps(restock, indent=2)}
    if shadow_runs is not None:
        files["shadow_runs.json"] = {"content": json.dumps(shadow_runs, indent=2)}
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.patch(
            f"{_GIST_API}/{gist_id}",
            headers=_auth_headers(token),
            json={"files": files},
        )
    resp.raise_for_status()
