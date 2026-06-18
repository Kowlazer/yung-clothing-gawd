"""Tests for src/email_sales.py — the persisted email sale-announcement store."""
from __future__ import annotations

from datetime import date

from src import email_sales

_NOW = "2026-05-19T14:00:00+00:00"
_TODAY = date(2026, 5, 19)


# ---------------------------------------------------------------------------
# upsert
# ---------------------------------------------------------------------------

class TestUpsert:
    def test_new_yes_appended_with_timestamps(self):
        out = email_sales.upsert([], [
            {"email_id": "m1", "shop": "Aniqi", "status": "yes",
             "description": "30% off", "starts_on": "2026-05-24",
             "ends_on": "2026-05-26"},
        ], _NOW)
        assert len(out) == 1
        e = out[0]
        assert e["shop"] == "Aniqi"
        assert e["email_id"] == "m1"
        assert e["status"] == "yes"
        assert e["description"] == "30% off"
        assert e["starts_on"] == "2026-05-24"
        assert e["ends_on"] == "2026-05-26"
        assert e["first_seen"] == _NOW
        assert e["last_seen"] == _NOW

    def test_non_yes_judgements_skipped(self):
        out = email_sales.upsert([], [
            {"email_id": "m1", "shop": "Aniqi", "status": "no"},
            {"email_id": "m2", "shop": "Wooj", "status": "unclear"},
        ], _NOW)
        assert out == []

    def test_missing_shop_or_email_id_skipped(self):
        out = email_sales.upsert([], [
            {"email_id": "", "shop": "Aniqi", "status": "yes"},
            {"email_id": "m2", "shop": "", "status": "yes"},
        ], _NOW)
        assert out == []

    def test_dedupe_preserves_first_seen_bumps_last_seen(self):
        prior = [{
            "shop": "Aniqi", "email_id": "m1", "status": "yes",
            "description": "old", "starts_on": None, "ends_on": None,
            "first_seen": "2026-05-10T00:00:00+00:00",
            "last_seen": "2026-05-10T00:00:00+00:00",
        }]
        out = email_sales.upsert(prior, [
            {"email_id": "m1", "shop": "aniqi", "status": "yes",
             "description": "refreshed", "ends_on": "2026-05-30"},
        ], _NOW)
        assert len(out) == 1
        e = out[0]
        assert e["first_seen"] == "2026-05-10T00:00:00+00:00"   # preserved
        assert e["last_seen"] == _NOW                            # bumped
        assert e["description"] == "refreshed"
        assert e["ends_on"] == "2026-05-30"

    def test_reseen_empty_values_do_not_clobber(self):
        prior = [{
            "shop": "Aniqi", "email_id": "m1", "status": "yes",
            "description": "good", "starts_on": "2026-05-24", "ends_on": None,
            "first_seen": _NOW, "last_seen": _NOW,
        }]
        out = email_sales.upsert(prior, [
            {"email_id": "m1", "shop": "Aniqi", "status": "yes",
             "description": None, "starts_on": None, "ends_on": None},
        ], "2026-05-20T00:00:00+00:00")
        e = out[0]
        assert e["description"] == "good"        # not clobbered
        assert e["starts_on"] == "2026-05-24"    # not clobbered
        assert e["last_seen"] == "2026-05-20T00:00:00+00:00"

    def test_prior_entries_carried_over(self):
        prior = [{"shop": "Wooj", "email_id": "x", "status": "yes",
                  "description": "d", "starts_on": None, "ends_on": None,
                  "first_seen": _NOW, "last_seen": _NOW}]
        out = email_sales.upsert(prior, [
            {"email_id": "m1", "shop": "Aniqi", "status": "yes"},
        ], _NOW)
        shops = {e["shop"] for e in out}
        assert shops == {"Wooj", "Aniqi"}

    def test_bad_date_strings_normalised_to_none(self):
        out = email_sales.upsert([], [
            {"email_id": "m1", "shop": "Aniqi", "status": "yes",
             "starts_on": "not-a-date", "ends_on": "2026-13-99"},
        ], _NOW)
        assert out[0]["starts_on"] is None
        assert out[0]["ends_on"] is None


# ---------------------------------------------------------------------------
# is_expired / prune
# ---------------------------------------------------------------------------

class TestExpiry:
    def _entry(self, **kw):
        base = {"shop": "S", "email_id": "i", "status": "yes",
                "description": None, "starts_on": None, "ends_on": None,
                "first_seen": _NOW, "last_seen": _NOW}
        base.update(kw)
        return base

    def test_future_start_never_expires(self):
        e = self._entry(starts_on="2026-06-01")
        assert email_sales.is_expired(e, _TODAY) is False

    def test_future_start_with_mis_resolved_past_end_not_expired(self):
        # Claude transposed the window — an ends_on *before* a still-future
        # starts_on must not yank the advance sale (future start wins).
        e = self._entry(starts_on="2026-06-01", ends_on="2026-05-02")
        assert email_sales.is_expired(e, _TODAY) is False

    def test_ended_within_grace_kept(self):
        # ends yesterday, grace = 1 day → today == ends+grace, not strictly past.
        e = self._entry(ends_on="2026-05-18")
        assert email_sales.is_expired(e, _TODAY) is False

    def test_ended_past_grace_expires(self):
        e = self._entry(ends_on="2026-05-17")  # today is 19th, grace 1 → expired
        assert email_sales.is_expired(e, _TODAY) is True

    def test_start_only_treated_as_single_day_end(self):
        # start-only sale on the 17th: effective end = 17th, +1 grace → expired 19th
        e = self._entry(starts_on="2026-05-17")
        assert email_sales.is_expired(e, _TODAY) is True

    def test_undated_within_ttl_kept(self):
        e = self._entry(last_seen="2026-05-17T00:00:00+00:00")  # 2 days < TTL 4
        assert email_sales.is_expired(e, _TODAY) is False

    def test_undated_past_ttl_expires(self):
        e = self._entry(last_seen="2026-05-10T00:00:00+00:00")  # 9 days > TTL 4
        assert email_sales.is_expired(e, _TODAY) is True

    def test_unparseable_last_seen_kept(self):
        e = self._entry(last_seen="garbage")
        assert email_sales.is_expired(e, _TODAY) is False

    def test_prune_drops_expired_keeps_live(self):
        live = self._entry(email_id="live", ends_on="2026-05-30")
        dead = self._entry(email_id="dead", ends_on="2026-05-01")
        out = email_sales.prune([live, dead], _TODAY)
        assert [e["email_id"] for e in out] == ["live"]

    def test_prune_drops_non_dicts(self):
        out = email_sales.prune([None, "junk", self._entry(ends_on="2026-05-30")], _TODAY)
        assert len(out) == 1


# ---------------------------------------------------------------------------
# active (ordering)
# ---------------------------------------------------------------------------

class TestActive:
    def _entry(self, shop, **kw):
        base = {"shop": shop, "email_id": shop, "status": "yes",
                "description": None, "starts_on": None, "ends_on": None,
                "first_seen": _NOW, "last_seen": _NOW}
        base.update(kw)
        return base

    def test_upcoming_sorted_before_ongoing(self):
        ongoing = self._entry("Ongoing", ends_on="2026-05-25")
        soon = self._entry("Soon", starts_on="2026-05-21")
        later = self._entry("Later", starts_on="2026-05-28")
        out = email_sales.active([ongoing, later, soon], _TODAY)
        assert [e["shop"] for e in out] == ["Soon", "Later", "Ongoing"]

    def test_expired_filtered_out(self):
        live = self._entry("Live", ends_on="2026-05-30")
        dead = self._entry("Dead", ends_on="2026-05-01")
        out = email_sales.active([live, dead], _TODAY)
        assert [e["shop"] for e in out] == ["Live"]

    def test_start_today_groups_with_upcoming(self):
        # A sale starting *today* sorts with the upcoming group (it reads
        # "starts today"), ahead of ongoing sales — matching relative_days.
        today_start = self._entry("Today", starts_on="2026-05-19")  # == _TODAY
        tomorrow = self._entry("Tomorrow", starts_on="2026-05-20")
        ongoing = self._entry("Ongoing", ends_on="2026-05-25")
        out = email_sales.active([ongoing, tomorrow, today_start], _TODAY)
        assert [e["shop"] for e in out] == ["Today", "Tomorrow", "Ongoing"]


# ---------------------------------------------------------------------------
# relative_days (countdown phase)
# ---------------------------------------------------------------------------

class TestRelativeDays:
    def _entry(self, **kw):
        base = {"shop": "S", "email_id": "i", "starts_on": None, "ends_on": None,
                "last_seen": _NOW}
        base.update(kw)
        return base

    def test_future_start_is_upcoming(self):
        phase, days, on = email_sales.relative_days(
            self._entry(starts_on="2026-05-24"), _TODAY)
        assert phase == "upcoming"
        assert days == 5
        assert on == date(2026, 5, 24)

    def test_start_today_reads_as_upcoming_zero(self):
        # starts today + has an end today: must read "starts today", not "ends".
        phase, days, on = email_sales.relative_days(
            self._entry(starts_on="2026-05-19", ends_on="2026-05-19"), _TODAY)
        assert phase == "upcoming"
        assert days == 0

    def test_started_with_future_end_is_ending(self):
        phase, days, on = email_sales.relative_days(
            self._entry(starts_on="2026-05-15", ends_on="2026-05-22"), _TODAY)
        assert phase == "ending"
        assert days == 3
        assert on == date(2026, 5, 22)

    def test_undated_is_active(self):
        phase, days, on = email_sales.relative_days(self._entry(), _TODAY)
        assert phase == "active"
        assert days is None
        assert on is None

    def test_start_only_past_start_is_active(self):
        phase, days, on = email_sales.relative_days(
            self._entry(starts_on="2026-05-17"), _TODAY)
        assert phase == "active"
