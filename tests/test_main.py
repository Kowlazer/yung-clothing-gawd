"""Tests for src/main.py — orchestration helpers and end-to-end wiring.

Helper tests are pure unit tests on the bucketing / aggregation logic.
The end-to-end test patches every I/O boundary (watchlist fetch, gist read/
write, extract, resolve_fuzzy, send_email) and asserts that the orchestrator
threads data through them correctly.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import ANY

import pytest

from src import main as main_mod
from src.classify import Entry
from src.config import Config


# ---------------------------------------------------------------------------
# Helpers — pure unit tests
# ---------------------------------------------------------------------------

def test_homepage_url_strips_path_and_query():
    assert main_mod._homepage_url("https://shop.com/collections/foo?x=1") == "https://shop.com"
    assert main_mod._homepage_url("https://shop.com/") == "https://shop.com"
    assert main_mod._homepage_url("https://shop.com") == "https://shop.com"


def test_homepage_url_passes_through_garbage():
    # A bare domain string without a scheme isn't recognized as a URL by urlparse;
    # we return it unchanged rather than corrupting it.
    assert main_mod._homepage_url("not-a-url") == "not-a-url"


def test_bucket_entries_product_url_to_extract_list():
    entries = [
        Entry("PRODUCT_URL", "https://aniqi.com/products/joggers", "Aniqi"),
    ]
    out = main_mod._bucket_entries(entries, aliases={})
    assert out["product_urls"] == [("https://aniqi.com/products/joggers", "Aniqi")]


def test_bucket_entries_dedups_duplicate_product_url_keeps_first_context():
    """Issue #21: the same product URL pasted under a shop header AND flagged via
    the dedicated Priority section yields two PRODUCT_URL entries. _bucket_entries
    must collapse them to one (keeping the first, shop-context occurrence) so the
    item isn't extracted / sale-detected twice — while still capturing the pin."""
    entries = [
        Entry("PRODUCT_URL", "https://aniqi.com/products/joggers", "Aniqi"),
        Entry("PRODUCT_URL", "https://aniqi.com/products/joggers", "", priority=True),
    ]
    out = main_mod._bucket_entries(entries, aliases={})
    assert out["product_urls"] == [("https://aniqi.com/products/joggers", "Aniqi")]
    assert "https://aniqi.com/products/joggers" in out["priority_urls"]


def test_bucket_entries_product_url_seeds_shops_map():
    """Regression: shop names that only have PRODUCT_URLs under them (no
    bare-domain SHOP_URL entry) used to fall through to resolve_fuzzy's DDG
    queue and show up as 'could not resolve' in the digest. Derive the
    homepage from the product URL instead."""
    entries = [
        Entry("SHOP_NAME", "BibiSama", "BibiSama"),
        Entry("PRODUCT_URL", "https://bibisama.com/products/bibisama-sweatsuit", "BibiSama"),
    ]
    out = main_mod._bucket_entries(entries, aliases={})
    assert {"shop": "BibiSama", "url": "https://bibisama.com"} in out["shops_to_check"]
    assert "BibiSama" not in out["shops_to_resolve"]


def test_bucket_entries_shop_url_normalizes_to_homepage():
    entries = [
        Entry("SHOP_URL", "https://aniqi.com/collections/all", "Aniqi"),
    ]
    out = main_mod._bucket_entries(entries, aliases={})
    assert out["shops_to_check"] == [
        {"shop": "Aniqi", "url": "https://aniqi.com"},
    ]


def test_bucket_entries_shop_name_cached_alias_goes_to_check():
    entries = [Entry("SHOP_NAME", "Aniqi", "Aniqi")]
    out = main_mod._bucket_entries(entries, aliases={"Aniqi": "https://aniqi.com"})
    assert out["shops_to_check"] == [{"shop": "Aniqi", "url": "https://aniqi.com"}]
    assert out["shops_to_resolve"] == []


def test_bucket_entries_shop_name_uncached_goes_to_resolve():
    entries = [Entry("SHOP_NAME", "MysteryBrand", "MysteryBrand")]
    out = main_mod._bucket_entries(entries, aliases={})
    assert out["shops_to_check"] == []
    assert out["shops_to_resolve"] == ["MysteryBrand"]


def test_bucket_entries_loose_mention_uses_shop_url_lookup():
    entries = [
        Entry("SHOP_URL", "https://aniqi.com", "Aniqi"),
        Entry("LOOSE_MENTION", "Law pants", "Aniqi"),
    ]
    out = main_mod._bucket_entries(entries, aliases={})
    assert out["loose_ready"] == [
        {"mention": "Law pants", "shop": "Aniqi",
         "shop_domain": "https://aniqi.com"},
    ]
    assert out["loose_deferred"] == []


def test_bucket_entries_loose_mention_uses_aliases_when_no_shop_url():
    entries = [Entry("LOOSE_MENTION", "Law pants", "Aniqi")]
    out = main_mod._bucket_entries(entries, aliases={"Aniqi": "https://aniqi.com"})
    assert out["loose_ready"][0]["shop_domain"] == "https://aniqi.com"


def test_bucket_entries_loose_mention_deferred_when_shop_unknown():
    entries = [Entry("LOOSE_MENTION", "Whatever", "UnknownShop")]
    out = main_mod._bucket_entries(entries, aliases={})
    assert out["loose_ready"] == []
    assert out["loose_deferred"] == [{"mention": "Whatever", "shop": "UnknownShop"}]
    # The unknown shop is added to the resolve queue so it gets cached next run.
    assert out["shops_to_resolve"] == ["UnknownShop"]


def test_bucket_entries_shop_name_dedupe():
    entries = [
        Entry("SHOP_NAME", "Aniqi", "Aniqi"),
        Entry("SHOP_NAME", "Aniqi", "Aniqi"),
    ]
    out = main_mod._bucket_entries(entries, aliases={})
    assert out["shops_to_resolve"] == ["Aniqi"]


def test_bucket_entries_loose_unknown_shop_dedup_with_shop_name():
    entries = [
        Entry("SHOP_NAME", "UnknownShop", "UnknownShop"),
        Entry("LOOSE_MENTION", "thing", "UnknownShop"),
    ]
    out = main_mod._bucket_entries(entries, aliases={})
    # SHOP_NAME already queued — loose-mention shouldn't double-add it.
    assert out["shops_to_resolve"] == ["UnknownShop"]


def test_bucket_entries_non_clothing_shops_empty_when_all_clothing():
    entries = [
        Entry("SHOP_NAME", "Aniqi", "Aniqi"),
        Entry("PRODUCT_URL", "https://aniqi.com/products/joggers", "Aniqi"),
    ]
    out = main_mod._bucket_entries(entries, aliases={})
    assert out["non_clothing_shops"] == []


def test_bucket_entries_collects_non_clothing_shops():
    """A shop whose entries are all flagged is_clothing=False is reported in
    non_clothing_shops (from SHOP_NAME value and URL/loose context alike)."""
    entries = [
        Entry("SHOP_NAME", "Keychron", "Keychron", False),
        Entry("PRODUCT_URL", "https://keychron.com/products/q1", "Keychron", False),
        Entry("LOOSE_MENTION", "G Pro X Superlight", "Logitech", False),
    ]
    out = main_mod._bucket_entries(entries, aliases={})
    assert out["non_clothing_shops"] == ["Keychron", "Logitech"]


def test_bucket_entries_non_clothing_bare_shop_url_uses_netloc():
    """A bare non-clothing SHOP_URL with no shop header gets its netloc as the
    label in shops_to_check; non_clothing_shops must use the same netloc so the
    shop's homepage sale status breaks out into the non-clothing block."""
    entries = [
        Entry("SHOP_URL", "https://keychron.com", "", False),
    ]
    out = main_mod._bucket_entries(entries, aliases={})
    assert {"shop": "keychron.com", "url": "https://keychron.com"} in out["shops_to_check"]
    assert out["non_clothing_shops"] == ["keychron.com"]


def test_bucket_entries_clothing_wins_on_dual_section_shop():
    """If a shop appears in both sections, clothing wins so its items stay in
    the main (top) digest sections."""
    entries = [
        Entry("PRODUCT_URL", "https://shop.com/products/shirt", "DualShop", True),
        Entry("PRODUCT_URL", "https://shop.com/products/mug", "DualShop", False),
    ]
    out = main_mod._bucket_entries(entries, aliases={})
    assert out["non_clothing_shops"] == []


def test_bucket_entries_amazon_untracked_url():
    """Amazon product URLs can't be crawled — they go into the untracked bucket,
    NOT the extract list, and don't seed a homepage sale check."""
    entries = [
        Entry("SHOP_NAME", "Amazon", "Amazon"),
        Entry("UNTRACKED_URL", "https://www.amazon.com/Some-Hoodie/dp/B07YF5CR5Z", "Amazon"),
    ]
    out = main_mod._bucket_entries(entries, aliases={})
    assert out["untracked_urls"] == [
        {"url": "https://www.amazon.com/Some-Hoodie/dp/B07YF5CR5Z",
         "shop": "Amazon", "is_clothing": True},
    ]
    assert out["product_urls"] == []
    assert out["shops_to_check"] == []


def test_bucket_entries_amazon_header_not_resolved():
    """The 'Amazon:' header has only untracked children — it must not be pushed
    into resolve_fuzzy's DDG/Claude queue (no 'could not resolve Amazon')."""
    entries = [
        Entry("SHOP_NAME", "Amazon", "Amazon"),
        Entry("UNTRACKED_URL", "https://www.amazon.com/dp/B07YF5CR5Z", "Amazon"),
    ]
    out = main_mod._bucket_entries(entries, aliases={})
    assert "Amazon" not in out["shops_to_resolve"]
    assert out["shops_to_check"] == []


def test_merge_aliases_keeps_high_and_low_confidence():
    aliases = main_mod._merge_aliases(
        {"Existing": "https://existing.com"},
        [
            {"shop_name": "HighShop", "url": "https://high.com", "confidence": "high"},
            {"shop_name": "LowShop", "url": "https://low.com", "confidence": "low"},
            {"shop_name": "NoneShop", "url": None, "confidence": "none"},
            {"shop_name": "EmptyShop", "url": "", "confidence": "high"},
        ],
    )
    assert aliases == {
        "Existing": "https://existing.com",
        "HighShop": "https://high.com",
        "LowShop": "https://low.com",
    }


def test_collect_unresolved_combines_buckets():
    fuzzy = {
        "unresolved": ["NoCandidates"],
        "resolutions": [
            {"shop_name": "PickedNone", "url": None, "confidence": "none"},
            {"shop_name": "PickedNullUrl", "url": "", "confidence": "low"},
            {"shop_name": "PickedHigh", "url": "https://x.com", "confidence": "high"},
        ],
    }
    out = main_mod._collect_unresolved(fuzzy, deferred_shops=[])
    assert "NoCandidates" in out
    assert "PickedNone" in out
    assert "PickedNullUrl" in out
    assert "PickedHigh" not in out


def test_digest_subject_counts_each_category():
    shop_sales = [
        {"shop": "A", "status": "yes"},
        {"shop": "B", "status": "no"},
        {"shop": "C", "status": "yes"},
    ]
    items = [
        {"is_uncertain": False, "result": {"sale_signal": "on_sale_per_page"}},
        {"is_uncertain": False, "result": {"sale_signal": "price_dropped"}},
        {"is_uncertain": False, "result": {"sale_signal": "no_change"}},
        {"is_uncertain": True, "result": {"sale_signal": "on_sale_per_page"}},
        {"is_uncertain": True, "result": {"sale_signal": "no_change"}},
    ]
    subject = main_mod._digest_subject(
        shop_sales, items, today=datetime(2026, 5, 18, tzinfo=timezone.utc)
    )
    assert subject == "Sale check — May 18 — 2 shops on sale, 2 items on sale, 2 uncertain"


# ---------------------------------------------------------------------------
# _extract_many — concurrent helper
# ---------------------------------------------------------------------------

def test_extract_many_empty_urls_returns_empty():
    assert main_mod._extract_many([]) == {}


def test_extract_many_runs_injected_fn_per_url():
    calls = []

    def fake(url):
        calls.append(url)
        return {"current_price": 10.0, "url": url}

    out = main_mod._extract_many(
        ["https://a.com/products/x", "https://b.com/products/y"],
        extract_fn=fake,
    )
    assert set(out.keys()) == {"https://a.com/products/x", "https://b.com/products/y"}
    assert sorted(calls) == sorted(out.keys())


def test_extract_many_serializes_within_domain():
    """Per-domain serialization: URLs sharing a netloc should run one at a
    time. Verified via a shared in-flight counter that errors if more than
    one fetch on the same domain is active simultaneously."""
    import threading
    active: dict[str, int] = {}
    lock = threading.Lock()
    max_observed: dict[str, int] = {}

    def fake(url):
        from urllib.parse import urlparse
        d = urlparse(url).netloc
        with lock:
            active[d] = active.get(d, 0) + 1
            max_observed[d] = max(max_observed.get(d, 0), active[d])
        # tiny pause so concurrent activity on other domains has a chance
        import time
        time.sleep(0.02)
        with lock:
            active[d] -= 1
        return {"current_price": 1.0}

    urls = [
        "https://a.com/products/1", "https://a.com/products/2",
        "https://a.com/products/3", "https://b.com/products/1",
        "https://b.com/products/2",
    ]
    main_mod._extract_many(urls, extract_fn=fake, max_workers=8, jitter=None)
    # No domain should ever have had more than 1 concurrent fetch.
    assert max(max_observed.values()) == 1


def test_extract_many_runs_domains_concurrently():
    """Cross-check: different domains DO run in parallel (otherwise the
    per-domain serialization above would have just made everything sequential)."""
    import threading
    import time
    active_total = [0]
    max_total = [0]
    lock = threading.Lock()

    def fake(url):
        with lock:
            active_total[0] += 1
            max_total[0] = max(max_total[0], active_total[0])
        time.sleep(0.05)
        with lock:
            active_total[0] -= 1
        return {"current_price": 1.0}

    urls = [f"https://shop{i}.com/products/x" for i in range(5)]
    main_mod._extract_many(urls, extract_fn=fake, max_workers=5, jitter=None)
    # 5 different domains should run in parallel.
    assert max_total[0] >= 2


def test_extract_many_applies_jitter_between_same_domain_requests(monkeypatch):
    """Jitter delay fires between requests within a domain, not before the
    first request and not across different domains."""
    sleeps: list[float] = []
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(main_mod.random, "uniform", lambda a, b: 0.42)

    urls = [
        "https://a.com/items/1", "https://a.com/items/2",
        "https://a.com/items/3", "https://b.com/items/1",
    ]
    main_mod._extract_many(
        urls,
        extract_fn=lambda u: {"current_price": 1.0},
        max_workers=4,
        jitter=(0.4, 0.5),
    )
    # 2 intra-domain gaps on a.com, 0 on b.com (single URL) = 2 sleeps total.
    assert len(sleeps) == 2
    assert all(s == 0.42 for s in sleeps)


def test_extract_many_swallows_per_url_exception():
    def fake(url):
        if "bad" in url:
            raise RuntimeError("boom")
        return {"current_price": 1.0}

    out = main_mod._extract_many(
        ["https://ok.com/products/x", "https://bad.com/products/y"],
        extract_fn=fake,
    )
    assert out["https://ok.com/products/x"]["current_price"] == 1.0
    assert out["https://bad.com/products/y"]["error_kind"] == "other"
    assert "boom" in out["https://bad.com/products/y"]["error"]


# ---------------------------------------------------------------------------
# Pants detection + per-URL preferred-sizes selection
# ---------------------------------------------------------------------------

class TestPantsDetection:
    def test_pants_slug_matches(self):
        assert main_mod._is_pants_url("https://shop.com/products/cargo-pants-black")

    def test_jeans_slug_matches(self):
        assert main_mod._is_pants_url("https://shop.com/products/raw-denim-jeans")

    def test_joggers_slug_matches(self):
        assert main_mod._is_pants_url("https://shop.com/products/fleece-joggers")

    def test_sweatpants_slug_matches(self):
        assert main_mod._is_pants_url("https://shop.com/products/heavyweight-sweatpants")

    def test_shorts_slug_matches(self):
        """Bottom-sizing applies to shorts too — same waist measurements."""
        assert main_mod._is_pants_url("https://shop.com/products/mesh-shorts")

    def test_chinos_slug_matches(self):
        assert main_mod._is_pants_url("https://shop.com/products/slim-chinos")

    def test_leggings_slug_matches(self):
        assert main_mod._is_pants_url("https://shop.com/products/yoga-leggings")

    def test_tee_slug_does_not_match(self):
        assert not main_mod._is_pants_url("https://shop.com/products/graphic-tee")

    def test_hoodie_slug_does_not_match(self):
        assert not main_mod._is_pants_url("https://shop.com/products/zip-hoodie")

    def test_pantyhose_does_not_match(self):
        """Word-boundary check: 'pantyhose' shouldn't trigger on 'pant'."""
        assert not main_mod._is_pants_url("https://shop.com/products/sheer-pantyhose")

    def test_label_fallback_when_slug_is_generic(self):
        """Etsy /listing/12345 has no descriptive slug — the cached label
        from prices.json picks up the slack on the second run."""
        url = "https://www.etsy.com/listing/1234567/widget"
        assert not main_mod._is_pants_url(url)
        assert main_mod._is_pants_url(url, label="Wide-leg trouser pants")

    def test_label_does_not_override_negative_slug(self):
        """Slug positive wins; slug negative + no label keyword stays negative."""
        assert not main_mod._is_pants_url(
            "https://shop.com/products/graphic-tee",
            label="Anime print shirt",
        )

    def test_case_insensitive(self):
        assert main_mod._is_pants_url("https://shop.com/products/CARGO-PANTS")


class TestPreferredSizesFor:
    def _cfg(self, *, tops: tuple[str, ...] = (), pants: tuple[str, ...] = ()) -> Config:
        from dataclasses import replace
        return replace(_FAKE_CONFIG, preferred_sizes=tops, preferred_sizes_pants=pants)

    def test_pants_uses_pants_list(self):
        cfg = self._cfg(tops=("M", "L", "XL"), pants=("S", "M", "L"))
        sizes = main_mod._preferred_sizes_for(
            cfg, "https://shop.com/products/cargo-pants"
        )
        assert sizes == ("S", "M", "L")

    def test_non_pants_uses_tops_list(self):
        cfg = self._cfg(tops=("M", "L", "XL"), pants=("S", "M", "L"))
        sizes = main_mod._preferred_sizes_for(
            cfg, "https://shop.com/products/graphic-tee"
        )
        assert sizes == ("M", "L", "XL")

    def test_pants_without_pants_list_falls_back_to_tops(self):
        """Pants URL but only PREFERRED_SIZES is set → falls back to that."""
        cfg = self._cfg(tops=("M", "L", "XL"), pants=())
        sizes = main_mod._preferred_sizes_for(
            cfg, "https://shop.com/products/cargo-pants"
        )
        assert sizes == ("M", "L", "XL")

    def test_neither_configured_returns_empty(self):
        cfg = self._cfg(tops=(), pants=())
        assert main_mod._preferred_sizes_for(
            cfg, "https://shop.com/products/cargo-pants"
        ) == ()

    def test_label_drives_pants_selection_for_generic_slug(self):
        cfg = self._cfg(tops=("M", "L", "XL"), pants=("S", "M", "L"))
        sizes = main_mod._preferred_sizes_for(
            cfg,
            "https://www.etsy.com/listing/123/widget",
            label="Linen trouser",
        )
        assert sizes == ("S", "M", "L")


def test_extract_many_passes_per_url_preferred_sizes_to_extract_fn():
    """When ``preferred_sizes`` is a callable, _extract_many resolves it per
    URL and forwards the resulting tuple as a kwarg to ``extract_fn``."""
    received: dict[str, tuple[str, ...]] = {}

    def fake(url, *, preferred_sizes=()):
        received[url] = preferred_sizes
        return {"current_price": 1.0}

    def sizes_for(url):
        return ("S", "M", "L") if "pants" in url else ("M", "L", "XL")

    main_mod._extract_many(
        [
            "https://shop.com/products/cargo-pants",
            "https://shop.com/products/cool-tee",
        ],
        extract_fn=fake,
        jitter=None,
        preferred_sizes=sizes_for,
    )
    assert received["https://shop.com/products/cargo-pants"] == ("S", "M", "L")
    assert received["https://shop.com/products/cool-tee"] == ("M", "L", "XL")


def test_extract_many_empty_callable_result_omits_kwarg():
    """Per-URL sizes resolving to () should call the fake with no kwarg, so
    test stubs with ``def fake(url):`` (no preferred_sizes param) still work."""
    called_with: list[tuple] = []

    def fake(url):
        called_with.append((url,))
        return {"current_price": 1.0}

    main_mod._extract_many(
        ["https://shop.com/products/foo"],
        extract_fn=fake,
        jitter=None,
        preferred_sizes=lambda u: (),
    )
    assert called_with == [("https://shop.com/products/foo",)]


# ---------------------------------------------------------------------------
# Integration — run() with all I/O patched
# ---------------------------------------------------------------------------

_FAKE_WATCHLIST = """\
Aniqi:
https://aniqi.com
https://aniqi.com/products/trafalgar-joggers
- Law pants

KillCrew:
- some shirt
"""

_FAKE_CONFIG = Config(
    watchlist_url="https://docs.google.com/document/d/abc/edit",
    resend_api_key="re_xxx",
    from_email="from@example.com",
    to_email="to@example.com",
    github_token="ghp_xxx",
    gist_id="gist123",
    anthropic_api_key="sk-ant-xxx",
    gmail_username="user@gmail.com",
    gmail_app_password="abcd efgh ijkl mnop",
    signup_enabled=False,
    signup_phone="",
    preferred_sizes=(),
    preferred_sizes_pants=(),
)


def _empty_gmail_result(*_a, **_kw):
    """Default _gmail_pipeline patch — empty result, no network."""
    return {
        "codes": [],
        "unattributed": [],
        "sale_signals": [],
        "gmail_state_out": {"processed_ids": {}},
    }


def _empty_voice_result(*_a, **_kw):
    """Default _voice_pipeline patch — empty result, no network."""
    return {
        "codes": [],
        "unattributed": [],
        "sale_signals": [],
        "untracked_senders": [],
        "voice_state_out": {"processed_ids": {}},
    }


def test_run_threads_data_through_pipeline(monkeypatch):
    captured: dict = {}

    monkeypatch.setattr(main_mod, "fetch_watchlist", lambda url: _FAKE_WATCHLIST)
    monkeypatch.setattr(main_mod, "_gmail_pipeline", _empty_gmail_result)
    monkeypatch.setattr(main_mod, "_voice_pipeline", _empty_voice_result)
    monkeypatch.setattr(main_mod, "_review_requests_pipeline",
                        lambda *a, **k: ([], None))
    monkeypatch.setattr(main_mod, "_restock_emails_pipeline",
                        lambda *a, **k: ([], None))

    monkeypatch.setattr(main_mod, "read_state", lambda gist_id, token: {
        "prices": {
            # Pre-existing entry — used to validate detect_sale sees it.
            "https://aniqi.com/products/trafalgar-joggers": {
                "label": "Trafalgar Joggers",
                "current_price": 72.0,
                "original_price": None,
                "currency": "USD",
                "in_stock": True,
                "low_stock": False,
                "last_checked": "2026-05-10T00:00:00Z",
                "last_seen": "2026-05-10T00:00:00Z",
                "consecutive_failures": 0,
                "last_error_kind": None,
            },
        },
        "aliases": {},  # KillCrew is uncached → goes to resolve_fuzzy
        "codes": [],
        "fx": {},  # no FX cache → get_rates will try to fetch; we'll patch it
        "gmail": {"processed_ids": {}},
        "voice": {"processed_ids": {}},
        "sms_aliases": {},
    })

    monkeypatch.setattr(
        main_mod, "get_rates",
        lambda cache: (None, {}),
    )

    def fake_extract(url):
        # Price drop from 72 -> 58: triggers price_dropped.
        return {
            "current_price": 58.0,
            "original_price": None,
            "currency": "USD",
            "on_sale": False,
            "out_of_stock": False,
            "low_stock": False,
            "label": "Trafalgar Joggers",
            "total_variant_count": 1,
            "available_variant_count": None,
            "color_options": [],
            "error": None,
            "error_kind": None,
        }

    monkeypatch.setattr(main_mod, "extract", fake_extract)

    def fake_resolve_fuzzy(*, shops_to_check, shops_to_resolve, loose_mentions,
                           email_signals=None, prior_verdicts=None, today=None,
                           shadow_model=None):
        captured["resolve_fuzzy_input"] = {
            "shops_to_check": shops_to_check,
            "shops_to_resolve": shops_to_resolve,
            "loose_mentions": loose_mentions,
            "email_signals": email_signals,
            "prior_verdicts": prior_verdicts,
            "today": today,
            "shadow_model": shadow_model,
        }
        return {
            "shop_sales": [{"shop": "Aniqi", "status": "yes",
                            "description": "20% off everything"}],
            "resolutions": [{"shop_name": "KillCrew",
                             "url": "https://killcrew.com",
                             "confidence": "high"}],
            "loose_matches": [],
            "email_sales": [],
            "unresolved": [],
            "shop_verdicts": [{"shop": "Aniqi", "hash": "H1", "status": "yes",
                               "description": "20% off everything"}],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }

    monkeypatch.setattr(main_mod, "resolve_fuzzy", fake_resolve_fuzzy)

    def fake_write_state(gist_id, token, *, prices, aliases, codes, fx,
                          gmail=None, voice=None, sms_aliases=None,
                          email_sales=None, body_scans=None, shop_verdicts=None,
                          shadow_runs=None):
        captured["write_state"] = {
            "prices": prices, "aliases": aliases,
            "codes": codes, "fx": fx, "gmail": gmail,
            "voice": voice, "sms_aliases": sms_aliases,
            "email_sales": email_sales, "body_scans": body_scans,
            "shop_verdicts": shop_verdicts, "shadow_runs": shadow_runs,
        }

    monkeypatch.setattr(main_mod, "write_state", fake_write_state)

    def fake_send_email(api_key, from_addr, to_addr, subject, body_md):
        captured["email"] = {
            "subject": subject, "body": body_md,
            "to": to_addr, "from": from_addr,
        }
        return "msg-id-123"

    monkeypatch.setattr(main_mod, "send_email", fake_send_email)

    digest_md = main_mod.run(_FAKE_CONFIG)

    # resolve_fuzzy got the right buckets
    rf = captured["resolve_fuzzy_input"]
    assert {"shop": "Aniqi", "url": "https://aniqi.com"} in rf["shops_to_check"]
    assert rf["shops_to_resolve"] == ["KillCrew"]
    # Loose "Law pants" is under Aniqi which has a known SHOP_URL → ready
    assert rf["loose_mentions"] == [
        {"mention": "Law pants", "shop": "Aniqi",
         "shop_domain": "https://aniqi.com"},
    ]

    # state write reflects the price drop + new alias from resolve_fuzzy
    ws = captured["write_state"]
    aniqi_entry = ws["prices"]["https://aniqi.com/products/trafalgar-joggers"]
    assert aniqi_entry["current_price"] == 58.0
    assert ws["aliases"] == {"KillCrew": "https://killcrew.com"}

    # cost lever #3: this run's fresh homepage verdict is upserted and persisted
    assert ws["shop_verdicts"] == [
        {"shop": "Aniqi", "hash": "H1", "status": "yes",
         "description": "20% off everything", "checked_at": ANY},
    ]
    # ...and prior_verdicts from state was threaded into resolve_fuzzy
    assert "prior_verdicts" in captured["resolve_fuzzy_input"]

    # email got a subject reflecting the counts
    assert "Sale check —" in captured["email"]["subject"]
    assert "1 shops on sale" in captured["email"]["subject"]
    assert "1 items on sale" in captured["email"]["subject"]
    assert captured["email"]["body"] == digest_md

    # digest contains the on-sale shop and the dropped-price item
    assert "Aniqi" in digest_md
    assert "20% off everything" in digest_md


def test_run_skips_extracting_when_no_loose_matches(monkeypatch):
    """When resolve_fuzzy returns no loose-match URLs, the second extract pass
    is skipped (no spurious calls). Verifies _extract_many is only called once
    for the product-URL batch."""
    extract_calls: list[list[str]] = []

    original_extract_many = main_mod._extract_many

    def spy_extract_many(urls, **kw):
        extract_calls.append(list(urls))
        return {u: {"current_price": 10.0, "original_price": None,
                    "currency": "USD", "on_sale": False, "out_of_stock": False,
                    "low_stock": False, "label": "x", "total_variant_count": None,
                    "available_variant_count": None, "color_options": [],
                    "error": None, "error_kind": None} for u in urls}

    monkeypatch.setattr(main_mod, "_extract_many", spy_extract_many)
    monkeypatch.setattr(main_mod, "fetch_watchlist",
                        lambda url: "Aniqi:\nhttps://aniqi.com/products/x\n")
    monkeypatch.setattr(main_mod, "_gmail_pipeline", _empty_gmail_result)
    monkeypatch.setattr(main_mod, "_voice_pipeline", _empty_voice_result)
    monkeypatch.setattr(main_mod, "_review_requests_pipeline",
                        lambda *a, **k: ([], None))
    monkeypatch.setattr(main_mod, "_restock_emails_pipeline",
                        lambda *a, **k: ([], None))
    monkeypatch.setattr(main_mod, "read_state",
                        lambda *a, **kw: {"prices": {}, "aliases": {},
                                          "codes": [], "fx": {},
                                          "gmail": {"processed_ids": {}},
                                          "voice": {"processed_ids": {}},
                                          "sms_aliases": {}})
    monkeypatch.setattr(main_mod, "get_rates", lambda cache: (None, {}))
    monkeypatch.setattr(main_mod, "resolve_fuzzy", lambda **kw: {
        "shop_sales": [], "resolutions": [], "loose_matches": [],
        "email_sales": [], "unresolved": [], "usage": None,
    })
    monkeypatch.setattr(main_mod, "write_state", lambda *a, **kw: None)
    monkeypatch.setattr(main_mod, "send_email", lambda *a, **kw: "id")

    main_mod.run(_FAKE_CONFIG)
    # Exactly one extract batch (the product-URL one), no loose-match second pass.
    assert len(extract_calls) == 1
    assert extract_calls[0] == ["https://aniqi.com/products/x"]


def test_run_renders_persisted_email_sale_with_no_new_signal(monkeypatch):
    """An advance sale judged on an earlier run keeps showing on later runs —
    when its source email is no longer re-fetched (gmail/voice yield nothing
    and resolve_fuzzy returns no email_sales) — sourced purely from the
    persisted email_sales store, and is written back still-active."""
    future = (datetime.now(timezone.utc).date() + timedelta(days=4)).isoformat()
    persisted = [{
        "shop": "Aniqi", "email_id": "old_msg", "status": "yes",
        "description": "Memorial Day sale, 30% off",
        "starts_on": future, "ends_on": None,
        "first_seen": "2026-01-01T00:00:00+00:00",
        "last_seen": "2026-01-01T00:00:00+00:00",
    }]
    captured: dict = {}

    monkeypatch.setattr(main_mod, "fetch_watchlist",
                        lambda url: "Aniqi:\nhttps://aniqi.com/products/x\n")
    monkeypatch.setattr(main_mod, "_gmail_pipeline", _empty_gmail_result)
    monkeypatch.setattr(main_mod, "_voice_pipeline", _empty_voice_result)
    monkeypatch.setattr(main_mod, "_review_requests_pipeline",
                        lambda *a, **k: ([], None))
    monkeypatch.setattr(main_mod, "_restock_emails_pipeline",
                        lambda *a, **k: ([], None))
    monkeypatch.setattr(main_mod, "read_state", lambda *a, **kw: {
        "prices": {}, "aliases": {}, "codes": [], "email_sales": persisted,
        "fx": {}, "gmail": {"processed_ids": {}},
        "voice": {"processed_ids": {}}, "sms_aliases": {},
    })
    monkeypatch.setattr(main_mod, "get_rates", lambda cache: (None, {}))
    monkeypatch.setattr(
        main_mod, "_extract_many",
        lambda urls, **kw: {u: {"current_price": 10.0, "original_price": None,
                                "currency": "USD", "on_sale": False,
                                "out_of_stock": False, "low_stock": False,
                                "label": "x", "total_variant_count": None,
                                "available_variant_count": None,
                                "color_options": [], "error": None,
                                "error_kind": None} for u in urls})
    monkeypatch.setattr(main_mod, "resolve_fuzzy", lambda **kw: {
        "shop_sales": [], "resolutions": [], "loose_matches": [],
        "email_sales": [], "unresolved": [], "usage": None,
    })
    monkeypatch.setattr(main_mod, "write_state",
                        lambda *a, **kw: captured.update(email_sales=kw.get("email_sales")))
    monkeypatch.setattr(main_mod, "send_email", lambda *a, **kw: "id")

    digest_md = main_mod.run(_FAKE_CONFIG)

    assert "## Sales announced by email" in digest_md
    assert "Memorial Day sale, 30% off" in digest_md
    # Persisted entry survived this run and is written back to state.
    assert captured["email_sales"] is not None
    assert any(e["email_id"] == "old_msg" for e in captured["email_sales"])


def test_run_filters_purchased_items_from_watchlist(monkeypatch):
    """End-to-end: wardrobe.watchlist_exclusions causes the daily cron to
    skip extracting the product URL on an approved-purchased line. Without
    this, the user would keep getting sale signals on items they already own
    until they manually pasted the lines out of the Doc."""
    extract_urls: list[str] = []

    monkeypatch.setattr(
        main_mod, "fetch_watchlist",
        lambda url: (
            "Aniqi:\n"
            "https://aniqi.com/products/already-bought\n"
            "https://aniqi.com/products/still-want\n"
        ),
    )
    monkeypatch.setattr(main_mod, "_gmail_pipeline", _empty_gmail_result)
    monkeypatch.setattr(main_mod, "_voice_pipeline", _empty_voice_result)
    monkeypatch.setattr(main_mod, "_review_requests_pipeline",
                        lambda *a, **k: ([], None))
    monkeypatch.setattr(main_mod, "_restock_emails_pipeline",
                        lambda *a, **k: ([], None))
    monkeypatch.setattr(main_mod, "read_state", lambda *a, **kw: {
        "prices": {}, "aliases": {"Aniqi": "https://aniqi.com"},
        "codes": [], "fx": {},
        "gmail": {"processed_ids": {}},
        "voice": {"processed_ids": {}},
        "sms_aliases": {},
        "wardrobe": {"watchlist_exclusions": [
            # The approved line is the product URL itself — the matcher
            # records whatever stripped Doc line caught the item, which
            # can be a URL line just as easily as a "- some item" line.
            {"matched_line": "https://aniqi.com/products/already-bought"},
        ]},
    })
    monkeypatch.setattr(main_mod, "get_rates", lambda cache: (None, {}))

    def spy_extract(url):
        extract_urls.append(url)
        return {"current_price": 10.0, "original_price": None,
                "currency": "USD", "on_sale": False, "out_of_stock": False,
                "low_stock": False, "label": url, "total_variant_count": None,
                "available_variant_count": None, "color_options": [],
                "error": None, "error_kind": None}

    monkeypatch.setattr(main_mod, "extract", spy_extract)
    monkeypatch.setattr(main_mod, "resolve_fuzzy", lambda **kw: {
        "shop_sales": [], "resolutions": [], "loose_matches": [],
        "email_sales": [], "unresolved": [], "usage": None,
    })
    monkeypatch.setattr(main_mod, "write_state", lambda *a, **kw: None)
    monkeypatch.setattr(main_mod, "send_email", lambda *a, **kw: "id")

    main_mod.run(_FAKE_CONFIG)

    assert "https://aniqi.com/products/already-bought" not in extract_urls
    assert "https://aniqi.com/products/still-want" in extract_urls


# ---------------------------------------------------------------------------
# _strip_email_sale_shops — pure unit tests
# ---------------------------------------------------------------------------

class TestStripEmailSaleShops:
    _TODAY = date(2026, 5, 19)

    def test_drops_homepage_entry_for_ongoing_email_shop(self):
        """A shop with an *ongoing* email sale is removed from shop_sales so it
        renders only in the email section (no contradictory double-listing)."""
        out = main_mod._strip_email_sale_shops(
            [{"shop": "Aniqi", "status": "no", "description": None},
             {"shop": "Wooj", "status": "yes", "description": "Sitewide 20%"}],
            [{"shop": "Aniqi", "description": "Big sale", "starts_on": None,
              "ends_on": None}],
            self._TODAY,
        )
        shops = [s["shop"] for s in out]
        assert "Aniqi" not in shops
        assert "Wooj" in shops

    def test_keeps_homepage_entry_for_upcoming_email_shop(self):
        """An *upcoming* email sale must NOT strip the homepage entry — the user
        wants to see both the current status and the upcoming countdown."""
        out = main_mod._strip_email_sale_shops(
            [{"shop": "Aniqi", "status": "yes", "description": "On sale now"}],
            [{"shop": "Aniqi", "description": "Memorial Day sale",
              "starts_on": "2026-05-24", "ends_on": "2026-05-26"}],
            self._TODAY,
        )
        # Homepage "yes" preserved; the upcoming sale lives in the email section.
        assert [s["shop"] for s in out] == ["Aniqi"]

    def test_case_insensitive_match(self):
        out = main_mod._strip_email_sale_shops(
            [{"shop": "ANIQI", "status": "yes", "description": "30%"}],
            [{"shop": "aniqi", "description": "newsletter"}],
            self._TODAY,
        )
        assert out == []

    def test_no_active_email_sales_passthrough(self):
        existing = [{"shop": "A", "status": "yes", "description": "d"}]
        out = main_mod._strip_email_sale_shops(existing, [], self._TODAY)
        assert out == existing
        # Returns a copy, not the same list object.
        assert out is not existing

    def test_homepage_only_shops_untouched(self):
        out = main_mod._strip_email_sale_shops(
            [{"shop": "A", "status": "yes"}, {"shop": "B", "status": "no"}],
            [{"shop": "C", "description": "sale"}],
            self._TODAY,
        )
        assert [s["shop"] for s in out] == ["A", "B"]


# ---------------------------------------------------------------------------
# _drop_ongoing_email_sale_shops — pure unit tests (cost lever #2)
# ---------------------------------------------------------------------------

class TestDropOngoingEmailSaleShops:
    _TODAY = date(2026, 5, 19)

    def test_drops_homepage_for_ongoing_email_shop(self):
        """A shop with an ongoing (active-now) email sale is removed from the
        homepage queue — its verdict would be discarded by the strip anyway."""
        out = main_mod._drop_ongoing_email_sale_shops(
            [{"shop": "Aniqi", "url": "https://aniqi.com"},
             {"shop": "Wooj", "url": "https://wooj.com"}],
            [{"shop": "Aniqi", "description": "Big sale",
              "starts_on": None, "ends_on": None}],   # undated → ongoing
            self._TODAY,
        )
        assert [s["shop"] for s in out] == ["Wooj"]

    def test_keeps_homepage_for_upcoming_email_shop(self):
        """An upcoming email sale must NOT skip the homepage check — the user
        wants the current homepage status alongside the countdown."""
        out = main_mod._drop_ongoing_email_sale_shops(
            [{"shop": "Aniqi", "url": "https://aniqi.com"}],
            [{"shop": "Aniqi", "description": "Memorial Day sale",
              "starts_on": "2026-05-24", "ends_on": "2026-05-26"}],
            self._TODAY,
        )
        assert [s["shop"] for s in out] == ["Aniqi"]

    def test_case_insensitive_match(self):
        out = main_mod._drop_ongoing_email_sale_shops(
            [{"shop": "ANIQI", "url": "https://aniqi.com"}],
            [{"shop": "aniqi", "description": "newsletter sale"}],
            self._TODAY,
        )
        assert out == []

    def test_expired_email_sale_does_not_skip(self):
        """A sale that already ended (past grace) is not active → still checked."""
        out = main_mod._drop_ongoing_email_sale_shops(
            [{"shop": "Aniqi", "url": "https://aniqi.com"}],
            [{"shop": "Aniqi", "description": "old sale",
              "starts_on": "2026-05-01", "ends_on": "2026-05-05"}],
            self._TODAY,
        )
        assert [s["shop"] for s in out] == ["Aniqi"]

    def test_no_prior_email_sales_passthrough(self):
        shops = [{"shop": "A", "url": "https://a.com"}]
        out = main_mod._drop_ongoing_email_sale_shops(shops, [], self._TODAY)
        assert out == shops
        assert out is not shops  # returns a copy


# ---------------------------------------------------------------------------
# _digest_subject — counts shops across homepage + email sources
# ---------------------------------------------------------------------------

class TestDigestSubject:
    _TODAY = datetime(2026, 5, 19, tzinfo=timezone.utc)

    def test_unions_homepage_and_ongoing_email_sale_shops(self):
        subject = main_mod._digest_subject(
            [{"shop": "Wooj", "status": "yes"}],
            [],
            [{"shop": "Aniqi", "description": "sale"}],  # undated → ongoing
            today=self._TODAY,
        )
        # Wooj (homepage) + Aniqi (ongoing email) = 2 distinct shops on sale.
        assert "2 shops on sale" in subject
        assert "upcoming" not in subject

    def test_dedupes_shop_in_both_sources(self):
        subject = main_mod._digest_subject(
            [{"shop": "Aniqi", "status": "yes"}],
            [],
            [{"shop": "aniqi", "description": "sale"}],
            today=self._TODAY,
        )
        assert "1 shops on sale" in subject

    def test_upcoming_email_sale_counted_separately(self):
        """An upcoming (not-yet-started) email sale is reported as 'upcoming',
        not folded into the 'on sale' count."""
        subject = main_mod._digest_subject(
            [{"shop": "Wooj", "status": "yes"}],
            [],
            [{"shop": "Aniqi", "description": "Memorial Day",
              "starts_on": "2026-05-24", "ends_on": "2026-05-26"}],
            today=self._TODAY,
        )
        assert "1 shops on sale" in subject   # only Wooj is on sale now
        assert "1 upcoming" in subject        # Aniqi starts later

    def test_no_upcoming_suffix_when_none(self):
        subject = main_mod._digest_subject(
            [{"shop": "Wooj", "status": "yes"}], [], today=self._TODAY,
        )
        assert "1 shops on sale" in subject
        assert "upcoming" not in subject


# ---------------------------------------------------------------------------
# _merge_codes — pure unit tests
# ---------------------------------------------------------------------------

class TestMergeCodes:
    _NOW = "2026-05-19T14:00:00+00:00"

    def test_watchlist_codes_stamped_fresh(self):
        out = main_mod._merge_codes(
            prior_codes=[],
            watchlist_raw=[{"shop": "A", "code": "X", "context": "use code X"}],
            email_codes=[],
            now_iso=self._NOW,
        )
        assert len(out) == 1
        c = out[0]
        assert c["source"] == "watchlist"
        assert c["first_seen"] == self._NOW
        assert c["last_seen"] == self._NOW

    def test_prior_email_code_seen_again_bumps_last_seen(self):
        prior = [{
            "shop": "A", "code": "X", "source": "email",
            "first_seen": "2026-05-01T00:00:00+00:00",
            "last_seen": "2026-05-01T00:00:00+00:00",
            "email_id": "old_msg",
        }]
        new = [{
            "shop": "A", "code": "X", "source": "email",
            "first_seen": self._NOW, "last_seen": self._NOW,
            "email_id": "new_msg",
        }]
        out = main_mod._merge_codes([], [], new, self._NOW)
        # No prior, just new — straight passthrough.
        assert out == new

        out = main_mod._merge_codes(prior, [], new, self._NOW)
        # first_seen preserved, last_seen + email_id refreshed.
        assert len(out) == 1
        assert out[0]["first_seen"] == "2026-05-01T00:00:00+00:00"
        assert out[0]["last_seen"] == self._NOW
        assert out[0]["email_id"] == "new_msg"

    def test_prior_email_code_not_seen_carries_over(self):
        prior = [{
            "shop": "A", "code": "Z", "source": "email",
            "first_seen": "2026-05-15T00:00:00+00:00",
            "last_seen": "2026-05-15T00:00:00+00:00",
        }]
        out = main_mod._merge_codes(prior, [], [], self._NOW)
        # Not pruned here — state.py prunes by age at write time.
        assert len(out) == 1
        assert out[0]["code"] == "Z"

    def test_prior_watchlist_codes_dropped(self):
        """Watchlist codes are rebuilt fresh every run from the doc, so the
        prior ones don't carry over (and could be stale if the doc changed)."""
        prior = [{"shop": "A", "code": "STALE", "source": "watchlist"}]
        out = main_mod._merge_codes(prior, [], [], self._NOW)
        assert out == []

    def test_dedupes_email_codes_within_run(self):
        new = [
            {"shop": "A", "code": "X", "source": "email",
             "first_seen": self._NOW, "last_seen": self._NOW},
            {"shop": "A", "code": "X", "source": "email",
             "first_seen": self._NOW, "last_seen": self._NOW},
        ]
        out = main_mod._merge_codes([], [], new, self._NOW)
        assert len(out) == 1

    def test_resighted_code_picks_up_fresh_confidence(self):
        """Legacy entries in the Gist may not have ``confidence`` at all,
        and codes that were rated under an older deny set may have stale
        ratings. The merge always overwrites with the freshly-computed
        value so the next digest groups it correctly."""
        prior = [{
            "shop": "junewave.com", "code": "SITEWIDE", "source": "email_unattributed",
            "first_seen": "2026-05-01T00:00:00+00:00",
            "last_seen": "2026-05-01T00:00:00+00:00",
            # Pretend this was added before the rating feature existed.
        }]
        new = [{
            "shop": "junewave.com", "code": "SITEWIDE", "source": "email_unattributed",
            "first_seen": self._NOW, "last_seen": self._NOW,
            "confidence": "low",
        }]
        out = main_mod._merge_codes(prior, [], new, self._NOW)
        assert len(out) == 1
        assert out[0]["confidence"] == "low"
        # first_seen still preserved from the prior sighting.
        assert out[0]["first_seen"] == "2026-05-01T00:00:00+00:00"

    def test_watchlist_codes_carry_their_confidence_through(self):
        out = main_mod._merge_codes(
            prior_codes=[],
            watchlist_raw=[
                {"shop": "A", "code": "SPRING30",
                 "context": "use code SPRING30", "confidence": "high"},
            ],
            email_codes=[],
            now_iso=self._NOW,
        )
        assert len(out) == 1
        assert out[0]["confidence"] == "high"


# ---------------------------------------------------------------------------
# _apply_wardrobe_exclusions — keeps purchased items out of the daily cron
# ---------------------------------------------------------------------------

class TestApplyWardrobeExclusions:
    """Drop watchlist lines the user approved in order_scan.

    The matcher records the stripped Doc line that triggered the match;
    the daily cron compares stripped-line-by-stripped-line and filters.
    Match is exact (no fuzzy / substring) to avoid silently hiding lines
    the user didn't actually approve.
    """

    _TEXT = (
        "Aniqi:\n"
        "https://aniqi.com\n"
        "https://aniqi.com/products/trafalgar-joggers\n"
        "- Law pants\n"
        "\n"
        "KillCrew:\n"
        "- some shirt\n"
    )

    def test_empty_exclusions_returns_text_unchanged(self):
        out = main_mod._apply_wardrobe_exclusions(self._TEXT, {})
        assert out == self._TEXT

    def test_missing_wardrobe_handled_gracefully(self):
        out = main_mod._apply_wardrobe_exclusions(self._TEXT, None)
        assert out == self._TEXT

    def test_approved_line_dropped(self):
        wardrobe = {"watchlist_exclusions": [
            {"matched_line": "- Law pants", "added_at": "x", "item_id": "y"},
        ]}
        out = main_mod._apply_wardrobe_exclusions(self._TEXT, wardrobe)
        assert "Law pants" not in out
        # Other lines preserved.
        assert "Aniqi:" in out
        assert "KillCrew:" in out
        assert "some shirt" in out

    def test_strip_match_is_whitespace_insensitive(self):
        """Approved line was stored already-stripped; the live Doc may
        have leading/trailing whitespace and should still match."""
        wardrobe = {"watchlist_exclusions": [
            {"matched_line": "some shirt"},  # no leading dash even
        ]}
        # Live Doc has the dash-prefixed version.
        out = main_mod._apply_wardrobe_exclusions(self._TEXT, wardrobe)
        # Conservative: exact stripped match. Different prefixes do NOT
        # match — order_scan stores the actual line it saw, so this
        # tests the "fuzzy-match-not-attempted" contract.
        assert "some shirt" in out

    def test_multiple_exclusions_drop_all_matches(self):
        wardrobe = {"watchlist_exclusions": [
            {"matched_line": "- Law pants"},
            {"matched_line": "- some shirt"},
        ]}
        out = main_mod._apply_wardrobe_exclusions(self._TEXT, wardrobe)
        assert "Law pants" not in out
        assert "some shirt" not in out
        # Headers and URLs untouched.
        assert "Aniqi:" in out
        assert "https://aniqi.com" in out

    def test_empty_matched_line_ignored(self):
        """A malformed exclusion with an empty matched_line must not
        accidentally drop every blank line in the watchlist (which would
        delete the section separators)."""
        wardrobe = {"watchlist_exclusions": [
            {"matched_line": ""},
            {"matched_line": "   "},
            {"matched_line": "- Law pants"},
        ]}
        out = main_mod._apply_wardrobe_exclusions(self._TEXT, wardrobe)
        # Blank line between Aniqi and KillCrew sections preserved.
        assert "\n\nKillCrew:" in out
        # Real exclusion still applied.
        assert "Law pants" not in out


# ---------------------------------------------------------------------------
# _gmail_pipeline — failure isolation
# ---------------------------------------------------------------------------

class TestGmailPipeline:
    _NOW = "2026-05-19T14:00:00+00:00"

    def test_imap_failure_returns_empty(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("imap connection refused")
        monkeypatch.setattr(main_mod, "fetch_promotions", boom)
        out = main_mod._gmail_pipeline(
            _FAKE_CONFIG, aliases={}, known_shops=[],
            prior_gmail_state={"processed_ids": {"old": self._NOW}},
            now_iso=self._NOW,
        )
        assert out["codes"] == []
        assert out["sale_signals"] == []
        # State carried over unchanged so dedup info isn't lost.
        assert out["gmail_state_out"] == {"processed_ids": {"old": self._NOW}}

    def test_happy_path_threads_signals_through(self, monkeypatch):
        monkeypatch.setattr(
            main_mod, "fetch_promotions",
            lambda username, app_password, *, skip_ids=None: [
                {"id": "m1", "from": "hello@aniqi.com",
                 "subject": "Spring Sale", "snippet": "", "date": "",
                 "body_text": "Use code SPRING30"},
            ],
        )
        out = main_mod._gmail_pipeline(
            _FAKE_CONFIG, {"Aniqi": "https://aniqi.com"}, ["Aniqi"],
            prior_gmail_state={"processed_ids": {}},
            now_iso=self._NOW,
        )
        assert len(out["codes"]) == 1
        assert out["codes"][0]["shop"] == "Aniqi"
        assert "m1" in out["gmail_state_out"]["processed_ids"]
        assert out["gmail_state_out"]["processed_ids"]["m1"] == self._NOW

    def test_skip_ids_built_from_prior_state(self, monkeypatch):
        captured: dict = {}

        def fake_fetch(username, app_password, *, skip_ids=None):
            captured["skip_ids"] = skip_ids
            captured["username"] = username
            captured["app_password"] = app_password
            return []
        monkeypatch.setattr(main_mod, "fetch_promotions", fake_fetch)
        main_mod._gmail_pipeline(
            _FAKE_CONFIG, {}, [],
            {"processed_ids": {"already_seen": self._NOW}},
            now_iso=self._NOW,
        )
        assert captured["skip_ids"] == {"already_seen"}
        assert captured["username"] == "user@gmail.com"
        assert captured["app_password"] == "abcd efgh ijkl mnop"


# ---------------------------------------------------------------------------
# _review_requests_pipeline — failure isolation + toggle gating
# ---------------------------------------------------------------------------

class TestReviewRequestsPipeline:
    _NOW = datetime(2026, 6, 7, 14, 0, 0, tzinfo=timezone.utc)

    def test_toggle_off_returns_empty_and_skips_fetch(self, monkeypatch):
        def boom(*a, **kw):  # must not be called when disabled
            raise AssertionError("fetch_review_requests called while disabled")
        monkeypatch.setattr(main_mod, "fetch_review_requests", boom)
        cfg = replace(_FAKE_CONFIG, review_requests_daily=False)
        assert main_mod._review_requests_pipeline(cfg, now=self._NOW) == ([], None)

    def test_fetch_failure_returns_empty(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("imap connection refused")
        monkeypatch.setattr(main_mod, "fetch_review_requests", boom)
        assert main_mod._review_requests_pipeline(_FAKE_CONFIG, now=self._NOW) == ([], None)

    def test_happy_path_dedupes_and_returns_all_url(self, monkeypatch):
        # Two reminders for the same order — collapse to the most recent.
        emails = [
            {"id": "1", "from": "Suzushii Clothing <no-reply@loox.io>",
             "subject": "Reminder: Order #138880, how did it go?",
             "body_text": "", "message_id": "<a@loox.io>",
             "date": "Tue, 02 Jun 2026 19:32:00 +0000"},
            {"id": "2", "from": "Suzushii Clothing <no-reply@loox.io>",
             "subject": "Order #138880, how did it go?",
             "body_text": "", "message_id": "<b@loox.io>",
             "date": "Wed, 27 May 2026 10:00:00 +0000"},
        ]

        def fake_fetch(username, app_password, *, days=None):
            assert days == _FAKE_CONFIG.review_requests_days
            return emails
        monkeypatch.setattr(main_mod, "fetch_review_requests", fake_fetch)

        requests, all_url = main_mod._review_requests_pipeline(
            _FAKE_CONFIG, now=self._NOW,
        )
        assert len(requests) == 1
        assert requests[0]["shop"] == "Suzushii Clothing"
        # Most-recent reminder (Jun 02, X-GM-MSGID "1") wins → direct-open link.
        assert requests[0]["date_iso"] == "2026-06-02"
        assert requests[0]["url"] == "https://mail.google.com/mail/u/0/#all/1"
        assert all_url == main_mod.review_requests.all_requests_url()


# ---------------------------------------------------------------------------
# _restock_emails_pipeline — failure isolation + toggle gating
# ---------------------------------------------------------------------------

class TestRestockEmailsPipeline:
    _NOW = datetime(2026, 6, 13, 14, 0, 0, tzinfo=timezone.utc)

    def test_toggle_off_returns_empty_and_skips_fetch(self, monkeypatch):
        def boom(*a, **kw):
            raise AssertionError("fetch_restock_emails called while disabled")
        monkeypatch.setattr(main_mod, "fetch_restock_emails", boom)
        cfg = replace(_FAKE_CONFIG, restock_emails_daily=False)
        assert main_mod._restock_emails_pipeline(cfg, now=self._NOW) == ([], None)

    def test_fetch_failure_returns_empty(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("imap connection refused")
        monkeypatch.setattr(main_mod, "fetch_restock_emails", boom)
        assert main_mod._restock_emails_pipeline(_FAKE_CONFIG, now=self._NOW) == ([], None)

    def test_happy_path_dedupes_and_returns_all_url(self, monkeypatch):
        emails = [
            {"id": "1", "from": "Norse Projects <no-reply@klaviyo.com>",
             "subject": "Aros Chino is back in stock in size M",
             "body_text": "", "message_id": "<a@klaviyo.com>",
             "date": "Sat, 13 Jun 2026 09:00:00 +0000"},
        ]

        def fake_fetch(username, app_password, *, days=None):
            assert days == _FAKE_CONFIG.restock_email_days
            return emails
        monkeypatch.setattr(main_mod, "fetch_restock_emails", fake_fetch)

        restocks, all_url = main_mod._restock_emails_pipeline(
            _FAKE_CONFIG, now=self._NOW,
        )
        assert len(restocks) == 1
        assert restocks[0]["shop"] == "Norse Projects"
        assert restocks[0]["item"] == "Aros Chino"
        assert restocks[0]["size"] == "M"
        assert all_url == main_mod.restock_emails.all_restocks_url()


# ---------------------------------------------------------------------------
# _voice_pipeline — failure isolation (mirrors TestGmailPipeline)
# ---------------------------------------------------------------------------

class TestVoicePipeline:
    _NOW = "2026-05-21T14:00:00+00:00"

    def test_imap_failure_returns_empty(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("imap connection refused")
        monkeypatch.setattr(main_mod, "fetch_voice_sms", boom)
        out = main_mod._voice_pipeline(
            _FAKE_CONFIG, sms_aliases={}, known_shops=[],
            prior_voice_state={"processed_ids": {"old": self._NOW}},
            now_iso=self._NOW,
        )
        assert out["codes"] == []
        assert out["sale_signals"] == []
        # State carried over unchanged so dedup info isn't lost.
        assert out["voice_state_out"] == {"processed_ids": {"old": self._NOW}}

    def test_happy_path_threads_signals_through(self, monkeypatch):
        monkeypatch.setattr(
            main_mod, "fetch_voice_sms",
            lambda username, app_password, *, label="GoogleVoice", skip_ids=None: [
                {"id": "s1", "from": "x", "subject": "x",
                 "sms_from_number": "+18885557700",
                 "sms_body": "Aniqi: Use code SMS25 for 25% off",
                 "date": ""},
            ],
        )
        out = main_mod._voice_pipeline(
            _FAKE_CONFIG, {"+18885557700": "Aniqi"}, ["Aniqi"],
            prior_voice_state={"processed_ids": {}},
            now_iso=self._NOW,
        )
        assert len(out["codes"]) == 1
        assert out["codes"][0]["shop"] == "Aniqi"
        assert out["codes"][0]["source"] == "sms"
        assert "s1" in out["voice_state_out"]["processed_ids"]
        assert out["voice_state_out"]["processed_ids"]["s1"] == self._NOW

    def test_skip_ids_and_label_threaded_through(self, monkeypatch):
        captured: dict = {}

        def fake_fetch(username, app_password, *, label="GoogleVoice", skip_ids=None):
            captured["skip_ids"] = skip_ids
            captured["label"] = label
            captured["username"] = username
            captured["app_password"] = app_password
            return []
        monkeypatch.setattr(main_mod, "fetch_voice_sms", fake_fetch)
        main_mod._voice_pipeline(
            _FAKE_CONFIG, {}, [],
            {"processed_ids": {"already_seen": self._NOW}},
            now_iso=self._NOW,
            label="CustomLabel",
        )
        assert captured["skip_ids"] == {"already_seen"}
        assert captured["label"] == "CustomLabel"
        assert captured["username"] == "user@gmail.com"
        assert captured["app_password"] == "abcd efgh ijkl mnop"


# ---------------------------------------------------------------------------
# Fit-feedback nudge — link data + weekly email gating
# ---------------------------------------------------------------------------

from dataclasses import replace as _replace
from urllib.parse import parse_qs, urlparse

from src.fit_links import verify as _verify

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _today_code() -> str:
    return datetime.now(timezone.utc).strftime("%a").lower()


def _configured(**over) -> Config:
    return _replace(
        _FAKE_CONFIG,
        fit_form_base_url="https://form.example/exec",
        fit_link_secret="sec",
        **over,
    )


class TestFitFeedbackData:
    def _wardrobe(self):
        return {"items": [
            {"id": "abc", "item_name": "Tee", "shop": "S", "size": "M",
             "color": "Black", "fit_review": None},
            {"id": "done", "item_name": "Cap", "fit_review": {"fit": "tts"}},
        ]}

    def test_dormant_when_unconfigured(self):
        render, all_url = main_mod._fit_feedback_data(self._wardrobe(), _FAKE_CONFIG)
        assert render == [] and all_url is None

    def test_empty_when_nothing_pending(self):
        wardrobe = {"items": [{"id": "x", "fit_review": {"fit": "tts"}}]}
        render, all_url = main_mod._fit_feedback_data(wardrobe, _configured())
        assert render == [] and all_url is None

    def test_builds_signed_links_for_pending(self):
        render, all_url = main_mod._fit_feedback_data(self._wardrobe(), _configured())
        assert [r["name"] for r in render] == ["Tee"]  # only the pending item
        r = render[0]
        assert r["shop"] == "S" and r["size"] == "M" and r["color"] == "Black"
        sig = parse_qs(urlparse(r["url"]).query)["sig"][0]
        assert _verify("abc", sig, "sec") is True
        assert all_url is not None

    def test_pending_sorted_newest_first(self):
        wardrobe = {"items": [
            {"id": "old", "item_name": "Old", "purchased_at": "2025-01-02",
             "fit_review": None},
            {"id": "new", "item_name": "New", "purchased_at": "2026-05-30",
             "fit_review": None},
            {"id": "mid", "item_name": "Mid", "purchased_at": "2025-12-01",
             "fit_review": None},
            {"id": "undated", "item_name": "Undated", "fit_review": None},
        ]}
        render, _ = main_mod._fit_feedback_data(wardrobe, _configured())
        # Newest purchase first; the undated item sorts last.
        assert [r["name"] for r in render] == ["New", "Mid", "Old", "Undated"]


class TestWeeklyFitEmail:
    def _pending(self):
        return [{"name": "Tee", "shop": "S", "size": "M", "color": None,
                 "url": "https://form.example/exec?item=abc&sig=x"}]

    def test_sends_on_matching_day(self, monkeypatch):
        sent = {}

        def fake(api_key, frm, to, subject, body):
            sent.update(subject=subject, body=body)
            return "id"

        monkeypatch.setattr(main_mod, "send_email", fake)
        cfg = _configured(fit_feedback_weekly=True, fit_feedback_weekly_day=_today_code())
        main_mod._maybe_send_weekly_fit_email(cfg, self._pending(), "all", dry_run=False)
        assert "Fit feedback" in sent["subject"]
        assert "Tee" in sent["body"]

    def test_no_send_on_other_day(self, monkeypatch):
        called = []
        monkeypatch.setattr(main_mod, "send_email", lambda *a, **k: called.append(1))
        other = next(d for d in _WEEKDAYS if d != _today_code())
        cfg = _configured(fit_feedback_weekly=True, fit_feedback_weekly_day=other)
        main_mod._maybe_send_weekly_fit_email(cfg, self._pending(), None, dry_run=False)
        assert called == []

    def test_no_send_when_disabled(self, monkeypatch):
        called = []
        monkeypatch.setattr(main_mod, "send_email", lambda *a, **k: called.append(1))
        cfg = _configured(fit_feedback_weekly=False, fit_feedback_weekly_day=_today_code())
        main_mod._maybe_send_weekly_fit_email(cfg, self._pending(), None, dry_run=False)
        assert called == []

    def test_no_send_when_nothing_pending(self, monkeypatch):
        called = []
        monkeypatch.setattr(main_mod, "send_email", lambda *a, **k: called.append(1))
        cfg = _configured(fit_feedback_weekly=True, fit_feedback_weekly_day=_today_code())
        main_mod._maybe_send_weekly_fit_email(cfg, [], None, dry_run=False)
        assert called == []

    def test_dry_run_does_not_send(self, monkeypatch, tmp_path):
        called = []
        monkeypatch.setattr(main_mod, "send_email", lambda *a, **k: called.append(1))
        monkeypatch.chdir(tmp_path)  # fit_digest.md lands in the temp dir, not cwd
        cfg = _configured(fit_feedback_weekly=True, fit_feedback_weekly_day=_today_code())
        main_mod._maybe_send_weekly_fit_email(cfg, self._pending(), None, dry_run=True)
        assert called == []
        assert (tmp_path / "fit_digest.md").exists()


# ---------------------------------------------------------------------------
# Weekly-ish BodySpec scan-cache refresh (age-gated, failure-isolated)
# ---------------------------------------------------------------------------

class TestBodyScansStale:
    def test_missing_or_empty_is_stale(self):
        assert main_mod._body_scans_stale(None, 7) is True
        assert main_mod._body_scans_stale({}, 7) is True
        assert main_mod._body_scans_stale({"scans": []}, 7) is True

    def test_undated_is_stale(self):
        assert main_mod._body_scans_stale({"scans": [{"result_id": "a"}]}, 7) is True

    def test_recent_is_fresh(self):
        recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        cache = {"refreshed_at": recent, "scans": [{"result_id": "a"}]}
        assert main_mod._body_scans_stale(cache, 7) is False

    def test_recently_refreshed_empty_cache_is_fresh(self):
        # Zero-scan account: a freshly written but empty cache must NOT be treated
        # as missing, or the cron would re-pull BodySpec every day.
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        assert main_mod._body_scans_stale({"refreshed_at": recent, "scans": []}, 7) is False

    def test_old_is_stale(self):
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        cache = {"refreshed_at": old, "scans": [{"result_id": "a"}]}
        assert main_mod._body_scans_stale(cache, 7) is True


class TestMaybeRefreshBodyScans:
    def _cfg(self, **over):
        return _replace(_FAKE_CONFIG, bodyspec_username="u", bodyspec_password="p",
                        body_scan_max_age_days=7, **over)

    def test_blank_creds_returns_none(self, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("must not authenticate without creds")
        monkeypatch.setattr(main_mod.bodyspec, "authenticate", _boom)
        # _FAKE_CONFIG has blank bodyspec creds.
        assert main_mod._maybe_refresh_body_scans(_FAKE_CONFIG, {}, dry_run=False) is None

    def test_dry_run_skips(self, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("dry-run must not authenticate")
        monkeypatch.setattr(main_mod.bodyspec, "authenticate", _boom)
        assert main_mod._maybe_refresh_body_scans(self._cfg(), {}, dry_run=True) is None

    def test_empty_cache_triggers_refresh(self, monkeypatch):
        fresh = {"refreshed_at": "now", "scans": [{"result_id": "a"}]}
        monkeypatch.setattr(main_mod.bodyspec, "authenticate", lambda u, p: "tok")
        monkeypatch.setattr(main_mod.bodyspec, "build_scan_cache", lambda tok: fresh)
        out = main_mod._maybe_refresh_body_scans(self._cfg(), {}, dry_run=False)
        assert out == fresh

    def test_fresh_cache_skips_refresh(self, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("fresh cache must not refresh")
        monkeypatch.setattr(main_mod.bodyspec, "authenticate", _boom)
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        cache = {"refreshed_at": recent, "scans": [{"result_id": "a"}]}
        assert main_mod._maybe_refresh_body_scans(self._cfg(), cache, dry_run=False) is None

    def test_fresh_but_empty_cache_skips_refresh(self, monkeypatch):
        # Zero-scan account that was refreshed recently must not re-auth daily.
        def _boom(*a, **k):
            raise AssertionError("fresh empty cache must not refresh")
        monkeypatch.setattr(main_mod.bodyspec, "authenticate", _boom)
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        cache = {"refreshed_at": recent, "scans": []}
        assert main_mod._maybe_refresh_body_scans(self._cfg(), cache, dry_run=False) is None

    def test_stale_cache_triggers_refresh(self, monkeypatch):
        fresh = {"refreshed_at": "now", "scans": [{"result_id": "b"}]}
        monkeypatch.setattr(main_mod.bodyspec, "authenticate", lambda u, p: "tok")
        monkeypatch.setattr(main_mod.bodyspec, "build_scan_cache", lambda tok: fresh)
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        cache = {"refreshed_at": old, "scans": [{"result_id": "a"}]}
        assert main_mod._maybe_refresh_body_scans(self._cfg(), cache, dry_run=False) == fresh

    def test_bodyspec_error_is_isolated(self, monkeypatch):
        def _raise(u, p):
            raise RuntimeError("bodyspec down")
        monkeypatch.setattr(main_mod.bodyspec, "authenticate", _raise)
        # A BodySpec outage must not propagate — returns None, leaving the cache as-is.
        assert main_mod._maybe_refresh_body_scans(self._cfg(), {}, dry_run=False) is None
