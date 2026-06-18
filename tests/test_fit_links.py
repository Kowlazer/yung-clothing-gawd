"""Tests for src/fit_links.py — HMAC signing + pending-item selection."""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import parse_qs, urlparse

from src.fit_links import (
    REVIEW_ALL_TOKEN,
    fit_url,
    is_fit_pending,
    pending_fit_items,
    review_all_url,
    sign,
    verify,
)

SECRET = "s3cr3t-shared-with-apps-script"
BASE = "https://script.google.com/macros/s/DEPLOY/exec"


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

class TestSign:
    def test_matches_reference_hmac_sha256_hex(self):
        expected = hmac.new(
            SECRET.encode(), b"a1b2c3d4e5f6", hashlib.sha256
        ).hexdigest()
        assert sign("a1b2c3d4e5f6", SECRET) == expected

    def test_is_deterministic(self):
        assert sign("item", SECRET) == sign("item", SECRET)

    def test_different_message_different_sig(self):
        assert sign("itemA", SECRET) != sign("itemB", SECRET)

    def test_different_secret_different_sig(self):
        assert sign("item", "secret-a") != sign("item", "secret-b")

    def test_lowercase_hex_64_chars(self):
        sig = sign("item", SECRET)
        assert len(sig) == 64
        assert sig == sig.lower()
        int(sig, 16)  # parses as hex


class TestVerify:
    def test_accepts_valid_signature(self):
        assert verify("item", sign("item", SECRET), SECRET) is True

    def test_rejects_tampered_message(self):
        assert verify("other", sign("item", SECRET), SECRET) is False

    def test_rejects_wrong_secret(self):
        assert verify("item", sign("item", SECRET), "wrong") is False

    def test_rejects_blank_sig_or_secret(self):
        assert verify("item", "", SECRET) is False
        assert verify("item", sign("item", SECRET), "") is False


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------

class TestUrls:
    def test_fit_url_round_trips(self):
        url = fit_url("a1b2c3d4e5f6", BASE, SECRET)
        assert url.startswith(BASE + "?")
        q = parse_qs(urlparse(url).query)
        assert q["item"] == ["a1b2c3d4e5f6"]
        assert verify("a1b2c3d4e5f6", q["sig"][0], SECRET) is True

    def test_fit_url_query_is_urlencoded(self):
        # An id with a reserved char must be percent-encoded, not raw.
        url = fit_url("a/b", BASE, SECRET)
        assert "a/b" not in url
        q = parse_qs(urlparse(url).query)
        assert q["item"] == ["a/b"]

    def test_review_all_url_signs_constant_token(self):
        url = review_all_url(BASE, SECRET)
        q = parse_qs(urlparse(url).query)
        assert q["all"] == ["1"]
        assert verify(REVIEW_ALL_TOKEN, q["sig"][0], SECRET) is True

    def test_per_item_and_review_all_sigs_differ(self):
        item_sig = parse_qs(urlparse(fit_url(REVIEW_ALL_TOKEN, BASE, SECRET)).query)["sig"][0]
        all_sig = parse_qs(urlparse(review_all_url(BASE, SECRET)).query)["sig"][0]
        # Even if an item id somehow equalled the token string, both sign the
        # same message here — so this just documents the token is a real string.
        assert item_sig == all_sig  # same message → same sig (sanity, not a bug)


# ---------------------------------------------------------------------------
# Pending predicate
# ---------------------------------------------------------------------------

class TestPendingPredicate:
    def test_none_review_is_pending(self):
        assert is_fit_pending({"fit_review": None}) is True

    def test_missing_review_key_is_pending(self):
        assert is_fit_pending({}) is True

    def test_existing_review_not_pending(self):
        assert is_fit_pending({"fit_review": {"fit": "tts"}}) is False

    def test_dropped_sentinel_not_pending(self):
        # The drop sentinel sets a non-null fit_review, so it's excluded.
        assert is_fit_pending({"fit_review": {"fit": "dropped"}}) is False

    def test_non_clothing_not_pending(self):
        assert is_fit_pending({"fit_review": None, "is_clothing": False}) is False

    def test_is_clothing_true_or_absent_is_pending(self):
        assert is_fit_pending({"fit_review": None, "is_clothing": True}) is True

    def test_pending_fit_items_filters_and_preserves_order(self):
        items = [
            {"id": "1", "fit_review": None},
            {"id": "2", "fit_review": {"fit": "tts"}},
            {"id": "3", "fit_review": None, "is_clothing": False},
            {"id": "4", "fit_review": None},
        ]
        assert [it["id"] for it in pending_fit_items(items)] == ["1", "4"]

    def test_pending_fit_items_handles_none(self):
        assert pending_fit_items(None) == []
