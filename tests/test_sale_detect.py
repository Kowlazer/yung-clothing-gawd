"""Tests for src/sale_detect.py.

All tests are pure Python — no network, no fixtures. Two helpers build
canonical extracted-result and history-entry dicts, with keyword overrides
for the specific field each test cares about.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src import price_history as ph
from src.sale_detect import PriceRules, detect_sale

URL = "https://example.com/products/test-product"

TODAY = date(2026, 6, 10)


def _iso(d: date) -> str:
    return d.isoformat() + "T00:00:00Z"


def _ago(days: int) -> date:
    return TODAY - timedelta(days=days)


def _extracted(**overrides) -> dict:
    """Canonical successful extract result (in-stock, no sale, price $50)."""
    base = {
        "current_price": 50.0,
        "original_price": None,
        "currency": "USD",
        "on_sale": False,
        "out_of_stock": False,
        "low_stock": False,
        "label": "Test Product",
        "total_variant_count": 1,
        "available_variant_count": None,
        "color_options": [],
        "error": None,
        "error_kind": None,
    }
    base.update(overrides)
    return base


def _history(**overrides) -> dict:
    """Canonical prior prices.json entry (in-stock, no failures, price $50)."""
    base = {
        "label": "Test Product",
        "current_price": 50.0,
        "original_price": None,
        "currency": "USD",
        "in_stock": True,
        "low_stock": False,
        "last_checked": "2026-05-16T14:00:00Z",
        "last_seen": "2026-05-16T14:00:00Z",
        "consecutive_failures": 0,
        "last_error_kind": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# On-sale page signal
# ---------------------------------------------------------------------------

class TestOnSalePage:
    def test_on_sale_per_page_signal(self):
        r = detect_sale(URL, _extracted(on_sale=True, original_price=80.0), _history())
        assert r["sale_signal"] == "on_sale_per_page"

    def test_on_sale_no_error_signal(self):
        r = detect_sale(URL, _extracted(on_sale=True, original_price=80.0), _history())
        assert r["error_signal"] is None

    def test_on_sale_stock_signal_still_fires(self):
        """Sale and stock signals are independent — OOS item can also be on sale."""
        r = detect_sale(
            URL,
            _extracted(on_sale=True, original_price=80.0, out_of_stock=True),
            _history(in_stock=True),
        )
        assert r["sale_signal"] == "on_sale_per_page"
        assert r["stock_signal"] == "newly_out_of_stock"


# ---------------------------------------------------------------------------
# Price-drop signal
# ---------------------------------------------------------------------------

class TestPriceDrop:
    def test_price_dropped_signal(self):
        r = detect_sale(URL, _extracted(current_price=40.0), _history(current_price=80.0))
        assert r["sale_signal"] == "price_dropped"

    def test_price_dropped_prior_price_set(self):
        r = detect_sale(URL, _extracted(current_price=40.0), _history(current_price=80.0))
        assert r["prior_price"] == 80.0

    def test_no_drop_same_price(self):
        r = detect_sale(URL, _extracted(current_price=50.0), _history(current_price=50.0))
        assert r["sale_signal"] == "no_change"

    def test_no_drop_price_raised(self):
        r = detect_sale(URL, _extracted(current_price=60.0), _history(current_price=50.0))
        assert r["sale_signal"] == "no_change"

    def test_no_drop_first_run(self):
        """No history → can't compute a drop, report no_change."""
        r = detect_sale(URL, _extracted(current_price=50.0), {})
        assert r["sale_signal"] == "no_change"
        assert r["prior_price"] is None

    def test_on_sale_is_primary_signal_when_both_apply(self):
        """on_sale=True is the primary sale_signal, but prior_price is still set
        when there's also a drop from history — digest renders both facts."""
        r = detect_sale(
            URL,
            _extracted(current_price=40.0, on_sale=True, original_price=80.0),
            _history(current_price=50.0),
        )
        assert r["sale_signal"] == "on_sale_per_page"
        assert r["prior_price"] == 50.0

    def test_on_sale_with_no_history_drop_has_no_prior_price(self):
        """on_sale but price didn't drop vs. history → only on_sale_per_page, no prior_price."""
        r = detect_sale(
            URL,
            _extracted(current_price=40.0, on_sale=True, original_price=80.0),
            _history(current_price=40.0),
        )
        assert r["sale_signal"] == "on_sale_per_page"
        assert r["prior_price"] is None

    def test_no_history_price_no_drop(self):
        """history has no current_price → can't compare, report no_change."""
        r = detect_sale(URL, _extracted(current_price=50.0), _history(current_price=None))
        assert r["sale_signal"] == "no_change"

    def test_no_extracted_price_no_drop(self):
        """extracted has no current_price → can't compare, report no_change."""
        r = detect_sale(URL, _extracted(current_price=None), _history(current_price=50.0))
        assert r["sale_signal"] == "no_change"


# ---------------------------------------------------------------------------
# Stock transitions
# ---------------------------------------------------------------------------

class TestStockTransitions:
    def test_newly_out_of_stock(self):
        r = detect_sale(URL, _extracted(out_of_stock=True), _history(in_stock=True))
        assert r["stock_signal"] == "newly_out_of_stock"

    def test_back_in_stock(self):
        r = detect_sale(URL, _extracted(out_of_stock=False), _history(in_stock=False))
        assert r["stock_signal"] == "back_in_stock"

    def test_newly_low_stock(self):
        r = detect_sale(URL, _extracted(low_stock=True), _history(low_stock=False))
        assert r["stock_signal"] == "newly_low_stock"

    def test_low_stock_no_repeat(self):
        """Already low-stock last run → no transition signal this run."""
        r = detect_sale(URL, _extracted(low_stock=True), _history(low_stock=True))
        assert r["stock_signal"] is None

    def test_oos_cannot_trigger_low_stock(self):
        """Out-of-stock takes precedence — newly_low_stock only fires for in-stock items."""
        r = detect_sale(
            URL,
            _extracted(out_of_stock=True, low_stock=True),
            _history(in_stock=True, low_stock=False),
        )
        assert r["stock_signal"] == "newly_out_of_stock"

    def test_no_stock_change_no_signal(self):
        r = detect_sale(URL, _extracted(), _history())
        assert r["stock_signal"] is None

    def test_legacy_entry_no_in_stock_field(self):
        """Legacy entries without in_stock default to True — no false OOS alarm."""
        history = _history()
        del history["in_stock"]
        r = detect_sale(URL, _extracted(out_of_stock=False), history)
        assert r["stock_signal"] is None  # was implicitly in_stock, still in_stock


# ---------------------------------------------------------------------------
# Error transitions
# ---------------------------------------------------------------------------

class TestErrors:
    def test_not_found_is_removed_from_shop(self):
        r = detect_sale(URL, _extracted(error_kind="not_found", error="HTTP 404"), _history())
        assert r["error_signal"] == "removed_from_shop"

    def test_not_found_prunes_entry(self):
        r = detect_sale(URL, _extracted(error_kind="not_found", error="HTTP 404"), _history())
        assert r["updated_entry"] is None

    def test_not_found_no_sale_signal(self):
        r = detect_sale(URL, _extracted(error_kind="not_found", error="HTTP 404"), _history())
        assert r["sale_signal"] is None
        assert r["stock_signal"] is None

    def test_not_found_exposes_last_known(self):
        h = _history(current_price=75.0)
        r = detect_sale(URL, _extracted(error_kind="not_found", error="HTTP 404"), h)
        assert r["last_known"] == h

    def test_blocked_is_could_not_check(self):
        r = detect_sale(URL, _extracted(error_kind="blocked", error="HTTP 403"), _history())
        assert r["error_signal"] == "could_not_check"

    def test_timeout_is_could_not_check(self):
        r = detect_sale(URL, _extracted(error_kind="timeout", error="fetch failed"), _history())
        assert r["error_signal"] == "could_not_check"

    def test_server_error_is_could_not_check(self):
        r = detect_sale(URL, _extracted(error_kind="server_error", error="HTTP 500"), _history())
        assert r["error_signal"] == "could_not_check"

    def test_other_error_is_could_not_check(self):
        r = detect_sale(URL, _extracted(error_kind="other", error="connection failed"), _history())
        assert r["error_signal"] == "could_not_check"

    def test_failure_increments_counter(self):
        r = detect_sale(
            URL,
            _extracted(error_kind="blocked", error="HTTP 403"),
            _history(consecutive_failures=2),
        )
        assert r["updated_entry"]["consecutive_failures"] == 3

    def test_first_failure_sets_counter_to_one(self):
        """History has no consecutive_failures field (legacy entry) → starts at 1."""
        history = _history()
        del history["consecutive_failures"]
        r = detect_sale(URL, _extracted(error_kind="blocked", error="HTTP 403"), history)
        assert r["updated_entry"]["consecutive_failures"] == 1

    def test_could_not_check_sets_last_error_kind(self):
        r = detect_sale(URL, _extracted(error_kind="timeout", error="timed out"), _history())
        assert r["updated_entry"]["last_error_kind"] == "timeout"

    def test_could_not_check_bumps_last_seen(self):
        r = detect_sale(URL, _extracted(error_kind="blocked", error="HTTP 403"), _history())
        assert r["updated_entry"]["last_seen"] != "2026-05-16T14:00:00Z"

    def test_could_not_check_preserves_last_checked(self):
        """last_checked is only bumped on success — must stay unchanged on error."""
        r = detect_sale(URL, _extracted(error_kind="blocked", error="HTTP 403"), _history())
        assert r["updated_entry"]["last_checked"] == "2026-05-16T14:00:00Z"

    def test_could_not_check_has_last_known(self):
        h = _history(current_price=70.0)
        r = detect_sale(URL, _extracted(error_kind="timeout", error="timed out"), h)
        assert r["last_known"] == h

    def test_empty_history_on_first_failure(self):
        """First-ever fetch fails → could_not_check with empty last_known."""
        r = detect_sale(URL, _extracted(error_kind="blocked", error="HTTP 403"), {})
        assert r["error_signal"] == "could_not_check"
        assert r["last_known"] is None


# ---------------------------------------------------------------------------
# Updated entry shape (success path)
# ---------------------------------------------------------------------------

class TestUpdatedEntry:
    def result(self) -> dict:
        return detect_sale(URL, _extracted(), _history(consecutive_failures=3))

    def test_resets_consecutive_failures(self):
        assert self.result()["updated_entry"]["consecutive_failures"] == 0

    def test_resets_last_error_kind(self):
        assert self.result()["updated_entry"]["last_error_kind"] is None

    def test_sets_last_checked(self):
        entry = self.result()["updated_entry"]
        assert entry["last_checked"] and "Z" in entry["last_checked"]

    def test_sets_last_seen(self):
        entry = self.result()["updated_entry"]
        assert entry["last_seen"] and "Z" in entry["last_seen"]

    def test_last_checked_equals_last_seen_on_success(self):
        entry = self.result()["updated_entry"]
        assert entry["last_checked"] == entry["last_seen"]

    def test_carries_price_fields(self):
        r = detect_sale(URL, _extracted(current_price=65.0, original_price=80.0), _history())
        entry = r["updated_entry"]
        assert entry["current_price"] == 65.0
        assert entry["original_price"] == 80.0

    def test_label_from_extracted(self):
        r = detect_sale(URL, _extracted(label="Cool Shorts"), _history(label="Old Label"))
        assert r["updated_entry"]["label"] == "Cool Shorts"

    def test_label_falls_back_to_history(self):
        """extract() sometimes returns label=None (blocked page) — use history label."""
        r = detect_sale(URL, _extracted(label=None), _history(label="History Label"))
        assert r["updated_entry"]["label"] == "History Label"

    def test_in_stock_field_set(self):
        r = detect_sale(URL, _extracted(out_of_stock=True), _history())
        assert r["updated_entry"]["in_stock"] is False

    def test_low_stock_field_set(self):
        r = detect_sale(URL, _extracted(low_stock=True), _history())
        assert r["updated_entry"]["low_stock"] is True

    def test_no_error_signal_on_success(self):
        r = detect_sale(URL, _extracted(), _history())
        assert r["error_signal"] is None

    def test_last_known_is_none_on_success(self):
        r = detect_sale(URL, _extracted(), _history())
        assert r["last_known"] is None


# ---------------------------------------------------------------------------
# Size-aware fields persist on updated_entry
#
# The digest renders 'only in L' / 'in stock in M, L' notes from these fields
# without re-deriving anything. Confirm sale_detect carries them through.
# ---------------------------------------------------------------------------

class TestUpdatedEntrySizeFields:
    def test_persists_size_options(self):
        r = detect_sale(
            URL,
            _extracted(size_options=["S", "M", "L", "XL"]),
            _history(),
        )
        assert r["updated_entry"]["size_options"] == ["S", "M", "L", "XL"]

    def test_persists_available_sizes(self):
        r = detect_sale(
            URL,
            _extracted(available_sizes=["L"]),
            _history(),
        )
        assert r["updated_entry"]["available_sizes"] == ["L"]

    def test_persists_preferred_sizes_applied(self):
        """Lets the digest know which preference list (tops vs. pants) ran
        for this URL without re-detecting garment type."""
        r = detect_sale(
            URL,
            _extracted(preferred_sizes_applied=["S", "M", "L"]),
            _history(),
        )
        assert r["updated_entry"]["preferred_sizes_applied"] == ["S", "M", "L"]

    def test_missing_fields_default_to_empty(self):
        """Legacy extract result (no size keys) → empty lists, not None."""
        r = detect_sale(URL, _extracted(), _history())
        entry = r["updated_entry"]
        assert entry["size_options"] == []
        assert entry["available_sizes"] == []
        assert entry["preferred_sizes_applied"] == []


# ---------------------------------------------------------------------------
# Standing-discount vs. genuine markdown (change-point price history)
#
# A page that advertises "was $X" every single day is a year-round anchor, not
# a real sale. detect_sale tells them apart by comparing today's price to the
# item's own trailing-max baseline over its observed history.
# ---------------------------------------------------------------------------

def _tracked_history(points, *, first_seen_days_ago, current_price, **overrides):
    """A prices.json entry WITH change-point history + first_seen stamped."""
    return _history(
        current_price=current_price,
        first_seen=_iso(_ago(first_seen_days_ago)),
        price_history=[ph.format_point(d, p) for d, p in points],
        **overrides,
    )


def _detect(extracted, history):
    return detect_sale(URL, extracted, history, today=TODAY, rules=PriceRules())


class TestStandingDiscount:
    def test_flat_anchor_is_standing_discount(self):
        """Page says 'was $100' but we've only ever seen $50 → standing discount."""
        hist = _tracked_history(
            [(_ago(120), 50.0)], first_seen_days_ago=120, current_price=50.0,
        )
        r = _detect(_extracted(current_price=50.0, on_sale=True, original_price=100.0), hist)
        assert r["sale_signal"] == "standing_discount"

    def test_real_drop_below_baseline_is_on_sale(self):
        """Held $100 for months, now $50 → genuine markdown, not standing."""
        hist = _tracked_history(
            [(_ago(120), 100.0)], first_seen_days_ago=120, current_price=100.0,
        )
        r = _detect(_extracted(current_price=50.0, on_sale=True, original_price=100.0), hist)
        assert r["sale_signal"] == "on_sale_per_page"
        assert r["prior_price"] == 100.0

    def test_observed_drop_overrides_margin(self):
        """Even a sub-margin move counts as genuine when the price actually fell
        since the last check (prior_price is set)."""
        hist = _tracked_history(
            [(_ago(120), 50.0)], first_seen_days_ago=120, current_price=50.0,
        )
        r = _detect(_extracted(current_price=49.5, on_sale=True, original_price=100.0), hist)
        assert r["sale_signal"] == "on_sale_per_page"

    def test_within_margin_no_prior_is_standing(self):
        """49.50 against a $50 baseline (1% < 2% margin) with no fresh drop → standing."""
        hist = _tracked_history(
            [(_ago(120), 50.0), (_ago(30), 49.5)],
            first_seen_days_ago=120, current_price=49.5,
        )
        r = _detect(_extracted(current_price=49.5, on_sale=True, original_price=100.0), hist)
        assert r["sale_signal"] == "standing_discount"

    def test_below_margin_no_prior_is_genuine(self):
        """$48 against a $50 baseline (4% > 2% margin), no fresh drop → genuine."""
        hist = _tracked_history(
            [(_ago(120), 50.0), (_ago(30), 48.0)],
            first_seen_days_ago=120, current_price=48.0,
        )
        r = _detect(_extracted(current_price=48.0, on_sale=True, original_price=100.0), hist)
        assert r["sale_signal"] == "on_sale_per_page"

    def test_insufficient_history_falls_back_to_page(self):
        """Tracked only 3 days → can't judge, trust the page markdown as before."""
        hist = _tracked_history(
            [(_ago(3), 50.0)], first_seen_days_ago=3, current_price=50.0,
        )
        r = _detect(_extracted(current_price=50.0, on_sale=True, original_price=100.0), hist)
        assert r["sale_signal"] == "on_sale_per_page"

    def test_legacy_entry_without_first_seen_falls_back(self):
        """An entry from before the history feature (no first_seen) is taken at
        face value, so nothing reclassifies on the first post-deploy run."""
        legacy = _history(current_price=50.0)  # no first_seen / price_history
        r = _detect(_extracted(current_price=50.0, on_sale=True, original_price=100.0), legacy)
        assert r["sale_signal"] == "on_sale_per_page"

    def test_standing_only_applies_when_page_on_sale(self):
        """No page markdown → never standing_discount (it's a sale-signal refinement)."""
        hist = _tracked_history(
            [(_ago(120), 50.0)], first_seen_days_ago=120, current_price=50.0,
        )
        r = _detect(_extracted(current_price=50.0, on_sale=False), hist)
        assert r["sale_signal"] == "no_change"


class TestPriceHistoryPersistence:
    def test_first_seen_stamped_on_new_entry(self):
        r = detect_sale(URL, _extracted(current_price=50.0), {}, today=TODAY)
        assert r["updated_entry"]["first_seen"]  # set to now

    def test_first_seen_preserved_across_runs(self):
        hist = _tracked_history(
            [(_ago(120), 50.0)], first_seen_days_ago=120, current_price=50.0,
        )
        r = _detect(_extracted(current_price=50.0), hist)
        assert r["updated_entry"]["first_seen"] == _iso(_ago(120))

    def test_history_appends_change_point_on_drop(self):
        hist = _tracked_history(
            [(_ago(120), 100.0)], first_seen_days_ago=120, current_price=100.0,
        )
        r = _detect(_extracted(current_price=50.0), hist)
        hist_out = r["updated_entry"]["price_history"]
        assert hist_out[-1] == ph.format_point(TODAY, 50.0)
        assert len(hist_out) == 2

    def test_history_unchanged_when_price_flat(self):
        pts = [(_ago(120), 50.0)]
        hist = _tracked_history(pts, first_seen_days_ago=120, current_price=50.0)
        r = _detect(_extracted(current_price=50.0), hist)
        assert r["updated_entry"]["price_history"] == [ph.format_point(_ago(120), 50.0)]

    def test_baseline_exposed_in_result(self):
        hist = _tracked_history(
            [(_ago(120), 100.0)], first_seen_days_ago=120, current_price=100.0,
        )
        r = _detect(_extracted(current_price=50.0), hist)
        assert r["baseline_price"] == 100.0
        assert r["baseline_days"] == 90


# ---------------------------------------------------------------------------
# Per-variant (size/colour) state history + transitions
# ---------------------------------------------------------------------------

from src import variant_history as vh  # noqa: E402


def _size_variants(options, available, low=()):
    """A `variants` snapshot with a single size dimension."""
    return {"size": {"options": list(options), "available": list(available),
                     "low": list(low)}}


class TestVariantTracking:
    def test_first_sight_seeds_history_no_transition(self):
        """No prior variant_history → every value is newly seen, no transitions,
        but each value's series is seeded with today's state."""
        ex = _extracted(variants=_size_variants(["M", "L"], ["M", "L"]))
        r = _detect(ex, _history())
        assert r["variant_changes"] == {}
        vhist = r["updated_entry"]["variant_history"]["size"]
        assert vhist["M"] == [vh.format_point(TODAY, "in")]
        assert vhist["L"] == [vh.format_point(TODAY, "in")]

    def test_size_sells_out_emits_transition(self):
        prior = {"size": {"M": [vh.format_point(_ago(30), "in")],
                          "L": [vh.format_point(_ago(30), "in")]}}
        ex = _extracted(variants=_size_variants(["M", "L"], ["L"]))  # M gone
        r = _detect(ex, _history(variant_history=prior))
        assert r["variant_changes"]["size"] == [{"value": "M", "from": "in", "to": "out"}]
        # M's series gains a change-point dated today; L unchanged (no-op append).
        assert r["updated_entry"]["variant_history"]["size"]["M"] == [
            vh.format_point(_ago(30), "in"), vh.format_point(TODAY, "out")]
        assert r["updated_entry"]["variant_history"]["size"]["L"] == [
            vh.format_point(_ago(30), "in")]

    def test_back_in_stock_and_low_transitions(self):
        prior = {"size": {"M": [vh.format_point(_ago(10), "out")],
                          "L": [vh.format_point(_ago(10), "in")]}}
        ex = _extracted(variants=_size_variants(["M", "L"], ["M", "L"], low=["L"]))
        r = _detect(ex, _history(variant_history=prior))
        changes = {c["value"]: (c["from"], c["to"]) for c in r["variant_changes"]["size"]}
        assert changes == {"M": ("out", "in"), "L": ("in", "low")}

    def test_no_change_is_noop(self):
        prior = {"size": {"M": [vh.format_point(_ago(30), "in")]}}
        ex = _extracted(variants=_size_variants(["M"], ["M"]))
        r = _detect(ex, _history(variant_history=prior))
        assert r["variant_changes"] == {}
        assert r["updated_entry"]["variant_history"]["size"]["M"] == [
            vh.format_point(_ago(30), "in")]

    def test_color_transition_tracked_independently(self):
        prior = {"color": {"Black": [vh.format_point(_ago(5), "in")],
                           "Red": [vh.format_point(_ago(5), "in")]}}
        ex = _extracted(variants={"color": {"options": ["Black", "Red"],
                                            "available": ["Black"], "low": []}})
        r = _detect(ex, _history(variant_history=prior))
        assert r["variant_changes"]["color"] == [{"value": "Red", "from": "in", "to": "out"}]

    def test_empty_variants_carries_prior_history_forward(self):
        """A run with no per-variant data must not wipe an existing timeline."""
        prior = {"size": {"M": [vh.format_point(_ago(30), "in")]}}
        r = _detect(_extracted(), _history(variant_history=prior))  # no variants key
        assert r["updated_entry"]["variant_history"] == prior
        assert r["variant_changes"] == {}

    def test_discontinued_value_is_dropped_not_marked_out(self):
        """A value no longer offered vanishes from history — discontinued is not
        a sell-out, so it must not emit an 'out' transition."""
        prior = {"size": {"M": [vh.format_point(_ago(30), "in")],
                          "XS": [vh.format_point(_ago(30), "in")]}}
        ex = _extracted(variants=_size_variants(["M"], ["M"]))  # XS no longer offered
        r = _detect(ex, _history(variant_history=prior))
        assert "XS" not in r["updated_entry"]["variant_history"]["size"]
        assert r["variant_changes"] == {}

    def test_retention_prunes_old_change_points(self):
        prior = {"size": {"M": [vh.format_point(_ago(400), "in")]}}
        ex = _extracted(variants=_size_variants(["M"], [], ))  # now out
        rules = PriceRules(variant_retention_days=365)
        r = detect_sale(URL, ex, _history(variant_history=prior), today=TODAY, rules=rules)
        series = r["updated_entry"]["variant_history"]["size"]["M"]
        # carry-in (the 400-day-ago 'in') + today's 'out'
        assert series == [vh.format_point(_ago(400), "in"), vh.format_point(TODAY, "out")]


# ---------------------------------------------------------------------------
# price_standing passthrough — deal quality rides on the same series
# ---------------------------------------------------------------------------

class TestPriceStandingPassthrough:
    def test_new_low_is_reported_as_lowest(self):
        hist = _tracked_history(
            [(_ago(120), 80.0), (_ago(40), 60.0)],
            first_seen_days_ago=120, current_price=60.0,
        )
        r = _detect(_extracted(current_price=45.0), hist)
        st = r["price_standing"]
        assert st["is_lowest"] is True
        assert st["tracked_days"] == 120

    def test_matching_an_older_low_is_not_the_lowest(self):
        hist = _tracked_history(
            [(_ago(120), 80.0), (_ago(90), 32.0), (_ago(60), 80.0)],
            first_seen_days_ago=120, current_price=80.0,
        )
        r = _detect(_extracted(current_price=50.0), hist)
        st = r["price_standing"]
        assert st["is_lowest"] is False
        assert st["prior_low"] == 32.0
        assert st["prior_low_on"] == _ago(90).isoformat()

    def test_shallow_history_makes_no_claim(self):
        hist = _tracked_history(
            [(_ago(3), 80.0)], first_seen_days_ago=3, current_price=80.0,
        )
        r = _detect(_extracted(current_price=50.0), hist)
        assert r["price_standing"] is None

    def test_error_path_carries_no_claim(self):
        """A blocked fetch has no price to rank — the key must be absent, not stale."""
        hist = _tracked_history(
            [(_ago(120), 80.0)], first_seen_days_ago=120, current_price=80.0,
        )
        r = detect_sale(URL, {"error_kind": "blocked"}, hist, today=TODAY)
        assert r.get("price_standing") is None

    def test_it_is_ranked_against_today_not_yesterday(self):
        """Today's observation is folded into the series before ranking, so a
        fresh all-time low reads as the lowest rather than as beaten by itself."""
        hist = _tracked_history(
            [(_ago(120), 80.0), (_ago(2), 55.0)],
            first_seen_days_ago=120, current_price=55.0,
        )
        r = _detect(_extracted(current_price=40.0), hist)
        assert r["price_standing"]["is_lowest"] is True
