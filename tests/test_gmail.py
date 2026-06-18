"""Tests for src/gmail.py (IMAP + App Password auth).

Strategy:
  * IMAP is mocked with a hand-rolled FakeIMAPClient that mimics enough of
    ``imaplib.IMAP4_SSL`` for fetch_promotions: ``select``, ``uid("SEARCH"/
    "FETCH", ...)``, ``logout``.
  * Pure helpers (_html_to_text, _sender_domain, _aliases_by_domain,
    _attribute, _extract_codes_from_text, _excerpt, _parse_fetch_response)
    are tested directly.
"""

from __future__ import annotations

import email
import imaplib
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from src.gmail import (
    DEFAULT_QUERY,
    _aliases_by_domain,
    _attribute,
    _excerpt,
    _extract_body_text,
    _extract_codes_from_text,
    _header,
    _html_to_text,
    _is_stub_plain,
    _parse_fetch_response,
    _parse_message,
    _sender_domain,
    _xgmraw_quote,
    extract_signals,
    fetch_promotions,
    fetch_restock_emails,
    fetch_review_requests,
    subscribed_shop_domains,
)


# ---------------------------------------------------------------------------
# FakeIMAPClient — minimal imaplib.IMAP4_SSL stand-in
# ---------------------------------------------------------------------------

class FakeIMAPClient:
    """Stand-in for ``imaplib.IMAP4_SSL``. Mimics select / uid / logout."""

    def __init__(
        self,
        search_uids: list[bytes] = (),
        fetches: dict[bytes, tuple[bytes, bytes]] | None = None,
        *,
        search_status: str = "OK",
        fetch_errors: set[bytes] | None = None,
    ):
        self._search_uids = list(search_uids)
        self._fetches = dict(fetches or {})
        self._search_status = search_status
        self._fetch_errors = fetch_errors or set()
        self.calls: list[tuple] = []

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return ("OK", [b""])

    def uid(self, command, *args):
        self.calls.append(("uid", command, args))
        if command == "SEARCH":
            if self._search_status != "OK":
                return (self._search_status, [b""])
            return ("OK", [b" ".join(self._search_uids)])
        if command == "FETCH":
            uid = args[0]
            if uid in self._fetch_errors:
                raise imaplib.IMAP4.error("simulated fetch error")
            if uid in self._fetches:
                meta, body = self._fetches[uid]
                return ("OK", [(meta, body), b")"])
            return ("NO", [b""])
        return ("BAD", [b"unknown"])

    def logout(self):
        self.calls.append(("logout",))
        return ("OK", [b"BYE"])


def _meta(uid: bytes, msgid: str) -> bytes:
    """Build the metadata preamble that real imaplib returns alongside fetched
    message bytes. The actual server format is e.g.
    ``b'1 (X-GM-MSGID 1701234 BODY[] {2345}'``."""
    return f"{uid.decode()} (X-GM-MSGID {msgid} BODY[] {{500}}".encode()


def _raw_text_message(
    subject: str, frm: str, body: str,
    *, content_type: str = "text/plain; charset=utf-8",
    date: str = "Sun, 19 May 2026 14:00:00 +0000",
) -> bytes:
    return (
        f"Subject: {subject}\r\n"
        f"From: {frm}\r\n"
        f"Date: {date}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"\r\n"
        f"{body}\r\n"
    ).encode("utf-8")


def _raw_multipart_message(
    subject: str, frm: str, plain: str, html: str,
) -> bytes:
    """Construct an RFC-822 multipart/alternative message."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = frm
    msg["Date"] = "Sun, 19 May 2026 14:00:00 +0000"
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


# ---------------------------------------------------------------------------
# fetch_promotions — IMAP integration with FakeIMAPClient
# ---------------------------------------------------------------------------

class TestFetchPromotions:
    def test_happy_path_returns_parsed_messages(self):
        raw1 = _raw_text_message(
            "Spring Sale!", "Aniqi <hello@aniqi.com>",
            "Use code SPRING30 at checkout",
        )
        raw2 = _raw_text_message(
            "New drop", "shop@bibisama.com", "Brand new collection",
        )
        fake = FakeIMAPClient(
            search_uids=[b"1", b"2"],
            fetches={
                b"1": (_meta(b"1", "10001"), raw1),
                b"2": (_meta(b"2", "10002"), raw2),
            },
        )
        out = fetch_promotions("user@gmail.com", "passwd", imap_client=fake)
        assert len(out) == 2
        assert out[0]["id"] == "10001"
        assert out[0]["subject"] == "Spring Sale!"
        assert out[0]["from"] == "Aniqi <hello@aniqi.com>"
        assert "SPRING30" in out[0]["body_text"]
        assert out[1]["id"] == "10002"

    def test_passes_query_via_x_gm_raw(self):
        fake = FakeIMAPClient(search_uids=[])
        fetch_promotions("u", "p", imap_client=fake, query="custom:query")
        search_calls = [c for c in fake.calls if c[0] == "uid" and c[1] == "SEARCH"]
        assert len(search_calls) == 1
        # args: ("X-GM-RAW", '"custom:query"')
        assert search_calls[0][2][0] == "X-GM-RAW"
        assert search_calls[0][2][1] == '"custom:query"'

    def test_select_uses_readonly_true(self):
        """readonly=True so reading messages does NOT mark them as read."""
        fake = FakeIMAPClient(search_uids=[])
        fetch_promotions("u", "p", imap_client=fake)
        select_call = [c for c in fake.calls if c[0] == "select"][0]
        assert select_call[2] is True  # readonly=True

    def test_skip_ids_filters_already_seen_msgids(self):
        raw1 = _raw_text_message("S1", "a@a.com", "B1")
        raw2 = _raw_text_message("S2", "b@b.com", "B2")
        fake = FakeIMAPClient(
            search_uids=[b"1", b"2"],
            fetches={
                b"1": (_meta(b"1", "10001"), raw1),
                b"2": (_meta(b"2", "10002"), raw2),
            },
        )
        out = fetch_promotions("u", "p", imap_client=fake, skip_ids={"10001"})
        assert [m["id"] for m in out] == ["10002"]

    def test_one_fetch_failure_does_not_kill_batch(self):
        raw_ok = _raw_text_message("S", "a@a.com", "B")
        fake = FakeIMAPClient(
            search_uids=[b"1", b"2"],
            fetches={b"1": (_meta(b"1", "10001"), raw_ok)},
            fetch_errors={b"2"},
        )
        out = fetch_promotions("u", "p", imap_client=fake)
        assert [m["id"] for m in out] == ["10001"]

    def test_search_failure_returns_empty(self):
        fake = FakeIMAPClient(search_uids=[], search_status="NO")
        assert fetch_promotions("u", "p", imap_client=fake) == []

    def test_no_messages_returns_empty(self):
        fake = FakeIMAPClient(search_uids=[])
        assert fetch_promotions("u", "p", imap_client=fake) == []

    def test_caps_to_max_messages_taking_most_recent(self):
        # 5 UIDs returned but max_messages=2 — Gmail returns oldest first,
        # so the most-recent two (last in the list) should be fetched.
        # X-GM-MSGID is always numeric (64-bit), so the parsing regex
        # requires digits — test fixtures must use numeric IDs.
        raws = {
            f"{i}".encode(): (
                _meta(f"{i}".encode(), f"1000{i}"),
                _raw_text_message(f"S{i}", "a@a.com", "B"),
            )
            for i in range(1, 6)
        }
        fake = FakeIMAPClient(
            search_uids=[b"1", b"2", b"3", b"4", b"5"],
            fetches=raws,
        )
        out = fetch_promotions("u", "p", imap_client=fake, max_messages=2)
        assert [m["id"] for m in out] == ["10004", "10005"]

    def test_injected_client_not_closed(self):
        """When the caller passes their own imap_client, fetch_promotions must
        not call logout — the caller owns the lifecycle."""
        fake = FakeIMAPClient(search_uids=[])
        fetch_promotions("u", "p", imap_client=fake)
        assert ("logout",) not in fake.calls


# ---------------------------------------------------------------------------
# _xgmraw_quote — IMAP quoted-string escaping for X-GM-RAW
# ---------------------------------------------------------------------------

class TestXgmrawQuote:
    def test_quoteless_query_just_wrapped(self):
        assert (_xgmraw_quote("category:promotions newer_than:2d")
                == '"category:promotions newer_than:2d"')

    def test_internal_double_quotes_escaped(self):
        # phrase query -> RFC-3501 backslash-escaped inside the wrapper
        assert _xgmraw_quote('"how did it go"') == '"\\"how did it go\\""'

    def test_backslash_escaped(self):
        assert _xgmraw_quote("a\\b") == '"a\\\\b"'


# ---------------------------------------------------------------------------
# fetch_review_requests — stateless review-request fetch
# ---------------------------------------------------------------------------

class TestFetchReviewRequests:
    def _raw(self, subject, frm, msgid):
        return (
            f"Subject: {subject}\r\n"
            f"From: {frm}\r\n"
            f"Message-ID: {msgid}\r\n"
            f"Date: Tue, 02 Jun 2026 19:32:00 +0000\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"\r\n"
            f"How did it go? Leave a review.\r\n"
        ).encode("utf-8")

    def test_happy_path_parses_message_id(self):
        raw = self._raw(
            "Order #138880, how did it go?",
            "Suzushii Clothing <no-reply@loox.io>", "<abc@loox.io>",
        )
        fake = FakeIMAPClient(
            search_uids=[b"1"],
            fetches={b"1": (_meta(b"1", "20001"), raw)},
        )
        out = fetch_review_requests("u", "p", imap_client=fake)
        assert len(out) == 1
        assert out[0]["id"] == "20001"
        assert out[0]["message_id"] == "<abc@loox.io>"
        assert out[0]["from"] == "Suzushii Clothing <no-reply@loox.io>"

    def test_default_query_is_review_search(self):
        fake = FakeIMAPClient(search_uids=[])
        fetch_review_requests("u", "p", imap_client=fake, days=30)
        search = [c for c in fake.calls if c[0] == "uid" and c[1] == "SEARCH"][0]
        assert search[2][0] == "X-GM-RAW"
        arg = search[2][1]
        assert "newer_than:30d" in arg
        assert "how did it go" in arg

    def test_explicit_query_is_escaped(self):
        fake = FakeIMAPClient(search_uids=[])
        fetch_review_requests("u", "p", imap_client=fake, query='"how did it go"')
        search = [c for c in fake.calls if c[0] == "uid" and c[1] == "SEARCH"][0]
        assert search[2][1] == '"\\"how did it go\\""'

    def test_select_uses_readonly_true(self):
        fake = FakeIMAPClient(search_uids=[])
        fetch_review_requests("u", "p", imap_client=fake)
        select_call = [c for c in fake.calls if c[0] == "select"][0]
        assert select_call[2] is True

    def test_injected_client_not_closed(self):
        fake = FakeIMAPClient(search_uids=[])
        fetch_review_requests("u", "p", imap_client=fake)
        assert ("logout",) not in fake.calls


# ---------------------------------------------------------------------------
# fetch_restock_emails — stateless back-in-stock fetch
# ---------------------------------------------------------------------------

class TestFetchRestockEmails:
    def _raw(self, subject, frm, msgid):
        return (
            f"Subject: {subject}\r\n"
            f"From: {frm}\r\n"
            f"Message-ID: {msgid}\r\n"
            f"Date: Sat, 13 Jun 2026 09:00:00 +0000\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"\r\n"
            f"It's back! Shop now.\r\n"
        ).encode("utf-8")

    def test_happy_path_parses_message_id(self):
        raw = self._raw(
            "Aros Chino is back in stock",
            "Norse Projects <no-reply@klaviyo.com>", "<abc@klaviyo.com>",
        )
        fake = FakeIMAPClient(
            search_uids=[b"1"], fetches={b"1": (_meta(b"1", "30001"), raw)},
        )
        out = fetch_restock_emails("u", "p", imap_client=fake)
        assert len(out) == 1
        assert out[0]["id"] == "30001"
        assert out[0]["message_id"] == "<abc@klaviyo.com>"

    def test_default_query_is_restock_search(self):
        fake = FakeIMAPClient(search_uids=[])
        fetch_restock_emails("u", "p", imap_client=fake, days=7)
        search = [c for c in fake.calls if c[0] == "uid" and c[1] == "SEARCH"][0]
        assert search[2][0] == "X-GM-RAW"
        arg = search[2][1]
        assert "newer_than:7d" in arg
        assert "back in stock" in arg

    def test_select_uses_readonly_true(self):
        fake = FakeIMAPClient(search_uids=[])
        fetch_restock_emails("u", "p", imap_client=fake)
        select_call = [c for c in fake.calls if c[0] == "select"][0]
        assert select_call[2] is True


# ---------------------------------------------------------------------------
# subscribed_shop_domains — per-domain "am I on this list?" inference
# ---------------------------------------------------------------------------

class FakeSearchClient:
    """IMAP stand-in whose SEARCH result depends on the query, so the
    per-domain subscription inference can be exercised offline."""

    def __init__(self, hits=()):
        self.hits = set(hits)  # domains that should return a non-empty SEARCH
        self.calls: list[tuple] = []

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return ("OK", [b""])

    def uid(self, command, *args):
        self.calls.append(("uid", command, args))
        if command == "SEARCH":
            query = args[-1]  # the quoted X-GM-RAW string
            for d in self.hits:
                if f"from:{d}" in query:
                    return ("OK", [b"1 2 3"])
            return ("OK", [b""])
        return ("BAD", [b"unknown"])

    def logout(self):
        self.calls.append(("logout",))
        return ("OK", [b"BYE"])


class TestSubscribedShopDomains:
    def test_returns_only_domains_with_hits(self):
        fake = FakeSearchClient(hits={"aniqi.com"})
        got = subscribed_shop_domains(
            "u", "p", ["aniqi.com", "other.com"], imap_client=fake,
        )
        assert got == {"aniqi.com"}

    def test_dedupes_and_lowercases(self):
        fake = FakeSearchClient(hits={"aniqi.com"})
        got = subscribed_shop_domains(
            "u", "p", ["Aniqi.com", "ANIQI.COM", ""], imap_client=fake,
        )
        assert got == {"aniqi.com"}
        searches = [c for c in fake.calls if c[0] == "uid" and c[1] == "SEARCH"]
        assert len(searches) == 1  # one query for the single unique domain

    def test_query_includes_category_and_window(self):
        fake = FakeSearchClient()
        subscribed_shop_domains(
            "u", "p", ["aniqi.com"], imap_client=fake, days=540,
        )
        search = [c for c in fake.calls if c[0] == "uid" and c[1] == "SEARCH"][0]
        q = search[2][-1]
        assert "from:aniqi.com" in q
        assert "category:promotions" in q
        assert "newer_than:540d" in q

    def test_empty_domains_issues_no_search(self):
        fake = FakeSearchClient(hits={"x.com"})
        assert subscribed_shop_domains("u", "p", [], imap_client=fake) == set()
        assert fake.calls == []

    def test_per_domain_error_skips_that_domain(self):
        class _ErrClient(FakeSearchClient):
            def uid(self, command, *args):
                if command == "SEARCH" and "boom.com" in args[-1]:
                    raise imaplib.IMAP4.error("simulated search error")
                return super().uid(command, *args)
        fake = _ErrClient(hits={"ok.com"})
        got = subscribed_shop_domains(
            "u", "p", ["boom.com", "ok.com"], imap_client=fake,
        )
        assert got == {"ok.com"}

    def test_non_ok_status_not_subscribed(self):
        class _NoClient(FakeSearchClient):
            def uid(self, command, *args):
                self.calls.append(("uid", command, args))
                if command == "SEARCH":
                    return ("NO", [b""])
                return ("BAD", [b""])
        fake = _NoClient()
        assert subscribed_shop_domains("u", "p", ["x.com"], imap_client=fake) == set()

    def test_passed_client_not_logged_out(self):
        fake = FakeSearchClient(hits={"aniqi.com"})
        subscribed_shop_domains("u", "p", ["aniqi.com"], imap_client=fake)
        assert ("logout",) not in fake.calls


# ---------------------------------------------------------------------------
# _parse_fetch_response — imaplib's nested-tuple parsing
# ---------------------------------------------------------------------------

class TestParseFetchResponse:
    def test_extracts_msgid_and_body(self):
        body = b"raw rfc822 bytes"
        out = _parse_fetch_response([
            (_meta(b"1", "17012345"), body),
            b")",
        ])
        assert out == ("17012345", body)

    def test_returns_none_on_missing_msgid(self):
        out = _parse_fetch_response([(b"1 (BODY[] {500}", b"body")])
        assert out is None

    def test_returns_none_on_empty_list(self):
        assert _parse_fetch_response([]) is None
        assert _parse_fetch_response([b")"]) is None


# ---------------------------------------------------------------------------
# Message parsing helpers
# ---------------------------------------------------------------------------

class TestHeader:
    def test_basic_header(self):
        msg = email.message_from_string("Subject: Hi there\n\nbody")
        assert _header(msg, "Subject") == "Hi there"

    def test_missing_header_returns_empty(self):
        msg = email.message_from_string("Subject: x\n\nbody")
        assert _header(msg, "From") == ""

    def test_rfc2047_encoded_header_decoded(self):
        """=?utf-8?B?...?= encoded headers should be decoded to real text."""
        msg = email.message_from_string(
            "Subject: =?utf-8?B?U3ByaW5nIFNhbGUg8J+OiQ==?=\n\nbody",
        )
        # decoded: "Spring Sale 🎉"
        assert "Spring Sale" in _header(msg, "Subject")


class TestExtractBodyText:
    def test_text_plain_passthrough(self):
        msg = email.message_from_bytes(
            _raw_text_message("S", "a@a.com", "plain content"),
        )
        # MIME bodies preserve their trailing CRLF; downstream consumers
        # (line-based code extraction, _excerpt) handle that fine.
        assert "plain content" in _extract_body_text(msg)

    def test_prefers_plain_over_html_in_multipart(self):
        msg = email.message_from_bytes(
            _raw_multipart_message(
                "S", "a@a.com",
                plain="plain version",
                html="<p>html version</p>",
            ),
        )
        out = _extract_body_text(msg)
        assert "plain version" in out
        assert "html version" not in out

    def test_falls_back_to_html_when_no_plain(self):
        # Build an html-only multipart
        m = EmailMessage()
        m["Subject"] = "S"
        m.set_content("<p>Hello world</p>", subtype="html")
        m.replace_header("Content-Type", "text/html; charset=utf-8")
        msg = email.message_from_bytes(m.as_bytes())
        out = _extract_body_text(msg)
        assert "Hello world" in out
        assert "<p>" not in out

    def test_handles_quoted_printable_encoding(self):
        raw = (
            b"Subject: S\r\n"
            b"From: a@a.com\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Transfer-Encoding: quoted-printable\r\n"
            b"\r\n"
            b"Use code =5BSPRING30=5D\r\n"
        )
        msg = email.message_from_bytes(raw)
        out = _extract_body_text(msg)
        # =5B / =5D are the qp-encoded [ and ]
        assert "[SPRING30]" in out

    # -----------------------------------------------------------------------
    # Stub-plain detection — regression for Anime Ape MEMORL20 (2026-05-25)
    #
    # Some shops include a token text/plain part ("This email is HTML-only,
    # view in browser") alongside a real text/html body that holds the
    # actual promo code. Reading text/plain blindly meant we lost the code.
    # _extract_body_text now switches to HTML when the plain part is short
    # AND mentions a stub marker.
    # -----------------------------------------------------------------------

    def test_short_plain_with_html_only_marker_falls_back_to_html(self):
        msg = email.message_from_bytes(
            _raw_multipart_message(
                "S", "a@a.com",
                plain=(
                    "This email was sent to you as HTML-only.\n"
                    "To view it, please visit:\n"
                    "https://example.com/view\n"
                    "Unsubscribe\n"
                ),
                html="<p>Use code MEMORL20 at checkout</p>",
            ),
        )
        out = _extract_body_text(msg)
        assert "MEMORL20" in out
        # The stub URL / boilerplate should NOT be present — we switched
        # away from plain entirely (we didn't concatenate).
        assert "HTML-only" not in out

    def test_short_plain_with_view_in_browser_marker_falls_back_to_html(self):
        msg = email.message_from_bytes(
            _raw_multipart_message(
                "S", "a@a.com",
                plain="View this email in your browser: https://example.com/view\n",
                html="<p>Use code SPRING30 at checkout</p>",
            ),
        )
        assert "SPRING30" in _extract_body_text(msg)

    def test_faithful_long_plain_still_preferred_over_html(self):
        """A real text/plain rendering of the email (well above the 800-char
        threshold) keeps winning — we don't introduce HTML noise for
        well-behaved senders."""
        long_plain = (
            "Hi there!\n\n"
            "Our Memorial Day Sale is officially live.\n"
            "Use code SPRING30 at checkout.\n"
            + ("filler line about new arrivals\n" * 60)  # > 800 chars
        )
        msg = email.message_from_bytes(
            _raw_multipart_message(
                "S", "a@a.com",
                plain=long_plain,
                html="<p>HTML version with DIFFERENT30</p>",
            ),
        )
        out = _extract_body_text(msg)
        assert "SPRING30" in out
        # Confirm we did NOT silently swap to the HTML version.
        assert "DIFFERENT30" not in out

    def test_short_plain_without_stub_marker_still_preferred(self):
        """A short text/plain that's substantive (no view-in-browser /
        html-only language) should stay preferred — it's just a terse email,
        not a stub. Avoids over-triggering on tiny promo blasts that happen
        to fit in under 800 chars."""
        msg = email.message_from_bytes(
            _raw_multipart_message(
                "S", "a@a.com",
                plain="Flash sale! Use code FLASH30 today only.",
                html="<p>HTML version with DIFFERENT30</p>",
            ),
        )
        out = _extract_body_text(msg)
        assert "FLASH30" in out
        assert "DIFFERENT30" not in out

    def test_stub_plain_inflated_by_long_tracking_url_falls_back_to_html(self):
        """Old Navy regression (2026-06-13): the text/plain part is a pure
        "view in browser" stub, but a ~900-char click-tracking URL pushes its
        raw length past the 800-char ceiling, so the old length check treated it
        as faithful and the HTML receipt (the real item list) was never read.
        Length is now measured on the URL-stripped remainder."""
        long_url = "https://click.email.oldnavy.com/?qs=" + ("a" * 900)
        msg = email.message_from_bytes(
            _raw_multipart_message(
                "Order Confirmation #1KGGB5P", "orders@email.oldnavy.com",
                plain=(
                    "Old Navy\n"
                    "Click below to view this message from Old Navy in a web "
                    f"browser:\n{long_url}\n"
                    "Privacy Policy: Unsubscribe: 200 Old Navy Lane\n"
                ),
                html=(
                    "<p>Your order #1KGGB5P has been received.</p>"
                    "<p>Subtotal $51.92</p>"
                    "<p>Crew-Neck T-Shirt L | In the Navy $6.49</p>"
                ),
            ),
        )
        out = _extract_body_text(msg)
        assert "Crew-Neck T-Shirt" in out
        assert "Subtotal" in out
        assert "oldnavy.com/?qs=" not in out  # switched away from the stub

    def test_near_empty_plain_backup_stub_falls_back_to_html(self):
        """Regression (2026-06-23) for a real merch shop whose order emails ship
        a literal "backup" placeholder (6 chars, no view-in-browser marker) in
        the text/plain part while the real receipt lives in text/html. A
        near-empty plain part alongside a full HTML body is a stub on length
        alone — so the HTML receipt (the real item list) must be read. (Fixture
        values are synthetic per the CLAUDE.md privacy guardrails.)"""
        msg = email.message_from_bytes(
            _raw_multipart_message(
                "[Acme Merch] Your Order Was Received #100200",
                "noreply@mail.accounts.acmestore.example",
                plain="backup",
                html=(
                    "<p>ORDER CONFIRMATION</p>"
                    "<p>Widget Hoodie</p>"
                    "<p>QTY: 1 $30.00</p>"
                ),
            ),
        )
        out = _extract_body_text(msg)
        assert "Widget Hoodie" in out
        assert "backup" not in out

    def test_is_stub_plain_threshold(self):
        """Lock the near-empty threshold: a placeholder-length plain part is a
        stub; a terse-but-substantive one (≥ the threshold, no marker) is not,
        so a real short email is never swept away as a stub."""
        assert _is_stub_plain("backup") is True
        assert _is_stub_plain("   ") is True            # whitespace-only
        assert _is_stub_plain("https://x.co/" + "a" * 99) is True  # URL-only noise
        # Substantive short plain (no marker) — must NOT read as a stub.
        assert _is_stub_plain("Flash sale! Use code FLASH30 today only.") is False
        assert _is_stub_plain("20% off everything!") is False

    # -----------------------------------------------------------------------
    # Raw HTML in the text/plain part — issue #10
    #
    # Some senders (Staples) ship raw HTML in the text/plain slot. Reading it
    # verbatim fed `<!DOCTYPE …>` / hex colours to the code regex, producing
    # junk codes (DOCTYPE, PUBLIC, F8F8F8). We now strip a plain part that
    # actually contains HTML.
    # -----------------------------------------------------------------------

    _STAPLES_HTML = (
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
        '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">'
        '<html><head><style>body{background:#F8F8F8}</style></head>'
        '<body bgcolor="#F8F8F8"><p>HALF OFF SALE</p>'
        '<p>Use code DYNAMITE10 at checkout</p></body></html>'
    )

    def test_text_plain_containing_raw_html_is_stripped(self):
        msg = email.message_from_bytes(
            _raw_multipart_message(
                "S", "deals@connected.staples.com",
                plain=self._STAPLES_HTML,
                html="<p>Use code DYNAMITE10 at checkout</p>",
            ),
        )
        out = _extract_body_text(msg)
        assert "DYNAMITE10" in out
        assert "DOCTYPE" not in out
        assert "F8F8F8" not in out
        assert "<" not in out  # all markup stripped

    def test_html_only_staples_email_yields_no_artifact_codes(self):
        """Issue #10 acceptance: a Staples-style HTML-only multipart yields the
        real code and zero codes from the HTML structure."""
        m = EmailMessage()
        m["Subject"] = "HALF OFF SALE"
        m.set_content(self._STAPLES_HTML, subtype="html")
        m.replace_header("Content-Type", "text/html; charset=utf-8")
        msg = email.message_from_bytes(m.as_bytes())
        body = _extract_body_text(msg)
        codes = {c["code"] for c in _extract_codes_from_text(f"HALF OFF SALE\n{body}")}
        assert "DYNAMITE10" in codes
        for artifact in ("DOCTYPE", "PUBLIC", "XHTML", "W3C", "F8F8F8", "SALE", "OFF"):
            assert artifact not in codes


class TestParseMessage:
    def test_flattens_to_dict(self):
        msg = email.message_from_bytes(
            _raw_text_message(
                "Spring Sale!", "Aniqi <hello@aniqi.com>",
                "Use code SPRING30",
            ),
        )
        out = _parse_message("17000001", msg)
        assert out["id"] == "17000001"
        assert out["from"] == "Aniqi <hello@aniqi.com>"
        assert out["subject"] == "Spring Sale!"
        assert "SPRING30" in out["body_text"]
        assert out["date"] != ""

    def test_includes_message_id(self):
        raw = (
            b"Subject: S\r\nFrom: a@a.com\r\n"
            b"Message-ID: <xyz@mail.com>\r\n\r\nbody\r\n"
        )
        out = _parse_message("1", email.message_from_bytes(raw))
        assert out["message_id"] == "<xyz@mail.com>"

    def test_message_id_blank_when_absent(self):
        out = _parse_message(
            "1", email.message_from_bytes(_raw_text_message("S", "a@a.com", "b")),
        )
        assert out["message_id"] == ""


class TestHtmlToText:
    def test_strips_scripts_and_styles(self):
        html = """
        <html><body>
          <script>alert('x')</script>
          <style>p { color: red; }</style>
          <p>Discount inside</p>
        </body></html>
        """
        out = _html_to_text(html)
        assert "Discount inside" in out
        assert "alert" not in out
        assert "color: red" not in out

    def test_preserves_paragraph_breaks(self):
        """Line-based code-context matching needs paragraph boundaries."""
        html = "<p>Free shipping</p><p>Use code SPRING30 at checkout</p>"
        out = _html_to_text(html)
        assert "\n" in out


# ---------------------------------------------------------------------------
# Attribution helpers
# ---------------------------------------------------------------------------

class TestSenderDomain:
    def test_named_address(self):
        assert _sender_domain("Aniqi <hello@aniqi.com>") == "aniqi.com"

    def test_bare_address(self):
        assert _sender_domain("hello@bibisama.shop") == "bibisama.shop"

    def test_normalizes_case(self):
        assert _sender_domain("Foo <X@SHOP.COM>") == "shop.com"

    def test_none_when_missing(self):
        assert _sender_domain("") is None
        assert _sender_domain("no-address-here") is None


class TestAliasesByDomain:
    def test_strips_www(self):
        idx = _aliases_by_domain({"Aniqi": "https://www.aniqi.com"})
        assert idx == {"aniqi.com": "Aniqi"}

    def test_skips_empty_urls(self):
        idx = _aliases_by_domain({"A": "", "B": "https://b.com"})
        assert idx == {"b.com": "B"}

    def test_handles_none_input(self):
        assert _aliases_by_domain({}) == {}


class TestAttribute:
    def test_direct_domain_match(self):
        em = {"from": "hello@aniqi.com", "subject": ""}
        assert _attribute(em, {"aniqi.com": "Aniqi"}, []) == "Aniqi"

    def test_subdomain_walks_to_parent(self):
        em = {"from": "news@mail.aniqi.com", "subject": ""}
        assert _attribute(em, {"aniqi.com": "Aniqi"}, []) == "Aniqi"

    def test_subject_fallback_when_sender_unknown(self):
        em = {"from": "hello@klaviyomail.com",
              "subject": "Aniqi: Spring Sale starts today"}
        assert _attribute(em, {}, ["Aniqi", "Pomelo"]) == "Aniqi"

    def test_no_match_returns_none(self):
        em = {"from": "unknown@randomshop.io", "subject": "Generic"}
        assert _attribute(em, {}, ["Aniqi"]) is None

    def test_subject_match_is_case_insensitive(self):
        em = {"from": "x@x.com", "subject": "ANIQI sale"}
        assert _attribute(em, {}, ["Aniqi"]) == "Aniqi"


# ---------------------------------------------------------------------------
# Code extraction + excerpt
# ---------------------------------------------------------------------------

class TestExtractCodesFromText:
    def test_simple_code_with_context(self):
        text = "Use code SPRING30 at checkout"
        out = _extract_codes_from_text(text)
        assert len(out) == 1
        assert out[0]["code"] == "SPRING30"

    def test_no_code_keyword_no_match(self):
        """A code-shaped token without a context keyword is ignored."""
        assert _extract_codes_from_text("Welcome to ANIQI") == []

    def test_dedupes_within_text(self):
        text = "Use code SPRING30\nDon't forget code SPRING30!"
        out = _extract_codes_from_text(text)
        # The bare SPRING30 should appear exactly once.
        assert [c["code"] for c in out].count("SPRING30") == 1

    # -----------------------------------------------------------------------
    # Cross-line context window — diagnosis 2026-05-25 showed that promo
    # emails frequently render the code as its own visual block, and BS4's
    # ``get_text(separator="\n")`` splits "Use code" from the code itself onto
    # adjacent lines. The ±1 sliding window bridges those splits.
    # -----------------------------------------------------------------------

    def test_code_on_line_below_context(self):
        """Otishi SummerSale15 case: context word on the line ABOVE the code."""
        text = "Use code\nSummerSale15\nat checkout."
        out = _extract_codes_from_text(text)
        # Canonicalised to uppercase.
        assert [c["code"] for c in out] == ["SUMMERSALE15"]

    def test_code_on_line_above_context(self):
        """Inverse: code first, ``at checkout with code`` underneath."""
        text = "SPRING30\nUse this code at checkout."
        out = _extract_codes_from_text(text)
        assert "SPRING30" in [c["code"] for c in out]

    def test_san_jose_improv_pattern(self):
        """All-uppercase IMPROV code on its own line after ``USE PROMO CODE``."""
        text = "USE PROMO CODE '\nIMPROV\n' AT CHECKOUT"
        out = _extract_codes_from_text(text)
        assert [c["code"] for c in out] == ["IMPROV"]

    def test_context_not_in_window_no_match(self):
        """Code more than 1 line away from any context word: still rejected.
        Guards the false-positive risk introduced by the window."""
        text = (
            "Use code\n"
            "filler line one\n"
            "filler line two\n"
            "SUMMERSALE15"
        )
        out = _extract_codes_from_text(text)
        assert out == []

    def test_url_slug_near_off_rejected(self):
        """The mixed-case regex now sees lowercase tokens, but URL slugs near
        the context word ``off`` must not become false-positive codes."""
        text = "20% off this week:\noff-script-red-embroidered-beanie"
        out = _extract_codes_from_text(text)
        assert out == []

    def test_canonicalises_to_uppercase(self):
        """Mixed-case input → uppercase canonical output."""
        text = "Use code SummerSale15"
        out = _extract_codes_from_text(text)
        assert out[0]["code"] == "SUMMERSALE15"

    def test_dedupes_across_case_variants(self):
        """``SummerSale15`` and ``SUMMERSALE15`` in the same body collapse."""
        text = (
            "Use code SummerSale15 at checkout.\n"
            "Also code SUMMERSALE15 below.\n"
        )
        out = _extract_codes_from_text(text)
        assert [c["code"] for c in out] == ["SUMMERSALE15"]


class TestExcerpt:
    def test_short_text_passthrough(self):
        assert _excerpt("hello") == "hello"

    def test_collapses_whitespace(self):
        assert _excerpt("a\n\nb   c") == "a b c"

    def test_truncates_long_text(self):
        out = _excerpt("x " * 2000)
        assert out.endswith("...[truncated]")
        assert len(out) < 2000

    def test_empty_returns_empty(self):
        assert _excerpt("") == ""
        assert _excerpt(None) == ""


# ---------------------------------------------------------------------------
# extract_signals — integration
# ---------------------------------------------------------------------------

class TestExtractSignals:
    _NOW = datetime(2026, 5, 19, 14, 0, 0, tzinfo=timezone.utc)

    def _email(self, mid: str, frm: str, subject: str, body: str,
               date: str = "") -> dict:
        return {"id": mid, "from": frm, "subject": subject,
                "snippet": "", "body_text": body, "date": date}

    def test_attributed_code_lands_in_codes_bucket(self):
        aliases = {"Aniqi": "https://aniqi.com"}
        emails = [self._email(
            "m1", "hello@aniqi.com", "Spring Sale",
            "Use code SPRING30 at checkout — 30% off",
        )]
        out = extract_signals(emails, aliases, ["Aniqi"], now=self._NOW)
        assert len(out["codes"]) == 1
        c = out["codes"][0]
        assert c["shop"] == "Aniqi"
        assert c["code"] == "SPRING30"
        assert c["source"] == "email"
        assert c["email_id"] == "m1"
        assert c["first_seen"] == c["last_seen"]
        assert out["unattributed"] == []

    def test_attributed_email_emits_sale_signal(self):
        aliases = {"Aniqi": "https://aniqi.com"}
        emails = [self._email(
            "m1", "news@aniqi.com", "Spring Sale starts today",
            "Big news! 30% off everything this weekend.",
        )]
        out = extract_signals(emails, aliases, ["Aniqi"], now=self._NOW)
        assert len(out["sale_signals"]) == 1
        sig = out["sale_signals"][0]
        assert sig["email_id"] == "m1"
        assert sig["shop"] == "Aniqi"
        assert sig["subject"] == "Spring Sale starts today"
        assert "30% off" in sig["body_excerpt"]

    def test_sale_signal_carries_parsed_email_date(self):
        aliases = {"Aniqi": "https://aniqi.com"}
        emails = [self._email(
            "m1", "news@aniqi.com", "Sale soon",
            "Memorial Day sale starts Monday.",
            date="Fri, 22 May 2026 09:30:00 -0500",
        )]
        out = extract_signals(emails, aliases, ["Aniqi"], now=self._NOW)
        assert out["sale_signals"][0]["email_date"] == "2026-05-22"

    def test_sale_signal_email_date_blank_when_unparseable(self):
        aliases = {"Aniqi": "https://aniqi.com"}
        emails = [self._email(
            "m1", "news@aniqi.com", "Sale", "30% off today.",
            date="not a real date",
        )]
        out = extract_signals(emails, aliases, ["Aniqi"], now=self._NOW)
        assert out["sale_signals"][0]["email_date"] == ""

    def test_unknown_sender_drops_into_unattributed(self):
        emails = [self._email(
            "m1", "newsletter@unknownbrand.io", "Big sale",
            "Use code MEGA50 for half off",
        )]
        out = extract_signals(emails, {}, [], now=self._NOW)
        assert out["codes"] == []
        assert out["sale_signals"] == []
        assert len(out["unattributed"]) == 1
        u = out["unattributed"][0]
        assert u["shop"] == "unknownbrand.io"
        assert u["code"] == "MEGA50"
        assert u["source"] == "email_unattributed"

    def test_processed_ids_includes_every_email(self):
        emails = [
            self._email("m1", "hello@aniqi.com", "S", "code FOO discount"),
            self._email("m2", "unknown@x.io", "S", "code BAR discount"),
        ]
        out = extract_signals(emails, {"Aniqi": "https://aniqi.com"}, ["Aniqi"],
                              now=self._NOW)
        assert set(out["processed_ids"]) == {"m1", "m2"}

    def test_subject_match_attributes_when_sender_unknown(self):
        emails = [self._email(
            "m1", "hello@klaviyomail.com",
            "Aniqi: VIP Sale starts now",
            "Use code VIP25 today only",
        )]
        out = extract_signals(emails, {}, ["Aniqi"], now=self._NOW)
        assert len(out["codes"]) == 1
        assert out["codes"][0]["shop"] == "Aniqi"

    def test_html_body_with_newlines_still_matches_codes(self):
        emails = [self._email(
            "m1", "hello@aniqi.com", "Sale",
            "Free shipping\nUse code WEEKEND25 to save 25%",
        )]
        out = extract_signals(emails, {"Aniqi": "https://aniqi.com"}, ["Aniqi"],
                              now=self._NOW)
        assert any(c["code"] == "WEEKEND25" for c in out["codes"])

    def test_emails_with_no_codes_still_emit_sale_signal(self):
        emails = [self._email(
            "m1", "hello@aniqi.com", "Sale starts Friday",
            "Mark your calendar — big things coming.",
        )]
        out = extract_signals(emails, {"Aniqi": "https://aniqi.com"}, ["Aniqi"],
                              now=self._NOW)
        assert out["codes"] == []
        assert len(out["sale_signals"]) == 1

    def test_no_emails_returns_empty(self):
        out = extract_signals([], {}, [], now=self._NOW)
        assert out["codes"] == []
        assert out["unattributed"] == []
        assert out["sale_signals"] == []
        assert out["processed_ids"] == []

    def test_attributed_code_carries_confidence_and_real_context(self):
        """The stored context should include the line that actually
        contained the token, not just the subject. Without the line, a
        low-confidence false positive in the digest is unexplainable —
        the user can't see what triggered the match."""
        emails = [self._email(
            "m1", "news@aniqi.com", "Spring Sale",
            "Use code SPRING30 at checkout — 30% off",
        )]
        out = extract_signals(emails, {"Aniqi": "https://aniqi.com"},
                              ["Aniqi"], now=self._NOW)
        c = out["codes"][0]
        assert c["confidence"] == "high"
        # Context retains the subject AND the harvested line.
        assert "Spring Sale" in c["context"]
        assert "SPRING30" in c["context"]

    def test_unattributed_code_carries_confidence_low_for_shouted_word(self):
        """A marketing email that yells ``OFF SITEWIDE`` produces a
        SITEWIDE entry — but it lands as low confidence so the digest
        can group it below real-looking codes."""
        emails = [self._email(
            "m1", "newsletter@unknownbrand.io",
            "LAST CHANCE: 30% OFF SITEWIDE",
            "30% OFF SITEWIDE this weekend only — don't miss it!",
        )]
        out = extract_signals(emails, {}, [], now=self._NOW)
        sitewide = next(u for u in out["unattributed"] if u["code"] == "SITEWIDE")
        assert sitewide["confidence"] == "low"

    def test_unattributed_real_code_marked_high(self):
        emails = [self._email(
            "m1", "news@anewbrand.io", "Welcome",
            "Use code WELCOME10 for 10% off your first order",
        )]
        out = extract_signals(emails, {}, [], now=self._NOW)
        w = next(u for u in out["unattributed"] if u["code"] == "WELCOME10")
        assert w["confidence"] == "high"
