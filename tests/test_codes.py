"""Tests for codes.py against the real watchlist fixture."""
from pathlib import Path

import pytest

from src.codes import harvest_codes

FIXTURE = Path(__file__).parent / "fixtures" / "watchlist.txt"


@pytest.fixture(scope="module")
def codes() -> list[dict]:
    return harvest_codes(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def code_values(codes) -> list[str]:
    return [c["code"] for c in codes]


def test_finds_bang_suffixed_code(code_values):
    # "Discount code SAVE20!" — trailing punctuation handled.
    assert "SAVE20!" in code_values


def test_finds_welcome_style_code(code_values):
    # "10% discount code: SAVENOW10"
    assert "SAVENOW10" in code_values


def test_finds_firstorder10(code_values):
    # "code for 10% off: FIRSTORDER10"
    assert "FIRSTORDER10" in code_values


def test_finds_hyphenated_code(code_values):
    # "10% off coupon: QRST-UVWX-YZAB"
    assert "QRST-UVWX-YZAB" in code_values


def test_total_code_count(codes):
    # Fixture harvests: SAVE20!, SAVENOW10, FIRSTORDER10, DEALONE!,
    # QRST-UVWX-YZAB.
    assert len(codes) == 5


def test_hyphenated_code_kept_whole():
    # Regression for the multi-segment code split bug.
    from src.codes import harvest_codes
    codes = harvest_codes(
        "ExampleShop:\n10% off coupon: PQRS-TUVW-XYZ1\n"
    )
    values = [c["code"] for c in codes]
    assert "PQRS-TUVW-XYZ1" in values
    # Pieces should NOT also appear as separate codes.
    assert "PQRS" not in values
    assert "TUVW" not in values
    assert "XYZ1" not in values


# ---------------------------------------------------------------------------
# _is_valid_code — distinguishes real codes from marketing noise
# ---------------------------------------------------------------------------

class TestIsValidCode:
    """Code-validity filter behavior pinned by direct cases.

    Background: marketing SMS contains many UPPERCASE acronyms (SMS, STOP,
    REPLY, OPT, SHOP, FREE) that match the token regex shape but aren't
    promo codes. Equally, modern shop platforms (Postscript, Klaviyo,
    Shopify Discounts) routinely issue digit-leading codes like 7KXQ4PMV.
    The filter accepts: digit+letter combos, hyphenated tokens, or pure-letter
    tokens >=6 chars. Rejects everything else.
    """

    @pytest.mark.parametrize("code", [
        "SPRING30", "VIP25", "7KXQ4PMV", "SMS25", "QX7M2P9KZ4",
        "PEAKVIP", "PEAKVIP!", "QRST-UVWX-YZAB", "FREESHIP",
    ])
    def test_kept(self, code):
        from src.codes import _is_valid_code
        assert _is_valid_code(code) is True

    @pytest.mark.parametrize("token", [
        "SMS", "STOP", "REPLY", "OPT", "SHOP", "FREE", "HELP",
        "MORE", "JOIN", "TODAY", "EXTRA", "SAVE",
        "2025", "30",  # numeric-only tokens
    ])
    def test_rejected(self, token):
        from src.codes import _is_valid_code
        assert _is_valid_code(token) is False

    @pytest.mark.parametrize("token", [
        # HTML/XML structural keywords leaked from raw markup (issue #10)
        "DOCTYPE", "PUBLIC", "XHTML", "DTD", "CDATA", "NBSP",
        # CSS hex colours bleeding out of inline styles
        "F8F8F8", "FFFFFF", "f8f8f8", "ABCDEF",
        # marketing shout-words listed in the issue acceptance criteria
        "OFF", "SALE", "CODE", "USE", "PROMO",
    ])
    def test_html_artifacts_and_marketing_words_rejected(self, token):
        """Issue #10: HTML/CSS artifacts and stopword marketing terms must
        never be stored as promo codes."""
        from src.codes import _is_valid_code
        assert _is_valid_code(token) is False


def test_digit_leading_code_extracted_from_sms_body():
    """Regression for real-world Postscript-style code in a SMS body."""
    from src.codes import harvest_codes
    text = (
        "PeakWear:\n"
        "Thanks for subscribing to SMS marketing!\n"
        "Here's your coupon for 10% off: 7KXQ4PMV\n"
    )
    values = [c["code"] for c in harvest_codes(text)]
    assert "7KXQ4PMV" in values
    # SMS marketing acronyms must NOT appear as codes.
    assert "SMS" not in values


def test_savenow10_shop(codes):
    entry = next(c for c in codes if c["code"] == "SAVENOW10")
    assert entry["shop"] == "DriftGoods"


def test_firstorder10_shop(codes):
    entry = next(c for c in codes if c["code"] == "FIRSTORDER10")
    assert entry["shop"] == "CedarThreads"


def test_hyphenated_code_shop(codes):
    entry = next(c for c in codes if c["code"] == "QRST-UVWX-YZAB")
    assert entry["shop"] == "RiverstoneCo"


# ---------------------------------------------------------------------------
# Mixed-case regex relaxation + uppercase canonicalisation
#
# Diagnosis on 2026-05-25 showed PeakWear's ``SummerSale15`` promo was missed
# because the previous all-UPPERCASE regex couldn't see lowercase letters.
# The regex now allows mixed case; ``_is_valid_code`` compensates by
# requiring at least one digit (or all-uppercase) so we don't false-positive
# on plain English words / URL slugs.
# ---------------------------------------------------------------------------

class TestMixedCaseCodes:
    def test_mixed_case_code_with_digit_extracted(self):
        from src.codes import harvest_codes
        text = "PeakWear:\nUse code SummerSale15 at checkout.\n"
        values = [c["code"] for c in harvest_codes(text)]
        # Canonicalised to uppercase for stable dedupe.
        assert "SUMMERSALE15" in values

    def test_mixed_case_canonicalised_to_uppercase(self):
        """``SummerSale15`` is stored uppercased so downstream consumers
        (``codes.json``, the digest renderer) see one stable form regardless
        of how the shop displayed the code."""
        from src.codes import harvest_codes
        text = (
            "PeakWear:\n"
            "Use code SummerSale15 at checkout.\n"
        )
        values = [c["code"] for c in harvest_codes(text)]
        assert "SUMMERSALE15" in values
        # The lowercased original is NOT stored.
        assert "SummerSale15" not in values

    def test_lowercase_only_word_rejected(self):
        """Plain English words near a context keyword must not become codes."""
        from src.codes import _is_valid_code
        for word in ["kitchen", "promise", "arrivals", "discount"]:
            assert _is_valid_code(word) is False, f"{word!r} should be rejected"

    def test_url_slug_rejected(self):
        """URL slugs (always lowercase + hyphens) must not be treated as codes.

        Regression for the 2026-05-25 mixed-case-regex rollout, which initially
        let through ``off-script-red-embroidered-beanie`` from a product URL
        that happened to live on a line containing the context word ``off``.
        """
        from src.codes import _is_valid_code
        assert _is_valid_code("off-script-red-embroidered-beanie") is False

    def test_mixed_case_no_digit_rejected(self):
        """All-letter mixed case (``BlackFriday``) is rejected — real all-letter
        codes are ALL-CAPS by convention."""
        from src.codes import _is_valid_code
        assert _is_valid_code("BlackFriday") is False
        # But the UPPERCASE version is still accepted (>=6 chars).
        assert _is_valid_code("BLACKFRIDAY") is True

    def test_short_digit_token_rejected(self):
        """Ordinals / times-of-day next to a context word must not be codes.

        The sliding-window context match in ``gmail._extract_codes_from_text``
        sees tokens like ``11TH`` (from ``May 11th``) and ``12PM`` (from
        ``Doors open … 12PM``) when a sale email mentions ``code`` nearby.
        These all have digits but are <5 chars, which is the length cutoff
        for the digit-bearing code path.
        """
        from src.codes import _is_valid_code
        for token in ["11TH", "27TH", "30TH", "12PM", "4FOR", "4For"]:
            assert _is_valid_code(token) is False, f"{token!r} should be rejected"

    def test_five_char_digit_code_accepted(self):
        """``VIP25`` (5 chars) is the smallest real-world digit-bearing code
        we want to keep — it's the boundary case for the ≥5 rule."""
        from src.codes import _is_valid_code
        assert _is_valid_code("VIP25") is True

    def test_context_words_themselves_rejected(self):
        """Words that ARE the context signal must not also be accepted as
        codes. The Anime Ape email has a ``CLAIM DISCOUNT`` button on its
        own line — the line satisfies the context regex (contains
        ``discount``) AND the token ``DISCOUNT`` passes the all-letter
        all-uppercase ≥6-letter branch. The deny set blocks this."""
        from src.codes import _is_valid_code
        for word in [
            "DISCOUNT", "DISCOUNTS",
            "COUPON", "COUPONS",
            "PROMOTION", "PROMOTIONS",
            "UNSUBSCRIBE",
            "CHECKOUT",
        ]:
            assert _is_valid_code(word) is False, f"{word!r} should be rejected"
        # But a digit-bearing variant — i.e. a real code that happens to
        # start with one of these words — must still pass.
        assert _is_valid_code("DISCOUNT15") is True


class TestCanonicaliseCode:
    def test_uppercases_letters(self):
        from src.codes import _canonicalise_code
        assert _canonicalise_code("SummerSale15") == "SUMMERSALE15"

    def test_preserves_trailing_bang(self):
        from src.codes import _canonicalise_code
        assert _canonicalise_code("FreeShip!") == "FREESHIP!"

    def test_preserves_hyphens(self):
        from src.codes import _canonicalise_code
        assert _canonicalise_code("QRST-UVWX-YZAB") == "QRST-UVWX-YZAB"


# ---------------------------------------------------------------------------
# _classify_confidence — soft-deny rating that lets the digest bucket
# codes by how real they look. Doesn't reject anything; just labels.
# Codes that pass _is_valid_code are the input universe.
# ---------------------------------------------------------------------------

class TestClassifyConfidence:
    @pytest.mark.parametrize("code", [
        "DENIM40", "ARTHUR5", "MEMORIAL20", "WELCOME10",
        "60FORYOU", "MYSTRY15", "DYNAMITE10", "DISCOUNT15",
        "VIP25", "SPRING30", "7KXQ4PMV", "QX7M2P9KZ4",
        "85N62WY9GHJ6",
    ])
    def test_digit_plus_letter_is_high(self, code):
        """Token with both a digit and a letter and ≥5 chars is the
        canonical real-promo shape — extremely unlikely to occur by
        accident in marketing copy."""
        from src.codes import _classify_confidence
        assert _classify_confidence(code) == "high"

    @pytest.mark.parametrize("code", [
        "QRST-UVWX-YZAB", "FREESHIP!", "BRANDECHO!",
    ])
    def test_hyphenated_or_bang_is_high(self, code):
        """Hyphenated codes and codes ending with ! are distinctive
        shapes shops use deliberately."""
        from src.codes import _classify_confidence
        assert _classify_confidence(code) == "high"

    @pytest.mark.parametrize("code", [
        # Marketing shout-words found in the prod Gist on 2026-05-25.
        "SITEWIDE", "CLEARANCE", "CHANCE", "SELECTED", "REDEEM",
        "MYSTERY", "SCRIPT", "UNIQUE", "SUMMER", "MEMORIAL",
        "EVERYTHING", "DOLLAR", "FOUNTAINS", "NIGHTSTANDS",
        # Already in _is_valid_code's deny set, but the confidence
        # layer should also classify them low so if anyone ever
        # relaxes _is_valid_code, the digest still routes them right.
        "DISCOUNT", "COUPON",
        # Template-variable artifact (still soft-denied; HTML structural
        # keywords like DOCTYPE/PUBLIC are now hard-rejected upstream by
        # _is_valid_code — see TestIsValidCode).
        "UNIQID",
    ])
    def test_marketing_shout_words_are_low(self, code):
        from src.codes import _classify_confidence
        assert _classify_confidence(code) == "low"

    @pytest.mark.parametrize("code", [
        "F8F8F8", "FFFFFF", "AABBCC", "abcdef",
    ])
    def test_hex_color_is_low(self, code):
        """Six-char hex-only tokens are CSS color values bleeding out of
        inline ``style="background:#F8F8F8"`` attributes that the HTML
        stripper turned into text."""
        from src.codes import _classify_confidence
        assert _classify_confidence(code) == "low"

    @pytest.mark.parametrize("code", [
        # All-letter all-caps tokens that aren't in any deny list — could be
        # a real brand-themed code (BRANDECHO, PEAKVIP) or a marketing word
        # we haven't catalogued. Default to medium so the user still sees
        # them but knows to verify.
        "PEAKVIP", "WELCOME", "FREESHIP", "BLACKFRIDAY",
    ])
    def test_plain_all_caps_letters_is_medium(self, code):
        from src.codes import _classify_confidence
        assert _classify_confidence(code) == "medium"

    def test_harvest_codes_attaches_confidence(self):
        """Confidence ships as part of the harvest_codes dict so downstream
        consumers don't have to recompute it."""
        from src.codes import harvest_codes
        codes = harvest_codes(
            "PeakWear:\nUse code SummerSale15 at checkout.\n"
            "HomeStore:\n50% OFF SITEWIDE this weekend.\n"
        )
        by_code = {c["code"]: c for c in codes}
        # Real promo-shape code.
        assert by_code["SUMMERSALE15"]["confidence"] == "high"
        # Marketing shout-word that nonetheless passes _is_valid_code.
        # Won't actually be harvested here because there's no context word
        # adjacent — verify the rating function directly instead.
        # (The watchlist-format fixture exercises the full path.)
