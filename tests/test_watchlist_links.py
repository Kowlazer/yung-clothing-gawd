"""Tests for src/watchlist_links.py — namespaced removal links + pending select."""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import parse_qs, urlparse

from src.fit_links import fit_url, review_all_url, sign, verify
from src.watchlist_links import (
    REMOVAL_ALL_TOKEN,
    is_removal_pending,
    pending_removal_items,
    removal_all_url,
    removal_message,
    removal_url,
)

SECRET = "s3cr3t-shared-with-apps-script"
BASE = "https://script.google.com/macros/s/DEPLOY/exec"


# ---------------------------------------------------------------------------
# Message construction + signing
# ---------------------------------------------------------------------------

class TestRemovalMessage:
    def test_prefixes_the_id(self):
        assert removal_message("a1b2c3d4e5f6") == "remove:a1b2c3d4e5f6"

    def test_signs_to_reference_hmac(self):
        expected = hmac.new(
            SECRET.encode(), b"remove:a1b2c3d4e5f6", hashlib.sha256
        ).hexdigest()
        assert sign(removal_message("a1b2c3d4e5f6"), SECRET) == expected


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------

class TestRemovalUrls:
    def test_removal_url_round_trips(self):
        url = removal_url("a1b2c3d4e5f6", BASE, SECRET)
        assert url.startswith(BASE + "?")
        q = parse_qs(urlparse(url).query)
        assert q["remove"] == ["a1b2c3d4e5f6"]
        assert verify(removal_message("a1b2c3d4e5f6"), q["sig"][0], SECRET) is True

    def test_removal_url_query_is_urlencoded(self):
        url = removal_url("a/b", BASE, SECRET)
        assert "remove=a/b" not in url
        q = parse_qs(urlparse(url).query)
        assert q["remove"] == ["a/b"]

    def test_removal_all_url_signs_constant_token(self):
        url = removal_all_url(BASE, SECRET)
        q = parse_qs(urlparse(url).query)
        assert q["removeall"] == ["1"]
        assert verify(REMOVAL_ALL_TOKEN, q["sig"][0], SECRET) is True


# ---------------------------------------------------------------------------
# Namespacing — a removal link must NOT be interchangeable with a fit link
# ---------------------------------------------------------------------------

class TestNamespacing:
    def test_removal_sig_differs_from_fit_sig_for_same_id(self):
        item_id = "a1b2c3d4e5f6"
        fit_sig = parse_qs(urlparse(fit_url(item_id, BASE, SECRET)).query)["sig"][0]
        rm_sig = parse_qs(urlparse(removal_url(item_id, BASE, SECRET)).query)["sig"][0]
        assert fit_sig != rm_sig

    def test_fit_sig_does_not_verify_as_removal(self):
        item_id = "a1b2c3d4e5f6"
        fit_sig = parse_qs(urlparse(fit_url(item_id, BASE, SECRET)).query)["sig"][0]
        # A leaked fit link can't be replayed against the removal endpoint.
        assert verify(removal_message(item_id), fit_sig, SECRET) is False

    def test_removal_all_token_differs_from_review_all(self):
        rm_all = parse_qs(urlparse(removal_all_url(BASE, SECRET)).query)["sig"][0]
        review_all = parse_qs(urlparse(review_all_url(BASE, SECRET)).query)["sig"][0]
        assert rm_all != review_all


# ---------------------------------------------------------------------------
# Pending predicate
# ---------------------------------------------------------------------------

class TestPendingPredicate:
    def test_pending_when_match_and_no_decision(self):
        assert is_removal_pending(
            {"watchlist_match": {"matched_line": "x", "approved_for_removal": None}}
        ) is True

    def test_not_pending_when_no_match(self):
        assert is_removal_pending({}) is False
        assert is_removal_pending({"watchlist_match": None}) is False

    def test_not_pending_when_already_approved(self):
        assert is_removal_pending(
            {"watchlist_match": {"approved_for_removal": True}}
        ) is False

    def test_not_pending_when_declined(self):
        assert is_removal_pending(
            {"watchlist_match": {"approved_for_removal": False}}
        ) is False

    def test_missing_decision_key_treated_as_pending(self):
        # A match dict without the key at all is still an undecided candidate.
        assert is_removal_pending({"watchlist_match": {"matched_line": "x"}}) is True

    def test_filters_and_preserves_order(self):
        items = [
            {"id": "1", "watchlist_match": {"approved_for_removal": None}},
            {"id": "2", "watchlist_match": {"approved_for_removal": True}},
            {"id": "3"},
            {"id": "4", "watchlist_match": {"approved_for_removal": None}},
        ]
        assert [it["id"] for it in pending_removal_items(items)] == ["1", "4"]

    def test_handles_none(self):
        assert pending_removal_items(None) == []
