"""Entry point — orchestrates the full sale-check run.

Single end-to-end pipeline:

    load config -> fetch watchlist -> classify -> harvest codes
    -> read Gist state -> resolve FX rates
    -> bucket entries (cache-first SHOP_NAME resolution; loose-mention domain lookup)
    -> ThreadPool extract Category A product pages
    -> resolve_fuzzy (Step 5 homepage check, Step 2 alias resolution, Step 6 loose match)
    -> ThreadPool extract any URLs Claude matched for loose mentions
    -> detect_sale on every extracted item
    -> build digest
    -> write Gist state (prices, aliases, codes, fx)
    -> send email

SHOP_NAME entries that aren't already in shop_aliases.json get sent to
resolve_fuzzy for resolution and persisted to the alias cache, but their
homepage sale-check is deferred to the next run (so resolve_fuzzy is only
called once per run). The result-shape lives in claude_fuzzy.py.
"""

from __future__ import annotations

import logging
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlparse

from src import bodyspec
from src import log_privacy
from src import shadow_compare
from src.claude_fuzzy import DEFAULT_MODEL as CLAUDE_DEFAULT_MODEL
from src.claude_fuzzy import resolve_fuzzy
from src.classify import Entry, classify, sales_tracking_shops
from src.codes import harvest_codes
from src import email_sales
from src import restock_emails
from src import review_requests
from src import shop_verdicts
from src.config import Config, load_config
from src.digest import build_digest, build_fit_digest
from src.email_send import send_email
from src.extract import extract
from src.fit_links import fit_url, pending_fit_items, review_all_url
from src.watchlist_links import (
    pending_removal_items,
    removal_all_url,
    removal_url,
)
from src.fx import get_rates
from src import http_util
from src.gmail import (
    extract_signals,
    fetch_promotions,
    fetch_restock_emails,
    fetch_review_requests,
)
from src.order_parse import is_excluded_shop
from src.sale_detect import PriceRules, detect_sale
from src.state import read_state, write_state
from src.voice import DEFAULT_LABEL as _VOICE_DEFAULT_LABEL
from src.voice import extract_sms_signals, fetch_voice_sms
from src.watchlist import fetch_watchlist

log = logging.getLogger(__name__)

_MAX_WORKERS = 10  # ThreadPool size for parallel product-page fetches
# Random delay between sequential requests against the same domain.
# Makes the request pattern look less bot-like to Cloudflare-protected shops.
# 0 disables (useful in tests).
_INTRA_DOMAIN_JITTER = (0.5, 1.5)  # seconds


def _is_shopify_url(url: str) -> bool:
    return "/products/" in url


# Global gate on Shopify product extracts. Each extract() fires 2-3 sub-requests
# (.json/.js/HTML) in a burst, so the gap sets the *averaged* per-IP rate against
# the platform. It used to be a flat 5 s — safe, but priced for a 429 storm and
# charged every day: with ~300 `/products/` URLs a run, that gate alone was 25 of
# the 37 minutes of the 2026-07-19 run, on a day with 14 total 429s.
#
# It is now the shared adaptive gate (``http_util.AdaptiveRateLimiter``) that
# learns *across* runs: each run is `seed()`ed at the safe 5 s ceiling (or a gap
# earned by a streak of clean runs, persisted in the Gist), a persistent throttle
# snaps it back to the ceiling, and a fully clean run shaves the *next* run's
# start down one step. This paces proactively from request #1 — the reactive
# start-at-1s version stormed on 2026-07-21/22 because the per-IP throttle trips
# before a within-run gate can react. See the note in ``src/http_util.py``.
_SHOPIFY_LIMITER = http_util.PLATFORM_LIMITER


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _homepage_url(url: str) -> str:
    """Normalize a SHOP_URL or collection URL to its bare ``scheme://netloc``."""
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        return url
    return f"{p.scheme}://{p.netloc}"


# Tokens that strongly imply the product is a pant/legging/short-style bottom.
# Matched against the URL slug and the cached product label so the per-URL
# preferred-sizes selection (PREFERRED_SIZES_PANTS) can fire. ``shorts`` is
# intentionally included — the user's bottom sizing applies to shorts too.
_PANTS_TOKEN_RE = re.compile(
    r"\b(?:pants?|trousers?|jeans|joggers?|sweatpants?|chinos?"
    r"|slacks?|leggings?|shorts?)\b",
    re.IGNORECASE,
)


def _is_pants_url(url: str, label: str | None = None) -> bool:
    """True when the URL slug or cached label looks like a bottom garment.

    URL slug is checked first because it's always available; label is a
    fallback for shops with generic slugs (Etsy ``/listing/12345``). False on
    first sighting of a generic-slug URL whose label hasn't been cached yet
    — the next run picks it up once ``prices.json`` has the label.
    """
    slug = urlparse(url).path
    if _PANTS_TOKEN_RE.search(slug):
        return True
    if label and _PANTS_TOKEN_RE.search(label):
        return True
    return False


def _preferred_sizes_for(
    cfg: Config,
    url: str,
    label: str | None = None,
) -> tuple[str, ...]:
    """Return the per-URL preferred-sizes shortlist.

    ``preferred_sizes_pants`` wins for pants-shaped URLs/labels when set;
    everything else falls back to ``preferred_sizes`` (which may itself be
    empty, in which case no size-aware OOS override applies).
    """
    if cfg.preferred_sizes_pants and _is_pants_url(url, label):
        return cfg.preferred_sizes_pants
    return cfg.preferred_sizes


def _apply_wardrobe_exclusions(text: str, wardrobe: dict | None) -> str:
    """Drop watchlist lines the user approved as 'purchased, remove from doc'.

    Source of truth is ``wardrobe.watchlist_exclusions`` (populated by
    ``order_scan._interactive_watchlist_approval``). Each entry stores the
    stripped watchlist-Doc line that the matcher caught when the order
    confirmation came in. We compare line-by-line against the live Doc and
    drop matches before ``classify()`` runs, so the daily cron stops
    extracting prices for items already in the wardrobe.

    Why filter in the cron rather than expect the user to paste-delete from
    the Doc: the print-and-paste step is easy to skip. Filtering here makes
    approval the only required action — the Doc remains the source of truth
    long-term but stale lines stop costing daily-run effort immediately.
    """
    excluded = {
        (e.get("matched_line") or "").strip()
        for e in ((wardrobe or {}).get("watchlist_exclusions") or [])
        if e.get("matched_line")
    }
    excluded.discard("")
    if not excluded:
        return text
    out: list[str] = []
    skipped = 0
    for line in text.splitlines():
        if line.strip() in excluded:
            skipped += 1
            continue
        out.append(line)
    if skipped:
        log.info(
            "filtered %d watchlist line(s) via %d wardrobe exclusion(s)",
            skipped, len(excluded),
        )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Bucketing — classify.Entry list -> the inputs each downstream step expects
# ---------------------------------------------------------------------------

def _bucket_entries(
    entries: list[Entry],
    aliases: dict[str, str],
) -> dict[str, Any]:
    """Walk classify output and bucket into downstream-step inputs.

    Returns a dict with:
        product_urls       list[(url, shop)]
        shops_to_check     list[{"shop", "url"}]      Step 5 input
        shops_to_resolve   list[str]                  Step 2 input
        loose_ready        list[{"mention", "shop", "shop_domain"}]   Step 6 input
        loose_deferred     list[{"mention", "shop"}]  shops we can't resolve yet
        non_clothing_shops list[str]  shop labels under the watchlist's
                           "Non-clothing Shops and URLs:" section (digest split)
        priority_urls      set[str]  product URLs the user marked with an inline
                           priority marker (⭐) — pinned to the digest's top
                           "Watching now" block
        untracked_urls     list[{"url", "shop", "is_clothing"}]  product URLs we
                           can't crawl (Amazon — bot wall); surfaced read-only in
                           their own digest block, never extracted or sale-checked
    """
    product_urls: list[tuple[str, str]] = []
    seen_product_urls: set[str] = set()  # global dedup — see below
    untracked_urls: list[dict[str, Any]] = []
    untracked_shop_labels: set[str] = set()  # shops whose only entries are untracked
    shops_map: dict[str, str] = {}        # shop label -> homepage URL
    shop_domain_lookup: dict[str, str] = {}  # shop label -> base URL (for loose-mention domain)

    # First pass: PRODUCT_URL and SHOP_URL entries
    for e in entries:
        if e.category == "PRODUCT_URL":
            # Dedup by URL (keep first occurrence's shop context). A product URL
            # belongs to one shop, so a duplicate — e.g. the user pasted it under
            # a shop header AND a dedicated "Priority:" section, or twice by
            # accident — would otherwise be extracted + sale-detected twice and
            # show two lines in the digest. The priority *flag* is captured
            # separately (priority_urls below ORs across all entries), so dropping
            # the duplicate here never loses the pin.
            if e.value in seen_product_urls:
                continue
            seen_product_urls.add(e.value)
            product_urls.append((e.value, e.context))
            # Many watchlists list "ShopName:" (→ SHOP_NAME) followed directly
            # by product URLs (no bare-domain SHOP_URL entry). Derive the
            # homepage from the product URL so the shop is checked for sales
            # and doesn't get pushed into resolve_fuzzy's DDG queue.
            label = (e.context or "").strip()
            if label:
                home = _homepage_url(e.value)
                shops_map.setdefault(label, home)
                shop_domain_lookup.setdefault(label, home)
        elif e.category == "SHOP_URL":
            label = (e.context or urlparse(e.value).netloc).strip()
            if not label:
                continue
            home = _homepage_url(e.value)
            shops_map.setdefault(label, home)
            shop_domain_lookup.setdefault(label, home)
        elif e.category == "UNTRACKED_URL":
            # Can't be crawled (Amazon) — list it read-only, don't extract it
            # and don't seed shops_map (no pointless amazon.com homepage check).
            untracked_urls.append(
                {"url": e.value, "shop": e.context, "is_clothing": e.is_clothing}
            )
            label = (e.context or "").strip()
            if label:
                untracked_shop_labels.add(label)

    # SHOP_NAME entries — cached aliases go to shops_to_check; rest queue for resolve.
    shop_names: list[str] = []
    seen_names: set[str] = set()
    for e in entries:
        if e.category == "SHOP_NAME" and e.value not in seen_names:
            seen_names.add(e.value)
            shop_names.append(e.value)

    uncached_names: list[str] = []
    for name in shop_names:
        if name in shops_map:
            # Already known from a SHOP_URL entry — no need to resolve again.
            continue
        if name in untracked_shop_labels:
            # A header (e.g. "Amazon:") whose only children are untracked URLs.
            # It's a known shop we deliberately don't crawl — skip the DDG/Claude
            # resolve so it doesn't surface as "could not resolve".
            continue
        cached = aliases.get(name)
        if cached:
            shops_map.setdefault(name, _homepage_url(cached))
            shop_domain_lookup.setdefault(name, cached)
        else:
            uncached_names.append(name)

    # LOOSE_MENTION entries — split into ready (domain known) and deferred.
    loose_ready: list[dict[str, str]] = []
    loose_deferred: list[dict[str, str]] = []
    for e in entries:
        if e.category != "LOOSE_MENTION":
            continue
        shop = (e.context or "").strip()
        if not shop:
            continue
        domain = shop_domain_lookup.get(shop) or aliases.get(shop)
        if domain:
            loose_ready.append(
                {"mention": e.value, "shop": shop, "shop_domain": domain}
            )
            shops_map.setdefault(shop, _homepage_url(domain))
        else:
            if shop not in seen_names:
                seen_names.add(shop)
                uncached_names.append(shop)
            loose_deferred.append({"mention": e.value, "shop": shop})

    # Shop labels that live wholly under the "Non-clothing Shops and URLs:"
    # watchlist section. A shop counts as non-clothing only when it has *no*
    # clothing entries (clothing wins on the rare dual-section shop), so its
    # items + homepage sale status break out into the digest's non-clothing
    # block instead of mixing with the clothing sections. Derived at the shop
    # level (not per-item) because loose mentions round-trip through Claude and
    # lose their per-entry is_clothing flag — but their shop label survives.
    clothing_shops: set[str] = set()
    nonclothing_shops: set[str] = set()
    for e in entries:
        # Mirror the shop-label each category actually lands under above, so the
        # set keys match the labels carried by items / shop_sales: SHOP_NAME uses
        # its value, a bare SHOP_URL falls back to its netloc (as the SHOP_URL
        # pass does), everything else uses the shop context.
        if e.category == "SHOP_NAME":
            label = e.value
        elif e.category == "SHOP_URL":
            label = e.context or urlparse(e.value).netloc
        else:
            label = e.context
        label = (label or "").strip()
        if not label:
            continue
        (clothing_shops if e.is_clothing else nonclothing_shops).add(label)
    non_clothing_shops = sorted(nonclothing_shops - clothing_shops, key=str.lower)

    # Product URLs the user flagged with an inline priority marker (⭐). Scoped to
    # PRODUCT_URLs — those carry a single price/stock the "Watching now" block can
    # show; a shop homepage doesn't. A set so a URL marked twice still counts once.
    priority_urls = {
        e.value for e in entries
        if e.category == "PRODUCT_URL" and e.priority
    }

    shops_to_check = [{"shop": s, "url": u} for s, u in shops_map.items()]
    return {
        "product_urls": product_urls,
        "shops_to_check": shops_to_check,
        "shops_to_resolve": uncached_names,
        "loose_ready": loose_ready,
        "loose_deferred": loose_deferred,
        "non_clothing_shops": non_clothing_shops,
        "priority_urls": priority_urls,
        "untracked_urls": untracked_urls,
    }


# ---------------------------------------------------------------------------
# Concurrent extraction
# ---------------------------------------------------------------------------

def _error_skeleton(exc: Exception) -> dict:
    return {
        "current_price": None,
        "original_price": None,
        "currency": None,
        "on_sale": False,
        "out_of_stock": False,
        "low_stock": False,
        "label": None,
        "total_variant_count": None,
        "available_variant_count": None,
        "color_options": [],
        "size_options": [],
        "available_sizes": [],
        "unpreferred_available_sizes": [],
        "preferred_sizes_applied": [],
        "error": f"unhandled: {exc}",
        "error_kind": "other",
    }


def _extract_many(
    urls: list[str],
    *,
    max_workers: int = _MAX_WORKERS,
    extract_fn=None,
    jitter: tuple[float, float] | None = _INTRA_DOMAIN_JITTER,
    preferred_sizes: tuple[str, ...] | Callable[[str], tuple[str, ...]] = (),
) -> dict[str, dict]:
    """Run ``extract_fn`` over ``urls`` in parallel, **serialized per domain**.

    Each domain gets its own worker that processes its URLs one at a time,
    with a random ``jitter`` delay between requests (looks less bot-like to
    Cloudflare). Different domains run concurrently up to ``max_workers``.

    ``extract_fn`` is injectable so tests can swap in a deterministic stub.
    When None, looks up ``src.main.extract`` at call time so monkeypatching
    the module-level binding works in tests. Per-URL failures are coerced to
    an ``error_kind='other'`` result rather than aborting the batch.

    ``preferred_sizes`` can be either a single tuple (applied to every URL)
    or a callable ``(url) -> tuple`` for per-URL selection — used to switch
    between top sizing and pants sizing based on the URL slug + cached label.

    Pass ``jitter=None`` (or ``(0, 0)``) in tests to skip the delay.
    """
    if not urls:
        return {}
    fn = extract_fn or extract

    by_domain: dict[str, list[str]] = {}
    for url in urls:
        domain = urlparse(url).netloc or url
        by_domain.setdefault(domain, []).append(url)

    # Resolve per-URL preferred sizes lazily. Keeps test stubs with a
    # ``def fake(url):`` signature working unchanged when the user never set
    # PREFERRED_SIZES (callable returns () or static tuple is empty).
    if callable(preferred_sizes):
        def _sizes_for(url: str) -> tuple[str, ...]:
            return preferred_sizes(url)
    else:
        sizes_tuple = preferred_sizes
        def _sizes_for(url: str) -> tuple[str, ...]:
            return sizes_tuple

    def _call_fn(url: str) -> dict:
        sizes = _sizes_for(url)
        if sizes:
            return fn(url, preferred_sizes=sizes)
        return fn(url)

    def _run_domain(domain_urls: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i, url in enumerate(domain_urls):
            if i > 0 and jitter and jitter[1] > 0:
                time.sleep(random.uniform(*jitter))
            # Shopify hosts many independent stores on shared infrastructure;
            # concurrent requests from one IP across all of them trigger a
            # platform-level 429. Serialise Shopify requests globally with a
            # minimum inter-request gap. Gated on the same "delays enabled"
            # signal as the jitter sleep above (jitter set and non-zero), so
            # tests passing jitter=None or (0, 0) skip all real sleeps.
            if jitter and jitter[1] > 0 and _is_shopify_url(url):
                _SHOPIFY_LIMITER.acquire()
            try:
                out[url] = _call_fn(url)
            except Exception as exc:  # noqa: BLE001 — never let one URL kill the batch
                log.warning("extract %s raised %s: %s", url, type(exc).__name__, exc)
                out[url] = _error_skeleton(exc)
        return out

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_run_domain, group) for group in by_domain.values()]
        for fut in as_completed(futures):
            results.update(fut.result())
    return results


# ---------------------------------------------------------------------------
# State + result aggregation
# ---------------------------------------------------------------------------

def _apply_detect(
    items_out: list[dict],
    prices: dict,
    url: str,
    shop: str | None,
    extracted: dict,
    *,
    is_uncertain: bool,
    rules: PriceRules | None = None,
    priority: bool = False,
) -> None:
    result = detect_sale(url, extracted, prices.get(url, {}), rules=rules)
    items_out.append({
        "url": url,
        "shop": shop,
        "is_uncertain": is_uncertain,
        "priority": priority,
        "result": result,
    })
    if result.get("updated_entry") is not None:
        prices[url] = result["updated_entry"]
    else:
        prices.pop(url, None)


def _merge_aliases(aliases: dict[str, str], resolutions: list[dict]) -> dict[str, str]:
    """Add newly resolved shop names to the alias cache.

    Only high/low-confidence resolutions are persisted. ``none`` and null-url
    answers are surfaced via the digest's "Could not resolve" section instead.
    """
    out = dict(aliases)
    for r in resolutions:
        url = r.get("url")
        if url and r.get("confidence") in ("high", "low"):
            out[r["shop_name"]] = url
    return out


def _strip_email_sale_shops(
    shop_sales: list[dict], active_email_sales: list[dict], today: date,
) -> list[dict]:
    """Drop homepage ``shop_sales`` entries for shops with an *ongoing* email sale.

    Email-derived sales render in their own dated digest section
    (``digest._email_sales_section``). A shop whose email sale is *ongoing*
    (active now) is shown *only* there — otherwise the same shop could appear
    contradictorily as "no sale" (homepage said no) and "on sale" (email said
    yes) in the same digest.

    A shop whose email sale is merely *upcoming* (hasn't started yet) keeps its
    homepage entry: the user wants to see both the current homepage status
    ("on sale now" / "no sale now") *and* the upcoming countdown, and there's no
    contradiction between "no sale now" and "sale starts Friday". Homepage-only
    shops (no email announcement) are untouched.
    """
    ongoing_shops = {
        (e.get("shop") or "").strip().lower()
        for e in active_email_sales or []
        if e.get("shop")
        and email_sales.relative_days(e, today)[0] != "upcoming"
    }
    if not ongoing_shops:
        return list(shop_sales)
    return [
        s for s in shop_sales
        if (s.get("shop") or "").strip().lower() not in ongoing_shops
    ]


def _drop_ongoing_email_sale_shops(
    shops_to_check: list[dict], prior_email_sales: list[dict], today: date,
) -> list[dict]:
    """Skip homepage sale-checks for shops with an *ongoing* email sale (cost lever #2).

    ``_strip_email_sale_shops`` already discards the homepage ``shop_sales``
    entry for any shop whose email sale is active *now* (it renders solely in
    the email-sales digest section) — so checking those homepages means paying
    Claude to judge a page whose verdict we then throw away. Drop them from the
    homepage queue up front instead.

    Keyed on the **prior** persisted store, not this run's fresh judgements:
    those email judgements come *out of* ``resolve_fuzzy``, so at filter time we
    only know the multi-day / advance sales carried over from earlier runs —
    which is exactly where the redundancy accumulates. A sale first surfaced via
    email *this* run still gets its homepage checked once (unavoidable, and no
    worse than today). ``upcoming`` email sales keep their homepage entry (the
    user wants the current homepage status alongside the countdown), mirroring
    ``_strip_email_sale_shops`` precisely.
    """
    ongoing_shops = {
        (e.get("shop") or "").strip().lower()
        for e in email_sales.active(prior_email_sales, today)
        if e.get("shop")
        and email_sales.relative_days(e, today)[0] != "upcoming"
    }
    if not ongoing_shops:
        return list(shops_to_check)
    return [
        s for s in shops_to_check
        if (s.get("shop") or "").strip().lower() not in ongoing_shops
    ]


def _merge_codes(
    prior_codes: list[dict],
    watchlist_raw: list[dict],
    email_codes: list[dict],
    now_iso: str,
) -> list[dict]:
    """Build the final codes list for the digest and state write.

    Watchlist codes are rebuilt fresh from the watchlist each run (the doc is
    the source of truth) and stamped with source="watchlist" + timestamps.
    Email codes are *merged* with any prior email entries: if the same
    ``(shop, code)`` was seen before, ``first_seen`` is preserved and
    ``last_seen`` is bumped. Prior email codes not re-seen this run are
    carried over untouched — state.py prunes them after _PRUNE_DAYS.
    """
    watchlist = [
        {
            "shop": c.get("shop", ""),
            "code": c.get("code", ""),
            "context": c.get("context", ""),
            "confidence": c.get("confidence", "medium"),
            "source": "watchlist",
            "first_seen": now_iso,
            "last_seen": now_iso,
        }
        for c in watchlist_raw
    ]

    prior_email_by_key: dict[tuple[str, str], dict] = {}
    for c in prior_codes or []:
        if not isinstance(c, dict):
            continue
        if c.get("source") in ("email", "email_unattributed"):
            key = (c.get("shop", ""), c.get("code", ""))
            prior_email_by_key[key] = c

    merged_email: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    for c in email_codes or []:
        key = (c.get("shop", ""), c.get("code", ""))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        prior = prior_email_by_key.get(key)
        if prior:
            merged = {**prior, "last_seen": c.get("last_seen", now_iso)}
            # Refresh context + email_id + confidence with the most recent
            # sighting. Confidence in particular needs to overwrite so legacy
            # entries from before the rating feature pick up a fresh tag the
            # next time they're seen.
            if c.get("context"):
                merged["context"] = c["context"]
            if c.get("email_id"):
                merged["email_id"] = c["email_id"]
            if c.get("confidence"):
                merged["confidence"] = c["confidence"]
            merged_email.append(merged)
        else:
            merged_email.append(c)

    # Carry over prior email codes we didn't re-see — state.py prunes by age.
    for key, prior in prior_email_by_key.items():
        if key not in seen_keys:
            merged_email.append(prior)

    return watchlist + merged_email


def _voice_pipeline(
    cfg: Config,
    sms_aliases: dict[str, str],
    known_shops: list[str],
    prior_voice_state: dict,
    *,
    now_iso: str,
    label: str = _VOICE_DEFAULT_LABEL,
) -> dict:
    """Run the full Google-Voice SMS step. Mirrors ``_gmail_pipeline``.

    Returns extract_sms_signals output plus the updated voice_state dict to
    persist. Failure-isolated: any exception is logged and an empty-result
    skeleton is returned so a GV/IMAP outage never blocks the rest of the
    run. SMS sale_signals share the email shape and are merged into the
    gmail signals list by the caller before resolve_fuzzy.
    """
    empty = {
        "codes": [],
        "unattributed": [],
        "sale_signals": [],
        "untracked_senders": [],
        "voice_state_out": prior_voice_state or {"processed_ids": {}},
    }
    try:
        prior_pids = (prior_voice_state or {}).get("processed_ids") or {}
        sms_list = fetch_voice_sms(
            cfg.gmail_username,
            cfg.gmail_app_password,
            label=label,
            skip_ids=set(prior_pids.keys()),
        )
        signals = extract_sms_signals(sms_list, sms_aliases, known_shops)
    except Exception:  # noqa: BLE001 — degrade voice failures, never block the run
        log.exception("voice: step failed, continuing without SMS signals")
        return empty

    new_pids = dict(prior_pids)
    for eid in signals.get("processed_ids", []):
        new_pids[eid] = now_iso
    return {
        "codes": signals.get("codes", []),
        "unattributed": signals.get("unattributed", []),
        "sale_signals": signals.get("sale_signals", []),
        "untracked_senders": signals.get("untracked_senders", []),
        "voice_state_out": {"processed_ids": new_pids},
    }


def _gmail_pipeline(
    cfg: Config,
    aliases: dict[str, str],
    known_shops: list[str],
    prior_gmail_state: dict,
    *,
    now_iso: str,
) -> dict:
    """Run the full Gmail step. Returns extract_signals output plus the
    updated gmail_state dict to persist. Failure-isolated: any exception is
    logged and the function returns an empty-result skeleton so a Gmail
    outage never blocks the rest of the run."""
    empty = {
        "codes": [],
        "unattributed": [],
        "sale_signals": [],
        "gmail_state_out": prior_gmail_state or {"processed_ids": {}},
    }
    try:
        prior_pids = (prior_gmail_state or {}).get("processed_ids") or {}
        emails = fetch_promotions(
            cfg.gmail_username,
            cfg.gmail_app_password,
            skip_ids=set(prior_pids.keys()),
        )
        signals = extract_signals(emails, aliases, known_shops)
    except Exception:  # noqa: BLE001 — degrade Gmail failures, never block the run
        log.exception("gmail: step failed, continuing without email signals")
        return empty

    new_pids = dict(prior_pids)
    for eid in signals.get("processed_ids", []):
        new_pids[eid] = now_iso
    return {
        "codes": signals.get("codes", []),
        "unattributed": signals.get("unattributed", []),
        "sale_signals": signals.get("sale_signals", []),
        "gmail_state_out": {"processed_ids": new_pids},
    }


def _review_requests_pipeline(
    cfg: Config, *, now: datetime,
) -> tuple[list[dict], str | None]:
    """Fetch + dedupe recent review-request emails for the digest section.

    Returns ``(render_list, all_time_url)``. Failure-isolated like
    ``_gmail_pipeline``: any error logs and returns ``([], None)`` so a Gmail
    hiccup never blocks the run. Also returns ``([], None)`` when the daily
    toggle is off. Stateless — no Gist read/write, no dedup-skip; the whole
    recent window is re-fetched every run (see src/review_requests.py)."""
    if not cfg.review_requests_daily:
        return [], None
    try:
        emails = fetch_review_requests(
            cfg.gmail_username,
            cfg.gmail_app_password,
            days=cfg.review_requests_days,
        )
        requests = review_requests.dedupe(emails, now=now)
    except Exception:  # noqa: BLE001 — degrade like the other Gmail steps
        log.exception("review_requests: step failed, continuing without the section")
        return [], None
    return requests, review_requests.all_requests_url()


def _restock_emails_pipeline(
    cfg: Config, *, now: datetime,
) -> tuple[list[dict], str | None]:
    """Fetch + dedupe recent back-in-stock emails for the digest section.

    Returns ``(render_list, all_time_url)``. Failure-isolated and stateless like
    ``_review_requests_pipeline``: any error logs and returns ``([], None)``, and
    so does the daily-toggle-off case. The render list is merged into the digest's
    "Back in stock" section (tagged as email alerts)."""
    if not cfg.restock_emails_daily:
        return [], None
    try:
        emails = fetch_restock_emails(
            cfg.gmail_username,
            cfg.gmail_app_password,
            days=cfg.restock_email_days,
        )
        restocks = restock_emails.dedupe(emails, now=now)
    except Exception:  # noqa: BLE001 — degrade like the other Gmail steps
        log.exception("restock_emails: step failed, continuing without the section")
        return [], None
    return restocks, restock_emails.all_restocks_url()


def _collect_unresolved(fuzzy: dict, deferred_shops: list[str]) -> list[str]:
    out = list(fuzzy.get("unresolved", []))
    for r in fuzzy.get("resolutions", []):
        if not r.get("url") or r.get("confidence") == "none":
            out.append(r["shop_name"])
    # Shops referenced only via deferred loose mentions whose names we did try
    # to resolve are already captured above. ``deferred_shops`` is informational.
    return sorted(set(out), key=str.lower)


# ---------------------------------------------------------------------------
# Subject line
# ---------------------------------------------------------------------------

def _digest_subject(
    shop_sales: list[dict],
    items: list[dict],
    active_email_sales: list[dict] | None = None,
    *,
    today: datetime | None = None,
) -> str:
    today = today or datetime.now(timezone.utc)
    today_date = today.date() if isinstance(today, datetime) else today
    # Shops on sale *now*: homepage "yes" entries plus active email
    # announcements that have already started (ongoing). Upcoming email sales
    # are counted separately so the "on sale" tally stays truthful.
    on_sale_now = {
        (s.get("shop") or "").strip().lower()
        for s in shop_sales if s.get("status") == "yes" and s.get("shop")
    }
    upcoming = set()
    for e in active_email_sales or []:
        shop = (e.get("shop") or "").strip().lower()
        if not shop:
            continue
        if email_sales.relative_days(e, today_date)[0] == "upcoming":
            upcoming.add(shop)
        else:
            on_sale_now.add(shop)
    # A shop that's both on sale now and has a separate upcoming sale counts
    # only toward "on sale" — avoid double-counting one shop in both tallies.
    upcoming -= on_sale_now
    n_shops = len(on_sale_now)
    m_items = sum(
        1 for i in items
        if not i.get("is_uncertain")
        and i["result"].get("sale_signal") in ("on_sale_per_page", "price_dropped")
    )
    k_uncertain = sum(1 for i in items if i.get("is_uncertain"))
    parts = [
        f"{n_shops} shops on sale",
        f"{m_items} items on sale",
        f"{k_uncertain} uncertain",
    ]
    if upcoming:
        parts.append(f"{len(upcoming)} upcoming")
    return f"Sale check — {today.strftime('%b %d')} — " + ", ".join(parts)


# ---------------------------------------------------------------------------
# Fit feedback (emailed web-form nudge)
# ---------------------------------------------------------------------------

def _wardrobe_items(wardrobe: dict | None, cfg: Config) -> list[dict]:
    """Wardrobe items with EXCLUDED_SHOPS purchases filtered out.

    The order scanner hard-deletes excluded items, but the daily cron never
    writes the wardrobe — so until the next ``order_scan`` run an excluded
    purchase could still be in the file. This read-time filter guarantees one
    can never surface in a daily-digest nudge (fit feedback / removal)."""
    items = (wardrobe or {}).get("items") or []
    if not cfg.excluded_shops:
        return items
    return [
        it for it in items
        if not is_excluded_shop(it.get("shop"), it.get("shop_domain"), cfg.excluded_shops)
    ]


def _fit_feedback_data(
    wardrobe: dict | None, cfg: Config,
) -> tuple[list[dict], str | None]:
    """Build the render list + review-all link for wardrobe items needing a fit
    review.

    Returns ``([], None)`` — feature dormant — whenever the form isn't
    configured (no ``FIT_FORM_BASE_URL`` / ``FIT_LINK_SECRET``) or nothing is
    pending. Each render dict carries a signed per-item ``url``; the secret never
    leaves this layer (digest.py only sees the finished links).
    """
    if not (cfg.fit_form_base_url and cfg.fit_link_secret):
        return [], None
    pending = pending_fit_items(_wardrobe_items(wardrobe, cfg))
    if not pending:
        return [], None
    # Newest purchases first: the daily digest renders only a capped slice, so
    # surfacing the most recent buys there matters. ISO ``purchased_at`` strings
    # sort lexically = chronologically; items missing a date sort last.
    pending = sorted(
        pending, key=lambda it: it.get("purchased_at") or "", reverse=True,
    )
    render = [
        {
            "name": it.get("item_name"),
            "shop": it.get("shop"),
            "size": it.get("size"),
            "color": it.get("color"),
            "url": fit_url(it["id"], cfg.fit_form_base_url, cfg.fit_link_secret),
        }
        for it in pending
        if it.get("id")
    ]
    return render, review_all_url(cfg.fit_form_base_url, cfg.fit_link_secret)


def _drop_removed_doc_lines(
    pending: list[dict], watchlist_text: str,
) -> list[dict]:
    """Drop removal candidates whose matched Doc line is already gone.

    The nudge exists for one purpose: get a line deleted off the watchlist Doc.
    Once the line isn't there any more there is nothing left to approve, and the
    approve-link can only ever report "line not found" — so a candidate in that
    state is pure noise that repeats in every digest forever.

    Lines vanish without the item's own ``approved_for_removal`` ever being set
    two ways: the user edits the Doc by hand, or *another* wardrobe item matched
    the same line and its approval deleted it (one Doc URL routinely matches two
    purchases from that shop). The Apps Script side now resolves those siblings
    at approval time, but this read-time filter is what makes it self-healing —
    it needs no write, so it also clears items stranded before that fix and any
    hand-edit of the Doc.

    A blank ``watchlist_text`` (fetch failed / empty Doc) skips the filter
    entirely: no evidence is not evidence that every line is gone.
    """
    lines = {ln.strip() for ln in (watchlist_text or "").splitlines() if ln.strip()}
    if not lines:
        return pending

    def _still_listed(item: dict) -> bool:
        line = ((item.get("watchlist_match") or {}).get("matched_line") or "").strip()
        # No stored line to look for — keep it rather than guess.
        return not line or line in lines

    kept = [it for it in pending if _still_listed(it)]
    if len(kept) != len(pending):
        log.info(
            "removal nudge: dropped %d candidate(s) whose Doc line is already gone",
            len(pending) - len(kept),
        )
    return kept


def _watchlist_removal_data(
    wardrobe: dict | None, cfg: Config, watchlist_text: str = "",
) -> tuple[list[dict], str | None]:
    """Build the render list + review-all link for purchased items still listed
    on the watchlist Doc (pending a remove-from-Doc decision).

    Mirrors ``_fit_feedback_data``: returns ``([], None)`` — feature dormant —
    whenever the web form isn't configured (no ``FIT_FORM_BASE_URL`` /
    ``FIT_LINK_SECRET``, both reused) or nothing is pending. Each render dict
    carries the matched Doc line (so the digest can show *how the item is listed*)
    plus a signed per-item ``url``; the secret never leaves this layer (digest.py
    only sees the finished links).

    ``watchlist_text`` is the **raw** Doc text (before the exclusion filter), used
    to drop candidates whose line is already gone — see ``_drop_removed_doc_lines``.
    """
    if not (cfg.fit_form_base_url and cfg.fit_link_secret):
        return [], None
    pending = _drop_removed_doc_lines(
        pending_removal_items(_wardrobe_items(wardrobe, cfg)), watchlist_text,
    )
    if not pending:
        return [], None
    # Newest purchases first — the daily digest renders only a capped slice, so
    # the most recent buys should surface there. ISO ``purchased_at`` sorts
    # lexically = chronologically; items missing a date sort last.
    pending = sorted(
        pending, key=lambda it: it.get("purchased_at") or "", reverse=True,
    )
    render = [
        {
            "name": it.get("item_name"),
            "shop": it.get("shop"),
            "size": it.get("size"),
            "color": it.get("color"),
            "matched_line": (it.get("watchlist_match") or {}).get("matched_line"),
            "url": removal_url(it["id"], cfg.fit_form_base_url, cfg.fit_link_secret),
        }
        for it in pending
        if it.get("id")
    ]
    return render, removal_all_url(cfg.fit_form_base_url, cfg.fit_link_secret)


def _maybe_send_weekly_fit_email(
    cfg: Config, pending: list[dict], review_all_url: str | None, dry_run: bool,
) -> None:
    """Send the standalone weekly fit-feedback email when it's due.

    Gated on: the weekly toggle, items actually pending, and today (UTC) being
    the configured weekday. A no-op otherwise — so it can run unconditionally
    on every daily pass. Dry runs write ``fit_digest.md`` instead of sending.
    """
    if not cfg.fit_feedback_weekly or not pending:
        return
    today = datetime.now(timezone.utc).strftime("%a").lower()
    if today != cfg.fit_feedback_weekly_day:
        return
    body = build_fit_digest(pending, review_all_url)
    if not body:
        return
    subject = f"Fit feedback — {len(pending)} item(s) waiting"
    if dry_run:
        log.info("DRY_RUN: skipping weekly fit email (subject: %s)", subject)
        try:
            with open("fit_digest.md", "w", encoding="utf-8") as f:
                f.write(f"<!-- {subject} -->\n\n{body}\n")
        except OSError as exc:
            log.warning("could not write fit_digest.md: %s", exc)
        return
    log.info("sending weekly fit email: %s", subject)
    send_email(cfg.resend_api_key, cfg.from_email, cfg.to_email, subject, body)


def _body_scans_stale(body_scans: dict | None, max_age_days: int) -> bool:
    """True when the cached BodySpec scans are due for a refresh: no cache yet
    (missing / undated) or last refreshed more than ``max_age_days`` ago.

    Gated purely on ``refreshed_at`` age — NOT on whether ``scans`` is empty.
    An account with genuinely zero DEXA scans still gets a written cache
    (``{refreshed_at, scans: []}``); treating that as "missing" would re-pull it
    every single day and defeat the weekly cadence.
    """
    raw = (body_scans or {}).get("refreshed_at")
    if not raw:
        return True  # never refreshed → bootstrap pull
    try:
        refreshed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    if refreshed.tzinfo is None:
        refreshed = refreshed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - refreshed >= timedelta(days=max_age_days)


def _maybe_refresh_body_scans(
    cfg: Config, body_scans: dict | None, dry_run: bool,
) -> dict | None:
    """Refresh the cached BodySpec scans (``body_scans.json``) when stale.

    Returns a fresh cache dict to persist, or ``None`` to leave the file
    untouched. Gated on: BodySpec creds present, and the existing cache being
    missing/empty (bootstrap) or older than ``cfg.body_scan_max_age_days``.
    Age-gated rather than weekday-gated so a missed cron day self-heals.

    The whole BodySpec round-trip is failure-isolated (same discipline as the
    Gmail/Voice blocks) — a transient auth/network error logs and returns
    ``None`` so a BodySpec hiccup never blocks the sales digest. The cron is the
    only weekly puller, so the fit-feedback web form and CLI backfill can match
    body state from this cache without ever re-hitting BodySpec themselves.
    """
    if not (cfg.bodyspec_username and cfg.bodyspec_password):
        return None  # feature dormant unless creds are configured
    if dry_run:
        log.info("DRY_RUN: skipping BodySpec scan-cache refresh")
        return None
    if not _body_scans_stale(body_scans, cfg.body_scan_max_age_days):
        log.info("body-scan cache fresh (< %dd) — skipping refresh",
                 cfg.body_scan_max_age_days)
        return None
    try:
        token = bodyspec.authenticate(cfg.bodyspec_username, cfg.bodyspec_password)
        cache = bodyspec.build_scan_cache(token)
    except Exception as exc:  # noqa: BLE001 — failure-isolated like Gmail/Voice
        log.warning("body-scan cache refresh failed (leaving cache as-is): %s", exc)
        return None
    log.info("refreshed body-scan cache: %d scan(s)", len(cache.get("scans") or []))
    return cache


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(cfg: Config | None = None) -> str:
    """Execute one full sale-check pass. Returns the digest markdown.

    Side effects: PATCHes the Gist, POSTs to Resend, calls the Anthropic API.
    """
    cfg = cfg or load_config()

    # Read state first so the watchlist text can be filtered against
    # wardrobe-approved removals before classification. fetch_watchlist
    # and read_state are independent — just swapped order to make the
    # exclusion filter possible.
    log.info("reading state from gist")
    state = read_state(cfg.gist_id, cfg.github_token)
    prices = dict(state.get("prices") or {})
    aliases = dict(state.get("aliases") or {})
    fx_cache = state.get("fx") or {}
    prior_codes = state.get("codes") or []
    prior_email_sales = state.get("email_sales") or []
    prior_verdicts = state.get("shop_verdicts") or []
    prior_shadow_runs = state.get("shadow_runs") or {}
    prior_gmail = state.get("gmail") or {}
    prior_voice = state.get("voice") or {}
    sms_aliases = dict(state.get("sms_aliases") or {})
    wardrobe = state.get("wardrobe") or {}
    body_scans = state.get("body_scans") or {}
    prior_throttle = state.get("throttle") or {}

    # Seed the Shopify pacing gate from what prior runs learned (the fix for the
    # 2026-07-21/22 per-IP rate-limit storm — see src/http_util.py). A clean run
    # persists a slightly narrower start; a storm persists the ceiling. Absent
    # state (first run after deploy) -> start at the proven-safe ceiling.
    _persisted_interval = prior_throttle.get("shopify_gate_interval")
    _SHOPIFY_LIMITER.seed(
        _persisted_interval
        if _persisted_interval is not None
        else http_util._ADAPT_MAX_INTERVAL
    )
    log.info(
        "seeded Shopify platform gate at %.2fs (persisted=%s, last_run_stormed=%s)",
        _SHOPIFY_LIMITER.interval,
        _persisted_interval,
        prior_throttle.get("last_run_stormed"),
    )

    log.info("fetching watchlist")
    # Keep the unfiltered Doc text: the removal nudge needs to know which lines
    # are *actually* still on the Doc, which the exclusion-filtered copy can't say.
    doc_text = fetch_watchlist(cfg.watchlist_url)
    text = _apply_wardrobe_exclusions(doc_text, wardrobe)
    entries = classify(text)
    # "Shops to track sales for:" Doc section — the SMS/email sale attribution
    # allowlist, managed in the Doc (unioned with the SMS_SALE_SHOPS env var).
    doc_sale_shops = sales_tracking_shops(text)
    watchlist_codes_raw = harvest_codes(text)
    log.info(
        "classified %d entries (%d product / %d shop-url / %d shop-name / %d loose)",
        len(entries),
        sum(1 for e in entries if e.category == "PRODUCT_URL"),
        sum(1 for e in entries if e.category == "SHOP_URL"),
        sum(1 for e in entries if e.category == "SHOP_NAME"),
        sum(1 for e in entries if e.category == "LOOSE_MENTION"),
    )

    fx_rates, fx_cache_out = get_rates(fx_cache)

    buckets = _bucket_entries(entries, aliases)

    # --- Gmail (issue #9) — failure-isolated -----------------------------
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    today = now.date()
    # Sale-announcement attribution covers watchlist shops PLUS the sale-tracking
    # allowlist — shops the user gets marketing from but hasn't watchlisted, so no
    # homepage price check runs for them (classify() strips the section before it
    # can become a SHOP_NAME entry); only their *announced* sales are surfaced.
    # The allowlist is the Doc's "Shops to track sales for:" section
    # (doc_sale_shops) unioned with the legacy SMS_SALE_SHOPS env var. BOTH
    # channels use the same set so a tracked shop's sale is caught on whichever
    # one it announces — a promo email OR a Google-Voice-forwarded text (an
    # allowlist shop is matched by its name appearing in the email subject / SMS
    # body, since it has no homepage domain in shop_aliases to match on).
    known_shops = sorted({
        *(s["shop"] for s in buckets["shops_to_check"] if s.get("shop")),
        *cfg.sms_sale_shops,
        *doc_sale_shops,
    })
    gmail_result = _gmail_pipeline(
        cfg, aliases, known_shops, prior_gmail, now_iso=now_iso,
    )
    log.info(
        "gmail: %d attributed codes, %d unattributed, %d sale signals",
        len(gmail_result["codes"]),
        len(gmail_result["unattributed"]),
        len(gmail_result["sale_signals"]),
    )

    # --- Voice (Google Voice SMS forwards) — failure-isolated -----------
    voice_label = os.environ.get("VOICE_GMAIL_LABEL", _VOICE_DEFAULT_LABEL)
    voice_result = _voice_pipeline(
        cfg, sms_aliases, known_shops, prior_voice,
        now_iso=now_iso, label=voice_label,
    )
    log.info(
        "voice: %d attributed codes, %d unattributed, %d sale signals",
        len(voice_result["codes"]),
        len(voice_result["unattributed"]),
        len(voice_result["sale_signals"]),
    )

    log.info(
        "extracting %d product URLs across %d shops",
        len(buckets["product_urls"]),
        len(buckets["shops_to_check"]),
    )

    def _sizes_for_url(url: str) -> tuple[str, ...]:
        label = (prices.get(url) or {}).get("label")
        return _preferred_sizes_for(cfg, url, label)

    extracted = _extract_many(
        [u for u, _ in buckets["product_urls"]],
        preferred_sizes=_sizes_for_url,
    )
    # Where the adaptive gate ended up + what next run will start at — the tuning
    # signal for _ADAPT_* in src/http_util.py. stormed=True means a host
    # persistently 429'd us, so next run seeds at the ceiling; a clean run seeds
    # one decay step lower. If stormed stays True day after day the floor/decay
    # aren't the lever — the runner IP is throttled at the source (see the plan's
    # fallback: egress change).
    log.info(
        "platform gate finished the item scan at %.2fs (floor %.2fs, ceiling %.2fs);"
        " next run seeds at %.2fs (stormed=%s)",
        _SHOPIFY_LIMITER.interval,
        http_util._ADAPT_MIN_INTERVAL,
        http_util._ADAPT_MAX_INTERVAL,
        _SHOPIFY_LIMITER.next_interval,
        _SHOPIFY_LIMITER.stormed,
    )

    # SMS sale_signals share the email signal shape (same {email_id, shop,
    # subject, body_excerpt} keys), so they ride the same resolve_fuzzy queue
    # without prompt changes — Claude makes a per-signal sale judgement
    # regardless of whether the underlying message was an email or an SMS.
    combined_signals = gmail_result["sale_signals"] + voice_result["sale_signals"]

    # Cost lever #2: don't re-check homepages for shops with an ongoing email
    # sale — their homepage verdict is discarded by _strip_email_sale_shops
    # anyway (they render only in the email-sales section). known_shops above is
    # built from the *full* set so Gmail/Voice attribution is unaffected.
    _before_filter = len(buckets["shops_to_check"])
    buckets["shops_to_check"] = _drop_ongoing_email_sale_shops(
        buckets["shops_to_check"], prior_email_sales, today,
    )
    if _before_filter != len(buckets["shops_to_check"]):
        log.info(
            "skipped %d homepage check(s) for shops with an ongoing email sale",
            _before_filter - len(buckets["shops_to_check"]),
        )
    log.info(
        "resolve_fuzzy: %d shop homepages, %d names to resolve, %d loose mentions, %d signals",
        len(buckets["shops_to_check"]),
        len(buckets["shops_to_resolve"]),
        len(buckets["loose_ready"]),
        len(combined_signals),
    )
    fuzzy = resolve_fuzzy(
        shops_to_check=buckets["shops_to_check"],
        shops_to_resolve=buckets["shops_to_resolve"],
        loose_mentions=buckets["loose_ready"],
        email_signals=combined_signals,
        prior_verdicts=prior_verdicts,
        today=today,
        shadow_model=cfg.shadow_model or None,
    )
    if fuzzy.get("usage"):
        log.info("claude usage: %s", fuzzy["usage"])

    # Cost lever #5 (issue #16): fold this run's shadow A/B verdict diff into
    # the persisted experiment log. None ⇒ shadow disabled / no API call /
    # shadow call failed ⇒ leave shadow_runs.json untouched.
    shadow_runs_store = None
    if fuzzy.get("shadow"):
        shadow = fuzzy["shadow"]
        shadow_summary = shadow.get("summary") or {}
        log.info(
            "shadow diff (%s vs %s): %d/%d verdicts agree; %d disagreement(s): %s",
            shadow.get("model"), CLAUDE_DEFAULT_MODEL,
            shadow_summary.get("agree", 0), shadow_summary.get("total", 0),
            len(shadow.get("disagreements") or []),
            shadow.get("usage"),
        )
        shadow_runs_store = shadow_compare.prune(
            shadow_compare.append_run(prior_shadow_runs, {
                "at": now_iso,
                "primary_model": CLAUDE_DEFAULT_MODEL,
                "shadow_model": shadow.get("model"),
                "summary": shadow.get("summary"),
                "disagreements": shadow.get("disagreements"),
                "primary_usage": fuzzy.get("usage"),
                "shadow_usage": shadow.get("usage"),
            }),
            today,
        )

    aliases = _merge_aliases(aliases, fuzzy.get("resolutions", []))

    # Persist email/SMS sale announcements so advance + multi-day sales keep
    # showing in the digest until they end (not just the day the email lands).
    # This run's "yes" judgements are upserted into the prior store, expired
    # entries pruned, and the still-active ones rendered in their own dated
    # digest section. See src/email_sales.py.
    email_sales_store = email_sales.prune(
        email_sales.upsert(prior_email_sales, fuzzy.get("email_sales", []), now_iso),
        today,
    )

    # Cost lever #3: fold this run's fresh homepage verdicts (cache misses) into
    # the persisted cache and prune stale entries. Next run reuses a verdict
    # whose shop's sale-signal hash is unchanged and still fresh instead of
    # re-paying Claude. See src/shop_verdicts.py.
    shop_verdicts_store = shop_verdicts.prune(
        shop_verdicts.upsert(prior_verdicts, fuzzy.get("shop_verdicts", []), now_iso),
        today,
    )
    active_email_sales = email_sales.active(email_sales_store, today)
    # Shops with an *ongoing* email sale render solely in the email section
    # (avoids a shop showing as both "no sale" and "on sale"); shops with only
    # an *upcoming* email sale keep their homepage entry so the user sees both
    # the current status and the upcoming countdown.
    shop_sales = _strip_email_sale_shops(
        fuzzy.get("shop_sales", []), active_email_sales, today,
    )
    # This run's "unclear" email judgements are low-signal and one-shot (not
    # persisted): surface them in their own "Possible sales (unclear)" section
    # so an ambiguous promo email still reaches the user the day it lands.
    # Skip shops already shown as a confirmed (active) email sale.
    _active_email_shops = {
        (e.get("shop") or "").strip().lower()
        for e in active_email_sales if e.get("shop")
    }
    email_unclear = [
        e for e in fuzzy.get("email_sales", [])
        if (e.get("status") or "").strip().lower() == "unclear"
        and (e.get("shop") or "").strip()
        and (e.get("shop") or "").strip().lower() not in _active_email_shops
    ]

    codes = _merge_codes(
        prior_codes,
        watchlist_codes_raw,
        (gmail_result["codes"] + gmail_result["unattributed"]
         + voice_result["codes"] + voice_result["unattributed"]),
        now_iso,
    )

    # Extract any URLs Claude matched for loose mentions
    loose_meta: dict[str, dict] = {}
    for m in fuzzy.get("loose_matches", []):
        url = m.get("matched_url")
        if url:
            loose_meta[url] = {"shop": m.get("shop"), "mention": m.get("mention")}
    loose_extracted = (
        _extract_many(list(loose_meta.keys()), preferred_sizes=_sizes_for_url)
        if loose_meta else {}
    )

    # Run sale detection on every URL we successfully extracted. price_rules carry
    # the change-point history knobs so detect_sale can tell a genuine markdown
    # from a year-round standing discount (see src/price_history.py).
    price_rules = PriceRules(
        retention_days=cfg.price_history_retention_days,
        baseline_days=cfg.price_baseline_days,
        min_history_days=cfg.price_history_min_days,
        drop_margin_pct=cfg.price_drop_margin_pct,
        variant_retention_days=cfg.variant_history_retention_days,
    )
    items: list[dict] = []
    priority_urls = buckets.get("priority_urls") or set()
    for url, shop in buckets["product_urls"]:
        _apply_detect(items, prices, url, shop, extracted[url],
                      is_uncertain=False, rules=price_rules,
                      priority=url in priority_urls)
    for url, ext in loose_extracted.items():
        meta = loose_meta[url]
        _apply_detect(items, prices, url, meta["shop"], ext,
                      is_uncertain=True, rules=price_rules)

    unresolved_shops = _collect_unresolved(
        fuzzy, [m["shop"] for m in buckets["loose_deferred"]]
    )

    # Fit-feedback nudge: signed per-item form links for wardrobe items still
    # awaiting a review. Computed once and reused by both the daily digest
    # section (gated on the daily toggle) and the standalone weekly email.
    fit_pending, fit_review_all = _fit_feedback_data(wardrobe, cfg)

    # Watchlist-removal nudge: signed approve-links for purchased items still
    # listed on the watchlist Doc. Approving in the web form deletes the Doc line
    # and records the removal in wardrobe.json (the cron only reads the wardrobe).
    removal_pending, removal_review_all = _watchlist_removal_data(
        wardrobe, cfg, doc_text,
    )

    # Review-request aggregation: recent post-purchase "leave a review" emails,
    # deduped one-per-order, each linking to the email. Failure-isolated +
    # stateless (re-fetched each run; no Gist file).
    review_requests_render, review_requests_all_url = _review_requests_pipeline(
        cfg, now=now,
    )
    log.info("review_requests: %d deduped entries", len(review_requests_render))

    # Back-in-stock email alerts: recent "your item is back" emails, deduped
    # per (shop, item), merged into the digest's "Back in stock" section.
    # Failure-isolated + stateless (re-fetched each run; no Gist file).
    restock_emails_render, restock_emails_all_url = _restock_emails_pipeline(
        cfg, now=now,
    )
    log.info("restock_emails: %d deduped entries", len(restock_emails_render))

    digest_md = build_digest({
        "items": items,
        "shop_sales": shop_sales,
        "non_clothing_shops": buckets["non_clothing_shops"],
        "untracked_items": buckets.get("untracked_urls") or [],
        "email_sales": active_email_sales,
        "email_unclear": email_unclear,
        "untracked_sms": voice_result["untracked_senders"],
        "today": today,
        "codes": codes,
        "unresolved_shops": unresolved_shops,
        "fx_rates": fx_rates,
        "fit_pending": fit_pending if cfg.fit_feedback_daily else [],
        "fit_review_all_url": fit_review_all if cfg.fit_feedback_daily else None,
        "removal_pending": removal_pending if cfg.watchlist_removal_daily else [],
        "removal_review_all_url": (
            removal_review_all if cfg.watchlist_removal_daily else None
        ),
        "review_requests": review_requests_render,
        "review_requests_all_url": review_requests_all_url,
        "review_requests_days": cfg.review_requests_days,
        "email_restocks": restock_emails_render,
        "email_restocks_all_url": restock_emails_all_url,
    })

    # SALE_CHECK_DRY_RUN=1 — local testing path. Skip the two side-effecting
    # writes (Gist PATCH + Resend POST) but still write digest.md locally so
    # the run can be eyeballed. Watchlist fetch, Anthropic call, and Gist
    # *read* still happen (all read-only or paid-but-harmless).
    dry_run = os.environ.get("SALE_CHECK_DRY_RUN", "").lower() in ("1", "true", "yes")

    # Weekly-ish BodySpec scan-cache refresh (age-gated, failure-isolated). None
    # ⇒ leave body_scans.json untouched (creds unset, dry-run, cache fresh, or
    # BodySpec errored). The fit-feedback web form reads this cache to match a
    # body state to a review the moment it's left.
    body_scans_out = _maybe_refresh_body_scans(cfg, body_scans, dry_run)

    if dry_run:
        log.info("DRY_RUN: skipping write_state")
    else:
        log.info("writing state to gist")
        write_state(
            cfg.gist_id,
            cfg.github_token,
            prices=prices,
            aliases=aliases,
            codes=codes,
            fx=fx_cache_out or None,
            gmail=gmail_result["gmail_state_out"],
            voice=voice_result["voice_state_out"],
            sms_aliases=sms_aliases,
            email_sales=email_sales_store,
            body_scans=body_scans_out,
            shop_verdicts=shop_verdicts_store,
            shadow_runs=shadow_runs_store,
            throttle={
                "shopify_gate_interval": _SHOPIFY_LIMITER.next_interval,
                "last_run_stormed": _SHOPIFY_LIMITER.stormed,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    subject = _digest_subject(shop_sales, items, active_email_sales, today=now)

    # Local-run convenience: dump the rendered digest to disk so it can be
    # eyeballed without paying for another API round-trip. Gitignored.
    try:
        with open("digest.md", "w", encoding="utf-8") as f:
            f.write(f"<!-- {subject} -->\n\n{digest_md}\n")
    except OSError as exc:
        log.warning("could not write digest.md: %s", exc)

    if dry_run:
        log.info("DRY_RUN: skipping send_email — digest in digest.md (subject: %s)", subject)
    else:
        log.info("sending email: %s", subject)
        send_email(
            cfg.resend_api_key,
            cfg.from_email,
            cfg.to_email,
            subject,
            digest_md,
        )

    # Standalone weekly fit-feedback email (separate from the sales digest).
    # No-op unless enabled, due today (UTC weekday), and items are pending.
    _maybe_send_weekly_fit_email(cfg, fit_pending, fit_review_all, dry_run)

    return digest_md


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("SALE_CHECK_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Public-repo Actions runs set SALE_CHECK_REDACT_LOGS=1 so the (publicly
    # readable) workflow log never carries watchlist URLs. Inert locally.
    log_privacy.install()
    # Local runs: load .env from sale-check/. GitHub Actions injects vars
    # directly into the environment, so load_dotenv is a no-op there.
    # override=True lets .env beat blank/stale shell values (some local shells
    # export an empty ANTHROPIC_API_KEY which would otherwise mask the real one).
    try:
        from dotenv import load_dotenv
        load_dotenv(override=True)
    except ImportError:
        pass
    try:
        run()
    except Exception:
        log.exception("sale-check run failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
