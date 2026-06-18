"""Local wardrobe browser — a personal web app to browse ``wardrobe.json``.

Separate from the daily cron. Run ``python -m src.wardrobe_browser`` to fetch
the wardrobe catalogue from the Gist (read-only), derive a garment category for
each item, and serve a single-page browser at http://localhost:8787 with four
ways in:

  * **Search** across shop / item name / colour / size / category.
  * **Timeline** — items grouped by purchase month, newest first.
  * **Categories** — filter to T-Shirts / Sweatpants / Jackets / ... .
  * **Brands** — grouped by brand, busiest first, with a type-to-filter box.
    Display-name and domain-slug spellings of one brand ("SORA Clothing" /
    "Soraclothing") are merged into a single brand (``brand_key``).

Local-only by design (no GitHub Actions workflow). Only ``GITHUB_TOKEN`` and
``GIST_ID`` are read from the environment (via ``.env``) — the full pipeline
config is not required, so the browser runs even when other secrets are absent.

**Read-only.** The browser never writes the Gist; it only GETs it. ``Refresh``
re-fetches the latest catalogue.

Garment categorisation prefers a **durable stored ``category``** stamped by
``order_scan`` (the Claude extraction at scan time, or the ``--classify``
backfill — issue #18); items not yet classified fall back to a **name
heuristic**: ``categorize`` checks an ordered list of apparel patterns, and the
non-clothing filter (``_NONCLOTHING_RE``) only runs on names that match no
apparel category, so a "Mickey Mouse Tee" stays a t-shirt. Names that match
neither land in the "Other" bucket (mostly design-only graphic tees whose names
carry no garment word, e.g. "Kitsune", "Raijin"). The name fallback is
imperfect by nature, which is exactly why the stored category exists — once an
item is classified, its category and ``is_clothing`` flag are read directly.

Future iteration: product images fetched from each item's shop page, cached in a
local ``--image-dir`` and served at ``/images/<id>``; until then every item
falls back to a per-category icon rendered client-side.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import threading
import time
import webbrowser
from collections import Counter
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote_plus

from src import fit_links, review_requests, state
from src.wardrobe_categories import (
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    NON_CLOTHING,
    VALID_KEYS,
    normalise_category,
)

log = logging.getLogger(__name__)

_UI_FILE = Path(__file__).with_name("wardrobe_browser.html")

# ---------------------------------------------------------------------------
# Garment categorisation
# ---------------------------------------------------------------------------
# Items stamped with a durable `category` by order_scan (the Claude extraction
# at scan time, or the `--classify` backfill — issue #18) are read straight
# from that field. The name-heuristic below is the FALLBACK for items not yet
# classified.

# Canonical keys + display labels come from the shared taxonomy
# (src/wardrobe_categories.py); CATEGORY_ORDER / CATEGORY_LABELS are imported
# above so the browser, the extractor, and the backfill all agree.

# "shorts"/"short" as a GARMENT noun — matches the glued "sweatshorts" too, but
# NOT the "short sleeve"/"short-sleeve" adjective (which the bare `shorts?` used
# to mis-grab as a pair of shorts). Used by all three shorts rules below.
_SHORTS_NOUN = r"shorts?(?!\s*-?\s*sleeve)"
# Use-signals that split shorts into athletic vs casual. Required *alongside*
# the shorts noun (lookaheads, so word order doesn't matter), so a "Sport Shirt"
# or "Cargo Pants" — no "shorts" — never trips these.
_SHORTS_ATHLETIC = (
    r"athletic|running|training|workout|\bgym\b|performance|compression|"
    r"\bmesh\b|\bactive\b|basketball|\bsport\b"
)
_SHORTS_CASUAL = (
    r"sweat\s?shorts?|chino|cargo|denim|\bjeans?\b|corduroy|lounge|fleece|"
    r"cotton|linen|\bboard\b|\bswim|casual"
)

# Name-heuristic fallback patterns, ordered by SPECIFICITY (first match wins)
# — distinct from the taxonomy's display order. Keys must be taxonomy keys.
#   * hoodie before sweatshirt   ("hooded sweatshirt" -> hoodie)
#   * sweatpants before pants    ("jogger sweatpants" -> sweatpants)
#   * shorts_athletic / shorts_casual before generic shorts, all before pants
#     ("mesh shorts" -> athletic, "cargo shorts" -> casual, plain -> shorts)
#   * tank / longsleeve before tshirt / shirt
#   * tshirt before shirt        ("graphic t-shirt" -> tshirt, not shirt)
# "knit" is deliberately NOT a sweater signal — "Waffle-Knit T-Shirt" is a tee.
_NAME_PATTERNS: list[tuple[str, str]] = [
    ("hoodie",     r"hoodie|hooded"),
    ("sweatshirt", r"sweatshirt|crew\s?neck|pullover|sweater|cardigan|jumper"),
    ("jacket",     r"jacket|coat|parka|windbreaker|bomber|\bvest\b|anorak|puffer"),
    ("sweatpants", r"sweat\s?pant|jogger|track\s?pant"),
    ("shorts_athletic", rf"(?=.*(?:{_SHORTS_ATHLETIC}))(?=.*{_SHORTS_NOUN})"),
    ("shorts_casual",   rf"(?=.*(?:{_SHORTS_CASUAL}))(?=.*{_SHORTS_NOUN})"),
    ("shorts",          _SHORTS_NOUN),
    ("tank",       r"\btank\b|sleeveless"),
    ("polo",       r"\bpolos?\b"),
    ("longsleeve", r"long.?sleeve"),
    ("tshirt",     r"t-?shirts?|\btee\b|\btees\b"),
    ("shirt",      r"shirt|button.?up|button.?down|flannel|jersey"),
    ("pants",      r"\bpants?\b|chino|trouser|jeans|denim|slacks|legging"),
    ("hat",        r"beanie|\bhat\b|\bcap\b|\bcaps\b|snapback|\bvisor\b"),
    ("socks",      r"socks?"),
    ("shoes",      r"shoes?|sneakers?|\bboots?\b|slippers?|sandals?|loafers?|slides?"),
    ("accessory",  r"\bglove|scarf|scarves|mitten|\bbelt\b|\brobe\b|bandana"),
    ("underwear",  r"boxers?|briefs?|underwear|trunks"),
]

_COMPILED_RULES: list[tuple[str, re.Pattern[str]]] = [
    (key, re.compile(pat, re.I)) for key, pat in _NAME_PATTERNS
]

# Non-clothing keywords. Only consulted for names that matched NO apparel
# category above, so an apparel word always wins (a "Mouse"-print tee is a tee).
# Broad on purpose — homeware, decor, electronics, supplements, grooming,
# furniture, jewelry, bags, games — to keep the "Other" bucket mostly real
# (design-named) garments. Word-boundary matched so it doesn't clip garments.
_NONCLOTHING_RE = re.compile(
    r"\b("
    r"rugs?|blankets?|throws?|pillows?|pillowcases?|duvets?|comforters?|quilts?|"
    r"shams?|towels?|washcloths?|bedding|bed\s?sheets?|mattress(?:es)?|curtains?|"
    r"cushions?|tablecloths?|napkins?|coasters?|placemats?|baskets?|"
    r"mugs?|candles?|vases?|lamps?|lights?|bulbs?|lanterns?|night\s?lights?|"
    r"cables?|chargers?|adapters?|usb|earbuds?|headphones?|speakers?|sound\s?bars?|"
    r"mice|mouse|keyboards?|monitors?|software|"
    r"plush|funko|figures?|figurines?|posters?|stickers?|keychains?|keyrings?|"
    r"key\s?tags?|pins?|mousepads?|"
    r"necklaces?|bracelets?|earrings?|pendants?|chains?|rings?|"
    r"creatine|whey|protein|supplements?|"
    r"deodorants?|shampoos?|conditioners?|sprays?|creams?|pomades?|"
    r"chairs?|stands?|display\s?cases?|cases?|"
    r"katanas?|dice|rpg|"
    r"headbands?|sunglasses|eyewear|aviators?|masks?|mirrors?|engraving|holders?|"
    r"bags?|totes?|duffles?|duffels?|slings?|backpacks?|wallets?|"
    r"brushes?|grocery|futons?|bonsai|tape"
    r")\b",
    re.I,
)


def categorize(name: str | None) -> str:
    """Return the garment-category key for an item name (``"other"`` if none)."""
    text = name or ""
    for key, pat in _COMPILED_RULES:
        if pat.search(text):
            return key
    return "other"


def classify_item(item: dict) -> tuple[str, bool]:
    """``(category_key, is_clothing)`` for a wardrobe item.

    A durable stored ``category`` (issue #18) wins: ``non_clothing`` (or a
    stored ``is_clothing == False``) hides the item; any other stored key is
    shown as that category. Items without a usable stored category fall back to
    the name heuristic: a stored ``is_clothing == False`` (from a Non-clothing
    watchlist match) is honoured, any apparel category is clothing, and an
    uncategorised name is clothing unless it trips the non-clothing filter.
    """
    stored = normalise_category(item.get("category"))
    if stored is not None:
        if stored == NON_CLOTHING or item.get("is_clothing") is False:
            return stored, False
        return stored, True

    name = item.get("item_name")
    cat = categorize(name)
    if item.get("is_clothing") is False:
        return cat, False
    if cat != "other":
        return cat, True
    if _NONCLOTHING_RE.search(name or ""):
        return cat, False
    return cat, True


# ---------------------------------------------------------------------------
# Payload building
# ---------------------------------------------------------------------------

def _price_display(price: dict | None) -> str | None:
    if not isinstance(price, dict):
        return None
    amount = price.get("amount")
    if amount is None:
        return None
    currency = (price.get("currency") or "USD").upper()
    symbol = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "C$", "AUD": "A$"}.get(currency, "")
    try:
        body = f"{float(amount):,.2f}"
    except (TypeError, ValueError):
        return None
    return f"{symbol}{body}" if symbol else f"{body} {currency}"


def _round_money(money: dict[str, float]) -> dict[str, float]:
    return {cur: round(v, 2) for cur, v in money.items()}


def _accumulate(bucket: dict[str, dict[str, float]], key: str, currency: str, amount: float) -> None:
    """Add ``amount`` to ``bucket[key][currency]`` (money math stays per-currency)."""
    sub = bucket.setdefault(key, {})
    sub[currency] = sub.get(currency, 0.0) + amount


def _image_url(item_id: str, image_dir: Path | None) -> str | None:
    """Local cached product image URL for an item, if one exists on disk.

    Iteration-2 hook: a future fetch step writes ``<image_dir>/<id>.<ext>``;
    this surfaces it as ``/images/<file>`` so the frontend can show the real
    product photo instead of a category icon. Empty/absent dir -> always None.
    """
    if not image_dir:
        return None
    for ext in ("jpg", "jpeg", "png", "webp", "gif"):
        f = image_dir / f"{item_id}.{ext}"
        if f.is_file():
            return f"/images/{f.name}"
    return None


_HREF_RE = re.compile(r"https?://[^\s)>\]]+", re.IGNORECASE)


def _direct_product_url(item: dict) -> tuple[str | None, str]:
    """A direct product URL for the item, plus which source it came from.

    Prefers the exact ``product_url`` stamped at scan time (issue #23), then a
    URL parsed out of the matched watchlist Doc line (present for items the user
    had watchlisted — points at the right *design*, occasionally a sibling cut).
    Returns ``(url, kind)`` with ``kind`` ``"product"`` or ``"watchlist"``, or
    ``(None, "")`` when neither exists."""
    pu = (item.get("product_url") or "").strip()
    if pu.startswith(("http://", "https://")):
        return pu, "product"
    match = item.get("watchlist_match")
    if isinstance(match, dict):
        found = _HREF_RE.search(match.get("matched_line") or "")
        if found:
            return found.group(0).rstrip(".,);'\""), "watchlist"
    return None, ""


def product_link(item: dict) -> dict | None:
    """The card / detail "find this product" link: a direct URL when we have
    one, else a Google search for the item.

    Tiered because only a fraction of the catalogue carries a stored URL:
    scan-time ``product_url`` (new purchases) and watchlist-matched lines give a
    direct link; everything else gets a forgiving Google search (``"name"
    shop``) that finds the live page, a moved listing, or nothing if it's long
    gone — the right behaviour for items bought years ago. Returns ``None`` only
    when there's neither a URL nor any text to search with. ``kind`` is
    ``"product"`` | ``"watchlist"`` | ``"search"`` so the UI can label it."""
    href, kind = _direct_product_url(item)
    if href:
        return {"href": href, "kind": kind}
    name = (item.get("item_name") or "").strip()
    shop = (item.get("shop") or "").strip()
    terms = " ".join(p for p in (f'"{name}"' if name else "", shop) if p).strip()
    if not terms:
        return None
    return {"href": "https://www.google.com/search?q=" + quote_plus(terms),
            "kind": "search"}


def _body_comp_summary(item: dict) -> dict | None:
    """Compact body-comp snapshot for the detail panel (or ``None``).

    The four metrics the user reasons about plus the scan date / provenance,
    pulled off the item's full ``body_comp`` block. Keeps the payload lean (no
    per-region breakdown).
    """
    bc = item.get("body_comp")
    if not isinstance(bc, dict):
        return None
    return {
        "weight_kg": bc.get("weight_kg"),
        "body_fat_pct": bc.get("body_fat_pct"),
        "lean_mass_kg": bc.get("lean_mass_kg"),
        "fat_mass_kg": bc.get("fat_mass_kg"),
        "scan_date": bc.get("scan_date"),
        "matched_to": bc.get("matched_to"),
        "days_from_event": bc.get("days_from_event"),
    }


def _normalise_item(item: dict, category: str, image_dir: Path | None) -> dict:
    purchased_at = (item.get("purchased_at") or "").strip()
    year = purchased_at[:4] if len(purchased_at) >= 4 else ""
    month = purchased_at[:7] if len(purchased_at) >= 7 else ""
    price = item.get("price_paid") if isinstance(item.get("price_paid"), dict) else None
    fit_review = item.get("fit_review") if isinstance(item.get("fit_review"), dict) else None
    # Pending = the canonical fit_links predicate (no review yet, is clothing).
    fit_pending = fit_review is None and item.get("is_clothing") is not False
    return {
        "id": item.get("id") or "",
        "name": (item.get("item_name") or "").strip(),
        "shop": (item.get("shop") or "").strip() or "Unknown",
        "shop_domain": item.get("shop_domain") or "",
        "category": category,
        "category_label": CATEGORY_LABELS.get(category, "Other"),
        "color": (item.get("color") or "").strip(),
        "size": (item.get("size") or "").strip(),
        "qty": item.get("qty") or 1,
        "price": price,
        "price_display": _price_display(price),
        "purchased_at": purchased_at,
        "year": year,
        "month": month,
        "image": _image_url(item.get("id") or "", image_dir),
        # Direct/search "view product" link (issue #23).
        "product_link": product_link(item),
        # Detail-panel fields (issue: frontend interactivity groundwork):
        "shipped_at": (item.get("shipped_at") or "") or None,
        "tracking_url": item.get("tracking_url") or None,
        "order_email_id": item.get("order_email_id") or None,
        "fit_review": fit_review,
        "fit_pending": fit_pending,
        "body_comp": _body_comp_summary(item),
    }


# ---------------------------------------------------------------------------
# Brand canonicalisation
# ---------------------------------------------------------------------------
# A "shop" is whoever the order email came from, and one brand can surface under
# several spellings: a From display-name ("SORA Clothing", via a shared
# shopifyemail.com sender) and a domain-derived slug ("Soraclothing", from
# soraclothing.com) are the *same* brand but never group together. Normalising
# the name — lowercase, drop every non-alphanumeric char — collapses both to the
# same key ("soraclothing"), so the browser can merge the variants into one
# brand with a combined count/spend. Pure name-based (no domain/Claude); a brand
# split across genuinely different retailers is out of scope here.

_BRAND_KEY_STRIP_RE = re.compile(r"[^a-z0-9]+")


def brand_key(name: str | None) -> str:
    """Normalised merge key for a brand/shop name.

    Lowercases and strips every non-alphanumeric character, so ``"SORA
    Clothing"``, ``"Soraclothing"`` and ``"sora-clothing"`` all map to
    ``"soraclothing"``.
    """
    return _BRAND_KEY_STRIP_RE.sub("", (name or "").lower())


def _canonical_brand_name(counts: Counter[str]) -> str:
    """Pick the nicest display name among the variants that share a brand key.

    Prefers a spaced, proper-cased name ("SORA Clothing") over a squashed
    domain slug ("Soraclothing"); then the more frequent, then the longer, then
    alphabetical for a stable tiebreak.
    """
    def rank(pair: tuple[str, int]) -> tuple:
        name, n = pair
        s = name.strip()
        spaced = " " in s
        mixed = any(c.isupper() for c in s) and any(c.islower() for c in s)
        # Negate the name for the final tiebreak so `max` prefers the
        # alphabetically-first variant deterministically.
        return (spaced, mixed, n, len(s), [-ord(c) for c in s.lower()])

    return max(counts.items(), key=rank)[0]


def _apply_brand_canonicalisation(items: list[dict]) -> None:
    """Rewrite each item's ``shop`` to the canonical brand name, in place.

    Items whose names collapse to the same :func:`brand_key` all adopt one
    chosen display name, so the shop facet / grouping / search treat the
    variants as a single brand.
    """
    names_by_key: dict[str, Counter[str]] = {}
    for it in items:
        names_by_key.setdefault(brand_key(it["shop"]), Counter())[it["shop"]] += 1
    canon = {key: _canonical_brand_name(c) for key, c in names_by_key.items()}
    for it in items:
        key = brand_key(it["shop"])
        it["shop"] = canon[key]
        # The merge key travels on the item too, so the frontend can match a
        # detected review-request email (tagged with the same key) to its shop.
        it["brand_key"] = key


# ---------------------------------------------------------------------------
# Payload building (continued)
# ---------------------------------------------------------------------------

def build_payload(wardrobe: dict | None, image_dir: Path | None = None) -> dict:
    """Shape ``wardrobe.json`` into the frontend payload.

    Returns ``{items, categories, shops, stats}`` where ``items`` is the
    clothing-only catalogue (newest purchase first), ``categories`` and
    ``shops`` are present-value facets with counts, and ``stats`` carries the
    headline totals (including how many non-clothing items were hidden).

    The ``shops`` facet is **brand-canonicalised** — display-name and
    domain-slug variants of one brand (e.g. "SORA Clothing" / "Soraclothing")
    are merged into a single entry (see :func:`_apply_brand_canonicalisation`).
    """
    raw = (wardrobe or {}).get("items") or []
    items: list[dict] = []
    hidden = 0
    for it in raw:
        category, is_clothing = classify_item(it)
        if not is_clothing:
            hidden += 1
            continue
        items.append(_normalise_item(it, category, image_dir))

    # Merge brand-name variants before any facet/grouping uses item["shop"].
    _apply_brand_canonicalisation(items)

    # Newest first; blank dates sort last.
    items.sort(key=lambda i: (i["purchased_at"] or "0000"), reverse=True)

    cat_counts = Counter(i["category"] for i in items)
    categories = [
        {"key": key, "label": CATEGORY_LABELS[key], "count": cat_counts[key]}
        for key in CATEGORY_ORDER
        if cat_counts.get(key)
    ]

    shop_counts = Counter(i["shop"] for i in items)
    shops = [
        {"name": name, "count": count}
        for name, count in sorted(shop_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    ]

    dates = [i["purchased_at"] for i in items if i["purchased_at"]]
    total_spent: dict[str, float] = {}
    spent_by_category: dict[str, dict[str, float]] = {}
    spent_by_month: dict[str, dict[str, float]] = {}
    for i in items:
        p = i["price"]
        if not (isinstance(p, dict) and p.get("amount") is not None):
            continue
        cur = (p.get("currency") or "USD").upper()
        try:
            amt = float(p["amount"]) * (i["qty"] or 1)
        except (TypeError, ValueError):
            continue
        total_spent[cur] = total_spent.get(cur, 0.0) + amt
        _accumulate(spent_by_category, i["category"], cur, amt)
        _accumulate(spent_by_month, i["month"], cur, amt)

    stats = {
        "total": len(items),
        "shop_count": len(shops),
        "category_count": len(categories),
        "hidden_non_clothing": hidden,
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
        "total_spent": _round_money(total_spent),
        # Authoritative, currency-aware spend rollups over the clothing
        # catalogue — backs the "Spend" view (the frontend recomputes these
        # client-side only when a search/facet filter is active).
        "spent_by_category": {k: _round_money(v) for k, v in spent_by_category.items()},
        "spent_by_month": {k: _round_money(v) for k, v in spent_by_month.items()},
        "generated_on": date.today().isoformat(),
    }
    return {"items": items, "categories": categories, "shops": shops, "stats": stats}


def apply_category_edit(wardrobe: dict, item_id: str, category: str) -> dict | None:
    """Set a manual ``category`` on one wardrobe item, in place.

    Validates ``category`` against the shared taxonomy; raises ``ValueError`` on
    an unknown key. Returns the edited raw item dict, or ``None`` when the id
    isn't found. Keeps ``is_clothing`` in sync with the new category:
    ``non_clothing`` sets ``is_clothing == False`` (hides it + skips the
    body-comp / fit nudges); any garment category clears a prior
    ``is_clothing == False`` so a re-categorised item reappears.
    """
    key = normalise_category(category)
    if key is None:
        raise ValueError(f"unknown category: {category!r}")
    for it in (wardrobe.get("items") or []):
        if it.get("id") == item_id:
            it["category"] = key
            if key == NON_CLOTHING:
                it["is_clothing"] = False
            elif it.get("is_clothing") is False:
                it.pop("is_clothing", None)
            return it
    return None


# ---------------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------------

def fetch_wardrobe(gist_id: str, token: str) -> dict:
    """Read ``wardrobe.json`` from the Gist (read-only).

    Reuses ``state.read_state`` so the >1 MB truncation handling (follow
    ``raw_url`` with the bearer token) is shared — a wardrobe carrying full
    body-comp blocks crosses 1 MB and would otherwise read back as ``{}``.
    """
    return state.read_state(gist_id, token).get("wardrobe") or {}


def _review_request_days(raw: str | None, default: int = 30) -> int:
    """Recent-window length (days) for review-request detection.

    Mirrors the daily cron's ``REVIEW_REQUESTS_DAYS`` handling: blank /
    non-numeric / non-positive falls back to the default.
    """
    try:
        days = int((raw or "").strip())
    except (TypeError, ValueError):
        return default
    return days if days > 0 else default


def _credentials(env: dict | None = None) -> tuple[str, str]:
    src = env if env is not None else os.environ
    token = (src.get("GITHUB_TOKEN") or "").strip()
    gist_id = (src.get("GIST_ID") or "").strip()
    missing = [k for k, v in (("GITHUB_TOKEN", token), ("GIST_ID", gist_id)) if not v]
    if missing:
        raise SystemExit(
            "wardrobe_browser: missing required env vars: " + ", ".join(missing)
            + "\nSet them in sale-check/.env (the same GITHUB_TOKEN/GIST_ID the"
            " daily run uses)."
        )
    return gist_id, token


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

# Category menu handed to the frontend's edit dropdown — every taxonomy key
# with its label, in display order (includes non_clothing so a wrongly-shown
# item can be hidden from the panel).
CATEGORY_CHOICES: list[dict] = [
    {"key": key, "label": CATEGORY_LABELS[key]} for key in CATEGORY_ORDER
]

# How long a fetched review-request batch is reused before re-hitting Gmail.
# Opening item after item shouldn't re-run the IMAP search each time; `Refresh`
# invalidates the cache so a freshly-arrived request still shows on demand.
_REVIEW_REQUESTS_TTL_SECONDS = 600


class _Catalogue:
    """Thread-safe holder for the current payload + the read/modify/write paths.

    ``refresh`` re-fetches read-only; ``edit_category`` writes a manual category
    straight to the Gist; ``submit_fit`` forwards a fit review to the Apps Script
    web app (so the Gist write + audit-Sheet log + body-comp match all run
    through the single existing implementation). ``review_requests`` reads
    (read-only) the recent post-purchase review-request emails from Gmail so the
    detail panel can link straight to one. The write paths end by rebuilding and
    storing the served payload.
    """

    def __init__(
        self,
        gist_id: str,
        token: str,
        image_dir: Path | None,
        *,
        fit_form_base_url: str = "",
        fit_link_secret: str = "",
        gmail_username: str = "",
        gmail_app_password: str = "",
        review_request_days: int = 30,
    ):
        self._gist_id = gist_id
        self._token = token
        self._image_dir = image_dir
        self._fit_base = fit_form_base_url
        self._fit_secret = fit_link_secret
        self._gmail_user = gmail_username
        self._gmail_pw = gmail_app_password
        self._rr_days = review_request_days
        self._lock = threading.Lock()
        self._payload: dict = {}
        # Review-request cache (own lock so a slow IMAP fetch never blocks the
        # payload lock): (result_dict, fetched_at_monotonic).
        self._rr_lock = threading.Lock()
        self._rr_cache: dict | None = None
        self._rr_at = 0.0

    def _store(self, payload: dict) -> dict:
        with self._lock:
            self._payload = payload
        return payload

    def refresh(self) -> dict:
        wardrobe = fetch_wardrobe(self._gist_id, self._token)
        # Drop the cached review requests so a Refresh re-checks Gmail (the
        # frontend re-requests them right after a refresh).
        with self._rr_lock:
            self._rr_cache = None
        return self._store(build_payload(wardrobe, self._image_dir))

    def edit_category(self, item_id: str, category: str) -> dict:
        """Persist a manual category to the Gist (read-modify-write), re-render.

        Re-reads the whole state so a concurrent cron/scan write isn't clobbered
        by a stale copy, edits in place, then writes wardrobe back (other Gist
        files pass through untouched). Raises ``KeyError`` if the id is unknown,
        ``ValueError`` for a bad category.
        """
        if not (item_id or "").strip():
            raise ValueError("missing item id")
        st = state.read_state(self._gist_id, self._token)
        wardrobe = st.get("wardrobe") or {}
        item = apply_category_edit(wardrobe, item_id, category)
        if item is None:
            raise KeyError(item_id)
        state.write_state(
            self._gist_id, self._token,
            prices=st.get("prices") or {},
            aliases=st.get("aliases") or {},
            codes=st.get("codes") or [],
            wardrobe=wardrobe,
        )
        return self._store(build_payload(wardrobe, self._image_dir))

    @property
    def fit_enabled(self) -> bool:
        return bool(self._fit_base and self._fit_secret)

    def submit_fit(self, body: dict) -> dict:
        """Forward a fit review to the Apps Script web app, then re-render.

        The Apps Script (`doPost` → `submitFitReview`) owns the durable write:
        wardrobe.json on the Gist, the audit Google Sheet, and the body-comp
        match to the nearest DEXA scan. We sign the item id with
        ``FIT_LINK_SECRET`` exactly like the emailed links so the same verifier
        accepts it. Raises ``RuntimeError`` when unconfigured or rejected.
        """
        if not self.fit_enabled:
            raise RuntimeError(
                "fit feedback is not configured — set FIT_FORM_BASE_URL and "
                "FIT_LINK_SECRET in sale-check/.env (and deploy the Apps Script "
                "doPost). See the wardrobe-browser handoff."
            )
        item_id = (body.get("id") or "").strip()
        if not item_id:
            raise ValueError("missing item id")
        out = {k: v for k, v in body.items() if k != "id"}
        out["action"] = "fit"
        out["item"] = item_id
        out["sig"] = fit_links.sign(item_id, self._fit_secret)

        import httpx
        resp = httpx.post(self._fit_base, json=out, follow_redirects=True, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or "Apps Script rejected the fit review")
        # Re-read so the freshly-written review is reflected in the served payload.
        wardrobe = fetch_wardrobe(self._gist_id, self._token)
        return self._store(build_payload(wardrobe, self._image_dir))

    @property
    def review_requests_enabled(self) -> bool:
        return bool(self._gmail_user and self._gmail_pw)

    def review_requests(self, *, force: bool = False) -> dict:
        """Recent post-purchase *review-request* emails, grouped for the panel.

        Reuses the daily digest's logic exactly — ``gmail.fetch_review_requests``
        for the recent window, ``review_requests.dedupe`` for one-entry-per-order
        precision — then tags each entry with a normalised ``brand_key`` (the
        same merge key carried on each item) so the frontend can match a request
        to the item's shop and offer a direct "leave a review" link.

        Returns ``{"enabled", "requests", "all_url", "error"?}``. Cached for
        ``_REVIEW_REQUESTS_TTL_SECONDS`` so browsing item-to-item doesn't re-run
        the IMAP search; ``Refresh`` clears the cache. **Failure-isolated** —
        Gmail unconfigured or unreachable yields an empty list, never raising
        (the panel falls back to its Gmail-search link).
        """
        base = {
            "enabled": self.review_requests_enabled,
            "all_url": review_requests.all_requests_url(),
        }
        if not self.review_requests_enabled:
            return {**base, "requests": []}

        now = time.monotonic()
        with self._rr_lock:
            cached = self._rr_cache
            fresh = cached is not None and (now - self._rr_at) < _REVIEW_REQUESTS_TTL_SECONDS
            if cached is not None and fresh and not force:
                return cached

        # Fetch outside the lock — IMAP is slow and must not block other readers.
        result = dict(base)
        try:
            from src import gmail
            emails = gmail.fetch_review_requests(
                self._gmail_user, self._gmail_pw, days=self._rr_days
            )
            requests = review_requests.dedupe(emails)
            for r in requests:
                r["brand_key"] = brand_key(r.get("shop"))
            result["requests"] = requests
        except Exception as exc:  # noqa: BLE001 — Gmail must never break the browser
            log.warning("wardrobe_browser: review-request fetch failed: %s", exc)
            result["requests"] = []
            result["error"] = str(exc)

        with self._rr_lock:
            self._rr_cache = result
            self._rr_at = time.monotonic()
        return result

    @property
    def payload(self) -> dict:
        with self._lock:
            return self._payload

    @property
    def image_dir(self) -> Path | None:
        return self._image_dir


def _make_handler(catalogue: _Catalogue):
    ui_html = _UI_FILE.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        # Quieter logging — one line per request at debug.
        def log_message(self, fmt, *args):  # noqa: N802
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, content_type: str):
            try:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                # The browser closed the socket before we finished writing —
                # common when a slow response (the IMAP review-request fetch)
                # is still in flight at page reload/navigation. Harmless; don't
                # let it dump a scary traceback in the user's terminal.
                log.debug("wardrobe_browser: client disconnected before response finished")

        def _send_json(self, payload: dict, code: int = 200):
            self._send(code, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

        def _read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            return json.loads(raw or b"{}")

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, ui_html, "text/html; charset=utf-8")
            elif path == "/api/wardrobe":
                self._send_json(catalogue.payload)
            elif path == "/api/meta":
                self._send_json({
                    "category_choices": CATEGORY_CHOICES,
                    "fit_enabled": catalogue.fit_enabled,
                    "review_requests_enabled": catalogue.review_requests_enabled,
                })
            elif path == "/api/review-requests":
                # Read-only Gmail check; self-isolates failures into the payload.
                self._send_json(catalogue.review_requests())
            elif path.startswith("/images/"):
                self._serve_image(path)
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/api/refresh":
                try:
                    payload = catalogue.refresh()
                except Exception as exc:  # noqa: BLE001 — surface fetch errors to the UI
                    log.warning("wardrobe_browser: refresh failed: %s", exc)
                    self._send_json({"error": str(exc)}, code=502)
                    return
                self._send_json(payload)
            elif path == "/api/item/category":
                self._handle_write(lambda b: catalogue.edit_category(
                    (b.get("id") or ""), b.get("category")))
            elif path == "/api/item/fit":
                self._handle_write(lambda b: catalogue.submit_fit(b))
            elif path == "/api/shutdown":
                # Stop the local server cleanly so the user doesn't Ctrl-C the
                # PowerShell window. ThreadingHTTPServer.shutdown() blocks until
                # serve_forever() returns and MUST run off the serving thread —
                # this handler runs in a worker thread and serve_forever() is on
                # the main thread, so calling it inline would deadlock. Spawn a
                # daemon thread; run()'s serve_forever() then returns, prints the
                # stop line, and server_close() runs in its finally. Local-only
                # (bound to 127.0.0.1), so no auth is needed.
                self._send_json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        def _handle_write(self, action):
            """Run a payload-returning write action; map errors to status codes."""
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json({"error": "invalid JSON body"}, code=400)
                return
            try:
                payload = action(body)
            except KeyError:
                self._send_json({"error": "item not found"}, code=404)
                return
            except ValueError as exc:
                self._send_json({"error": str(exc)}, code=400)
                return
            except RuntimeError as exc:  # not configured / Apps Script rejected
                self._send_json({"error": str(exc)}, code=503)
                return
            except Exception as exc:  # noqa: BLE001 — surface to the UI
                log.warning("wardrobe_browser: write failed: %s", exc)
                self._send_json({"error": str(exc)}, code=502)
                return
            self._send_json(payload)

        def _serve_image(self, path: str):
            image_dir = catalogue.image_dir
            name = Path(path[len("/images/"):]).name  # strip any traversal
            if not image_dir or not name:
                self._send(404, b"no image", "text/plain; charset=utf-8")
                return
            f = image_dir / name
            if not f.is_file():
                self._send(404, b"no image", "text/plain; charset=utf-8")
                return
            ctype = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".gif": "image/gif",
            }.get(f.suffix.lower(), "application/octet-stream")
            self._send(200, f.read_bytes(), ctype)

    return Handler


def run(
    *,
    port: int = 8787,
    host: str = "127.0.0.1",
    open_browser: bool = True,
    image_dir: Path | None = None,
    env: dict | None = None,
) -> None:
    gist_id, token = _credentials(env)
    src = env if env is not None else os.environ
    fit_base = (src.get("FIT_FORM_BASE_URL") or "").strip()
    fit_secret = (src.get("FIT_LINK_SECRET") or "").strip()
    gmail_user = (src.get("GMAIL_USERNAME") or "").strip()
    gmail_pw = (src.get("GMAIL_APP_PASSWORD") or "").strip()
    rr_days = _review_request_days(src.get("REVIEW_REQUESTS_DAYS"))
    catalogue = _Catalogue(
        gist_id, token, image_dir,
        fit_form_base_url=fit_base, fit_link_secret=fit_secret,
        gmail_username=gmail_user, gmail_app_password=gmail_pw,
        review_request_days=rr_days,
    )
    log.info("wardrobe_browser: fetching wardrobe from Gist ...")
    payload = catalogue.refresh()
    stats = payload["stats"]
    print(
        f"Wardrobe loaded: {stats['total']} clothing items across "
        f"{stats['shop_count']} brands "
        f"({stats['hidden_non_clothing']} non-clothing hidden)."
    )
    print(
        "Fit feedback: "
        + ("enabled (writes via your Apps Script web app)" if catalogue.fit_enabled
           else "disabled (set FIT_FORM_BASE_URL + FIT_LINK_SECRET in .env to enable)")
    )
    print(
        "Review-request detection: "
        + ("enabled (reads recent review-request emails from Gmail)"
           if catalogue.review_requests_enabled
           else "disabled (set GMAIL_USERNAME + GMAIL_APP_PASSWORD in .env to enable)")
    )

    server = ThreadingHTTPServer((host, port), _make_handler(catalogue))
    url = f"http://{host}:{port}/"
    print(f"Wardrobe browser running at {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping wardrobe browser.")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        prog="python -m src.wardrobe_browser",
        description="Browse your wardrobe.json locally in a web app.",
    )
    parser.add_argument("--port", type=int, default=8787, help="port (default 8787)")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true", help="don't auto-open the browser")
    parser.add_argument(
        "--image-dir", type=Path, default=None,
        help="folder of cached product images named <item_id>.<ext> (iteration 2)",
    )
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    run(
        port=args.port,
        host=args.host,
        open_browser=not args.no_browser,
        image_dir=args.image_dir,
    )


if __name__ == "__main__":
    main()
