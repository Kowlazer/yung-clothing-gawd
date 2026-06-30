"""Tests for src/wardrobe_browser.py.

Covers the pure logic: name-based categorisation, the clothing/non-clothing
split (apparel-first so an apparel word always wins), price formatting, the
local-image lookup, and the frontend payload (filtering, facets, sort, stats).
The HTTP server is not exercised here.
"""

from __future__ import annotations

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
    assert wb._image_url("abc123", tmp_path) == "/images/abc123.png"
    assert wb._image_url("missing", tmp_path) is None


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
