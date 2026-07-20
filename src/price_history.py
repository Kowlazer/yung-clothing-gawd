"""Change-point price history for distinguishing real drops from standing "sales".

A product's day-to-day price is stored on its ``prices.json`` entry as a compact
**change-point** series: a list of ``"YYYY-MM-DD:price"`` string tokens, one per
*change* (not per day). A price that holds steady costs a single point no matter
how long we track it, so a year-round "50% off" anchor item stays one token while
a volatile item grows only with the number of actual price moves. Each token
means "the price became ``price`` on this date and held until the next token's
date" — so the whole series is recoverable from the change-points alone.

This is what lets us tell a *genuine* markdown (today's price is below the item's
own recent high) from a *standing discount* (the page advertises a markdown but
the price has never actually been higher across our window — the fake "always on
sale" case). The trailing-max **baseline** over a window is the comparison point;
``sale_detect`` consumes it.

All functions are pure and operate on the token list + a ``today`` date, so they
test without a clock or network. Tokens are kept chronological. Malformed tokens
(hand-edited state, future format drift) are skipped defensively rather than
raising — a corrupt point should never crash the daily run.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)


def today_utc() -> date:
    """Current UTC date — the clock seam tests stub by passing ``today`` explicitly."""
    return datetime.now(timezone.utc).date()


def _fmt_num(price: float) -> str:
    """Render a price without a trailing ``.0`` for whole numbers, else as-is.

    ``50.0 -> "50"``, ``49.99 -> "49.99"``. Keeps tokens short and stable so an
    unchanged whole-dollar price doesn't churn the token (``"50"`` every run).
    """
    if price == int(price):
        return str(int(price))
    return repr(price)


def format_point(d: date, price: float) -> str:
    """Build one ``"YYYY-MM-DD:price"`` change-point token."""
    return f"{d.isoformat()}:{_fmt_num(price)}"


def parse_point(token: str) -> tuple[date, float] | None:
    """Parse one token back to ``(date, price)``; ``None`` if malformed.

    The date is ISO (no colons) so a left ``partition(":")`` cleanly splits the
    single separator from the numeric tail.
    """
    if not isinstance(token, str):
        return None
    day_str, sep, price_str = token.partition(":")
    if not sep:
        return None
    try:
        return date.fromisoformat(day_str), float(price_str)
    except (ValueError, TypeError):
        return None


def parse_history(points: list | None) -> list[tuple[date, float]]:
    """Parse a token list to ``[(date, price), ...]``, chronological, malformed dropped."""
    if not points:
        return []
    parsed = [p for p in (parse_point(t) for t in points) if p is not None]
    parsed.sort(key=lambda dp: dp[0])
    return parsed


def append_observation(points: list | None, price: float, today: date) -> list[str]:
    """Append today's ``price`` as a change-point, only when it differs.

    Change-point semantics: a point is added solely when the price changed from
    the last recorded point. A same-day re-run that reports a *different* price
    replaces today's point rather than stacking a second one for the same date,
    so there is at most one token per date and the series stays a true step
    function. An unchanged price is a no-op (the existing tail still describes
    today). Returns a fresh token list; never mutates the input.
    """
    parsed = parse_history(points)
    if not parsed:
        return [format_point(today, price)]

    last_date, last_price = parsed[-1]
    if last_price == price:
        return [format_point(d, p) for d, p in parsed]  # no change

    if last_date == today:
        parsed[-1] = (today, price)  # same-day correction — replace, don't stack
    else:
        parsed.append((today, price))
    return [format_point(d, p) for d, p in parsed]


def prune(points: list | None, today: date, retention_days: int) -> list[str]:
    """Drop change-points older than ``retention_days``, keeping one carry-in.

    Everything on or after the cutoff is kept. The single most-recent point
    *before* the cutoff is also kept — it carries the price that was in effect
    when the window opened, so the series stays well-defined (and a long-standing
    price keeps its original "in effect since" date instead of being silently
    re-dated). Bounds the list at (changes within the window) + 1.
    """
    parsed = parse_history(points)
    if not parsed:
        return []
    cutoff = _shift(today, retention_days)
    within = [dp for dp in parsed if dp[0] >= cutoff]
    before = [dp for dp in parsed if dp[0] < cutoff]
    kept = ([before[-1]] + within) if before else within
    return [format_point(d, p) for d, p in kept]


def baseline_max(points: list | None, today: date, window_days: int) -> float | None:
    """Trailing-max price over the last ``window_days`` — the item's recent high.

    Considers every price that was *in effect at any moment* inside the window:
    all points on/after the window start, plus the carry-in point in effect when
    the window opened. The max of those is the comparison baseline — today's
    price sitting below it (by a margin) is a real markdown; sitting at it means
    the "sale" never actually lowered the price. ``None`` when there's no history.
    """
    parsed = parse_history(points)
    if not parsed:
        return None
    start = _shift(today, window_days)
    within = [p for d, p in parsed if d >= start]
    before = [p for d, p in parsed if d < start]
    candidates = ([before[-1]] if before else []) + within
    return max(candidates) if candidates else None


def _shift(today: date, days: int) -> date:
    """``today`` minus ``days``, guarding against an absurd/negative knob."""
    return today - timedelta(days=max(int(days), 0))


# ---------------------------------------------------------------------------
# Deal quality: how today's price ranks against everything we've seen
# ---------------------------------------------------------------------------
# ``baseline_max`` answers "is this a real markdown or an anchor". This answers
# the question that comes next, and that the digest could never answer before:
# *is it a good one*. The same change-point series already holds it — a step
# function over the retention window — so the whole thing is a walk over points
# we're storing anyway, at no extra fetch, storage, or Claude cost.
#
# Below ``min_tracked_days`` we say nothing rather than something thin: "lowest
# in 9 days" reads like a fact and is nearly noise, and the first weeks after a
# URL is added are exactly when the series is shallowest.
MIN_TRACKED_DAYS = 21

# A prior price only counts as "lower" if it was lower by this much. Shop prices
# wobble between checks for reasons that have nothing to do with a sale, and
# probes of the real state showed those wobbles would otherwise dominate the
# output ($110 vs $106, $148 vs $144, $87 vs $86 — technically lower, useless to
# know, frequent enough to teach the reader to skip the note).
#
# 5% because the noise has an identified source and a measured size. Every
# oscillating series in the 2026-07-19 state traced to ONE shop — carmico.ca, a
# Canadian storefront that recomputes its USD prices from CAD daily, producing
# runs like 108, 107, 106, 107, 106, 107, 109, 110 on a product nobody ever put
# on sale. That FX drift tops out around 4%; the genuine markdowns in the same
# state start at 7.7% ($36.69 vs $33.84) and run past 30% ($34.99 vs $22.99). 5% sits in
# the empty band between the two, so it excludes the drift with headroom on both
# sides rather than splitting a continuum.
#
# Deliberately separate from ``PriceRules.drop_margin_pct``, which answers a
# different question (is a page's advertised markdown real) — tuning one should
# not silently move the other.
MIN_GAP_PCT = 5.0


def price_standing(
    points: list | None,
    today: date,
    price: float,
    *,
    min_tracked_days: int = MIN_TRACKED_DAYS,
    min_gap_pct: float = MIN_GAP_PCT,
) -> dict | None:
    """Rank ``price`` against the item's own observed history.

    ``points`` is the change-point series **including today's observation** (the
    tail therefore carries ``price``), as ``sale_detect`` has it after
    ``append_observation``. Returns ``None`` when the series is unusable or too
    shallow to be worth a claim — the caller then simply says nothing.

    Otherwise:

        tracked_days      int          span from the earliest point we still hold
        is_lowest         bool         never *materially* lower in that span
        days_since_lower  int | None   days since it last was (None if never)
        prior_low         float | None the lowest price seen below ``price``
        prior_low_on      str | None   ISO date that low *started*

    "Lower" means lower by more than ``min_gap_pct`` — see ``MIN_GAP_PCT``. So
    ``is_lowest`` reads as "at, or within noise of, the best we've seen", which
    is what the digest claims with it. It is only returned when the price has
    also been *materially higher* at some point: a series that has never moved
    makes its own price the lowest by definition, and that is not news.

    Interval semantics match the rest of the module: a point's price is in
    effect from its own date until the day before the next point (or today, for
    the tail). Degenerate runs — duplicate dates from hand-edited state, points
    dated in the future — are skipped rather than raising, same defensive stance
    as ``parse_point``.
    """
    parsed = parse_history(points)
    if not parsed:
        return None

    tracked_days = (today - parsed[0][0]).days
    if tracked_days < max(int(min_tracked_days), 0):
        return None

    # Only a materially lower price counts — a dollar of drift is not a deal
    # the user missed.
    threshold = price * (1.0 - max(float(min_gap_pct), 0.0) / 100.0)

    last_lower_end: date | None = None      # most recent day a lower price held
    lows: list[tuple[float, date]] = []     # (price, day that price started)
    for i, (start, p) in enumerate(parsed):
        end = parsed[i + 1][0] - timedelta(days=1) if i + 1 < len(parsed) else today
        end = min(end, today)               # clamp a future-dated point
        if end < start or p >= threshold:
            continue
        lows.append((p, start))
        if last_lower_end is None or end > last_lower_end:
            last_lower_end = end

    if not lows:
        # Never been lower — but that only *means* something if it has ever been
        # higher. A price that has sat flat since we first saw it is trivially
        # its own lowest, and saying so implies a deal that does not exist. The
        # 2026-07-19 verification digest was full of these ("$48 — not on sale —
        # lowest in 40d of tracking" on an item that has only ever been $48), so
        # a flat series makes no claim at all.
        ceiling = price * (1.0 + max(float(min_gap_pct), 0.0) / 100.0)
        if not any(p > ceiling for _, p in parsed):
            return None
        return {
            "tracked_days": tracked_days,
            "is_lowest": True,
            "days_since_lower": None,
            "prior_low": None,
            "prior_low_on": None,
        }

    prior_low = min(p for p, _ in lows)
    # Most recent time it was that cheap — "back in April" should mean the last
    # April, not the first, when a price oscillates.
    prior_low_on = max(start for p, start in lows if p == prior_low)
    return {
        "tracked_days": tracked_days,
        "is_lowest": False,
        "days_since_lower": (today - last_lower_end).days,
        "prior_low": prior_low,
        "prior_low_on": prior_low_on.isoformat(),
    }
