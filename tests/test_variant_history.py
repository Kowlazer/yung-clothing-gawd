"""Tests for src/variant_history.py — the per-variant change-point state series.

Pure date math, no clock or network: every test passes an explicit ``today``.
The categorical twin of test_price_history.py.
"""

from __future__ import annotations

from datetime import date, timedelta

from src import variant_history as vh

TODAY = date(2026, 6, 10)


def _d(days_ago: int) -> date:
    return TODAY - timedelta(days=days_ago)


# ---------------------------------------------------------------------------
# Token format / parse
# ---------------------------------------------------------------------------

class TestTokens:
    def test_format(self):
        assert vh.format_point(date(2026, 6, 10), "out") == "2026-06-10:out"

    def test_round_trip(self):
        tok = vh.format_point(date(2026, 1, 2), "low")
        assert vh.parse_point(tok) == (date(2026, 1, 2), "low")

    def test_parse_malformed_returns_none(self):
        assert vh.parse_point("not-a-point") is None
        assert vh.parse_point("2026-13-40:in") is None  # impossible date
        assert vh.parse_point("2026-06-10:banana") is None  # unknown state
        assert vh.parse_point(123) is None  # type: ignore[arg-type]

    def test_parse_history_sorts_and_drops_malformed(self):
        raw = ["2026-06-05:low", "bad", "2026-06-01:in", "2026-06-07:weird"]
        assert vh.parse_history(raw) == [
            (date(2026, 6, 1), "in"),
            (date(2026, 6, 5), "low"),
        ]

    def test_parse_history_empty(self):
        assert vh.parse_history(None) == []
        assert vh.parse_history([]) == []


# ---------------------------------------------------------------------------
# append_observation — change-point semantics
# ---------------------------------------------------------------------------

class TestAppend:
    def test_empty_seeds_first_point(self):
        assert vh.append_observation([], "in", TODAY) == ["2026-06-10:in"]

    def test_unchanged_state_is_noop(self):
        pts = [vh.format_point(_d(30), "in")]
        assert vh.append_observation(pts, "in", TODAY) == pts

    def test_changed_state_appends(self):
        pts = [vh.format_point(_d(30), "in")]
        out = vh.append_observation(pts, "out", TODAY)
        assert out == ["2026-05-11:in", "2026-06-10:out"]

    def test_same_day_change_replaces_not_stacks(self):
        pts = vh.append_observation([vh.format_point(_d(5), "in")], "low", TODAY)
        out = vh.append_observation(pts, "out", TODAY)
        assert out == ["2026-06-05:in", "2026-06-10:out"]
        assert len(out) == 2  # today's point replaced, not duplicated

    def test_does_not_mutate_input(self):
        pts = [vh.format_point(_d(10), "in")]
        original = list(pts)
        vh.append_observation(pts, "out", TODAY)
        assert pts == original

    def test_back_in_stock_is_recorded(self):
        pts = [vh.format_point(_d(40), "in"), vh.format_point(_d(20), "out")]
        out = vh.append_observation(pts, "in", TODAY)
        assert out[-1] == "2026-06-10:in"


# ---------------------------------------------------------------------------
# prune — retention window + carry-in
# ---------------------------------------------------------------------------

class TestPrune:
    def test_keeps_within_window(self):
        pts = [vh.format_point(_d(10), "in"), vh.format_point(_d(2), "out")]
        assert vh.prune(pts, TODAY, 365) == pts

    def test_drops_old_but_keeps_one_carry_in(self):
        pts = [
            vh.format_point(_d(400), "in"),   # before window — carry-in
            vh.format_point(_d(390), "low"),  # before window — should drop
            vh.format_point(_d(10), "out"),   # within window
        ]
        out = vh.prune(pts, TODAY, 365)
        assert out == [vh.format_point(_d(390), "low"), vh.format_point(_d(10), "out")]

    def test_flat_ancient_state_is_retained(self):
        pts = [vh.format_point(_d(900), "in")]
        assert vh.prune(pts, TODAY, 365) == pts

    def test_empty(self):
        assert vh.prune([], TODAY, 365) == []


# ---------------------------------------------------------------------------
# current_state / days_in_state
# ---------------------------------------------------------------------------

class TestCurrentState:
    def test_none_when_empty(self):
        assert vh.current_state([]) is None
        assert vh.current_state(None) is None

    def test_returns_latest(self):
        pts = [vh.format_point(_d(30), "in"), vh.format_point(_d(5), "low")]
        assert vh.current_state(pts) == "low"


class TestDaysInState:
    def test_none_when_empty(self):
        assert vh.days_in_state([], TODAY) is None

    def test_run_length_since_last_change(self):
        pts = [vh.format_point(_d(30), "in"), vh.format_point(_d(5), "low")]
        assert vh.days_in_state(pts, TODAY) == 5

    def test_zero_on_the_day_it_flipped(self):
        pts = [vh.format_point(_d(10), "in"), vh.format_point(TODAY, "out")]
        assert vh.days_in_state(pts, TODAY) == 0

    def test_carry_in_keeps_original_since_date(self):
        """A state that has held since before the window still reports its true age."""
        pts = vh.prune([vh.format_point(_d(900), "in")], TODAY, 365)
        assert vh.days_in_state(pts, TODAY) == 900
