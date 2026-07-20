"""Tests for src/price_history.py — the change-point price series.

Pure date math, no clock or network: every test passes an explicit ``today``.
"""

from __future__ import annotations

from datetime import date

from src import price_history as ph

TODAY = date(2026, 6, 10)


def _d(days_ago: int) -> date:
    from datetime import timedelta

    return TODAY - timedelta(days=days_ago)


# ---------------------------------------------------------------------------
# Token format / parse
# ---------------------------------------------------------------------------

class TestTokens:
    def test_whole_number_drops_trailing_zero(self):
        assert ph.format_point(date(2026, 6, 10), 50.0) == "2026-06-10:50"

    def test_decimal_preserved(self):
        assert ph.format_point(date(2026, 6, 10), 49.99) == "2026-06-10:49.99"

    def test_round_trip(self):
        tok = ph.format_point(date(2026, 1, 2), 19.95)
        assert ph.parse_point(tok) == (date(2026, 1, 2), 19.95)

    def test_parse_malformed_returns_none(self):
        assert ph.parse_point("not-a-point") is None
        assert ph.parse_point("2026-13-40:50") is None  # impossible date
        assert ph.parse_point("2026-06-10:abc") is None
        assert ph.parse_point(123) is None  # type: ignore[arg-type]

    def test_parse_history_sorts_and_drops_malformed(self):
        raw = ["2026-06-05:40", "bad", "2026-06-01:50"]
        assert ph.parse_history(raw) == [(date(2026, 6, 1), 50.0), (date(2026, 6, 5), 40.0)]

    def test_parse_history_empty(self):
        assert ph.parse_history(None) == []
        assert ph.parse_history([]) == []


# ---------------------------------------------------------------------------
# append_observation — change-point semantics
# ---------------------------------------------------------------------------

class TestAppend:
    def test_empty_seeds_first_point(self):
        assert ph.append_observation([], 50.0, TODAY) == ["2026-06-10:50"]

    def test_unchanged_price_is_noop(self):
        pts = [ph.format_point(_d(30), 50.0)]
        assert ph.append_observation(pts, 50.0, TODAY) == pts

    def test_changed_price_appends(self):
        pts = [ph.format_point(_d(30), 100.0)]
        out = ph.append_observation(pts, 50.0, TODAY)
        assert out == ["2026-05-11:100", "2026-06-10:50"]

    def test_same_day_change_replaces_not_stacks(self):
        """A second run the same day with a new price corrects today's point."""
        pts = ph.append_observation([ph.format_point(_d(5), 80.0)], 70.0, TODAY)
        # now two points, the second dated today
        out = ph.append_observation(pts, 60.0, TODAY)
        assert out == ["2026-06-05:80", "2026-06-10:60"]
        assert len(out) == 2  # today's point replaced, not duplicated

    def test_does_not_mutate_input(self):
        pts = [ph.format_point(_d(10), 100.0)]
        original = list(pts)
        ph.append_observation(pts, 50.0, TODAY)
        assert pts == original

    def test_rise_back_up_is_recorded(self):
        pts = [ph.format_point(_d(40), 100.0), ph.format_point(_d(20), 50.0)]
        out = ph.append_observation(pts, 100.0, TODAY)
        assert out[-1] == "2026-06-10:100"


# ---------------------------------------------------------------------------
# prune — retention window + carry-in
# ---------------------------------------------------------------------------

class TestPrune:
    def test_keeps_within_window(self):
        pts = [ph.format_point(_d(10), 50.0), ph.format_point(_d(2), 40.0)]
        assert ph.prune(pts, TODAY, 365) == pts

    def test_drops_old_but_keeps_one_carry_in(self):
        pts = [
            ph.format_point(_d(400), 100.0),  # before window — carry-in
            ph.format_point(_d(390), 90.0),   # before window — should drop
            ph.format_point(_d(10), 50.0),    # within window
        ]
        out = ph.prune(pts, TODAY, 365)
        # the 390-days-ago point is dropped; the 400-days-ago carry-in is kept
        assert out == [ph.format_point(_d(390), 90.0), ph.format_point(_d(10), 50.0)]

    def test_flat_ancient_point_is_retained(self):
        """A year-round price tracked for years stays as a single carry-in."""
        pts = [ph.format_point(_d(900), 50.0)]
        assert ph.prune(pts, TODAY, 365) == pts

    def test_empty(self):
        assert ph.prune([], TODAY, 365) == []


# ---------------------------------------------------------------------------
# baseline_max — trailing max over the window
# ---------------------------------------------------------------------------

class TestBaseline:
    def test_none_when_empty(self):
        assert ph.baseline_max([], TODAY, 90) is None

    def test_flat_price_baseline_equals_price(self):
        pts = [ph.format_point(_d(120), 50.0)]
        assert ph.baseline_max(pts, TODAY, 90) == 50.0

    def test_trailing_max_picks_recent_high(self):
        pts = [ph.format_point(_d(80), 100.0), ph.format_point(_d(5), 50.0)]
        assert ph.baseline_max(pts, TODAY, 90) == 100.0

    def test_carry_in_counts_when_high_predates_window(self):
        """A $100 price in effect before the 90d window opened still sets the
        baseline — it was the price as the window began."""
        pts = [ph.format_point(_d(200), 100.0), ph.format_point(_d(120), 100.0),
               ph.format_point(_d(10), 50.0)]
        assert ph.baseline_max(pts, TODAY, 90) == 100.0

    def test_old_high_excluded_once_superseded_within_window(self):
        """A spike that ended *inside* the window still counts (it was in effect
        during the window); but a low carry-in doesn't inflate the max."""
        pts = [ph.format_point(_d(120), 40.0),  # carry-in: $40
               ph.format_point(_d(30), 60.0),   # rose to $60 within window
               ph.format_point(_d(5), 45.0)]    # back to $45
        # window sees 40 (carry-in), 60, 45 -> max 60
        assert ph.baseline_max(pts, TODAY, 90) == 60.0


# ---------------------------------------------------------------------------
# price_standing — deal quality
# ---------------------------------------------------------------------------

class TestPriceStanding:
    def test_no_history_returns_none(self):
        assert ph.price_standing([], TODAY, 50.0) is None
        assert ph.price_standing(None, TODAY, 50.0) is None

    def test_too_shallow_makes_no_claim(self):
        """"Lowest in 9 days" reads like a fact and is nearly noise."""
        pts = [ph.format_point(_d(9), 60.0), ph.format_point(TODAY, 50.0)]
        assert ph.price_standing(pts, TODAY, 50.0) is None

    def test_shallow_threshold_is_tunable(self):
        pts = [ph.format_point(_d(9), 60.0), ph.format_point(TODAY, 50.0)]
        out = ph.price_standing(pts, TODAY, 50.0, min_tracked_days=5)
        assert out and out["is_lowest"] is True

    def test_flat_price_makes_no_claim(self):
        """A price that has never moved is trivially its own lowest — saying so
        implies a deal that doesn't exist. (The 2026-07-19 verification digest
        was full of "$48, not on sale, lowest in 40d" on never-moved items.)"""
        pts = [ph.format_point(_d(40), 48.0)]
        assert ph.price_standing(pts, TODAY, 48.0) is None

    def test_a_wobble_sized_rise_is_not_movement(self):
        pts = [ph.format_point(_d(40), 49.0), ph.format_point(TODAY, 48.0)]
        assert ph.price_standing(pts, TODAY, 48.0) is None

    def test_lowest_ever_seen(self):
        pts = [ph.format_point(_d(120), 80.0), ph.format_point(_d(40), 60.0),
               ph.format_point(TODAY, 50.0)]
        out = ph.price_standing(pts, TODAY, 50.0)
        assert out == {
            "tracked_days": 120,
            "is_lowest": True,
            "days_since_lower": None,
            "prior_low": None,
            "prior_low_on": None,
        }

    def test_equal_to_the_old_low_still_counts_as_lowest(self):
        """Matching the best price we've seen is not "worse than" it."""
        pts = [ph.format_point(_d(90), 40.0), ph.format_point(_d(60), 80.0),
               ph.format_point(TODAY, 40.0)]
        out = ph.price_standing(pts, TODAY, 40.0)
        assert out["is_lowest"] is True

    def test_reports_the_lower_price_and_when_it_held(self):
        pts = [ph.format_point(_d(100), 80.0),
               ph.format_point(_d(60), 32.0),   # the low, held 60d..41d ago
               ph.format_point(_d(40), 80.0),
               ph.format_point(TODAY, 50.0)]
        out = ph.price_standing(pts, TODAY, 50.0)
        assert out["is_lowest"] is False
        assert out["prior_low"] == 32.0
        assert out["prior_low_on"] == _d(60).isoformat()
        # The $32 run ended the day before the $80 point, i.e. 41 days ago.
        assert out["days_since_lower"] == 41

    def test_most_recent_occurrence_of_a_repeated_low_wins(self):
        """"Back in April" should mean the last April, not the first."""
        pts = [ph.format_point(_d(200), 30.0),
               ph.format_point(_d(150), 90.0),
               ph.format_point(_d(80), 30.0),   # same low, more recently
               ph.format_point(_d(50), 90.0),
               ph.format_point(TODAY, 60.0)]
        out = ph.price_standing(pts, TODAY, 60.0)
        assert out["prior_low"] == 30.0
        assert out["prior_low_on"] == _d(80).isoformat()
        assert out["days_since_lower"] == 51   # the $30 run ended _d(51)

    def test_only_prices_below_today_count_as_lower(self):
        pts = [ph.format_point(_d(90), 55.0), ph.format_point(TODAY, 50.0)]
        out = ph.price_standing(pts, TODAY, 50.0)
        assert out["is_lowest"] is True

    def test_trivial_wobble_is_not_a_missed_deal(self):
        """$87 today vs $86 in June is drift, not a deal worth reporting — a
        probe of the real state showed these would have dominated the output."""
        pts = [ph.format_point(_d(90), 86.0), ph.format_point(_d(60), 100.0),
               ph.format_point(TODAY, 87.0)]
        out = ph.price_standing(pts, TODAY, 87.0)
        assert out["is_lowest"] is True

    def test_fx_drift_band_is_excluded(self):
        """Regression guard for carmico.ca: a CAD shop recomputing USD prices
        daily drifts a few percent with no sale behind it ($87 today, $84 a
        fortnight ago). That band must stay silent."""
        pts = [ph.format_point(_d(40), 86.0), ph.format_point(_d(30), 84.0),
               ph.format_point(_d(20), 100.0), ph.format_point(TODAY, 87.0)]
        out = ph.price_standing(pts, TODAY, 87.0)
        assert out["is_lowest"] is True      # the $84 drift is not a missed deal

    def test_a_material_gap_still_reports(self):
        pts = [ph.format_point(_d(90), 74.0), ph.format_point(_d(60), 100.0),
               ph.format_point(TODAY, 88.0)]           # 16% above the old low
        out = ph.price_standing(pts, TODAY, 88.0)
        assert out["is_lowest"] is False
        assert out["prior_low"] == 74.0

    def test_gap_margin_is_tunable(self):
        pts = [ph.format_point(_d(90), 86.0), ph.format_point(_d(60), 100.0),
               ph.format_point(TODAY, 87.0)]
        out = ph.price_standing(pts, TODAY, 87.0, min_gap_pct=0.0)
        assert out["is_lowest"] is False       # every cent counts at 0%

    def test_zero_price_does_not_blow_up(self):
        pts = [ph.format_point(_d(90), 20.0), ph.format_point(TODAY, 0.0)]
        out = ph.price_standing(pts, TODAY, 0.0)
        assert out["is_lowest"] is True

    def test_tracked_days_spans_the_whole_retained_series(self):
        pts = [ph.format_point(_d(300), 80.0), ph.format_point(TODAY, 50.0)]
        assert ph.price_standing(pts, TODAY, 50.0)["tracked_days"] == 300

    def test_malformed_points_are_skipped_not_raised(self):
        pts = ["garbage", ph.format_point(_d(60), 80.0), None,
               ph.format_point(TODAY, 50.0)]
        out = ph.price_standing(pts, TODAY, 50.0)
        assert out["is_lowest"] is True and out["tracked_days"] == 60

    def test_future_dated_point_does_not_produce_negative_days(self):
        """Hand-edited / clock-skewed state must not yield nonsense output."""
        pts = [ph.format_point(_d(90), 30.0), ph.format_point(_d(-5), 80.0),
               ph.format_point(TODAY, 50.0)]
        out = ph.price_standing(pts, TODAY, 50.0)
        assert out["days_since_lower"] >= 0

    def test_same_day_superseded_price_never_held(self):
        """Two points on one date (legacy / hand-edited state): the first was
        superseded the same day, so it never actually held for a day and must
        not be reported as a price the item "was"."""
        pts = [ph.format_point(_d(60), 30.0), ph.format_point(_d(60), 90.0),
               ph.format_point(TODAY, 50.0)]
        out = ph.price_standing(pts, TODAY, 50.0)
        assert out["is_lowest"] is True
        assert out["days_since_lower"] is None
