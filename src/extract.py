"""Fetch a product URL and extract price/availability data."""

from __future__ import annotations

import json
import logging
import re
import warnings
from typing import Any

import extruct
import httpx
from bs4 import BeautifulSoup

from src.http_util import get_with_retry

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
_TIMEOUT = 15.0
_LOW_STOCK_THRESHOLD = 5

_SCHEMA_OOS = frozenset({
    "http://schema.org/OutOfStock",
    "https://schema.org/OutOfStock",
    "OutOfStock",
    "http://schema.org/SoldOut",
    "https://schema.org/SoldOut",
    "SoldOut",
})

# Matches "only 3 left", "hurry, only 2 remaining", "4 items in stock"
_LOW_STOCK_RE = re.compile(
    r"only\s+(\d+)\s+left"
    r"|hurry[,!.\s]+(?:only\s+)?(\d+)\s+(?:left|remaining)"
    r"|(\d+)\s+(?:items?\s+)?(?:in\s+stock|remaining)\b",
    re.IGNORECASE,
)
_OOS_TEXT_RE = re.compile(r"\b(?:sold\s*out|out\s*of\s*stock)\b", re.IGNORECASE)


def _to_float(val: Any) -> float | None:
    """Convert a price-like value to float; None for blank or non-numeric input."""
    if val is None:
        return None
    try:
        s = str(val).replace(",", "").strip()
        return float(s) if s else None
    except (ValueError, TypeError):
        return None


_COLOR_OPTION_NAMES = frozenset({"color", "colour", "colors", "colours"})
_SIZE_OPTION_NAMES = frozenset({"size", "sizes"})

# Canonical size aliases. Shops spell letter sizes many ways ("Medium",
# "X-Large", "2XL"); normalise them so they compare against the user's
# PREFERRED_SIZES shortlist. Labels not in this map (numeric ring sizes like
# "7", pants waists like "32") pass through unchanged so they still compare
# as themselves.
_SIZE_ALIASES = {
    "XXS": "XXS", "XXSMALL": "XXS", "2XS": "XXS",
    "XS": "XS", "XSMALL": "XS", "EXTRASMALL": "XS",
    "S": "S", "SM": "S", "SMALL": "S",
    "M": "M", "MED": "M", "MEDIUM": "M",
    "L": "L", "LG": "L", "LARGE": "L",
    "XL": "XL", "XLARGE": "XL", "EXTRALARGE": "XL", "1XL": "XL",
    "XXL": "XXL", "XXLARGE": "XXL", "2XL": "XXL", "2X": "XXL",
    "XXXL": "XXXL", "XXXLARGE": "XXXL", "3XL": "XXXL", "3X": "XXXL",
}


def _normalize_size(label: Any) -> str:
    """Canonicalise a size label for comparison (e.g. 'X-Large' -> 'XL')."""
    cleaned = re.sub(r"[^A-Z0-9]", "", str(label).upper())
    return _SIZE_ALIASES.get(cleaned, cleaned)


def _is_low_total(qtys: list) -> bool:
    """True when every qty is a known int and their total sits in (0, threshold].

    Conservative, mirroring ``_woo_low_stock``: a single unknown qty among the
    in-stock variants of a value means we won't call it low (we'd rather miss a
    low-stock flag than raise a false one). ``0`` total (oversell with no real
    stock) isn't "low" either.
    """
    known = [q for q in qtys if isinstance(q, int) and not isinstance(q, bool)]
    if not known or len(known) != len(qtys):
        return False
    total = sum(known)
    return 0 < total <= _LOW_STOCK_THRESHOLD


def _dimension_availability(
    variants: list[dict], opt_key: str, offered: list[str]
) -> tuple[list[str], list[str]]:
    """Per-value availability + low-stock for one Shopify option dimension.

    ``available`` is every value with at least one in-stock variant, in the
    product's own option order (values seen only on variants are appended after).
    ``low`` is the subset whose in-stock variants ALL report a known
    ``inventory_quantity`` summing to (0, threshold] — so "M low" means few M
    remain across whatever colours carry it. Only meaningful when the JSON
    exposes per-variant ``available``; callers gate on ``has_availability``.
    """
    in_stock_qty: dict[str, list] = {}
    for v in variants:
        if not v.get("available", True):
            continue
        raw = v.get(opt_key)
        if raw is None:
            continue
        label = str(raw).strip()
        if not label:
            continue
        in_stock_qty.setdefault(label, []).append(v.get("inventory_quantity"))
    available = [val for val in offered if val in in_stock_qty]
    available += [val for val in in_stock_qty if val not in available]
    low = [val for val in available if _is_low_total(in_stock_qty[val])]
    return available, low


def _parse_shopify_json(data: dict) -> dict:
    """
    Extract price, stock, and variant data from a Shopify product.json payload.

    '_has_availability' signals whether out_of_stock and available_variant_count
    can be trusted — many Shopify stores omit the 'available' field from public
    product JSON, in which case we fall back to HTML-based OOS detection.

    When a Shopify product exposes a "Size" option, ``size_options`` lists
    every size value the product offers and ``available_sizes`` lists the
    subset for which at least one variant has ``available=True``. Both stay
    empty when the product has no size option or the JSON omits availability.
    """
    product = data.get("product", {})
    variants: list[dict] = product.get("variants") or []
    if not variants:
        return {}

    has_availability = any("available" in v for v in variants)

    if has_availability:
        available_variants = [v for v in variants if v.get("available", True)]
        out_of_stock: bool | None = not bool(available_variants)
        variant = available_variants[0] if available_variants else variants[0]
        available_count: int | None = len(available_variants)
    else:
        out_of_stock = None  # unknown; caller should fall back to HTML
        variant = variants[0]
        available_count = None

    price = _to_float(variant.get("price"))
    compare_at = _to_float(variant.get("compare_at_price"))
    # Only treat compare_at as a real markdown when strictly greater than price.
    # Guards against Shopify stores that set compare_at_price = "0.00" as a
    # placeholder while still labelling the current price "Sale price".
    original_price = compare_at if (compare_at and price and compare_at > price) else None

    currency = variant.get("price_currency") or "USD"

    inventory = variant.get("inventory_quantity")
    low_stock: bool | None = None
    if inventory is not None and out_of_stock is False:
        low_stock = 0 < inventory <= _LOW_STOCK_THRESHOLD

    # Color + Size options — find the option whose name matches "color"/"colour"
    # or "size" and record its values and 1-based position (option1/2/3) so we
    # can pull each variant's value for that dimension.
    color_options: list[str] = []
    color_option_index: int | None = None
    size_option_index: int | None = None
    size_options: list[str] = []
    for opt in product.get("options") or []:
        name = (opt.get("name") or "").strip().lower()
        values = [
            str(v).strip() for v in (opt.get("values") or [])
            if v is not None and str(v).strip()
        ]
        if color_option_index is None and name in _COLOR_OPTION_NAMES:
            color_option_index = opt.get("position") or 1
            color_options = values
        if size_option_index is None and name in _SIZE_OPTION_NAMES:
            size_option_index = opt.get("position") or 1
            size_options = values

    # Per-value availability + low-stock for each dimension — only meaningful
    # when the JSON exposes 'available' on variants. Without it we can't tell
    # which values are in stock, so the lists stay empty and callers fall back
    # to existing page-level OOS logic. ``available_sizes`` is the size
    # dimension's availability (kept as a top-level field for back-compat).
    size_available: list[str] = []
    size_low: list[str] = []
    color_available: list[str] = []
    color_low: list[str] = []
    if has_availability:
        if size_option_index is not None:
            size_available, size_low = _dimension_availability(
                variants, f"option{size_option_index}", size_options)
        if color_option_index is not None:
            color_available, color_low = _dimension_availability(
                variants, f"option{color_option_index}", color_options)

    # Only expose a dimension in ``variants`` when availability is actually
    # known (the JSON carried 'available'). A present dimension therefore always
    # means in/low/out is real — never "we couldn't tell" — so downstream stock
    # tracking doesn't mistake unknown for sold-out.
    variants_map: dict = {}
    if has_availability:
        if size_options:
            variants_map["size"] = {"options": size_options,
                                    "available": size_available, "low": size_low}
        if color_options:
            variants_map["color"] = {"options": color_options,
                                     "available": color_available, "low": color_low}

    return {
        "label": product.get("title"),
        "current_price": price,
        "original_price": original_price,
        "currency": currency,
        "out_of_stock": out_of_stock,
        "low_stock": low_stock,
        "total_variant_count": len(variants),
        "available_variant_count": available_count,
        "color_options": color_options,
        "size_options": size_options,
        "available_sizes": size_available,
        "variants": variants_map,
        "_has_availability": has_availability,
    }


def _woo_qty(raw: Any) -> int | None:
    """Parse a WooCommerce ``max_qty`` to int; ``None`` when unknown/blank.

    ``max_qty`` is an int (or digit string) when WooCommerce manages inventory,
    else ``""`` (stock-management off). ``None`` signals "unknown" to callers.
    """
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw)
    return None


def _woo_low_stock(variations: list[dict]) -> bool:
    """Product-level low-stock signal from WooCommerce ``max_qty``.

    True only when every in-stock variation reports a known quantity AND all of
    them are at or below the threshold. Returns False the moment any in-stock
    variant's quantity is unknown (stock-management off → ``max_qty`` is ``""``)
    — we won't claim "low" without full visibility, matching the conservative
    page-text heuristic it replaces.
    """
    qtys: list[int] = []
    for v in variations:
        if not v.get("is_in_stock"):
            continue
        q = _woo_qty(v.get("max_qty"))
        if q is None:
            return False  # unknown qty on an in-stock variant
        qtys.append(q)
    return bool(qtys) and max(qtys) <= _LOW_STOCK_THRESHOLD


def _woo_attr_key(variations: list[dict], needles: tuple[str, ...]) -> str | None:
    """First variation-attribute key whose name contains any of ``needles``.

    Generalises the original size-key lookup to any dimension — e.g.
    ``("size",)`` finds ``attribute_pa_size``; ``("color", "colour")`` finds
    ``attribute_pa_color`` or ``attribute_pa_colour``.
    """
    return next(
        (k for v in variations for k in (v.get("attributes") or {})
         if any(n in k.lower() for n in needles)),
        None,
    )


def _woo_dimension(form: Any, variations: list[dict], attr_key: str) -> dict:
    """Per-value ``options``/``available``/``low`` for one WooCommerce dimension.

    The size-or-colour generalisation of the original size logic: display labels
    + canonical order come from the matching ``<select>`` (slugs like ``xxxl``
    render as ``XXXL``); a value is available if any in-stock variation carries
    it (an empty attribute value spans every value, so one in-stock spanning
    variation makes all available); a value is low if it's available, not covered
    by a spanning in-stock variation (whose ``max_qty`` is dimension-wide and
    ambiguous), and every exact in-stock variation carrying it reports a known
    ``max_qty`` whose max is ≤ threshold.
    """
    slug_to_label: dict[str, str] = {}
    order: list[str] = []
    select = form.find("select", attrs={"name": attr_key})
    for opt in (select.find_all("option") if select else []):
        slug = (opt.get("value") or "").strip()
        if not slug:
            continue  # "Choose an option" placeholder
        slug_to_label[slug] = opt.get_text(strip=True) or slug.upper()
        order.append(slug)

    offered: list[str] = list(order)
    available: set[str] = set()
    spans_all_in_stock = False
    for v in variations:
        slug = str((v.get("attributes") or {}).get(attr_key) or "").strip()
        if slug and slug not in offered:
            offered.append(slug)  # value absent from the <select>; keep DOM order
        if not v.get("is_in_stock"):
            continue
        if slug:
            available.add(slug)
        else:
            spans_all_in_stock = True
    if spans_all_in_stock:
        available.update(offered)

    def label(slug: str) -> str:
        return slug_to_label.get(slug, slug.upper())

    low_slugs: list[str] = []
    if not spans_all_in_stock:
        for slug in offered:
            if slug not in available:
                continue
            qtys: list[int] = []
            ok = True
            for v in variations:
                if not v.get("is_in_stock"):
                    continue
                if str((v.get("attributes") or {}).get(attr_key) or "").strip() != slug:
                    continue
                q = _woo_qty(v.get("max_qty"))
                if q is None:
                    ok = False
                    break
                qtys.append(q)
            if ok and qtys and max(qtys) <= _LOW_STOCK_THRESHOLD:
                low_slugs.append(slug)

    return {
        "options": [label(s) for s in offered],
        "available": [label(s) for s in offered if s in available],
        "low": [label(s) for s in low_slugs],
    }


def _woo_price(variations: list[dict]) -> dict:
    """Price + compare-at from a representative WooCommerce variation.

    Prefers the first in-stock variation (the price the buyer would actually
    pay), falling back to the first variation when all are OOS — mirroring the
    Shopify variant pick. ``display_price`` is the live (sale) price and
    ``display_regular_price`` the pre-markdown price; a markdown counts only
    when strictly greater than the live price, guarding the same way
    ``_parse_shopify_json`` does against a 0/placeholder regular price.
    """
    in_stock = [v for v in variations if v.get("is_in_stock")]
    pv = in_stock[0] if in_stock else variations[0]
    current = _to_float(pv.get("display_price"))
    regular = _to_float(pv.get("display_regular_price"))
    original = regular if (regular and current and regular > current) else None
    return {"current_price": current, "original_price": original}


def _parse_woocommerce_variations(soup: BeautifulSoup) -> dict:
    """Extract price + per-size availability from a WooCommerce variable product.

    The WooCommerce analog of ``_parse_shopify_json``: instead of a ``.json``
    endpoint, a variable product inlines its variations as a JSON array on the
    cart form's ``data-product_variations`` attribute. Each entry has an
    ``attributes`` map (``{"attribute_pa_size": "xl", ...}``), ``display_price``
    / ``display_regular_price`` floats, an ``is_in_stock`` bool, and ``max_qty``
    (the stock count when WooCommerce manages inventory, else ``""``).
    WooCommerce only inlines this when the variation count is below the theme's
    AJAX threshold (default 30); above it the attribute is the string ``"false"``
    and the data loads over AJAX (not available to us here).

    Returns ``current_price`` / ``original_price`` plus ``size_options`` /
    ``available_sizes`` (display-cased, in the product's own option order) and
    product-level ``out_of_stock`` / ``low_stock`` so both price extraction and
    the size-aware OOS override in ``parse`` work on Woo shops exactly as they
    do on Shopify. Returns ``{}`` when the page has no parseable inline
    variation data — simple (non-variable) products, AJAX-threshold products,
    or non-Woo pages — so callers fall back to page-level HTML/JSON-LD parsing.
    """
    form = soup.find("form", class_="variations_form")
    if form is None:
        return {}
    try:
        variations = json.loads(form.get("data-product_variations") or "")
    except (ValueError, TypeError):
        return {}
    if not isinstance(variations, list) or not variations:
        return {}  # "false" above the inline threshold, or genuinely empty

    price = _woo_price(variations)

    # Parse each variant dimension present (size and/or colour). The size
    # dimension still drives the legacy size_options/available_sizes fields and
    # the product-level stock decision; colour is tracked alongside it in the
    # ``variants`` map. Both come from the same <select>-labelled inline JSON.
    size_key = _woo_attr_key(variations, ("size",))
    color_key = _woo_attr_key(variations, ("color", "colour"))

    variants_map: dict = {}
    if size_key is not None:
        variants_map["size"] = _woo_dimension(form, variations, size_key)
    if color_key is not None and color_key != size_key:
        variants_map["color"] = _woo_dimension(form, variations, color_key)

    if size_key is not None:
        size_dim = variants_map["size"]
        size_options = size_dim["options"]
        available_sizes = size_dim["available"]
        out_of_stock = not available_sizes
        low_stock = _woo_low_stock(variations) if available_sizes else False
    else:
        # Variable product with no size dimension (e.g. colour-only). Report
        # product-level stock from any in-stock variation so an all-OOS product
        # is still caught and its price extracted; size arrays stay empty.
        size_options = []
        available_sizes = []
        out_of_stock = not any(v.get("is_in_stock") for v in variations)
        low_stock = False

    return {
        **price,
        "size_options": size_options,
        "available_sizes": available_sizes,
        "out_of_stock": out_of_stock,
        "low_stock": low_stock,
        "variants": variants_map,
    }


def _price_specs(raw: Any) -> list[dict]:
    """Normalize a JSON-LD ``priceSpecification`` into a flat list of spec dicts.

    Handles the three shapes seen in the wild: a single spec dict, a list of
    specs, and the numeric-keyed dict-of-specs some WooCommerce SEO plugins
    emit (``{"0": {...UnitPriceSpecification...}, "priceCurrency": "USD"}``) —
    where the real spec is nested one level down under a stringified index.
    """
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, dict)]
    if isinstance(raw, dict):
        nested = [v for v in raw.values() if isinstance(v, dict) and "price" in v]
        return nested or [raw]
    return []


def _is_list_price(spec: dict) -> bool:
    pt = spec.get("priceType", "") or spec.get("@type", "")
    return "ListPrice" in pt or "RRP" in pt


def _jsonld_price(items: list[dict]) -> dict:
    """Extract price and availability from extruct JSON-LD output."""
    for item in items:
        if item.get("@type") != "Product":
            continue
        raw_offers = item.get("offers") or {}
        offers = raw_offers if isinstance(raw_offers, list) else [raw_offers]
        if not offers:
            continue
        offer = offers[0]

        price = _to_float(offer.get("price") or offer.get("lowPrice"))
        currency = offer.get("priceCurrency", "USD")
        availability = offer.get("availability", "")

        specs = _price_specs(offer.get("priceSpecification"))
        # WooCommerce SEO plugins omit a top-level offer.price and put the live
        # price in a (often nested) UnitPriceSpecification — fall back to the
        # first non-ListPrice spec so we don't drop to the page-wide $ regex.
        if price is None:
            for spec in specs:
                sp = _to_float(spec.get("price"))
                if sp is not None and not _is_list_price(spec):
                    price = sp
                    break

        original_price = next(
            (_to_float(spec.get("price")) for spec in specs if _is_list_price(spec)),
            None,
        )

        return {
            "label": item.get("name"),
            "current_price": price,
            "original_price": original_price,
            "currency": currency,
            "out_of_stock": availability in _SCHEMA_OOS,
        }
    return {}


def _extract_label(soup: BeautifulSoup) -> str | None:
    """Best-effort product name, independent of price (issue #8).

    Price extraction and label extraction are decoupled: a page we can reach but
    can't pin a price on (zero-price stub, JS-rendered price, odd markup) should
    still yield its name so the digest's "Could not check" section reads
    "BibiSama — Wave Shorts: could not check" instead of a bare URL.

    Only the **product-specific** structured meta — ``og:title`` / ``twitter:title``
    — is consulted, NOT the bare ``<title>`` tag. A real product template emits
    one of these; a bot-wall / DataDome challenge stub (HTTP 200/403 with a
    generic ``<title>etsy.com</title>``) emits neither, so a blocked page yields
    no phantom label (``TestEtsyBlocked::test_no_phantom_data``). Returns None
    when no product-title meta is present.
    """
    for prop in ("og:title", "twitter:title"):
        tag = (
            soup.find("meta", attrs={"property": prop})
            or soup.find("meta", attrs={"name": prop})
        )
        content = (tag.get("content") if tag else "") or ""
        if content.strip():
            return content.strip()
    return None


def _og_price(soup: BeautifulSoup) -> dict:
    """Extract price from OpenGraph meta tags."""
    tag = soup.find("meta", attrs={"property": "og:price:amount"})
    if not tag:
        return {}
    cur_tag = soup.find("meta", attrs={"property": "og:price:currency"})
    title_tag = soup.find("meta", attrs={"property": "og:title"})
    return {
        "label": title_tag.get("content") if title_tag else None,
        "current_price": _to_float(tag.get("content")),
        "original_price": None,
        "currency": cur_tag.get("content") if cur_tag else None,
        "out_of_stock": False,
    }


def _microdata_price(soup: BeautifulSoup) -> dict:
    """Extract price from microdata itemprop=price."""
    tag = soup.find(itemprop="price")
    if not tag:
        return {}
    val = tag.get("content") or tag.get_text(strip=True)
    return {
        "label": None,
        "current_price": _to_float(val),
        "original_price": None,
        "currency": None,
        "out_of_stock": False,
    }


def _regex_price(soup: BeautifulSoup) -> dict:
    """Last-resort regex price extraction. Unreliable — emits a warning."""
    matches = re.findall(r"\$\s*(\d+(?:\.\d{2})?)", soup.get_text())
    if not matches:
        return {}
    warnings.warn("extract: using regex price fallback — result may be inaccurate", stacklevel=4)
    return {
        "label": None,
        "current_price": float(matches[0]),
        "original_price": None,
        "currency": "USD",
        "out_of_stock": False,
    }


def _html_oos(soup: BeautifulSoup) -> bool:
    """Detect OOS from the add-to-cart submit button text."""
    # Scope to the cart-add form button: avoids page-wide "sold out" false
    # positives (FAQs, related-product badges). Known limitation on the
    # HTML-only path (Shopify JSON missing): button text reflects the default
    # variant only, so we can't tell if other sizes are still available. The
    # size-aware OOS override in ``parse`` covers the Shopify-JSON case;
    # non-Shopify / blocked-JSON shops still fall through this single boolean.
    forms = soup.find_all("form", attrs={"action": re.compile(r"/cart/add")})
    cart_buttons: list = []
    for form in forms:
        cart_buttons.extend(form.find_all("button"))
    if not cart_buttons:
        cart_buttons = soup.find_all("button", attrs={"name": "add"})

    for btn in cart_buttons:
        if _OOS_TEXT_RE.search(btn.get_text(strip=True)):
            return True
    return False


def _html_low_stock(soup: BeautifulSoup) -> tuple[bool, int | None]:
    """
    Detect low-stock banners from visible page text.
    Returns (is_low_stock, quantity_or_None).
    Conservative: returns (False, None) when no explicit count is found.
    """
    m = _LOW_STOCK_RE.search(soup.get_text(" ", strip=True))
    if m:
        count = int(next(g for g in m.groups() if g is not None))
        return count <= _LOW_STOCK_THRESHOLD, count
    return False, None


def _shopify_json_url(url: str) -> str:
    """Convert a Shopify product URL to its .json endpoint."""
    return url.split("?")[0].split("#")[0].rstrip("/") + ".json"


def _shopify_js_url(url: str) -> str:
    """Convert a Shopify product URL to its .js storefront endpoint."""
    return url.split("?")[0].split("#")[0].rstrip("/") + ".js"


def _merge_js_availability(product_json: dict, js_data: dict) -> None:
    """
    Patch per-variant ``available`` flags from the ``.js`` storefront payload
    into a ``.json`` product dict, matched by variant ``id``.

    Some storefronts omit ``available`` from the public ``.json`` endpoint but
    expose it on ``.js`` (the AJAX storefront API). We only borrow the boolean
    flags — never price — because ``.js`` reports prices in cents and omits
    ``price_currency``, so ``.json`` stays the price source. No-op when the
    ``.json`` variants already carry ``available`` or when ``.js`` doesn't.
    """
    variants = (product_json.get("product") or {}).get("variants") or []
    if not variants or any("available" in v for v in variants):
        return
    js_avail = {
        v.get("id"): v["available"]
        for v in (js_data.get("variants") or [])
        if v.get("id") is not None and "available" in v
    }
    if not js_avail:
        return
    for v in variants:
        avail = js_avail.get(v.get("id"))
        if avail is not None:
            v["available"] = avail


def parse(
    html: str,
    url: str,
    product_json: dict | None = None,
    *,
    preferred_sizes: tuple[str, ...] = (),
) -> dict:
    """
    Parse already-fetched HTML (and optional Shopify product JSON) into a
    price/availability dict.

    Tests call this directly with fixture HTML to avoid network requests.

    ``preferred_sizes`` is the user's size shortlist (e.g. ``("M", "L", "XL")``).
    When non-empty AND the product source exposes per-variant availability
    (Shopify ``.json``/``.js`` or WooCommerce inline ``data-product_variations``)
    AND the product has a Size option, the product is treated as out-of-stock
    when none of the preferred sizes is available — even if other sizes are.
    The surviving sizes (if any) are surfaced in ``unpreferred_available_sizes``
    so the digest can render a "still available in S, XL" note.

    Returns:
        current_price                  float | None
        original_price                 float | None   (compare-at; only when genuinely > current)
        currency                       str | None
        on_sale                        bool
        out_of_stock                   bool
        low_stock                      bool
        label                          str | None
        total_variant_count            int | None     (Shopify-only; None elsewhere)
        available_variant_count        int | None     (only when Shopify exposes 'available')
        color_options                  list[str]      (empty if no color option found)
        size_options                   list[str]      (every size the product offers; Shopify + WooCommerce)
        available_sizes                list[str]      (subset of size_options currently in stock)
        unpreferred_available_sizes    list[str]      (non-empty only when forced OOS by size preference)
        preferred_sizes_applied        list[str]      (echoes the preferred_sizes input; lets downstream stages persist the per-item preference without re-deriving)
        variants                       dict           ({dim: {options, available, low}} per-value availability for "size"/"colour"; empty when no per-variant source was available)
        error                          None           (always None; set by extract() on fetch failure)
        error_kind                     None           (always None; set by extract() on fetch failure)
    """
    result: dict = {
        "current_price": None,
        "original_price": None,
        "currency": None,
        "on_sale": False,
        "out_of_stock": False,
        "low_stock": False,
        "label": None,
        "total_variant_count": None,
        "available_variant_count": None,
        "color_options": [],
        "size_options": [],
        "available_sizes": [],
        "unpreferred_available_sizes": [],
        "preferred_sizes_applied": list(preferred_sizes),
        # Per-dimension availability snapshot {dim: {options, available, low}};
        # empty when no per-variant source (HTML-only / JSON-LD) was available.
        "variants": {},
        "error": None,
        "error_kind": None,
    }

    # --- Shopify JSON (most reliable source for price and compare-at) ---
    shopify: dict = _parse_shopify_json(product_json) if product_json else {}

    # --- HTML structured-data extraction ---
    soup = BeautifulSoup(html, "lxml")
    html_structured: dict = {}
    try:
        data = extruct.extract(html, base_url=url, syntaxes=["json-ld", "opengraph", "microdata"])
        html_structured = (
            _jsonld_price(data.get("json-ld", []))
            or _og_price(soup)
            or _microdata_price(soup)
        )
    except Exception as exc:
        log.warning("extruct failed for %s: %s", url, exc)
        html_structured = _og_price(soup) or _microdata_price(soup)

    # --- WooCommerce inline variation data (the Shopify-JSON analog for Woo
    #     shops: price, compare-at, and per-size availability all live on the
    #     variable-product form) ---
    woo: dict = _parse_woocommerce_variations(soup) if not shopify else {}

    # Only fall back to regex when no structured source found a price — the
    # whole-page regex grabs the first '$N' on the page (a shipping-notice
    # banner, a related product), so it's a genuine last resort.
    if not (html_structured.get("current_price") or shopify.get("current_price")
            or woo.get("current_price")):
        html_structured = _regex_price(soup)

    # --- Merge: Shopify JSON wins for price, then WooCommerce variation JSON,
    #     then HTML structured data. HTML wins for OOS when Shopify doesn't
    #     expose per-variant availability ---
    if shopify.get("current_price"):
        result["current_price"] = shopify["current_price"]
        result["original_price"] = shopify.get("original_price")
        result["currency"] = shopify.get("currency") or html_structured.get("currency") or "USD"
        result["label"] = shopify.get("label") or html_structured.get("label")
    elif woo.get("current_price"):
        result["current_price"] = woo["current_price"]
        result["original_price"] = woo.get("original_price")
        # The variation JSON carries no currency code or title; take them from
        # the page's JSON-LD/OG (WooCommerce reliably emits both).
        result["currency"] = html_structured.get("currency") or "USD"
        result["label"] = html_structured.get("label")
    elif html_structured.get("current_price"):
        result["current_price"] = html_structured["current_price"]
        result["original_price"] = html_structured.get("original_price")
        result["currency"] = html_structured.get("currency")
        result["label"] = html_structured.get("label")

    # --- Label (decoupled from price — issue #8) ---
    # The merge above only sets a label on the price-source branch it took, and
    # the regex fallback above can overwrite html_structured (dropping its
    # og:title). So when no label survived, pull one straight from the page's
    # meta tags. Runs unconditionally so a blocked/zero-price item still carries
    # its name into the digest (and persists via prices.json across later failing
    # runs — detect_sale keeps `label` when extraction returns None).
    if not result["label"]:
        result["label"] = shopify.get("label") or _extract_label(soup)

    # --- OOS / low-stock ---
    # Shopify JSON 'available' field is authoritative when present.
    # Most public Shopify product.json responses omit it, so we fall back to
    # HTML: JSON-LD availability > button-text patterns > class patterns.
    if shopify.get("_has_availability"):
        result["out_of_stock"] = bool(shopify.get("out_of_stock"))
        ls = shopify.get("low_stock")
        result["low_stock"] = bool(ls) if ls is not None else False
    elif woo:
        # WooCommerce inline variation data is authoritative for stock — it
        # reports per-variant availability directly, so prefer it over the
        # whole-page low-stock regex (which can't see which sizes are gone).
        result["out_of_stock"] = bool(woo.get("out_of_stock"))
        result["low_stock"] = bool(woo.get("low_stock"))
    else:
        html_oos = html_structured.get("out_of_stock", False)
        result["out_of_stock"] = html_oos or _html_oos(soup)
        if not result["out_of_stock"]:
            is_low, _ = _html_low_stock(soup)
            result["low_stock"] = is_low

    # --- Sale detection ---
    # Require compare_at strictly greater than current price.
    # This guards against the common Shopify "Sale price" theme label that
    # appears even when compare_at_price is "0.00" (no real markdown).
    cp = result["current_price"]
    op = result["original_price"]
    result["on_sale"] = bool(cp and op and op > cp)

    # --- Variant counts and color/size options ---
    if shopify:
        result["total_variant_count"] = shopify.get("total_variant_count")
        result["available_variant_count"] = shopify.get("available_variant_count")
        result["color_options"] = shopify.get("color_options") or []
        result["size_options"] = shopify.get("size_options") or []
        result["available_sizes"] = shopify.get("available_sizes") or []
        result["variants"] = shopify.get("variants") or {}
    elif woo:
        # WooCommerce supplies size + colour availability (no variant counts).
        result["size_options"] = woo.get("size_options") or []
        result["available_sizes"] = woo.get("available_sizes") or []
        result["variants"] = woo.get("variants") or {}

    # --- Size-aware OOS override ---
    # When the user supplies a preferred-sizes shortlist AND we have reliable
    # per-size availability, force OOS only if none of those sizes is in stock,
    # exposing the still-available sizes for the digest note. Size labels are
    # canonicalised first ("Medium" -> "M", "X-Large" -> "XL") so spelled-out
    # shops match. The filter only applies when the product actually OFFERS at
    # least one preferred size: products sized in a different space (e.g.
    # numeric ring sizes 7-11) have no M/L/XL equivalent, so they fall back to
    # plain any-size-in-stock availability. Products without a size option or
    # without per-variant 'available' also fall through untouched.
    if preferred_sizes and result["size_options"] and result["available_sizes"]:
        pref = {_normalize_size(s) for s in preferred_sizes if s and str(s).strip()}
        offered = {_normalize_size(s) for s in result["size_options"]}
        if pref & offered:  # product offers at least one of the user's sizes
            available = {_normalize_size(s) for s in result["available_sizes"]}
            if not (pref & available):
                result["out_of_stock"] = True
                result["low_stock"] = False  # OOS supersedes low-stock
                result["unpreferred_available_sizes"] = sorted(result["available_sizes"])

    return result


def _classify_error(exc: Exception | None, status: int | None) -> str:
    """Map an httpx exception or HTTP status code to a stable error_kind string."""
    if status is not None:
        if status == 404:
            return "not_found"
        if status in (403, 503):
            return "blocked"
        if status == 429:
            return "rate_limited"
        if 500 <= status < 600:
            return "server_error"
        return "other"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    return "other"


def extract(url: str, *, preferred_sizes: tuple[str, ...] = ()) -> dict:
    """
    Fetch a product URL and return price/availability data.

    Tries the Shopify .json endpoint first for /products/ URLs, then fetches
    and parses the HTML page. On HTTP error or connection failure, returns a
    dict with `error` and `error_kind` set, other fields at defaults.

    ``preferred_sizes`` is forwarded to ``parse`` for size-aware OOS detection.
    """
    result: dict = {
        "current_price": None,
        "original_price": None,
        "currency": None,
        "on_sale": False,
        "out_of_stock": False,
        "low_stock": False,
        "label": None,
        "total_variant_count": None,
        "available_variant_count": None,
        "color_options": [],
        "size_options": [],
        "available_sizes": [],
        "unpreferred_available_sizes": [],
        "preferred_sizes_applied": list(preferred_sizes),
        # Per-dimension availability snapshot {dim: {options, available, low}};
        # empty when no per-variant source (HTML-only / JSON-LD) was available.
        "variants": {},
        "error": None,
        "error_kind": None,
    }

    product_json: dict | None = None
    if "/products/" in url:
        json_url = _shopify_json_url(url)
        try:
            with httpx.Client(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
                resp = get_with_retry(client, json_url)
            if resp.status_code == 200:
                product_json = resp.json()
        except Exception as exc:
            log.debug("shopify .json unavailable for %s: %s", json_url, exc)

        # Some storefronts omit per-variant 'available' from .json. The .js
        # endpoint exposes it reliably; borrow just those flags (never price —
        # .js reports cents) so size-aware OOS detection can work.
        variants = (product_json or {}).get("product", {}).get("variants") or []
        if variants and not any("available" in v for v in variants):
            js_url = _shopify_js_url(url)
            try:
                with httpx.Client(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
                    js_resp = get_with_retry(client, js_url)
                if js_resp.status_code == 200:
                    _merge_js_availability(product_json, js_resp.json())
            except Exception as exc:
                log.debug("shopify .js unavailable for %s: %s", js_url, exc)

    try:
        with httpx.Client(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = get_with_retry(client, url)
    except Exception as exc:
        result["error"] = f"fetch failed: {exc}"
        result["error_kind"] = _classify_error(exc, None)
        return result

    if resp.status_code != 200:
        result["error"] = f"HTTP {resp.status_code}"
        result["error_kind"] = _classify_error(None, resp.status_code)
        return result

    parsed = parse(resp.text, url, product_json=product_json, preferred_sizes=preferred_sizes)
    result.update(parsed)
    return result
