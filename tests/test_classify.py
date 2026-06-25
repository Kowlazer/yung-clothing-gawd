"""Tests for classify.py against the real watchlist fixture."""
from pathlib import Path

import pytest

from src.classify import (
    Category,
    Entry,
    classify,
    sales_tracking_shops,
    _classify_url,
)

FIXTURE = Path(__file__).parent / "fixtures" / "watchlist.txt"


@pytest.fixture(scope="module")
def entries() -> list[Entry]:
    return classify(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def by_category(entries) -> dict[Category, list[Entry]]:
    result: dict[Category, list[Entry]] = {}
    for e in entries:
        result.setdefault(e.category, []).append(e)
    return result


# ---------------------------------------------------------------------------
# URL classification
# ---------------------------------------------------------------------------

class TestUrlClassification:
    def test_shopify_product_url(self, entries):
        values = [e.value for e in entries]
        assert "https://peakwear.com/products/cable-knit-sweater" in values

    def test_shopify_product_url_category(self, entries):
        e = next(e for e in entries if e.value == "https://peakwear.com/products/cable-knit-sweater")
        assert e.category == "PRODUCT_URL"

    def test_product_url_with_querystring(self, entries):
        # Query-string preservation: the merino-henley URL has ?_pos=&_sid=&_ss=
        # in the Shops and URLs section.
        matching = [e for e in entries if "_pos=4" in e.value and "peakwear" in e.value]
        assert matching, "peakwear query-string URL not found"
        assert matching[0].category == "PRODUCT_URL"

    def test_collection_url_is_shop(self, entries):
        matching = [e for e in entries if e.value == "https://peakwear.com/collections/jackets"]
        assert matching, "peakwear /collections/jackets not found"
        assert matching[0].category == "SHOP_URL"

    def test_collections_products_is_product(self, entries):
        # /collections/<name>/products/<slug> → PRODUCT_URL
        url = "https://peakwear.com/collections/sweaters/products/lambswool-crewneck"
        matching = [e for e in entries if e.value == url]
        assert matching, f"{url} not found"
        assert matching[0].category == "PRODUCT_URL"

    def test_etsy_listing_is_product(self, entries):
        matching = [e for e in entries if "etsy.com/listing" in e.value]
        assert matching, "Etsy listing not found"
        for e in matching:
            assert e.category == "PRODUCT_URL"

    def test_woocommerce_product_url(self, entries):
        url = "https://www.vergestudio.com/product/boxy-tee/"
        matching = [e for e in entries if e.value == url]
        assert matching, f"{url} not found"
        assert matching[0].category == "PRODUCT_URL"

    def test_woocommerce_product_category_is_shop(self, entries):
        url = "https://www.vergestudio.com/product-category/apparel/outerwear/"
        matching = [e for e in entries if e.value == url]
        assert matching, f"{url} not found"
        assert matching[0].category == "SHOP_URL"

    def test_reddit_urls_excluded(self, entries):
        reddit = [e for e in entries if "reddit.com" in e.value]
        assert not reddit, "Reddit URLs should be IGNORE (excluded from entries)"

    def test_redbubble_urls_excluded(self, entries):
        rb = [e for e in entries if "redbubble.com" in e.value]
        assert not rb, "Redbubble URLs should be IGNORE"

    def test_shop_homepage_is_shop_url(self, entries):
        home = [e for e in entries if e.value == "https://cinderhall.com/"]
        assert home, "CinderHall homepage not found"
        assert home[0].category == "SHOP_URL"

    def test_peakwear_products_have_shop_context(self, entries):
        peak = [e for e in entries if "peakwear.com/products" in e.value]
        assert len(peak) >= 5
        for e in peak:
            assert e.context == "PeakWear", f"expected PeakWear context, got {e.context!r}"

    def test_shop_header_with_many_products(self, entries):
        peak = [e for e in entries if "peakwear.com/products" in e.value]
        assert len({e.value for e in peak}) >= 7

    def test_notes_section_urls_filtered(self, entries):
        # URLs from the free-form "Notes:" section above the "Shops and URLs:"
        # header should be filtered out entirely.
        skipme = [e for e in entries if "skipme-shop" in e.value]
        assert not skipme, "Notes-section skipme URL should be filtered"
        reddit = [e for e in entries if "reddit.com" in e.value]
        assert not reddit, "Notes-section Reddit URL should be filtered"


# ---------------------------------------------------------------------------
# Shop names
# ---------------------------------------------------------------------------

class TestShopNames:
    def test_shop_with_loose_items_survives(self, by_category):
        # WillowCraft has loose items underneath, so it's not an orphan and
        # survives classification.
        names = [e.value for e in by_category.get("SHOP_NAME", [])]
        assert "WillowCraft" in names

    def test_peakwear_shop_name(self, by_category):
        names = [e.value for e in by_category.get("SHOP_NAME", [])]
        assert "PeakWear" in names

    def test_verge_in_shops_section(self, by_category):
        # VergeStudio appears as a proper header with URLs in the Shops and URLs section.
        names = [e.value for e in by_category.get("SHOP_NAME", [])]
        assert "VergeStudio" in names

    def test_orphan_shop_names_dropped(self, by_category):
        # Bare "ShopName:" headers with no URLs / items underneath are
        # placeholders the user keeps as a memory aid for purchased shops.
        # They should NOT be emitted (no DDG / Claude waste).
        names = [e.value for e in by_category.get("SHOP_NAME", [])]
        for orphan in ("GhostShopA", "GhostShopB", "GhostShopC"):
            assert orphan not in names, f"{orphan} should be dropped (no children)"

    def test_notes_section_shop_names_filtered(self, by_category):
        # Names that only appear in the "Notes:" section should be filtered
        # out unless they also appear as proper headers in Shops and URLs.
        names = [e.value.lower() for e in by_category.get("SHOP_NAME", [])]
        assert not any(n == "somebrand" for n in names), \
            "SomeBrand is only in Notes section"
        assert not any(n == "rugplaceone" for n in names), \
            "RugPlaceOne is only in Notes section"


# ---------------------------------------------------------------------------
# Loose mentions
# ---------------------------------------------------------------------------

class TestLooseMentions:
    def test_shop_header_bullet_items(self, by_category):
        # WillowCraft is in the Shops and URLs section with multiple loose items
        # (Oatmeal waffle tee, Olive ripstop shorts, etc.) listed without dashes.
        loose = [e for e in by_category.get("LOOSE_MENTION", [])
                 if e.context == "WillowCraft"]
        assert len(loose) >= 2

    def test_tidalsupply_brown_corduroy(self, by_category):
        # "Brown corduroy shorts" loose line under TidalSupply header.
        loose = [e for e in by_category.get("LOOSE_MENTION", [])
                 if e.context == "TidalSupply"]
        items = [e.value.lower() for e in loose]
        assert any("corduroy" in v for v in items)

    def test_notes_section_loose_mentions_filtered(self, by_category):
        # Loose mentions that only appear in the Notes section (e.g. the
        # "SomeBrand cool graphic tee" line) should be filtered out entirely —
        # Notes is for free-form scratch, not shopping intent.
        loose = [e.value.lower() for e in by_category.get("LOOSE_MENTION", [])]
        assert not any("somebrand" in v for v in loose)
        assert not any("graphic tee" in v for v in loose)


# ---------------------------------------------------------------------------
# Ignores (things that must NOT appear in entries)
# ---------------------------------------------------------------------------

class TestIgnored:
    def test_generic_shoe_search_ignored(self, entries):
        assert not any("wide toe box" in e.value.lower() for e in entries)

    def test_ring_search_ignored(self, entries):
        assert not any("look for a ring" in e.value.lower() for e in entries)

    def test_places_to_buy_rugs_ignored(self, entries):
        assert not any("places to buy rugs" in e.value.lower() for e in entries)

    def test_orders_header_ignored(self, entries):
        assert not any("orders to make" in e.value.lower() for e in entries)

    def test_threadheads_ignored(self, entries):
        assert not any("threadheads" in e.value.lower() for e in entries)


# ---------------------------------------------------------------------------
# Sanity counts (loose bounds — not brittle exact numbers)
# ---------------------------------------------------------------------------

class TestCounts:
    def test_has_many_product_urls(self, by_category):
        assert len(by_category.get("PRODUCT_URL", [])) >= 30

    def test_has_many_shop_names(self, by_category):
        assert len(by_category.get("SHOP_NAME", [])) >= 15

    def test_has_loose_mentions(self, by_category):
        assert len(by_category.get("LOOSE_MENTION", [])) >= 3


# ---------------------------------------------------------------------------
# Section-split feature (Notes: above, Shops and URLs: below)
# ---------------------------------------------------------------------------

class TestSectionSplit:
    def test_notes_section_ignored_when_marker_present(self):
        text = (
            "Notes:\n"
            "https://noisy.com/products/skip-me\n"
            "OtherShop SomeBrand graphic tee\n"
            "\n"
            "Shops and URLs:\n"
            "PeakWear:\n"
            "https://peakwear.com/products/keep-me\n"
        )
        entries = classify(text)
        values = [e.value for e in entries]
        assert "https://peakwear.com/products/keep-me" in values
        assert "https://noisy.com/products/skip-me" not in values

    def test_falls_back_to_whole_doc_when_marker_absent(self):
        # Older docs without the split header should still parse the whole text.
        text = (
            "PeakWear:\n"
            "https://peakwear.com/products/joggers\n"
        )
        entries = classify(text)
        values = [e.value for e in entries]
        assert "https://peakwear.com/products/joggers" in values

    def test_marker_matches_case_insensitively(self):
        text = (
            "Notes:\nstuff\n"
            "SHOPS AND URLS:\n"
            "PeakWear:\nhttps://peakwear.com/products/x\n"
        )
        entries = classify(text)
        assert any("peakwear.com/products/x" in e.value for e in entries)

    def test_clothing_prefix_on_main_marker(self):
        # Users who add a Non-clothing section commonly rename the main
        # header to "Clothing Shops and URLs:" to make the pair explicit.
        # Both forms must be accepted.
        text = (
            "Notes:\n"
            "https://noisy.com/products/skip-me\n"
            "Clothing Shops and URLs:\n"
            "PeakWear:\nhttps://peakwear.com/products/keep-me\n"
        )
        entries = classify(text)
        values = [e.value for e in entries]
        assert "https://peakwear.com/products/keep-me" in values
        assert "https://noisy.com/products/skip-me" not in values


# ---------------------------------------------------------------------------
# Orphan shop filter (bare "ShopName:" with no children gets dropped)
# ---------------------------------------------------------------------------

class TestOrphanShopFilter:
    def test_orphan_shop_dropped(self):
        text = (
            "Shops and URLs:\n"
            "GhostShopA:\n"
            "\n"
            "PeakWear:\nhttps://peakwear.com/products/joggers\n"
        )
        entries = classify(text)
        names = [e.value for e in entries if e.category == "SHOP_NAME"]
        assert "GhostShopA" not in names
        assert "PeakWear" in names

    def test_shop_with_url_child_kept(self):
        text = "PeakWear:\nhttps://peakwear.com/products/x\n"
        entries = classify(text)
        names = [e.value for e in entries if e.category == "SHOP_NAME"]
        assert "PeakWear" in names

    def test_shop_with_loose_mention_kept(self):
        text = "TidalSupply:\nBrown corduroy shorts\n"
        entries = classify(text)
        names = [e.value for e in entries if e.category == "SHOP_NAME"]
        assert "TidalSupply" in names

    def test_multiple_orphans_all_dropped(self):
        text = (
            "Shops and URLs:\n"
            "ShopA:\n"
            "\n"
            "ShopB:\n"
            "\n"
            "PeakWear:\nhttps://peakwear.com/products/x\n"
        )
        entries = classify(text)
        names = [e.value for e in entries if e.category == "SHOP_NAME"]
        assert names == ["PeakWear"]


# ---------------------------------------------------------------------------
# Non-clothing section — entries below the marker get is_clothing=False
# and bypass the clothing-keyword gate on bare item lines.
# ---------------------------------------------------------------------------

class TestNonClothingSection:
    def test_clothing_section_defaults_true(self):
        text = (
            "Shops and URLs:\n"
            "PeakWear:\nhttps://peakwear.com/products/joggers\n"
        )
        entries = classify(text)
        assert all(e.is_clothing for e in entries)

    def test_entries_below_marker_flagged_false(self):
        text = (
            "Shops and URLs:\n"
            "PeakWear:\nhttps://peakwear.com/products/joggers\n"
            "\n"
            "Non-clothing Shops and URLs:\n"
            "Logitech:\nhttps://logitech.com/products/g-pro-x-superlight\n"
        )
        entries = classify(text)
        by_value = {e.value: e for e in entries}
        assert by_value["https://peakwear.com/products/joggers"].is_clothing is True
        assert by_value["https://logitech.com/products/g-pro-x-superlight"].is_clothing is False
        assert by_value["PeakWear"].is_clothing is True
        assert by_value["Logitech"].is_clothing is False

    def test_marker_matches_case_and_hyphen_variants(self):
        for header in (
            "Non-clothing Shops and URLs:",
            "Non Clothing Shops and URLs:",
            "NON-CLOTHING SHOPS AND URLS:",
            "non-clothing shops and urls:",
        ):
            text = (
                "Shops and URLs:\n"
                "PeakWear:\nhttps://peakwear.com/products/joggers\n"
                f"{header}\n"
                "Logitech:\nhttps://logitech.com/products/mouse\n"
            )
            entries = classify(text)
            mouse = next(e for e in entries if "mouse" in e.value)
            assert mouse.is_clothing is False, f"failed for header: {header!r}"

    def test_bare_item_line_accepted_under_non_clothing_shop(self):
        # In the clothing section a "Logitech G304 wireless mouse" line
        # (no dash, no clothing keyword) falls through to IGNORE. Inside
        # the non-clothing section it should attach to the shop.
        text = (
            "Shops and URLs:\n"
            "Non-clothing Shops and URLs:\n"
            "Logitech:\n"
            "G304 wireless mouse\n"
        )
        entries = classify(text)
        loose = [e for e in entries if e.category == "LOOSE_MENTION"]
        assert any("G304 wireless mouse" in e.value for e in loose)
        assert all(not e.is_clothing for e in loose)

    def test_current_shop_does_not_leak_across_marker(self):
        # PeakWear is the active shop right before the marker. A bare URL
        # after the marker must not adopt PeakWear as its context.
        text = (
            "Shops and URLs:\n"
            "PeakWear:\n"
            "Non-clothing Shops and URLs:\n"
            "https://logitech.com/products/g-pro-x-superlight\n"
        )
        entries = classify(text)
        mouse = next(e for e in entries if "logitech" in e.value)
        assert mouse.context == ""

    def test_dash_item_inside_non_clothing_section(self):
        text = (
            "Shops and URLs:\n"
            "Non-clothing Shops and URLs:\n"
            "Logitech:\n"
            "- G Pro X Superlight\n"
        )
        entries = classify(text)
        loose = [e for e in entries if e.category == "LOOSE_MENTION"]
        assert loose and loose[0].value == "G Pro X Superlight"
        assert loose[0].is_clothing is False


# ---------------------------------------------------------------------------
# Inline priority marker (⭐ / [priority]) on a URL line → Entry.priority
# ---------------------------------------------------------------------------

class TestPriorityMarker:
    def test_unmarked_entries_default_false(self):
        text = "PeakWear:\nhttps://peakwear.com/products/joggers\n"
        entries = classify(text)
        assert all(e.priority is False for e in entries)

    def test_star_marks_product_url(self):
        text = "PeakWear:\nhttps://peakwear.com/products/joggers ⭐\n"
        entries = classify(text)
        url = next(e for e in entries if e.category == "PRODUCT_URL")
        assert url.priority is True
        # The shop header on its own line is not marked.
        shop = next(e for e in entries if e.category == "SHOP_NAME")
        assert shop.priority is False

    def test_emoji_presentation_selector_variant(self):
        # ⭐️ = ⭐ + U+FE0F (what many keyboards actually insert).
        text = "PeakWear:\nhttps://peakwear.com/products/joggers ⭐️\n"
        entries = classify(text)
        url = next(e for e in entries if e.category == "PRODUCT_URL")
        assert url.priority is True

    def test_textual_tag_marks_url_case_insensitively(self):
        for tag in ("[priority]", "[PRIORITY]", "[Priority]"):
            text = f"PeakWear:\nhttps://peakwear.com/products/joggers {tag}\n"
            entries = classify(text)
            url = next(e for e in entries if e.category == "PRODUCT_URL")
            assert url.priority is True, f"failed for tag {tag!r}"

    def test_marker_does_not_leak_into_url_value(self):
        # A marker typed flush against the URL (no space) must not become part
        # of the stored, fetchable URL.
        text = "PeakWear:\nhttps://peakwear.com/products/joggers⭐\n"
        entries = classify(text)
        url = next(e for e in entries if e.category == "PRODUCT_URL")
        assert url.value == "https://peakwear.com/products/joggers"
        assert url.priority is True

    def test_spaced_marker_url_value_clean(self):
        text = "PeakWear:\nhttps://peakwear.com/products/joggers ⭐\n"
        entries = classify(text)
        url = next(e for e in entries if e.category == "PRODUCT_URL")
        assert url.value == "https://peakwear.com/products/joggers"

    def test_priority_carries_is_clothing_flag(self):
        text = (
            "Shops and URLs:\n"
            "Non-clothing Shops and URLs:\n"
            "Logitech:\nhttps://logitech.com/products/mouse ⭐\n"
        )
        entries = classify(text)
        url = next(e for e in entries if e.category == "PRODUCT_URL")
        assert url.priority is True
        assert url.is_clothing is False

    def test_unmarked_sibling_url_stays_unmarked(self):
        text = (
            "PeakWear:\n"
            "https://peakwear.com/products/watched ⭐\n"
            "https://peakwear.com/products/not-watched\n"
        )
        entries = classify(text)
        by_value = {e.value: e for e in entries}
        assert by_value["https://peakwear.com/products/watched"].priority is True
        assert by_value["https://peakwear.com/products/not-watched"].priority is False


# ---------------------------------------------------------------------------
# Amazon product URLs → UNTRACKED_URL (can't be crawled; surface-only)
# ---------------------------------------------------------------------------

class TestUntrackedAmazon:
    def test_dp_url_is_untracked(self):
        url = "https://www.amazon.com/Amazon-Essentials-Pullover/dp/B07YF5CR5Z"
        assert _classify_url(url) == "UNTRACKED_URL"

    def test_bare_dp_url_is_untracked(self):
        assert _classify_url("https://www.amazon.com/dp/B0DBQ5C9P5") == "UNTRACKED_URL"

    def test_gp_product_url_is_untracked(self):
        # Would otherwise match the generic /product/ PRODUCT_URL rule.
        url = "https://www.amazon.com/gp/product/B08NY1QFQR"
        assert _classify_url(url) == "UNTRACKED_URL"

    def test_non_us_amazon_tld_is_untracked(self):
        assert _classify_url("https://www.amazon.co.uk/dp/B07YF5CR5Z") == "UNTRACKED_URL"

    def test_amazon_homepage_stays_shop_url(self):
        # No product path → still a (pointless but harmless) SHOP_URL, not surfaced
        # as an item.
        assert _classify_url("https://www.amazon.com/") == "SHOP_URL"
        assert _classify_url("https://www.amazon.com/s?k=hoodie") == "SHOP_URL"

    def test_non_amazon_product_url_unaffected(self):
        assert _classify_url("https://peakwear.com/products/joggers") == "PRODUCT_URL"

    def test_classify_tags_entry_and_keeps_shop_context(self):
        text = (
            "Amazon:\n"
            "https://www.amazon.com/Amazon-Essentials-Pullover/dp/B07YF5CR5Z\n"
            "https://www.amazon.com/32-DEGREES-Heather/dp/B08NY1QFQR\n"
        )
        entries = classify(text)
        untracked = [e for e in entries if e.category == "UNTRACKED_URL"]
        assert len(untracked) == 2
        assert all(e.context == "Amazon" for e in untracked)
        assert all(e.is_clothing for e in untracked)

    def test_marker_strip_still_applies(self):
        # A priority marker typed flush against an Amazon URL is still stripped so
        # the stored URL stays valid (even though untracked items can't be pinned).
        text = "Amazon:\nhttps://www.amazon.com/dp/B07YF5CR5Z[priority]\n"
        entries = classify(text)
        url = next(e for e in entries if e.category == "UNTRACKED_URL")
        assert url.value == "https://www.amazon.com/dp/B07YF5CR5Z"


# ---------------------------------------------------------------------------
# "Shops to track sales for:" section (SMS/email sale allowlist in the Doc)
# ---------------------------------------------------------------------------

class TestSalesTrackingShops:
    def test_extracted_from_fixture(self, entries):
        # The fixture's section: MidnightMerch / "- FlashDeals Co" / "NovaThread,"
        # / "novathread" → bullet + trailing comma stripped, case-insensitive
        # dedup, first-seen order + original casing preserved.
        shops = sales_tracking_shops(FIXTURE.read_text(encoding="utf-8"))
        assert shops == ["MidnightMerch", "FlashDeals Co", "NovaThread"]

    def test_section_lines_are_not_classified(self, entries):
        # Crucial: the names must NOT leak in as entries (a bare CamelCase name
        # like MidnightMerch would otherwise resolve to SHOP_NAME and get
        # pointlessly homepage-checked).
        values = {e.value for e in entries}
        contexts = {e.context for e in entries}
        for name in ("MidnightMerch", "FlashDeals Co", "NovaThread"):
            assert name not in values
            assert name not in contexts
        # The header line itself (which matches the generic ShopName: shape) is
        # gone too.
        assert not any("track sales" in e.value.lower() for e in entries)

    def test_blank_line_terminates_section(self):
        text = (
            "Shops to track sales for:\n"
            "Lumastep\n"
            "Grey Fox\n"
            "\n"
            "Driftwave:\n"
            "https://driftwave.com/products/airmax\n"
        )
        assert sales_tracking_shops(text) == ["Lumastep", "Grey Fox"]
        # The shop below the blank line is parsed normally, untouched.
        entries = classify(text)
        assert any(e.value == "https://driftwave.com/products/airmax" for e in entries)
        assert any(e.category == "SHOP_NAME" and e.value == "Driftwave" for e in entries)

    def test_known_header_terminates_without_blank_line(self):
        text = (
            "Shops to track sales for:\n"
            "Lumastep\n"
            "Shops and URLs:\n"
            "Driftwave:\n"
            "https://driftwave.com/products/airmax\n"
        )
        assert sales_tracking_shops(text) == ["Lumastep"]

    def test_section_above_shops_and_urls_marker(self):
        # classify() discards everything above "Shops and URLs:", but the
        # allowlist must still be found wherever the user places it.
        text = (
            "Notes: budget month\n"
            "Shops to track sales for:\n"
            "Lumastep\n"
            "Grey Fox\n"
            "\n"
            "Shops and URLs:\n"
            "Driftwave:\n"
            "https://driftwave.com/products/airmax\n"
        )
        assert sales_tracking_shops(text) == ["Lumastep", "Grey Fox"]
        # And nothing from the section leaks into the parsed entries.
        values = {e.value for e in classify(text)}
        assert "Lumastep" not in values and "Grey Fox" not in values

    def test_runs_to_end_of_doc(self):
        text = "Shops to track sales for:\nLumastep\nGrey Fox\nNovaThread\n"
        assert sales_tracking_shops(text) == ["Lumastep", "Grey Fox", "NovaThread"]

    def test_absent_section_returns_empty(self):
        assert sales_tracking_shops("Driftwave:\nhttps://driftwave.com/products/a\n") == []

    def test_case_insensitive_header(self):
        text = "SHOPS TO TRACK SALES FOR:\nLumastep\n"
        assert sales_tracking_shops(text) == ["Lumastep"]


class TestGenericSectionResetsShop:
    """Issue #4: a generic section divider that looks like a "ShopName:" header
    must reset the shop context so URLs beneath it do not inherit the previous
    shop (animecollective product URLs were being attributed to "100moons")."""

    def test_animecollective_urls_do_not_inherit_prior_shop(self):
        text = (
            "Shops and URLs:\n"
            "100moons:\n"
            "https://100moons.com/products/foo\n"
            "Animecollective stuff from sale:\n"
            "https://animecollective.com/products/bar\n"
            "https://animecollective.com/products/baz\n"
        )
        by_url = {
            e.value: e for e in classify(text) if e.category == "PRODUCT_URL"
        }
        # The 100moons URL keeps its shop.
        assert by_url["https://100moons.com/products/foo"].context == "100moons"
        # The animecollective URLs must NOT inherit "100moons".
        assert by_url["https://animecollective.com/products/bar"].context == ""
        assert by_url["https://animecollective.com/products/baz"].context == ""

    def test_divider_does_not_emit_a_shop_name_entry(self):
        text = (
            "Shops and URLs:\n"
            "100moons:\n"
            "https://100moons.com/products/foo\n"
            "Orders to make next:\n"
            "https://elsewhere.com/products/bar\n"
        )
        shop_names = {e.value for e in classify(text) if e.category == "SHOP_NAME"}
        assert "Orders to make next" not in shop_names
        assert "Animecollective stuff from sale" not in shop_names
