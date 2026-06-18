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
