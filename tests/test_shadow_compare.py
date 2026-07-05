"""Tests for src/shadow_compare.py + src/shadow_report.py (cost lever #5)."""
from __future__ import annotations

from datetime import date

from src import shadow_compare
from src.shadow_compare import append_run, compare, prune, summarize
from src.shadow_report import _risk_split, format_report


def _shop(task_id, shop, status, description=None):
    return {"id": task_id, "shop": shop, "status": status,
            "description": description}


def _tool_input(shop_sales=None, resolutions=None, loose_matches=None,
                email_sales=None):
    return {
        "shop_sales": shop_sales or [],
        "resolutions": resolutions or [],
        "loose_matches": loose_matches or [],
        "email_sales": email_sales or [],
    }


# ---------------------------------------------------------------------------
# compare — agreement semantics
# ---------------------------------------------------------------------------

class TestCompare:
    def test_identical_statuses_agree(self):
        primary = _tool_input(shop_sales=[_shop("shop_0", "Aniqi", "yes", "30% off")])
        shadow = _tool_input(shop_sales=[_shop("shop_0", "Aniqi", "yes", "30% sitewide")])
        out = compare(primary, shadow)
        assert out["summary"]["total"] == 1
        assert out["summary"]["agree"] == 1
        assert out["disagreements"] == []

    def test_description_wording_never_scored(self):
        # Same status, wildly different descriptions → still agreement.
        primary = _tool_input(shop_sales=[_shop("shop_0", "Aniqi", "yes", "a")])
        shadow = _tool_input(shop_sales=[_shop("shop_0", "Aniqi", "yes", None)])
        assert compare(primary, shadow)["summary"]["agree"] == 1

    def test_status_diff_is_disagreement(self):
        primary = _tool_input(shop_sales=[_shop("shop_0", "Aniqi", "no")])
        shadow = _tool_input(shop_sales=[_shop("shop_0", "Aniqi", "yes", "30% off")])
        out = compare(primary, shadow)
        assert out["summary"]["agree"] == 0
        d = out["disagreements"][0]
        assert d["type"] == "shop_sales"
        assert d["key"] == "Aniqi"
        assert d["primary"]["status"] == "no"
        assert d["shadow"]["status"] == "yes"

    def test_status_case_insensitive(self):
        primary = _tool_input(shop_sales=[_shop("shop_0", "Aniqi", "Yes")])
        shadow = _tool_input(shop_sales=[_shop("shop_0", "Aniqi", "yes")])
        assert compare(primary, shadow)["summary"]["agree"] == 1

    def test_email_dates_are_scored(self):
        # Same status but a different resolved end date → disagreement (the
        # dates drive the email-sale persistence window).
        primary = _tool_input(email_sales=[{
            "id": "email_0", "email_id": "m1", "shop": "Aniqi",
            "status": "yes", "starts_on": None, "ends_on": "2026-07-06",
        }])
        shadow = _tool_input(email_sales=[{
            "id": "email_0", "email_id": "m1", "shop": "Aniqi",
            "status": "yes", "starts_on": None, "ends_on": "2026-07-07",
        }])
        out = compare(primary, shadow)
        assert out["summary"]["agree"] == 0
        assert out["disagreements"][0]["type"] == "email_sales"

    def test_resolution_url_trailing_slash_tolerated(self):
        primary = _tool_input(resolutions=[{
            "id": "resolve_0", "shop_name": "Greyfox",
            "url": "https://greyfox.com/", "confidence": "high",
        }])
        shadow = _tool_input(resolutions=[{
            "id": "resolve_0", "shop_name": "Greyfox",
            "url": "https://greyfox.com", "confidence": "low",
        }])
        # URL matches after normalisation; confidence is recorded, not scored.
        assert compare(primary, shadow)["summary"]["agree"] == 1

    def test_loose_match_url_diff_is_disagreement(self):
        primary = _tool_input(loose_matches=[{
            "id": "loose_0", "mention": "Law pants", "shop": "Aniqi",
            "matched_url": "https://aniqi.com/products/law-joggers",
            "confidence": "high",
        }])
        shadow = _tool_input(loose_matches=[{
            "id": "loose_0", "mention": "Law pants", "shop": "Aniqi",
            "matched_url": None, "confidence": "none",
        }])
        out = compare(primary, shadow)
        assert out["summary"]["agree"] == 0
        assert out["disagreements"][0]["key"] == "Law pants"

    def test_missing_shadow_entry_is_disagreement(self):
        # e.g. the shadow response truncated at the output cap and dropped it.
        primary = _tool_input(shop_sales=[_shop("shop_0", "Aniqi", "no"),
                                          _shop("shop_1", "Greyfox", "yes", "x")])
        shadow = _tool_input(shop_sales=[_shop("shop_0", "Aniqi", "no")])
        out = compare(primary, shadow)
        assert out["summary"]["total"] == 2
        assert out["summary"]["agree"] == 1
        d = out["disagreements"][0]
        assert d["shadow"] is None
        assert d["primary"]["status"] == "yes"

    def test_extra_shadow_entry_is_disagreement(self):
        primary = _tool_input()
        shadow = _tool_input(shop_sales=[_shop("shop_9", "Ghost", "yes", "?")])
        out = compare(primary, shadow)
        assert out["summary"]["total"] == 1
        assert out["disagreements"][0]["primary"] is None

    def test_by_type_counts_only_present_types(self):
        primary = _tool_input(shop_sales=[_shop("shop_0", "A", "no")])
        shadow = _tool_input(shop_sales=[_shop("shop_0", "A", "no")])
        out = compare(primary, shadow)
        assert out["summary"]["by_type"] == {
            "shop_sales": {"total": 1, "agree": 1},
        }

    def test_junk_entries_skipped(self):
        primary = {"shop_sales": ["junk", {"no": "id"},
                                  _shop("shop_0", "A", "no")]}
        shadow = {"shop_sales": [_shop("shop_0", "A", "no")], "resolutions": None}
        out = compare(primary, shadow)
        assert out["summary"] == {
            "total": 1, "agree": 1,
            "by_type": {"shop_sales": {"total": 1, "agree": 1}},
        }


# ---------------------------------------------------------------------------
# Store lifecycle
# ---------------------------------------------------------------------------

class TestStoreLifecycle:
    def test_append_to_empty(self):
        store = append_run({}, {"at": "2026-07-05T14:00:00+00:00"})
        assert len(store["runs"]) == 1

    def test_append_preserves_prior_and_drops_junk(self):
        prior = {"runs": [{"at": "2026-07-04"}, "junk"]}
        store = append_run(prior, {"at": "2026-07-05"})
        assert [r["at"] for r in store["runs"]] == ["2026-07-04", "2026-07-05"]

    def test_prune_drops_old_runs(self):
        store = {"runs": [
            {"at": "2026-05-01T14:00:00+00:00"},   # 65 days old — pruned
            {"at": "2026-07-01T14:00:00+00:00"},   # fresh — kept
            {"at": "not-a-date"},                   # unparseable — kept (safe)
        ]}
        out = prune(store, today=date(2026, 7, 5))
        assert [r["at"] for r in out["runs"]] == [
            "2026-07-01T14:00:00+00:00", "not-a-date",
        ]

    def test_prune_boundary_uses_retention_days(self):
        assert shadow_compare._RETENTION_DAYS == 30
        store = {"runs": [{"at": "2026-06-05"}]}  # exactly 30 days — kept
        assert prune(store, today=date(2026, 7, 5))["runs"]
        store = {"runs": [{"at": "2026-06-04"}]}  # 31 days — pruned
        assert not prune(store, today=date(2026, 7, 5))["runs"]


# ---------------------------------------------------------------------------
# summarize + report
# ---------------------------------------------------------------------------

def _run(at, agree, total, disagreements=None, primary_usage=None,
         shadow_usage=None):
    return {
        "at": at,
        "primary_model": "claude-sonnet-4-6",
        "shadow_model": "claude-haiku-4-5-20251001",
        "summary": {"total": total, "agree": agree,
                    "by_type": {"shop_sales": {"total": total, "agree": agree}}},
        "disagreements": disagreements or [],
        "primary_usage": primary_usage or {"input_tokens": 50_000,
                                           "output_tokens": 4_000},
        "shadow_usage": shadow_usage or {"input_tokens": 50_000,
                                         "output_tokens": 4_000},
    }


class TestSummarize:
    def test_aggregates_counts_usage_and_stamps_disagreements(self):
        store = {"runs": [
            _run("2026-07-05T14:00:00+00:00", 9, 10, disagreements=[{
                "type": "shop_sales", "key": "Aniqi",
                "primary": {"status": "no"}, "shadow": {"status": "yes"},
            }]),
            _run("2026-07-06T14:00:00+00:00", 10, 10),
        ]}
        agg = summarize(store)
        assert agg["runs"] == 2
        assert (agg["total"], agg["agree"]) == (20, 19)
        assert agg["by_type"]["shop_sales"] == {"total": 20, "agree": 19}
        assert agg["primary_usage"]["input_tokens"] == 100_000
        assert agg["disagreements"][0]["at"] == "2026-07-05T14:00:00+00:00"
        assert agg["first_at"].startswith("2026-07-05")
        assert agg["last_at"].startswith("2026-07-06")

    def test_empty_store(self):
        agg = summarize({})
        assert agg["runs"] == 0
        assert agg["total"] == 0


class TestRiskSplit:
    def test_classifies_three_ways(self):
        disagreements = [
            # shadow yes vs primary no → false-positive risk
            {"type": "shop_sales", "primary": {"status": "no"},
             "shadow": {"status": "yes"}},
            # primary yes vs shadow unclear → missed-sale risk
            {"type": "email_sales", "primary": {"status": "yes"},
             "shadow": {"status": "unclear"}},
            # no↔unclear wobble → other
            {"type": "shop_sales", "primary": {"status": "unclear"},
             "shadow": {"status": "no"}},
            # url diff → other
            {"type": "resolutions", "primary": {"url": "https://a.com"},
             "shadow": {"url": "https://b.com"}},
            # missing side → other
            {"type": "shop_sales", "primary": {"status": "yes"},
             "shadow": None},
        ]
        assert _risk_split(disagreements) == (1, 1, 3)


class TestFormatReport:
    def test_empty_store_message(self):
        assert "No shadow runs" in format_report({})

    def test_report_carries_the_decision_surface(self):
        store = {"runs": [_run("2026-07-05T14:00:00+00:00", 9, 10,
                               disagreements=[{
                                   "type": "shop_sales", "key": "Aniqi",
                                   "primary": {"status": "no"},
                                   "shadow": {"status": "yes",
                                              "description": "30% off"},
                               }])]}
        report = format_report(store)
        assert "9/10" in report and "90%" in report
        assert "false-positive risk): 1" in report
        assert "Aniqi" in report
        assert "status=no" in report and "status=yes" in report
        # Cost line: Sonnet 50K in + 4K out = $0.21; Haiku same usage = $0.07.
        assert "$0.21" in report and "$0.07" in report
        assert "~67%" in report

    def test_perfect_agreement_report(self):
        store = {"runs": [_run("2026-07-05", 10, 10)]}
        assert "none — every compared verdict matched" in format_report(store)
