"""Unit tests for src/browser_fetch.py — the pure parts only.

The actual Chromium render path is exercised live (see issue #1); here we
cover the launch budget and the env parsing, which is everything that can
misbehave without a browser present.
"""

from __future__ import annotations

import pytest

from src import browser_fetch


@pytest.fixture(autouse=True)
def _reset_budget(monkeypatch):
    """Each test starts with an unspent budget, unfired warning, clean cache."""
    monkeypatch.setattr(browser_fetch, "_attempts", 0)
    monkeypatch.setattr(browser_fetch, "_budget_warned", False)
    monkeypatch.setattr(browser_fetch, "_blocked_domains", set())


class TestReadMaxAttempts:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("BROWSER_FALLBACK_MAX_ITEMS", raising=False)
        assert browser_fetch._read_max_attempts() == 8

    def test_explicit_value(self, monkeypatch):
        monkeypatch.setenv("BROWSER_FALLBACK_MAX_ITEMS", "3")
        assert browser_fetch._read_max_attempts() == 3

    def test_zero_disables(self, monkeypatch):
        monkeypatch.setenv("BROWSER_FALLBACK_MAX_ITEMS", "0")
        assert browser_fetch._read_max_attempts() == 0

    def test_garbage_falls_back(self, monkeypatch):
        monkeypatch.setenv("BROWSER_FALLBACK_MAX_ITEMS", "lots")
        assert browser_fetch._read_max_attempts() == 8

    def test_negative_falls_back(self, monkeypatch):
        monkeypatch.setenv("BROWSER_FALLBACK_MAX_ITEMS", "-2")
        assert browser_fetch._read_max_attempts() == 8


class TestBudget:
    def test_counts_up_to_max_then_refuses(self, monkeypatch):
        monkeypatch.setattr(browser_fetch, "_MAX_ATTEMPTS", 2)
        assert browser_fetch._take_attempt() is True
        assert browser_fetch._take_attempt() is True
        assert browser_fetch._take_attempt() is False
        assert browser_fetch._take_attempt() is False

    def test_exhausted_budget_short_circuits_fetch(self, monkeypatch):
        # With a zero budget, fetch_rendered_html returns None before ever
        # importing playwright — this test would hang/fail if it launched.
        monkeypatch.setattr(browser_fetch, "_MAX_ATTEMPTS", 0)
        assert browser_fetch.fetch_rendered_html("https://shop.com/p/x") is None

    def test_budget_warning_logged_once(self, monkeypatch, caplog):
        monkeypatch.setattr(browser_fetch, "_MAX_ATTEMPTS", 0)
        with caplog.at_level("WARNING", logger="src.browser_fetch"):
            browser_fetch._take_attempt()
            browser_fetch._take_attempt()
        warnings = [r for r in caplog.records
                    if "browser-render budget exhausted" in r.message]
        assert len(warnings) == 1


class TestBlockedDomainCache:
    def test_bot_blocked_domain_short_circuits_siblings(self, monkeypatch):
        # A domain recorded as bot-blocked is refused before the budget is
        # touched — 8 same-domain blocked items must cost one launch, not 8.
        monkeypatch.setattr(browser_fetch, "_blocked_domains", {"www.shop.com"})
        monkeypatch.setattr(browser_fetch, "_MAX_ATTEMPTS", 8)
        assert browser_fetch.fetch_rendered_html("https://www.shop.com/p/two") is None
        assert browser_fetch._attempts == 0

    def test_other_domains_unaffected(self, monkeypatch):
        monkeypatch.setattr(browser_fetch, "_blocked_domains", {"www.shop.com"})
        monkeypatch.setattr(browser_fetch, "_MAX_ATTEMPTS", 0)  # stop pre-launch
        # Different domain falls through the cache check to the budget gate
        # (which refuses at 0) — proving the cache is per-domain, not global.
        assert browser_fetch.fetch_rendered_html("https://other.com/p/x") is None
        assert browser_fetch._budget_warned is True
