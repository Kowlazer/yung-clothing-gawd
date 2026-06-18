"""Tests for src/voice.py (GV-forwarded SMS via the same Gmail IMAP path).

Strategy:
  * Fixture-driven — real .eml samples (sanitized) under tests/fixtures/voice/
    drive both the message-parse and the signal-extract paths so any change
    to Google's forward template surfaces immediately.
  * IMAP is mocked with the same FakeIMAPClient pattern as test_gmail.py.
  * Pure helpers (_extract_sender_number, _extract_sms_body, _attribute_sms)
    are tested directly.
"""
from __future__ import annotations

import email
import imaplib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.voice import (
    DEFAULT_LABEL,
    _attribute_sms,
    _excerpt,
    _extract_codes_from_text,
    _extract_sender_number,
    _extract_sms_body,
    _is_text_message,
    _normalize_number,
    _parse_voice_message,
    extract_sms_signals,
    fetch_voice_sms,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "voice"


def _load_fixture(name: str) -> email.message.Message:
    raw = (_FIXTURES / name).read_bytes()
    return email.message_from_bytes(raw)


# ---------------------------------------------------------------------------
# FakeIMAPClient — minimal imaplib.IMAP4_SSL stand-in (duplicated from
# test_gmail.py to keep this test file standalone)
# ---------------------------------------------------------------------------

class FakeIMAPClient:
    def __init__(
        self,
        search_uids: list[bytes] = (),
        fetches: dict[bytes, tuple[bytes, bytes]] | None = None,
        *,
        select_status: str = "OK",
        search_status: str = "OK",
        fetch_errors: set[bytes] | None = None,
    ):
        self._search_uids = list(search_uids)
        self._fetches = dict(fetches or {})
        self._select_status = select_status
        self._search_status = search_status
        self._fetch_errors = fetch_errors or set()
        self.calls: list[tuple] = []

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return (self._select_status, [b""])

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
    return f"{uid.decode()} (X-GM-MSGID {msgid} BODY[] {{500}}".encode()


# ---------------------------------------------------------------------------
# _extract_sender_number
# ---------------------------------------------------------------------------

class TestExtractSenderNumber:
    def test_us_11_digit_number_gets_plus_prefix(self):
        hdr = '"(833) 302-2004" <15555550100.18333022004.dk1Z91JTlg@txt.voice.google.com>'
        assert _extract_sender_number(hdr) == "+18333022004"

    def test_short_code_left_as_is(self):
        hdr = '"21234" <15555550100.21234.sh0rtZJTlg@txt.voice.google.com>'
        assert _extract_sender_number(hdr) == "21234"

    def test_returns_none_for_non_voice_address(self):
        assert _extract_sender_number("Foo <bar@example.com>") is None

    def test_returns_none_for_empty(self):
        assert _extract_sender_number("") is None
        assert _extract_sender_number(None) is None

    # --- new (2026) format: From is generic, number is in the subject --------

    def test_new_format_shortcode_from_subject(self):
        hdr = "Google Voice <voice-noreply@google.com>"
        assert _extract_sender_number(hdr, "New text message from 49469") == "49469"

    def test_new_format_phone_from_subject(self):
        hdr = "Google Voice <voice-noreply@google.com>"
        out = _extract_sender_number(hdr, "New text message from (844) 619-9172")
        assert out == "+18446199172"

    def test_old_from_format_wins_over_subject(self):
        # Old format present → subject is ignored (From is authoritative).
        hdr = '"x" <15555550100.21234.abc@txt.voice.google.com>'
        assert _extract_sender_number(hdr, "New text message from 99999") == "21234"

    def test_new_format_no_subject_match_returns_none(self):
        hdr = "Google Voice <voice-noreply@google.com>"
        assert _extract_sender_number(hdr, "Some unrelated subject") is None


class TestNormalizeNumber:
    def test_ten_digit_gets_us_country_code(self):
        assert _normalize_number("(844) 619-9172") == "+18446199172"

    def test_eleven_digit_leading_one(self):
        assert _normalize_number("18446199172") == "+18446199172"

    def test_short_code_kept_verbatim(self):
        assert _normalize_number("49469") == "49469"

    def test_no_digits_returns_none(self):
        assert _normalize_number("") is None
        assert _normalize_number(None) is None
        assert _normalize_number("no digits here") is None


class TestIsTextMessage:
    def test_text_message_subject_is_sms(self):
        assert _is_text_message("New text message from 49469") is True

    def test_voicemail_subject_is_not_sms(self):
        assert _is_text_message("New voicemail from (555) 555-0123") is False

    def test_missed_call_subject_is_not_sms(self):
        assert _is_text_message("New missed call from (555) 555-0123") is False

    def test_empty_subject_treated_as_sms(self):
        # Lenient: only explicit voicemail/missed-call markers exclude.
        assert _is_text_message("") is True
        assert _is_text_message(None) is True


# ---------------------------------------------------------------------------
# _extract_sms_body
# ---------------------------------------------------------------------------

class TestExtractSmsBody:
    def test_strips_voice_logo_header_and_footer(self):
        raw = (
            "\n"
            "<https://voice.google.com>\n"
            "Aniqi: New drop is live! Use code SMS25 for 25% off this weekend.\n"
            "To respond to this text message, reply to this email or visit Google Voice.\n"
            "YOUR ACCOUNT...\n"
        )
        out = _extract_sms_body(raw)
        assert out == "Aniqi: New drop is live! Use code SMS25 for 25% off this weekend."

    def test_preserves_multiline_sms_body(self):
        raw = (
            "<https://voice.google.com>\n"
            "Line one of the SMS\n"
            "Line two with code FOO10\n"
            "Line three\n"
            "To respond to this text message, reply to this email or visit Google Voice.\n"
        )
        out = _extract_sms_body(raw)
        assert "Line one of the SMS" in out
        assert "Line two with code FOO10" in out
        assert "Line three" in out
        assert "To respond" not in out

    def test_strips_reworded_2026_footer(self):
        # The 2026 template reworded the footer: "this message" (no "text") and
        # "launch Google Voice" — the old regex missed it, leaving junk behind.
        raw = (
            "\n"
            "<https://voice.google.com>\n"
            "Steady Hands: 20% OFF SITEWIDE ENDS TONIGHT!\n"
            "To respond to this message, launch Google Voice (https://voice.google.com)\n"
            "on your mobile device or computer.\n"
            "YOUR ACCOUNT <https://voice.google.com> HELP CENTER\n"
        )
        out = _extract_sms_body(raw)
        assert out == "Steady Hands: 20% OFF SITEWIDE ENDS TONIGHT!"
        assert "To respond" not in out
        assert "YOUR ACCOUNT" not in out

    def test_strips_trailer_only_variant_without_respond_line(self):
        # Older variant: no "To respond" line, just the GV boilerplate trailer.
        raw = (
            "<https://voice.google.com>\n"
            "Are we still on for tomorrow\n"
            "YOUR ACCOUNT HELP CENTER\n"
            "<https://support.google.com/voice#topic=1707989> HELP FORUM\n"
            "Google LLC\n"
        )
        out = _extract_sms_body(raw)
        assert out == "Are we still on for tomorrow"
        assert "YOUR ACCOUNT" not in out
        assert "HELP FORUM" not in out

    def test_returns_text_unchanged_when_no_markers(self):
        # Defensive: if Google changes the template, don't silently drop content.
        raw = "Plain text with no GV wrappers"
        assert _extract_sms_body(raw) == "Plain text with no GV wrappers"

    def test_empty_input(self):
        assert _extract_sms_body("") == ""
        assert _extract_sms_body(None) == ""


# ---------------------------------------------------------------------------
# _parse_voice_message — uses real fixture
# ---------------------------------------------------------------------------

class TestParseVoiceMessage:
    def test_no_code_fixture(self):
        msg = _load_fixture("sms_no_code.eml")
        out = _parse_voice_message("17000001", msg)
        assert out["id"] == "17000001"
        assert out["subject"] == "New text message from (833) 302-2004"
        assert out["sms_from_number"] == "+18333022004"
        assert out["sms_body"] == (
            "BulkSMS.com covers over 1200 networks worldwide, including yours!"
        )
        # Footer text must NOT leak into sms_body.
        assert "To respond" not in out["sms_body"]
        assert "Google LLC" not in out["sms_body"]

    def test_with_code_fixture(self):
        msg = _load_fixture("sms_with_code.eml")
        out = _parse_voice_message("17000002", msg)
        assert out["sms_from_number"] == "+18885557700"
        assert "Aniqi" in out["sms_body"]
        assert "SMS25" in out["sms_body"]

    def test_short_code_sender_fixture(self):
        msg = _load_fixture("sms_short_code_sender.eml")
        out = _parse_voice_message("17000003", msg)
        assert out["sms_from_number"] == "21234"
        assert "Pomelo" in out["sms_body"]
        assert "SHORT15" in out["sms_body"]
        assert "Reply STOP to opt out" in out["sms_body"]

    def test_new_format_shortcode_fixture(self):
        # 2026 format: From is voice-noreply@google.com, number in the subject.
        msg = _load_fixture("sms_new_format_shortcode.eml")
        out = _parse_voice_message("17000004", msg)
        assert out["sms_from_number"] == "49469"  # recovered from subject
        assert out["is_sms"] is True
        assert out["sms_body"].startswith("Steady Hands: OUR MEMORIAL DAY SALE")
        assert "20% OFF SITEWIDE" in out["sms_body"]
        # reworded footer + GV trailer must be stripped
        assert "To respond" not in out["sms_body"]
        assert "YOUR ACCOUNT" not in out["sms_body"]
        assert "Google LLC" not in out["sms_body"]

    def test_voicemail_fixture_flagged_not_sms(self):
        msg = _load_fixture("voicemail.eml")
        out = _parse_voice_message("17000005", msg)
        assert out["is_sms"] is False


# ---------------------------------------------------------------------------
# _attribute_sms
# ---------------------------------------------------------------------------

class TestAttributeSms:
    def test_known_phone_number_match(self):
        sms = {"sms_from_number": "+18334567890", "sms_body": "Use code FOO"}
        assert _attribute_sms(sms, {"+18334567890": "Aniqi"}, []) == "Aniqi"

    def test_short_code_match(self):
        sms = {"sms_from_number": "21234", "sms_body": "blah"}
        assert _attribute_sms(sms, {"21234": "Pomelo"}, []) == "Pomelo"

    def test_body_substring_match_when_number_unknown(self):
        sms = {"sms_from_number": "+19998887777",
               "sms_body": "Aniqi: 25% off this weekend"}
        assert _attribute_sms(sms, {}, ["Aniqi", "Pomelo"]) == "Aniqi"

    def test_body_match_is_case_insensitive(self):
        sms = {"sms_from_number": None, "sms_body": "ANIQI sale today only"}
        assert _attribute_sms(sms, {}, ["Aniqi"]) == "Aniqi"

    def test_no_match_returns_none(self):
        sms = {"sms_from_number": "+10000000000", "sms_body": "Random text"}
        assert _attribute_sms(sms, {}, ["Aniqi"]) is None


# ---------------------------------------------------------------------------
# _extract_codes_from_text — same heuristic as gmail's; smoke-test only here
# ---------------------------------------------------------------------------

class TestExtractCodesFromText:
    def test_finds_code_with_context_keyword(self):
        out = _extract_codes_from_text("Use code SMS25 for 25% off")
        assert [c["code"] for c in out] == ["SMS25"]

    def test_no_keyword_means_no_match(self):
        # ANIQI is a brand name in the body, not a promo code line.
        assert _extract_codes_from_text("Welcome to ANIQI") == []

    def test_real_tooluckymerch_sms_extracts_digit_leading_code(self):
        """Regression for the SMS that exposed the digit-leading + SMS-acronym
        bug — both 7KXQ4PMV (kept) and SMS (rejected) on the same line."""
        body = (
            "TooLuckyMerch: Thanks for subscribing to SMS marketing "
            "(e.g. cart reminders)! Here's your coupon for 10% off: "
            "7KXQ4PMV Shop here: https://tooluckymerch.pscrpt.io/aJJFBy"
        )
        out = [c["code"] for c in _extract_codes_from_text(body)]
        assert out == ["7KXQ4PMV"]


# ---------------------------------------------------------------------------
# extract_sms_signals — integration
# ---------------------------------------------------------------------------

class TestExtractSmsSignals:
    _NOW = datetime(2026, 5, 21, 14, 0, 0, tzinfo=timezone.utc)

    def _sms(self, eid: str, number: str | None, body: str,
             subject: str = "New text message") -> dict:
        return {"id": eid, "from": "", "subject": subject,
                "sms_from_number": number, "sms_body": body, "date": ""}

    def test_attributed_sms_emits_code_and_sale_signal(self):
        sms_aliases = {"+18885557700": "Aniqi"}
        sms_list = [self._sms(
            "m1", "+18885557700",
            "Aniqi: New drop is live! Use code SMS25 for 25% off",
        )]
        out = extract_sms_signals(sms_list, sms_aliases, ["Aniqi"], now=self._NOW)
        assert len(out["codes"]) == 1
        c = out["codes"][0]
        assert c["shop"] == "Aniqi"
        assert c["code"] == "SMS25"
        assert c["source"] == "sms"
        assert c["email_id"] == "m1"
        assert c["first_seen"] == c["last_seen"]
        assert "from SMS:" in c["context"]
        assert len(out["sale_signals"]) == 1
        assert out["sale_signals"][0]["shop"] == "Aniqi"
        assert out["unattributed"] == []

    def test_body_attribution_when_phone_unknown(self):
        # Phone number not in sms_aliases but the body opens with the shop name.
        sms_list = [self._sms(
            "m1", "+19998887777",
            "Aniqi: Flash sale today, code FLASH40 for 40% off",
        )]
        out = extract_sms_signals(sms_list, {}, ["Aniqi"], now=self._NOW)
        assert len(out["codes"]) == 1
        assert out["codes"][0]["shop"] == "Aniqi"

    def test_unknown_sender_goes_to_unattributed(self):
        sms_list = [self._sms(
            "m1", "+15554443333",
            "Mystery brand here. Use code MYSTERY50 to save",
        )]
        out = extract_sms_signals(sms_list, {}, [], now=self._NOW)
        assert out["codes"] == []
        assert out["sale_signals"] == []
        assert len(out["unattributed"]) == 1
        u = out["unattributed"][0]
        assert u["shop"] == "+15554443333"  # phone number is the placeholder shop
        assert u["code"] == "MYSTERY50"
        assert u["source"] == "sms_unattributed"

    def test_unknown_sender_with_no_number_uses_unknown(self):
        sms_list = [self._sms("m1", None, "code GHOST20 discount today")]
        out = extract_sms_signals(sms_list, {}, [], now=self._NOW)
        assert out["unattributed"][0]["shop"] == "(unknown)"

    def test_attributed_sms_with_no_code_still_emits_sale_signal(self):
        sms_list = [self._sms(
            "m1", "+18885557700",
            "Aniqi: Big sale starts Friday — mark your calendar!",
        )]
        out = extract_sms_signals(
            sms_list, {"+18885557700": "Aniqi"}, ["Aniqi"], now=self._NOW,
        )
        assert out["codes"] == []
        assert len(out["sale_signals"]) == 1

    def test_processed_ids_includes_every_sms(self):
        sms_list = [
            self._sms("m1", "+18885557700", "Aniqi code FOO discount"),
            self._sms("m2", "+19990001111", "code BAR discount"),
        ]
        out = extract_sms_signals(
            sms_list, {"+18885557700": "Aniqi"}, ["Aniqi"], now=self._NOW,
        )
        assert set(out["processed_ids"]) == {"m1", "m2"}

    def test_sale_signal_carries_email_date_from_header(self):
        sms = self._sms("m1", "+18885557700", "Aniqi: Sale today, code SAVE20")
        sms["date"] = "Fri, 12 Jun 2026 09:05:46 -0700"
        out = extract_sms_signals(
            [sms], {"+18885557700": "Aniqi"}, ["Aniqi"], now=self._NOW,
        )
        assert out["sale_signals"][0]["email_date"] == "2026-06-12"

    def test_voicemail_skipped_but_marked_processed(self):
        sms = self._sms(
            "m1", "+15555550123", "Hi this is the front desk about your appointment",
            subject="New voicemail from (555) 555-0123",
        )
        out = extract_sms_signals([sms], {}, ["PeakWear"], now=self._NOW)
        assert out["processed_ids"] == ["m1"]  # not re-fetched next run
        assert out["sale_signals"] == []
        assert out["codes"] == []
        assert out["unattributed"] == []
        assert out["untracked_senders"] == []

    def test_verification_text_skipped(self):
        # 2FA code text would otherwise emit "G-976717" as an unattributed code.
        sms = self._sms(
            "m1", "22000", "G-976717 is your Google verification code",
            subject="New text message from 22000",
        )
        out = extract_sms_signals([sms], {}, [], now=self._NOW)
        assert out["unattributed"] == []
        assert out["untracked_senders"] == []
        assert out["processed_ids"] == ["m1"]

    def test_untracked_marketing_brand_surfaced_for_discovery(self):
        # Non-watchlist shop with a "Brand:" lead + sale lexeme → discovery.
        sms = self._sms(
            "m1", "89258",
            "Grey Fox: BOGO 70% Off thousands of styles. Shop now.",
            subject="New text message from 89258",
        )
        out = extract_sms_signals([sms], {}, ["PeakWear"], now=self._NOW)
        assert out["sale_signals"] == []  # not attributed
        assert len(out["untracked_senders"]) == 1
        u = out["untracked_senders"][0]
        assert u["brand"] == "Grey Fox"
        assert u["number"] == "89258"
        assert "BOGO" in u["excerpt"]

    def test_attributed_shop_not_in_discovery_list(self):
        # Allowlist/watchlist shop's text attributes → never in discovery.
        sms = self._sms(
            "m1", "49469", "Steady Hands: 20% off sitewide today",
            subject="New text message from 49469",
        )
        out = extract_sms_signals([sms], {}, ["Steady Hands"], now=self._NOW)
        assert len(out["sale_signals"]) == 1
        assert out["untracked_senders"] == []

    def test_untracked_without_sale_lexeme_not_surfaced(self):
        # A "Brand:" lead but no deal language → not discovery-worthy noise.
        sms = self._sms(
            "m1", "31354", "Fabletics: Your order is on the way. Track it here.",
            subject="New text message from 31354",
        )
        out = extract_sms_signals([sms], {}, ["Aniqi"], now=self._NOW)
        assert out["untracked_senders"] == []

    def test_empty_input(self):
        out = extract_sms_signals([], {}, [], now=self._NOW)
        assert out == {"codes": [], "unattributed": [], "sale_signals": [],
                       "untracked_senders": [], "processed_ids": []}


# ---------------------------------------------------------------------------
# fetch_voice_sms — IMAP integration with FakeIMAPClient
# ---------------------------------------------------------------------------

class TestFetchVoiceSms:
    def test_happy_path_parses_real_fixture(self):
        raw = (_FIXTURES / "sms_with_code.eml").read_bytes()
        fake = FakeIMAPClient(
            search_uids=[b"1"],
            fetches={b"1": (_meta(b"1", "20001"), raw)},
        )
        out = fetch_voice_sms("u@gmail.com", "pw", imap_client=fake)
        assert len(out) == 1
        sms = out[0]
        assert sms["id"] == "20001"
        assert sms["sms_from_number"] == "+18885557700"
        assert "SMS25" in sms["sms_body"]

    def test_select_uses_configured_label_and_readonly(self):
        fake = FakeIMAPClient(search_uids=[])
        fetch_voice_sms("u", "p", imap_client=fake, label="MyLabel")
        select_calls = [c for c in fake.calls if c[0] == "select"]
        assert len(select_calls) == 1
        # mailbox wrapped in quotes so labels with spaces are accepted
        assert select_calls[0][1] == '"MyLabel"'
        assert select_calls[0][2] is True

    def test_default_label_is_googlevoice(self):
        fake = FakeIMAPClient(search_uids=[])
        fetch_voice_sms("u", "p", imap_client=fake)
        select_call = [c for c in fake.calls if c[0] == "select"][0]
        assert select_call[1] == f'"{DEFAULT_LABEL}"'

    def test_select_failure_returns_empty(self):
        fake = FakeIMAPClient(search_uids=[], select_status="NO")
        assert fetch_voice_sms("u", "p", imap_client=fake) == []

    def test_skip_ids_filters_already_seen(self):
        raw = (_FIXTURES / "sms_with_code.eml").read_bytes()
        fake = FakeIMAPClient(
            search_uids=[b"1", b"2"],
            fetches={
                b"1": (_meta(b"1", "20001"), raw),
                b"2": (_meta(b"2", "20002"), raw),
            },
        )
        out = fetch_voice_sms("u", "p", imap_client=fake, skip_ids={"20001"})
        assert [m["id"] for m in out] == ["20002"]

    def test_one_fetch_failure_does_not_kill_batch(self):
        raw = (_FIXTURES / "sms_with_code.eml").read_bytes()
        fake = FakeIMAPClient(
            search_uids=[b"1", b"2"],
            fetches={b"1": (_meta(b"1", "20001"), raw)},
            fetch_errors={b"2"},
        )
        out = fetch_voice_sms("u", "p", imap_client=fake)
        assert [m["id"] for m in out] == ["20001"]

    def test_search_failure_returns_empty(self):
        fake = FakeIMAPClient(search_uids=[], search_status="NO")
        assert fetch_voice_sms("u", "p", imap_client=fake) == []

    def test_caps_to_max_messages_taking_most_recent(self):
        raw = (_FIXTURES / "sms_with_code.eml").read_bytes()
        fetches = {
            f"{i}".encode(): (_meta(f"{i}".encode(), f"2000{i}"), raw)
            for i in range(1, 6)
        }
        fake = FakeIMAPClient(
            search_uids=[b"1", b"2", b"3", b"4", b"5"], fetches=fetches,
        )
        out = fetch_voice_sms("u", "p", imap_client=fake, max_messages=2)
        assert [m["id"] for m in out] == ["20004", "20005"]

    def test_injected_client_not_closed(self):
        fake = FakeIMAPClient(search_uids=[])
        fetch_voice_sms("u", "p", imap_client=fake)
        assert ("logout",) not in fake.calls


# ---------------------------------------------------------------------------
# _excerpt — smoke-test (mirrors gmail._excerpt)
# ---------------------------------------------------------------------------

class TestExcerpt:
    def test_collapses_whitespace(self):
        assert _excerpt("a\n\nb   c") == "a b c"

    def test_truncates_long_text(self):
        out = _excerpt("x " * 2000)
        assert out.endswith("...[truncated]")

    def test_empty_returns_empty(self):
        assert _excerpt("") == ""
        assert _excerpt(None) == ""
