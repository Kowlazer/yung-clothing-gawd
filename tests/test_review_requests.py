"""Tests for src/review_requests.py — pure dedup + link logic.

The IMAP fetch lives in gmail.py (tested there); here we cover the pure
helpers: query/link building, shop attribution, order-id parsing, subject
normalization, the dedupe key, and the end-to-end ``dedupe`` collapsing.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src import review_requests as rr


# ---------------------------------------------------------------------------
# Query + link builders
# ---------------------------------------------------------------------------

class TestQueries:
    def test_subject_query_is_subject_anchored_or_group(self):
        assert rr._SUBJECT_QUERY.startswith("subject:(")
        assert rr._SUBJECT_QUERY.endswith(")")
        assert "review" in rr._SUBJECT_QUERY
        assert '"how did it go"' in rr._SUBJECT_QUERY
        assert " OR " in rr._SUBJECT_QUERY

    def test_search_query_bounds_window(self):
        q = rr.search_query(30)
        assert "newer_than:30d" in q
        assert rr._SUBJECT_QUERY in q

    def test_search_query_respects_days(self):
        assert "newer_than:7d" in rr.search_query(7)

    def test_all_requests_url_is_unbounded_search_link(self):
        url = rr.all_requests_url()
        assert url.startswith("https://mail.google.com/mail/u/0/#search/")
        # All-time → no date bound.
        assert "newer_than" not in url
        # Fully URL-encoded — no raw spaces or quotes leak into the link.
        assert " " not in url
        assert '"' not in url


class TestIsReviewRequest:
    def test_standard_requests_pass(self):
        for subj in [
            "Reminder: Order #138880, how did it go?",
            "Leave a review. Earn HT Rewards points.",
            "Alex, review your purchase from Maple Co",
            "Hi Alex, share your thoughts about the Trailhead Hoodie",
            "Tell us what you think",
            "So, what do you think?",
            "How's your new masterpiece?",
            "You rated us 5 stars — one more favor?",
            "Did your recent order meet your expectations? Review it",
            "How are you enjoying your purchase?",
            "Reminder: Add a photo or a video to your review of Saitama Tank",
            "Alex, you have a new item to review.",
        ]:
            assert rr.is_review_request(subj), subj

    def test_already_reviewed_confirmations_rejected(self):
        for subj in [
            "Thank you for reviewing Saitama Bonsai Tank Top",
            "Thanks for reviewing THE OTISHI 2.0",
            "Thank you for your review!",
        ]:
            assert not rr.is_review_request(subj), subj

    def test_order_lifecycle_noise_rejected(self):
        for subj in [
            "Your Order is Out For Delivery",
            "Your order has arrived! See what's inside.",
            "Order #50444 confirmed",
            "Your Passport to Paradise Awaits!",
        ]:
            assert not rr.is_review_request(subj), subj

    def test_non_shopping_review_words_rejected(self):
        # Bare review/rate/feedback in non-product contexts must NOT match.
        for subj in [
            "Check Your Personal Loan Rate Now",          # bank
            "Alex, review your Google Account settings",  # security
            "[GitHub] Please review this sign in",        # security
            "Action Required: Review unusual activity on your card",
            "We want your feedback regarding case 88175777",  # USPS
            "The Feedback Loop That Changes the Curve",   # newsletter
            "Please read. It’s not what you think it is.",  # marketing
            "We are reviewing our catalog",               # "reviewing"
        ]:
            assert not rr.is_review_request(subj), subj

    def test_review_platform_sender_rescues_offpattern_subject(self):
        # Catgirl Riot's "Fast review …" via Loox: off-pattern, but the platform
        # sender vouches for it.
        subj = "Fast review ➡️ furious discount. Order #7663"
        assert rr.is_review_request(subj, "Catgirl Riot <no-reply@loox.io>")
        # Same subject from a non-platform sender stays rejected.
        assert not rr.is_review_request(subj, "Catgirl Riot <hi@catgirlriot.com>")

    def test_platform_rescue_still_respects_exclude(self):
        # A thank-you from a platform is still a confirmation, not a request.
        assert not rr.is_review_request(
            "Thank you for reviewing X", "Shop <no-reply@loox.io>",
        )

    def test_genuine_request_with_leading_thanks_not_excluded(self):
        # "Thanks for your order — leave a review" is a request, not a
        # "thank you for reviewing" confirmation: the exclude must not over-fire.
        assert rr.is_review_request("Thanks for your order — leave a review!")
        assert rr.is_review_request("Thank you for shopping! How did it go?")

    def test_unanchored_tokens_do_not_false_match(self):
        # "review it" must not match "Preview it"; "rate ..." must not match
        # inside "accurate".
        assert not rr.is_review_request("Preview it now — new drop just landed")
        assert not rr.is_review_request("Our accurate us-based shipping update")


class TestSenderIsReviewPlatform:
    def test_known_platform(self):
        assert rr._sender_is_review_platform("Shop <no-reply@loox.io>")
        assert rr._sender_is_review_platform("x@judge.me")

    def test_platform_subdomain(self):
        assert rr._sender_is_review_platform("x@mail.okendo.io")

    def test_non_platform(self):
        assert not rr._sender_is_review_platform("x@amazon.com")
        assert not rr._sender_is_review_platform("")


class TestEmailPermalink:
    def test_hex_all_link_is_primary(self):
        # Direct-open #all/<hex> wins when the X-GM-MSGID is present.
        # 1234567890 -> hex 499602d2
        url = rr.email_permalink("<abc123@mail.gmail.com>", gm_id="1234567890")
        assert url == "https://mail.google.com/mail/u/0/#all/499602d2"

    def test_hex_link_small_id(self):
        assert rr.email_permalink(None, gm_id="255") == (
            "https://mail.google.com/mail/u/0/#all/ff"
        )

    def test_rfc822msgid_fallback_when_no_gm_id(self):
        url = rr.email_permalink("<abc123@mail.gmail.com>")
        assert url == (
            "https://mail.google.com/mail/u/0/#search/"
            "rfc822msgid:abc123@mail.gmail.com"
        )

    def test_rfc822msgid_fallback_when_gm_id_invalid(self):
        url = rr.email_permalink("abc@x.com", gm_id="not-a-number")
        assert url.endswith("rfc822msgid:abc@x.com")

    def test_strips_angle_brackets_on_fallback(self):
        url = rr.email_permalink("<abc@x.com>")
        assert url.endswith("rfc822msgid:abc@x.com")

    def test_none_when_no_identifiers(self):
        assert rr.email_permalink(None, None) is None
        assert rr.email_permalink("", "") is None

    def test_none_when_gm_id_invalid_and_no_message_id(self):
        assert rr.email_permalink(None, gm_id="not-a-number") is None


# ---------------------------------------------------------------------------
# Shop attribution
# ---------------------------------------------------------------------------

class TestShopFromSender:
    def test_display_name_is_the_shop(self):
        assert rr._shop_from_sender(
            "Suzushii Clothing <no-reply@loox.io>"
        ) == "Suzushii Clothing"

    def test_strips_reviews_suffix(self):
        assert rr._shop_from_sender("Aniqi Reviews <x@y.com>") == "Aniqi"

    def test_strips_team_suffix(self):
        assert rr._shop_from_sender("Pomelo Team <x@y.com>") == "Pomelo"

    def test_strips_via_platform(self):
        assert rr._shop_from_sender("BibiSama via Yotpo <x@y.com>") == "BibiSama"

    def test_falls_back_to_domain_when_no_name(self):
        assert rr._shop_from_sender("<no-reply@loox.io>") == "loox.io"

    def test_generic_display_name_falls_back_to_domain(self):
        # "no-reply" as a display name is a role address, not the shop.
        assert rr._shop_from_sender("no-reply <no-reply@amazon.com>") == "amazon.com"

    def test_info_display_name_falls_back_to_domain(self):
        assert rr._shop_from_sender("info <info@shop.com>") == "shop.com"

    def test_bare_address_falls_back_to_domain(self):
        assert rr._shop_from_sender("no-reply@loox.io") == "loox.io"

    def test_strips_www_in_domain_fallback(self):
        assert rr._shop_from_sender("feedback@www.shop.com") == "shop.com"


# ---------------------------------------------------------------------------
# Order-id parsing
# ---------------------------------------------------------------------------

class TestOrderId:
    def test_subject_order_hash(self):
        assert rr._order_id("Reminder: Order #138880, how did it go?", "") == "138880"

    def test_subject_order_no_keyword(self):
        assert rr._order_id("Your order no. 778899 — review it", "") == "778899"

    def test_subject_order_number_keyword(self):
        assert rr._order_id("order number 5567123 feedback", "") == "5567123"

    def test_bare_hash_in_subject(self):
        assert rr._order_id("We'd love your feedback #55512", "") == "55512"

    def test_body_keyword_when_subject_generic(self):
        assert rr._order_id(
            "How did it go?", "Hi! Your order no. 4321009 shipped. Leave a review.",
        ) == "4321009"

    def test_alphanumeric_order_id(self):
        assert rr._order_id("Order #SC1234 review", "") == "SC1234"

    def test_none_when_no_order(self):
        assert rr._order_id("How are you enjoying your purchase?", "no numbers here") is None

    def test_short_numbers_not_treated_as_order(self):
        # "#12" is too short to be an order number.
        assert rr._order_id("Top 12 picks for you", "") is None

    def test_uppercased(self):
        assert rr._order_id("order #ab999", "") == "AB999"


# ---------------------------------------------------------------------------
# Subject normalization + dedupe key
# ---------------------------------------------------------------------------

class TestNormalizeSubject:
    def test_strips_reminder_prefix_digits_punct(self):
        assert rr._normalize_subject(
            "Reminder: Order #138880, how did it go?"
        ) == "order how did it go"

    def test_strips_re_prefix(self):
        assert rr._normalize_subject("Re: How did we do?") == "how did we do"

    def test_strips_stacked_prefixes(self):
        assert rr._normalize_subject("Re: Fwd: Reminder: Leave a review!") == "leave a review"

    def test_generic_subjects_collapse_identically(self):
        a = rr._normalize_subject("How are you enjoying your purchase?")
        b = rr._normalize_subject("How are you enjoying your purchase?!")
        assert a == b == "how are you enjoying your purchase"


class TestDedupeKey:
    def test_order_path(self):
        assert rr._dedupe_key("Aniqi", "138880", "x") == ("aniqi", "order:138880")

    def test_subject_path_when_no_order(self):
        assert rr._dedupe_key("Aniqi", None, "How did we do?") == (
            "aniqi", "subj:how did we do",
        )

    def test_shop_lowercased(self):
        assert rr._dedupe_key("ANIQI", "1", "x")[0] == "aniqi"


# ---------------------------------------------------------------------------
# dedupe — end to end
# ---------------------------------------------------------------------------

class TestDedupe:
    _NOW = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)

    def _email(self, mid, frm, subject, date, body="", message_id=None):
        return {"id": mid, "from": frm, "subject": subject, "body_text": body,
                "date": date, "message_id": message_id or f"<{mid}@x.com>"}

    def test_empty_input(self):
        assert rr.dedupe([], now=self._NOW) == []

    def test_reminders_for_same_order_collapse_to_most_recent(self):
        emails = [
            self._email("1", "Suzushii Clothing <no-reply@loox.io>",
                        "Reminder: Order #138880, how did it go?",
                        "Tue, 02 Jun 2026 19:32:00 +0000"),
            self._email("2", "Suzushii Clothing <no-reply@loox.io>",
                        "Order #138880, how did it go?",
                        "Wed, 27 May 2026 10:00:00 +0000"),
        ]
        out = rr.dedupe(emails, now=self._NOW)
        assert len(out) == 1
        assert out[0]["shop"] == "Suzushii Clothing"
        assert out[0]["date_iso"] == "2026-06-02"
        assert out[0]["days_ago"] == 5
        # Direct-open #all/<hex> built from the winning email's X-GM-MSGID ("1").
        assert out[0]["url"] == "https://mail.google.com/mail/u/0/#all/1"

    def test_distinct_orders_kept_separately(self):
        emails = [
            self._email("1", "Shop <r@loox.io>", "Order #111000 how did it go?",
                        "Tue, 02 Jun 2026 19:32:00 +0000"),
            self._email("2", "Shop <r@loox.io>", "Order #222000 how did it go?",
                        "Wed, 03 Jun 2026 10:00:00 +0000"),
        ]
        out = rr.dedupe(emails, now=self._NOW)
        assert len(out) == 2

    def test_generic_subject_collapses_without_order(self):
        emails = [
            self._email("1", "Shop <r@judge.me>", "How are you enjoying your order?",
                        "Tue, 02 Jun 2026 19:32:00 +0000"),
            self._email("2", "Shop <r@judge.me>", "How are you enjoying your order?",
                        "Sun, 31 May 2026 10:00:00 +0000"),
        ]
        out = rr.dedupe(emails, now=self._NOW)
        assert len(out) == 1
        assert out[0]["date_iso"] == "2026-06-02"

    def test_sorted_newest_first(self):
        emails = [
            self._email("1", "A <r@loox.io>", "Order #1 how did it go?",
                        "Mon, 01 Jun 2026 10:00:00 +0000"),
            self._email("2", "B <r@loox.io>", "Order #2 how did it go?",
                        "Fri, 05 Jun 2026 10:00:00 +0000"),
            self._email("3", "C <r@loox.io>", "Order #3 how did it go?",
                        "Wed, 03 Jun 2026 10:00:00 +0000"),
        ]
        out = rr.dedupe(emails, now=self._NOW)
        assert [r["shop"] for r in out] == ["B", "C", "A"]

    def test_unparseable_date_sorts_last_and_days_ago_none(self):
        emails = [
            self._email("1", "Dated <r@loox.io>", "Order #1 how did it go?",
                        "Fri, 05 Jun 2026 10:00:00 +0000"),
            self._email("2", "Undated <r@loox.io>", "Order #2 how did it go?",
                        "not a real date"),
        ]
        out = rr.dedupe(emails, now=self._NOW)
        assert out[-1]["shop"] == "Undated"
        assert out[-1]["days_ago"] is None
        assert out[-1]["date_iso"] is None

    def test_long_subject_clipped(self):
        long_subject = "Order #999 how did it go? " + "x" * 300
        out = rr.dedupe(
            [self._email("1", "Shop <r@loox.io>", long_subject,
                         "Fri, 05 Jun 2026 10:00:00 +0000")],
            now=self._NOW,
        )
        assert len(out[0]["subject"]) <= rr._MAX_SUBJECT
        assert out[0]["subject"].endswith("…")

    def test_non_request_subjects_filtered_out(self):
        emails = [
            self._email("1", "Real <r@loox.io>", "Order #1 how did it go?",
                        "Fri, 05 Jun 2026 10:00:00 +0000"),
            self._email("2", "JUNK Brands <r@x.com>", "Your Order is Out For Delivery",
                        "Fri, 05 Jun 2026 10:00:00 +0000"),
            self._email("3", "Otishi <r@loox.io>",
                        "Thank you for reviewing THE OTISHI 2.0",
                        "Fri, 05 Jun 2026 10:00:00 +0000"),
        ]
        out = rr.dedupe(emails, now=self._NOW)
        assert [r["shop"] for r in out] == ["Real"]
