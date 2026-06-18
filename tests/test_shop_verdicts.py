"""Tests for src/shop_verdicts.py — the homepage verdict cache (cost lever #3).

Pure module: no network, no Claude. The index / lookup (read side) and upsert /
prune (write side) are exercised directly with fixed dates.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from src import shop_verdicts

TODAY = date(2026, 6, 9)


def _iso(d: date) -> str:
    """An ISO timestamp on day ``d`` (noon UTC) — the shape upsert writes."""
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------

class TestIndex:
    def test_keys_by_lowercased_shop(self):
        idx = shop_verdicts.index([{"shop": "Aniqi", "hash": "h"}])
        assert "aniqi" in idx and idx["aniqi"]["hash"] == "h"

    def test_skips_junk_and_shopless(self):
        idx = shop_verdicts.index([None, "x", {"hash": "h"}, {"shop": "  "}])
        assert idx == {}

    def test_later_duplicate_wins(self):
        idx = shop_verdicts.index([
            {"shop": "A", "hash": "old"}, {"shop": "a", "hash": "new"},
        ])
        assert idx["a"]["hash"] == "new"

    def test_none_passthrough(self):
        assert shop_verdicts.index(None) == {}


# ---------------------------------------------------------------------------
# lookup
# ---------------------------------------------------------------------------

class TestLookup:
    def _idx(self, **over):
        entry = {"shop": "Aniqi", "hash": "H1", "status": "yes",
                 "description": "30% off", "checked_at": _iso(TODAY)}
        entry.update(over)
        return shop_verdicts.index([entry])

    def test_hit_when_hash_matches_and_fresh(self):
        got = shop_verdicts.lookup(self._idx(), "Aniqi", "H1", TODAY)
        assert got is not None and got["status"] == "yes"

    def test_case_insensitive_shop(self):
        assert shop_verdicts.lookup(self._idx(), "ANIQI", "H1", TODAY) is not None

    def test_miss_on_hash_mismatch(self):
        assert shop_verdicts.lookup(self._idx(), "Aniqi", "H2", TODAY) is None

    def test_miss_when_shop_absent(self):
        assert shop_verdicts.lookup(self._idx(), "Other", "H1", TODAY) is None

    def test_miss_on_empty_hash(self):
        assert shop_verdicts.lookup(self._idx(), "Aniqi", "", TODAY) is None

    def test_miss_when_stale_past_ceiling(self):
        old = _iso(TODAY - timedelta(days=8))   # > _REUSE_MAX_AGE_DAYS (7)
        idx = self._idx(checked_at=old)
        assert shop_verdicts.lookup(idx, "Aniqi", "H1", TODAY) is None

    def test_hit_at_exactly_ceiling(self):
        edge = _iso(TODAY - timedelta(days=7))  # == ceiling → still reused
        idx = self._idx(checked_at=edge)
        assert shop_verdicts.lookup(idx, "Aniqi", "H1", TODAY) is not None

    def test_miss_when_checked_at_missing(self):
        idx = self._idx()
        idx["aniqi"].pop("checked_at")
        assert shop_verdicts.lookup(idx, "Aniqi", "H1", TODAY) is None

    def test_miss_when_checked_at_unparseable(self):
        idx = self._idx(checked_at="not-a-date")
        assert shop_verdicts.lookup(idx, "Aniqi", "H1", TODAY) is None


# ---------------------------------------------------------------------------
# upsert
# ---------------------------------------------------------------------------

class TestUpsert:
    NOW = _iso(TODAY)

    def test_appends_new_shop_with_now_stamp(self):
        out = shop_verdicts.upsert([], [
            {"shop": "Aniqi", "hash": "H1", "status": "yes",
             "description": "30% off"},
        ], self.NOW)
        assert out == [{
            "shop": "Aniqi", "hash": "H1", "status": "yes",
            "description": "30% off", "checked_at": self.NOW,
        }]

    def test_overwrites_existing_shop(self):
        prior = [{"shop": "Aniqi", "hash": "OLD", "status": "no",
                  "description": None, "checked_at": _iso(TODAY - timedelta(days=3))}]
        out = shop_verdicts.upsert(prior, [
            {"shop": "aniqi", "hash": "NEW", "status": "yes",
             "description": "50% off"},
        ], self.NOW)
        assert len(out) == 1
        assert out[0]["hash"] == "NEW" and out[0]["status"] == "yes"
        assert out[0]["checked_at"] == self.NOW

    def test_unjudged_prior_rides_along_untouched(self):
        # A cache HIT shop is not in `judged`; its entry must survive with its
        # ORIGINAL checked_at so the freshness ceiling counts from the last real
        # judgement, not the last hit.
        orig = _iso(TODAY - timedelta(days=4))
        prior = [{"shop": "Hokuro", "hash": "H", "status": "yes",
                  "description": "65% off", "checked_at": orig}]
        out = shop_verdicts.upsert(prior, [], self.NOW)
        assert out == prior
        assert out[0]["checked_at"] == orig

    def test_skips_judged_without_shop_or_hash(self):
        out = shop_verdicts.upsert([], [
            {"hash": "H", "status": "yes"},      # no shop
            {"shop": "X", "status": "yes"},      # no hash
        ], self.NOW)
        assert out == []

    def test_does_not_mutate_prior(self):
        prior = [{"shop": "A", "hash": "H", "status": "no",
                  "description": None, "checked_at": self.NOW}]
        shop_verdicts.upsert(prior, [
            {"shop": "A", "hash": "H2", "status": "yes", "description": "x"},
        ], self.NOW)
        assert prior[0]["hash"] == "H"   # original list/entry untouched

    def test_mixed_hit_and_miss(self):
        # Hokuro is a hit (rides along), Aniqi a miss (overwritten), Comfrt new.
        prior = [
            {"shop": "Hokuro", "hash": "HOK", "status": "yes",
             "description": "65% off", "checked_at": _iso(TODAY - timedelta(days=2))},
            {"shop": "Aniqi", "hash": "OLD", "status": "no",
             "description": None, "checked_at": _iso(TODAY - timedelta(days=2))},
        ]
        out = shop_verdicts.upsert(prior, [
            {"shop": "Aniqi", "hash": "NEW", "status": "yes", "description": "drop"},
            {"shop": "Comfrt", "hash": "CMF", "status": "yes", "description": "70% off"},
        ], self.NOW)
        by_shop = {e["shop"]: e for e in out}
        assert by_shop["Hokuro"]["checked_at"] == _iso(TODAY - timedelta(days=2))
        assert by_shop["Aniqi"]["hash"] == "NEW" and by_shop["Aniqi"]["checked_at"] == self.NOW
        assert by_shop["Comfrt"]["checked_at"] == self.NOW


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------

class TestPrune:
    def test_drops_entries_past_horizon(self):
        old = _iso(TODAY - timedelta(days=31))   # > _PRUNE_DAYS (30)
        out = shop_verdicts.prune([{"shop": "A", "hash": "H", "checked_at": old}], TODAY)
        assert out == []

    def test_keeps_recent_entries(self):
        recent = _iso(TODAY - timedelta(days=10))
        entry = {"shop": "A", "hash": "H", "checked_at": recent}
        assert shop_verdicts.prune([entry], TODAY) == [entry]

    def test_keeps_entry_at_exactly_horizon(self):
        edge = _iso(TODAY - timedelta(days=30))
        entry = {"shop": "A", "hash": "H", "checked_at": edge}
        assert shop_verdicts.prune([entry], TODAY) == [entry]

    def test_keeps_unparseable_checked_at(self):
        entry = {"shop": "A", "hash": "H", "checked_at": "not-a-date"}
        assert shop_verdicts.prune([entry], TODAY) == [entry]

    def test_keeps_missing_checked_at(self):
        entry = {"shop": "A", "hash": "H"}
        assert shop_verdicts.prune([entry], TODAY) == [entry]

    def test_drops_non_dict_junk(self):
        out = shop_verdicts.prune([None, "x", {"shop": "A", "hash": "H"}], TODAY)
        assert out == [{"shop": "A", "hash": "H"}]

    def test_empty_passthrough(self):
        assert shop_verdicts.prune([], TODAY) == []
        assert shop_verdicts.prune(None, TODAY) == []


# ---------------------------------------------------------------------------
# Lifecycle — the upsert→index→lookup contract across runs
# ---------------------------------------------------------------------------

class TestCacheLifecycle:
    def test_unchanged_sale_is_a_hit_next_run(self):
        store = shop_verdicts.upsert([], [
            {"shop": "Hokuro", "hash": "HASH_65OFF", "status": "yes",
             "description": "65% off"},
        ], _iso(TODAY))
        idx = shop_verdicts.index(store)
        assert shop_verdicts.lookup(idx, "Hokuro", "HASH_65OFF", TODAY) is not None

    def test_changed_sale_is_a_miss_next_run(self):
        store = shop_verdicts.upsert([], [
            {"shop": "Hokuro", "hash": "HASH_65OFF", "status": "yes",
             "description": "65% off"},
        ], _iso(TODAY))
        idx = shop_verdicts.index(store)
        # Promo changed (65→50% off) → different hash → miss → re-judged.
        assert shop_verdicts.lookup(idx, "Hokuro", "HASH_50OFF", TODAY) is None
