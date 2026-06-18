"""Tests for src/restock_detect.py — pure regex/parse logic + locator fakes.

Playwright isn't spun up; small fakes mimic the sync API surface the helpers
call (``locator``, ``first``, ``nth``, ``count``, ``is_visible``,
``inner_text``, ``get_attribute``, ``select_option``, ``click``).
"""
from __future__ import annotations

from typing import Any

import pytest

from src import restock_detect as rd


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeLocator:
    def __init__(
        self, *, visible: bool = True, text: str = "",
        attrs: dict[str, str] | None = None,
        children: dict[str, "FakeLocator"] | None = None,
        items: list["FakeLocator"] | None = None,
    ) -> None:
        self._visible = visible
        self._text = text
        self._attrs = attrs or {}
        self._children = children or {}
        self._items = items or []
        self.clicked = 0
        self.selected: list[str] = []
        self.fill_calls: list[str] = []

    @property
    def first(self) -> "FakeLocator":
        return self

    def locator(self, selector: str) -> "FakeLocator":
        if selector in self._children:
            return self._children[selector]
        if selector == "option" and self._items:
            # Return a container whose nth()/count() expose the options.
            return _List(self._items)
        return FakeLocator(visible=False)

    def nth(self, i: int) -> "FakeLocator":
        return self._items[i] if i < len(self._items) else FakeLocator(visible=False)

    def count(self) -> int:
        return len(self._items) if self._items else 1

    def is_visible(self, timeout: int | None = None) -> bool:  # noqa: ARG002
        return self._visible

    def inner_text(self, timeout: int | None = None) -> str:  # noqa: ARG002
        return self._text

    def get_attribute(self, name: str) -> str | None:
        return self._attrs.get(name)

    def select_option(self, *, label: str, timeout: int | None = None) -> None:  # noqa: ARG002
        self.selected.append(label)

    def click(self, timeout: int | None = None) -> None:  # noqa: ARG002
        self.clicked += 1

    def fill(self, text: str) -> None:
        self.fill_calls.append(text)


class _List(FakeLocator):
    """A locator standing in for a multi-element match (options/swatches)."""

    def __init__(self, items: list[FakeLocator]) -> None:
        super().__init__(items=items)

    def count(self) -> int:
        return len(self._items)


class FakePage:
    def __init__(
        self, *, url: str = "https://shop.com/p/x",
        body_text: str = "", selectors: dict[str, FakeLocator] | None = None,
    ) -> None:
        self.url = url
        self._body_text = body_text
        self._selectors = selectors or {}
        self.waits: list[int] = []

    def locator(self, selector: str) -> FakeLocator:
        if selector in self._selectors:
            return self._selectors[selector]
        if selector == "body":
            return FakeLocator(text=self._body_text)
        return FakeLocator(visible=False)

    def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestVendorSelectors:
    def test_documented_vendors_present(self):
        assert {"klaviyo_bis", "swym_bis", "backinstock", "appikon"} <= set(
            rd.RESTOCK_VENDOR_SELECTORS.keys())


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

class TestRestockText:
    @pytest.mark.parametrize("text", [
        "Notify me when available",
        "Email me when back in stock",
        "We'll let you know when this is available",
        "Sold out — get an in-stock alert",
    ])
    def test_positive(self, text):
        assert rd.looks_like_restock_text(text)

    @pytest.mark.parametrize("text", [
        "Subscribe to our newsletter for 10% off",
        "Add to cart",
        "",
        None,
    ])
    def test_negative(self, text):
        assert not rd.looks_like_restock_text(text)


class TestRestockSuccess:
    @pytest.mark.parametrize("text", [
        "Thanks! We'll notify you when it's back.",
        "You're on the list.",
        "Request received — we'll email you.",
        "You're all set!",
    ])
    def test_positive(self, text):
        assert rd.looks_like_restock_success(text)

    @pytest.mark.parametrize("text", ["Out of stock", "Error: invalid email", ""])
    def test_negative(self, text):
        assert not rd.looks_like_restock_success(text)


class TestSizeMatches:
    def test_exact(self):
        assert rd.size_matches("M", "M")

    def test_spelled_out(self):
        assert rd.size_matches("Medium", "medium")
        assert rd.size_matches("X-Large", "xlarge")

    def test_token_within_compound_option(self):
        assert rd.size_matches("Medium / Black", "Medium")

    def test_s_does_not_match_xs(self):
        assert not rd.size_matches("XS", "S")

    def test_non_match(self):
        assert not rd.size_matches("Large", "M")

    def test_empty(self):
        assert not rd.size_matches("", "M")
        assert not rd.size_matches("M", "")


# ---------------------------------------------------------------------------
# detect_restock_form
# ---------------------------------------------------------------------------

class TestDetectRestockForm:
    def test_vendor_hit(self):
        sel = rd.RESTOCK_VENDOR_SELECTORS["klaviyo_bis"]
        page = FakePage(selectors={sel: FakeLocator(visible=True)})
        form, vendor = rd.detect_restock_form(page)
        assert vendor == "klaviyo_bis"
        assert form is not None

    def test_generic_requires_email_and_restock_text(self):
        # Generic container with an email field AND restock-looking text → match.
        from src.popup_detect import EMAIL_INPUT_SELECTOR
        container = FakeLocator(
            visible=True, text="Notify me when available",
            children={EMAIL_INPUT_SELECTOR: FakeLocator(visible=True)},
        )
        page = FakePage(selectors={rd.GENERIC_RESTOCK_SELECTOR: container})
        form, vendor = rd.detect_restock_form(page)
        assert vendor == "generic"
        assert form is not None

    def test_generic_rejected_without_email(self):
        container = FakeLocator(visible=True, text="Notify me when available")
        page = FakePage(selectors={rd.GENERIC_RESTOCK_SELECTOR: container})
        form, vendor = rd.detect_restock_form(page)
        assert (form, vendor) == (None, None)

    def test_generic_rejected_without_restock_text(self):
        from src.popup_detect import EMAIL_INPUT_SELECTOR
        container = FakeLocator(
            visible=True, text="Join our newsletter",
            children={EMAIL_INPUT_SELECTOR: FakeLocator(visible=True)},
        )
        page = FakePage(selectors={rd.GENERIC_RESTOCK_SELECTOR: container})
        form, vendor = rd.detect_restock_form(page)
        assert (form, vendor) == (None, None)

    def test_nothing_found(self):
        page = FakePage()
        assert rd.detect_restock_form(page) == (None, None)


# ---------------------------------------------------------------------------
# select_size
# ---------------------------------------------------------------------------

class TestSelectSize:
    def test_native_select_matches_option(self):
        options = [FakeLocator(text="Small"), FakeLocator(text="Medium"),
                   FakeLocator(text="Large")]
        select = FakeLocator(visible=True, items=options)
        # locator(selector) matches one <select> → model it as a 1-element list.
        page = FakePage(selectors={rd._SIZE_SELECT_SELECTOR: _List([select])})
        assert rd.select_size(page, "M") is True
        assert select.selected == ["Medium"]

    def test_swatch_click_when_no_select(self):
        swatches = _List([
            FakeLocator(visible=True, text="S"),
            FakeLocator(visible=True, text="M"),
        ])
        page = FakePage(selectors={rd._SIZE_SWATCH_SELECTOR: swatches})
        assert rd.select_size(page, "M") is True
        assert swatches.nth(1).clicked == 1

    def test_no_match_returns_false(self):
        select = FakeLocator(visible=True, items=[FakeLocator(text="XL")])
        page = FakePage(selectors={rd._SIZE_SELECT_SELECTOR: select})
        assert rd.select_size(page, "M") is False

    def test_empty_size(self):
        assert rd.select_size(FakePage(), "") is False


# ---------------------------------------------------------------------------
# select_size_in_form (form-scoped popup size selector)
# ---------------------------------------------------------------------------

class TestSelectSizeInForm:
    def test_form_select_matches_option(self):
        options = [FakeLocator(text="Small"), FakeLocator(text="Medium")]
        select = FakeLocator(visible=True, items=options)
        # locator(selector) matches one <select> → model it as a 1-element list.
        form = FakeLocator(visible=True, children={rd._SIZE_SELECT_SELECTOR: _List([select])})
        assert rd.select_size_in_form(form, "M") is True
        assert select.selected == ["Medium"]

    def test_any_select_fallback_matches_options(self):
        # Swym BIS uses <select id="swym-remind-me-oos-options"> — no 'size' in
        # the id, so the size-specific selector misses but the any-<select>
        # fallback matches by option text.
        options = [FakeLocator(text="Small"), FakeLocator(text="Medium")]
        swym = FakeLocator(visible=True, items=options)
        form = FakeLocator(visible=True, children={"select": _List([swym])})
        assert rd.select_size_in_form(form, "M") is True
        assert swym.selected == ["Medium"]

    def test_form_swatch_click_when_no_select(self):
        swatches = _List([
            FakeLocator(visible=True, text="S"),
            FakeLocator(visible=True, text="M"),
        ])
        form = FakeLocator(visible=True, children={rd._SIZE_SWATCH_SELECTOR: swatches})
        assert rd.select_size_in_form(form, "M") is True
        assert swatches.nth(1).clicked == 1

    def test_no_match_returns_false(self):
        select = FakeLocator(visible=True, items=[FakeLocator(text="XL")])
        form = FakeLocator(visible=True, children={rd._SIZE_SELECT_SELECTOR: select})
        assert rd.select_size_in_form(form, "M") is False

    def test_empty_size(self):
        assert rd.select_size_in_form(FakeLocator(visible=True), "") is False


# ---------------------------------------------------------------------------
# detect_restock_success
# ---------------------------------------------------------------------------

class TestDetectRestockSuccess:
    def test_message_on_form(self):
        form = FakeLocator(visible=True, text="Thanks! We'll notify you.")
        page = FakePage()
        assert rd.detect_restock_success(
            page, form, original_url="https://shop.com/p/x", post_submit_wait_ms=0)

    def test_form_disappeared_counts_as_success(self):
        form = FakeLocator(visible=False, text="")
        page = FakePage(body_text="")
        assert rd.detect_restock_success(
            page, form, original_url="https://shop.com/p/x", post_submit_wait_ms=0)

    def test_no_signal_is_failure(self):
        form = FakeLocator(visible=True, text="Out of stock")
        page = FakePage(url="https://shop.com/p/x", body_text="Out of stock")
        assert not rd.detect_restock_success(
            page, form, original_url="https://shop.com/p/x", post_submit_wait_ms=0)
