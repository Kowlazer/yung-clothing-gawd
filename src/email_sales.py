"""Persisted store for sale announcements mined from the Gmail Promotions tab.

The daily run judges each watchlist-attributed promo email via Claude
(``claude_fuzzy`` Task type 4) into ``status`` + ``description`` + an optional
resolved sale window (``starts_on`` / ``ends_on``). Those judgements are
one-shot — an email is processed exactly once (deduped via
``gmail_state.processed_ids``) and never re-fetched once it falls out of the
2-day Promotions window. Without persistence an advance announcement ("sale
starts May 24") would show in a single day's digest and then vanish.

This module keeps the judged ``"yes"`` announcements alive across runs so the
digest can keep reminding the user until the sale actually ends, with a
countdown. It mirrors the ``codes.json`` lifecycle in ``state.py``: a flat list
upserted each run and pruned once expired.

Persisted entry shape (one per announcement, file ``email_sales.json``)::

    {
      "shop": "Aniqi",
      "email_id": "<X-GM-MSGID>",          # stable dedupe key with shop
      "status": "yes",                     # only "yes" is persisted
      "description": "Memorial Day sale, 30% off sitewide" | null,
      "starts_on": "2026-05-24" | null,    # resolved by Claude, ISO date
      "ends_on":   "2026-05-26" | null,
      "first_seen": "<iso ts>",
      "last_seen":  "<iso ts>"
    }

Lifecycle rules (all pure, ``today`` is a ``datetime.date``):

  * **effective end** = ``ends_on`` if present, else ``starts_on`` (a sale we
    only know the start of is shown through that day), else ``None`` (undated).
  * **expired** when an effective end exists and ``today`` is more than
    ``_ENDED_GRACE_DAYS`` past it; for undated entries, when ``last_seen`` is
    older than ``_UNDATED_TTL_DAYS`` (matching the "day-of" spirit so a one-shot
    "sale on now" eventually drops).
  * a future ``starts_on`` never expires — that's the whole point of advance
    notice.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)

# An undated "yes" announcement (no resolved window at all) lingers this many
# days past its last sighting before pruning. Short on purpose: an email with
# no parseable dates is treated as a "today-ish" signal, not a standing sale.
_UNDATED_TTL_DAYS = 4
# Keep a dated sale visible one day past its effective end so a late-night
# "ends Sunday" sale isn't yanked the moment the clock rolls over to Monday UTC.
_ENDED_GRACE_DAYS = 1


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parse_date(value: str | None) -> date | None:
    """Parse an ISO ``YYYY-MM-DD`` (or full ISO timestamp) into a ``date``.

    Returns None for empty / unparseable input — callers treat that as "no
    date known", never as an error.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _norm_date_str(value: str | None) -> str | None:
    """Normalise an incoming date to a canonical ``YYYY-MM-DD`` string or None."""
    d = _parse_date(value)
    return d.isoformat() if d else None


def _effective_end(entry: dict) -> date | None:
    """The last day this announcement is considered relevant.

    ``ends_on`` wins; absent that, ``starts_on`` (we show a start-only sale
    through its start day); absent both, None (undated → TTL governs).
    """
    ends = _parse_date(entry.get("ends_on"))
    if ends is not None:
        return ends
    return _parse_date(entry.get("starts_on"))


def _today(today: date | None) -> date:
    return today or datetime.now(timezone.utc).date()


# ---------------------------------------------------------------------------
# Dedupe key
# ---------------------------------------------------------------------------

def _key(entry: dict) -> tuple[str, str] | None:
    shop = (entry.get("shop") or "").strip().lower()
    email_id = str(entry.get("email_id") or "").strip()
    if not shop or not email_id:
        return None
    return (shop, email_id)


# ---------------------------------------------------------------------------
# Upsert this run's judgements
# ---------------------------------------------------------------------------

def upsert(prior: list[dict], judged: list[dict], now_iso: str) -> list[dict]:
    """Fold this run's ``email_sales`` judgements into the prior persisted list.

    Only ``status == "yes"`` judgements with both a shop and an ``email_id`` are
    kept — ambiguous ("unclear") or negative ("no") judgements are not persisted
    so the section stays high-signal. Entries are keyed by
    ``(shop.lower(), email_id)``:

      * **new** key → appended with ``first_seen`` = ``last_seen`` = ``now_iso``.
      * **existing** key → ``last_seen`` bumped; description / dates refreshed
        only when the re-sighting carries a non-empty value (so a later empty
        re-judge never clobbers a good earlier one). In practice the dedupe in
        ``gmail_state`` means an email is judged once, so this branch is
        defensive.

    Prior entries not re-seen this run ride along untouched — ``prune`` ages
    them out by date.
    """
    out = [dict(e) for e in (prior or []) if isinstance(e, dict)]
    index: dict[tuple[str, str], int] = {}
    for i, e in enumerate(out):
        k = _key(e)
        if k is not None:
            index[k] = i

    for j in judged or []:
        if (j.get("status") or "").strip().lower() != "yes":
            continue
        shop = (j.get("shop") or "").strip()
        email_id = str(j.get("email_id") or "").strip()
        if not shop or not email_id:
            continue
        desc = (j.get("description") or "").strip() or None
        starts = _norm_date_str(j.get("starts_on"))
        ends = _norm_date_str(j.get("ends_on"))
        k = (shop.lower(), email_id)
        if k in index:
            e = out[index[k]]
            e["last_seen"] = now_iso
            if desc:
                e["description"] = desc
            if starts:
                e["starts_on"] = starts
            if ends:
                e["ends_on"] = ends
        else:
            out.append({
                "shop": shop,
                "email_id": email_id,
                "status": "yes",
                "description": desc,
                "starts_on": starts,
                "ends_on": ends,
                "first_seen": now_iso,
                "last_seen": now_iso,
            })
            index[k] = len(out) - 1
    return out


# ---------------------------------------------------------------------------
# Expiry / prune
# ---------------------------------------------------------------------------

def is_expired(entry: dict, today: date | None = None) -> bool:
    """True when an announcement should no longer appear and can be dropped."""
    today = _today(today)
    starts = _parse_date(entry.get("starts_on"))
    if starts is not None and starts >= today:
        # Upcoming or starting today never expires — that's the whole point of
        # advance notice. Checked before the effective-end branch so a
        # mis-resolved ``ends_on`` *before* a still-future ``starts_on`` can't
        # yank the sale prematurely.
        return False
    eff = _effective_end(entry)
    if eff is not None:
        return today > eff + timedelta(days=_ENDED_GRACE_DAYS)
    last_seen = _parse_date(entry.get("last_seen"))
    if last_seen is None:
        # Unparseable timestamp → keep (safe fallback, mirrors _prune_codes).
        return False
    return today > last_seen + timedelta(days=_UNDATED_TTL_DAYS)


def prune(entries: list[dict], today: date | None = None) -> list[dict]:
    """Drop expired announcements. Non-dict junk is dropped too."""
    today = _today(today)
    out: list[dict] = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        if is_expired(e, today):
            log.info(
                "email_sales: pruning expired %s sale (end=%s last_seen=%s)",
                e.get("shop"), _effective_end(e), e.get("last_seen"),
            )
            continue
        out.append(e)
    return out


# ---------------------------------------------------------------------------
# Active list (for the digest) + countdown phase
# ---------------------------------------------------------------------------

def _sort_key(entry: dict, today: date) -> tuple:
    """Upcoming sales first (soonest start), then ongoing (soonest end)."""
    starts = _parse_date(entry.get("starts_on"))
    shop = (entry.get("shop") or "").lower()
    if starts is not None and starts >= today:
        # ``>=`` (not ``>``) so a sale starting *today* groups with the
        # upcoming ones, matching ``relative_days`` which reads it as
        # "starts today" rather than "ends today".
        return (0, starts, shop)
    end_ord = _effective_end(entry) or date.max
    return (1, end_ord, shop)


def active(entries: list[dict], today: date | None = None) -> list[dict]:
    """Non-expired announcements, ordered upcoming-first for the digest."""
    today = _today(today)
    live = [
        e for e in entries or []
        if isinstance(e, dict) and not is_expired(e, today)
    ]
    return sorted(live, key=lambda e: _sort_key(e, today))


def relative_days(
    entry: dict, today: date | None = None,
) -> tuple[str, int | None, date | None]:
    """Classify an entry's countdown for rendering. Pure; digest formats it.

    Returns ``(phase, days, on)``:
      * ``("upcoming", n, start_date)`` — starts on/after today (``n`` = days
        until start; 0 = today). Takes precedence so a sale that starts today
        reads "starts today", not "ends today".
      * ``("ending", n, end_date)`` — already started, with a known end on/after
        today (``n`` = days until end).
      * ``("active", None, None)`` — ongoing with no future-facing date to count
        toward (undated, or a start-only sale past its start day).
    """
    today = _today(today)
    starts = _parse_date(entry.get("starts_on"))
    if starts is not None and starts >= today:
        return ("upcoming", (starts - today).days, starts)
    eff = _effective_end(entry)
    if eff is not None and eff >= today:
        return ("ending", (eff - today).days, eff)
    return ("active", None, None)
