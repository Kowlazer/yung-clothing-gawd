"""Tests for src/order_parse.py — the deterministic field extractors."""
from __future__ import annotations

import pytest

from src.order_parse import (
    _parse_amount,
    _registrable,
    extract_order_number,
    extract_total,
    extract_tracking_url,
    is_excluded_shop,
    resolve_shop,
    sender_domain,
)


# ---------------------------------------------------------------------------
# Excluded-shop matching
# ---------------------------------------------------------------------------

class TestIsExcludedShop:
    EXC = ("nocturne goods",)

    def test_matches_shop_name_case_insensitive(self):
        assert is_excluded_shop("Nocturne Goods", "", self.EXC)
        assert is_excluded_shop("NOCTURNE GOODS", "", self.EXC)

    def test_matches_despaced_domain(self):
        assert is_excluded_shop("", "nocturne-goods.com", self.EXC)
        assert is_excluded_shop("", "nocturnegoods.com", self.EXC)
        assert is_excluded_shop("", "shop.nocturne-goods.com", self.EXC)

    def test_matches_when_name_is_a_substring(self):
        # token normalises to "nocturnegoods"; "Nocturne Goods LLC" -> "nocturnegoodsllc"
        assert is_excluded_shop("Nocturne Goods LLC", "", self.EXC)

    def test_non_match(self):
        assert not is_excluded_shop("PeakWear", "peakwear.com", self.EXC)

    def test_empty_exclusions_never_matches(self):
        assert not is_excluded_shop("Nocturne Goods", "nocturne-goods.com", ())

    def test_multiple_tokens(self):
        exc = ("nocturne goods", "acme")
        assert is_excluded_shop("ACME Corp", "acme.io", exc)
        assert is_excluded_shop("Nocturne Goods", "x.com", exc)
        assert not is_excluded_shop("Norse Projects", "norseprojects.com", exc)


# ---------------------------------------------------------------------------
# Sender domain + shop resolution
# ---------------------------------------------------------------------------

class TestSenderDomain:
    def test_normal_from(self):
        assert sender_domain("Shop <hi@norseprojects.com>") == "norseprojects.com"

    def test_plain_email(self):
        assert sender_domain("orders@uniqlo.com") == "uniqlo.com"

    def test_lowercases(self):
        assert sender_domain("Hi <HI@SHOP.COM>") == "shop.com"

    def test_no_match(self):
        assert sender_domain("malformed") == ""

    def test_empty(self):
        assert sender_domain("") == ""


class TestResolveShop:
    def test_alias_hit(self):
        aliases = {"Norse Projects": "https://norseprojects.com"}
        shop, domain = resolve_shop("Order <hi@norseprojects.com>", aliases)
        assert shop == "Norse Projects"
        assert domain == "norseprojects.com"

    def test_alias_with_www_prefix(self):
        # shop_aliases entries sometimes include www. — the reverse index
        # should strip it.
        aliases = {"Uniqlo": "https://www.uniqlo.com"}
        shop, domain = resolve_shop("orders@uniqlo.com", aliases)
        assert shop == "Uniqlo"
        assert domain == "uniqlo.com"

    def test_transactional_subdomain_collapses(self):
        # mail.norseprojects.com → norseprojects.com.
        aliases = {"Norse Projects": "https://norseprojects.com"}
        shop, domain = resolve_shop("hi@mail.norseprojects.com", aliases)
        assert shop == "Norse Projects"
        assert domain == "norseprojects.com"

    def test_subdomain_walk_up(self):
        # Multiple subdomain levels — walk up until we hit the alias.
        aliases = {"Aniqi": "https://aniqi.com"}
        shop, domain = resolve_shop("orders@order-confirm.aniqi.com", aliases)
        assert shop == "Aniqi"
        # Domain is the alias-matched domain, not the original subdomain.
        assert domain == "aniqi.com"

    def test_no_alias_synthesises_canonical(self):
        # An unknown sender — synthesise the canonical name from the apex.
        shop, domain = resolve_shop("orders@junkbrands.com", {})
        assert shop == "Junkbrands"
        assert domain == "junkbrands.com"

    def test_empty_from(self):
        shop, domain = resolve_shop("", {"Foo": "https://foo.com"})
        assert shop == ""
        assert domain == ""

    def test_hyphenated_domain(self):
        shop, domain = resolve_shop("hi@fishers-finery.com", {})
        # Synthesised canonical replaces hyphens with spaces, title-cases.
        assert shop == "Fishers Finery"
        assert domain == "fishers-finery.com"

    def test_single_letter_marketing_subdomain_strips(self):
        # Grey Fox sends from s.greyfox.com — the "s." is a generic
        # marketing-service prefix and should be stripped.
        shop, domain = resolve_shop("Grey Fox <orders@s.greyfox.com>", {})
        assert shop == "Greyfox"
        assert domain == "greyfox.com"

    def test_single_letter_subdomain_respects_aliases(self):
        # Alias takes precedence over synthesised canonical even when a
        # single-letter prefix was stripped.
        aliases = {"Grey Fox": "https://greyfox.com"}
        shop, domain = resolve_shop("Grey Fox <orders@s.greyfox.com>", aliases)
        assert shop == "Grey Fox"
        assert domain == "greyfox.com"

    def test_delivery_subdomain_folds_to_apex(self):
        # H&M ships order/delivery mail from us@delivery.hm.com. The "delivery."
        # prefix must fold to the apex (it used to resolve to shop "Delivery").
        shop, domain = resolve_shop("H&M <us@delivery.hm.com>", {})
        assert domain == "hm.com"
        assert shop == "Hm"  # synthesised; an alias upgrades it to "H&M"

    def test_delivery_subdomain_respects_alias(self):
        # Alias URL must resolve to the hm.com apex (the reverse index only
        # strips a leading "www."), so use www.hm.com / hm.com — not www2.hm.com.
        aliases = {"H&M": "https://www.hm.com"}
        shop, domain = resolve_shop("H&M <us@delivery.hm.com>", aliases)
        assert shop == "H&M"
        assert domain == "hm.com"

    @pytest.mark.parametrize("prefix", ["shipping", "tracking", "news", "marketing", "members"])
    def test_other_transactional_prefixes_fold(self, prefix):
        shop, domain = resolve_shop(f"Brand <hi@{prefix}.brandco.com>", {})
        assert domain == "brandco.com"
        assert shop == "Brandco"

    def test_shared_sender_uses_display_name(self):
        # Shopify's t.shopifyemail.com is shared across thousands of
        # shops; the real shop identity lives in the From display name.
        shop, domain = resolve_shop(
            "Dattehameha <noreply@t.shopifyemail.com>", {},
        )
        assert shop == "Dattehameha"
        assert domain == "shopifyemail.com"

    def test_shared_sender_alias_still_wins(self):
        # If the user has an alias mapping shopifyemail.com to some
        # canonical name (unlikely but possible), the alias wins.
        aliases = {"Dattehameha": "https://dattehameha.store"}
        # No alias maps to shopifyemail.com, so we fall back to display.
        shop, domain = resolve_shop(
            "Dattehameha <hi@t.shopifyemail.com>", aliases,
        )
        assert shop == "Dattehameha"
        assert domain == "shopifyemail.com"

    def test_shared_sender_no_display_name_falls_through(self):
        # No display name → synthesised canonical from the shared apex
        # ("Shopifyemail" — ugly but stable; user can add an alias).
        shop, domain = resolve_shop("noreply@t.shopifyemail.com", {})
        assert shop == "Shopifyemail"
        assert domain == "shopifyemail.com"

    def test_shared_sender_generic_display_name_falls_through(self):
        # "Order Confirmation" is generic — don't trust it as a shop name.
        shop, domain = resolve_shop(
            "Order Confirmation <noreply@t.shopifyemail.com>", {},
        )
        assert shop == "Shopifyemail"
        assert domain == "shopifyemail.com"

    def test_uniform_case_display_name_titlecased(self):
        # All-uppercase display gets title-cased to "Dattehameha".
        shop, _ = resolve_shop(
            "DATTEHAMEHA <noreply@t.shopifyemail.com>", {},
        )
        assert shop == "Dattehameha"

    def test_mixed_case_display_name_preserved(self):
        # Intentional brand casing like "theanimecollective" must survive.
        shop, _ = resolve_shop(
            "theAnimeCollective <hi@s.shopifyemail.com>", {},
        )
        assert shop == "theAnimeCollective"

    def test_transactional_strip_does_not_collapse_to_bare_tld(self):
        # Pathological: "o.com" — stripping "o." would leave "com" which
        # is just a TLD. Guard rejects the strip.
        shop, domain = resolve_shop("hi@o.com", {})
        assert domain == "o.com"
        assert shop == "O"

    def test_surviving_subdomain_synthesises_brand_not_subdomain(self):
        # Regression (2026-06-24) for a real merch sender of the shape
        # noreply@mail.accounts.<brand>.com: after "mail." is stripped the apex
        # is "accounts.<brand>.com" — "accounts." is not a known transactional
        # prefix. The old parts[0] synthesis mis-named the shop "Accounts"; it
        # must now synthesise the brand label and the registrable domain.
        # (Synthetic values per the CLAUDE.md privacy guardrails.)
        shop, domain = resolve_shop(
            "Acme Merch <noreply@mail.accounts.acmestore.com>", {},
        )
        assert shop == "Acmestore"
        assert domain == "acmestore.com"

    def test_surviving_subdomain_respects_alias(self):
        # An alias keyed at the registrable domain still wins via walk-up,
        # upgrading the synthesised "Acmestore" to the user's canonical name.
        aliases = {"Acme": "https://acmestore.com"}
        shop, domain = resolve_shop(
            "Acme Merch <noreply@mail.accounts.acmestore.com>", aliases,
        )
        assert shop == "Acme"
        assert domain == "acmestore.com"

    def test_multipart_tld_synthesis(self):
        # A multi-part public suffix (".co.uk") — the brand label is the one
        # left of "co", and the registrable domain keeps both suffix labels.
        shop, domain = resolve_shop("orders@shop.britishbrand.co.uk", {})
        assert shop == "Britishbrand"
        assert domain == "britishbrand.co.uk"

    def test_registrable_helper(self):
        assert _registrable("accounts.acmestore.com") == ("acmestore", "acmestore.com")
        assert _registrable("junkbrands.com") == ("junkbrands", "junkbrands.com")
        assert _registrable("shop.brand.co.uk") == ("brand", "brand.co.uk")
        assert _registrable("o.com") == ("o", "o.com")
        # Deeply nested infra subdomains still resolve to the brand.
        assert _registrable("a.b.c.example.com") == ("example", "example.com")


# ---------------------------------------------------------------------------
# Total + currency
# ---------------------------------------------------------------------------

class TestParseAmount:
    def test_us_thousands(self):
        assert _parse_amount("1,234.56") == 1234.56

    def test_us_no_thousands(self):
        assert _parse_amount("99.99") == 99.99

    def test_european_decimal_comma(self):
        assert _parse_amount("1.234,56") == 1234.56

    def test_european_comma_decimal_only(self):
        assert _parse_amount("99,99") == 99.99

    def test_only_commas_thousands(self):
        assert _parse_amount("1,234,567") == 1234567.0

    def test_integer(self):
        assert _parse_amount("42") == 42.0

    def test_garbage(self):
        assert _parse_amount("xyz") is None

    def test_empty(self):
        assert _parse_amount("") is None


class TestExtractTotal:
    def test_shopify_us(self):
        body = (
            "Subtotal: $120.00\n"
            "Shipping: $5.00\n"
            "Tax: $9.60\n"
            "Total: $134.60\n"
        )
        result = extract_total(body)
        assert result == {"amount": 134.60, "currency": "USD"}

    def test_euro_with_symbol(self):
        body = "Order Total: €99,00"
        result = extract_total(body)
        assert result == {"amount": 99.00, "currency": "EUR"}

    def test_explicit_currency_code(self):
        body = "Grand Total: 120.00 CAD"
        result = extract_total(body)
        assert result == {"amount": 120.00, "currency": "CAD"}

    def test_falls_back_to_subtotal(self):
        # No "Total" line — uses subtotal.
        body = "Subtotal: $50.00\nThanks for your order!\n"
        result = extract_total(body)
        assert result == {"amount": 50.00, "currency": "USD"}

    def test_returns_none_when_no_total(self):
        assert extract_total("just some text with no money") is None

    def test_empty_body(self):
        assert extract_total("") is None

    def test_picks_last_total_line(self):
        # When "Total" appears mid-body (e.g. a section total) and then
        # again at the bottom as the order total, the last one wins.
        body = (
            "Section Total: $20.00\n"
            "More items...\n"
            "Order Total: $99.99\n"
        )
        result = extract_total(body)
        assert result == {"amount": 99.99, "currency": "USD"}

    def test_currency_symbol_defaults_to_usd_when_ambiguous(self):
        body = "Total: $50.00"
        assert extract_total(body)["currency"] == "USD"


# ---------------------------------------------------------------------------
# Tracking URL
# ---------------------------------------------------------------------------

class TestExtractTrackingUrl:
    def test_ups(self):
        body = "Track your package: https://www.ups.com/track?tracknum=1Z123\n"
        assert extract_tracking_url(body) == "https://www.ups.com/track?tracknum=1Z123"

    def test_fedex(self):
        body = "Tracking: https://www.fedex.com/fedextrack/?trknbr=99999"
        assert extract_tracking_url(body) == "https://www.fedex.com/fedextrack/?trknbr=99999"

    def test_usps(self):
        assert extract_tracking_url("see https://tools.usps.com/track?tLabels=ABC") \
            == "https://tools.usps.com/track?tLabels=ABC"

    def test_route(self):
        assert extract_tracking_url("Track on https://track.route.com/abc123") \
            == "https://track.route.com/abc123"

    def test_aftership(self):
        assert extract_tracking_url("https://norseprojects.aftership.com/abc") \
            == "https://norseprojects.aftership.com/abc"

    def test_strips_trailing_punctuation(self):
        body = "Click https://ups.com/track?n=123. Thanks!"
        assert extract_tracking_url(body) == "https://ups.com/track?n=123"

    def test_no_match(self):
        assert extract_tracking_url("just thanks for shopping") is None

    def test_empty_body(self):
        assert extract_tracking_url("") is None


# ---------------------------------------------------------------------------
# Order number
# ---------------------------------------------------------------------------

class TestExtractOrderNumber:
    def test_with_hash(self):
        assert extract_order_number("Order #ABC123 confirmed") == "ABC123"

    def test_with_colon(self):
        assert extract_order_number("Order number: 1234567") == "1234567"

    def test_uppercases(self):
        assert extract_order_number("Order #abc123") == "ABC123"

    def test_too_short_rejected(self):
        # Minimum 4 chars after the prefix — "#1" / "#us" are noise.
        assert extract_order_number("Order #1") is None

    def test_hyphenated(self):
        assert extract_order_number("Order #SHOP-1234-XYZ") == "SHOP-1234-XYZ"

    def test_no_match(self):
        assert extract_order_number("just some text") is None

    def test_empty(self):
        assert extract_order_number("") is None
