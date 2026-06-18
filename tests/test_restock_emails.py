"""Tests for src/restock_emails.py — classifier, parsing, dedupe."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src import restock_emails as re_


def _email(eid, frm, subject, body="", date="Sat, 13 Jun 2026 09:00:00 +0000",
           message_id="<m1@shop.com>"):
    return {"id": eid, "from": frm, "subject": subject, "body_text": body,
            "date": date, "message_id": message_id}


# ---------------------------------------------------------------------------
# is_restock_email
# ---------------------------------------------------------------------------

class TestIsRestockEmail:
    @pytest.mark.parametrize("subject", [
        "Good news! The Aros Chino is back in stock",
        "Back in stock: Merino Crew",
        "Your size is now available",
        "It's back! Grab it before it's gone",
        "The Hoodie is available again",
        "Restocked: Cargo Pant",
        "Back in your size — Linen Shirt",
        # The most common BIS phrasing — must NOT be dropped by a bare "signed up".
        "The item you signed up for is back in stock",
        # Restock + urgency in one subject — scarcity must not disqualify it.
        "Back in stock — selling fast!",
    ])
    def test_positive(self, subject):
        assert re_.is_restock_email(subject)

    @pytest.mark.parametrize("subject", [
        "Back in stock soon — join the waitlist",
        "New arrivals just dropped",
        "Pre-order the new collection",
        "Almost gone — selling fast",
        "You'll be notified when the Hoodie is back in stock",  # signup ack
        "Thanks for signing up for back in stock alerts",
        "Low stock on your favorites",
        "20% off everything this weekend",
    ])
    def test_negative(self, subject):
        assert not re_.is_restock_email(subject)


# ---------------------------------------------------------------------------
# extract_item
# ---------------------------------------------------------------------------

class TestExtractItem:
    def test_after_colon(self):
        assert re_.extract_item("Back in stock: Merino Crew Sweater") == "Merino Crew Sweater"

    def test_before_phrase(self):
        assert re_.extract_item("The Aros Chino is back in stock") == "Aros Chino"

    def test_before_phrase_now_available(self):
        assert re_.extract_item("Linen Shirt is now available") == "Linen Shirt"

    def test_strips_leading_filler(self):
        assert re_.extract_item("Good news! Cargo Pant is back in stock") == "Cargo Pant"

    def test_unrecognised_returns_none(self):
        assert re_.extract_item("It's back!") is None


# ---------------------------------------------------------------------------
# extract_size
# ---------------------------------------------------------------------------

class TestExtractSize:
    def test_in_size(self):
        assert re_.extract_size("Now available in size M") == "M"

    def test_size_colon(self):
        assert re_.extract_size("Restocked", "Size: L is back") == "L"

    def test_parenthetical(self):
        assert re_.extract_size("Merino Crew (XL) is back in stock") == "XL"

    def test_none_when_absent(self):
        assert re_.extract_size("Back in stock: Merino Crew") is None

    @pytest.mark.parametrize("subject", [
        "Now available — check our size guide",
        "Back in stock: should you size up?",
    ])
    def test_non_size_words_not_extracted(self, subject):
        assert re_.extract_size(subject) is None


# ---------------------------------------------------------------------------
# dedupe
# ---------------------------------------------------------------------------

class TestDedupe:
    _NOW = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)

    def test_filters_non_restock(self):
        emails = [
            _email("1", "Aniqi <hi@aniqi.com>", "New arrivals just dropped"),
            _email("2", "Aniqi <hi@aniqi.com>", "Back in stock: Hoodie"),
        ]
        out = re_.dedupe(emails, now=self._NOW)
        assert len(out) == 1
        assert out[0]["item"] == "Hoodie"

    def test_dedupes_per_shop_item_newest_wins(self):
        emails = [
            _email("1", "Aniqi <hi@aniqi.com>", "Back in stock: Hoodie",
                   date="Wed, 10 Jun 2026 09:00:00 +0000"),
            _email("2", "Aniqi <hi@aniqi.com>", "Back in stock: Hoodie",
                   date="Fri, 12 Jun 2026 09:00:00 +0000"),
        ]
        out = re_.dedupe(emails, now=self._NOW)
        assert len(out) == 1
        assert out[0]["date_iso"] == "2026-06-12"

    def test_render_shape(self):
        out = re_.dedupe(
            [_email("1", "Norse Projects <no-reply@klaviyo.com>",
                    "Aros Chino is back in stock in size M")],
            now=self._NOW,
        )
        r = out[0]
        assert r["shop"] == "Norse Projects"
        assert r["item"] == "Aros Chino"
        assert r["size"] == "M"
        assert r["days_ago"] == 0
        assert r["url"].startswith("https://mail.google.com/mail/u/0/#all/")

    def test_distinct_items_kept(self):
        emails = [
            _email("1", "Aniqi <hi@aniqi.com>", "Back in stock: Hoodie"),
            _email("2", "Aniqi <hi@aniqi.com>", "Back in stock: Tee"),
        ]
        out = re_.dedupe(emails, now=self._NOW)
        assert {r["item"] for r in out} == {"Hoodie", "Tee"}


# ---------------------------------------------------------------------------
# query + link
# ---------------------------------------------------------------------------

class TestQueryAndLink:
    def test_search_query_has_window(self):
        assert "newer_than:7d" in re_.search_query(7)
        assert "subject:(" in re_.search_query(7)

    def test_all_url(self):
        url = re_.all_restocks_url()
        assert url.startswith("https://mail.google.com/mail/u/0/#search/")
