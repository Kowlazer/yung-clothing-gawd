"""Read and write state to GitHub Gist.

Gist files (13):
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
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

_GIST_API = "https://api.github.com/gists"
_GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
_PRUNE_DAYS = 30           # prices + email/sms-sourced codes
_GMAIL_PRUNE_DAYS = 14     # processed-email-id dedupe window
_VOICE_PRUNE_DAYS = 14     # processed-sms-id dedupe window
_TIMEOUT = 15.0


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", **_GITHUB_HEADERS}


def _file_content(f: dict, client: httpx.Client | None, token: str | None) -> str:
    """Full text content of one Gist file.

    GitHub truncates a file's inline ``content`` once it exceeds 1 MB and sets
    ``truncated: true`` with a ``raw_url`` pointing at the full bytes. We follow
    that so large state files read back intact rather than as truncated
    (malformed) JSON. ``wardrobe.json`` crossed 1 MB once items carry the full
    BodySpec ``body_comp`` block (see src/bodyspec.py), so without this the whole
    wardrobe would silently read back as ``{}`` — a data-loss trap for the next
    read-modify-write. The secret-gist ``raw_url`` requires the bearer token.
    """
    if f.get("truncated") and f.get("raw_url") and client is not None:
        resp = client.get(
            f["raw_url"],
            headers={"Authorization": f"Bearer {token}"} if token else None,
        )
        resp.raise_for_status()
        return resp.text
    return f.get("content") or ""


def _parse_gist_file(
    files: dict, name: str, default: Any = None,
    *, client: httpx.Client | None = None, token: str | None = None,
) -> Any:
    """Parse one file out of a Gist payload. Returns ``default`` (or ``{}``)
    when the file is missing, empty, or malformed JSON. Follows ``raw_url`` for
    files GitHub truncated (>1 MB) when a ``client``/``token`` are supplied."""
    if default is None:
        default = {}
    f = files.get(name)
    if not f:
        return default
    content = _file_content(f, client, token)
    if not content.strip():
        return default
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        log.warning("state: malformed JSON in gist file %s — treating as default", name)
        return default


def read_state(gist_id: str, token: str) -> dict:
    """Fetch the Gist and return all state blobs as a dict."""
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.get(f"{_GIST_API}/{gist_id}", headers=_auth_headers(token))
        resp.raise_for_status()
        files = resp.json().get("files", {})
        # Keep the client open: truncated (>1 MB) files are re-fetched via raw_url.
        return {
            "prices": _parse_gist_file(files, "prices.json", default={}, client=client, token=token),
            "aliases": _parse_gist_file(files, "shop_aliases.json", default={}, client=client, token=token),
            "codes": _parse_gist_file(files, "codes.json", default=[], client=client, token=token),
            "email_sales": _parse_gist_file(files, "email_sales.json", default=[], client=client, token=token),
            "fx": _parse_gist_file(files, "fx_rates.json", default={}, client=client, token=token),
            "gmail": _parse_gist_file(files, "gmail_state.json", default={}, client=client, token=token),
            "voice": _parse_gist_file(files, "voice_state.json", default={}, client=client, token=token),
            "sms_aliases": _parse_gist_file(files, "sms_aliases.json", default={}, client=client, token=token),
            "signup": _parse_gist_file(files, "signup_state.json", default={}, client=client, token=token),
            "wardrobe": _parse_gist_file(files, "wardrobe.json", default={}, client=client, token=token),
            "body_scans": _parse_gist_file(files, "body_scans.json", default={}, client=client, token=token),
            "shop_verdicts": _parse_gist_file(files, "shop_verdicts.json", default=[], client=client, token=token),
            "restock": _parse_gist_file(files, "restock_state.json", default={}, client=client, token=token),
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
) -> None:
    """Prune stale entries, then update Gist files in one PATCH.

    ``fx``, ``gmail``, ``voice``, ``sms_aliases``, ``signup``, ``wardrobe``,
    ``email_sales``, ``body_scans``, ``shop_verdicts``, and ``restock`` are
    optional — pass None to leave the corresponding file untouched (e.g. when
    the FX fetch failed
    and the cache shouldn't be overwritten, when Gmail/Voice/signup was
    skipped/failed this run, when the wardrobe scan didn't run, or when the
    BodySpec scan cache wasn't refreshed this run).

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
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.patch(
            f"{_GIST_API}/{gist_id}",
            headers=_auth_headers(token),
            json={"files": files},
        )
    resp.raise_for_status()
