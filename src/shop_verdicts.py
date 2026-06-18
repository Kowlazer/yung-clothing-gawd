"""Persisted per-shop homepage sale-verdict cache (cost lever #3).

Homepages rarely change day to day: a sale running today is almost always still
running tomorrow with identical promo text. Yet ``claude_fuzzy.resolve_fuzzy``
re-sends every signal-bearing homepage to Claude every run and gets the same
answer back, paying full input cost each time. Levers #1/#2 already drop the
*no-signal* and *ongoing-email-sale* homepages for free; this cache drops the
*stable* ones — whatever their verdict ("yes", "no", or "unclear").

The cache is keyed on a content hash of the homepage's **sale-signal
substrings** (not the whole excerpt — see ``claude_fuzzy._verdict_hash``), so
volatile junk (cart counts, "12 viewing now", rotating carousels) doesn't bust
it, but any change to the actual promo wording does → the entry misses → the
homepage is re-judged. That's what keeps the cache from ever masking a sale
that started, changed, or ended:

  * sale **starts**  → promo text appears → hash changes → re-judged.
  * sale **changes** (30→50% off) → matched substrings change → re-judged.
  * sale **ends**    → promo text gone → ``claude_fuzzy._has_sale_signal`` is
    False → lever #1 records "no" *before* this cache is consulted.

A reuse-freshness ceiling (``_REUSE_MAX_AGE_DAYS``) re-judges a verdict even on
a hash match once it's older than the ceiling, bounding how stale a reused
answer can be — the safety net for a change the signal-hash can't see (e.g. a
sale announced only in a homepage image). Entries not re-confirmed for
``_PRUNE_DAYS`` (shop left the watchlist, or its homepage went no-signal so
lever #1 now owns it) are pruned.

Unlike lever #1 the digest is *not* guaranteed byte-identical: a reused
``description`` is whatever Claude last wrote for the same signal substrings —
correct for the current promo, but possibly worded differently than a fresh
call would word it today. The ``status`` (the field that drives correctness) is
always consistent with the current signal text.

Persisted entry shape (one per shop, file ``shop_verdicts.json``)::

    {
      "shop": "Aniqi",
      "hash": "<sha256 hex of the sale-signal substrings>",
      "status": "yes" | "no" | "unclear",
      "description": "30% off sitewide" | null,
      "checked_at": "<iso ts of the run that last judged it via Claude>"
    }

This module is pure (no Claude, no I/O). The hash itself lives in
``claude_fuzzy`` (which owns ``_SALE_SIGNAL_RE``); everything here takes a
precomputed hash. It mirrors the ``src/email_sales.py`` lifecycle: an
``index`` + ``lookup`` for the read side, ``upsert`` + ``prune`` for the write
side, all keyed by ``shop.lower()``.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

log = logging.getLogger(__name__)

# A cached verdict is reused only when its hash still matches AND it was judged
# within this many days. Past the ceiling the homepage is re-sent even on a
# hash match, so a reused verdict can be at most this stale — cheap insurance
# against a sale change the signal-substring hash can't see (e.g. image-only).
_REUSE_MAX_AGE_DAYS = 7
# An entry not re-confirmed for this long is dropped (the shop left the
# watchlist, or its homepage went no-signal and lever #1 now records it for
# free). A live shop is re-judged every _REUSE_MAX_AGE_DAYS, refreshing
# checked_at well inside this horizon, so live shops are never pruned.
_PRUNE_DAYS = 30


def _today(today: date | None) -> date:
    return today or datetime.now(timezone.utc).date()


def _checked_date(entry: dict) -> date | None:
    """The ``checked_at`` timestamp as a ``date``, or None if missing/unparseable."""
    raw = entry.get("checked_at")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _shop_key(value: str | None) -> str:
    return (value or "").strip().lower()


# ---------------------------------------------------------------------------
# Read side — build an index, look a verdict up
# ---------------------------------------------------------------------------

def index(prior: list[dict]) -> dict[str, dict]:
    """Build a ``{shop.lower(): entry}`` lookup from the persisted store.

    Non-dict junk and entries without a shop are skipped; later duplicates win
    (defensive — the store holds one entry per shop).
    """
    out: dict[str, dict] = {}
    for e in prior or []:
        if not isinstance(e, dict):
            continue
        k = _shop_key(e.get("shop"))
        if k:
            out[k] = e
    return out


def lookup(
    idx: dict[str, dict], shop: str, content_hash: str,
    today: date | None = None,
) -> dict | None:
    """Return the cached entry to reuse for ``shop``, or None to re-judge.

    A hit requires all three: the shop is in the cache, its stored ``hash``
    equals ``content_hash`` (the homepage's current sale-signal fingerprint),
    and it was judged within ``_REUSE_MAX_AGE_DAYS``. Any miss → None → the
    caller sends the homepage to Claude as usual.
    """
    today = _today(today)
    if not content_hash:
        return None
    entry = idx.get(_shop_key(shop))
    if not entry or entry.get("hash") != content_hash:
        return None
    checked = _checked_date(entry)
    if checked is None or (today - checked).days > _REUSE_MAX_AGE_DAYS:
        return None
    return entry


# ---------------------------------------------------------------------------
# Write side — fold this run's fresh judgements in, prune the stale
# ---------------------------------------------------------------------------

def upsert(prior: list[dict], judged: list[dict], now_iso: str) -> list[dict]:
    """Fold this run's freshly-judged verdicts into the prior store.

    ``judged`` holds only the homepages actually sent to Claude this run (cache
    misses), each shaped ``{shop, hash, status, description}``. Each overwrites
    its shop's entry with the new hash + verdict + ``checked_at = now_iso``; a
    shop not yet in the store is appended.

    Prior entries not re-judged this run ride along untouched. That includes
    cache **hits** — a hit deliberately keeps its original ``checked_at`` so the
    reuse-freshness ceiling still forces a re-judge ``_REUSE_MAX_AGE_DAYS`` after
    the *last real* judgement, not after the last hit (otherwise a stable sale
    would never be re-validated). Entries for shops not checked at all ride
    along too, and ``prune`` ages them out.
    """
    out = [dict(e) for e in (prior or []) if isinstance(e, dict)]
    idx: dict[str, int] = {}
    for i, e in enumerate(out):
        k = _shop_key(e.get("shop"))
        if k:
            idx[k] = i

    for j in judged or []:
        shop = (j.get("shop") or "").strip()
        content_hash = j.get("hash")
        if not shop or not content_hash:
            continue
        record = {
            "shop": shop,
            "hash": content_hash,
            "status": j.get("status"),
            "description": j.get("description"),
            "checked_at": now_iso,
        }
        k = shop.lower()
        if k in idx:
            out[idx[k]] = record
        else:
            out.append(record)
            idx[k] = len(out) - 1
    return out


def prune(entries: list[dict], today: date | None = None) -> list[dict]:
    """Drop entries not re-confirmed within ``_PRUNE_DAYS`` (and non-dict junk).

    An entry with a missing/unparseable ``checked_at`` is kept (safe fallback,
    mirroring the other state pruners) — it ages out once it gets a real stamp.
    """
    today = _today(today)
    out: list[dict] = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        checked = _checked_date(e)
        if checked is not None and (today - checked).days > _PRUNE_DAYS:
            log.info(
                "shop_verdicts: pruning stale %s verdict (checked_at=%s)",
                e.get("shop"), e.get("checked_at"),
            )
            continue
        out.append(e)
    return out
