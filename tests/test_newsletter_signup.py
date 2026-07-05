"""Tests for src/newsletter_signup.py — Phase 1 scaffolding.

The Playwright-backed ``_visit`` is exercised by a small monkeypatched stub
rather than spinning up real Chromium. Pure helpers (shop extraction, skip
logic, attempt recording) are tested directly.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src import newsletter_signup as ns


# ---------------------------------------------------------------------------
# _homepage_url
# ---------------------------------------------------------------------------

class TestHomepageUrl:
    def test_strips_path(self):
        assert ns._homepage_url("https://shop.com/products/abc") == "https://shop.com"

    def test_preserves_scheme(self):
        assert ns._homepage_url("http://shop.com/x") == "http://shop.com"

    def test_returns_input_on_unparseable(self):
        # No scheme/netloc → pass through unchanged.
        assert ns._homepage_url("not-a-url") == "not-a-url"


# ---------------------------------------------------------------------------
# _shops_from_watchlist
# ---------------------------------------------------------------------------

class TestShopsFromWatchlist:
    def test_extracts_product_urls(self):
        text = (
            "Aniqi:\n"
            "- https://aniqi.com/products/hoodie\n"
            "- https://aniqi.com/products/tee\n"
        )
        shops = ns._shops_from_watchlist(text, aliases={})
        assert shops == ["https://aniqi.com"]

    def test_dedupes_same_host_across_products(self):
        text = (
            "https://shop.com/products/a\n"
            "https://shop.com/products/b\n"
        )
        shops = ns._shops_from_watchlist(text, aliases={})
        assert shops == ["https://shop.com"]

    def test_resolves_shop_name_via_aliases(self):
        text = "KillCrew:\n- some shirt\n"
        aliases = {"KillCrew": "https://killcrew.com"}
        shops = ns._shops_from_watchlist(text, aliases)
        assert "https://killcrew.com" in shops

    def test_unresolved_shop_name_dropped(self):
        text = "UnknownShop:\n- something\n"
        shops = ns._shops_from_watchlist(text, aliases={})
        assert shops == []


# ---------------------------------------------------------------------------
# _shop_domain
# ---------------------------------------------------------------------------

class TestShopDomain:
    def test_basic_host(self):
        assert ns._shop_domain("https://aniqi.com") == "aniqi.com"

    def test_strips_www(self):
        assert ns._shop_domain("https://www.aniqi.com/products/x") == "aniqi.com"

    def test_lowercases(self):
        assert ns._shop_domain("https://Aniqi.COM") == "aniqi.com"

    def test_no_netloc_returns_none(self):
        assert ns._shop_domain("not-a-url") is None


# ---------------------------------------------------------------------------
# _seed_inferred_subscriptions
# ---------------------------------------------------------------------------

class TestSeedInferredSubscriptions:
    def test_seeds_matching_domain(self):
        state: dict = {}
        seeded = ns._seed_inferred_subscriptions(
            ["https://aniqi.com", "https://other.com"], state, {"aniqi.com"},
            now_iso=_ISO,
        )
        assert seeded == ["https://aniqi.com"]
        rec = state["https://aniqi.com"]["email"]
        assert rec["signed_up_at"] == _ISO
        assert rec["inferred"] is True
        # Mirrored into _should_skip's gate.
        assert ns._should_skip(
            "https://aniqi.com", state, ["email"], retry_failed=False,
        ) is True
        # Non-matching shop untouched.
        assert "https://other.com" not in state

    def test_records_already_subscribed_attempt(self):
        state: dict = {}
        ns._seed_inferred_subscriptions(
            ["https://aniqi.com"], state, {"aniqi.com"}, now_iso=_ISO,
        )
        attempts = state["https://aniqi.com"]["attempts"]
        assert attempts[-1]["result"] == "already_subscribed"
        assert attempts[-1]["source"] == "gmail_inferred"

    def test_does_not_overwrite_real_record(self):
        state = {
            "https://aniqi.com": {
                "email": {"signed_up_at": "2025-01-01T00:00:00+00:00",
                          "code_received": "REAL10"},
                "phone": None, "attempts": [],
            }
        }
        seeded = ns._seed_inferred_subscriptions(
            ["https://aniqi.com"], state, {"aniqi.com"}, now_iso=_ISO,
        )
        assert seeded == []
        assert state["https://aniqi.com"]["email"]["code_received"] == "REAL10"

    def test_empty_subscribed_seeds_nothing(self):
        state: dict = {}
        assert ns._seed_inferred_subscriptions(
            ["https://aniqi.com"], state, set(), now_iso=_ISO,
        ) == []
        assert state == {}


# ---------------------------------------------------------------------------
# _infer_subscribed (failure isolation)
# ---------------------------------------------------------------------------

class TestInferSubscribed:
    def test_returns_subscribed_domains(self, monkeypatch, fake_cfg):
        monkeypatch.setattr(
            ns, "subscribed_shop_domains",
            lambda user, pw, domains: {d for d in domains if d == "aniqi.com"},
        )
        got = ns._infer_subscribed(
            ["https://aniqi.com", "https://other.com"], fake_cfg,
        )
        assert got == {"aniqi.com"}

    def test_swallows_errors_and_returns_empty(self, monkeypatch, fake_cfg):
        def _boom(*a, **k):
            raise RuntimeError("imap down")
        monkeypatch.setattr(ns, "subscribed_shop_domains", _boom)
        # Failure-isolated: no exception, empty set so the run proceeds.
        assert ns._infer_subscribed(["https://aniqi.com"], fake_cfg) == set()

    def test_no_domains_skips_query(self, monkeypatch, fake_cfg):
        called = {"n": 0}
        def _spy(*a, **k):
            called["n"] += 1
            return set()
        monkeypatch.setattr(ns, "subscribed_shop_domains", _spy)
        assert ns._infer_subscribed(["not-a-url"], fake_cfg) == set()
        assert called["n"] == 0


# ---------------------------------------------------------------------------
# _should_skip
# ---------------------------------------------------------------------------

_ISO = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc).isoformat()


class TestShouldSkip:
    def test_no_record_means_dont_skip(self):
        assert ns._should_skip("https://s.com", {}, ["email"], retry_failed=False) is False

    def test_success_on_all_channels_means_skip(self):
        state = {
            "https://s.com": {
                "email": {"signed_up_at": _ISO},
                "phone": {"signed_up_at": _ISO},
                "attempts": [],
            }
        }
        assert ns._should_skip("https://s.com", state, ["email", "phone"], retry_failed=False) is True

    def test_partial_success_on_one_channel_dont_skip(self):
        state = {
            "https://s.com": {
                "email": {"signed_up_at": _ISO},
                "phone": None,
                "attempts": [],
            }
        }
        assert ns._should_skip("https://s.com", state, ["email", "phone"], retry_failed=False) is False

    def test_retry_failed_overrides_skip_for_transient(self):
        state = {
            "https://s.com": {
                "email": {"signed_up_at": _ISO},
                "phone": {"signed_up_at": _ISO},
                "attempts": [{"at": _ISO, "channel": "email", "result": "no_popup_detected"}],
            }
        }
        assert ns._should_skip("https://s.com", state, ["email", "phone"], retry_failed=True) is False

    def test_retry_failed_no_op_when_last_was_success(self):
        state = {
            "https://s.com": {
                "email": {"signed_up_at": _ISO},
                "phone": {"signed_up_at": _ISO},
                "attempts": [{"at": _ISO, "channel": "email", "result": "success"}],
            }
        }
        assert ns._should_skip("https://s.com", state, ["email", "phone"], retry_failed=True) is True


# ---------------------------------------------------------------------------
# _record_attempt
# ---------------------------------------------------------------------------

class TestRecordAttempt:
    def test_creates_new_entry(self):
        state: dict = {}
        attempt = {"at": _ISO, "channel": "email", "result": "no_popup_detected"}
        ns._record_attempt(state, "https://s.com", attempt)
        assert state["https://s.com"]["email"] is None
        assert state["https://s.com"]["phone"] is None
        assert state["https://s.com"]["attempts"] == [attempt]

    def test_appends_to_existing_attempts(self):
        prior = {"at": _ISO, "channel": "email", "result": "success"}
        state = {"https://s.com": {"email": None, "phone": None, "attempts": [prior]}}
        new = {"at": _ISO, "channel": "phone", "result": "no_popup_detected"}
        ns._record_attempt(state, "https://s.com", new)
        assert state["https://s.com"]["attempts"] == [prior, new]

    def test_success_mirrors_into_channel_record(self):
        """A real success populates the channel record so future runs skip the shop."""
        state: dict = {}
        attempt = {
            "at": _ISO, "channel": "email", "result": "success",
            "code_received": "WELCOME15", "vendor": "klaviyo",
        }
        ns._record_attempt(state, "https://s.com", attempt)
        assert state["https://s.com"]["email"] == {
            "signed_up_at": _ISO, "code_received": "WELCOME15",
        }
        assert state["https://s.com"]["phone"] is None

    def test_success_without_code_omits_code_field(self):
        state: dict = {}
        attempt = {"at": _ISO, "channel": "email", "result": "success",
                   "code_received": None, "vendor": "generic"}
        ns._record_attempt(state, "https://s.com", attempt)
        assert state["https://s.com"]["email"] == {"signed_up_at": _ISO}

    def test_dry_run_success_does_not_mirror(self):
        """Dry-run 'success' is informational only — don't gate future skips on it."""
        state: dict = {}
        attempt = {"at": _ISO, "channel": "email", "result": "success",
                   "dry_run": True, "vendor": "klaviyo"}
        ns._record_attempt(state, "https://s.com", attempt)
        assert state["https://s.com"]["email"] is None

    def test_failure_does_not_mirror(self):
        state: dict = {}
        attempt = {"at": _ISO, "channel": "email", "result": "form_fill_failed"}
        ns._record_attempt(state, "https://s.com", attempt)
        assert state["https://s.com"]["email"] is None


# ---------------------------------------------------------------------------
# _safe_filename
# ---------------------------------------------------------------------------

class TestSafeFilename:
    def test_uses_host(self):
        assert ns._safe_filename("https://shop.com/x") == "shop.com.png"

    def test_replaces_unsafe_chars(self):
        assert ns._safe_filename("https://shop.com:8080/x") == "shop.com_8080.png"

    def test_suffix_appended_before_extension(self):
        assert ns._safe_filename("https://shop.com", suffix="_pre") == "shop.com_pre.png"


# ---------------------------------------------------------------------------
# run() — integration with monkeypatched _visit + state I/O
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_cfg():
    """Default fixture has signup_enabled=True so existing tests exercise
    the real code path. A separate test below covers the disabled case."""
    from src.config import Config
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
        signup_enabled=True,
        signup_phone="+15555550100",
        preferred_sizes=(),
        preferred_sizes_pants=(),
    )


@pytest.fixture
def disabled_cfg(fake_cfg):
    """A copy of fake_cfg with signup_enabled=False."""
    from dataclasses import replace
    return replace(fake_cfg, signup_enabled=False)


class TestRun:
    def _patch_everything(
        self, monkeypatch, *, shops, prior_signup=None,
        visit_result: dict | None = None,
    ):
        """Stub out network: watchlist, state read/write, Playwright visit."""
        monkeypatch.setattr(ns, "fetch_watchlist", lambda url: "")
        monkeypatch.setattr(
            ns, "_shops_from_watchlist", lambda text, aliases: list(shops),
        )
        monkeypatch.setattr(
            ns, "read_state",
            lambda gid, tok: {
                "prices": {},
                "aliases": {},
                "codes": [],
                "fx": {},
                "gmail": {},
                "voice": {},
                "sms_aliases": {},
                "signup": dict(prior_signup or {}),
            },
        )
        captured: dict = {"visits": []}
        def _write(*args, **kwargs):
            captured["call"] = kwargs
        monkeypatch.setattr(ns, "write_state", _write)
        default_result = visit_result or {
            "at": _ISO, "channel": "email", "result": "no_popup_detected",
            "vendor": None, "code_received": None, "dry_run": None,
        }
        def _fake_visit(shop, email, phone=None, *, channels=None,
                        dry_run=False, screenshot_dir=None, **_kw):
            captured["visits"].append({
                "shop": shop, "email": email, "phone": phone,
                "channels": list(channels) if channels is not None else None,
                "dry_run": dry_run, "screenshot_dir": screenshot_dir,
            })
            # Phase 3: _visit returns a LIST of per-channel attempt records.
            return [dict(default_result)]
        monkeypatch.setattr(ns, "_visit", _fake_visit)
        # Default: no inferred subscriptions, so every non-skipped shop is
        # visited (the pre-inference behavior). Tests that exercise the Gmail
        # auto-skip override this with their own stub. Stubbing here also keeps
        # run() off the real IMAP network.
        monkeypatch.setattr(ns, "_infer_subscribed", lambda shops, cfg: set())
        # Kill the inter-shop sleep so tests stay fast.
        monkeypatch.setattr(ns.time, "sleep", lambda _s: None)
        return captured

    def test_visits_all_shops_and_writes_state(self, monkeypatch, fake_cfg):
        captured = self._patch_everything(
            monkeypatch, shops=["https://a.com", "https://b.com"],
        )
        rc = ns.run(argv=["--screenshot-dir", "tmp"], cfg=fake_cfg)
        assert rc == 0
        signup = captured["call"]["signup"]
        assert set(signup.keys()) == {"https://a.com", "https://b.com"}
        assert len(signup["https://a.com"]["attempts"]) == 1

    def test_dry_run_skips_write(self, monkeypatch, fake_cfg):
        captured = self._patch_everything(monkeypatch, shops=["https://a.com"])
        rc = ns.run(argv=["--dry-run"], cfg=fake_cfg)
        assert rc == 0
        assert "call" not in captured

    def test_max_shops_caps_visits(self, monkeypatch, fake_cfg):
        captured = self._patch_everything(
            monkeypatch, shops=["https://a.com", "https://b.com", "https://c.com"],
        )
        rc = ns.run(argv=["--max-shops", "2"], cfg=fake_cfg)
        assert rc == 0
        assert len(captured["call"]["signup"]) == 2

    def test_skips_shops_already_successful(self, monkeypatch, fake_cfg):
        prior = {
            "https://a.com": {
                "email": {"signed_up_at": _ISO},
                "phone": {"signed_up_at": _ISO},
                "attempts": [],
            }
        }
        captured = self._patch_everything(
            monkeypatch,
            shops=["https://a.com", "https://b.com"],
            prior_signup=prior,
        )
        rc = ns.run(argv=[], cfg=fake_cfg)
        assert rc == 0
        signup = captured["call"]["signup"]
        # a.com unchanged (no new attempt); b.com got a fresh attempt.
        assert signup["https://a.com"]["attempts"] == []
        assert len(signup["https://b.com"]["attempts"]) == 1

    def test_single_shop_arg_overrides_watchlist(self, monkeypatch, fake_cfg):
        captured = self._patch_everything(
            monkeypatch, shops=["https://x.com", "https://y.com"],
        )
        rc = ns.run(argv=["--shop", "https://one.com/products/abc"], cfg=fake_cfg)
        assert rc == 0
        # Only the --shop URL (normalized to homepage) gets visited.
        assert list(captured["call"]["signup"].keys()) == ["https://one.com"]

    def test_phone_channel_visits_with_phone(self, monkeypatch, fake_cfg):
        """Phase 3: --channel=phone visits and passes the phone number +
        phone-only channel set to _visit (no email inference)."""
        captured = self._patch_everything(monkeypatch, shops=["https://a.com"])
        called = {"n": 0}
        monkeypatch.setattr(
            ns, "_infer_subscribed",
            lambda shops, cfg: called.__setitem__("n", called["n"] + 1) or set(),
        )
        rc = ns.run(argv=["--channel", "phone"], cfg=fake_cfg)
        assert rc == 0
        assert len(captured["visits"]) == 1
        v = captured["visits"][0]
        assert v["channels"] == ["phone"]
        assert v["phone"] == fake_cfg.signup_phone
        # Email-scoped inference doesn't run for a phone-only signup.
        assert called["n"] == 0

    def test_phone_channel_without_number_bails(self, monkeypatch, fake_cfg):
        """--channel=phone with no SIGNUP_PHONE configured does nothing."""
        from dataclasses import replace
        cfg = replace(fake_cfg, signup_phone="")
        captured = self._patch_everything(monkeypatch, shops=["https://a.com"])
        rc = ns.run(argv=["--channel", "phone"], cfg=cfg)
        assert rc == 0
        assert captured["visits"] == []
        assert "call" not in captured

    def test_both_without_number_drops_phone(self, monkeypatch, fake_cfg):
        """--channel=both with no SIGNUP_PHONE falls back to email-only."""
        from dataclasses import replace
        cfg = replace(fake_cfg, signup_phone="")
        captured = self._patch_everything(monkeypatch, shops=["https://a.com"])
        rc = ns.run(argv=["--channel", "both"], cfg=cfg)
        assert rc == 0
        assert captured["visits"][0]["channels"] == ["email"]

    def test_both_visits_email_done_shop_for_phone(self, monkeypatch, fake_cfg):
        """Under --channel=both a shop already subscribed on email is still
        visited — but only the phone channel is filled this time."""
        prior = {
            "https://a.com": {
                "email": {"signed_up_at": _ISO}, "phone": None, "attempts": [],
            }
        }
        captured = self._patch_everything(
            monkeypatch, shops=["https://a.com"], prior_signup=prior,
        )
        rc = ns.run(argv=["--channel", "both"], cfg=fake_cfg)
        assert rc == 0
        assert len(captured["visits"]) == 1
        assert captured["visits"][0]["channels"] == ["phone"]

    def test_both_skips_when_email_done_and_phone_unavailable(
        self, monkeypatch, fake_cfg,
    ):
        """A phone channel marked unavailable counts as done — so a shop with
        email-done + phone-unavailable is fully skipped under --channel=both."""
        prior = {
            "https://a.com": {
                "email": {"signed_up_at": _ISO},
                "phone": {"unavailable": True, "checked_at": _ISO},
                "attempts": [],
            }
        }
        captured = self._patch_everything(
            monkeypatch, shops=["https://a.com"], prior_signup=prior,
        )
        rc = ns.run(argv=["--channel", "both"], cfg=fake_cfg)
        assert rc == 0
        assert captured["visits"] == []

    def test_visit_receives_email_from_config(self, monkeypatch, fake_cfg):
        captured = self._patch_everything(monkeypatch, shops=["https://a.com"])
        ns.run(argv=["--dry-run"], cfg=fake_cfg)
        assert captured["visits"][0]["email"] == fake_cfg.gmail_username

    def test_visit_receives_dry_run_flag(self, monkeypatch, fake_cfg):
        captured = self._patch_everything(monkeypatch, shops=["https://a.com"])
        ns.run(argv=["--dry-run"], cfg=fake_cfg)
        assert captured["visits"][0]["dry_run"] is True

    def test_success_persists_channel_record(self, monkeypatch, fake_cfg):
        """A real signup success should write the email channel record so the
        next non-dry-run skips the shop."""
        captured = self._patch_everything(
            monkeypatch, shops=["https://a.com"],
            visit_result={
                "at": _ISO, "channel": "email", "result": "success",
                "vendor": "klaviyo", "code_received": "WELCOME15", "dry_run": None,
            },
        )
        ns.run(argv=[], cfg=fake_cfg)
        signup = captured["call"]["signup"]
        assert signup["https://a.com"]["email"] == {
            "signed_up_at": _ISO, "code_received": "WELCOME15",
        }

    def test_disabled_signup_refuses_to_run(self, monkeypatch, disabled_cfg):
        """SIGNUP_ENABLED unset/false should short-circuit before any work."""
        captured = self._patch_everything(monkeypatch, shops=["https://a.com"])
        rc = ns.run(argv=[], cfg=disabled_cfg)
        assert rc == 0
        # Nothing happened — no visits, no state write.
        assert captured["visits"] == []
        assert "call" not in captured

    def test_email_only_skips_when_email_channel_done(self, monkeypatch, fake_cfg):
        """--channel=email skips a shop already subscribed on email."""
        prior = {
            "https://a.com": {
                "email": {"signed_up_at": _ISO, "code_received": "OLD"},
                "phone": None,
                "attempts": [],
            }
        }
        captured = self._patch_everything(
            monkeypatch, shops=["https://a.com"], prior_signup=prior,
        )
        ns.run(argv=["--channel", "email"], cfg=fake_cfg)
        # No visit happened — shop was already signed up on email.
        assert captured["visits"] == []

    def test_inference_seeds_and_skips_subscribed(self, monkeypatch, fake_cfg):
        """A shop the Gmail inference flags as subscribed gets an inferred
        record + is skipped; the rest are visited normally. (--channel=email so
        the inferred email record fully gates the skip.)"""
        captured = self._patch_everything(
            monkeypatch, shops=["https://a.com", "https://b.com"],
        )
        monkeypatch.setattr(ns, "_infer_subscribed", lambda shops, cfg: {"a.com"})
        rc = ns.run(argv=["--channel", "email"], cfg=fake_cfg)
        assert rc == 0
        # a.com inferred-subscribed → skipped; only b.com visited.
        assert [v["shop"] for v in captured["visits"]] == ["https://b.com"]
        signup = captured["call"]["signup"]
        assert signup["https://a.com"]["email"]["inferred"] is True
        assert signup["https://a.com"]["attempts"][-1]["result"] == "already_subscribed"
        assert len(signup["https://b.com"]["attempts"]) == 1

    def test_no_infer_flag_visits_all(self, monkeypatch, fake_cfg):
        """--no-infer-subscribed skips the Gmail query and visits every shop."""
        captured = self._patch_everything(
            monkeypatch, shops=["https://a.com", "https://b.com"],
        )
        called = {"n": 0}
        def _spy(shops, cfg):
            called["n"] += 1
            return {"a.com"}
        monkeypatch.setattr(ns, "_infer_subscribed", _spy)
        rc = ns.run(argv=["--no-infer-subscribed"], cfg=fake_cfg)
        assert rc == 0
        assert called["n"] == 0  # inference never ran
        assert {v["shop"] for v in captured["visits"]} == {
            "https://a.com", "https://b.com",
        }

    def test_shop_arg_bypasses_inference_and_skip(self, monkeypatch, fake_cfg):
        """An explicit --shop should visit even if a prior subscribed record
        exists, and must not run the Gmail inference."""
        prior = {
            "https://one.com": {
                "email": {"signed_up_at": _ISO, "inferred": True},
                "phone": None, "attempts": [],
            }
        }
        captured = self._patch_everything(
            monkeypatch, shops=["https://x.com"], prior_signup=prior,
        )
        called = {"n": 0}
        def _spy(shops, cfg):
            called["n"] += 1
            return set()
        monkeypatch.setattr(ns, "_infer_subscribed", _spy)
        rc = ns.run(argv=["--shop", "https://one.com"], cfg=fake_cfg)
        assert rc == 0
        assert called["n"] == 0  # inference skipped for explicit --shop
        assert [v["shop"] for v in captured["visits"]] == ["https://one.com"]

    def test_report_only_prints_split_and_does_no_work(
        self, monkeypatch, fake_cfg, capsys,
    ):
        captured = self._patch_everything(
            monkeypatch, shops=["https://a.com", "https://b.com"],
        )
        monkeypatch.setattr(ns, "_infer_subscribed", lambda shops, cfg: {"a.com"})
        rc = ns.run(argv=["--report-only"], cfg=fake_cfg)
        assert rc == 0
        out = capsys.readouterr().out
        assert "already subscribed (marketing mail found): 1" in out
        assert "signup targets (no marketing mail found): 1" in out
        assert "https://a.com" in out and "https://b.com" in out
        # Read-only: no visits, no Gist write.
        assert captured["visits"] == []
        assert "call" not in captured

    def test_report_only_bypasses_disabled_toggle(
        self, monkeypatch, disabled_cfg, capsys,
    ):
        captured = self._patch_everything(monkeypatch, shops=["https://a.com"])
        monkeypatch.setattr(ns, "_infer_subscribed", lambda shops, cfg: set())
        rc = ns.run(argv=["--report-only"], cfg=disabled_cfg)
        assert rc == 0
        assert "Subscription report" in capsys.readouterr().out
        assert captured["visits"] == []


# ---------------------------------------------------------------------------
# Phase 3 — phone-format + channel helpers
# ---------------------------------------------------------------------------

class TestPhoneFormats:
    def test_e164_us_number_expands_to_three_shapes(self):
        assert ns.phone_formats("+15555550100") == [
            "+15555550100", "(555) 555-0100", "5555550100",
        ]

    def test_eleven_digit_with_country_code(self):
        assert ns.phone_formats("15555550100") == [
            "+15555550100", "(555) 555-0100", "5555550100",
        ]

    def test_bare_ten_digits(self):
        assert ns.phone_formats("5555550100")[0] == "+15555550100"

    def test_formatted_input_is_normalised(self):
        assert ns.phone_formats("(555) 555-0100") == [
            "+15555550100", "(555) 555-0100", "5555550100",
        ]

    def test_empty_is_empty_list(self):
        assert ns.phone_formats("") == []
        assert ns.phone_formats("   ") == []

    def test_non_us_number_returned_verbatim(self):
        # +44 (UK) — not NANP, so we don't guess a national format.
        assert ns.phone_formats("+442071838750") == ["+442071838750"]


class FakeField:
    """Minimal phone/email/submit field. ``valid`` scripts checkValidity()."""

    def __init__(self, *, valid: bool = True, fill_raises: bool = False,
                 click_raises: bool = False) -> None:
        self.fills: list[str] = []
        self.clicks = 0
        self._valid = valid
        self._fill_raises = fill_raises
        self._click_raises = click_raises

    def fill(self, value: str) -> None:
        if self._fill_raises:
            raise RuntimeError("fill blocked")
        self.fills.append(value)

    def evaluate(self, _js: str):
        return self._valid

    def click(self) -> None:
        if self._click_raises:
            raise RuntimeError("click blocked")
        self.clicks += 1


class TestFillPhone:
    def test_fills_e164_first_when_valid(self):
        field = FakeField(valid=True)
        assert ns._fill_phone(field, "+15555550100") == "+15555550100"
        assert field.fills == ["+15555550100"]

    def test_falls_back_to_national_when_e164_invalid(self):
        # Invalid for the first two tries, valid on bare digits.
        class _F(FakeField):
            def __init__(self):
                super().__init__()
                self._seq = iter([False, False, True])
            def evaluate(self, _js):
                return next(self._seq)
        field = _F()
        assert ns._fill_phone(field, "+15555550100") == "5555550100"
        assert field.fills == ["+15555550100", "(555) 555-0100", "5555550100"]

    def test_returns_last_tried_when_none_validate(self):
        field = FakeField(valid=False)
        # All three rejected → returns the last value tried, still "filled".
        assert ns._fill_phone(field, "+15555550100") == "5555550100"

    def test_returns_none_when_empty_phone(self):
        field = FakeField()
        assert ns._fill_phone(field, "") is None
        assert field.fills == []


class TestChannelHelpers:
    def test_subscribed_channels_reads_signed_up(self):
        entry = {"email": {"signed_up_at": _ISO}, "phone": None}
        assert ns._subscribed_channels(entry) == {"email"}

    def test_subscribed_channels_excludes_unavailable_phone(self):
        entry = {"email": None, "phone": {"unavailable": True, "checked_at": _ISO}}
        # Unavailable phone is "done" for skip, but not "subscribed" (nothing
        # to re-fill), so it isn't returned here.
        assert ns._subscribed_channels(entry) == set()

    def test_channel_done_true_for_signed_up(self):
        assert ns._channel_done({"signed_up_at": _ISO}) is True

    def test_channel_done_true_for_unavailable(self):
        assert ns._channel_done({"unavailable": True}) is True

    def test_channel_done_false_for_none_or_empty(self):
        assert ns._channel_done(None) is False
        assert ns._channel_done({}) is False


# ---------------------------------------------------------------------------
# Phase 3 — _record_attempt + _should_skip phone behaviour
# ---------------------------------------------------------------------------

class TestRecordAttemptPhase3:
    def test_phone_success_mirrors_without_code(self):
        state: dict = {}
        ns._record_attempt(state, "https://s.com", {
            "at": _ISO, "channel": "phone", "result": "success",
            "code_received": "IGNORED", "vendor": "klaviyo",
        })
        # Phone record never carries a code.
        assert state["https://s.com"]["phone"] == {"signed_up_at": _ISO}

    def test_no_phone_field_marks_unavailable(self):
        state: dict = {}
        ns._record_attempt(state, "https://s.com", {
            "at": _ISO, "channel": "phone", "result": "no_phone_field",
        })
        assert state["https://s.com"]["phone"] == {
            "unavailable": True, "checked_at": _ISO,
        }

    def test_no_phone_field_does_not_clobber_real_signup(self):
        state = {"https://s.com": {
            "email": None, "phone": {"signed_up_at": "2025-01-01T00:00:00+00:00"},
            "attempts": [],
        }}
        ns._record_attempt(state, "https://s.com", {
            "at": _ISO, "channel": "phone", "result": "no_phone_field",
        })
        assert state["https://s.com"]["phone"] == {
            "signed_up_at": "2025-01-01T00:00:00+00:00",
        }

    def test_requires_otp_does_not_mirror(self):
        state: dict = {}
        ns._record_attempt(state, "https://s.com", {
            "at": _ISO, "channel": "phone", "result": "requires_otp",
        })
        assert state["https://s.com"]["phone"] is None

    def test_dry_run_no_phone_field_does_not_mark_unavailable(self):
        state: dict = {}
        ns._record_attempt(state, "https://s.com", {
            "at": _ISO, "channel": "phone", "result": "no_phone_field",
            "dry_run": True,
        })
        assert state["https://s.com"]["phone"] is None


class TestShouldSkipPhase3:
    def test_unavailable_phone_counts_as_done(self):
        state = {"https://s.com": {
            "email": {"signed_up_at": _ISO},
            "phone": {"unavailable": True, "checked_at": _ISO},
            "attempts": [],
        }}
        assert ns._should_skip(
            "https://s.com", state, ["email", "phone"], retry_failed=False,
        ) is True


# ---------------------------------------------------------------------------
# Phase 3 — _signup_in_popup orchestration (popup_detect helpers monkeypatched)
# ---------------------------------------------------------------------------

class TestSignupInPopup:
    def _patch(self, monkeypatch, *, email_field=None, phone_field=None,
              submit=None, success=True, code=None, visible_text=""):
        """Patch the popup_detect helpers _signup_in_popup calls so we can drive
        its branching with simple fakes. ``phone_field`` may be a list to script
        successive find_phone_field() return values (step 1 then step 2)."""
        monkeypatch.setattr(ns, "find_email_field", lambda popup, **k: email_field)
        if isinstance(phone_field, list):
            seq = iter(phone_field)
            monkeypatch.setattr(
                ns, "find_phone_field", lambda popup, **k: next(seq, None),
            )
        else:
            monkeypatch.setattr(ns, "find_phone_field", lambda popup, **k: phone_field)
        monkeypatch.setattr(ns, "find_submit_button", lambda popup, **k: submit)
        monkeypatch.setattr(ns, "detect_success", lambda page, popup, **k: (success, code))
        monkeypatch.setattr(ns, "check_consent_if_present", lambda popup, **k: False)
        monkeypatch.setattr(ns, "_visible_text", lambda page, popup: visible_text)
        monkeypatch.setattr(ns, "_screenshot", lambda page, path: None)
        monkeypatch.setattr(ns, "detect_popup", lambda page, **k: (None, None))

    class _Page:
        """Bare page stand-in: only the settle-retry's wait is ever called."""

        def wait_for_timeout(self, ms: int) -> None:  # noqa: ARG002
            pass

    def _call(self, channels, **kw):
        return ns._signup_in_popup(
            page=self._Page(), popup=object(), vendor="klaviyo",
            email="user@gmail.com", phone="+15555550100",
            channels=channels, dry_run=kw.pop("dry_run", False),
            now_iso=_ISO, shop="https://s.com", **kw,
        )

    def test_email_only_popup_success(self, monkeypatch):
        ef, sb = FakeField(), FakeField()
        self._patch(monkeypatch, email_field=ef, submit=sb, code="WELCOME15")
        out = self._call(["email"])
        assert [(r["channel"], r["result"]) for r in out] == [("email", "success")]
        assert out[0]["code_received"] == "WELCOME15"
        assert ef.fills == ["user@gmail.com"] and sb.clicks == 1

    def test_single_form_both_fields_success(self, monkeypatch):
        ef, pf, sb = FakeField(), FakeField(), FakeField()
        self._patch(monkeypatch, email_field=ef, phone_field=pf, submit=sb, code="X")
        out = self._call(["email", "phone"])
        assert [(r["channel"], r["result"]) for r in out] == [
            ("email", "success"), ("phone", "success"),
        ]
        # One submit covers both fields.
        assert sb.clicks == 1
        assert pf.fills == ["+15555550100"]
        # Phone record never carries the code.
        phone_rec = [r for r in out if r["channel"] == "phone"][0]
        assert phone_rec["code_received"] is None

    def test_phone_only_popup_success(self, monkeypatch):
        pf, sb = FakeField(), FakeField()
        self._patch(monkeypatch, phone_field=pf, submit=sb)
        out = self._call(["phone"])
        assert [(r["channel"], r["result"]) for r in out] == [("phone", "success")]

    def test_phone_otp_prompt_recorded(self, monkeypatch):
        pf, sb = FakeField(), FakeField()
        self._patch(monkeypatch, phone_field=pf, submit=sb,
                    visible_text="Enter the verification code we just sent you")
        out = self._call(["phone"])
        assert out == [{
            "at": _ISO, "channel": "phone", "result": "requires_otp",
            "vendor": "klaviyo", "code_received": None, "dry_run": None,
        }]

    def test_email_only_shop_marks_phone_unavailable(self, monkeypatch):
        # Popup has email + submit but no phone field, even after submit (step 2
        # re-detect also finds nothing).
        ef, sb = FakeField(), FakeField()
        self._patch(monkeypatch, email_field=ef, phone_field=[None, None], submit=sb)
        out = self._call(["email", "phone"])
        results = {(r["channel"], r["result"]) for r in out}
        assert ("email", "success") in results
        assert ("phone", "no_phone_field") in results

    def test_email_first_phone_second_multistep(self, monkeypatch):
        ef, pf, sb = FakeField(), FakeField(), FakeField()
        # Step 1: no phone field. Step 2 (after email submit): phone appears.
        self._patch(monkeypatch, email_field=ef, phone_field=[None, pf], submit=sb)
        out = self._call(["email", "phone"])
        results = {(r["channel"], r["result"]) for r in out}
        assert ("email", "success") in results
        assert ("phone", "success") in results
        assert pf.fills == ["+15555550100"]

    def test_not_a_form_when_no_submit(self, monkeypatch):
        ef = FakeField()
        self._patch(monkeypatch, email_field=ef, submit=None)
        out = self._call(["email"])
        assert [(r["channel"], r["result"]) for r in out] == [("email", "form_fill_failed")]
        assert ef.fills == []  # never filled — no submit button

    def test_settle_retry_recovers_late_rendering_form(self, monkeypatch):
        """A popup whose fields render in late (entrance animation) is
        re-scanned once after a settle wait instead of being written off
        as unfillable — observed live (issue #14)."""
        ef, sb = FakeField(), FakeField()
        self._patch(monkeypatch, email_field=None, submit=None)
        email_seq = iter([None, ef])
        submit_seq = iter([None, sb])
        monkeypatch.setattr(
            ns, "find_email_field", lambda popup, **k: next(email_seq, ef),
        )
        monkeypatch.setattr(
            ns, "find_submit_button", lambda popup, **k: next(submit_seq, sb),
        )
        out = self._call(["email"])
        assert [(r["channel"], r["result"]) for r in out] == [("email", "success")]
        assert ef.fills == ["user@gmail.com"] and sb.clicks == 1

    def test_dry_run_reports_without_submitting(self, monkeypatch):
        ef, pf, sb = FakeField(), FakeField(), FakeField()
        self._patch(monkeypatch, email_field=ef, phone_field=pf, submit=sb)
        out = self._call(["email", "phone"], dry_run=True)
        results = {(r["channel"], r["result"]) for r in out}
        assert results == {("email", "success"), ("phone", "success")}
        assert all(r["dry_run"] is True for r in out)
        # Nothing actually filled or clicked.
        assert ef.fills == [] and pf.fills == [] and sb.clicks == 0

    def test_email_submit_failure_recorded(self, monkeypatch):
        ef, sb = FakeField(), FakeField()
        self._patch(monkeypatch, email_field=ef, submit=sb, success=False)
        out = self._call(["email"])
        assert [(r["channel"], r["result"]) for r in out] == [
            ("email", "form_fill_failed"),
        ]
