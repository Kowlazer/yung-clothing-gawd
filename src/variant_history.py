"""Change-point availability history for a single variant value (one size or one colour).

The size/colour **stock state** of a product is tracked the same compact way
``price_history`` tracks price: each variant value (every offered size, every
offered colour) gets its own list of ``"YYYY-MM-DD:state"`` string tokens, one
per *state change* — not per day. ``state`` is one of ``in`` / ``low`` / ``out``.
A size that stays in stock costs a single token no matter how long we track it,
so the storage scales with stock *moves*, not days — exactly the property that
keeps ``prices.json`` from bloating. Each token means "this value entered
``state`` on this date and held it until the next token's date", so the whole
step-function is recoverable from the change-points alone.

This is what lets the digest say both *what flipped today* ("M just sold out")
and *how long it's been that way* ("L low for 5 days"): ``current_state`` reads
the tail, ``days_in_state`` measures the run since the last change-point.

All functions are pure and operate on the token list + a ``today`` date, so they
test without a clock or network. Tokens are kept chronological. Malformed tokens
(hand-edited state, an unknown state word, future format drift) are skipped
defensively rather than raising — a corrupt point must never crash the daily run.
This module is the categorical twin of ``price_history`` and deliberately mirrors
its shape (``parse_history`` / ``append_observation`` / ``prune`` with carry-in).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

log = logging.getLogger(__name__)

# The only states a variant value can be in. Ordered worst→best is irrelevant;
# this is just the allow-list ``parse_point`` validates against so a junk token
# ("2026-06-10:banana") is dropped instead of polluting the series.
STATES = frozenset({"in", "low", "out"})


def format_point(d: date, state: str) -> str:
    """Build one ``"YYYY-MM-DD:state"`` change-point token."""
    return f"{d.isoformat()}:{state}"


def parse_point(token: str) -> tuple[date, str] | None:
    """Parse one token back to ``(date, state)``; ``None`` if malformed.

    The date is ISO (no colons) so a left ``partition(":")`` cleanly splits the
    single separator from the state tail. An unrecognised state word is treated
    as malformed (dropped) so the series only ever holds known states.
    """
    if not isinstance(token, str):
        return None
    day_str, sep, state = token.partition(":")
    if not sep or state not in STATES:
        return None
    try:
        return date.fromisoformat(day_str), state
    except (ValueError, TypeError):
        return None


def parse_history(points: list | None) -> list[tuple[date, str]]:
    """Parse a token list to ``[(date, state), ...]``, chronological, malformed dropped."""
    if not points:
        return []
    parsed = [p for p in (parse_point(t) for t in points) if p is not None]
    parsed.sort(key=lambda dp: dp[0])
    return parsed


def append_observation(points: list | None, state: str, today: date) -> list[str]:
    """Append today's ``state`` as a change-point, only when it differs.

    Change-point semantics, identical to ``price_history.append_observation``: a
    point is added solely when the state changed from the last recorded point. A
    same-day re-run that reports a *different* state replaces today's point rather
    than stacking a second one for the same date, so there is at most one token
    per date and the series stays a true step function. An unchanged state is a
    no-op. Returns a fresh token list; never mutates the input.
    """
    parsed = parse_history(points)
    if not parsed:
        return [format_point(today, state)]

    last_date, last_state = parsed[-1]
    if last_state == state:
        return [format_point(d, s) for d, s in parsed]  # no change

    if last_date == today:
        parsed[-1] = (today, state)  # same-day correction — replace, don't stack
    else:
        parsed.append((today, state))
    return [format_point(d, s) for d, s in parsed]


def prune(points: list | None, today: date, retention_days: int) -> list[str]:
    """Drop change-points older than ``retention_days``, keeping one carry-in.

    Mirrors ``price_history.prune``: everything on or after the cutoff is kept,
    plus the single most-recent point *before* the cutoff — it carries the state
    that was in effect when the window opened, so the series stays well-defined
    and ``days_in_state`` keeps the original "in effect since" date for a state
    that has held longer than the window.
    """
    parsed = parse_history(points)
    if not parsed:
        return []
    cutoff = _shift(today, retention_days)
    within = [dp for dp in parsed if dp[0] >= cutoff]
    before = [dp for dp in parsed if dp[0] < cutoff]
    kept = ([before[-1]] + within) if before else within
    return [format_point(d, s) for d, s in kept]


def current_state(points: list | None) -> str | None:
    """The state in effect as of the latest change-point; ``None`` if no history."""
    parsed = parse_history(points)
    return parsed[-1][1] if parsed else None


def days_in_state(points: list | None, today: date) -> int | None:
    """How many days the current state has held — ``today`` minus the last change-point.

    The last change-point dates when the current state *began*, so this is the
    length of the ongoing run (``0`` the day it flipped). ``None`` when there's no
    history. Never negative even if a future-dated point sneaks in (clamped to 0).
    """
    parsed = parse_history(points)
    if not parsed:
        return None
    return max((today - parsed[-1][0]).days, 0)


def _shift(today: date, days: int) -> date:
    """``today`` minus ``days``, guarding against an absurd/negative knob."""
    return today - timedelta(days=max(int(days), 0))
