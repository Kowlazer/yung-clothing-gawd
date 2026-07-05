"""Tests for src/claude_fuzzy.py.

Strategy:
  * Candidate gathering uses httpx — stubbed with pytest-httpx (httpx_mock).
  * The Anthropic call uses a hand-rolled FakeAnthropicClient so we never
    touch the real API. The fake exposes a `.messages.create(...)` that
    records its kwargs and returns a configurable response object.
  * `_homepage_excerpt`, `_unwrap_ddg`, `_usage_dict` are pure helpers and
    are tested directly.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest

from src import claude_fuzzy
from src.claude_fuzzy import (
    SYSTEM_PROMPT,
    TOOL_SCHEMA,
    _HOMEPAGE_TEXT_LIMIT,
    _build_payload,
    _ddg_search,
    _empty_result,
    _homepage_excerpt,
    _onsite_search,
    _unwrap_ddg,
    _usage_dict,
    resolve_fuzzy,
)


# ---------------------------------------------------------------------------
# FakeAnthropicClient — records call kwargs, returns a scripted response
# ---------------------------------------------------------------------------

class _FakeMessages:
    def __init__(self, response):
        self._response = response
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


class FakeAnthropicClient:
    """Stand-in for anthropic.Anthropic with the .messages.create(...) shape."""

    def __init__(self, tool_input: dict, usage: dict | None = None,
                 stop_reason: str | None = None):
        content = [SimpleNamespace(
            type="tool_use",
            name="submit_results",
            input=tool_input,
        )]
        response = SimpleNamespace(
            content=content,
            usage=SimpleNamespace(**usage) if usage else None,
            stop_reason=stop_reason,
        )
        self.messages = _FakeMessages(response)


def _ok_tool_input(shop_sales=None, resolutions=None, loose_matches=None, email_sales=None):
    return {
        "shop_sales":    shop_sales or [],
        "resolutions":   resolutions or [],
        "loose_matches": loose_matches or [],
        "email_sales":   email_sales or [],
    }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestHomepageExcerpt:
    def test_strips_scripts_and_styles(self):
        html = """
        <html><head>
        <style>body { color: red; }</style>
        <script>alert('x')</script>
        </head><body>
        <p>Hello world</p>
        </body></html>
        """
        out = _homepage_excerpt(html)
        assert "Hello world" in out
        assert "alert" not in out
        assert "color: red" not in out

    def test_collapses_whitespace(self):
        html = "<body><p>a\n\n\n   b\t\tc</p></body>"
        assert _homepage_excerpt(html) == "a b c"

    def test_truncates_to_limit(self):
        big = "<body>" + ("x " * 5000) + "</body>"
        out = _homepage_excerpt(big)
        assert out.endswith("...[truncated]")
        # No sale signal past the head → body capped at the limit; only the
        # marker is appended past it (no promo windows).
        assert len(out) == _HOMEPAGE_TEXT_LIMIT + len(" ...[truncated]")

    def test_handles_no_body(self):
        # Malformed HTML still produces something rather than crashing.
        out = _homepage_excerpt("just text, no tags")
        assert "just text" in out


class TestPromoWindows:
    """Cost lever #4: sale signals past the head slice are appended as small
    context windows instead of being lost to a flat truncation."""

    @staticmethod
    def _page(tail: str) -> str:
        filler = "x " * ((_HOMEPAGE_TEXT_LIMIT + 400) // 2)
        return f"<body>{filler}{tail}</body>"

    def test_signal_past_head_is_kept(self):
        out = _homepage_excerpt(self._page(
            "mid page hero banner 30% off everything this week only"))
        assert "[sale mentions further down the page:]" in out
        assert "30% off everything" in out
        # And the pre-filter now sees it — under the old flat slice this page
        # would have been wrongly recorded "no".
        assert claude_fuzzy._has_sale_signal(out)

    def test_window_carries_surrounding_context(self):
        out = _homepage_excerpt(self._page(
            "SUMMER EVENT our biggest clearance yet ends Sunday midnight"))
        # Not just the bare lexeme — the neighbouring words come along so
        # Claude can actually judge the mention.
        assert "biggest clearance yet ends Sunday" in out

    def test_no_windows_when_signals_only_in_head(self):
        html = ("<body>Big sale 30% off today "
                + "x " * ((_HOMEPAGE_TEXT_LIMIT + 400) // 2) + "</body>")
        out = _homepage_excerpt(html)
        assert "[sale mentions further down the page:]" not in out
        assert out.endswith("...[truncated]")

    def test_adjacent_signals_merge_into_one_window(self):
        out = _homepage_excerpt(self._page(
            "sale sale sale 20% off use code SAVE20 discount deals"))
        assert out.count("[sale mentions further down the page:]") == 1
        # One merged window, not one snippet per lexeme (no " ... " joins).
        tail = out.split("[sale mentions further down the page:]")[1]
        assert " ... " not in tail

    def test_total_window_budget_is_capped(self):
        # Many far-apart signals: appended windows must respect the cap.
        tail = ("y " * 400).join(f"promo {i} 30% off" for i in range(20))
        out = _homepage_excerpt(self._page(tail))
        appended = out.split("[sale mentions further down the page:]")[1]
        assert len(appended) <= claude_fuzzy._PROMO_WINDOWS_LIMIT + 200

    def test_boundary_straddling_signal_survives(self):
        # Place a signal exactly across the head cut so the head slice holds
        # only its first half — the window must still carry it whole.
        filler = "x" * (_HOMEPAGE_TEXT_LIMIT - 4)
        out = _homepage_excerpt(f"<body>{filler} 30% off sitewide now</body>")
        assert "30% off sitewide" in out
        assert claude_fuzzy._has_sale_signal(out)

    def test_short_pages_unchanged(self):
        out = _homepage_excerpt("<body>Spring sale 20% off</body>")
        assert out == "Spring sale 20% off"
        assert "[sale mentions" not in out


class TestHasSaleSignal:
    """The deterministic pre-filter (cost lever #1): must flag every sale-ish
    lexeme the SYSTEM_PROMPT treats as a signal, and must NOT flag bare
    percentages ('100% cotton') or non-promotional banners."""

    @pytest.mark.parametrize("text", [
        "Spring Sale — 30% off everything",
        "Take 30% off with code SPRING30",
        "BOGO on all tees",
        "Year-round clearance section",
        "Save $20 today only",
        "Sale",
        "Use code WELCOME10",
        "Flash sale ends Sunday",
        "Shop the outlet",
        "Up to 50% off sitewide",
        "Everything discounted this week",
        "Grab this deal",
    ])
    def test_positive(self, text):
        assert claude_fuzzy._has_sale_signal(text)

    @pytest.mark.parametrize("text", [
        "New arrivals just dropped",
        "Free shipping on all orders",
        "Free returns within 30 days",
        "100% organic cotton basics",   # bare percentage must NOT trigger
        "Our story, lookbook and journal",
        "Sign up for our newsletter",
        "",
    ])
    def test_negative(self, text):
        assert not claude_fuzzy._has_sale_signal(text)


class TestUnwrapDDG:
    def test_passthrough_when_not_redirect(self):
        assert _unwrap_ddg("https://example.com") == "https://example.com"

    def test_extracts_uddg_param(self):
        wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Faniqi.com&rut=abc"
        assert _unwrap_ddg(wrapped) == "https://aniqi.com"


class TestUsageDict:
    def test_none_in_none_out(self):
        assert _usage_dict(None) is None

    def test_extracts_known_fields(self):
        u = SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=80,
            cache_creation_input_tokens=0,
        )
        d = _usage_dict(u)
        assert d == {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 80,
            "cache_creation_input_tokens": 0,
        }

    def test_skips_missing_fields(self):
        u = SimpleNamespace(input_tokens=10, output_tokens=5)
        d = _usage_dict(u)
        assert d == {"input_tokens": 10, "output_tokens": 5}


class TestBuildPayload:
    def test_round_trips_through_json(self):
        s = _build_payload(
            [{"id": "shop_0", "shop": "X", "url": "https://x.com",
              "html_excerpt": "hello"}],
            [{"id": "resolve_0", "shop_name": "Y", "candidates": []}],
            [{"id": "loose_0", "mention": "Z hat", "shop": "Y",
              "shop_domain": "y.com", "candidates": []}],
            [{"id": "email_0", "email_id": "m1", "shop": "X",
              "subject": "Sale!", "body_excerpt": "30% off"}],
        )
        parsed = json.loads(s)
        assert {"shop_homepage_tasks", "shop_resolve_tasks",
                "loose_mention_tasks", "email_sales_tasks"} <= parsed.keys()
        assert parsed["shop_homepage_tasks"][0]["shop"] == "X"
        assert parsed["email_sales_tasks"][0]["subject"] == "Sale!"


# ---------------------------------------------------------------------------
# DDG search candidate gathering
# ---------------------------------------------------------------------------

class TestDDGSearch:
    def test_parses_results(self, httpx_mock):
        html = """
        <html><body>
          <div class="result">
            <a class="result__a" href="https://example.com/">Example Store</a>
            <a class="result__snippet">Buy shirts here.</a>
          </div>
          <div class="result">
            <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fother.com">Other</a>
          </div>
        </body></html>
        """
        httpx_mock.add_response(
            url="https://html.duckduckgo.com/html/",
            method="POST", text=html,
        )
        results = _ddg_search("example store")
        assert len(results) == 2
        assert results[0]["url"] == "https://example.com/"
        assert results[0]["title"] == "Example Store"
        assert results[0]["snippet"] == "Buy shirts here."
        # Second result should have been unwrapped.
        assert results[1]["url"] == "https://other.com"

    def test_empty_results_on_4xx(self, httpx_mock):
        httpx_mock.add_response(
            url="https://html.duckduckgo.com/html/",
            method="POST", status_code=429, text="rate-limited",
        )
        assert _ddg_search("anything") == []

    def test_empty_results_on_network_error(self, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectError("boom"))
        assert _ddg_search("anything") == []

    def test_limits_to_five(self, httpx_mock):
        rows = "".join(
            f'<div class="result"><a class="result__a" href="https://s{i}.com">S{i}</a></div>'
            for i in range(10)
        )
        httpx_mock.add_response(
            url="https://html.duckduckgo.com/html/",
            method="POST", text=f"<html><body>{rows}</body></html>",
        )
        assert len(_ddg_search("x")) == 5


# ---------------------------------------------------------------------------
# On-site search
# ---------------------------------------------------------------------------

class TestOnsiteSearch:
    def test_finds_product_links(self, httpx_mock):
        html = """
        <html><body>
          <a href="/products/trafalgar-joggers">TRAFALGAR JOGGERS</a>
          <a href="/products/law-pants">LAW PANTS</a>
          <a href="/collections/all">All products</a>
          <a href="/products/trafalgar-joggers?variant=1">Dupe link</a>
        </body></html>
        """
        httpx_mock.add_response(
            url="https://aniqi.com/search?q=trafalgar",
            text=html,
        )
        results = _onsite_search("aniqi.com", "trafalgar")
        urls = [r["url"] for r in results]
        # Two unique product URLs, dupe deduped, /collections/ ignored.
        assert "https://aniqi.com/products/trafalgar-joggers" in urls
        assert "https://aniqi.com/products/law-pants" in urls
        assert "https://aniqi.com/collections/all" not in urls
        assert len(urls) == 2

    def test_falls_back_to_image_alt_for_empty_anchors(self, httpx_mock):
        html = """
        <html><body>
          <a href="/products/silent-anchor"><img alt="Silent Anchor Tee"/></a>
        </body></html>
        """
        httpx_mock.add_response(
            url="https://shop.example.com/search?q=anything",
            text=html,
        )
        results = _onsite_search("shop.example.com", "anything")
        assert results[0]["title"] == "Silent Anchor Tee"

    def test_empty_on_fetch_failure(self, httpx_mock):
        httpx_mock.add_response(
            url="https://shop.example.com/search?q=q",
            status_code=500,
        )
        assert _onsite_search("shop.example.com", "q") == []

    def test_adds_https_when_missing(self, httpx_mock):
        httpx_mock.add_response(
            url="https://shop.example.com/search?q=q",
            text="<html></html>",
        )
        # Bare domain → should be promoted to https://
        _onsite_search("shop.example.com", "q")
        # No assertion error means the request URL matched the mock.

    def test_trailing_slash_in_shop_domain_does_not_create_double_slash(self, httpx_mock):
        """Regression: aliases stored with trailing slashes (e.g. from
        DDG-resolved URLs) used to produce ``//search`` which 301s on Shopify
        and then 404s. Strip trailing slashes before appending /search."""
        httpx_mock.add_response(
            url="https://shop.example.com/search?q=q",
            text="<html></html>",
        )
        _onsite_search("https://shop.example.com/", "q")
        # If the rstrip didn't happen, httpx_mock would raise on the //search URL.


# ---------------------------------------------------------------------------
# resolve_fuzzy
# ---------------------------------------------------------------------------

class TestResolveFuzzyShortCircuit:
    def test_empty_inputs_no_api_call(self):
        # Pass a fake client so any accidental call would surface.
        fake = FakeAnthropicClient(_ok_tool_input())
        result = resolve_fuzzy([], [], [], client=fake)
        assert result == _empty_result()
        assert fake.messages.last_kwargs is None

    def test_no_candidates_gathered_skips_api(self, httpx_mock):
        # Homepage fetch fails → skipped_shop_sales (could not fetch)
        # DDG returns empty → unresolved
        # On-site returns empty → skipped_loose
        httpx_mock.add_response(url="https://x.com", status_code=500)
        httpx_mock.add_response(
            url="https://html.duckduckgo.com/html/",
            method="POST", text="<html><body></body></html>",
        )
        httpx_mock.add_response(
            url="https://y.com/search?q=hat",
            text="<html><body></body></html>",
        )

        fake = FakeAnthropicClient(_ok_tool_input())
        result = resolve_fuzzy(
            shops_to_check=[{"shop": "X", "url": "https://x.com"}],
            shops_to_resolve=["Y"],
            loose_mentions=[{"mention": "hat", "shop": "Y",
                             "shop_domain": "y.com"}],
            client=fake,
        )

        assert fake.messages.last_kwargs is None
        assert result["unresolved"] == ["Y"]
        assert result["shop_sales"][0]["status"] == "unclear"
        assert result["shop_sales"][0]["description"] == "could not fetch homepage"
        assert result["loose_matches"][0]["matched_url"] is None
        assert result["loose_matches"][0]["confidence"] == "none"


class TestSaleSignalPrefilter:
    """Cost lever #1: homepages with no sale lexeme are recorded "no" locally
    and never reach Claude; only signal-bearing homepages are sent."""

    def test_no_signal_homepage_skips_api(self, httpx_mock):
        httpx_mock.add_response(
            url="https://plain.com",
            text="<html><body>New arrivals. Free shipping on orders over $50.</body></html>",
        )
        fake = FakeAnthropicClient(_ok_tool_input())
        result = resolve_fuzzy(
            shops_to_check=[{"shop": "Plain", "url": "https://plain.com"}],
            shops_to_resolve=[], loose_mentions=[], client=fake,
        )
        # No sale lexeme → no API call; recorded "no" locally.
        assert fake.messages.last_kwargs is None
        assert result["shop_sales"] == [
            {"shop": "Plain", "status": "no", "description": None}
        ]

    def test_only_signal_shops_sent_to_claude(self, httpx_mock):
        httpx_mock.add_response(
            url="https://plain.com",
            text="<html><body>Just our lookbook and new arrivals.</body></html>",
        )
        httpx_mock.add_response(
            url="https://saleshop.com",
            text="<html><body>SPRING SALE 30% off sitewide!</body></html>",
        )
        fake = FakeAnthropicClient(_ok_tool_input(shop_sales=[{
            "id": "shop_1", "shop": "SaleShop", "status": "yes",
            "description": "30% off sitewide",
        }]))
        result = resolve_fuzzy(
            shops_to_check=[
                {"shop": "Plain", "url": "https://plain.com"},
                {"shop": "SaleShop", "url": "https://saleshop.com"},
            ],
            shops_to_resolve=[], loose_mentions=[], client=fake,
        )
        # Only the signal-bearing shop reached Claude...
        body = json.loads(fake.messages.last_kwargs["messages"][0]["content"])
        assert [t["shop"] for t in body["shop_homepage_tasks"]] == ["SaleShop"]
        # ...but both shops appear in the result: Plain "no" (local), SaleShop "yes".
        assert {s["shop"]: s["status"] for s in result["shop_sales"]} == {
            "Plain": "no", "SaleShop": "yes",
        }


class TestHomepageProxyFallback:
    """Issues #1/#2: a Cloudflare-style block (403/503) on a shop homepage —
    which 403s the datacenter IP while serving residential IPs fine — is retried
    through the reader proxy, recovering the page's visible text so the
    sale-check still gets a signal instead of resolving to "could not fetch
    homepage". Non-block failures (404, etc.) and a disabled toggle do NOT hit
    the proxy."""

    _PROXY = claude_fuzzy.extract._READER_PROXY + "https://blocked.com"

    def test_block_recovered_via_reader_proxy(self, httpx_mock):
        httpx_mock.add_response(url="https://blocked.com", status_code=403)
        httpx_mock.add_response(
            url=self._PROXY,
            json={"code": 200, "data": {"text": "SPRING SALE 30% off sitewide"}},
        )
        text = claude_fuzzy._fetch_homepage("https://blocked.com")
        assert text is not None and "30% off" in text

    def test_404_does_not_hit_proxy(self, httpx_mock):
        # No proxy response is registered: httpx_mock raises if one is requested.
        httpx_mock.add_response(url="https://gone.com", status_code=404)
        assert claude_fuzzy._fetch_homepage("https://gone.com") is None

    def test_proxy_miss_falls_back_to_none(self, httpx_mock):
        httpx_mock.add_response(url="https://blocked.com", status_code=403)
        httpx_mock.add_response(url=self._PROXY, status_code=500)
        assert claude_fuzzy._fetch_homepage("https://blocked.com") is None

    def test_toggle_off_skips_proxy(self, httpx_mock, monkeypatch):
        monkeypatch.setattr("src.extract._PROXY_FALLBACK_ENABLED", False)
        httpx_mock.add_response(url="https://blocked.com", status_code=403)
        assert claude_fuzzy._fetch_homepage("https://blocked.com") is None

    def test_recovered_homepage_reaches_claude(self, httpx_mock):
        # End-to-end through resolve_fuzzy: a blocked homepage whose proxied text
        # carries a sale signal is judged by Claude like any other (no longer
        # dropped as "could not fetch homepage").
        httpx_mock.add_response(url="https://blocked.com", status_code=403)
        httpx_mock.add_response(
            url=self._PROXY,
            json={"data": {"text": "SPRING SALE 30% off sitewide!"}},
        )
        fake = FakeAnthropicClient(_ok_tool_input(shop_sales=[{
            "id": "shop_0", "shop": "Blocked", "status": "yes",
            "description": "30% off sitewide",
        }]))
        result = resolve_fuzzy(
            shops_to_check=[{"shop": "Blocked", "url": "https://blocked.com"}],
            shops_to_resolve=[], loose_mentions=[], client=fake,
        )
        body = json.loads(fake.messages.last_kwargs["messages"][0]["content"])
        assert [t["shop"] for t in body["shop_homepage_tasks"]] == ["Blocked"]
        assert result["shop_sales"][0]["status"] == "yes"


class TestVerdictCache:
    """Cost lever #3: a signal-bearing homepage whose sale-signal hash matches a
    still-fresh cached verdict is reused locally instead of re-sent to Claude; a
    changed, stale, or absent hash falls through to a fresh Claude judgement."""

    _SALE_HTML = "<html><body>SPRING SALE 30% off sitewide! Shop now.</body></html>"
    _TODAY = date(2026, 6, 9)

    def _cache_entry(self, html, shop, *, status="yes", description="30% off",
                     age_days=0):
        excerpt = claude_fuzzy._homepage_excerpt(html)
        checked = (self._TODAY - timedelta(days=age_days)).isoformat()
        return {"shop": shop, "hash": claude_fuzzy._verdict_hash(excerpt),
                "status": status, "description": description,
                "checked_at": checked}

    def test_fresh_hash_match_skips_api(self, httpx_mock):
        httpx_mock.add_response(url="https://aniqi.com", text=self._SALE_HTML)
        fake = FakeAnthropicClient(_ok_tool_input())
        prior = [self._cache_entry(self._SALE_HTML, "Aniqi",
                                   description="30% off sitewide")]
        result = resolve_fuzzy(
            [{"shop": "Aniqi", "url": "https://aniqi.com"}], [], [],
            client=fake, prior_verdicts=prior, today=self._TODAY,
        )
        # Cache hit → no Claude call; the cached verdict is reused verbatim.
        assert fake.messages.last_kwargs is None
        assert result["shop_sales"] == [
            {"shop": "Aniqi", "status": "yes", "description": "30% off sitewide"}
        ]
        # Nothing was judged this run → no fresh verdict to persist.
        assert result["shop_verdicts"] == []

    def test_changed_hash_sends_to_api(self, httpx_mock):
        httpx_mock.add_response(url="https://aniqi.com", text=self._SALE_HTML)
        fake = FakeAnthropicClient(_ok_tool_input(shop_sales=[{
            "id": "shop_0", "shop": "Aniqi", "status": "yes",
            "description": "30% off sitewide",
        }]))
        # Cached hash is for a *different* promo → miss → re-judged.
        prior = [{"shop": "Aniqi", "hash": "STALE_DIFFERENT_HASH",
                  "status": "no", "description": None,
                  "checked_at": self._TODAY.isoformat()}]
        result = resolve_fuzzy(
            [{"shop": "Aniqi", "url": "https://aniqi.com"}], [], [],
            client=fake, prior_verdicts=prior, today=self._TODAY,
        )
        assert fake.messages.last_kwargs is not None    # API was called
        assert {s["shop"]: s["status"] for s in result["shop_sales"]} == {"Aniqi": "yes"}

    def test_stale_entry_resent_even_on_hash_match(self, httpx_mock):
        httpx_mock.add_response(url="https://aniqi.com", text=self._SALE_HTML)
        fake = FakeAnthropicClient(_ok_tool_input(shop_sales=[{
            "id": "shop_0", "shop": "Aniqi", "status": "yes", "description": "x",
        }]))
        prior = [self._cache_entry(self._SALE_HTML, "Aniqi", age_days=8)]  # > 7d
        resolve_fuzzy(
            [{"shop": "Aniqi", "url": "https://aniqi.com"}], [], [],
            client=fake, prior_verdicts=prior, today=self._TODAY,
        )
        assert fake.messages.last_kwargs is not None    # ceiling forced a re-judge

    def test_fresh_verdicts_returned_for_judged_shops(self, httpx_mock):
        httpx_mock.add_response(url="https://aniqi.com", text=self._SALE_HTML)
        fake = FakeAnthropicClient(_ok_tool_input(shop_sales=[{
            "id": "shop_0", "shop": "Aniqi", "status": "yes",
            "description": "30% off sitewide",
        }]))
        result = resolve_fuzzy(
            [{"shop": "Aniqi", "url": "https://aniqi.com"}], [], [],
            client=fake, today=self._TODAY,   # no prior cache → everything judged
        )
        excerpt = claude_fuzzy._homepage_excerpt(self._SALE_HTML)
        assert result["shop_verdicts"] == [{
            "shop": "Aniqi",
            "hash": claude_fuzzy._verdict_hash(excerpt),
            "status": "yes",
            "description": "30% off sitewide",
        }]

    def test_no_signal_beats_stale_cache(self, httpx_mock):
        # A homepage that lost its sale text is recorded "no" by lever #1 before
        # the cache is even consulted — a stale "yes" entry can't resurrect it.
        httpx_mock.add_response(
            url="https://aniqi.com",
            text="<html><body>New arrivals. Free shipping.</body></html>",
        )
        fake = FakeAnthropicClient(_ok_tool_input())
        prior = [{"shop": "Aniqi", "hash": "WHATEVER", "status": "yes",
                  "description": "30% off", "checked_at": self._TODAY.isoformat()}]
        result = resolve_fuzzy(
            [{"shop": "Aniqi", "url": "https://aniqi.com"}], [], [],
            client=fake, prior_verdicts=prior, today=self._TODAY,
        )
        assert fake.messages.last_kwargs is None
        assert result["shop_sales"] == [
            {"shop": "Aniqi", "status": "no", "description": None}
        ]

    def test_volatile_text_does_not_bust_hash(self):
        # Same promo, different volatile junk (cart count, viewer count) → the
        # signal-substring hash is identical, so the cache still hits.
        a = "<html><body>3 items in cart. SPRING SALE 30% off! 12 viewing.</body></html>"
        b = "<html><body>0 items in cart. SPRING SALE 30% off! 47 viewing.</body></html>"
        ha = claude_fuzzy._verdict_hash(claude_fuzzy._homepage_excerpt(a))
        hb = claude_fuzzy._verdict_hash(claude_fuzzy._homepage_excerpt(b))
        assert ha == hb

    def test_changed_promo_changes_hash(self):
        a = "<html><body>SPRING SALE 30% off sitewide!</body></html>"
        b = "<html><body>SPRING SALE 50% off sitewide!</body></html>"
        ha = claude_fuzzy._verdict_hash(claude_fuzzy._homepage_excerpt(a))
        hb = claude_fuzzy._verdict_hash(claude_fuzzy._homepage_excerpt(b))
        assert ha != hb


class TestResolveFuzzyHappyPath:
    def test_full_flow(self, httpx_mock):
        # Homepage fetch
        httpx_mock.add_response(
            url="https://aniqi.com",
            text="<html><body>SPRING SALE 30% off everything!</body></html>",
        )
        # DDG search for "KillCrew official store"
        httpx_mock.add_response(
            url="https://html.duckduckgo.com/html/",
            method="POST",
            text=(
                '<html><body>'
                '<div class="result">'
                '<a class="result__a" href="https://killcrew.com">'
                'KillCrew Apparel</a>'
                '<a class="result__snippet">Official store</a>'
                '</div>'
                '</body></html>'
            ),
        )
        # On-site search for the loose mention
        httpx_mock.add_response(
            url="https://aniqi.com/search?q=Law+pants",
            text=(
                '<html><body>'
                '<a href="/products/trafalgar-joggers">Trafalgar Law Joggers</a>'
                '</body></html>'
            ),
        )

        # Scripted Claude response
        fake = FakeAnthropicClient(
            tool_input=_ok_tool_input(
                shop_sales=[{
                    "id": "shop_0", "shop": "Aniqi", "status": "yes",
                    "description": "30% off sitewide (Spring Sale)",
                }],
                resolutions=[{
                    "id": "resolve_0", "shop_name": "KillCrew",
                    "url": "https://killcrew.com", "confidence": "high",
                }],
                loose_matches=[{
                    "id": "loose_0", "mention": "Law pants", "shop": "Aniqi",
                    "matched_url": "https://aniqi.com/products/trafalgar-joggers",
                    "confidence": "low",
                }],
            ),
            usage={"input_tokens": 1500, "output_tokens": 80,
                   "cache_creation_input_tokens": 1200,
                   "cache_read_input_tokens": 0},
        )

        result = resolve_fuzzy(
            shops_to_check=[{"shop": "Aniqi", "url": "https://aniqi.com"}],
            shops_to_resolve=["KillCrew"],
            loose_mentions=[{"mention": "Law pants", "shop": "Aniqi",
                             "shop_domain": "aniqi.com"}],
            client=fake,
        )

        # IDs stripped from response items
        assert "id" not in result["shop_sales"][0]
        assert result["shop_sales"][0]["status"] == "yes"
        assert result["resolutions"][0]["url"] == "https://killcrew.com"
        assert result["loose_matches"][0]["matched_url"].endswith("trafalgar-joggers")
        assert result["unresolved"] == []
        assert result["usage"]["input_tokens"] == 1500
        assert result["usage"]["cache_creation_input_tokens"] == 1200

    def test_api_kwargs_use_cached_system_and_forced_tool(self, httpx_mock):
        httpx_mock.add_response(
            url="https://aniqi.com",
            text="<html><body>No sale today.</body></html>",
        )
        fake = FakeAnthropicClient(_ok_tool_input(
            shop_sales=[{"id": "shop_0", "shop": "Aniqi", "status": "no",
                         "description": None}],
        ))
        resolve_fuzzy(
            [{"shop": "Aniqi", "url": "https://aniqi.com"}], [], [], client=fake,
        )

        kw = fake.messages.last_kwargs
        assert kw is not None
        # System prompt is sent as a cache-controlled block.
        assert isinstance(kw["system"], list)
        assert kw["system"][0]["text"] == SYSTEM_PROMPT
        assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
        # Tool use is forced.
        assert kw["tool_choice"] == {"type": "tool", "name": "submit_results"}
        assert kw["tools"][0]["name"] == TOOL_SCHEMA["name"]
        # Payload is a single user message with JSON-shaped content.
        assert len(kw["messages"]) == 1
        body = json.loads(kw["messages"][0]["content"])
        assert body["shop_homepage_tasks"][0]["shop"] == "Aniqi"

    def test_mixed_some_fetch_others_succeed(self, httpx_mock):
        # First homepage 200, second 500.
        httpx_mock.add_response(
            url="https://aniqi.com",
            text="<html><body>SPRING SALE 30%!</body></html>",
        )
        httpx_mock.add_response(
            url="https://broken.com", status_code=500,
        )
        fake = FakeAnthropicClient(_ok_tool_input(
            shop_sales=[{
                "id": "shop_0", "shop": "Aniqi", "status": "yes",
                "description": "30% off",
            }],
        ))
        result = resolve_fuzzy(
            shops_to_check=[
                {"shop": "Aniqi", "url": "https://aniqi.com"},
                {"shop": "Broken", "url": "https://broken.com"},
            ],
            shops_to_resolve=[], loose_mentions=[], client=fake,
        )

        # Skipped shop appears first (it was prepended to API results).
        assert len(result["shop_sales"]) == 2
        statuses = {s["shop"]: s["status"] for s in result["shop_sales"]}
        assert statuses == {"Broken": "unclear", "Aniqi": "yes"}


class TestMaxTokensGuard:
    """A truncated (max_tokens) response should log a loud warning so silent
    JSON truncation never goes unnoticed; a normal stop should stay quiet."""

    def test_warns_when_response_truncated(self, httpx_mock, caplog):
        httpx_mock.add_response(
            url="https://aniqi.com",
            text="<html><body>SPRING SALE 30%!</body></html>",
        )
        fake = FakeAnthropicClient(
            _ok_tool_input(shop_sales=[{
                "id": "shop_0", "shop": "Aniqi", "status": "yes",
                "description": "30% off",
            }]),
            stop_reason="max_tokens",
        )
        with caplog.at_level(logging.WARNING, logger="src.claude_fuzzy"):
            resolve_fuzzy(
                [{"shop": "Aniqi", "url": "https://aniqi.com"}], [], [],
                client=fake,
            )
        assert any("max_tokens" in r.message for r in caplog.records)

    def test_no_warning_on_normal_stop(self, httpx_mock, caplog):
        httpx_mock.add_response(
            url="https://aniqi.com",
            text="<html><body>No sale today.</body></html>",
        )
        fake = FakeAnthropicClient(_ok_tool_input(shop_sales=[{
            "id": "shop_0", "shop": "Aniqi", "status": "no", "description": None,
        }]))
        with caplog.at_level(logging.WARNING, logger="src.claude_fuzzy"):
            resolve_fuzzy(
                [{"shop": "Aniqi", "url": "https://aniqi.com"}], [], [],
                client=fake,
            )
        assert not any("max_tokens" in r.message for r in caplog.records)


class TestResolveFuzzyEmailSignals:
    def test_email_only_triggers_api_call(self):
        """When all three traditional inputs are empty but email_signals is not,
        the API call still goes through (no candidate gathering needed)."""
        fake = FakeAnthropicClient(_ok_tool_input(
            email_sales=[{
                "id": "email_0", "email_id": "msg_abc", "shop": "Aniqi",
                "status": "yes",
                "description": "25% off ends Sunday, code SUMMER25",
            }],
        ))
        result = resolve_fuzzy(
            shops_to_check=[],
            shops_to_resolve=[],
            loose_mentions=[],
            email_signals=[{
                "email_id": "msg_abc", "shop": "Aniqi",
                "subject": "25% off this weekend",
                "body_excerpt": "Use SUMMER25 by Sunday.",
            }],
            client=fake,
        )

        assert fake.messages.last_kwargs is not None
        # ID stripped from response, but the email_id is preserved.
        assert "id" not in result["email_sales"][0]
        assert result["email_sales"][0]["email_id"] == "msg_abc"
        assert result["email_sales"][0]["status"] == "yes"

    def test_email_payload_shape_sent_to_claude(self):
        fake = FakeAnthropicClient(_ok_tool_input(email_sales=[]))
        resolve_fuzzy(
            [], [], [],
            email_signals=[{
                "email_id": "msg_1", "shop": "Aniqi",
                "subject": "Sale!", "body_excerpt": "snippet",
                "email_date": "2026-05-22",
            }],
            client=fake,
        )
        body = json.loads(fake.messages.last_kwargs["messages"][0]["content"])
        assert body["email_sales_tasks"][0]["email_id"] == "msg_1"
        assert body["email_sales_tasks"][0]["shop"] == "Aniqi"
        assert body["email_sales_tasks"][0]["body_excerpt"] == "snippet"
        # email_date threads through so Claude can resolve relative windows.
        assert body["email_sales_tasks"][0]["email_date"] == "2026-05-22"

    def test_email_date_defaults_blank_when_absent(self):
        fake = FakeAnthropicClient(_ok_tool_input(email_sales=[]))
        resolve_fuzzy(
            [], [], [],
            email_signals=[{
                "email_id": "msg_1", "shop": "Aniqi",
                "subject": "Sale!", "body_excerpt": "snippet",
            }],
            client=fake,
        )
        body = json.loads(fake.messages.last_kwargs["messages"][0]["content"])
        assert body["email_sales_tasks"][0]["email_date"] == ""

    def test_resolved_sale_window_passthrough(self):
        """starts_on / ends_on from the model survive into the result."""
        fake = FakeAnthropicClient(_ok_tool_input(email_sales=[{
            "id": "email_0", "email_id": "msg_abc", "shop": "Aniqi",
            "status": "yes", "description": "Memorial Day sale, 30% off",
            "starts_on": "2026-05-24", "ends_on": "2026-05-26",
        }]))
        result = resolve_fuzzy(
            [], [], [],
            email_signals=[{
                "email_id": "msg_abc", "shop": "Aniqi",
                "subject": "Coming soon", "body_excerpt": "starts Monday",
                "email_date": "2026-05-22",
            }],
            client=fake,
        )
        es = result["email_sales"][0]
        assert es["starts_on"] == "2026-05-24"
        assert es["ends_on"] == "2026-05-26"
        assert "id" not in es

    def test_all_four_inputs_empty_short_circuits(self):
        fake = FakeAnthropicClient(_ok_tool_input())
        result = resolve_fuzzy([], [], [], email_signals=[], client=fake)
        assert fake.messages.last_kwargs is None
        assert result == _empty_result()
        assert result["email_sales"] == []


class TestResolveFuzzyErrors:
    def test_raises_when_model_skips_tool(self, httpx_mock):
        httpx_mock.add_response(
            url="https://aniqi.com",
            # Must carry a sale signal so it survives the #1 pre-filter and
            # actually reaches Claude (otherwise there's no API call to fail).
            text="<html><body>Spring sale 30% off</body></html>",
        )
        # Build a fake whose response contains only a text block — no tool_use.
        text_block = SimpleNamespace(type="text", text="sorry")
        response = SimpleNamespace(content=[text_block], usage=None)

        class _BrokenMessages:
            def create(self, **_):
                return response

        broken = SimpleNamespace(messages=_BrokenMessages())

        with pytest.raises(RuntimeError, match="submit_results"):
            resolve_fuzzy(
                shops_to_check=[{"shop": "Aniqi", "url": "https://aniqi.com"}],
                shops_to_resolve=[], loose_mentions=[], client=broken,
            )


# ---------------------------------------------------------------------------
# Shadow A/B run (cost lever #5, issue #16)
# ---------------------------------------------------------------------------

def _make_response(tool_input, usage=None):
    content = [SimpleNamespace(type="tool_use", name="submit_results",
                               input=tool_input)]
    return SimpleNamespace(
        content=content,
        usage=SimpleNamespace(**usage) if usage else None,
        stop_reason=None,
    )


class _RoutingMessages:
    """Routes .create(model=...) to per-model scripted responses."""

    def __init__(self, responses_by_model, fail_models=()):
        self._responses = responses_by_model
        self._fail = set(fail_models)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        model = kwargs["model"]
        if model in self._fail:
            raise RuntimeError(f"boom from {model}")
        return self._responses[model]


def _email_signal():
    return [{"email_id": "msg_1", "shop": "Aniqi",
             "subject": "Sale!", "body_excerpt": "30% off",
             "email_date": "2026-07-04"}]


def _email_result(status):
    return _ok_tool_input(email_sales=[{
        "id": "email_0", "email_id": "msg_1", "shop": "Aniqi",
        "status": status, "description": None,
        "starts_on": None, "ends_on": None,
    }])


class TestResolveFuzzyShadow:
    PRIMARY = claude_fuzzy.DEFAULT_MODEL
    SHADOW = "claude-haiku-4-5-20251001"

    def _client(self, primary_input, shadow_input, fail_models=()):
        messages = _RoutingMessages({
            self.PRIMARY: _make_response(
                primary_input, usage={"input_tokens": 100, "output_tokens": 10}),
            self.SHADOW: _make_response(
                shadow_input, usage={"input_tokens": 100, "output_tokens": 8}),
        }, fail_models=fail_models)
        return SimpleNamespace(messages=messages)

    def test_shadow_gets_identical_payload(self):
        fake = self._client(_email_result("yes"), _email_result("yes"))
        result = resolve_fuzzy([], [], [], email_signals=_email_signal(),
                               client=fake, shadow_model=self.SHADOW)
        calls = fake.messages.calls
        assert [c["model"] for c in calls] == [self.PRIMARY, self.SHADOW]
        # Byte-identical payload + system prompt — a true A/B.
        assert calls[0]["messages"] == calls[1]["messages"]
        assert calls[0]["system"] == calls[1]["system"]
        shadow = result["shadow"]
        assert shadow["model"] == self.SHADOW
        assert shadow["summary"] == {
            "total": 1, "agree": 1,
            "by_type": {"email_sales": {"total": 1, "agree": 1}},
        }
        assert shadow["disagreements"] == []
        assert shadow["usage"] == {"input_tokens": 100, "output_tokens": 8}

    def test_disagreement_recorded_primary_result_untouched(self):
        fake = self._client(_email_result("no"), _email_result("yes"))
        result = resolve_fuzzy([], [], [], email_signals=_email_signal(),
                               client=fake, shadow_model=self.SHADOW)
        # The digest-facing result carries the PRIMARY verdict only.
        assert result["email_sales"][0]["status"] == "no"
        shadow = result["shadow"]
        assert shadow["summary"]["agree"] == 0
        assert shadow["disagreements"][0]["primary"]["status"] == "no"
        assert shadow["disagreements"][0]["shadow"]["status"] == "yes"

    def test_shadow_failure_is_isolated(self):
        fake = self._client(_email_result("yes"), _email_result("yes"),
                            fail_models={self.SHADOW})
        result = resolve_fuzzy([], [], [], email_signals=_email_signal(),
                               client=fake, shadow_model=self.SHADOW)
        # Primary result intact; shadow slot None (logged, not raised).
        assert result["email_sales"][0]["status"] == "yes"
        assert result["shadow"] is None

    def test_no_shadow_model_no_second_call(self):
        fake = self._client(_email_result("yes"), _email_result("yes"))
        result = resolve_fuzzy([], [], [], email_signals=_email_signal(),
                               client=fake)
        assert [c["model"] for c in fake.messages.calls] == [self.PRIMARY]
        assert result["shadow"] is None

    def test_no_tasks_no_shadow_call(self):
        fake = self._client(_ok_tool_input(), _ok_tool_input())
        result = resolve_fuzzy([], [], [], email_signals=[], client=fake,
                               shadow_model=self.SHADOW)
        assert fake.messages.calls == []
        assert result["shadow"] is None
