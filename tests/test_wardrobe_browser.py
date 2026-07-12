"""Tests for src/wardrobe_browser.py.

Covers the pure logic: name-based categorisation, the clothing/non-clothing
split (apparel-first so an apparel word always wins), price formatting, the
local-image lookup, and the frontend payload (filtering, facets, sort, stats).
The HTTP server is not exercised here.
"""

from __future__ import annotations

import os

import pytest

from src import wardrobe_browser as wb


# --------------------------------------------------------------------------
# categorize
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("Long-Sleeve Rotation T-Shirt", "longsleeve"),   # longsleeve beats tshirt
    ("Waffle-Knit T-Shirt", "tshirt"),                # "knit" is NOT a sweater signal
    ("Tapered Jogger Sweatpants", "sweatpants"),      # sweatpants beats pants
    ("The One Jogger", "sweatpants"),
    ("Fleece Colorblock Hoodie", "hoodie"),           # hoodie beats sweatshirt
    ("Printed Sweatshorts", "shorts_casual"),         # sweatshorts -> casual
    ("Cargo Shorts", "shorts_casual"),                # cargo -> casual
    ("Mesh Performance Shorts", "shorts_athletic"),   # mesh/performance -> athletic
    ("Lightweight Running Short", "shorts_athletic"), # running, singular noun
    ("Jujutsu Kaisen Anime Print Shorts", "shorts"),  # no signal -> generic shorts
    ("Short Sleeve Performance Tee", "tshirt"),       # "short sleeve" is NOT shorts
    ("Premium BodySpec Tank Top", "tank"),
    ("Slim Fit Performance Dress Shirt", "shirt"),
    ("Devil Child Tee", "tshirt"),
    ("Corduroy Pants Paisley Stitch Pants", "pants"),
    ("Embroidered Beanie", "hat"),
    ("Cow Slippers", "shoes"),
    ("Doraemon Slides", "shoes"),
    ("Kitsune", "other"),                             # design-only name
    ("USB C to C Fast Charger Cable", "other"),       # non-clothing -> other key
])
def test_categorize(name, expected):
    assert wb.categorize(name) == expected


def test_categorize_blank():
    assert wb.categorize("") == "other"
    assert wb.categorize(None) == "other"


# --------------------------------------------------------------------------
# classify_item — clothing vs non-clothing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name,is_clothing", [
    ("Egghead Manga Cover Rug", False),
    ("Throw Pillow in Monochromatic Colors", False),
    ("USB C Charger Block", False),
    ("Botesty Sound Headphones Wired with Microphone", False),  # plural caught
    ("ZTANPS 50PCS Disposable Face Masks", False),              # plural caught
    ("Micronized Creatine Powder", False),
    ("Titanium Steel Chain Necklace", False),
    ("Staples Hyken Ergonomic Swivel Task Chair", False),
    ("Govee Smart Light Bulbs", False),
    ("7 Piece RPG Set - Red Swirl", False),
    ("Cloud Duffle Bag", False),
    # Real clothing must survive even with non-clothing-ish words present:
    ("Mickey Mouse Graphic Tee", True),         # "Mouse" present, but it's a tee
    ("Sonic The Hedgehog Jeans With Belt & Chain", True),  # "Chain" present, it's jeans
    ("Texere Men's Terry Cloth Bathrobe", True),           # robe -> accessory/clothing
    ("Kitsune", True),                          # uncategorised, no non-clothing signal
    ("Turtle School (Oversize Drop-Shoulder)", True),
])
def test_classify_clothing(name, is_clothing):
    _, clothing = wb.classify_item({"item_name": name})
    assert clothing is is_clothing


def test_classify_respects_stored_is_clothing_false():
    # A stored Non-clothing flag always wins, even for an apparel-named item.
    cat, clothing = wb.classify_item({"item_name": "Graphic Tee", "is_clothing": False})
    assert cat == "tshirt"
    assert clothing is False


# --------------------------------------------------------------------------
# classify_item — stored category preference (issue #18)
# --------------------------------------------------------------------------

def test_stored_category_wins_over_name():
    # A design-only name the heuristic would bucket as "other" is shown as the
    # stored garment category instead.
    cat, clothing = wb.classify_item({"item_name": "Kitsune", "category": "tshirt"})
    assert cat == "tshirt"
    assert clothing is True


def test_stored_non_clothing_hides_item():
    cat, clothing = wb.classify_item(
        {"item_name": "3D Zip Set", "category": "non_clothing"}
    )
    assert cat == "non_clothing"
    assert clothing is False


def test_stored_category_overrides_nonclothing_name_heuristic():
    # Name trips _NONCLOTHING_RE ("bag"), but Claude stored it as accessory.
    cat, clothing = wb.classify_item(
        {"item_name": "Belt Bag Crossbody", "category": "accessory"}
    )
    assert cat == "accessory"
    assert clothing is True


def test_invalid_stored_category_falls_back_to_name():
    cat, clothing = wb.classify_item(
        {"item_name": "Tapered Jogger Sweatpants", "category": "garbage"}
    )
    assert cat == "sweatpants"
    assert clothing is True


def test_stored_garment_category_with_is_clothing_false_stays_hidden():
    # Watchlist authority: an explicit is_clothing False hides it even if the
    # stored category is a garment.
    cat, clothing = wb.classify_item(
        {"item_name": "Tee", "category": "tshirt", "is_clothing": False}
    )
    assert clothing is False


# --------------------------------------------------------------------------
# _price_display
# --------------------------------------------------------------------------

@pytest.mark.parametrize("price,expected", [
    ({"amount": 120.0, "currency": "USD"}, "$120.00"),
    ({"amount": 1200, "currency": "USD"}, "$1,200.00"),
    ({"amount": 30, "currency": "GBP"}, "£30.00"),
    ({"amount": 15.5, "currency": "JPY"}, "15.50 JPY"),  # unknown symbol
    ({"amount": None}, None),
    (None, None),
    ("nope", None),
])
def test_price_display(price, expected):
    assert wb._price_display(price) == expected


# --------------------------------------------------------------------------
# _image_url
# --------------------------------------------------------------------------

def test_image_url_none_without_dir():
    assert wb._image_url("abc123", None) is None


def test_image_url_finds_cached_file(tmp_path):
    (tmp_path / "abc123.png").write_bytes(b"x")
    url = wb._image_url("abc123", tmp_path)
    # A ?v=<mtime> cache-buster is appended so an in-place photo replace still
    # changes the URL (see _image_url's docstring); the path is otherwise stable.
    assert url.split("?", 1)[0] == "/images/abc123.png"
    assert url.startswith("/images/abc123.png?v=")
    assert wb._image_url("missing", tmp_path) is None


def test_image_url_cache_buster_changes_on_rewrite(tmp_path):
    f = tmp_path / "abc123.png"
    f.write_bytes(b"old")
    before = wb._image_url("abc123", tmp_path)
    st = f.stat()                   # bump mtime by 1s so the two URLs must differ
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    after = wb._image_url("abc123", tmp_path)
    assert before != after
    assert before.split("?", 1)[0] == after.split("?", 1)[0]


# --------------------------------------------------------------------------
# fetch_images (--fetch-images, issue #19)
# --------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status=200, content=b"IMGBYTES", ctype="image/jpeg"):
        self.status_code = status
        self.content = content
        self.headers = {"content-type": ctype} if ctype is not None else {}


class _FakeClient:
    """Maps url -> _FakeResp (or an Exception to raise). Records calls."""

    def __init__(self, responses):
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url):
        self.calls.append(url)
        r = self.responses[url]
        if isinstance(r, Exception):
            raise r
        return r

    def close(self):
        pass


def _no_sleep(_seconds):
    pass


class TestFetchImages:
    def test_downloads_named_by_content_type(self, tmp_path):
        items = [{"id": "aaa", "image_url": "https://cdn.x/a"},
                 {"id": "bbb", "image_url": "https://cdn.x/b"}]
        client = _FakeClient({
            "https://cdn.x/a": _FakeResp(ctype="image/jpeg"),
            "https://cdn.x/b": _FakeResp(ctype="image/png", content=b"PNG"),
        })
        stats = wb.fetch_images(items, tmp_path, client=client, sleep=_no_sleep)
        assert stats == {"targets": 2, "downloaded": 2, "cached": 0, "failed": 0}
        assert (tmp_path / "aaa.jpg").read_bytes() == b"IMGBYTES"
        assert (tmp_path / "bbb.png").read_bytes() == b"PNG"

    def test_skips_cached_unless_refresh(self, tmp_path):
        (tmp_path / "aaa.jpg").write_bytes(b"OLD")
        items = [{"id": "aaa", "image_url": "https://cdn.x/a"}]
        client = _FakeClient({"https://cdn.x/a": _FakeResp(content=b"NEW")})
        stats = wb.fetch_images(items, tmp_path, client=client, sleep=_no_sleep)
        assert stats["cached"] == 1
        assert client.calls == []
        assert (tmp_path / "aaa.jpg").read_bytes() == b"OLD"

        stats = wb.fetch_images(
            items, tmp_path, refresh=True, client=client, sleep=_no_sleep)
        assert stats["downloaded"] == 1
        assert (tmp_path / "aaa.jpg").read_bytes() == b"NEW"

    def test_refresh_drops_stale_other_extension(self, tmp_path):
        # A cached .jpg must not shadow a re-downloaded .png in _image_url's
        # probe order — one file per id.
        (tmp_path / "aaa.jpg").write_bytes(b"OLD")
        items = [{"id": "aaa", "image_url": "https://cdn.x/a"}]
        client = _FakeClient(
            {"https://cdn.x/a": _FakeResp(ctype="image/png", content=b"PNG")})
        wb.fetch_images(items, tmp_path, refresh=True, client=client, sleep=_no_sleep)
        assert not (tmp_path / "aaa.jpg").exists()
        assert (tmp_path / "aaa.png").read_bytes() == b"PNG"

    def test_non_image_content_type_never_written(self, tmp_path):
        # A CDN error page served as 200 text/html must not be cached as a .jpg.
        items = [{"id": "aaa", "image_url": "https://cdn.x/a.jpg"}]
        client = _FakeClient(
            {"https://cdn.x/a.jpg": _FakeResp(ctype="text/html", content=b"<html>")})
        stats = wb.fetch_images(items, tmp_path, client=client, sleep=_no_sleep)
        assert stats["failed"] == 1
        assert list(tmp_path.iterdir()) == []

    def test_missing_content_type_falls_back_to_url_extension(self, tmp_path):
        items = [{"id": "aaa", "image_url": "https://cdn.x/photo.webp?v=1"}]
        client = _FakeClient(
            {"https://cdn.x/photo.webp?v=1": _FakeResp(ctype=None, content=b"WEBP")})
        stats = wb.fetch_images(items, tmp_path, client=client, sleep=_no_sleep)
        assert stats["downloaded"] == 1
        assert (tmp_path / "aaa.webp").read_bytes() == b"WEBP"

    def test_http_error_and_exception_are_isolated(self, tmp_path):
        # One 404 and one network error must not stop the batch.
        items = [{"id": "aaa", "image_url": "https://cdn.x/a"},
                 {"id": "bbb", "image_url": "https://cdn.x/b"},
                 {"id": "ccc", "image_url": "https://cdn.x/c"}]
        client = _FakeClient({
            "https://cdn.x/a": _FakeResp(status=404),
            "https://cdn.x/b": RuntimeError("boom"),
            "https://cdn.x/c": _FakeResp(content=b"OK"),
        })
        stats = wb.fetch_images(items, tmp_path, client=client, sleep=_no_sleep)
        assert stats == {"targets": 3, "downloaded": 1, "cached": 0, "failed": 2}
        assert (tmp_path / "ccc.jpg").read_bytes() == b"OK"

    def test_items_without_image_url_ignored(self, tmp_path):
        target = tmp_path / "imgcache"
        items = [{"id": "aaa"}, {"id": "bbb", "image_url": "  "},
                 {"image_url": "https://cdn.x/orphan.jpg"}]  # no id
        stats = wb.fetch_images(items, target, client=_FakeClient({}), sleep=_no_sleep)
        assert stats == {"targets": 0, "downloaded": 0, "cached": 0, "failed": 0}
        # No targets → the cache dir isn't even created.
        assert not target.exists()

    def test_amazon_thumbnail_upgraded_to_full_size(self, tmp_path):
        # The 90px email thumbnail is re-requested at _SL600_; the original is
        # only the fallback.
        small = "https://m.media-amazon.com/images/I/61z67o0urxL._SS90_.jpg"
        big = "https://m.media-amazon.com/images/I/61z67o0urxL._SL600_.jpg"
        items = [{"id": "aaa", "image_url": small}]
        client = _FakeClient({big: _FakeResp(content=b"BIG")})
        stats = wb.fetch_images(items, tmp_path, client=client, sleep=_no_sleep)
        assert stats["downloaded"] == 1
        assert client.calls == [big]
        assert (tmp_path / "aaa.jpg").read_bytes() == b"BIG"

    def test_upgrade_miss_falls_back_to_stored_url(self, tmp_path):
        small = "https://m.media-amazon.com/images/I/61z67o0urxL._SS90_.jpg"
        big = "https://m.media-amazon.com/images/I/61z67o0urxL._SL600_.jpg"
        items = [{"id": "aaa", "image_url": small}]
        client = _FakeClient({big: _FakeResp(status=404),
                              small: _FakeResp(content=b"SMALL")})
        stats = wb.fetch_images(items, tmp_path, client=client, sleep=_no_sleep)
        assert stats["downloaded"] == 1
        assert client.calls == [big, small]
        assert (tmp_path / "aaa.jpg").read_bytes() == b"SMALL"


def test_upgraded_image_url_only_rewrites_amazon():
    small = "https://m.media-amazon.com/images/I/61z67o0urxL._SS90_.jpg"
    assert wb._upgraded_image_url(small) == (
        "https://m.media-amazon.com/images/I/61z67o0urxL._SL600_.jpg")
    # No size token → nothing to upgrade.
    assert wb._upgraded_image_url(
        "https://m.media-amazon.com/images/I/61z67o0urxL.jpg") is None
    # Non-Amazon hosts are never rewritten.
    assert wb._upgraded_image_url(
        "https://cdn.shopify.com/a/tee._SS90_.jpg") is None


# --------------------------------------------------------------------------
# product_link
# --------------------------------------------------------------------------

def test_product_link_prefers_stored_product_url():
    pl = wb.product_link({
        "product_url": "https://xsekai.com/products/sukuna-tee",
        "item_name": "Sukuna Tee", "shop": "XSekai",
        "watchlist_match": {"matched_line": "https://other.com/x"},
    })
    assert pl == {"href": "https://xsekai.com/products/sukuna-tee", "kind": "product"}


def test_product_link_falls_back_to_watchlist_url():
    pl = wb.product_link({
        "item_name": "Raijin", "shop": "Bosuman",
        "watchlist_match": {"matched_line": "Raijin https://bosuman.com/products/raijin"},
    })
    assert pl == {"href": "https://bosuman.com/products/raijin", "kind": "watchlist"}


def test_product_link_strips_trailing_punctuation_from_watchlist_url():
    pl = wb.product_link({
        "item_name": "Tee", "shop": "Shop",
        "watchlist_match": {"matched_line": "(https://shop.com/products/tee)."},
    })
    assert pl["href"] == "https://shop.com/products/tee"
    assert pl["kind"] == "watchlist"


def test_product_link_search_when_no_url():
    pl = wb.product_link({"item_name": "Vintage Hoodie", "shop": "Pomel"})
    assert pl["kind"] == "search"
    assert pl["href"].startswith("https://www.google.com/search?q=")
    # Quoted item name + shop, URL-encoded.
    assert "Vintage+Hoodie" in pl["href"]
    assert "Pomel" in pl["href"]
    assert "%22" in pl["href"]  # the quotes around the item name


def test_product_link_search_shop_only_when_unnamed():
    pl = wb.product_link({"item_name": "", "shop": "Pomel"})
    assert pl["kind"] == "search"
    assert "Pomel" in pl["href"]
    assert "%22" not in pl["href"]  # no empty quoted phrase


def test_product_link_none_without_url_or_text():
    assert wb.product_link({"item_name": "", "shop": ""}) is None


def test_product_link_ignores_non_http_product_url():
    # A malformed stored value must not be emitted as a direct link.
    pl = wb.product_link({"product_url": "shop.com/x", "item_name": "Tee", "shop": "Shop"})
    assert pl["kind"] == "search"


def test_build_payload_includes_product_link():
    p = wb.build_payload({"items": [
        {"id": "1", "item_name": "Tee", "shop": "Shop", "purchased_at": "2026-01-01"},
    ]})
    assert p["items"][0]["product_link"]["kind"] == "search"


# --------------------------------------------------------------------------
# build_payload
# --------------------------------------------------------------------------

def _wardrobe():
    return {"items": [
        {"id": "a", "item_name": "Graphic Tee", "shop": "Sumie", "shop_domain": "sumie.com",
         "color": "White", "size": "XL", "qty": 1,
         "price_paid": {"amount": 30.0, "currency": "USD"}, "purchased_at": "2026-06-11"},
        {"id": "b", "item_name": "Tapered Jogger Sweatpants", "shop": "Old Navy",
         "color": "Navy", "size": "M", "qty": 2,
         "price_paid": {"amount": 25.0, "currency": "USD"}, "purchased_at": "2025-01-15"},
        {"id": "c", "item_name": "Devil Child Tee", "shop": "Sumie",
         "color": None, "size": "L", "purchased_at": "2026-06-10",
         "price_paid": {"amount": 28.0, "currency": "USD"}},
        {"id": "d", "item_name": "Egghead Manga Cover Rug", "shop": "Rugz",
         "purchased_at": "2024-03-01", "price_paid": {"amount": 50.0, "currency": "USD"}},
        {"id": "e", "item_name": "Kitsune", "shop": "Bosuman", "purchased_at": "2026-02-02"},
    ]}


def test_build_payload_hides_non_clothing():
    p = wb.build_payload(_wardrobe())
    ids = {i["id"] for i in p["items"]}
    assert "d" not in ids                       # rug hidden
    assert ids == {"a", "b", "c", "e"}
    assert p["stats"]["hidden_non_clothing"] == 1
    assert p["stats"]["total"] == 4


def test_build_payload_sorts_newest_first():
    p = wb.build_payload(_wardrobe())
    dates = [i["purchased_at"] for i in p["items"]]
    assert dates == sorted(dates, reverse=True)
    assert p["items"][0]["id"] == "a"  # 2026-06-11


def test_build_payload_categories_facet():
    p = wb.build_payload(_wardrobe())
    cats = {c["key"]: c["count"] for c in p["categories"]}
    assert cats["tshirt"] == 2
    assert cats["sweatpants"] == 1
    assert cats["other"] == 1
    # Facet order follows the canonical CATEGORY_ORDER.
    keys = [c["key"] for c in p["categories"]]
    assert keys == sorted(keys, key=wb.CATEGORY_ORDER.index)


def test_build_payload_shops_facet_sorted_by_count():
    p = wb.build_payload(_wardrobe())
    assert p["shops"][0] == {"name": "Sumie", "count": 2}
    assert {s["name"] for s in p["shops"]} == {"Sumie", "Old Navy", "Bosuman"}


# --------------------------------------------------------------------------
# brand canonicalisation (merge name variants of one brand)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("SORA Clothing", "soraclothing"),
    ("Soraclothing", "soraclothing"),
    ("sora-clothing", "soraclothing"),
    ("FuHa!", "fuha"),
    ("100Moons", "100moons"),
    ("  Old   Navy ", "oldnavy"),
    ("", ""),
    (None, ""),
])
def test_brand_key_normalises(name, expected):
    assert wb.brand_key(name) == expected


def test_canonical_brand_name_prefers_spaced_proper_name():
    from collections import Counter
    # The domain slug "Soraclothing" appears once; the display name "SORA
    # Clothing" appears more often AND is spaced — it wins.
    counts = Counter({"SORA Clothing": 3, "Soraclothing": 1})
    assert wb._canonical_brand_name(counts) == "SORA Clothing"


def test_canonical_brand_name_spaced_beats_more_frequent_slug():
    from collections import Counter
    # Even when the squashed slug is more frequent, a spaced proper name reads
    # better as the brand label.
    counts = Counter({"Soraclothing": 5, "SORA Clothing": 1})
    assert wb._canonical_brand_name(counts) == "SORA Clothing"


def test_build_payload_merges_brand_name_variants():
    # "SORA Clothing" (display name) and "Soraclothing" (domain slug) are one
    # brand and must merge into a single facet entry + group.
    w = {"items": [
        {"id": "a", "item_name": "Tee", "shop": "SORA Clothing", "category": "tshirt",
         "purchased_at": "2026-06-01", "price_paid": {"amount": 20.0, "currency": "USD"}},
        {"id": "b", "item_name": "Hoodie", "shop": "SORA Clothing", "category": "hoodie",
         "purchased_at": "2026-05-01", "price_paid": {"amount": 40.0, "currency": "USD"}},
        {"id": "c", "item_name": "Cap", "shop": "Soraclothing", "category": "hat",
         "purchased_at": "2026-04-01", "price_paid": {"amount": 15.0, "currency": "USD"}},
    ]}
    p = wb.build_payload(w)
    # One merged brand, count 3, under the nicer display name.
    assert p["shops"] == [{"name": "SORA Clothing", "count": 3}]
    assert p["stats"]["shop_count"] == 1
    # Every item now displays the canonical brand name.
    assert {i["shop"] for i in p["items"]} == {"SORA Clothing"}


def test_build_payload_tags_items_with_brand_key():
    # Each item carries the normalised merge key so the frontend can match a
    # detected review-request email (tagged with the same key) to its shop.
    w = {"items": [
        {"id": "a", "item_name": "Tee", "shop": "SORA Clothing", "category": "tshirt",
         "purchased_at": "2026-06-01"},
        {"id": "b", "item_name": "Cap", "shop": "Soraclothing", "category": "hat",
         "purchased_at": "2026-05-01"},
    ]}
    items = wb.build_payload(w)["items"]
    # Both spellings merge to one brand_key even though they're one brand now.
    assert {i["brand_key"] for i in items} == {"soraclothing"}
    for i in items:
        assert i["brand_key"] == wb.brand_key(i["shop"])


def test_build_payload_does_not_over_merge_distinct_brands():
    # Brands whose normalised keys differ stay separate.
    w = {"items": [
        {"id": "a", "item_name": "Tee", "shop": "Toka", "category": "tshirt",
         "purchased_at": "2026-06-01"},
        {"id": "b", "item_name": "Tee", "shop": "Pomel", "category": "tshirt",
         "purchased_at": "2026-06-01"},
    ]}
    p = wb.build_payload(w)
    assert {s["name"] for s in p["shops"]} == {"Toka", "Pomel"}


def test_build_payload_stats_spend_and_dates():
    p = wb.build_payload(_wardrobe())
    s = p["stats"]
    # 30 + 25*2 + 28 = 108 (rug excluded)
    assert s["total_spent"]["USD"] == 108.0
    assert s["date_min"] == "2025-01-15"
    assert s["date_max"] == "2026-06-11"
    assert s["shop_count"] == 3


def test_build_payload_spend_by_category():
    s = wb.build_payload(_wardrobe())["stats"]
    # tee a (30) + tee c (28) = 58; sweatpants b (25*2) = 50.
    assert s["spent_by_category"]["tshirt"] == {"USD": 58.0}
    assert s["spent_by_category"]["sweatpants"] == {"USD": 50.0}
    # Kitsune ("other") has no price, so it never creates a spend bucket.
    assert "other" not in s["spent_by_category"]
    # Category totals reconcile with the headline total_spent.
    by_cat = sum(m["USD"] for m in s["spent_by_category"].values())
    assert by_cat == s["total_spent"]["USD"]


def test_build_payload_spend_by_month():
    s = wb.build_payload(_wardrobe())["stats"]
    assert s["spent_by_month"]["2026-06"] == {"USD": 58.0}
    assert s["spent_by_month"]["2025-01"] == {"USD": 50.0}
    by_month = sum(m["USD"] for m in s["spent_by_month"].values())
    assert by_month == s["total_spent"]["USD"]


def test_build_payload_spend_keeps_currencies_separate():
    w = {"items": [
        {"id": "a", "item_name": "Tee", "shop": "S", "category": "tshirt",
         "purchased_at": "2026-06-01", "price_paid": {"amount": 20.0, "currency": "USD"}},
        {"id": "b", "item_name": "Tee", "shop": "S", "category": "tshirt",
         "purchased_at": "2026-06-01", "price_paid": {"amount": 10.0, "currency": "GBP"}},
    ]}
    s = wb.build_payload(w)["stats"]
    # Same category + month, two currencies — never summed together.
    assert s["spent_by_category"]["tshirt"] == {"USD": 20.0, "GBP": 10.0}
    assert s["spent_by_month"]["2026-06"] == {"USD": 20.0, "GBP": 10.0}


def test_build_payload_normalises_item_fields():
    p = wb.build_payload(_wardrobe())
    tee = next(i for i in p["items"] if i["id"] == "a")
    assert tee["category"] == "tshirt"
    assert tee["category_label"] == "T-Shirts"
    assert tee["price_display"] == "$30.00"
    assert tee["year"] == "2026"
    assert tee["month"] == "2026-06"
    assert tee["image"] is None


def test_build_payload_empty():
    p = wb.build_payload({})
    assert p["items"] == []
    assert p["stats"]["total"] == 0
    assert p["categories"] == []
    assert p["shops"] == []


def test_build_payload_includes_detail_fields():
    w = {"items": [{
        "id": "a", "item_name": "Aros Chino", "shop": "Norse", "category": "pants",
        "purchased_at": "2026-04-15", "shipped_at": "2026-04-18",
        "tracking_url": "https://ups.com/x", "order_email_id": "12345",
        "fit_review": {"fit": "tts", "notes": "great", "reviewed_at": "2026-05-01T00:00:00Z"},
        "body_comp": {"weight_kg": 75.2, "body_fat_pct": 18.4, "lean_mass_kg": 58.0,
                      "fat_mass_kg": 14.1, "scan_date": "2026-04-10", "matched_to": "purchase",
                      "days_from_event": -5, "regions": {"trunk": {}}},
    }]}
    it = wb.build_payload(w)["items"][0]
    assert it["shipped_at"] == "2026-04-18"
    assert it["tracking_url"] == "https://ups.com/x"
    assert it["order_email_id"] == "12345"
    assert it["fit_review"]["fit"] == "tts"
    assert it["fit_pending"] is False
    # body_comp is summarised (no per-region blob in the payload).
    assert it["body_comp"]["weight_kg"] == 75.2
    assert "regions" not in it["body_comp"]


def test_build_payload_fit_pending_flag():
    w = {"items": [{"id": "a", "item_name": "Tee", "shop": "S", "category": "tshirt",
                    "purchased_at": "2026-04-15"}]}
    assert wb.build_payload(w)["items"][0]["fit_pending"] is True


# --------------------------------------------------------------------------
# apply_category_edit
# --------------------------------------------------------------------------

def test_apply_category_edit_sets_garment():
    w = {"items": [{"id": "a", "item_name": "Kitsune", "category": "tshirt"}]}
    item = wb.apply_category_edit(w, "a", "hoodie")
    assert item["category"] == "hoodie"
    assert "is_clothing" not in item


def test_apply_category_edit_non_clothing_hides():
    w = {"items": [{"id": "a", "item_name": "Mug", "category": "other"}]}
    item = wb.apply_category_edit(w, "a", "non_clothing")
    assert item["category"] == "non_clothing"
    assert item["is_clothing"] is False


def test_apply_category_edit_garment_clears_prior_false():
    # Re-categorising a hidden item to a garment re-shows it.
    w = {"items": [{"id": "a", "item_name": "Tee", "category": "non_clothing",
                    "is_clothing": False}]}
    item = wb.apply_category_edit(w, "a", "tshirt")
    assert item["category"] == "tshirt"
    assert "is_clothing" not in item


def test_apply_category_edit_unknown_raises():
    w = {"items": [{"id": "a", "item_name": "X"}]}
    with pytest.raises(ValueError):
        wb.apply_category_edit(w, "a", "garbage")


def test_apply_category_edit_missing_id_returns_none():
    w = {"items": [{"id": "a", "item_name": "X"}]}
    assert wb.apply_category_edit(w, "nope", "tshirt") is None


# --------------------------------------------------------------------------
# _Catalogue write paths
# --------------------------------------------------------------------------

class TestCatalogueWrites:
    def _state(self):
        return {
            "wardrobe": {"items": [
                {"id": "a", "item_name": "Kitsune", "shop": "Bosuman", "category": "tshirt",
                 "purchased_at": "2026-04-15"},
            ]},
            "prices": {}, "aliases": {}, "codes": [],
        }

    def test_edit_category_writes_and_returns_payload(self, monkeypatch):
        st = self._state()
        written = {}
        monkeypatch.setattr(wb.state, "read_state", lambda g, t, **k: st)
        monkeypatch.setattr(wb.state, "write_state",
                            lambda g, t, **kw: written.update(kw))
        cat = wb._Catalogue("g", "t", None)
        payload = cat.edit_category("a", "hoodie")
        assert written["wardrobe"]["items"][0]["category"] == "hoodie"
        assert payload["items"][0]["category"] == "hoodie"

    def test_edit_category_unknown_id_raises_keyerror(self, monkeypatch):
        monkeypatch.setattr(wb.state, "read_state", lambda g, t, **k: self._state())
        monkeypatch.setattr(wb.state, "write_state", lambda *a, **k: None)
        cat = wb._Catalogue("g", "t", None)
        with pytest.raises(KeyError):
            cat.edit_category("missing", "hoodie")

    def test_submit_fit_unconfigured_raises(self):
        cat = wb._Catalogue("g", "t", None)  # no fit secrets
        assert cat.fit_enabled is False
        with pytest.raises(RuntimeError, match="not configured"):
            cat.submit_fit({"id": "a", "fit": "tts"})

    def test_submit_fit_forwards_signed_and_refreshes(self, monkeypatch):
        import httpx
        captured = {}

        class _Resp:
            def raise_for_status(self): pass
            def json(self): return {"ok": True, "item_name": "Kitsune"}

        def fake_post(url, json=None, **kw):
            captured["url"] = url
            captured["json"] = json
            return _Resp()

        monkeypatch.setattr(httpx, "post", fake_post)
        monkeypatch.setattr(wb, "fetch_wardrobe",
                            lambda g, t: self._state()["wardrobe"])
        cat = wb._Catalogue("g", "t", None,
                            fit_form_base_url="https://script/exec",
                            fit_link_secret="sekret")
        payload = cat.submit_fit({"id": "a", "fit": "tts", "notes": "good"})
        body = captured["json"]
        assert body["action"] == "fit"
        assert body["item"] == "a"
        assert "id" not in body
        # Signature matches fit_links.sign(item_id, secret).
        from src import fit_links
        assert body["sig"] == fit_links.sign("a", "sekret")
        assert payload["items"][0]["id"] == "a"

    def test_submit_fit_rejected_raises(self, monkeypatch):
        import httpx

        class _Resp:
            def raise_for_status(self): pass
            def json(self): return {"ok": False, "error": "bad fit value"}

        monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
        cat = wb._Catalogue("g", "t", None,
                            fit_form_base_url="https://script/exec",
                            fit_link_secret="sekret")
        with pytest.raises(RuntimeError, match="bad fit value"):
            cat.submit_fit({"id": "a", "fit": "tts"})


# --------------------------------------------------------------------------
# _review_request_days
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("30", 30), ("7", 7), ("", 30), (None, 30), ("nope", 30),
    ("0", 30), ("-5", 30), ("  14 ", 14),
])
def test_review_request_days(raw, expected):
    assert wb._review_request_days(raw) == expected


# --------------------------------------------------------------------------
# _Catalogue.review_requests — detect open review-request emails
# --------------------------------------------------------------------------

class TestReviewRequests:
    # Synthetic emails shaped like gmail._parse_message output. Both pass
    # review_requests.is_review_request (subject phrases), distinct shops.
    def _emails(self):
        return [
            {"id": "100", "from": "Acme Co <no-reply@acme.com>",
             "subject": "How did we do?", "body_text": "Order #1234 placed",
             "date": "Mon, 16 Jun 2026 10:00:00 +0000", "message_id": "<m1@acme>"},
            {"id": "101", "from": "Pomel <hi@loox.io>",
             "subject": "Leave a review of your order",
             "body_text": "thanks for shopping",
             "date": "Sun, 15 Jun 2026 09:00:00 +0000", "message_id": "<m2@pomel>"},
        ]

    def test_disabled_without_gmail_creds(self):
        cat = wb._Catalogue("g", "t", None)  # no GMAIL_* creds
        assert cat.review_requests_enabled is False
        out = cat.review_requests()
        assert out["enabled"] is False
        assert out["requests"] == []
        assert out["all_url"]   # all-time Gmail link always present

    def test_fetches_dedupes_and_tags_brand_key(self, monkeypatch):
        calls = {"n": 0}

        def fake_fetch(user, pw, *, days=30, **kw):
            calls["n"] += 1
            return self._emails()

        monkeypatch.setattr("src.gmail.fetch_review_requests", fake_fetch)
        cat = wb._Catalogue("g", "t", None,
                            gmail_username="me@gmail.com", gmail_app_password="pw")
        assert cat.review_requests_enabled is True
        out = cat.review_requests()
        assert out["enabled"] is True
        shops = {r["shop"]: r["brand_key"] for r in out["requests"]}
        assert shops == {"Acme Co": "acmeco", "Pomel": "pomel"}
        # Each entry carries the digest's render fields + the new brand_key.
        first = out["requests"][0]
        assert {"shop", "subject", "date_iso", "days_ago", "url", "brand_key"} <= set(first)
        assert calls["n"] == 1
        # Second call within the TTL is served from cache (no re-fetch).
        cat.review_requests()
        assert calls["n"] == 1
        # force=True bypasses the cache.
        cat.review_requests(force=True)
        assert calls["n"] == 2

    def test_refresh_invalidates_cache(self, monkeypatch):
        calls = {"n": 0}
        monkeypatch.setattr("src.gmail.fetch_review_requests",
                            lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1)
                                             or self._emails()))
        monkeypatch.setattr(wb, "fetch_wardrobe", lambda g, t: {"items": []})
        cat = wb._Catalogue("g", "t", None,
                            gmail_username="me@gmail.com", gmail_app_password="pw")
        cat.review_requests()
        assert calls["n"] == 1
        cat.refresh()                 # clears the review-request cache
        cat.review_requests()
        assert calls["n"] == 2

    def test_failure_isolated(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("imap down")

        monkeypatch.setattr("src.gmail.fetch_review_requests", boom)
        cat = wb._Catalogue("g", "t", None,
                            gmail_username="me@gmail.com", gmail_app_password="pw")
        out = cat.review_requests()
        assert out["enabled"] is True
        assert out["requests"] == []
        assert "imap down" in out["error"]


def test_review_requests_route(monkeypatch):
    """GET /api/review-requests and /api/meta over a live server."""
    import threading
    import httpx
    from http.server import ThreadingHTTPServer

    monkeypatch.setattr(
        "src.gmail.fetch_review_requests",
        lambda *a, **k: [{
            "id": "1", "from": "Acme Co <no-reply@acme.com>",
            "subject": "How did we do?", "body_text": "Order #1234",
            "date": "Mon, 16 Jun 2026 10:00:00 +0000", "message_id": "<m@a>"}],
    )
    cat = wb._Catalogue("g", "t", None,
                        gmail_username="me@gmail.com", gmail_app_password="pw")
    cat._store({"items": [], "categories": [], "shops": [], "stats": {}})
    server = ThreadingHTTPServer(("127.0.0.1", 0), wb._make_handler(cat))
    port = server.server_address[1]
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        rr = httpx.get(f"http://127.0.0.1:{port}/api/review-requests", timeout=5.0).json()
        assert rr["enabled"] is True
        assert rr["requests"][0]["brand_key"] == "acmeco"
        meta = httpx.get(f"http://127.0.0.1:{port}/api/meta", timeout=5.0).json()
        assert meta["review_requests_enabled"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_category_choices_cover_taxonomy():
    keys = {c["key"] for c in wb.CATEGORY_CHOICES}
    assert keys == set(wb.CATEGORY_ORDER)
    assert all(c["label"] for c in wb.CATEGORY_CHOICES)


def test_shutdown_route_stops_server():
    """POST /api/shutdown returns {ok:true} and stops the server on its own.

    A real round-trip against a live ThreadingHTTPServer — exercises the
    deadlock-safe pattern (shutdown() runs on a daemon thread, off the serving
    thread) rather than stubbing it, so a regression that called shutdown()
    inline (and hung) would fail here.
    """
    import threading
    import httpx
    from http.server import ThreadingHTTPServer

    cat = wb._Catalogue("g", "t", None)
    cat._store({"items": [], "categories": [], "shops": [], "stats": {}})
    server = ThreadingHTTPServer(("127.0.0.1", 0), wb._make_handler(cat))
    port = server.server_address[1]
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        resp = httpx.post(f"http://127.0.0.1:{port}/api/shutdown", timeout=5.0)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        serving.join(timeout=5.0)
        assert not serving.is_alive()
    finally:
        server.server_close()


# --------------------------------------------------------------------------
# extract_page_image (issue #30 — manual paste-back, phase B)
# --------------------------------------------------------------------------

def test_extract_page_image_og_meta():
    html = '<html><head><meta property="og:image" content="https://cdn.x/tee.jpg"></head></html>'
    assert wb.extract_page_image(html) == "https://cdn.x/tee.jpg"


def test_extract_page_image_attribute_order_and_quotes():
    # Reversed attribute order, single quotes, self-closing.
    html = "<meta content='https://cdn.x/a.png' property='og:image'/>"
    assert wb.extract_page_image(html) == "https://cdn.x/a.png"


def test_extract_page_image_name_attr_and_entities():
    html = '<meta name="og:image" content="https://cdn.x/a.jpg?w=600&amp;h=600">'
    assert wb.extract_page_image(html) == "https://cdn.x/a.jpg?w=600&h=600"


def test_extract_page_image_og_wins_over_jsonld():
    html = (
        '<script type="application/ld+json">{"image": "https://cdn.x/ld.jpg"}</script>'
        '<meta property="og:image" content="https://cdn.x/og.jpg">'
    )
    assert wb.extract_page_image(html) == "https://cdn.x/og.jpg"


@pytest.mark.parametrize("block", [
    '{"@type": "Product", "image": "https://cdn.x/ld.jpg"}',
    '{"@type": "Product", "image": ["https://cdn.x/ld.jpg", "https://cdn.x/2.jpg"]}',
    '{"@type": "Product", "image": {"@type": "ImageObject", "url": "https://cdn.x/ld.jpg"}}',
    '{"@type": "Product", "image": {"contentUrl": "https://cdn.x/ld.jpg"}}',
    '{"@graph": [{"@type": "Product", "image": "https://cdn.x/ld.jpg"}]}',
])
def test_extract_page_image_jsonld_shapes(block):
    html = f'<script type="application/ld+json">{block}</script>'
    assert wb.extract_page_image(html) == "https://cdn.x/ld.jpg"


def test_extract_page_image_tolerates_bad_jsonld():
    html = (
        '<script type="application/ld+json">not json {{</script>'
        '<script type="application/ld+json">{"image": "https://cdn.x/ok.jpg"}</script>'
    )
    assert wb.extract_page_image(html) == "https://cdn.x/ok.jpg"


def test_extract_page_image_none_when_absent():
    assert wb.extract_page_image("<html><body>plain page</body></html>") is None
    assert wb.extract_page_image("") is None


# --------------------------------------------------------------------------
# apply_image_edit
# --------------------------------------------------------------------------

def _image_edit_wardrobe(**extra):
    return {"items": [
        {"id": "a", "item_name": "Kitsune Tee", "shop": "Sumie",
         "category": "tshirt", "purchased_at": "2026-04-15", **extra},
    ]}


def test_apply_image_edit_stamps_image_url():
    w = _image_edit_wardrobe()
    item = wb.apply_image_edit(w, "a", "https://cdn.x/tee.jpg")
    assert item["image_url"] == "https://cdn.x/tee.jpg"
    assert "product_url" not in item


def test_apply_image_edit_donates_product_url_when_missing():
    w = _image_edit_wardrobe()
    item = wb.apply_image_edit(
        w, "a", "https://cdn.x/t.jpg", product_url="https://shop.x/products/tee")
    assert item["product_url"] == "https://shop.x/products/tee"


def test_apply_image_edit_never_overwrites_product_url():
    w = _image_edit_wardrobe(product_url="https://shop.x/products/orig")
    item = wb.apply_image_edit(
        w, "a", "https://cdn.x/t.jpg", product_url="https://shop.x/products/new")
    assert item["product_url"] == "https://shop.x/products/orig"


def test_apply_image_edit_unknown_id_returns_none():
    assert wb.apply_image_edit(_image_edit_wardrobe(), "zzz", "https://x/t.jpg") is None


# --------------------------------------------------------------------------
# _Catalogue.add_image (paste a product-page / direct image URL)
# --------------------------------------------------------------------------

class _CMClient(_FakeClient):
    """_FakeClient variant for add_image: context-manager + headers kwarg."""

    def get(self, url, **kw):
        return super().get(url)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _PageResp:
    """A fake product-page response (the og:image extraction input)."""

    def __init__(self, text, status=200, url="https://shop.x/products/tee"):
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers = {"content-type": "text/html"}
        self.url = url


class TestAddImage:
    def _state(self, **extra):
        return {
            "wardrobe": {"items": [
                {"id": "a", "item_name": "Kitsune Tee", "shop": "Sumie",
                 "category": "tshirt", "purchased_at": "2026-04-15", **extra},
            ]},
            "prices": {}, "aliases": {}, "codes": [],
        }

    def _cat(self, tmp_path, monkeypatch, client, state=None):
        import httpx
        st = state or self._state()
        written = {}
        monkeypatch.setattr(wb.state, "read_state", lambda g, t, **k: st)
        monkeypatch.setattr(wb.state, "write_state",
                            lambda g, t, **kw: written.update(kw))
        monkeypatch.setattr(httpx, "Client", lambda **kw: client)
        return wb._Catalogue("g", "t", tmp_path), written

    def test_direct_image_url_downloads_and_stamps(self, tmp_path, monkeypatch):
        client = _CMClient({"https://cdn.x/tee.jpg": _FakeResp(content=b"IMG")})
        cat, written = self._cat(tmp_path, monkeypatch, client)
        payload = cat.add_image({"id": "a", "image_url": "https://cdn.x/tee.jpg"})
        assert (tmp_path / "a.jpg").read_bytes() == b"IMG"
        item = written["wardrobe"]["items"][0]
        assert item["image_url"] == "https://cdn.x/tee.jpg"
        assert "product_url" not in item          # direct path donates nothing
        assert payload["items"][0]["image"].split("?", 1)[0] == "/images/a.jpg"

    def test_page_url_extracts_og_image_and_donates_product_url(
            self, tmp_path, monkeypatch):
        page = '<meta property="og:image" content="https://cdn.x/tee.jpg">'
        client = _CMClient({
            "https://shop.x/products/tee?utm_source=news": _PageResp(page),
            "https://cdn.x/tee.jpg": _FakeResp(content=b"IMG"),
        })
        cat, written = self._cat(tmp_path, monkeypatch, client)
        cat.add_image({"id": "a",
                       "page_url": "https://shop.x/products/tee?utm_source=news"})
        item = written["wardrobe"]["items"][0]
        assert item["image_url"] == "https://cdn.x/tee.jpg"
        # product_url donated, cleaned of tracking params (order_scan's cleaner).
        assert item["product_url"] == "https://shop.x/products/tee"
        assert (tmp_path / "a.jpg").read_bytes() == b"IMG"

    def test_page_url_keeps_existing_product_url(self, tmp_path, monkeypatch):
        page = '<meta property="og:image" content="https://cdn.x/tee.jpg">'
        client = _CMClient({
            "https://shop.x/products/tee": _PageResp(page),
            "https://cdn.x/tee.jpg": _FakeResp(content=b"IMG"),
        })
        st = self._state(product_url="https://shop.x/products/orig")
        cat, written = self._cat(tmp_path, monkeypatch, client, state=st)
        cat.add_image({"id": "a", "page_url": "https://shop.x/products/tee"})
        assert written["wardrobe"]["items"][0]["product_url"] == \
            "https://shop.x/products/orig"

    def test_relative_og_image_resolved_against_final_page_url(
            self, tmp_path, monkeypatch):
        page = '<meta property="og:image" content="/cdn/tee.jpg">'
        client = _CMClient({
            "https://shop.x/products/tee": _PageResp(page),
            "https://shop.x/cdn/tee.jpg": _FakeResp(content=b"IMG"),
        })
        cat, written = self._cat(tmp_path, monkeypatch, client)
        cat.add_image({"id": "a", "page_url": "https://shop.x/products/tee"})
        assert written["wardrobe"]["items"][0]["image_url"] == \
            "https://shop.x/cdn/tee.jpg"

    def test_heic_image_url_converted_before_download(self, tmp_path, monkeypatch):
        # Chrome can't decode HEIC; the Shopify CDN converts on request — the
        # converted URL is both fetched and stamped (order_scan._heic_safe).
        heic = "https://cdn.shopify.com/s/files/1/tee.heic"
        converted = heic + "?format=pjpg"
        client = _CMClient({converted: _FakeResp(content=b"IMG")})
        cat, written = self._cat(tmp_path, monkeypatch, client)
        cat.add_image({"id": "a", "image_url": heic})
        assert client.calls == [converted]
        assert written["wardrobe"]["items"][0]["image_url"] == converted

    def test_no_image_on_page_raises_and_writes_nothing(self, tmp_path, monkeypatch):
        client = _CMClient(
            {"https://shop.x/products/tee": _PageResp("<html>no og here</html>")})
        cat, written = self._cat(tmp_path, monkeypatch, client)
        with pytest.raises(ValueError, match="no product image"):
            cat.add_image({"id": "a", "page_url": "https://shop.x/products/tee"})
        assert written == {}
        assert list(tmp_path.iterdir()) == []

    def test_page_fetch_error_status_raises(self, tmp_path, monkeypatch):
        client = _CMClient(
            {"https://shop.x/products/tee": _PageResp("blocked", status=403)})
        cat, written = self._cat(tmp_path, monkeypatch, client)
        with pytest.raises(ValueError, match="HTTP 403"):
            cat.add_image({"id": "a", "page_url": "https://shop.x/products/tee"})
        assert written == {}

    def test_non_image_download_raises_and_writes_nothing(self, tmp_path, monkeypatch):
        client = _CMClient(
            {"https://cdn.x/err": _FakeResp(ctype="text/html", content=b"<html>")})
        cat, written = self._cat(tmp_path, monkeypatch, client)
        with pytest.raises(ValueError, match="didn't return an image"):
            cat.add_image({"id": "a", "image_url": "https://cdn.x/err"})
        assert written == {}
        assert list(tmp_path.iterdir()) == []

    def test_unknown_id_cleans_up_cache_file(self, tmp_path, monkeypatch):
        client = _CMClient({"https://cdn.x/tee.jpg": _FakeResp(content=b"IMG")})
        cat, written = self._cat(tmp_path, monkeypatch, client)
        with pytest.raises(KeyError):
            cat.add_image({"id": "zzz", "image_url": "https://cdn.x/tee.jpg"})
        assert written == {}
        assert not (tmp_path / "zzz.jpg").exists()

    def test_rejects_missing_or_non_http_input(self, tmp_path, monkeypatch):
        cat, _ = self._cat(tmp_path, monkeypatch, _CMClient({}))
        with pytest.raises(ValueError, match="paste"):
            cat.add_image({"id": "a"})
        with pytest.raises(ValueError, match="http"):
            cat.add_image({"id": "a", "image_url": "ftp://x/y.jpg"})
        with pytest.raises(ValueError, match="missing item id"):
            cat.add_image({"id": "", "image_url": "https://x/y.jpg"})


# --------------------------------------------------------------------------
# _Catalogue.upload_image (file upload — cache-only, zero Gist writes)
# --------------------------------------------------------------------------

class TestUploadImage:
    def _cat(self, tmp_path):
        cat = wb._Catalogue("g", "t", tmp_path)
        wardrobe = {"items": [
            {"id": "a", "item_name": "Kitsune Tee", "shop": "Sumie",
             "category": "tshirt", "purchased_at": "2026-04-15"},
        ]}
        cat._store(wb.build_payload(wardrobe, tmp_path), wardrobe)
        return cat

    def test_upload_writes_cache_and_rebuilds_payload(self, tmp_path):
        # No state.read_state/write_state monkeypatch on purpose: the upload
        # path must never touch the Gist (it would blow up on fake creds).
        payload = self._cat(tmp_path).upload_image("a", "photo.png", "image/png", b"PNG")
        assert (tmp_path / "a.png").read_bytes() == b"PNG"
        assert payload["items"][0]["image"].split("?", 1)[0] == "/images/a.png"

    def test_upload_ext_from_filename_when_ctype_unhelpful(self, tmp_path):
        self._cat(tmp_path).upload_image(
            "a", "photo.webp", "application/octet-stream", b"WEBP")
        assert (tmp_path / "a.webp").read_bytes() == b"WEBP"

    def test_upload_replaces_other_extension_cache(self, tmp_path):
        (tmp_path / "a.jpg").write_bytes(b"OLD")
        self._cat(tmp_path).upload_image("a", "new.png", "image/png", b"NEW")
        assert not (tmp_path / "a.jpg").exists()
        assert (tmp_path / "a.png").read_bytes() == b"NEW"

    def test_upload_unknown_id_raises(self, tmp_path):
        with pytest.raises(KeyError):
            self._cat(tmp_path).upload_image("zzz", "p.png", "image/png", b"x")

    def test_upload_unsupported_type_raises(self, tmp_path):
        with pytest.raises(ValueError, match="unsupported"):
            self._cat(tmp_path).upload_image("a", "notes.txt", "text/plain", b"x")

    def test_upload_empty_raises(self, tmp_path):
        with pytest.raises(ValueError, match="empty"):
            self._cat(tmp_path).upload_image("a", "p.png", "image/png", b"")


def test_image_file_route_uploads(tmp_path):
    """POST /api/item/image-file over a live server: raw body + query params."""
    import threading
    import httpx
    from http.server import ThreadingHTTPServer

    cat = wb._Catalogue("g", "t", tmp_path)
    wardrobe = {"items": [
        {"id": "a", "item_name": "Kitsune Tee", "shop": "Sumie",
         "category": "tshirt", "purchased_at": "2026-04-15"},
    ]}
    cat._store(wb.build_payload(wardrobe, tmp_path), wardrobe)
    server = ThreadingHTTPServer(("127.0.0.1", 0), wb._make_handler(cat))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        r = httpx.post(
            f"http://127.0.0.1:{port}/api/item/image-file?id=a&name=p.png",
            content=b"PNGBYTES", headers={"Content-Type": "image/png"},
            timeout=5.0)
        assert r.status_code == 200
        assert r.json()["items"][0]["image"].split("?", 1)[0] == "/images/a.png"
        assert (tmp_path / "a.png").read_bytes() == b"PNGBYTES"
        # Unknown id → clean JSON 404, not a traceback.
        r = httpx.post(
            f"http://127.0.0.1:{port}/api/item/image-file?id=zzz&name=p.png",
            content=b"x", headers={"Content-Type": "image/png"}, timeout=5.0)
        assert r.status_code == 404
        assert r.json()["error"] == "item not found"
    finally:
        server.shutdown()
        server.server_close()


def test_build_payload_uses_stored_categories():
    # Stored categories (issue #18) move a design-only tee out of "Other" and
    # hide a stored non_clothing item the name heuristic would have shown.
    w = {"items": [
        {"id": "tee", "item_name": "Kitsune", "shop": "Sumie",
         "category": "tshirt", "purchased_at": "2026-06-01"},
        {"id": "gadget", "item_name": "3D Zip Set", "shop": "Baggu",
         "category": "non_clothing", "purchased_at": "2026-05-01"},
    ]}
    p = wb.build_payload(w)
    ids = {i["id"] for i in p["items"]}
    assert ids == {"tee"}
    assert p["stats"]["hidden_non_clothing"] == 1
    cats = {c["key"] for c in p["categories"]}
    assert cats == {"tshirt"}          # no "other" bucket
