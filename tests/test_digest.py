"""Tests for src/digest.py.

Helpers build canonical detect_sale-shaped results so each test only specifies
the field(s) it cares about. No network, no fixtures.
"""

from __future__ import annotations

from datetime import date

from src.digest import (
    _DAILY_FIT_PENDING_CAP,
    _DAILY_REMOVAL_CAP,
    _review_age,
    _review_requests_section,
    _untracked_label,
    _untracked_sms_section,
    build_digest,
    build_fit_digest,
)


URL = "https://shop.example.com/products/cool-shirt"


def _result(**overrides) -> dict:
    """Canonical successful detect_sale output (no-change, in-stock, $50)."""
    base = {
        "sale_signal": "no_change",
        "stock_signal": None,
        "error_signal": None,
        "prior_price": None,
        "last_known": None,
        "updated_entry": {
            "label": "Cool Shirt",
            "current_price": 50.0,
            "original_price": None,
            "currency": "USD",
            "in_stock": True,
            "low_stock": False,
            "last_checked": "2026-05-17T14:00:00Z",
            "last_seen": "2026-05-17T14:00:00Z",
            "consecutive_failures": 0,
            "last_error_kind": None,
        },
    }
    base.update(overrides)
    return base


def _err_result(error_signal="could_not_check", kind="blocked", failures=2, last_known=None):
    """Canonical error-path detect_sale output."""
    if last_known is None:
        last_known = {
            "label": "Cool Shirt",
            "current_price": 50.0,
            "currency": "USD",
            "in_stock": True,
            "last_checked": "2026-05-16T14:00:00Z",
            "consecutive_failures": failures - 1,
            "last_error_kind": None,
        }
    return {
        "sale_signal": None,
        "stock_signal": None,
        "error_signal": error_signal,
        "prior_price": None,
        "last_known": last_known,
        "updated_entry": (
            None
            if error_signal == "removed_from_shop"
            else {**last_known, "consecutive_failures": failures, "last_error_kind": kind}
        ),
    }


def _item(url=URL, shop="ExampleShop", is_uncertain=False, **result_overrides) -> dict:
    return {
        "url": url,
        "shop": shop,
        "is_uncertain": is_uncertain,
        "result": _result(**result_overrides),
    }


# ---------------------------------------------------------------------------
# Empty / minimal
# ---------------------------------------------------------------------------

class TestEmpty:
    def test_completely_empty_input_returns_empty_string(self):
        assert build_digest({}) == ""

    def test_all_empty_lists_returns_empty_string(self):
        assert build_digest({"items": [], "shop_sales": [], "codes": [], "unresolved_shops": []}) == ""

    def test_no_signals_only_roster_section(self):
        """No-signal items now only appear in the 'All items by shop' roster."""
        out = build_digest({"items": [_item()]})
        assert "## All items by shop" in out
        assert "## Items unchanged" not in out
        assert "## Items on sale" not in out
        assert "## Shops on sale" not in out


# ---------------------------------------------------------------------------
# Items on sale
# ---------------------------------------------------------------------------

class TestItemsOnSale:
    def test_on_sale_per_page_with_original(self):
        item = _item(sale_signal="on_sale_per_page")
        item["result"]["updated_entry"]["current_price"] = 40.0
        item["result"]["updated_entry"]["original_price"] = 80.0
        out = build_digest({"items": [item]})
        assert "## Items on sale (specific URLs)" in out
        assert "**Cool Shirt**" in out
        assert "$40" in out
        assert "was $80 listed" in out
        assert f"[link]({URL})" in out

    def test_price_dropped_renders_prior_price(self):
        item = _item(sale_signal="price_dropped", prior_price=72.0)
        item["result"]["updated_entry"]["current_price"] = 58.0
        out = build_digest({"items": [item]})
        assert "$58" in out
        assert "down from $72 last checked" in out

    def test_on_sale_and_dropped_renders_both(self):
        item = _item(sale_signal="on_sale_per_page", prior_price=50.0)
        item["result"]["updated_entry"]["current_price"] = 40.0
        item["result"]["updated_entry"]["original_price"] = 80.0
        out = build_digest({"items": [item]})
        assert "was $80 listed" in out
        assert "down from $50 last checked" in out

    def test_uncertain_match_lands_in_uncertain_section(self):
        item = _item(sale_signal="on_sale_per_page", is_uncertain=True)
        item["result"]["updated_entry"]["current_price"] = 40.0
        item["result"]["updated_entry"]["original_price"] = 80.0
        out = build_digest({"items": [item]})
        assert "## Uncertain matches" in out
        assert "## Items on sale (specific URLs)" not in out

    def test_currency_non_usd_without_fx_renders_native_only(self):
        """When fx_rates aren't provided, non-USD prices render in native currency.
        Backward-compatible with pre-Phase-5b digests."""
        item = _item(sale_signal="price_dropped", prior_price=80.0)
        item["result"]["updated_entry"]["current_price"] = 60.0
        item["result"]["updated_entry"]["currency"] = "CAD"
        out = build_digest({"items": [item]})
        assert "$60 CAD" in out
        assert "down from $80 CAD last checked" in out

    def test_decimal_price_keeps_two_digits(self):
        item = _item(sale_signal="price_dropped", prior_price=50.0)
        item["result"]["updated_entry"]["current_price"] = 39.99
        out = build_digest({"items": [item]})
        assert "$39.99" in out

    def test_on_sale_but_sold_out_marks_out_of_stock(self):
        """A sale on an item that's sold out in every size still lands in
        'Items on sale' (the bucket sits ahead of still_oos), but must be
        flagged out of stock so a dead sale doesn't read as buyable (audit
        2026-06-15: Biscuits Shorts on sale but OOS in every size)."""
        item = _item(sale_signal="on_sale_per_page")
        u = item["result"]["updated_entry"]
        u["current_price"] = 28.0
        u["original_price"] = 40.0
        u["in_stock"] = False
        out = build_digest({"items": [item]})
        assert "## Items on sale (specific URLs)" in out
        assert "was $40 listed" in out
        assert "out of stock" in out

    def test_on_sale_oos_in_preferred_sizes_folds_size_note(self):
        """OOS in the user's sizes but available in others → the size note is
        folded into the OOS tag, matching the roster line's wording."""
        item = _item(sale_signal="on_sale_per_page")
        u = item["result"]["updated_entry"]
        u["current_price"] = 28.0
        u["original_price"] = 40.0
        u["in_stock"] = False
        u["unpreferred_available_sizes"] = ["S", "XL"]
        out = build_digest({"items": [item]})
        assert "out of stock (still available in S, XL)" in out


# ---------------------------------------------------------------------------
# Standing discounts (year-round "always marked down")
# ---------------------------------------------------------------------------

def _standing_item(is_uncertain=False, shop="ExampleShop"):
    item = _item(
        shop=shop,
        is_uncertain=is_uncertain,
        sale_signal="standing_discount",
        baseline_price=50.0,
        baseline_days=90,
    )
    item["result"]["updated_entry"]["current_price"] = 50.0
    item["result"]["updated_entry"]["original_price"] = 100.0
    return item


class TestStandingDiscounts:
    def test_standing_discount_own_section(self):
        out = build_digest({"items": [_standing_item()]})
        assert "## Standing discounts (always marked down)" in out
        assert "## Items on sale (specific URLs)" not in out

    def test_standing_discount_line_explains_no_real_drop(self):
        out = build_digest({"items": [_standing_item()]})
        assert "no real drop" in out
        assert 'was $100' in out
        assert "the last 90d" in out

    def test_uncertain_standing_stays_in_uncertain_section(self):
        """A loose-mention standing discount isn't split out — too low-confidence."""
        out = build_digest({"items": [_standing_item(is_uncertain=True)]})
        assert "## Uncertain matches" in out
        assert "## Standing discounts (always marked down)" not in out

    def test_no_standing_section_when_none(self):
        out = build_digest({"items": [_item(sale_signal="no_change")]})
        assert "## Standing discounts" not in out

    def test_standing_discount_sold_out_marks_out_of_stock(self):
        """A year-round-discounted item that's sold out lands in this bucket
        (ahead of still_oos) — surface the OOS status so it doesn't read as
        buyable, just like the on-sale line."""
        item = _standing_item()
        item["result"]["updated_entry"]["in_stock"] = False
        out = build_digest({"items": [item]})
        assert "## Standing discounts (always marked down)" in out
        assert "out of stock" in out

    def test_standing_discount_keeps_back_in_stock_note(self):
        """A standing-discount item that also just came back in stock lands in
        this bucket ahead of back_in_stock — the note must survive on the line."""
        item = _standing_item()
        item["result"]["stock_signal"] = "back_in_stock"
        out = build_digest({"items": [item]})
        assert "## Standing discounts (always marked down)" in out
        assert "back in stock" in out

    def test_standing_discount_keeps_low_stock_note(self):
        item = _standing_item()
        item["result"]["stock_signal"] = "newly_low_stock"
        out = build_digest({"items": [item]})
        assert "low stock" in out

    def test_non_clothing_standing_discount_breaks_out(self):
        item = _standing_item(shop="GadgetShop")
        out = build_digest({
            "items": [item],
            "non_clothing_shops": ["GadgetShop"],
        })
        assert "# Non-clothing" in out
        assert "## Standing discounts (non-clothing)" in out
        # and NOT in the top (clothing) standing section
        assert "## Standing discounts (always marked down)" not in out


# ---------------------------------------------------------------------------
# Stock transitions
# ---------------------------------------------------------------------------

class TestStockTransitions:
    def test_newly_out_of_stock_section(self):
        item = _item(stock_signal="newly_out_of_stock")
        item["result"]["updated_entry"]["in_stock"] = False
        out = build_digest({"items": [item]})
        assert "## Newly out of stock" in out
        assert "**Cool Shirt**" in out
        assert "was $50" in out

    def test_newly_oos_with_sale_signal_stays_in_oos_section(self):
        """An item that's both on-sale AND newly OOS lands in newly-OOS only,
        but the sale facts are noted on the line."""
        item = _item(sale_signal="on_sale_per_page", stock_signal="newly_out_of_stock")
        item["result"]["updated_entry"]["current_price"] = 40.0
        item["result"]["updated_entry"]["original_price"] = 80.0
        item["result"]["updated_entry"]["in_stock"] = False
        out = build_digest({"items": [item]})
        assert "## Newly out of stock" in out
        assert "## Items on sale (specific URLs)" not in out
        assert "on sale" in out
        assert "$80 listed" in out

    def test_back_in_stock_section(self):
        item = _item(stock_signal="back_in_stock")
        out = build_digest({"items": [item]})
        assert "## Back in stock" in out

    def test_newly_low_stock_section(self):
        item = _item(stock_signal="newly_low_stock")
        item["result"]["updated_entry"]["low_stock"] = True
        out = build_digest({"items": [item]})
        assert "## Now low stock" in out

    def test_back_in_stock_with_sale_signal_lands_in_on_sale_with_note(self):
        item = _item(sale_signal="price_dropped", stock_signal="back_in_stock", prior_price=60.0)
        item["result"]["updated_entry"]["current_price"] = 40.0
        out = build_digest({"items": [item]})
        assert "## Items on sale (specific URLs)" in out
        assert "back in stock" in out


class TestEmailRestocks:
    def _restock(self, **over):
        r = {
            "shop": "Norse Projects", "item": "Aros Chino", "size": "M",
            "subject": "Aros Chino is back in stock", "date_iso": "2026-06-13",
            "days_ago": 0, "url": "https://mail.google.com/mail/u/0/#all/abc",
        }
        r.update(over)
        return r

    def test_email_restock_creates_section_when_no_items(self):
        out = build_digest({"items": [], "email_restocks": [self._restock()]})
        assert "## Back in stock" in out
        assert "**Norse Projects**" in out
        assert "_(email alert)_" in out
        assert "size M" in out
        assert "[open email](https://mail.google.com/mail/u/0/#all/abc)" in out

    def test_merges_with_scrape_driven_back_in_stock(self):
        item = _item(stock_signal="back_in_stock")
        out = build_digest({"items": [item], "email_restocks": [self._restock()]})
        # One heading, both sources present beneath it.
        assert out.count("## Back in stock") == 1
        assert "**Cool Shirt**" in out          # scrape-driven item
        assert "_(email alert)_" in out          # email-sourced line

    def test_no_section_when_both_empty(self):
        out = build_digest({"items": [], "email_restocks": []})
        assert "## Back in stock" not in out

    def test_all_url_trailing_link(self):
        out = build_digest({
            "items": [], "email_restocks": [self._restock()],
            "email_restocks_all_url": "https://mail.google.com/mail/u/0/#search/x",
        })
        assert "See all back-in-stock emails" in out

    def test_falls_back_to_subject_when_no_item(self):
        out = build_digest({"items": [], "email_restocks": [
            self._restock(item=None, subject="It's back!")]})
        assert "It's back!" in out

    def test_low_stock_with_sale_signal_lands_in_on_sale_with_note(self):
        item = _item(sale_signal="on_sale_per_page", stock_signal="newly_low_stock")
        item["result"]["updated_entry"]["current_price"] = 40.0
        item["result"]["updated_entry"]["original_price"] = 80.0
        out = build_digest({"items": [item]})
        assert "## Items on sale (specific URLs)" in out
        assert "low stock" in out


# ---------------------------------------------------------------------------
# Size-aware OOS note ("still available in S, XL")
#
# Triggered by sale_detect populating updated_entry.unpreferred_available_sizes
# when the user's preferred sizes are all OOS but other sizes still are.
# ---------------------------------------------------------------------------

class TestSizeNote:
    def test_newly_oos_renders_size_note(self):
        item = _item(stock_signal="newly_out_of_stock")
        item["result"]["updated_entry"]["in_stock"] = False
        item["result"]["updated_entry"]["unpreferred_available_sizes"] = ["S", "XL"]
        out = build_digest({"items": [item]})
        assert "## Newly out of stock" in out
        assert "still available in S, XL" in out

    def test_still_oos_renders_size_note(self):
        """Items in the 'Still out of stock' section also get the note when
        the user's sizes specifically are missing."""
        item = _item()  # no stock transition; we'll mark in_stock=False below
        item["result"]["updated_entry"]["in_stock"] = False
        item["result"]["updated_entry"]["unpreferred_available_sizes"] = ["XS", "S"]
        out = build_digest({"items": [item]})
        assert "## Still out of stock" in out
        assert "still available in XS, S" in out

    def test_empty_list_omits_note(self):
        """Truly OOS (nothing available) → no parenthetical added."""
        item = _item(stock_signal="newly_out_of_stock")
        item["result"]["updated_entry"]["in_stock"] = False
        item["result"]["updated_entry"]["unpreferred_available_sizes"] = []
        out = build_digest({"items": [item]})
        assert "## Newly out of stock" in out
        assert "still available in" not in out

    def test_field_missing_omits_note(self):
        """Legacy entries without the field (pre-feature data) render unchanged."""
        item = _item(stock_signal="newly_out_of_stock")
        item["result"]["updated_entry"]["in_stock"] = False
        # Deliberately do NOT set unpreferred_available_sizes
        out = build_digest({"items": [item]})
        assert "## Newly out of stock" in out
        assert "still available in" not in out

    def test_roster_appends_note_to_oos_tag(self):
        """The 'All items by shop' roster line should fold the note into the
        OOS tag in parentheses rather than as a new dashed segment."""
        item = _item(stock_signal="newly_out_of_stock")
        item["result"]["updated_entry"]["in_stock"] = False
        item["result"]["updated_entry"]["unpreferred_available_sizes"] = ["S", "XL"]
        out = build_digest({"items": [item]})
        assert "newly out of stock (still available in S, XL)" in out

    def test_roster_oos_without_signal_also_gets_note(self):
        """Roster path 'out of stock' (no stock_signal but in_stock=False)
        also receives the note inline."""
        item = _item()
        item["result"]["updated_entry"]["in_stock"] = False
        item["result"]["updated_entry"]["unpreferred_available_sizes"] = ["S"]
        out = build_digest({"items": [item]})
        assert "out of stock (still available in S)" in out


# ---------------------------------------------------------------------------
# Partial-stock size note ("only in L" / "in stock in M, L")
#
# Triggered when the user's preferred sizes are PARTIALLY available — some
# preferred sizes are in stock, some aren't. The item itself is still in
# stock (so unpreferred_available_sizes stays empty).
# ---------------------------------------------------------------------------

def _add_size_data(
    item: dict,
    *,
    preferred=("M", "L", "XL"),
    offered=("S", "M", "L", "XL"),
    available=("L",),
) -> None:
    """Populate size_options / available_sizes / preferred_sizes_applied on
    a digest test item — mirrors what sale_detect persists from extract."""
    e = item["result"]["updated_entry"]
    e["preferred_sizes_applied"] = list(preferred)
    e["size_options"] = list(offered)
    e["available_sizes"] = list(available)


class TestPartialStockSizeNote:
    def test_only_one_preferred_size_left_renders_only_in(self):
        """Three preferred sizes (M, L, XL), only L in stock → 'only in L'."""
        item = _item()
        _add_size_data(item, available=("L",))
        out = build_digest({"items": [item]})
        # Roster picks it up (in-stock items don't get a bucket section, so
        # the partial-stock note shows up in the per-shop roster line).
        assert "only in L" in out

    def test_subset_of_preferred_renders_in_stock_in(self):
        """Two preferred sizes remain: 'in stock in M, L'."""
        item = _item()
        _add_size_data(item, available=("M", "L"))
        out = build_digest({"items": [item]})
        assert "in stock in M, L" in out

    def test_all_preferred_in_stock_renders_full_matrix(self):
        """User opted into a full size matrix on every tracked item, so even
        when every preferred size is available the note still fires."""
        item = _item()
        _add_size_data(item, available=("M", "L", "XL"))
        out = build_digest({"items": [item]})
        assert "in stock in M, L, XL" in out

    def test_no_preferred_overlap_no_note(self):
        """Ring sizes 7-11 against M/L/XL preferences → preferred filter
        doesn't apply, no note rendered."""
        item = _item()
        _add_size_data(
            item,
            offered=("7", "8", "9", "10", "11"),
            available=("7", "8"),
        )
        out = build_digest({"items": [item]})
        assert "only in" not in out
        assert "in stock in" not in out

    def test_legacy_entry_without_size_fields_omits_note(self):
        """Pre-feature items (no preferred_sizes_applied) render unchanged."""
        item = _item()  # no size fields
        out = build_digest({"items": [item]})
        assert "only in" not in out
        assert "in stock in" not in out

    def test_size_label_aliases_match(self):
        """Shop spells out 'Medium'/'Large' — should still match user's M/L."""
        item = _item()
        _add_size_data(
            item,
            preferred=("M", "L", "XL"),
            offered=("Small", "Medium", "Large", "X-Large"),
            available=("Large",),
        )
        out = build_digest({"items": [item]})
        # Shop's label preserved verbatim in the note.
        assert "only in Large" in out

    def test_on_sale_line_gets_note(self):
        """On-sale items also surface the partial-stock note so the buyer
        knows up front whether their size is still there."""
        item = _item(
            sale_signal="on_sale_per_page",
            stock_signal=None,
        )
        item["result"]["updated_entry"]["original_price"] = 80.0
        _add_size_data(item, available=("L",))
        out = build_digest({"items": [item]})
        assert "## Items on sale (specific URLs)" in out
        # The on-sale line itself (not just the roster) carries the note.
        on_sale_section = out.split("## Items on sale")[1].split("##")[0]
        assert "only in L" in on_sale_section

    def test_back_in_stock_line_gets_note(self):
        item = _item(stock_signal="back_in_stock")
        _add_size_data(item, available=("L",))
        out = build_digest({"items": [item]})
        back_section = out.split("## Back in stock")[1].split("##")[0]
        assert "only in L" in back_section

    def test_now_low_stock_line_gets_note(self):
        item = _item(stock_signal="newly_low_stock")
        item["result"]["updated_entry"]["low_stock"] = True
        _add_size_data(item, available=("L",))
        out = build_digest({"items": [item]})
        low_section = out.split("## Now low stock")[1].split("##")[0]
        assert "only in L" in low_section

    def test_oos_unpreferred_branch_wins_over_partial(self):
        """If unpreferred_available_sizes is set (OOS-override case), it
        takes precedence over the partial-stock branch — we don't want to
        double-render."""
        item = _item(stock_signal="newly_out_of_stock")
        item["result"]["updated_entry"]["in_stock"] = False
        item["result"]["updated_entry"]["unpreferred_available_sizes"] = ["S", "XS"]
        _add_size_data(item, available=())  # nothing preferred is in stock
        out = build_digest({"items": [item]})
        assert "still available in S, XS" in out
        assert "only in" not in out
        assert "in stock in" not in out


# ---------------------------------------------------------------------------
# Could not check — suppression policy
# ---------------------------------------------------------------------------

class TestCouldNotCheckSuppression:
    def test_never_checked_is_suppressed(self):
        """No prior last_checked → first-run Cloudflare noise, suppress."""
        item = {"url": URL, "shop": "S", "is_uncertain": False,
                "result": _err_result(kind="blocked", failures=1, last_known={})}
        out = build_digest({"items": [item]})
        assert "## Could not check" not in out

    def test_first_blocked_failure_is_suppressed(self):
        """1st transient (blocked) failure after a prior success → suppress."""
        item = {"url": URL, "shop": "S", "is_uncertain": False,
                "result": _err_result(kind="blocked", failures=1)}
        out = build_digest({"items": [item]})
        assert "## Could not check" not in out

    def test_first_timeout_failure_is_suppressed(self):
        item = {"url": URL, "shop": "S", "is_uncertain": False,
                "result": _err_result(kind="timeout", failures=1)}
        out = build_digest({"items": [item]})
        assert "## Could not check" not in out

    def test_first_server_error_is_shown(self):
        item = {"url": URL, "shop": "S", "is_uncertain": False,
                "result": _err_result(kind="server_error", failures=1)}
        out = build_digest({"items": [item]})
        assert "## Could not check" in out
        assert "server error" in out

    def test_first_other_error_is_shown(self):
        item = {"url": URL, "shop": "S", "is_uncertain": False,
                "result": _err_result(kind="other", failures=1)}
        out = build_digest({"items": [item]})
        assert "## Could not check" in out
        assert "fetch failed" in out

    def test_second_blocked_failure_is_shown(self):
        item = {"url": URL, "shop": "S", "is_uncertain": False,
                "result": _err_result(kind="blocked", failures=2)}
        out = build_digest({"items": [item]})
        assert "## Could not check" in out
        assert "blocked by site" in out

    def test_could_not_check_includes_shop_and_last_price(self):
        item = {"url": URL, "shop": "ExampleShop", "is_uncertain": False,
                "result": _err_result(kind="blocked", failures=2)}
        out = build_digest({"items": [item]})
        assert "(ExampleShop)" in out
        assert "last seen $50" in out


# ---------------------------------------------------------------------------
# Removed from shop
# ---------------------------------------------------------------------------

class TestRemovedFromShop:
    def test_removed_section_renders(self):
        item = {"url": URL, "shop": "ExampleShop", "is_uncertain": False,
                "result": _err_result(error_signal="removed_from_shop", kind=None, failures=1)}
        out = build_digest({"items": [item]})
        assert "## Removed from shop" in out
        assert "**Cool Shirt** (ExampleShop)" in out
        assert "was $50" in out

    def test_removed_is_not_suppressed_even_on_first_strike(self):
        item = {"url": URL, "shop": "ExampleShop", "is_uncertain": False,
                "result": _err_result(error_signal="removed_from_shop", kind=None, failures=1)}
        out = build_digest({"items": [item]})
        assert "## Removed from shop" in out


# ---------------------------------------------------------------------------
# Compact sections
# ---------------------------------------------------------------------------

class TestStillOutOfStock:
    def test_still_oos_compact_format(self):
        oos = _item()
        oos["result"]["updated_entry"]["in_stock"] = False
        out = build_digest({"items": [oos]})
        assert "## Still out of stock" in out
        # Compact: no price, just label + link. Slice to the section body only
        # (next section starts with '##').
        section = out.split("## Still out of stock")[1].split("\n##")[0]
        assert "**Cool Shirt**" in section
        assert "$50" not in section

    def test_still_oos_separate_from_newly_oos(self):
        newly = _item(stock_signal="newly_out_of_stock")
        newly["result"]["updated_entry"]["in_stock"] = False
        newly["result"]["updated_entry"]["label"] = "Newly OOS Item"
        still = _item(url="https://s.com/products/still")
        still["result"]["updated_entry"]["in_stock"] = False
        still["result"]["updated_entry"]["label"] = "Long OOS Item"
        out = build_digest({"items": [newly, still]})
        assert "## Newly out of stock" in out
        assert "## Still out of stock" in out
        newly_section = out.split("## Newly out of stock")[1].split("##")[0]
        still_section = out.split("## Still out of stock")[1]
        assert "Newly OOS Item" in newly_section
        assert "Long OOS Item" in still_section


# ---------------------------------------------------------------------------
# Shops, codes, unresolved
# ---------------------------------------------------------------------------

class TestShopSales:
    def test_shops_on_sale_with_description(self):
        out = build_digest({"shop_sales": [
            {"shop": "Aritzia", "status": "yes", "description": "30% off, code SPRING30"},
        ]})
        assert "## Shops on sale" in out
        assert "**Aritzia**: 30% off, code SPRING30" in out

    def test_shops_no_sale_compact_alphabetized(self):
        out = build_digest({"shop_sales": [
            {"shop": "Zebra", "status": "no"},
            {"shop": "Aniqi", "status": "no"},
            {"shop": "Hokuro", "status": "no"},
        ]})
        assert "## Shops with no sale" in out
        line = out.split("## Shops with no sale\n")[1]
        assert line.startswith("Aniqi, Hokuro, Zebra")

    def test_unclear_shops_separate_section(self):
        out = build_digest({"shop_sales": [
            {"shop": "Aritzia", "status": "yes", "description": "30% off"},
            {"shop": "MysteryCorp", "status": "unclear"},
        ]})
        assert "## Sale status unclear" in out
        assert "MysteryCorp" in out

    def test_yes_and_no_in_different_sections(self):
        out = build_digest({"shop_sales": [
            {"shop": "Aritzia", "status": "yes", "description": "30% off"},
            {"shop": "Boring Shop", "status": "no"},
        ]})
        yes = out.split("## Shops on sale")[1].split("##")[0]
        no = out.split("## Shops with no sale")[1]
        assert "Aritzia" in yes
        assert "Boring Shop" in no
        assert "Boring Shop" not in yes


class TestEmailSalesSection:
    _TODAY = date(2026, 5, 19)

    def test_upcoming_renders_starts_countdown(self):
        out = build_digest({
            "email_sales": [{
                "shop": "Aniqi", "description": "Memorial Day sale, 30% off",
                "starts_on": "2026-05-24", "ends_on": "2026-05-26",
            }],
            "today": self._TODAY,
        })
        assert "## Sales announced by email" in out
        assert "**Aniqi**: Memorial Day sale, 30% off" in out
        assert "starts in 5 days (Sun May 24)" in out

    def test_ongoing_renders_ends_countdown(self):
        out = build_digest({
            "email_sales": [{
                "shop": "Wooj", "description": "20% off",
                "starts_on": "2026-05-15", "ends_on": "2026-05-22",
            }],
            "today": self._TODAY,
        })
        assert "ends in 3 days (Fri May 22)" in out

    def test_undated_renders_without_countdown(self):
        out = build_digest({
            "email_sales": [{
                "shop": "Otishi", "description": "Sale on now",
                "starts_on": None, "ends_on": None,
            }],
            "today": self._TODAY,
        })
        assert "- **Otishi**: Sale on now" in out
        # No " — " countdown suffix appended.
        assert "Otishi**: Sale on now —" not in out

    def test_empty_omits_section(self):
        out = build_digest({"email_sales": [], "today": self._TODAY})
        assert "## Sales announced by email" not in out

    def test_starts_today_reads_starts_not_ends(self):
        out = build_digest({
            "email_sales": [{
                "shop": "Aniqi", "description": "Flash sale",
                "starts_on": "2026-05-19", "ends_on": "2026-05-19",
            }],
            "today": self._TODAY,
        })
        assert "starts today" in out
        assert "ends today" not in out


class TestPossibleEmailSalesSection:
    def test_unclear_email_renders_with_description(self):
        out = build_digest({
            "email_unclear": [
                {"shop": "Aniqi", "description": "spring event, terms vague"},
            ],
        })
        assert "## Possible sales (unclear)" in out
        assert "- **Aniqi**: spring event, terms vague" in out

    def test_unclear_email_without_description_renders_shop_only(self):
        out = build_digest({"email_unclear": [{"shop": "Wooj", "description": None}]})
        assert "## Possible sales (unclear)" in out
        assert "- **Wooj**" in out

    def test_deduped_by_shop_prefers_description(self):
        out = build_digest({
            "email_unclear": [
                {"shop": "Aniqi", "description": "maybe a sale"},
                {"shop": "aniqi", "description": "another email"},
            ],
        })
        assert out.count("**Aniqi**") == 1
        assert "maybe a sale" in out

    def test_empty_omits_section(self):
        out = build_digest({"email_unclear": []})
        assert "## Possible sales (unclear)" not in out


class TestCodes:
    def test_codes_section_renders(self):
        out = build_digest({"codes": [
            {"shop": "HakiStop", "code": "WELCOME15", "context": "..."},
            {"shop": "XSekai", "code": "FIRSTORDER10", "context": "..."},
        ]})
        assert "## Saved promo codes" in out
        assert "**HakiStop**: WELCOME15" in out
        assert "**XSekai**: FIRSTORDER10" in out

    def test_codes_dedupe(self):
        out = build_digest({"codes": [
            {"shop": "HakiStop", "code": "WELCOME15", "context": "a"},
            {"shop": "HakiStop", "code": "WELCOME15", "context": "b"},
        ]})
        assert out.count("WELCOME15") == 1

    def test_codes_alphabetized_by_shop(self):
        out = build_digest({"codes": [
            {"shop": "Zara", "code": "ZZZ"},
            {"shop": "Aritzia", "code": "AAA"},
        ]})
        section = out.split("## Saved promo codes\n")[1]
        assert section.index("Aritzia") < section.index("Zara")

    def test_unattributed_codes_in_separate_section(self):
        out = build_digest({"codes": [
            {"shop": "Aniqi", "code": "ATTR10", "source": "email"},
            {"shop": "unknown.io", "code": "UNATTR50",
             "source": "email_unattributed"},
        ]})
        assert "## Saved promo codes" in out
        assert "## Unattributed promo codes" in out
        # Attributed code in the main section, NOT in unattributed.
        saved = out.split("## Saved promo codes")[1].split("##")[0]
        unattr = out.split("## Unattributed promo codes")[1].split("##")[0]
        assert "ATTR10" in saved
        assert "ATTR10" not in unattr
        assert "UNATTR50" in unattr
        assert "UNATTR50" not in saved

    def test_unattributed_section_omitted_when_empty(self):
        out = build_digest({"codes": [
            {"shop": "Aniqi", "code": "ATTR10", "source": "email"},
        ]})
        assert "## Unattributed promo codes" not in out

    def test_legacy_codes_without_source_render_in_main_section(self):
        out = build_digest({"codes": [{"shop": "A", "code": "LEGACY"}]})
        assert "## Saved promo codes" in out
        assert "LEGACY" in out


class TestCodesConfidenceGrouping:
    """Both code sections bucket by confidence so likely-marketing words
    drop below the real-looking codes instead of polluting the top.
    Backfill ensures legacy entries without a confidence field still
    bucket correctly without a one-shot Gist migration."""

    def test_low_confidence_subsection_under_unattributed(self):
        out = build_digest({"codes": [
            {"shop": "junewave.com", "code": "SITEWIDE",
             "source": "email_unattributed", "confidence": "low"},
            {"shop": "otishi.com", "code": "WELCOME10",
             "source": "email_unattributed", "confidence": "high"},
        ]})
        section = out.split("## Unattributed promo codes")[1].split("\n##")[0]
        # Both render.
        assert "WELCOME10" in section
        assert "SITEWIDE" in section
        # Low-confidence header appears, and SITEWIDE sits after the marker.
        assert "Low confidence" in section
        assert section.index("WELCOME10") < section.index("Low confidence")
        assert section.index("Low confidence") < section.index("SITEWIDE")

    def test_high_confidence_renders_without_subheader(self):
        """When only high-confidence codes exist, no confidence sub-header
        appears — the section is clean."""
        out = build_digest({"codes": [
            {"shop": "otishi.com", "code": "WELCOME10",
             "source": "email_unattributed", "confidence": "high"},
        ]})
        section = out.split("## Unattributed promo codes")[1].split("\n##")[0]
        assert "WELCOME10" in section
        assert "Low confidence" not in section
        assert "Uncertain" not in section

    def test_medium_confidence_subsection(self):
        out = build_digest({"codes": [
            {"shop": "x.com", "code": "PEAKVIP",
             "source": "email_unattributed", "confidence": "medium"},
            {"shop": "y.com", "code": "DENIM40",
             "source": "email_unattributed", "confidence": "high"},
        ]})
        section = out.split("## Unattributed promo codes")[1].split("\n##")[0]
        assert "Uncertain" in section
        assert section.index("DENIM40") < section.index("Uncertain")
        assert section.index("Uncertain") < section.index("PEAKVIP")

    def test_legacy_entries_backfilled_via_classify(self):
        """Codes from before the rating feature have no ``confidence``
        field. The digest backfills via _classify_confidence so the user
        sees the right grouping immediately, without waiting for the next
        cron re-extraction."""
        out = build_digest({"codes": [
            # No confidence field on either entry.
            {"shop": "junewave.com", "code": "SITEWIDE",
             "source": "email_unattributed"},
            {"shop": "otishi.com", "code": "WELCOME10",
             "source": "email_unattributed"},
        ]})
        section = out.split("## Unattributed promo codes")[1].split("\n##")[0]
        # SITEWIDE should still be classified low and sit after the header,
        # even though the dict has no ``confidence`` key.
        assert "Low confidence" in section
        assert section.index("WELCOME10") < section.index("Low confidence")
        assert section.index("Low confidence") < section.index("SITEWIDE")

    def test_attributed_section_also_groups_by_confidence(self):
        out = build_digest({"codes": [
            {"shop": "Aniqi", "code": "DISCOUNT_FAKE",
             "source": "email", "confidence": "low"},
            {"shop": "Aniqi", "code": "DYNAMITE10",
             "source": "email", "confidence": "high"},
        ]})
        section = out.split("## Saved promo codes")[1].split("\n##")[0]
        assert "DYNAMITE10" in section
        assert "DISCOUNT_FAKE" in section
        assert "Low confidence" in section
        assert section.index("DYNAMITE10") < section.index("Low confidence")


class TestUnresolvedShops:
    def test_renders(self):
        out = build_digest({"unresolved_shops": ["CannedGoodsClothing", "MysteryBrand"]})
        assert "## Could not resolve" in out
        assert "CannedGoodsClothing" in out
        assert "MysteryBrand" in out

    def test_empty_omitted(self):
        out = build_digest({"unresolved_shops": []})
        assert "## Could not resolve" not in out


# ---------------------------------------------------------------------------
# Label fallback
# ---------------------------------------------------------------------------

class TestLabelFallback:
    def test_uses_updated_label_first(self):
        item = _item()
        item["result"]["updated_entry"]["label"] = "From Updated"
        item["result"]["last_known"] = {"label": "From History", "last_checked": "x"}
        out = build_digest({"items": [item]})
        assert "From Updated" in out
        assert "From History" not in out

    def test_falls_back_to_last_known_label(self):
        item = {"url": URL, "shop": "S", "is_uncertain": False,
                "result": _err_result(kind="server_error", failures=1,
                                      last_known={"label": "From History",
                                                  "current_price": 50.0, "currency": "USD",
                                                  "in_stock": True, "last_checked": "x"})}
        out = build_digest({"items": [item]})
        assert "From History" in out

    def test_falls_back_to_url_slug(self):
        """No label anywhere → use URL slug."""
        item = {"url": "https://shop.com/products/mystery-slug-here",
                "shop": "S", "is_uncertain": False,
                "result": _err_result(kind="server_error", failures=1,
                                      last_known={"current_price": 50.0, "currency": "USD",
                                                  "in_stock": True, "last_checked": "x"})}
        out = build_digest({"items": [item]})
        assert "mystery-slug-here" in out


# ---------------------------------------------------------------------------
# FX conversion (Phase 5b)
# ---------------------------------------------------------------------------

# Rate of 1.5 chosen so '$45 CAD / 1.5 = $30 USD' renders cleanly without
# any decimals — keeps assertions readable.
_FX = {"USD": 1, "CAD": 1.5, "EUR": 0.8}


class TestFxConversion:
    def test_usd_unaffected_by_fx_rates(self):
        item = _item()
        item["result"]["updated_entry"]["current_price"] = 50.0
        out = build_digest({"items": [item], "fx_rates": _FX})
        roster = out.split("## All items by shop")[1]
        assert "$50 — [link]" in roster
        assert "USD" not in roster  # plain USD doesn't get a "USD" suffix

    def test_cad_converts_to_usd_dual_render(self):
        item = _item()
        item["result"]["updated_entry"]["current_price"] = 45.0
        item["result"]["updated_entry"]["currency"] = "CAD"
        out = build_digest({"items": [item], "fx_rates": _FX})
        roster = out.split("## All items by shop")[1]
        assert "$30 USD [CAD $45]" in roster

    def test_unknown_currency_falls_back_to_native(self):
        item = _item()
        item["result"]["updated_entry"]["current_price"] = 100.0
        item["result"]["updated_entry"]["currency"] = "XYZ"  # not in _FX
        out = build_digest({"items": [item], "fx_rates": _FX})
        roster = out.split("## All items by shop")[1]
        assert "$100 XYZ" in roster
        assert "USD" not in roster.split("[link]")[0]  # no fake "USD" tag

    def test_on_sale_dual_render_both_prices(self):
        item = _item(sale_signal="on_sale_per_page")
        item["result"]["updated_entry"]["current_price"] = 30.0
        item["result"]["updated_entry"]["original_price"] = 60.0
        item["result"]["updated_entry"]["currency"] = "CAD"
        out = build_digest({"items": [item], "fx_rates": _FX})
        # Main 'Items on sale' section
        sale_section = out.split("## Items on sale")[1].split("##")[0]
        assert "$20 USD [CAD $30]" in sale_section
        assert "was $40 USD [CAD $60] listed" in sale_section

    def test_price_dropped_dual_render_prior(self):
        item = _item(sale_signal="price_dropped", prior_price=75.0)
        item["result"]["updated_entry"]["current_price"] = 45.0
        item["result"]["updated_entry"]["currency"] = "CAD"
        out = build_digest({"items": [item], "fx_rates": _FX})
        sale_section = out.split("## Items on sale")[1].split("##")[0]
        assert "$30 USD [CAD $45]" in sale_section
        assert "down from $50 USD [CAD $75] last checked" in sale_section

    def test_could_not_check_dual_render_last_seen(self):
        item = {"url": URL, "shop": "Carmico", "is_uncertain": False,
                "result": {"sale_signal": None, "stock_signal": None,
                           "error_signal": "could_not_check", "prior_price": None,
                           "last_known": {"label": "CAD Item", "current_price": 90.0,
                                          "currency": "CAD", "in_stock": True,
                                          "last_checked": "2026-05-16T14:00:00Z"},
                           "updated_entry": {"label": "CAD Item", "current_price": 90.0,
                                             "currency": "CAD", "in_stock": True,
                                             "last_checked": "2026-05-16T14:00:00Z",
                                             "consecutive_failures": 3,
                                             "last_error_kind": "blocked"}}}
        out = build_digest({"items": [item], "fx_rates": _FX})
        section = out.split("## Could not check")[1].split("##")[0]
        assert "last seen $60 USD [CAD $90]" in section

    def test_roster_uses_fx_conversion(self):
        item = _item(sale_signal="on_sale_per_page", prior_price=75.0)
        item["result"]["updated_entry"]["current_price"] = 30.0
        item["result"]["updated_entry"]["original_price"] = 60.0
        item["result"]["updated_entry"]["currency"] = "CAD"
        out = build_digest({"items": [item], "fx_rates": _FX})
        roster = out.split("## All items by shop")[1]
        assert "$20 USD [CAD $30]" in roster
        assert "on sale, was $40 USD [CAD $60]" in roster
        assert "down from $50 USD [CAD $75]" in roster

    def test_fx_rates_none_renders_native_currency_tag(self):
        """Explicit None should behave the same as missing fx_rates key."""
        item = _item()
        item["result"]["updated_entry"]["current_price"] = 45.0
        item["result"]["updated_entry"]["currency"] = "CAD"
        out = build_digest({"items": [item], "fx_rates": None})
        roster = out.split("## All items by shop")[1]
        assert "$45 CAD" in roster
        assert "USD" not in roster.split("[link]")[0]


# ---------------------------------------------------------------------------
# Section ordering
# ---------------------------------------------------------------------------

class TestSectionOrdering:
    def test_full_digest_section_order(self):
        on_sale = _item(sale_signal="on_sale_per_page")
        on_sale["result"]["updated_entry"]["current_price"] = 40.0
        on_sale["result"]["updated_entry"]["original_price"] = 80.0
        on_sale["result"]["updated_entry"]["label"] = "Sale Item"

        unchanged = _item(url="https://s.com/u")
        unchanged["result"]["updated_entry"]["label"] = "Unchanged Item"

        data = {
            "items": [on_sale, unchanged],
            "shop_sales": [{"shop": "Aritzia", "status": "yes", "description": "30% off"},
                           {"shop": "Boring", "status": "no"}],
            "codes": [{"shop": "HakiStop", "code": "WELCOME15"}],
            "unresolved_shops": ["MysteryBrand"],
        }
        out = build_digest(data)

        expected_order = [
            "## Shops on sale",
            "## Items on sale (specific URLs)",
            "## Could not resolve",
            "## Saved promo codes",
            "## Shops with no sale",
            "## All items by shop",
        ]
        positions = [out.index(h) for h in expected_order]
        assert positions == sorted(positions), f"sections out of order: {positions}"

    def test_no_items_unchanged_section(self):
        """The 'Items unchanged' section was removed — roster supersedes it."""
        out = build_digest({"items": [_item()]})
        assert "## Items unchanged" not in out


# ---------------------------------------------------------------------------
# All items by shop (roster)
# ---------------------------------------------------------------------------

class TestRoster:
    def test_roster_appears_when_items_present(self):
        out = build_digest({"items": [_item()]})
        assert "## All items by shop" in out

    def test_roster_omitted_when_no_items(self):
        out = build_digest({"shop_sales": [{"shop": "X", "status": "yes", "description": "y"}]})
        assert "## All items by shop" not in out

    def test_groups_by_shop_with_h3_headers(self):
        a = _item(shop="Aniqi", url="https://aniqi.com/products/a")
        a["result"]["updated_entry"]["label"] = "Law Pants"
        b = _item(shop="Aniwrld", url="https://aniwrld.com/products/b")
        b["result"]["updated_entry"]["label"] = "Akatsuki Hoodie"
        out = build_digest({"items": [a, b]})
        assert "### Aniqi" in out
        assert "### Aniwrld" in out

    def test_shops_alphabetized(self):
        z = _item(shop="Zara", url="https://zara.com/products/z")
        z["result"]["updated_entry"]["label"] = "Zara Coat"
        a = _item(shop="Aniqi", url="https://aniqi.com/products/a")
        a["result"]["updated_entry"]["label"] = "Law Pants"
        out = build_digest({"items": [z, a]})
        roster = out.split("## All items by shop")[1]
        assert roster.index("### Aniqi") < roster.index("### Zara")

    def test_items_within_shop_alphabetized_by_label(self):
        a = _item(shop="Aniqi", url="https://aniqi.com/products/x")
        a["result"]["updated_entry"]["label"] = "Zebra Pants"
        b = _item(shop="Aniqi", url="https://aniqi.com/products/y")
        b["result"]["updated_entry"]["label"] = "Apple Shirt"
        out = build_digest({"items": [a, b]})
        roster = out.split("## All items by shop")[1]
        assert roster.index("Apple Shirt") < roster.index("Zebra Pants")

    def test_missing_shop_grouped_under_unknown(self):
        item = _item(shop=None)
        out = build_digest({"items": [item]})
        assert "### (unknown shop)" in out

    def test_plain_item_inline_format(self):
        item = _item()
        item["result"]["updated_entry"]["label"] = "Basic Tee"
        item["result"]["updated_entry"]["current_price"] = 35.0
        out = build_digest({"items": [item]})
        roster = out.split("## All items by shop")[1]
        assert "**Basic Tee** — $35 — [link](" in roster

    def test_on_sale_inline_tag(self):
        item = _item(sale_signal="on_sale_per_page")
        item["result"]["updated_entry"]["current_price"] = 40.0
        item["result"]["updated_entry"]["original_price"] = 80.0
        out = build_digest({"items": [item]})
        roster = out.split("## All items by shop")[1]
        assert "$40 (on sale, was $80)" in roster

    def test_price_dropped_inline_tag(self):
        item = _item(sale_signal="price_dropped", prior_price=72.0)
        item["result"]["updated_entry"]["current_price"] = 58.0
        out = build_digest({"items": [item]})
        roster = out.split("## All items by shop")[1]
        assert "$58 (down from $72)" in roster

    def test_on_sale_and_dropped_combined_tag(self):
        item = _item(sale_signal="on_sale_per_page", prior_price=50.0)
        item["result"]["updated_entry"]["current_price"] = 40.0
        item["result"]["updated_entry"]["original_price"] = 80.0
        out = build_digest({"items": [item]})
        roster = out.split("## All items by shop")[1]
        assert "$40 (on sale, was $80; down from $50)" in roster

    def test_oos_inline_tag(self):
        item = _item()
        item["result"]["updated_entry"]["in_stock"] = False
        out = build_digest({"items": [item]})
        roster = out.split("## All items by shop")[1]
        assert "out of stock" in roster

    def test_newly_oos_inline_tag(self):
        item = _item(stock_signal="newly_out_of_stock")
        item["result"]["updated_entry"]["in_stock"] = False
        out = build_digest({"items": [item]})
        roster = out.split("## All items by shop")[1]
        assert "newly out of stock" in roster

    def test_low_stock_inline_tag(self):
        item = _item()
        item["result"]["updated_entry"]["low_stock"] = True
        out = build_digest({"items": [item]})
        roster = out.split("## All items by shop")[1]
        assert "low stock" in roster

    def test_could_not_check_in_roster_with_last_known(self):
        item = {"url": URL, "shop": "Nordstrom", "is_uncertain": False,
                "result": _err_result(kind="blocked", failures=3)}
        out = build_digest({"items": [item]})
        roster = out.split("## All items by shop")[1]
        assert "last seen $50" in roster
        assert "blocked by site" in roster

    def test_suppressed_could_not_check_still_in_roster(self):
        """Suppression only applies to the main 'Could not check' section.
        The roster always lists the item with its last-known price + error tag."""
        item = {"url": URL, "shop": "Nordstrom", "is_uncertain": False,
                "result": _err_result(kind="blocked", failures=1)}
        out = build_digest({"items": [item]})
        assert "## Could not check" not in out  # suppressed
        roster = out.split("## All items by shop")[1]
        assert "last seen $50" in roster
        assert "blocked by site" in roster

    def test_removed_in_roster_with_tag(self):
        item = {"url": URL, "shop": "Shop", "is_uncertain": False,
                "result": _err_result(error_signal="removed_from_shop", kind=None, failures=1)}
        out = build_digest({"items": [item]})
        roster = out.split("## All items by shop")[1]
        assert "was $50" in roster
        assert "removed from shop" in roster

    def test_currency_in_roster(self):
        item = _item()
        item["result"]["updated_entry"]["current_price"] = 60.0
        item["result"]["updated_entry"]["currency"] = "CAD"
        out = build_digest({"items": [item]})
        roster = out.split("## All items by shop")[1]
        assert "$60 CAD" in roster


# ---------------------------------------------------------------------------
# Fit feedback section
# ---------------------------------------------------------------------------

def _pending(**kw) -> dict:
    base = {
        "name": "Aros Chino",
        "shop": "Aniqi",
        "size": "M",
        "color": "Black",
        "url": "https://form.example/exec?item=abc&sig=deadbeef",
    }
    base.update(kw)
    return base


class TestFitFeedbackSection:
    def test_section_omitted_when_no_pending(self):
        out = build_digest({"items": [], "fit_pending": []})
        assert "Fit feedback wanted" not in out

    def test_section_renders_each_pending_item_with_link(self):
        out = build_digest({"fit_pending": [_pending(), _pending(name="Tee", size="L")]})
        assert "## Fit feedback wanted" in out
        section = out.split("## Fit feedback wanted")[1]
        assert "**Aros Chino** (Aniqi)" in section
        assert "size M, Black" in section
        assert "[leave fit feedback](https://form.example/exec?item=abc&sig=deadbeef)" in section
        assert "2 item(s) waiting" in section

    def test_review_all_link_included_when_provided(self):
        out = build_digest({
            "fit_pending": [_pending()],
            "fit_review_all_url": "https://form.example/exec?all=1&sig=abc",
        })
        assert "[Review all](https://form.example/exec?all=1&sig=abc)" in out

    def test_no_review_all_link_when_absent(self):
        out = build_digest({"fit_pending": [_pending()]})
        assert "Review all" not in out

    def test_item_without_shop_or_size_still_renders(self):
        out = build_digest({"fit_pending": [
            {"name": "Mystery", "shop": None, "size": None, "color": None,
             "url": "https://form.example/exec?item=z&sig=1"},
        ]})
        section = out.split("## Fit feedback wanted")[1]
        assert "**Mystery**" in section
        assert "[leave fit feedback]" in section

    def test_daily_section_caps_links_and_reports_total(self):
        n = _DAILY_FIT_PENDING_CAP + 7
        pending = [_pending(name=f"Item {i}", url=f"https://f.ex/e?i={i}&sig=s")
                   for i in range(n)]
        out = build_digest({
            "fit_pending": pending,
            "fit_review_all_url": "https://form.example/exec?all=1&sig=abc",
        })
        section = out.split("## Fit feedback wanted")[1]
        # Capped to the newest N links, with the true total surfaced + Review all.
        assert section.count("[leave fit feedback]") == _DAILY_FIT_PENDING_CAP
        assert f"{_DAILY_FIT_PENDING_CAP} of {n} items waiting" in section
        assert "(newest shown)" in section
        assert "[Review all](https://form.example/exec?all=1&sig=abc)" in section

    def test_no_truncation_notice_at_or_below_cap(self):
        pending = [_pending(name=f"Item {i}") for i in range(_DAILY_FIT_PENDING_CAP)]
        out = build_digest({"fit_pending": pending})
        section = out.split("## Fit feedback wanted")[1]
        assert section.count("[leave fit feedback]") == _DAILY_FIT_PENDING_CAP
        assert "newest shown" not in section
        assert f"{_DAILY_FIT_PENDING_CAP} item(s) waiting" in section


class TestBuildFitDigest:
    def test_empty_when_nothing_pending(self):
        assert build_fit_digest([]) == ""

    def test_has_heading_and_items(self):
        body = build_fit_digest([_pending()], "https://form.example/exec?all=1&sig=a")
        assert body.startswith("# Fit feedback")
        assert "## Fit feedback wanted" in body
        assert "**Aros Chino**" in body
        assert "[Review all]" in body

    def test_weekly_is_uncapped(self):
        # The daily digest caps fit links; the weekly email must list them all.
        n = _DAILY_FIT_PENDING_CAP + 10
        pending = [_pending(name=f"Item {i}", url=f"https://f.ex/e?i={i}&sig=s")
                   for i in range(n)]
        body = build_fit_digest(pending)
        assert body.count("[leave fit feedback]") == n
        assert "newest shown" not in body
        assert f"{n} item(s) waiting" in body


# ---------------------------------------------------------------------------
# Watchlist-removal section
# ---------------------------------------------------------------------------

def _removal(**kw) -> dict:
    base = {
        "name": "Aros Chino",
        "shop": "Aniqi",
        "size": "M",
        "color": "Black",
        "matched_line": "https://aniqi.com/products/aros-chino",
        "url": "https://form.example/exec?remove=abc&sig=deadbeef",
    }
    base.update(kw)
    return base


class TestRemovalSection:
    def test_section_omitted_when_no_pending(self):
        out = build_digest({"items": [], "removal_pending": []})
        assert "remove from watchlist" not in out.lower()

    def test_section_renders_each_item_with_doc_line_and_link(self):
        out = build_digest({"removal_pending": [_removal(), _removal(name="Tee", size="L")]})
        assert "## Bought — remove from watchlist?" in out
        section = out.split("## Bought — remove from watchlist?")[1]
        assert "**Aros Chino** (Aniqi)" in section
        assert "size M, Black" in section
        # The user must see *how it's listed in the Doc* before approving.
        assert "listed as `https://aniqi.com/products/aros-chino`" in section
        assert "[approve removal](https://form.example/exec?remove=abc&sig=deadbeef)" in section
        assert "2 purchased item(s)" in section

    def test_review_all_link_included_when_provided(self):
        out = build_digest({
            "removal_pending": [_removal()],
            "removal_review_all_url": "https://form.example/exec?removeall=1&sig=abc",
        })
        assert "[Review all](https://form.example/exec?removeall=1&sig=abc)" in out

    def test_no_review_all_link_when_absent(self):
        out = build_digest({"removal_pending": [_removal()]})
        assert "Review all" not in out

    def test_item_without_shop_or_matched_line_still_renders(self):
        out = build_digest({"removal_pending": [
            {"name": "Mystery", "shop": None, "size": None, "color": None,
             "matched_line": None, "url": "https://form.example/exec?remove=z&sig=1"},
        ]})
        section = out.split("## Bought — remove from watchlist?")[1]
        assert "**Mystery**" in section
        assert "listed as" not in section  # no doc line to show
        assert "[approve removal]" in section

    def test_daily_section_caps_links_and_reports_total(self):
        n = _DAILY_REMOVAL_CAP + 5
        pending = [_removal(name=f"Item {i}", url=f"https://f.ex/e?remove={i}&sig=s")
                   for i in range(n)]
        out = build_digest({
            "removal_pending": pending,
            "removal_review_all_url": "https://form.example/exec?removeall=1&sig=abc",
        })
        section = out.split("## Bought — remove from watchlist?")[1]
        assert section.count("[approve removal]") == _DAILY_REMOVAL_CAP
        assert f"{_DAILY_REMOVAL_CAP} of {n} purchased items" in section
        assert "(newest shown)" in section
        assert "[Review all](https://form.example/exec?removeall=1&sig=abc)" in section

    def test_no_truncation_notice_at_or_below_cap(self):
        pending = [_removal(name=f"Item {i}") for i in range(_DAILY_REMOVAL_CAP)]
        out = build_digest({"removal_pending": pending})
        section = out.split("## Bought — remove from watchlist?")[1]
        assert section.count("[approve removal]") == _DAILY_REMOVAL_CAP
        assert "newest shown" not in section


# ---------------------------------------------------------------------------
# Non-clothing block — items + homepage sale status whose shop is in
# data["non_clothing_shops"] break out into a "# Non-clothing" block at the
# very bottom (full mirror of the homepage-driven sections).
# ---------------------------------------------------------------------------

GADGET_URL = "https://keychron.com/products/q1-pro"


def _on_sale(url=URL, shop="ExampleShop", label="Cool Shirt", cur=40.0, orig=80.0) -> dict:
    item = _item(url=url, shop=shop, sale_signal="on_sale_per_page")
    item["result"]["updated_entry"]["label"] = label
    item["result"]["updated_entry"]["current_price"] = cur
    item["result"]["updated_entry"]["original_price"] = orig
    return item


class TestNonClothingBlock:
    def test_no_block_when_no_non_clothing_shops(self):
        """Back-compat: without a non_clothing_shops set, everything is clothing
        and the block is absent."""
        out = build_digest({"items": [_on_sale()]})
        assert "# Non-clothing" not in out.splitlines()
        assert "## Items on sale (specific URLs)" in out

    def test_non_clothing_item_breaks_out(self):
        item = _on_sale(url=GADGET_URL, shop="Keychron", label="Q1 Pro Keyboard")
        out = build_digest({
            "items": [item],
            "non_clothing_shops": ["Keychron"],
        })
        assert "# Non-clothing" in out.splitlines()
        assert "## Items on sale (non-clothing)" in out
        # Not in the clothing on-sale section.
        assert "## Items on sale (specific URLs)" not in out
        assert "**Q1 Pro Keyboard**" in out
        assert "## All non-clothing items by shop" in out
        # Clothing roster absent (no clothing items at all).
        assert "## All items by shop" not in out

    def test_block_renders_at_the_bottom(self):
        clothing = _on_sale()  # ExampleShop, stays on top
        gadget = _on_sale(url=GADGET_URL, shop="Keychron", label="Q1 Pro Keyboard")
        out = build_digest({
            "items": [clothing, gadget],
            "non_clothing_shops": ["Keychron"],
        })
        lines = out.splitlines()
        assert lines.index("# Non-clothing") > lines.index("## All items by shop")
        # Clothing item stays in the top on-sale section.
        assert "## Items on sale (specific URLs)" in out
        assert "## Items on sale (non-clothing)" in out

    def test_items_and_rosters_split_by_shop(self):
        shirt = _on_sale(label="Cool Shirt")            # ExampleShop (clothing)
        gadget = _on_sale(url=GADGET_URL, shop="Keychron", label="Q1 Pro Keyboard")
        out = build_digest({
            "items": [shirt, gadget],
            "non_clothing_shops": ["Keychron"],
        })
        top = out.split("# Non-clothing")[0]
        bottom = out.split("# Non-clothing")[1]
        assert "Cool Shirt" in top and "Q1 Pro Keyboard" not in top
        assert "Q1 Pro Keyboard" in bottom and "Cool Shirt" not in bottom

    def test_newly_oos_non_clothing_section(self):
        item = _item(url=GADGET_URL, shop="Keychron", stock_signal="newly_out_of_stock")
        item["result"]["updated_entry"]["label"] = "Q1 Pro Keyboard"
        item["result"]["updated_entry"]["in_stock"] = False
        out = build_digest({"items": [item], "non_clothing_shops": ["Keychron"]})
        assert "## Newly out of stock (non-clothing)" in out
        assert "## Newly out of stock\n" not in out  # clothing variant absent

    def test_shop_sales_move_to_non_clothing(self):
        shop_sales = [
            {"shop": "Aviator Nation", "status": "yes", "description": "30% off"},
            {"shop": "Keychron", "status": "yes", "description": "15% off"},
            {"shop": "Logitech", "status": "no"},
            {"shop": "Razer", "status": "unclear"},
        ]
        out = build_digest({
            "shop_sales": shop_sales,
            "non_clothing_shops": ["Keychron", "Logitech", "Razer"],
        })
        # Clothing shop stays in the top "Shops on sale" list.
        top = out.split("# Non-clothing")[0]
        assert "## Shops on sale" in top
        assert "Aviator Nation" in top
        assert "Keychron" not in top
        # Non-clothing shop sale status moves down with its own headers.
        assert "## Non-clothing shops on sale" in out
        assert "## Non-clothing shops with no sale" in out
        assert "## Non-clothing sale status unclear" in out

    def test_shop_match_is_case_insensitive(self):
        item = _on_sale(url=GADGET_URL, shop="Keychron", label="Q1 Pro Keyboard")
        out = build_digest({
            "items": [item],
            "non_clothing_shops": ["keychron"],  # lower-cased in the set
        })
        assert "## Items on sale (non-clothing)" in out

    def test_block_self_suppresses_with_no_matching_content(self):
        """A non-clothing shop in the set but no matching items/shop_sales →
        no block at all."""
        out = build_digest({
            "items": [_on_sale()],  # clothing only
            "non_clothing_shops": ["GhostShop"],
        })
        assert "# Non-clothing" not in out.splitlines()
        assert "## Items on sale (specific URLs)" in out

    def test_codes_stay_on_top_not_split(self):
        """Promo codes aren't tracked items/shops — they stay in the top
        section even for a non-clothing shop."""
        out = build_digest({
            "items": [_on_sale(url=GADGET_URL, shop="Keychron", label="Q1 Pro")],
            "non_clothing_shops": ["Keychron"],
            "codes": [{"shop": "Keychron", "code": "SAVE15", "source": "watchlist"}],
        })
        top = out.split("# Non-clothing")[0]
        assert "## Saved promo codes" in top
        assert "SAVE15" in top


# ---------------------------------------------------------------------------
# Review-requests section
# ---------------------------------------------------------------------------

class TestReviewAge:
    def test_today(self):
        assert _review_age(0) == "today"

    def test_negative_clock_skew_reads_today(self):
        assert _review_age(-1) == "today"

    def test_yesterday(self):
        assert _review_age(1) == "yesterday"

    def test_n_days_ago(self):
        assert _review_age(5) == "5 days ago"

    def test_none_when_unknown(self):
        assert _review_age(None) == ""


class TestReviewRequestsSection:
    def _req(self, **over):
        base = {"shop": "Suzushii Clothing",
                "subject": "Order #138880, how did it go?",
                "days_ago": 5,
                "url": "https://mail.google.com/mail/u/0/#search/rfc822msgid:a@x.com"}
        base.update(over)
        return base

    def test_self_suppresses_when_empty(self):
        assert _review_requests_section([], "https://all", 30) is None

    def test_renders_entries_with_link_and_age(self):
        out = _review_requests_section([self._req()], "https://all/search", 30)
        assert out.startswith("## Review requests")
        assert "1 review request (last 30 days, one per order)." in out
        assert "[See all review requests](https://all/search)" in out
        assert "**Suzushii Clothing**" in out
        assert "Order #138880, how did it go?" in out
        assert "5 days ago" in out
        assert "[open](https://mail.google.com/mail/u/0/#search/rfc822msgid:a@x.com)" in out

    def test_plural_count(self):
        out = _review_requests_section(
            [self._req(), self._req(shop="Other")], None, 30,
        )
        assert "2 review requests (last 30 days, one per order)." in out

    def test_omits_all_link_when_none(self):
        out = _review_requests_section([self._req()], None, 30)
        assert "See all review requests" not in out

    def test_handles_missing_url_and_age(self):
        out = _review_requests_section(
            [self._req(url=None, days_ago=None)], "https://all", 30,
        )
        assert "**Suzushii Clothing**" in out
        assert "[open]" not in out

    def test_wired_into_build_digest(self):
        out = build_digest({
            "items": [],
            "review_requests": [self._req()],
            "review_requests_all_url": "https://all/search",
            "review_requests_days": 30,
        })
        assert "## Review requests" in out
        assert "Suzushii Clothing" in out

    def test_absent_from_build_digest_when_empty(self):
        out = build_digest({"items": [_on_sale()]})
        assert "## Review requests" not in out


# ---------------------------------------------------------------------------
# Per-variant (size + colour) low-stock, colour availability, and transitions
# ---------------------------------------------------------------------------

_ANCHOR = "2026-06-10T14:00:00Z"


def _with_variants(item, *, variants=None, variant_history=None,
                   variant_changes=None, last_checked=_ANCHOR):
    """Attach a per-variant snapshot/history/changes to a digest test item."""
    e = item["result"]["updated_entry"]
    e["last_checked"] = last_checked
    if variants is not None:
        e["variants"] = variants
    if variant_history is not None:
        e["variant_history"] = variant_history
    if variant_changes is not None:
        item["result"]["variant_changes"] = variant_changes
    return item


class TestVariantNotes:
    def test_size_low_marker_with_duration(self):
        item = _item()
        _add_size_data(item, available=("M", "L"))
        _with_variants(
            item,
            variants={"size": {"options": ["S", "M", "L", "XL"],
                               "available": ["M", "L"], "low": ["L"]}},
            variant_history={"size": {"L": ["2026-06-05:low"]}},
        )
        out = build_digest({"items": [item], "today": date(2026, 6, 10)})
        assert "in stock in M, L (L low 5d)" in out

    def test_size_low_marker_without_history_has_no_duration(self):
        item = _item()
        _add_size_data(item, available=("M", "L"))
        _with_variants(item, variants={"size": {"options": ["S", "M", "L", "XL"],
                                                "available": ["M", "L"], "low": ["L"]}})
        out = build_digest({"items": [item], "today": date(2026, 6, 10)})
        assert "in stock in M, L (L low)" in out

    def test_transition_sold_out_shows_on_roster(self):
        item = _with_variants(
            _item(),
            variant_changes={"size": [{"value": "M", "from": "in", "to": "out"}]},
        )
        out = build_digest({"items": [item], "today": date(2026, 6, 10)})
        assert "M sold out" in out

    def test_transitions_grouped_by_phrase(self):
        item = _with_variants(
            _item(),
            variant_changes={
                "size": [{"value": "M", "from": "in", "to": "out"},
                         {"value": "L", "from": "in", "to": "low"}],
                "color": [{"value": "Black", "from": "out", "to": "in"}],
            },
        )
        out = build_digest({"items": [item], "today": date(2026, 6, 10)})
        assert "M sold out; L now low; Black back in stock" in out

    def test_color_note_flags_sold_out_colour(self):
        item = _with_variants(
            _item(),
            variants={"color": {"options": ["Black", "Olive", "Red"],
                                "available": ["Black", "Olive"], "low": []}},
        )
        out = build_digest({"items": [item], "today": date(2026, 6, 10)})
        assert "colors: Black, Olive (Red sold out)" in out

    def test_color_note_absent_when_all_available(self):
        item = _with_variants(
            _item(),
            variants={"color": {"options": ["Black", "Red"],
                                "available": ["Black", "Red"], "low": []}},
        )
        out = build_digest({"items": [item], "today": date(2026, 6, 10)})
        assert "colors:" not in out

    def test_color_low_with_duration(self):
        item = _with_variants(
            _item(),
            variants={"color": {"options": ["Black", "Olive"],
                                "available": ["Black", "Olive"], "low": ["Olive"]}},
            variant_history={"color": {"Olive": ["2026-06-08:low"]}},
        )
        out = build_digest({"items": [item], "today": date(2026, 6, 10)})
        assert "colors: Black, Olive (Olive low 2d)" in out


# ---------------------------------------------------------------------------
# Priority "Watching now" block (inline-⭐-flagged items pinned to the top)
# ---------------------------------------------------------------------------

def _priority_item(**result_overrides) -> dict:
    item = _item(**result_overrides)
    item["priority"] = True
    return item


class TestPrioritySection:
    def test_no_section_when_nothing_flagged(self):
        out = build_digest({"items": [_item()]})
        assert "Watching now" not in out

    def test_section_renders_at_top(self):
        out = build_digest({"items": [_priority_item()]})
        assert "## ⭐ Watching now" in out
        # Pinned ahead of everything else (here, the roster).
        assert out.index("Watching now") < out.index("All items by shop")

    def test_unchanged_item_shows_explicit_status(self):
        # A no-change, in-stock item still spells out price + verdicts.
        out = build_digest({"items": [_priority_item()]})
        line = next(l for l in out.splitlines() if l.startswith("- **Cool Shirt**"))
        assert "$50" in line
        assert "not on sale" in line
        assert "in stock" in line

    def test_on_sale_priority_line(self):
        item = _priority_item(sale_signal="on_sale_per_page")
        item["result"]["updated_entry"]["current_price"] = 40.0
        item["result"]["updated_entry"]["original_price"] = 80.0
        out = build_digest({"items": [item]})
        assert "on sale, was $80" in out
        assert "$40" in out

    def test_out_of_stock_priority_line(self):
        item = _priority_item(stock_signal="newly_out_of_stock")
        item["result"]["updated_entry"]["in_stock"] = False
        out = build_digest({"items": [item]})
        assert "newly out of stock" in out

    def test_standing_discount_priority_line(self):
        out = build_digest({"items": [_priority_item(sale_signal="standing_discount")]})
        assert "marked down (no real drop)" in out

    def test_priority_item_suppressed_from_change_sections(self):
        # An on-sale priority item must not ALSO appear in "Items on sale".
        item = _priority_item(sale_signal="on_sale_per_page")
        item["result"]["updated_entry"]["current_price"] = 40.0
        item["result"]["updated_entry"]["original_price"] = 80.0
        out = build_digest({"items": [item]})
        assert "## Items on sale (specific URLs)" not in out

    def test_priority_item_still_in_roster(self):
        out = build_digest({"items": [_priority_item()]})
        # Appears at top AND in the exhaustive roster (2 bullet lines for it).
        assert out.count("- **Cool Shirt**") == 2

    def test_non_priority_sibling_still_in_change_section(self):
        # A non-priority on-sale item keeps its normal section; only the flagged
        # one moves to the top block.
        watched = _priority_item()  # no-change, just watched
        on_sale = _item(url="https://shop.example.com/products/other",
                        sale_signal="on_sale_per_page")
        on_sale["result"]["updated_entry"]["label"] = "Other Shirt"
        on_sale["result"]["updated_entry"]["current_price"] = 40.0
        on_sale["result"]["updated_entry"]["original_price"] = 80.0
        out = build_digest({"items": [watched, on_sale]})
        assert "## ⭐ Watching now" in out
        assert "## Items on sale (specific URLs)" in out
        assert "Other Shirt" in out

    def test_could_not_check_priority_line(self):
        item = {
            "url": URL, "shop": "ExampleShop", "is_uncertain": False,
            "priority": True, "result": _err_result(),
        }
        out = build_digest({"items": [item]})
        assert "## ⭐ Watching now" in out
        assert "couldn't check" in out

    def test_non_clothing_priority_pinned_to_same_top_block(self):
        item = _priority_item()
        item["shop"] = "Logitech"
        out = build_digest({
            "items": [item],
            "non_clothing_shops": ["Logitech"],
        })
        # One top block, above the "# Non-clothing" divider's roster.
        assert "## ⭐ Watching now" in out
        assert out.index("Watching now") < out.index("# Non-clothing")


# ---------------------------------------------------------------------------
# "Amazon (price not tracked)" block — un-crawlable URLs, surfaced read-only
# ---------------------------------------------------------------------------

class TestUntrackedLabel:
    def test_descriptive_slug_becomes_title(self):
        url = "https://www.amazon.com/Amazon-Essentials-Lightweight-Pullover/dp/B07YF5CR5Z"
        assert _untracked_label(url) == "Amazon Essentials Lightweight Pullover"

    def test_bare_dp_falls_back_to_asin(self):
        assert _untracked_label("https://www.amazon.com/dp/B0DBQ5C9P5") == "Amazon item B0DBQ5C9P5"

    def test_gp_product_falls_back_to_asin(self):
        assert _untracked_label("https://www.amazon.com/gp/product/B08NY1QFQR") == "Amazon item B08NY1QFQR"


class TestUntrackedSection:
    def _untracked(self, **over) -> dict:
        base = {
            "url": "https://www.amazon.com/Some-Hoodie/dp/B07YF5CR5Z",
            "shop": "Amazon", "is_clothing": True,
        }
        base.update(over)
        return base

    def test_no_section_when_empty(self):
        out = build_digest({"items": [], "untracked_items": []})
        assert "price not tracked" not in out

    def test_section_renders_titled_link(self):
        out = build_digest({"items": [], "untracked_items": [self._untracked()]})
        assert "## Amazon (price not tracked)" in out
        assert (
            "- **Some Hoodie** — [link](https://www.amazon.com/Some-Hoodie/dp/B07YF5CR5Z)"
            in out
        )

    def test_multiple_sorted_by_title(self):
        out = build_digest({"items": [], "untracked_items": [
            self._untracked(url="https://www.amazon.com/Zebra-Tee/dp/B000000001"),
            self._untracked(url="https://www.amazon.com/Apple-Tee/dp/B000000002"),
        ]})
        assert out.index("Apple Tee") < out.index("Zebra Tee")

    def test_coexists_with_real_items(self):
        item = _item(sale_signal="on_sale_per_page")
        item["result"]["updated_entry"]["current_price"] = 40.0
        item["result"]["updated_entry"]["original_price"] = 80.0
        out = build_digest({"items": [item], "untracked_items": [self._untracked()]})
        assert "## Items on sale (specific URLs)" in out
        assert "## Amazon (price not tracked)" in out


class TestUntrackedSmsSection:
    def _s(self, brand, excerpt="50% off everything today", number="49469"):
        return {"brand": brand, "number": number, "excerpt": excerpt, "email_id": "x"}

    def test_none_when_empty(self):
        assert _untracked_sms_section([]) is None

    def test_aggregates_by_brand_with_count_and_example(self):
        out = _untracked_sms_section([
            self._s("Grey Fox", "BOGO 70% Off thousands of styles"),
            self._s("Grey Fox", "2 for $25 tees"),
            self._s("Harborlight", "men's summer collection, shop the sale"),
        ])
        assert "## Untracked SMS senders" in out
        # Hint points the user at the Doc-managed allowlist section.
        assert "Shops to track sales for:" in out
        assert "**Grey Fox** (2 texts)" in out
        assert "**Harborlight** (1 text)" in out  # singular
        # busiest brand first
        assert out.index("Grey Fox") < out.index("Harborlight")
        # the first example for the brand is shown
        assert "BOGO 70% Off" in out

    def test_blank_brand_ignored(self):
        assert _untracked_sms_section([self._s("")]) is None

    def test_wired_through_build_digest(self):
        out = build_digest({"items": [], "untracked_sms": [self._s("Junewave")]})
        assert "## Untracked SMS senders" in out
        assert "**Junewave** (1 text)" in out
