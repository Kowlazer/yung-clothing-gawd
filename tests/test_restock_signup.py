"""Tests for src/restock_signup.py.

The Playwright ``_visit`` is monkeypatched; pure helpers (target selection,
skip logic, attempt recording) and the ``run()`` orchestration are tested
directly.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from src import restock_signup as rs
from src.config import Config

_ISO = "2026-06-13T00:00:00+00:00"


@pytest.fixture
def cfg():
    return Config(
        watchlist_url="https://docs.google.com/document/d/abc/edit",
        resend_api_key="re_xxx",
        from_email="from@example.com",
        to_email="to@example.com",
        github_token="ghp_xxx",
        gist_id="gist123",
        anthropic_api_key="sk-ant-xxx",
        gmail_username="user@gmail.com",
        gmail_app_password="pw",
        signup_enabled=False,
        signup_phone="+15555550100",
        preferred_sizes=("M", "L"),
        preferred_sizes_pants=(),
        restock_signup_enabled=True,
    )


# ---------------------------------------------------------------------------
# _oos_target
# ---------------------------------------------------------------------------

class TestOosTarget:
    def test_preferred_size_oos(self):
        entry = {
            "in_stock": False,
            "size_options": ["S", "M", "L"],
            "available_sizes": ["S"],
            "preferred_sizes_applied": ["M", "L"],
        }
        assert rs._oos_target("u", entry) == ("u", ["M", "L"])

    def test_spelled_out_options_match_preferred(self):
        entry = {
            "in_stock": False,
            "size_options": ["Small", "Medium", "Large"],
            "available_sizes": ["Small"],
            "preferred_sizes_applied": ["M", "L"],
        }
        # Returns the product's own casing for on-page matching.
        assert rs._oos_target("u", entry) == ("u", ["Medium", "Large"])

    def test_whole_product_oos_no_sizes(self):
        entry = {"in_stock": False, "size_options": [], "available_sizes": []}
        assert rs._oos_target("u", entry) == ("u", [])

    def test_in_stock_is_not_a_target(self):
        entry = {
            "in_stock": True,
            "size_options": ["S", "M"],
            "available_sizes": ["S", "M"],
            "preferred_sizes_applied": ["M"],
        }
        assert rs._oos_target("u", entry) is None

    def test_preferred_in_stock_not_a_target(self):
        # Product OOS overall but the user's size IS available → nothing to do.
        entry = {
            "in_stock": False,
            "size_options": ["S", "M", "L"],
            "available_sizes": ["M"],
            "preferred_sizes_applied": ["M"],
        }
        assert rs._oos_target("u", entry) is None


# ---------------------------------------------------------------------------
# _collect_targets
# ---------------------------------------------------------------------------

class TestCollectTargets:
    def _prices(self):
        return {
            "https://a.com/p1": {
                "in_stock": False, "size_options": ["M", "L"],
                "available_sizes": [], "preferred_sizes_applied": ["M", "L"],
            },
            "https://a.com/p2": {  # in stock, not a target
                "in_stock": True, "size_options": ["M"],
                "available_sizes": ["M"], "preferred_sizes_applied": ["M"],
            },
        }

    def test_all_oos_products(self):
        args = _args()
        targets = rs._collect_targets(args, self._prices())
        assert targets == [("https://a.com/p1", ["M", "L"])]

    def test_size_filter_narrows(self):
        args = _args(size="M")
        targets = rs._collect_targets(args, self._prices())
        assert targets == [("https://a.com/p1", ["M"])]

    def test_url_with_size(self):
        args = _args(url="https://x.com/p", size="M")
        assert rs._collect_targets(args, {}) == [("https://x.com/p", ["M"])]

    def test_url_falls_back_to_product_level(self):
        # --url not in prices, no --size → product-level attempt.
        args = _args(url="https://x.com/p")
        assert rs._collect_targets(args, {}) == [("https://x.com/p", [])]


# ---------------------------------------------------------------------------
# skip + record
# ---------------------------------------------------------------------------

class TestSkipAndRecord:
    def test_product_level_key(self):
        assert rs._size_keys([]) == [rs.PRODUCT_LEVEL]
        assert rs._size_keys(["M"]) == ["M"]

    def test_effective_channels_excludes_done(self):
        state = {"u": {"sizes": {
            "M": {"email": {"signed_up_at": _ISO}, "phone": None},
        }, "attempts": []}}
        # email done, phone pending → only phone remains for M; L is untouched.
        assert rs._effective_channels(
            "u", "M", ["email", "phone"], state, retry_failed=False) == ["phone"]
        assert rs._effective_channels(
            "u", "L", ["email", "phone"], state, retry_failed=False) == ["email", "phone"]

    def test_phone_unavailable_counts_as_done(self):
        state = {"u": {"sizes": {
            "M": {"email": {"signed_up_at": _ISO},
                  "phone": {"unavailable": True, "checked_at": _ISO}},
        }, "attempts": []}}
        assert rs._effective_channels(
            "u", "M", ["email", "phone"], state, retry_failed=False) == []

    def test_legacy_flat_slot_reads_as_email(self):
        # Pre-phone records stored sizes[M] = {signed_up_at, vendor} (email).
        state = {"u": {"sizes": {"M": {"signed_up_at": _ISO, "vendor": "swym_bis"}},
                       "attempts": []}}
        assert rs._is_done("u", "M", "email", state, retry_failed=False) is True
        assert rs._is_done("u", "M", "phone", state, retry_failed=False) is False

    def test_record_email_success_nested(self):
        state: dict = {}
        rs._record_attempt(state, "u", {
            "at": _ISO, "size": "M", "channel": "email", "result": "success",
            "vendor": "klaviyo_bis",
        })
        assert state["u"]["sizes"]["M"]["email"] == {
            "signed_up_at": _ISO, "vendor": "klaviyo_bis"}
        assert state["u"]["sizes"]["M"]["phone"] is None
        assert len(state["u"]["attempts"]) == 1

    def test_record_phone_success_nested(self):
        state: dict = {}
        rs._record_attempt(state, "u", {
            "at": _ISO, "size": "M", "channel": "phone", "result": "success",
            "vendor": "swym_bis",
        })
        assert state["u"]["sizes"]["M"]["phone"] == {
            "signed_up_at": _ISO, "vendor": "swym_bis"}

    def test_record_no_phone_field_marks_unavailable(self):
        state: dict = {}
        rs._record_attempt(state, "u", {
            "at": _ISO, "size": "M", "channel": "phone", "result": "no_phone_field",
        })
        assert state["u"]["sizes"]["M"]["phone"] == {
            "unavailable": True, "checked_at": _ISO}

    def test_record_migrates_legacy_then_adds_phone(self):
        state = {"u": {"sizes": {"M": {"signed_up_at": _ISO, "vendor": "v"}},
                       "attempts": []}}
        rs._record_attempt(state, "u", {
            "at": "T2", "size": "M", "channel": "phone", "result": "success",
            "vendor": "swym_bis",
        })
        slot = state["u"]["sizes"]["M"]
        assert slot["email"] == {"signed_up_at": _ISO, "vendor": "v"}
        assert slot["phone"] == {"signed_up_at": "T2", "vendor": "swym_bis"}

    def test_record_failure_does_not_mirror(self):
        state: dict = {}
        rs._record_attempt(state, "u", {
            "at": _ISO, "size": "M", "channel": "email",
            "result": "no_form_detected", "vendor": None,
        })
        assert state["u"]["sizes"] == {}

    def test_dry_run_success_not_mirrored(self):
        state: dict = {}
        rs._record_attempt(state, "u", {
            "at": _ISO, "size": "M", "channel": "email", "result": "success",
            "dry_run": True,
        })
        assert state["u"]["sizes"] == {}


# ---------------------------------------------------------------------------
# run() orchestration
# ---------------------------------------------------------------------------

class TestRun:
    def _patch(self, monkeypatch, *, prices, prior_restock=None, visit=None):
        monkeypatch.setattr(rs, "read_state", lambda gid, tok: {
            "prices": prices, "aliases": {}, "codes": [],
            "restock": dict(prior_restock or {}),
        })
        captured: dict = {"visits": [], "write": None}

        def _write(*a, **k):
            captured["write"] = k
        monkeypatch.setattr(rs, "write_state", _write)

        def _fake_visit(url, size_specs, email, phone=None, *, dry_run,
                        screenshot_dir, **_kw):
            captured["visits"].append({
                "url": url, "specs": [(s, list(ch)) for s, ch in size_specs],
                "sizes": [s for s, _ in size_specs], "email": email,
                "phone": phone, "dry_run": dry_run,
            })
            if visit is not None:
                return visit(url, size_specs)
            return [{"at": _ISO, "size": s, "channel": c, "result": "success",
                     "vendor": "v"}
                    for s, chs in size_specs for c in chs]
        monkeypatch.setattr(rs, "_visit", _fake_visit)
        monkeypatch.setattr(rs.time, "sleep", lambda _s: None)
        return captured

    def _prices(self):
        return {"https://a.com/p1": {
            "in_stock": False, "size_options": ["M", "L"],
            "available_sizes": [], "preferred_sizes_applied": ["M", "L"],
        }}

    def test_disabled_short_circuits(self, monkeypatch, cfg):
        cap = self._patch(monkeypatch, prices=self._prices())
        rc = rs.run(argv=[], cfg=replace(cfg, restock_signup_enabled=False))
        assert rc == 0
        assert cap["visits"] == []
        assert cap["write"] is None

    def test_visits_and_writes(self, monkeypatch, cfg):
        cap = self._patch(monkeypatch, prices=self._prices())
        rc = rs.run(argv=[], cfg=cfg)
        assert rc == 0
        assert cap["visits"][0]["url"] == "https://a.com/p1"
        assert cap["visits"][0]["sizes"] == ["M", "L"]
        assert cap["visits"][0]["email"] == "user@gmail.com"
        assert cap["visits"][0]["phone"] == "+15555550100"
        # Default --channel both → each size targets email + phone.
        assert cap["visits"][0]["specs"][0] == ("M", ["email", "phone"])
        assert "restock" in cap["write"]
        m_slot = cap["write"]["restock"]["https://a.com/p1"]["sizes"]["M"]
        assert m_slot["email"]["vendor"] == "v"
        assert m_slot["phone"]["vendor"] == "v"

    def test_phone_channel_passes_phone_only(self, monkeypatch, cfg):
        cap = self._patch(monkeypatch, prices=self._prices())
        rc = rs.run(argv=["--channel", "phone"], cfg=cfg)
        assert rc == 0
        assert cap["visits"][0]["specs"][0] == ("M", ["phone"])

    def test_phone_channel_without_number_bails(self, monkeypatch, cfg):
        cap = self._patch(monkeypatch, prices=self._prices())
        rc = rs.run(argv=["--channel", "phone"],
                    cfg=replace(cfg, signup_phone=""))
        assert rc == 0
        assert cap["visits"] == []
        assert cap["write"] is None

    def test_both_without_number_drops_phone(self, monkeypatch, cfg):
        cap = self._patch(monkeypatch, prices=self._prices())
        rc = rs.run(argv=["--channel", "both"],
                    cfg=replace(cfg, signup_phone=""))
        assert rc == 0
        assert cap["visits"][0]["specs"][0] == ("M", ["email"])

    def test_dry_run_skips_write(self, monkeypatch, cfg):
        cap = self._patch(monkeypatch, prices=self._prices())
        rc = rs.run(argv=["--dry-run"], cfg=cfg)
        assert rc == 0
        assert cap["write"] is None

    def test_skips_already_signed_up(self, monkeypatch, cfg):
        # Both sizes done on both channels → fully skipped under --channel both.
        done = {"email": {"signed_up_at": _ISO}, "phone": {"signed_up_at": _ISO}}
        prior = {"https://a.com/p1": {
            "sizes": {"M": dict(done), "L": dict(done)}, "attempts": [],
        }}
        cap = self._patch(monkeypatch, prices=self._prices(), prior_restock=prior)
        rc = rs.run(argv=[], cfg=cfg)
        assert rc == 0
        assert cap["visits"] == []

    def test_both_revisits_email_done_for_phone(self, monkeypatch, cfg):
        # Email done but phone pending → still visited, for phone only.
        prior = {"https://a.com/p1": {
            "sizes": {"M": {"email": {"signed_up_at": _ISO}, "phone": None},
                      "L": {"email": {"signed_up_at": _ISO}, "phone": None}},
            "attempts": [],
        }}
        cap = self._patch(monkeypatch, prices=self._prices(), prior_restock=prior)
        rc = rs.run(argv=[], cfg=cfg)
        assert rc == 0
        assert cap["visits"][0]["specs"] == [("M", ["phone"]), ("L", ["phone"])]

    def test_list_targets_bypasses_gate(self, monkeypatch, cfg, capsys):
        cap = self._patch(monkeypatch, prices=self._prices())
        rc = rs.run(argv=["--list-targets"],
                    cfg=replace(cfg, restock_signup_enabled=False))
        assert rc == 0
        out = capsys.readouterr().out
        assert "https://a.com/p1" in out
        assert cap["visits"] == []

    def test_max_items_caps(self, monkeypatch, cfg):
        prices = {
            "https://a.com/p1": {"in_stock": False, "size_options": ["M"],
                                 "available_sizes": [], "preferred_sizes_applied": ["M"]},
            "https://a.com/p2": {"in_stock": False, "size_options": ["M"],
                                 "available_sizes": [], "preferred_sizes_applied": ["M"]},
        }
        cap = self._patch(monkeypatch, prices=prices)
        rc = rs.run(argv=["--max-items", "1"], cfg=cfg)
        assert rc == 0
        assert len(cap["visits"]) == 1


# ---------------------------------------------------------------------------
# _attempt_one orchestration (restock_detect helpers monkeypatched)
# ---------------------------------------------------------------------------

class FakeField:
    """Minimal email/phone/submit field. ``valid`` scripts checkValidity()."""

    def __init__(self, *, valid=True, fill_raises=False, click_raises=False):
        self.fills: list[str] = []
        self.clicks = 0
        self._valid = valid
        self._fill_raises = fill_raises
        self._click_raises = click_raises

    def fill(self, value):
        if self._fill_raises:
            raise RuntimeError("fill blocked")
        self.fills.append(value)

    def evaluate(self, _js):
        return self._valid

    def click(self):
        if self._click_raises:
            raise RuntimeError("click blocked")
        self.clicks += 1


class TestAttemptOne:
    def _patch(self, monkeypatch, *, email_field=None, phone_field=None,
               submit=None, ok=True, vendor="klaviyo_bis", visible_text="",
               size_selectable=True, size_in_form_ok=True, form_found=True):
        monkeypatch.setattr(rs, "select_size", lambda page, size, **k: size_selectable)
        monkeypatch.setattr(rs, "reveal_restock_form", lambda page, **k: True)
        monkeypatch.setattr(
            rs, "detect_restock_form",
            lambda page, **k: ((object(), vendor) if form_found else (None, None)))
        monkeypatch.setattr(rs, "select_size_in_form", lambda form, size, **k: size_in_form_ok)
        monkeypatch.setattr(rs, "find_email_field", lambda form, **k: email_field)
        monkeypatch.setattr(rs, "find_phone_field", lambda form, **k: phone_field)
        monkeypatch.setattr(rs, "find_restock_submit", lambda form, **k: submit)
        monkeypatch.setattr(rs, "check_consent_if_present", lambda form, **k: False)
        monkeypatch.setattr(
            rs, "detect_restock_success", lambda page, form, **k: ok)
        monkeypatch.setattr(rs, "visible_text", lambda page, form: visible_text)
        monkeypatch.setattr(rs, "_screenshot", lambda page, path: None)

    def _call(self, channels, **kw):
        return rs._attempt_one(
            object(), RuntimeError, "https://s.com/p", "M",
            "user@gmail.com", "+15555550100", channels=channels,
            dry_run=kw.pop("dry_run", False), screenshot_dir="x",
            post_submit_wait_ms=10)

    def test_email_only_form_success(self, monkeypatch):
        ef, sb = FakeField(), FakeField()
        self._patch(monkeypatch, email_field=ef, submit=sb)
        out = self._call(["email"])
        assert [(r["channel"], r["result"]) for r in out] == [("email", "success")]
        assert out[0]["vendor"] == "klaviyo_bis"
        assert ef.fills == ["user@gmail.com"] and sb.clicks == 1

    def test_both_fields_single_submit(self, monkeypatch):
        ef, pf, sb = FakeField(), FakeField(), FakeField()
        self._patch(monkeypatch, email_field=ef, phone_field=pf, submit=sb)
        out = self._call(["email", "phone"])
        assert {(r["channel"], r["result"]) for r in out} == {
            ("email", "success"), ("phone", "success")}
        assert sb.clicks == 1
        assert pf.fills == ["+15555550100"]

    def test_phone_only_form_success(self, monkeypatch):
        pf, sb = FakeField(), FakeField()
        self._patch(monkeypatch, phone_field=pf, submit=sb)
        out = self._call(["phone"])
        assert [(r["channel"], r["result"]) for r in out] == [("phone", "success")]

    def test_no_phone_field_marks_unavailable(self, monkeypatch):
        ef, sb = FakeField(), FakeField()
        self._patch(monkeypatch, email_field=ef, phone_field=None, submit=sb)
        out = self._call(["email", "phone"])
        results = {(r["channel"], r["result"]) for r in out}
        assert ("email", "success") in results
        assert ("phone", "no_phone_field") in results

    def test_phone_otp_recorded(self, monkeypatch):
        pf, sb = FakeField(), FakeField()
        self._patch(monkeypatch, phone_field=pf, submit=sb,
                    visible_text="Enter the verification code we texted you")
        out = self._call(["phone"])
        assert [(r["channel"], r["result"]) for r in out] == [("phone", "requires_otp")]

    def test_size_not_found_when_neither_page_nor_form(self, monkeypatch):
        # size_not_found only when the size is selectable neither on the page
        # nor in the popup's own dropdown.
        self._patch(monkeypatch, email_field=FakeField(), submit=FakeField(),
                    size_selectable=False, size_in_form_ok=False)
        out = self._call(["email", "phone"])
        assert [(r["channel"], r["result"]) for r in out] == [("email", "size_not_found")]

    def test_popup_size_select_rescues_page_failure(self, monkeypatch):
        # Page size selection fails but the popup's dropdown works → proceed.
        ef, sb = FakeField(), FakeField()
        self._patch(monkeypatch, email_field=ef, submit=sb,
                    size_selectable=False, size_in_form_ok=True)
        out = self._call(["email"])
        assert [(r["channel"], r["result"]) for r in out] == [("email", "success")]

    def test_no_form_detected(self, monkeypatch):
        self._patch(monkeypatch, form_found=False)
        out = self._call(["email"])
        assert [(r["channel"], r["result"]) for r in out] == [("email", "no_form_detected")]

    def test_no_submit_button(self, monkeypatch):
        ef = FakeField()
        self._patch(monkeypatch, email_field=ef, submit=None)
        out = self._call(["email"])
        assert [(r["channel"], r["result"]) for r in out] == [("email", "form_fill_failed")]
        assert ef.fills == []

    def test_dry_run_reports_without_submitting(self, monkeypatch):
        ef, pf, sb = FakeField(), FakeField(), FakeField()
        self._patch(monkeypatch, email_field=ef, phone_field=pf, submit=sb)
        out = self._call(["email", "phone"], dry_run=True)
        assert {(r["channel"], r["result"]) for r in out} == {
            ("email", "success"), ("phone", "success")}
        assert all(r["dry_run"] is True for r in out)
        assert ef.fills == [] and pf.fills == [] and sb.clicks == 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _args(**over):
    import argparse
    defaults = dict(
        url=None, size=None, channel="both", dry_run=False, max_items=None,
        retry_failed=False, list_targets=False, screenshot_dir="x",
    )
    defaults.update(over)
    return argparse.Namespace(**defaults)
