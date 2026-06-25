"""Tests for src/extract.py against saved HTML fixtures.

Each fixture is a real page from the watchlist fetched on 2026-05-17. Tests
call parse() directly with the saved HTML (and optional Shopify product JSON)
so they never touch the network.

Fixtures:
  aniqi_trafalgar     — "Sale price" theme label with compare_at_price="0.00" (not a real sale)
  pomel_counter_punch — OOS per JSON-LD; Shopify JSON has no 'available' field
  hakistop_farms_tee  — in stock, OG price extraction (no JSON-LD Product)
  onsen_preorder      — OOS per JSON-LD; preorder item; HTML-only (no Shopify JSON)
  hokuro_wave_shorts  — genuinely on sale ($26 from $58, compare_at > price)
"""

from __future__ import annotations

import html as _htmllib
import json
from pathlib import Path

import pytest

from src.extract import parse

FIXTURES = Path(__file__).parent / "fixtures"


def _html(slug: str) -> str:
    return (FIXTURES / f"{slug}.html").read_text(encoding="utf-8")


def _json(slug: str) -> dict:
    return json.loads((FIXTURES / f"{slug}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# aniqi_trafalgar
# The Shopify "Sale price" gotcha: theme labels the price "Sale price" even
# when compare_at_price is "0.00" — no actual markdown. on_sale must be False.
# ---------------------------------------------------------------------------

class TestAniqi:
    URL = "https://aniqi.com/products/trafalgar-joggers"

    def result(self):
        return parse(_html("aniqi_trafalgar"), self.URL, product_json=_json("aniqi_trafalgar"))

    def test_price(self):
        assert self.result()["current_price"] == 68.0

    def test_not_on_sale(self):
        """compare_at_price='0.00' must not trigger on_sale."""
        r = self.result()
        assert r["on_sale"] is False
        assert r["original_price"] is None

    def test_in_stock(self):
        assert self.result()["out_of_stock"] is False

    def test_label(self):
        assert "TRAFALGAR" in (self.result()["label"] or "").upper()

    def test_currency(self):
        assert self.result()["currency"] == "USD"


# ---------------------------------------------------------------------------
# pomel_counter_punch
# JSON-LD reports OutOfStock; Shopify JSON variant has no 'available' field
# so OOS detection falls back to HTML-sourced JSON-LD.
# ---------------------------------------------------------------------------

class TestPomel:
    URL = "https://pomelclothing.com/products/counter-punch-white-tee"

    def result(self):
        return parse(_html("pomel_counter_punch"), self.URL, product_json=_json("pomel_counter_punch"))

    def test_price(self):
        assert self.result()["current_price"] == 37.0

    def test_oos(self):
        """JSON-LD availability=OutOfStock must surface as out_of_stock=True."""
        assert self.result()["out_of_stock"] is True

    def test_not_on_sale(self):
        r = self.result()
        assert r["on_sale"] is False
        assert r["original_price"] is None

    def test_label(self):
        label = self.result()["label"] or ""
        assert "COUNTER" in label.upper() or "PUNCH" in label.upper()


# ---------------------------------------------------------------------------
# hakistop_farms_tee
# No JSON-LD Product block — price comes from og:price:amount or Shopify JSON.
# Should be in stock with no sale.
# ---------------------------------------------------------------------------

class TestHakistop:
    URL = "https://hakistop.com/products/hakistop-farms-tee"

    def result(self):
        return parse(_html("hakistop_farms_tee"), self.URL, product_json=_json("hakistop_farms_tee"))

    def test_price(self):
        assert self.result()["current_price"] == 45.0

    def test_not_on_sale(self):
        assert self.result()["on_sale"] is False

    def test_in_stock(self):
        assert self.result()["out_of_stock"] is False


# ---------------------------------------------------------------------------
# onsen_preorder
# JSON-LD reports OutOfStock (preorder item). No Shopify JSON (503 from server)
# so this exercises the pure HTML-extraction path.
# ---------------------------------------------------------------------------

class TestOnsenPreorder:
    URL = "https://onsendesigns.net/products/preorder-v2-kanto-region-hoodie-copy"

    def result(self):
        # No product_json — tests HTML-only path
        return parse(_html("onsen_preorder"), self.URL)

    def test_price(self):
        assert self.result()["current_price"] == 79.0

    def test_oos(self):
        """Preorder item — JSON-LD availability=OutOfStock."""
        assert self.result()["out_of_stock"] is True

    def test_label(self):
        label = self.result()["label"] or ""
        # extruct may normalise case; check a fragment that's definitely in the name
        assert "hoodie" in label.lower() or "kanto" in label.lower() or "johto" in label.lower()


# ---------------------------------------------------------------------------
# hokuro_wave_shorts
# Genuinely on sale: Shopify JSON compare_at_price="58.00" > price="26.00".
# JSON-LD also confirms InStock.
# ---------------------------------------------------------------------------

class TestHokuro:
    URL = "https://www.hokuroclothing.com/collections/best-sellers/products/wave-shorts"

    def result(self):
        return parse(_html("hokuro_wave_shorts"), self.URL, product_json=_json("hokuro_wave_shorts"))

    def test_on_sale(self):
        assert self.result()["on_sale"] is True

    def test_prices(self):
        r = self.result()
        assert r["current_price"] == 26.0
        assert r["original_price"] == 58.0

    def test_in_stock(self):
        """JSON-LD availability=InStock — should not be flagged OOS."""
        assert self.result()["out_of_stock"] is False

    def test_label(self):
        label = self.result()["label"] or ""
        assert "WAVE" in label.upper() or "SHORT" in label.upper()


# ---------------------------------------------------------------------------
# aniwrld_homepage_first_product (newly discovered Shopify shop)
# Pirate woven blanket tapestry — on sale $64.99 from $150 (huge markdown).
# Single-variant product (no size/color options).
# ---------------------------------------------------------------------------

class TestAniwrld:
    URL = "https://aniwrld.com/products/pirate-4-woven-blanket-tapestry"

    def result(self):
        return parse(_html("aniwrld_homepage_first_product"), self.URL,
                     product_json=_json("aniwrld_homepage_first_product"))

    def test_on_sale(self):
        assert self.result()["on_sale"] is True

    def test_prices(self):
        r = self.result()
        assert r["current_price"] == 64.99
        assert r["original_price"] == 150.0

    def test_single_variant(self):
        assert self.result()["total_variant_count"] == 1


# ---------------------------------------------------------------------------
# starsalts_homepage_first_product (newly discovered Shopify shop)
# Smeargle button-up — 7 size variants, $50, no sale, in stock.
# ---------------------------------------------------------------------------

class TestStarsalts:
    URL = "https://starsalts.com/products/smeargle-splatter-button-up-shirt"

    def result(self):
        return parse(_html("starsalts_homepage_first_product"), self.URL,
                     product_json=_json("starsalts_homepage_first_product"))

    def test_price(self):
        assert self.result()["current_price"] == 50.0

    def test_not_on_sale(self):
        assert self.result()["on_sale"] is False

    def test_seven_variants(self):
        assert self.result()["total_variant_count"] == 7


# ---------------------------------------------------------------------------
# Non-Shopify fixtures — graceful degradation
# These pages either use a different e-commerce platform (WooCommerce) or
# return a bot-protection challenge. The extractor must not crash and must
# return all-default values rather than guessing.
# ---------------------------------------------------------------------------

class TestXenpachiWooCommerce:
    """Xenpachi runs WooCommerce. We parse its inline variation JSON for both
    price and per-size availability: this product is $38.55 (on sale from
    $47.11) and sold out in every size. The price comes from structured
    variation data, never from scraping a dollar amount out of the page text."""

    URL = "https://www.xenpachi.com/product/joy/"

    def test_does_not_crash(self):
        # Just calling parse() without raising is the assertion.
        parse(_html("xenpachi_joy"), self.URL)

    def test_price_from_variation_json(self):
        r = parse(_html("xenpachi_joy"), self.URL)
        assert r["current_price"] == 38.55

    def test_sale_detected_from_regular_price(self):
        r = parse(_html("xenpachi_joy"), self.URL)
        assert r["on_sale"] is True
        assert r["original_price"] == 47.11

    def test_all_sizes_out_of_stock(self):
        r = parse(_html("xenpachi_joy"), self.URL)
        assert r["out_of_stock"] is True
        assert r["available_sizes"] == []


class TestAnimeapeBlocked:
    """Animeape returns a Cloudflare challenge page (HTTP 403 with ~6KB body).
    parse() should handle this gracefully even though in production extract()
    would short-circuit on the 403 status before calling parse()."""

    URL = "https://animeape.com/product/sakonji-urokodaki-demon-slayer-button-down-hawaiian-shirt/"

    def test_does_not_crash(self):
        parse(_html("animeape_sakonji"), self.URL)

    def test_no_phantom_data(self):
        r = parse(_html("animeape_sakonji"), self.URL)
        assert r["current_price"] is None
        assert r["on_sale"] is False
        assert r["out_of_stock"] is False


class TestEtsyBlocked:
    """Etsy returns a DataDome JS-challenge page (HTTP 403, ~800B). Same
    graceful-degradation expectation as the Animeape case."""

    URL = "https://www.etsy.com/listing/4298147709/anime-embroidered-sweatshirt-custom"

    def test_does_not_crash(self):
        parse(_html("etsy_anime_embroidery_blocked"), self.URL)

    def test_no_phantom_data(self):
        r = parse(_html("etsy_anime_embroidery_blocked"), self.URL)
        assert r["current_price"] is None
        assert r["on_sale"] is False
        assert r["label"] is None


class TestLabelDecoupledFromPrice:
    """Issue #8: label extraction is independent of price extraction. A page we
    can reach but can't pin a price on still yields its product name (from
    og:title / twitter:title) so the digest's 'Could not check' section reads
    'BibiSama — Wave Shorts: could not check' instead of a bare URL."""

    URL = "https://bibisama.example/products/wave-shorts"

    def test_og_title_label_without_price(self):
        html = (
            "<html><head>"
            '<meta property="og:title" content="BibiSama — Wave Shorts">'
            "</head><body><p>Price loads via JavaScript; nothing in the HTML.</p>"
            "</body></html>"
        )
        r = parse(html, self.URL)
        assert r["current_price"] is None
        assert r["label"] == "BibiSama — Wave Shorts"

    def test_twitter_title_fallback(self):
        html = (
            "<html><head>"
            '<meta name="twitter:title" content="Wave Shorts">'
            "</head><body></body></html>"
        )
        r = parse(html, self.URL)
        assert r["label"] == "Wave Shorts"

    def test_bare_title_is_not_a_phantom_label(self):
        # A generic <title> (bot-wall / host name) must NOT become a label.
        html = "<html><head><title>example.com</title></head><body></body></html>"
        r = parse(html, self.URL)
        assert r["label"] is None


# ---------------------------------------------------------------------------
# _html_oos behavior — conservative scoping to the cart-add form button
# ---------------------------------------------------------------------------

class TestHtmlOosScoping:
    """`_html_oos` must only inspect the add-to-cart submit button, not
    arbitrary page elements. Synthetic HTML used to isolate the behavior."""

    URL = "https://example.com/products/foo"

    def test_page_wide_sold_out_text_ignored(self):
        """A 'Sold out' button outside the cart-add form is NOT OOS."""
        html = """
        <html><body>
          <p>Why do popular items sell out so fast?</p>
          <button>Sold out — see FAQ</button>
          <form action="/cart/add" method="post">
            <button type="submit" name="add">Add to cart</button>
          </form>
          <span>$25.00</span>
        </body></html>
        """
        assert parse(html, self.URL)["out_of_stock"] is False

    def test_cart_button_sold_out_is_oos(self):
        """The cart-add form's submit button saying 'Sold out' IS OOS."""
        html = """
        <html><body>
          <form action="/cart/add" method="post">
            <button type="submit" name="add">Sold out</button>
          </form>
          <span>$25.00</span>
        </body></html>
        """
        assert parse(html, self.URL)["out_of_stock"] is True

    def test_fallback_to_name_add_button(self):
        """When no cart-add <form> is found, fall back to button[name=add]."""
        html = """
        <html><body>
          <button name="add">Sold out</button>
          <span>$25.00</span>
        </body></html>
        """
        assert parse(html, self.URL)["out_of_stock"] is True


# ---------------------------------------------------------------------------
# parse() return-value contract
# ---------------------------------------------------------------------------

class TestContract:
    """parse() must always return all required keys with correct types."""

    REQUIRED_KEYS = {
        "current_price", "original_price", "currency",
        "on_sale", "out_of_stock", "low_stock", "label",
        "total_variant_count", "available_variant_count", "color_options",
        "size_options", "available_sizes", "unpreferred_available_sizes",
        "preferred_sizes_applied",
        "error", "error_kind",
    }

    def test_keys_present(self):
        r = parse(_html("hakistop_farms_tee"), "https://hakistop.com/products/hakistop-farms-tee")
        assert self.REQUIRED_KEYS.issubset(r.keys())

    def test_on_sale_is_bool(self):
        r = parse(_html("aniqi_trafalgar"), "https://aniqi.com/products/trafalgar-joggers",
                  product_json=_json("aniqi_trafalgar"))
        assert isinstance(r["on_sale"], bool)

    def test_out_of_stock_is_bool(self):
        r = parse(_html("pomel_counter_punch"), "https://pomelclothing.com/products/counter-punch-white-tee",
                  product_json=_json("pomel_counter_punch"))
        assert isinstance(r["out_of_stock"], bool)

    def test_error_is_none(self):
        r = parse(_html("hokuro_wave_shorts"), "https://www.hokuroclothing.com/collections/best-sellers/products/wave-shorts",
                  product_json=_json("hokuro_wave_shorts"))
        assert r["error"] is None
        assert r["error_kind"] is None

    def test_color_options_is_list(self):
        """color_options is always a list (possibly empty), never None."""
        r = parse(_html("aniqi_trafalgar"), "https://aniqi.com/products/trafalgar-joggers",
                  product_json=_json("aniqi_trafalgar"))
        assert isinstance(r["color_options"], list)


# ---------------------------------------------------------------------------
# Variant counts and color options (Shopify-only fields)
# ---------------------------------------------------------------------------

class TestVariantFields:
    def test_aniqi_variant_count(self):
        """Aniqi trafalgar joggers has 5 size variants."""
        r = parse(_html("aniqi_trafalgar"), "https://aniqi.com/products/trafalgar-joggers",
                  product_json=_json("aniqi_trafalgar"))
        assert r["total_variant_count"] == 5

    def test_aniqi_available_count_is_none_when_unknown(self):
        """Shopify JSON omits 'available' → available_variant_count is None."""
        r = parse(_html("aniqi_trafalgar"), "https://aniqi.com/products/trafalgar-joggers",
                  product_json=_json("aniqi_trafalgar"))
        assert r["available_variant_count"] is None

    def test_aniqi_no_color_options(self):
        """Aniqi product has only a Size option, no Color."""
        r = parse(_html("aniqi_trafalgar"), "https://aniqi.com/products/trafalgar-joggers",
                  product_json=_json("aniqi_trafalgar"))
        assert r["color_options"] == []

    def test_no_product_json_means_no_variant_data(self):
        """parse() without product_json leaves variant counts at None and color_options [].
        We don't currently extract these from HTML — Shopify JSON is the source."""
        r = parse(_html("onsen_preorder"), "https://onsendesigns.net/products/preorder-v2-kanto-region-hoodie-copy")
        assert r["total_variant_count"] is None
        assert r["available_variant_count"] is None
        assert r["color_options"] == []
        assert r["size_options"] == []
        assert r["available_sizes"] == []


# ---------------------------------------------------------------------------
# Per-size availability + size-aware OOS override
#
# These tests synthesize Shopify product JSON inline rather than adding more
# fixtures — the fields exercised (options[], variants[].option1/2/3,
# variants[].available) are well-documented Shopify schema, and inline data
# makes the test intent obvious at a glance.
# ---------------------------------------------------------------------------

URL = "https://shop.example.com/products/foo"
_MINIMAL_HTML = "<html><body></body></html>"


def _shopify(*, options: list[dict], variants: list[dict], title: str = "Foo") -> dict:
    return {"product": {"title": title, "options": options, "variants": variants}}


def _size_variant(size: str, *, available: bool, price: str = "50.00") -> dict:
    return {
        "title": size, "price": price, "compare_at_price": "0.00",
        "option1": size, "option2": None, "option3": None,
        "available": available, "price_currency": "USD",
    }


class TestSizeOptionsParsing:
    """``_parse_shopify_json`` should pull size_options + available_sizes out of
    a Shopify product whose first option is named 'Size'."""

    def test_size_options_lists_every_size(self):
        product = _shopify(
            options=[{"name": "Size", "position": 1, "values": ["XS", "S", "M", "L", "XL"]}],
            variants=[_size_variant(s, available=True) for s in ("XS", "S", "M", "L", "XL")],
        )
        r = parse(_MINIMAL_HTML, URL, product_json=product)
        assert r["size_options"] == ["XS", "S", "M", "L", "XL"]

    def test_available_sizes_subset_of_in_stock(self):
        product = _shopify(
            options=[{"name": "Size", "position": 1, "values": ["XS", "S", "M", "L", "XL"]}],
            variants=[
                _size_variant("XS", available=False),
                _size_variant("S",  available=False),
                _size_variant("M",  available=True),
                _size_variant("L",  available=True),
                _size_variant("XL", available=False),
            ],
        )
        r = parse(_MINIMAL_HTML, URL, product_json=product)
        assert r["available_sizes"] == ["M", "L"]

    def test_size_label_case_insensitive(self):
        """Lowercase 'size' on the option name still triggers detection."""
        product = _shopify(
            options=[{"name": "size", "position": 1, "values": ["M", "L"]}],
            variants=[_size_variant("M", available=True),
                      _size_variant("L", available=True)],
        )
        r = parse(_MINIMAL_HTML, URL, product_json=product)
        assert r["size_options"] == ["M", "L"]
        assert r["available_sizes"] == ["M", "L"]

    def test_no_size_option_leaves_lists_empty(self):
        """An accessory with only a Color option must not produce size data."""
        product = _shopify(
            options=[{"name": "Color", "position": 1, "values": ["Black", "Red"]}],
            variants=[
                {"title": "Black", "price": "30.00", "compare_at_price": "0.00",
                 "option1": "Black", "option2": None, "option3": None,
                 "available": True, "price_currency": "USD"},
                {"title": "Red", "price": "30.00", "compare_at_price": "0.00",
                 "option1": "Red", "option2": None, "option3": None,
                 "available": True, "price_currency": "USD"},
            ],
        )
        r = parse(_MINIMAL_HTML, URL, product_json=product)
        assert r["size_options"] == []
        assert r["available_sizes"] == []

    def test_color_plus_size_picks_size_from_correct_option_index(self):
        """When the product has Color at position 1 and Size at position 2,
        we must read option2 — not option1 — for the size label."""
        product = _shopify(
            options=[
                {"name": "Color", "position": 1, "values": ["Black", "Red"]},
                {"name": "Size",  "position": 2, "values": ["S", "M", "L"]},
            ],
            variants=[
                {"title": "Black / S", "price": "50.00", "compare_at_price": "0.00",
                 "option1": "Black", "option2": "S", "option3": None,
                 "available": False, "price_currency": "USD"},
                {"title": "Black / M", "price": "50.00", "compare_at_price": "0.00",
                 "option1": "Black", "option2": "M", "option3": None,
                 "available": True, "price_currency": "USD"},
                {"title": "Red / L", "price": "50.00", "compare_at_price": "0.00",
                 "option1": "Red", "option2": "L", "option3": None,
                 "available": True, "price_currency": "USD"},
            ],
        )
        r = parse(_MINIMAL_HTML, URL, product_json=product)
        assert r["size_options"] == ["S", "M", "L"]
        # M and L both have at least one in-stock variant; S has none.
        assert set(r["available_sizes"]) == {"M", "L"}
        # And color_options is still populated independently.
        assert r["color_options"] == ["Black", "Red"]

    def test_no_available_field_means_no_available_sizes(self):
        """When the JSON omits 'available' on variants, we can't tell which
        sizes are in stock — available_sizes stays empty so callers fall back
        to existing page-level OOS logic."""
        product = _shopify(
            options=[{"name": "Size", "position": 1, "values": ["M", "L"]}],
            variants=[
                {"title": "M", "price": "50.00", "compare_at_price": "0.00",
                 "option1": "M", "option2": None, "option3": None,
                 "price_currency": "USD"},
                {"title": "L", "price": "50.00", "compare_at_price": "0.00",
                 "option1": "L", "option2": None, "option3": None,
                 "price_currency": "USD"},
            ],
        )
        r = parse(_MINIMAL_HTML, URL, product_json=product)
        assert r["size_options"] == ["M", "L"]
        assert r["available_sizes"] == []


class TestSizeAwareOOS:
    """``parse(preferred_sizes=...)`` upgrades the OOS decision: when the user's
    sizes are gone, the product is OOS regardless of what other sizes show."""

    def _five_size_product(self, available_sizes: set[str]) -> dict:
        sizes = ("XS", "S", "M", "L", "XL")
        return _shopify(
            options=[{"name": "Size", "position": 1, "values": list(sizes)}],
            variants=[_size_variant(s, available=(s in available_sizes)) for s in sizes],
        )

    def test_preferred_in_stock_stays_in_stock(self):
        """M is available — preferred sizes (M, L, XL) overlap → in stock, no note."""
        product = self._five_size_product({"M"})
        r = parse(_MINIMAL_HTML, URL, product_json=product,
                  preferred_sizes=("M", "L", "XL"))
        assert r["out_of_stock"] is False
        assert r["unpreferred_available_sizes"] == []

    def test_all_preferred_gone_forces_oos(self):
        """Only XS and S in stock; user wants M/L/XL → forced OOS with note."""
        product = self._five_size_product({"XS", "S"})
        r = parse(_MINIMAL_HTML, URL, product_json=product,
                  preferred_sizes=("M", "L", "XL"))
        assert r["out_of_stock"] is True
        assert r["unpreferred_available_sizes"] == ["S", "XS"]  # sorted

    def test_completely_oos_keeps_existing_oos(self):
        """Nothing available — already OOS, no note (empty unpreferred list)."""
        product = self._five_size_product(set())
        r = parse(_MINIMAL_HTML, URL, product_json=product,
                  preferred_sizes=("M", "L", "XL"))
        assert r["out_of_stock"] is True
        assert r["unpreferred_available_sizes"] == []

    def test_no_preferred_sizes_leaves_behavior_unchanged(self):
        """Empty tuple = feature off. M/L in stock, S OOS → in stock as before."""
        product = self._five_size_product({"M", "L"})
        r = parse(_MINIMAL_HTML, URL, product_json=product, preferred_sizes=())
        assert r["out_of_stock"] is False
        assert r["unpreferred_available_sizes"] == []

    def test_preferred_match_is_case_insensitive(self):
        """Variants use uppercase 'M'; user supplies lowercase 'm' → still matches."""
        product = self._five_size_product({"M"})
        r = parse(_MINIMAL_HTML, URL, product_json=product,
                  preferred_sizes=("m", "l"))
        assert r["out_of_stock"] is False

    def test_accessory_without_size_option_unaffected(self):
        """A color-only product never triggers the size-aware override."""
        product = _shopify(
            options=[{"name": "Color", "position": 1, "values": ["Black"]}],
            variants=[{"title": "Black", "price": "30.00", "compare_at_price": "0.00",
                       "option1": "Black", "option2": None, "option3": None,
                       "available": True, "price_currency": "USD"}],
        )
        r = parse(_MINIMAL_HTML, URL, product_json=product,
                  preferred_sizes=("M", "L"))
        assert r["out_of_stock"] is False
        assert r["unpreferred_available_sizes"] == []

    def test_low_stock_cleared_when_forced_oos(self):
        """If the default-variant was 'low stock' but every preferred size is
        unavailable, low_stock must be cleared (OOS supersedes)."""
        product = _shopify(
            options=[{"name": "Size", "position": 1, "values": ["XS", "M"]}],
            variants=[
                # XS in stock with low inventory → would normally set low_stock=True.
                {"title": "XS", "price": "50.00", "compare_at_price": "0.00",
                 "option1": "XS", "option2": None, "option3": None,
                 "available": True, "inventory_quantity": 2,
                 "price_currency": "USD"},
                _size_variant("M", available=False),
            ],
        )
        r = parse(_MINIMAL_HTML, URL, product_json=product,
                  preferred_sizes=("M",))
        assert r["out_of_stock"] is True
        assert r["low_stock"] is False
        assert r["unpreferred_available_sizes"] == ["XS"]

    def test_default_parameter_omitted_means_no_override(self):
        """Calling parse() without preferred_sizes (default) preserves
        existing behaviour exactly — Blaze Katana scenario shouldn't get
        force-OOS'd from a generic /products call site that doesn't know
        about the user's sizes."""
        product = self._five_size_product({"XS", "S"})
        r = parse(_MINIMAL_HTML, URL, product_json=product)
        # XS/S available → existing logic says in stock.
        assert r["out_of_stock"] is False
        assert r["unpreferred_available_sizes"] == []

    def test_preferred_sizes_applied_echoed_back(self):
        """Downstream stages persist the per-URL preference on each entry so
        the digest can render 'only in L' notes without re-deriving garment
        category. parse echoes the tuple verbatim."""
        product = self._five_size_product({"M", "L"})
        r = parse(_MINIMAL_HTML, URL, product_json=product,
                  preferred_sizes=("S", "M", "L"))
        assert r["preferred_sizes_applied"] == ["S", "M", "L"]

    def test_preferred_sizes_applied_empty_when_unset(self):
        product = self._five_size_product({"M", "L"})
        r = parse(_MINIMAL_HTML, URL, product_json=product)
        assert r["preferred_sizes_applied"] == []


class TestSizeNormalization:
    """Shops spell sizes out ('Medium', 'X-Large') and use foreign size spaces
    (ring sizes 7-11). The size-aware override must canonicalise labels before
    comparing, and must not apply at all when the product offers none of the
    user's sizes."""

    def _spelled_product(self, available_full_names):
        sizes = ["XX-Small", "X-Small", "Small", "Medium",
                 "Large", "X-Large", "XX-Large", "XXX-Large"]
        return _shopify(
            options=[{"name": "Size", "position": 1, "values": sizes}],
            variants=[_size_variant(s, available=(s in available_full_names)) for s in sizes],
        )

    def test_normalize_size_helper(self):
        from src.extract import _normalize_size
        assert _normalize_size("Medium") == "M"
        assert _normalize_size("X-Large") == "XL"
        assert _normalize_size("xx-large") == "XXL"
        assert _normalize_size("2XL") == "XXL"
        assert _normalize_size("Small") == "S"
        assert _normalize_size("7") == "7"          # ring size, unchanged
        assert _normalize_size("32") == "32"        # waist size, unchanged

    def test_spelled_out_size_matches_preferred(self):
        """'Medium'/'Large'/'X-Large' in stock must satisfy preferred M/L/XL."""
        product = self._spelled_product({"Medium", "Large", "X-Large"})
        r = parse(_MINIMAL_HTML, URL, product_json=product,
                  preferred_sizes=("M", "L", "XL"))
        assert r["out_of_stock"] is False
        assert r["unpreferred_available_sizes"] == []

    def test_spelled_out_only_medium_in_stock(self):
        """Only 'Medium' left — M is preferred, so still in stock."""
        product = self._spelled_product({"Medium"})
        r = parse(_MINIMAL_HTML, URL, product_json=product,
                  preferred_sizes=("M", "L", "XL"))
        assert r["out_of_stock"] is False

    def test_spelled_out_preferred_gone_forces_oos(self):
        """Only 'Small' left; user wants M/L/XL → forced OOS."""
        product = self._spelled_product({"Small"})
        r = parse(_MINIMAL_HTML, URL, product_json=product,
                  preferred_sizes=("M", "L", "XL"))
        assert r["out_of_stock"] is True
        assert r["unpreferred_available_sizes"] == ["Small"]

    def test_ring_sizes_fall_back_to_any_in_stock(self):
        """A ring sized 7-11 offers no M/L/XL equivalent → the size filter must
        not apply; any in-stock variant keeps it in stock."""
        sizes = ["7", "8", "9", "10", "11"]
        product = _shopify(
            options=[{"name": "Size", "position": 1, "values": sizes}],
            variants=[_size_variant(s, available=True) for s in sizes],
        )
        r = parse(_MINIMAL_HTML, URL, product_json=product,
                  preferred_sizes=("M", "L", "XL"))
        assert r["out_of_stock"] is False
        assert r["unpreferred_available_sizes"] == []

    def test_ring_all_sizes_oos_still_oos(self):
        """If the foreign-sized product is genuinely sold out everywhere, base
        availability (empty available_sizes) keeps it OOS — the override is
        skipped, not relied upon."""
        sizes = ["7", "8", "9"]
        product = _shopify(
            options=[{"name": "Size", "position": 1, "values": sizes}],
            variants=[_size_variant(s, available=False) for s in sizes],
        )
        r = parse(_MINIMAL_HTML, URL, product_json=product,
                  preferred_sizes=("M", "L", "XL"))
        assert r["out_of_stock"] is True


# ---------------------------------------------------------------------------
# Borrowing per-variant availability from the .js storefront endpoint
#
# Some Shopify storefronts omit 'available' from the public .json product
# endpoint but expose it on .js. _merge_js_availability patches the flags in
# by variant id so size-aware OOS detection works for those shops. .js prices
# are in cents, so we deliberately borrow ONLY 'available', never price.
# ---------------------------------------------------------------------------

class TestMergeJsAvailability:
    def _json_product(self, sizes):
        """A .json-shaped product whose variants lack the 'available' key."""
        return {"product": {"title": "Foo", "options": [
            {"name": "Size", "position": 1, "values": list(sizes)}],
            "variants": [
                {"id": 1000 + i, "title": s, "price": "32.00",
                 "compare_at_price": "0.00", "option1": s,
                 "option2": None, "option3": None, "price_currency": "USD"}
                for i, s in enumerate(sizes)
            ]}}

    def _js_payload(self, sizes, available):
        """A .js-shaped payload (no 'product' wrapper, prices in cents)."""
        return {"available": any(s in available for s in sizes),
                "variants": [
                    {"id": 1000 + i, "title": s, "price": 3200,
                     "option1": s, "available": (s in available)}
                    for i, s in enumerate(sizes)
                ]}

    def test_js_url_derivation(self):
        from src.extract import _shopify_js_url
        assert _shopify_js_url("https://shop.com/products/foo?variant=9#x") == \
            "https://shop.com/products/foo.js"

    def test_available_flags_patched_in_by_id(self):
        from src.extract import _merge_js_availability
        sizes = ("XS", "S", "M", "L", "XL")
        pj = self._json_product(sizes)
        js = self._js_payload(sizes, available={"M", "L"})
        _merge_js_availability(pj, js)
        got = {v["option1"]: v.get("available") for v in pj["product"]["variants"]}
        assert got == {"XS": False, "S": False, "M": True, "L": True, "XL": False}

    def test_merge_enables_size_aware_oos(self):
        """The Blaze Katana scenario end to end: .json has no availability, .js
        says only M/L are in stock, user's sizes include M → in stock."""
        from src.extract import _merge_js_availability
        sizes = ("XS", "S", "M", "L", "XL", "XXL", "XXXL")
        pj = self._json_product(sizes)
        _merge_js_availability(pj, self._js_payload(sizes, available={"M", "L"}))
        r = parse(_MINIMAL_HTML, URL, product_json=pj,
                  preferred_sizes=("M", "L", "XL"))
        assert r["out_of_stock"] is False
        assert r["available_sizes"] == ["M", "L"]
        assert r["current_price"] == 32.0  # from .json (dollars), not .js cents

    def test_noop_when_json_already_has_available(self):
        """If .json already exposes 'available', .js must not override it."""
        from src.extract import _merge_js_availability
        sizes = ("M", "L")
        pj = self._json_product(sizes)
        for v in pj["product"]["variants"]:
            v["available"] = True  # .json said both in stock
        _merge_js_availability(pj, self._js_payload(sizes, available=set()))
        assert all(v["available"] is True for v in pj["product"]["variants"])

    def test_noop_when_js_lacks_available(self):
        from src.extract import _merge_js_availability
        sizes = ("M", "L")
        pj = self._json_product(sizes)
        js = {"variants": [{"id": 1000, "title": "M", "option1": "M"},
                           {"id": 1001, "title": "L", "option1": "L"}]}
        _merge_js_availability(pj, js)
        assert all("available" not in v for v in pj["product"]["variants"])


# ---------------------------------------------------------------------------
# error_kind classification (extract()-only field, tested via _classify_error)
# ---------------------------------------------------------------------------

class TestErrorKind:
    def test_404_is_not_found(self):
        from src.extract import _classify_error
        assert _classify_error(None, 404) == "not_found"

    def test_403_is_blocked(self):
        from src.extract import _classify_error
        assert _classify_error(None, 403) == "blocked"

    def test_503_is_blocked(self):
        from src.extract import _classify_error
        assert _classify_error(None, 503) == "blocked"

    def test_500_is_server_error(self):
        from src.extract import _classify_error
        assert _classify_error(None, 500) == "server_error"

    def test_timeout_exception(self):
        from src.extract import _classify_error
        import httpx
        exc = httpx.ReadTimeout("slow")
        assert _classify_error(exc, None) == "timeout"

    def test_unknown_exception_is_other(self):
        from src.extract import _classify_error
        assert _classify_error(RuntimeError("nope"), None) == "other"


# ---------------------------------------------------------------------------
# WooCommerce per-size availability
#
# WooCommerce variable products inline their variations as a JSON array on the
# cart form's data-product_variations attribute (below the theme's AJAX
# threshold). parse() reads it into size_options/available_sizes so the same
# size-aware OOS override that works on Shopify also fires on Woo shops.
# Builders below emit the real DOM shape: a <form class="variations_form">
# carrying the (HTML-escaped) JSON plus a <select> for display-cased labels.
# ---------------------------------------------------------------------------

WOO_URL = "https://shop.example.com/product/foo"


def _woo_variation(size_slug, *, in_stock, max_qty="", key="attribute_pa_size",
                   price=None, regular=None):
    v = {"attributes": {key: size_slug}, "is_in_stock": in_stock, "max_qty": max_qty}
    if price is not None:
        v["display_price"] = price
    if regular is not None:
        v["display_regular_price"] = regular
    return v


def _woo_html(variations, *, size_select=None, size_key="attribute_pa_size",
              raw_attr=None):
    """Minimal WooCommerce variable-product page.

    ``size_select`` is ``[(slug, label), ...]`` rendered into a <select> so the
    parser can pick up display-cased labels; omit it to test the slug-uppercase
    fallback. ``raw_attr`` overrides the variations attribute verbatim (e.g.
    ``"false"`` for the above-threshold case).
    """
    if raw_attr is not None:
        attr = raw_attr
    else:
        attr = _htmllib.escape(json.dumps(variations), quote=True)
    options = "".join(
        f'<option value="{slug}">{label}</option>'
        for slug, label in (size_select or [])
    )
    select = (
        f'<select name="{size_key}"><option value="">Choose an option</option>'
        f"{options}</select>"
        if size_select is not None else ""
    )
    return (
        "<html><body>"
        f'<form class="variations_form cart" data-product_variations="{attr}">'
        f"{select}</form></body></html>"
    )


_WATER_PILLAR_SELECT = [("s", "S"), ("m", "M"), ("l", "L"),
                        ("xl", "XL"), ("xxl", "XXL"), ("xxxl", "XXXL")]


class TestWooCommerceVariations:
    def test_water_pillar_scenario_forces_oos_in_your_sizes(self):
        """The reported case: S–XXL gone, only XXXL (qty 4) left, user wants
        M/L/XL → forced OOS with 'still available in XXXL' and no low-stock."""
        variations = [
            _woo_variation("s", in_stock=False),
            _woo_variation("m", in_stock=False),
            _woo_variation("l", in_stock=False),
            _woo_variation("xl", in_stock=False),
            _woo_variation("xxl", in_stock=False),
            _woo_variation("xxxl", in_stock=True, max_qty=4),
        ]
        r = parse(_woo_html(variations, size_select=_WATER_PILLAR_SELECT), WOO_URL,
                  preferred_sizes=("M", "L", "XL"))
        assert r["size_options"] == ["S", "M", "L", "XL", "XXL", "XXXL"]
        assert r["available_sizes"] == ["XXXL"]
        assert r["out_of_stock"] is True
        assert r["low_stock"] is False
        assert r["unpreferred_available_sizes"] == ["XXXL"]

    def test_preferred_size_in_stock_stays_in_stock(self):
        variations = [
            _woo_variation("s", in_stock=False),
            _woo_variation("m", in_stock=True, max_qty=20),
            _woo_variation("l", in_stock=False),
        ]
        r = parse(_woo_html(variations, size_select=[("s", "S"), ("m", "M"), ("l", "L")]),
                  WOO_URL, preferred_sizes=("M", "L", "XL"))
        assert r["out_of_stock"] is False
        assert r["available_sizes"] == ["M"]
        assert r["unpreferred_available_sizes"] == []

    def test_all_sizes_oos_marks_product_oos(self):
        variations = [
            _woo_variation("m", in_stock=False),
            _woo_variation("l", in_stock=False),
        ]
        r = parse(_woo_html(variations, size_select=[("m", "M"), ("l", "L")]),
                  WOO_URL, preferred_sizes=("M", "L", "XL"))
        assert r["out_of_stock"] is True
        assert r["available_sizes"] == []
        # Override skipped (nothing available) → no spurious unpreferred note.
        assert r["unpreferred_available_sizes"] == []

    def test_low_stock_from_max_qty_when_preferred_size_low(self):
        """Only M in stock with qty 3 (<= threshold) → low stock, in stock."""
        variations = [
            _woo_variation("m", in_stock=True, max_qty=3),
            _woo_variation("l", in_stock=False),
        ]
        r = parse(_woo_html(variations, size_select=[("m", "M"), ("l", "L")]),
                  WOO_URL, preferred_sizes=("M", "L"))
        assert r["out_of_stock"] is False
        assert r["low_stock"] is True

    def test_unknown_max_qty_is_not_low_stock(self):
        """Stock-management off (max_qty='') → can't claim low stock."""
        variations = [_woo_variation("m", in_stock=True, max_qty="")]
        r = parse(_woo_html(variations, size_select=[("m", "M")]),
                  WOO_URL, preferred_sizes=("M",))
        assert r["out_of_stock"] is False
        assert r["low_stock"] is False

    def test_high_max_qty_is_not_low_stock(self):
        variations = [_woo_variation("m", in_stock=True, max_qty=50)]
        r = parse(_woo_html(variations, size_select=[("m", "M")]),
                  WOO_URL, preferred_sizes=("M",))
        assert r["low_stock"] is False

    def test_display_labels_pulled_from_select(self):
        """Variation slugs are lowercase; the note must use the <select>'s
        display text (XXXL), not the raw slug."""
        variations = [
            _woo_variation("m", in_stock=False),
            _woo_variation("xxxl", in_stock=True, max_qty=2),
        ]
        r = parse(_woo_html(variations, size_select=[("m", "M"), ("xxxl", "XXXL")]),
                  WOO_URL, preferred_sizes=("M",))
        assert r["unpreferred_available_sizes"] == ["XXXL"]

    def test_slug_uppercased_when_no_select(self):
        """No <select> present → fall back to uppercasing the slug."""
        variations = [
            _woo_variation("m", in_stock=False),
            _woo_variation("xxxl", in_stock=True, max_qty=2),
        ]
        r = parse(_woo_html(variations), WOO_URL, preferred_sizes=("M",))
        assert r["available_sizes"] == ["XXXL"]
        assert r["out_of_stock"] is True

    def test_above_ajax_threshold_attr_false_falls_back(self):
        """When variations exceed the inline threshold the attribute is the
        string 'false' — no size data, fall back to page-level OOS (in stock
        here, since nothing flags OOS)."""
        r = parse(_woo_html([], raw_attr="false"), WOO_URL,
                  preferred_sizes=("M", "L"))
        assert r["size_options"] == []
        assert r["available_sizes"] == []
        assert r["out_of_stock"] is False

    def test_colour_only_variable_product_reports_stock_not_sizes(self):
        """A variable product with no size dimension still yields product-level
        stock (all variants OOS → OOS) but empty size arrays."""
        variations = [
            {"attributes": {"attribute_pa_color": "red"}, "is_in_stock": False,
             "max_qty": ""},
            {"attributes": {"attribute_pa_color": "blue"}, "is_in_stock": False,
             "max_qty": ""},
        ]
        r = parse(_woo_html(variations), WOO_URL, preferred_sizes=("M", "L"))
        assert r["size_options"] == []
        assert r["available_sizes"] == []
        assert r["out_of_stock"] is True

    def test_no_preferred_sizes_keeps_woo_product_in_stock(self):
        """Feature off (no PREFERRED_SIZES): XXXL in stock keeps the product in
        stock — the override never runs, mirroring the Shopify default path."""
        variations = [
            _woo_variation("m", in_stock=False),
            _woo_variation("xxxl", in_stock=True, max_qty=4),
        ]
        r = parse(_woo_html(variations, size_select=[("m", "M"), ("xxxl", "XXXL")]),
                  WOO_URL, preferred_sizes=())
        assert r["out_of_stock"] is False
        assert r["unpreferred_available_sizes"] == []

    def test_non_woo_page_unaffected(self):
        """No variations_form → woo path is a no-op; behaves like before."""
        r = parse(_MINIMAL_HTML, WOO_URL, preferred_sizes=("M", "L"))
        assert r["size_options"] == []
        assert r["available_sizes"] == []

    def test_empty_size_variation_spans_all_offered(self):
        """A variation whose size value is '' applies to every size; in stock,
        it makes all offered sizes available."""
        variations = [
            _woo_variation("", in_stock=True, max_qty=""),
        ]
        r = parse(_woo_html(variations, size_select=[("s", "S"), ("m", "M"), ("l", "L")]),
                  WOO_URL, preferred_sizes=("M",))
        assert r["available_sizes"] == ["S", "M", "L"]
        assert r["out_of_stock"] is False

    def test_price_and_sale_from_variations(self):
        variations = [
            _woo_variation("m", in_stock=True, max_qty=10, price=25, regular=50),
            _woo_variation("l", in_stock=False, price=25, regular=50),
        ]
        r = parse(_woo_html(variations, size_select=[("m", "M"), ("l", "L")]),
                  WOO_URL, preferred_sizes=("M",))
        assert r["current_price"] == 25.0
        assert r["original_price"] == 50.0
        assert r["on_sale"] is True

    def test_no_sale_when_regular_not_greater(self):
        """display_regular_price == display_price (or 0) is not a markdown."""
        variations = [_woo_variation("m", in_stock=True, max_qty=10, price=30, regular=30)]
        r = parse(_woo_html(variations, size_select=[("m", "M")]), WOO_URL)
        assert r["current_price"] == 30.0
        assert r["original_price"] is None
        assert r["on_sale"] is False

    def test_price_prefers_in_stock_variant(self):
        """The buyer pays the in-stock variant's price, not a sold-out one's."""
        variations = [
            _woo_variation("s", in_stock=False, price=99, regular=99),
            _woo_variation("m", in_stock=True, max_qty=10, price=25, regular=50),
        ]
        r = parse(_woo_html(variations, size_select=[("s", "S"), ("m", "M")]), WOO_URL)
        assert r["current_price"] == 25.0
        assert r["original_price"] == 50.0

    def test_price_falls_back_to_first_when_all_oos(self):
        variations = [
            _woo_variation("m", in_stock=False, price=40, regular=40),
            _woo_variation("l", in_stock=False, price=40, regular=40),
        ]
        r = parse(_woo_html(variations, size_select=[("m", "M"), ("l", "L")]), WOO_URL)
        assert r["current_price"] == 40.0

    def test_missing_display_price_leaves_price_none(self):
        """A variation form without price fields yields no price (no phantom)
        and falls through — here to nothing, since the page has no price."""
        variations = [_woo_variation("m", in_stock=True, max_qty=10)]
        r = parse(_woo_html(variations, size_select=[("m", "M")]), WOO_URL)
        assert r["current_price"] is None
        assert r["on_sale"] is False

    def test_real_fixture_water_pillar(self):
        """Regression against the actual saved dattehameha.store page."""
        html = _html("dattehameha_water_pillar")
        url = ("https://dattehameha.store/product/"
               "water-pillar-hawaiian-shirt-oversize-kimono-fit")
        r = parse(html, url, preferred_sizes=("M", "L", "XL"))
        assert r["size_options"] == ["S", "M", "L", "XL", "XXL", "XXXL"]
        assert r["available_sizes"] == ["XXXL"]
        assert r["out_of_stock"] is True
        assert r["low_stock"] is False
        assert r["unpreferred_available_sizes"] == ["XXXL"]
        # Price comes from the variation JSON, not the "$10" shipping banner.
        assert r["current_price"] == 25.0
        assert r["original_price"] == 50.0
        assert r["on_sale"] is True
        assert r["currency"] == "USD"
        assert r["label"] == "Water Pillar Hawaiian Shirt"


# ---------------------------------------------------------------------------
# JSON-LD priceSpecification shapes
#
# Some WooCommerce SEO plugins omit a top-level offer.price and stash the live
# price in a (sometimes numeric-key-nested) UnitPriceSpecification. _jsonld_price
# must read it so simple (non-variable) Woo products — which have no inline
# variation JSON to fall back on — don't drop to the page-wide $ regex.
# ---------------------------------------------------------------------------

class TestJsonLdPriceSpecification:
    def _product(self, offer):
        return [{"@type": "Product", "name": "Foo", "offers": [offer]}]

    def test_nested_numeric_key_unit_price_spec(self):
        from src.extract import _jsonld_price
        offer = {
            "@type": "Offer",
            "priceSpecification": {
                "0": {"@type": "UnitPriceSpecification", "price": "25.00",
                      "priceCurrency": "USD"},
                "priceCurrency": "USD",
            },
            "availability": "https://schema.org/InStock",
        }
        r = _jsonld_price(self._product(offer))
        assert r["current_price"] == 25.0
        assert r["currency"] == "USD"
        assert r["out_of_stock"] is False

    def test_direct_offer_price_still_wins(self):
        """A normal offer.price is untouched by the spec fallback."""
        from src.extract import _jsonld_price
        offer = {"@type": "Offer", "price": "40.00", "priceCurrency": "USD",
                 "availability": "https://schema.org/InStock"}
        r = _jsonld_price(self._product(offer))
        assert r["current_price"] == 40.0

    def test_list_price_spec_is_original_not_current(self):
        """A ListPrice spec is the compare-at; the live price comes from the
        UnitPriceSpecification, and the markdown is surfaced as original."""
        from src.extract import _jsonld_price
        offer = {
            "@type": "Offer",
            "priceSpecification": [
                {"@type": "UnitPriceSpecification", "price": "25.00"},
                {"priceType": "https://schema.org/ListPrice", "price": "50.00"},
            ],
            "availability": "https://schema.org/InStock",
        }
        r = _jsonld_price(self._product(offer))
        assert r["current_price"] == 25.0
        assert r["original_price"] == 50.0

    def test_simple_woo_product_price_end_to_end(self):
        """A simple (non-variable) Woo product — no variations form — gets its
        price from the nested JSON-LD spec rather than the $ regex."""
        ld = json.dumps({
            "@type": "Product", "name": "Plain Tee",
            "offers": {"@type": "Offer", "availability": "https://schema.org/InStock",
                       "priceSpecification": {
                           "0": {"@type": "UnitPriceSpecification", "price": "32.00",
                                 "priceCurrency": "USD"}}},
        })
        html = (f'<html><head><script type="application/ld+json">{ld}</script>'
                f'</head><body>Free shipping over $100!</body></html>')
        r = parse(html, "https://shop.example.com/product/plain-tee")
        assert r["current_price"] == 32.0  # not 100 from the shipping line
        assert r["currency"] == "USD"


# ---------------------------------------------------------------------------
# Per-dimension `variants` map (the per-variant stock-tracking foundation)
#
# parse() returns a `variants` dict {dim: {options, available, low}} for size
# and/or colour, computed from the same per-variant sources as the legacy size
# fields. `available` is marginal (a value is in stock if any variant carrying
# it is), `low` sums known per-variant quantities for the value.
# ---------------------------------------------------------------------------

def _sv(option1, *, available, inv=None, option2=None, price="50.00"):
    """A Shopify variant with optional inventory_quantity."""
    v = {"title": option1, "price": price, "compare_at_price": "0.00",
         "option1": option1, "option2": option2, "option3": None,
         "available": available, "price_currency": "USD"}
    if inv is not None:
        v["inventory_quantity"] = inv
    return v


class TestShopifyVariantsMap:
    def test_size_available_and_low(self):
        """M plentiful, L down to 2 (low), XL sold out."""
        product = _shopify(
            options=[{"name": "Size", "position": 1, "values": ["M", "L", "XL"]}],
            variants=[_sv("M", available=True, inv=20),
                      _sv("L", available=True, inv=2),
                      _sv("XL", available=False, inv=0)],
        )
        size = parse(_MINIMAL_HTML, URL, product_json=product)["variants"]["size"]
        assert size["options"] == ["M", "L", "XL"]
        assert size["available"] == ["M", "L"]
        assert size["low"] == ["L"]

    def test_color_dimension_available(self):
        """Colour at option1, Size at option2: Red sold out in every size."""
        product = _shopify(
            options=[
                {"name": "Color", "position": 1, "values": ["Black", "Olive", "Red"]},
                {"name": "Size",  "position": 2, "values": ["S", "M"]},
            ],
            variants=[_sv("Black", available=True, option2="S"),
                      _sv("Black", available=True, option2="M"),
                      _sv("Olive", available=True, option2="M"),
                      _sv("Red", available=False, option2="S"),
                      _sv("Red", available=False, option2="M")],
        )
        v = parse(_MINIMAL_HTML, URL, product_json=product)["variants"]
        assert v["color"]["options"] == ["Black", "Olive", "Red"]
        assert v["color"]["available"] == ["Black", "Olive"]
        assert set(v["size"]["available"]) == {"S", "M"}  # marginal projection

    def test_low_sums_across_colors_of_a_size(self):
        """A size's low-stock totals its per-colour quantities: 2 Black + 2 Red = 4 ≤ 5."""
        product = _shopify(
            options=[
                {"name": "Color", "position": 1, "values": ["Black", "Red"]},
                {"name": "Size",  "position": 2, "values": ["M"]},
            ],
            variants=[_sv("Black", available=True, inv=2, option2="M"),
                      _sv("Red", available=True, inv=2, option2="M")],
        )
        assert parse(_MINIMAL_HTML, URL, product_json=product)["variants"]["size"]["low"] == ["M"]

    def test_unknown_inventory_is_not_low(self):
        product = _shopify(
            options=[{"name": "Size", "position": 1, "values": ["M"]}],
            variants=[_sv("M", available=True)],  # no inventory_quantity
        )
        size = parse(_MINIMAL_HTML, URL, product_json=product)["variants"]["size"]
        assert size["available"] == ["M"]
        assert size["low"] == []

    def test_no_available_field_omits_variants_entirely(self):
        """Without per-variant 'available' we can't say which sizes are in stock,
        so the dimension is omitted from `variants` (a present dim always means
        availability is known) — the HTML fallback decides product-level OOS."""
        product = _shopify(
            options=[{"name": "Size", "position": 1, "values": ["M", "L"]}],
            variants=[{"title": "M", "price": "50.00", "compare_at_price": "0.00",
                       "option1": "M", "option2": None, "option3": None,
                       "price_currency": "USD"},
                      {"title": "L", "price": "50.00", "compare_at_price": "0.00",
                       "option1": "L", "option2": None, "option3": None,
                       "price_currency": "USD"}],
        )
        r = parse(_MINIMAL_HTML, URL, product_json=product)
        assert r["variants"] == {}
        assert r["size_options"] == ["M", "L"]  # legacy field still populated

    def test_no_size_no_color_means_empty_variants(self):
        product = _shopify(
            options=[{"name": "Material", "position": 1, "values": ["Cotton"]}],
            variants=[_sv("Cotton", available=True)],
        )
        assert parse(_MINIMAL_HTML, URL, product_json=product)["variants"] == {}


def _woo_html_multi(variations, selects):
    """WooCommerce variable-product page with one <select> per dimension.

    ``selects`` is ``{attr_key: [(slug, label), ...]}``.
    """
    attr = _htmllib.escape(json.dumps(variations), quote=True)
    selects_html = ""
    for key, opts in selects.items():
        options = "".join(f'<option value="{s}">{lbl}</option>' for s, lbl in opts)
        selects_html += (f'<select name="{key}">'
                         f'<option value="">Choose an option</option>{options}</select>')
    return ("<html><body>"
            f'<form class="variations_form cart" data-product_variations="{attr}">'
            f"{selects_html}</form></body></html>")


class TestWooCommerceVariantsMap:
    def test_size_and_color_dimensions(self):
        variations = [
            {"attributes": {"attribute_pa_size": "m", "attribute_pa_color": "black"},
             "is_in_stock": True, "max_qty": 2,
             "display_price": 25, "display_regular_price": 50},
            {"attributes": {"attribute_pa_size": "l", "attribute_pa_color": "black"},
             "is_in_stock": False, "max_qty": ""},
            {"attributes": {"attribute_pa_size": "m", "attribute_pa_color": "red"},
             "is_in_stock": False, "max_qty": ""},
        ]
        html = _woo_html_multi(variations, {
            "attribute_pa_size": [("m", "M"), ("l", "L")],
            "attribute_pa_color": [("black", "Black"), ("red", "Red")],
        })
        v = parse(html, WOO_URL)["variants"]
        assert v["size"]["options"] == ["M", "L"]
        assert v["size"]["available"] == ["M"]
        assert v["size"]["low"] == ["M"]          # qty 2
        assert v["color"]["options"] == ["Black", "Red"]
        assert v["color"]["available"] == ["Black"]
        assert v["color"]["low"] == ["Black"]     # the only in-stock variant is qty 2

    def test_color_only_emits_color_variants(self):
        variations = [
            {"attributes": {"attribute_pa_color": "red"}, "is_in_stock": False, "max_qty": ""},
            {"attributes": {"attribute_pa_color": "blue"}, "is_in_stock": True, "max_qty": 3},
        ]
        html = _woo_html_multi(variations,
                               {"attribute_pa_color": [("red", "Red"), ("blue", "Blue")]})
        r = parse(html, WOO_URL)
        assert "size" not in r["variants"]
        color = r["variants"]["color"]
        assert color["options"] == ["Red", "Blue"]
        assert color["available"] == ["Blue"]
        assert color["low"] == ["Blue"]           # qty 3
        assert r["out_of_stock"] is False
