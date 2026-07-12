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

Reads are GETs against the Gist; ``Refresh`` re-fetches the latest catalogue.
Interactive writes go through the local server: category edits and manual
image adds write the Gist read-modify-write style, fit reviews forward to the
Apps Script web app, and image-file uploads touch only the local cache.

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

Product images (issue #19): ``order_scan`` stamps ``image_url`` on items from
their order-confirmation emails (scan-time + the ``--reharvest-images``
backfill); ``--fetch-images`` downloads those into a local ``--image-dir``
cache (default ``./images``, gitignored) served at ``/images/<id>.<ext>``.
Items without a cached photo fall back to a per-category icon rendered
client-side. The manual paste-back rung (issue #30) covers what no automation
reaches: the detail panel's "Add photo" inputs accept a product-page URL
(og:image + product_url double-stamp), a direct image URL, or a file upload
(cache-only, no Gist write), and a "No photo" work-queue filter surfaces the
remaining icon-only items.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import threading
import time
import webbrowser
from collections import Counter
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

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


_IMAGE_FILE_EXTS = ("jpg", "jpeg", "png", "webp", "gif")


def _image_url(item_id: str, image_dir: Path | None) -> str | None:
    """Local cached product image URL for an item, if one exists on disk.

    The ``--fetch-images`` step writes ``<image_dir>/<id>.<ext>``; this
    surfaces it as ``/images/<file>`` so the frontend can show the real
    product photo instead of a category icon. Empty/absent dir -> always None.

    The URL carries a ``?v=<mtime_ns>`` cache-buster. Replacing a photo in
    place keeps the same id + extension, so the path is byte-identical; the
    browser then reuses the already-decoded same-URL image from its in-page
    memory cache (which ``Cache-Control: no-store`` does not govern) and shows
    the old photo until a full reload. Keying the URL on the file's mtime makes
    a rewrite yield a new URL — while identical bytes keep a stable mtime, so
    nothing re-downloads needlessly. The server strips the query before serving
    (see ``_serve_image``).
    """
    if not image_dir:
        return None
    for ext in _IMAGE_FILE_EXTS:
        f = image_dir / f"{item_id}.{ext}"
        if f.is_file():
            return f"/images/{f.name}?v={f.stat().st_mtime_ns}"
    return None


# ---------------------------------------------------------------------------
# Product-image fetch step (--fetch-images, issue #19)
# ---------------------------------------------------------------------------
# ``order_scan`` (scan-time + --reharvest-images) stamps ``image_url`` on items
# from their order emails. The URL string rides wardrobe.json (Gist, portable);
# the BYTES are cached here, locally — email-CDN URLs rot (image.email.*,
# iterable, sendinblue), and even cdn.shopify.com dies with the merchant.
# Bytes never go in the Gist (base64 bloat + the >1 MB truncation trap).

# Content types we'll cache, mapped to the extension _image_url probes.
_IMAGE_CONTENT_TYPES = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/pjpeg": "jpg",
    "image/png": "png", "image/webp": "webp", "image/gif": "gif",
}
# Realistic browser UA — some image CDNs refuse the default python-httpx one.
_IMAGE_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
}


def _ext_from_image_url(url: str) -> str | None:
    path = urlparse(url or "").path.lower()
    for ext in _IMAGE_FILE_EXTS:
        if path.endswith("." + ext):
            return "jpg" if ext == "jpeg" else ext
    return None


def _save_image_bytes(item_id: str, ext: str, content: bytes, image_dir: Path) -> str:
    """Write ``<image_dir>/<item_id>.<ext>``, returning the filename.

    One file per id: other-extension leftovers are dropped first so a format
    change (or a manual image replacing an old cached one) can't leave a stale
    .jpg shadowing the new .png in ``_image_url``'s probe order."""
    image_dir.mkdir(parents=True, exist_ok=True)
    for old in _IMAGE_FILE_EXTS:
        stale = image_dir / f"{item_id}.{old}"
        if old != ext and stale.is_file():
            stale.unlink()
    f = image_dir / f"{item_id}.{ext}"
    f.write_bytes(content)
    return f.name


def _download_image(client, url: str, item_id: str, image_dir: Path) -> str | None:
    """GET ``url`` and cache it as ``<item_id>.<ext>`` when it's a real image.

    The shared validation rung for ``fetch_images`` and the manual add-image
    paths: HTTP 200, an image/* (or absent) content-type, and a known extension
    mapped from the content-type or the URL. A 200 that isn't an image (CDN
    error page) is never written. Returns the cached filename, or ``None`` when
    the response isn't usable; network errors propagate to the caller."""
    resp = client.get(url)
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    ext = _IMAGE_CONTENT_TYPES.get(ctype) or _ext_from_image_url(url)
    ok = (
        resp.status_code == 200 and resp.content and ext
        and (not ctype or ctype.startswith("image/"))
    )
    if not ok:
        log.info("fetch-images: %s -> %s (%s) — skipped",
                 item_id, resp.status_code, ctype or "no content-type")
        return None
    return _save_image_bytes(item_id, ext, resp.content, image_dir)


# Amazon order emails embed 90px thumbnails (``..._SS90_.jpg``). The size token
# is a render instruction on the same image id, so rewriting it requests the
# identical photo at a usable size. Live-checked 2026-07-10.
_AMAZON_SIZE_TOKEN_RE = re.compile(r"\._[A-Z]{2}\d+_\.")


def _upgraded_image_url(url: str) -> str | None:
    """A better-resolution variant of ``url`` worth trying first, or None.

    Only Amazon's size-token rewrite for now. The caller falls back to the
    original URL if the upgraded one doesn't fetch."""
    host = urlparse(url or "").netloc.lower()
    if host.endswith("media-amazon.com") or host.endswith("images-amazon.com"):
        upgraded = _AMAZON_SIZE_TOKEN_RE.sub("._SL600_.", url)
        if upgraded != url:
            return upgraded
    return None


def fetch_images(
    items: list[dict],
    image_dir: Path,
    *,
    refresh: bool = False,
    client=None,
    sleep=time.sleep,
) -> dict:
    """Download each item's ``image_url`` into ``image_dir/<id>.<ext>``.

    Incremental: an id that already has a cached file is skipped unless
    ``refresh``. Failure-isolated per item — a rotted URL / CDN error just
    leaves that item on its category-icon fallback and is re-tried next run
    (the cache file is the state). A 200 whose content-type isn't an image
    (CDN error page) is never written. Between real downloads a short jitter
    keeps the CDN hits polite. Returns counts: targets / downloaded / cached /
    failed."""
    stats = {"targets": 0, "downloaded": 0, "cached": 0, "failed": 0}
    targets = [
        it for it in items or []
        if (it.get("image_url") or "").strip() and (it.get("id") or "").strip()
    ]
    if not targets:
        return stats
    image_dir.mkdir(parents=True, exist_ok=True)

    import httpx

    own = client is None
    client = client or httpx.Client(
        follow_redirects=True, timeout=30.0, headers=_IMAGE_FETCH_HEADERS)
    try:
        for it in targets:
            stats["targets"] += 1
            item_id = it["id"].strip()
            if not refresh and _image_url(item_id, image_dir):
                stats["cached"] += 1
                continue
            url = it["image_url"].strip()
            # Try a better-resolution variant first (Amazon emails embed 90px
            # thumbnails); the stored URL stays the fallback + the record.
            upgraded = _upgraded_image_url(url)
            written = False
            for attempt in filter(None, (upgraded, url)):
                try:
                    if _download_image(client, attempt, item_id, image_dir):
                        stats["downloaded"] += 1
                        written = True
                        break
                except Exception as exc:  # noqa: BLE001 — per-item isolation
                    log.info("fetch-images: %s failed: %s", item_id, exc)
            if not written:
                stats["failed"] += 1
            sleep(random.uniform(0.2, 0.6))
    finally:
        if own:
            client.close()
    return stats


# ---------------------------------------------------------------------------
# Manual add-image paths (issue #30 — image-gap phase B)
# ---------------------------------------------------------------------------
# The paste-back rung for the ~150 items automation can't reach (issues
# #19/#28/#29 ran to their ceilings): the user pastes a product-page URL, a
# direct image URL, or uploads a file, from the detail panel. Page fetches run
# from this machine (residential IP), so shops that bot-wall the Actions IP
# fetch fine here.

# Browser-shaped headers for the product-page fetch (same UA as the image
# fetch; an HTML Accept so strict origins don't 406 an image-only Accept).
_PAGE_FETCH_HEADERS = {
    "User-Agent": _IMAGE_FETCH_HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Uploads: bound the request body so a mispasted video can't balloon the cache.
_UPLOAD_MAX_BYTES = 15 * 1024 * 1024

_META_TAG_RE = re.compile(r"<meta\s[^>]*?/?>", re.IGNORECASE | re.DOTALL)
_META_ATTR_RE = re.compile(
    r"""([a-zA-Z:_-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_JSONLD_RE = re.compile(
    r"<script[^>]+type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


def _meta_attrs(tag: str) -> dict[str, str]:
    # findall yields '' (not None) for the non-participating quote group.
    return {k.lower(): (v1 or v2 or "") for k, v1, v2 in _META_ATTR_RE.findall(tag)}


def _jsonld_image_value(node) -> str | None:
    """First usable ``image`` URL anywhere inside a parsed JSON-LD node."""
    if isinstance(node, dict):
        img = node.get("image")
        if img is not None:
            found = _image_url_from_jsonld_field(img)
            if found:
                return found
        for v in node.values():
            found = _jsonld_image_value(v)
            if found:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _jsonld_image_value(v)
            if found:
                return found
    return None


def _image_url_from_jsonld_field(img) -> str | None:
    """The URL out of a JSON-LD ``image`` field: str, list, or ImageObject."""
    if isinstance(img, str):
        return img.strip() or None
    if isinstance(img, list):
        for entry in img:
            found = _image_url_from_jsonld_field(entry)
            if found:
                return found
        return None
    if isinstance(img, dict):
        for key in ("url", "contentUrl"):
            val = img.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def extract_page_image(html: str) -> str | None:
    """The product photo URL declared by a product page, or ``None``.

    Deliberately dumb (no HTML parser dependency): ``og:image`` meta first —
    the near-universal product-page convention, attribute order tolerated —
    then any JSON-LD block's ``image`` field. Entity-unescaped; the caller
    resolves relative URLs against the page URL."""
    import html as html_mod

    for tag in _META_TAG_RE.findall(html or ""):
        attrs = _meta_attrs(tag)
        if (attrs.get("property") or attrs.get("name")) == "og:image":
            content = html_mod.unescape(attrs.get("content") or "").strip()
            if content:
                return content
    for m in _JSONLD_RE.finditer(html or ""):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        found = _jsonld_image_value(data)
        if found:
            return html_mod.unescape(found)
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


def apply_image_edit(
    wardrobe: dict, item_id: str, image_url: str, *, product_url: str | None = None,
) -> dict | None:
    """Stamp a manually-supplied ``image_url`` on one wardrobe item, in place.

    Mirrors :func:`apply_category_edit`. When ``product_url`` is given and the
    item has none, it's donated too (the paste-a-product-page path recovers
    both, same double payoff as the storefront search — issue #29). An existing
    ``product_url`` is never overwritten. Returns the edited raw item dict, or
    ``None`` when the id isn't found."""
    for it in (wardrobe.get("items") or []):
        if it.get("id") == item_id:
            it["image_url"] = image_url
            if product_url and not (it.get("product_url") or "").strip():
                it["product_url"] = product_url
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

    ``fresh=True``: this is a long-lived process, so a Refresh after an external
    writer (an ``order_scan`` in another terminal) must not be served a stale
    revision from GitHub's edge cache (issue #20).
    """
    return state.read_state(gist_id, token, fresh=True).get("wardrobe") or {}


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
        # Last raw wardrobe seen (any read or write path) — lets a cache-only
        # write (image upload) rebuild the served payload without a Gist
        # round-trip. The payload lock guards it alongside the payload.
        self._wardrobe: dict | None = None
        # Review-request cache (own lock so a slow IMAP fetch never blocks the
        # payload lock): (result_dict, fetched_at_monotonic).
        self._rr_lock = threading.Lock()
        self._rr_cache: dict | None = None
        self._rr_at = 0.0

    def _store(self, payload: dict, wardrobe: dict | None = None) -> dict:
        with self._lock:
            self._payload = payload
            if wardrobe is not None:
                self._wardrobe = wardrobe
        return payload

    def refresh(self) -> dict:
        wardrobe = fetch_wardrobe(self._gist_id, self._token)
        # Drop the cached review requests so a Refresh re-checks Gmail (the
        # frontend re-requests them right after a refresh).
        with self._rr_lock:
            self._rr_cache = None
        return self._store(build_payload(wardrobe, self._image_dir), wardrobe)

    def edit_category(self, item_id: str, category: str) -> dict:
        """Persist a manual category to the Gist (read-modify-write), re-render.

        Re-reads the whole state so a concurrent cron/scan write isn't clobbered
        by a stale copy, edits in place, then writes wardrobe back (other Gist
        files pass through untouched). Raises ``KeyError`` if the id is unknown,
        ``ValueError`` for a bad category.
        """
        if not (item_id or "").strip():
            raise ValueError("missing item id")
        # fresh=True: read-modify-write must start from the current revision, not
        # a stale edge-cached one, or a concurrent cron write could be clobbered
        # (issue #20).
        st = state.read_state(self._gist_id, self._token, fresh=True)
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
        return self._store(build_payload(wardrobe, self._image_dir), wardrobe)

    def add_image(self, body: dict) -> dict:
        """Manually attach a product photo from a pasted URL (issue #30).

        Two paste paths, exclusive (``image_url`` wins when both arrive):

        * ``page_url`` — fetch the product page from this machine (residential
          IP, so shops that bot-wall the Actions IP work), extract its
          ``og:image`` / JSON-LD image, download that into the local cache, and
          stamp ``image_url`` = the extracted URL plus ``product_url`` = the
          cleaned page URL when the item lacks one.
        * ``image_url`` — download + stamp directly. The Amazon workhorse
          (right-click the order-history photo → copy image address); Amazon
          product *pages* are bot-walled even locally, their image CDN isn't.

        The download is the validation (same rung as ``--fetch-images``): a
        URL that doesn't yield real image bytes stamps nothing. Raises
        ``ValueError`` for bad input / an unusable URL, ``KeyError`` for an
        unknown id — mapped to clean JSON errors by the route handler."""
        if not self._image_dir:
            raise RuntimeError("no image directory configured (--image-dir)")
        item_id = (body.get("id") or "").strip()
        if not item_id:
            raise ValueError("missing item id")
        page_url = (body.get("page_url") or "").strip()
        image_url = (body.get("image_url") or "").strip()
        chosen = image_url or page_url
        if not chosen:
            raise ValueError("paste a product-page URL or a direct image URL")
        if not chosen.startswith(("http://", "https://")):
            raise ValueError("that doesn't look like an http(s) URL")

        import httpx

        from src.order_scan import _clean_product_url, _heic_safe

        product_url: str | None = None
        with httpx.Client(
            follow_redirects=True, timeout=30.0, headers=_IMAGE_FETCH_HEADERS,
        ) as client:
            if not image_url:
                try:
                    resp = client.get(page_url, headers=_PAGE_FETCH_HEADERS)
                except Exception as exc:
                    raise ValueError(f"couldn't fetch that page: {exc}") from exc
                if resp.status_code != 200:
                    raise ValueError(
                        f"page fetch failed (HTTP {resp.status_code})")
                found = extract_page_image(resp.text)
                if not found:
                    raise ValueError(
                        "no product image found on that page — try pasting the "
                        "image URL directly (right-click the photo → copy image "
                        "address)")
                # Relative og:image is rare but cheap to resolve; the final
                # (post-redirect) page URL is the right base.
                image_url = urljoin(str(resp.url), found)
                product_url = _clean_product_url(page_url)
            image_url = _heic_safe(image_url)
            try:
                saved = _download_image(client, image_url, item_id, self._image_dir)
            except Exception as exc:
                raise ValueError(f"couldn't download that image: {exc}") from exc
        if not saved:
            raise ValueError(
                "that URL didn't return an image (jpg/png/webp/gif) — check it "
                "opens as a bare photo in a browser tab")

        # Stamp the Gist (read-modify-write, same pattern as edit_category).
        st = state.read_state(self._gist_id, self._token, fresh=True)
        wardrobe = st.get("wardrobe") or {}
        item = apply_image_edit(
            wardrobe, item_id, image_url, product_url=product_url)
        if item is None:
            # Unknown id: drop the just-written cache file so a bogus request
            # can't leave an orphan that _image_url would happily serve.
            stale = self._image_dir / saved
            if stale.is_file():
                stale.unlink()
            raise KeyError(item_id)
        state.write_state(
            self._gist_id, self._token,
            prices=st.get("prices") or {},
            aliases=st.get("aliases") or {},
            codes=st.get("codes") or [],
            wardrobe=wardrobe,
        )
        return self._store(build_payload(wardrobe, self._image_dir), wardrobe)

    def upload_image(
        self, item_id: str, filename: str, content_type: str, content: bytes,
    ) -> dict:
        """Save an uploaded image file into the local cache (issue #30).

        **Cache-only — zero Gist writes.** ``build_payload`` surfaces a cached
        file by item id regardless of the ``image_url`` stamp, and cache
        presence is exactly what stops ``order_scan --search-images`` from
        re-targeting the item. For dead-shop items whose photo only survives as
        a screenshot/Google/Reddit find."""
        if not self._image_dir:
            raise RuntimeError("no image directory configured (--image-dir)")
        item_id = (item_id or "").strip()
        if not item_id:
            raise ValueError("missing item id")
        if not content:
            raise ValueError("empty file")
        ctype = (content_type or "").split(";")[0].strip().lower()
        ext = _IMAGE_CONTENT_TYPES.get(ctype) or _ext_from_image_url(filename or "")
        if not ext:
            raise ValueError("unsupported image type — use jpg/png/webp/gif")
        with self._lock:
            wardrobe = self._wardrobe
        known = {
            (it.get("id") or "").strip()
            for it in ((wardrobe or {}).get("items") or [])
        }
        if item_id not in known:
            raise KeyError(item_id)
        _save_image_bytes(item_id, ext, content, self._image_dir)
        return self._store(build_payload(wardrobe, self._image_dir), wardrobe)

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
        return self._store(build_payload(wardrobe, self._image_dir), wardrobe)

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
            elif path == "/api/item/image":
                self._handle_write(catalogue.add_image)
            elif path == "/api/item/image-file":
                self._handle_upload()
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
            """Run a payload-returning write action on the JSON body."""
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError):
                self._send_json({"error": "invalid JSON body"}, code=400)
                return
            self._respond_write(lambda: action(body))

        def _handle_upload(self):
            """POST /api/item/image-file?id=..&name=.. with the raw file bytes.

            Raw-body upload (no multipart parsing): the frontend POSTs the File
            object directly, so the browser sets Content-Type from the file and
            the id/filename ride the query string."""
            q = parse_qs(urlparse(self.path).query)
            item_id = (q.get("id") or [""])[0]
            name = (q.get("name") or [""])[0]
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                self._send_json({"error": "empty upload"}, code=400)
                return
            if length > _UPLOAD_MAX_BYTES:
                self._send_json(
                    {"error": "file too large (15 MB max)"}, code=400)
                return
            content = self.rfile.read(length)
            ctype = self.headers.get("Content-Type") or ""
            self._respond_write(
                lambda: catalogue.upload_image(item_id, name, ctype, content))

        def _respond_write(self, fn):
            """Run a payload-returning write; map errors to clean JSON codes."""
            try:
                payload = fn()
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


def _fetch_images_in_background(
    catalogue: "_Catalogue", gist_id: str, token: str, image_dir: Path,
    *, refresh: bool = False,
) -> threading.Thread:
    """Run the image fetch on a daemon thread so serving starts immediately.

    The retry of permanently-rotted URLs (~fast 404s / dead DNS) costs ~15s a
    pass — hidden here instead of delaying the UI. When anything new lands,
    the served payload is rebuilt so the next page load / Refresh shows it."""
    def _work():
        try:
            wardrobe = fetch_wardrobe(gist_id, token)
            stats = fetch_images(
                wardrobe.get("items") or [], image_dir, refresh=refresh)
            print(
                f"Product images: {stats['downloaded']} downloaded, "
                f"{stats['cached']} already cached, {stats['failed']} failed "
                f"({stats['targets']} item(s) carry an image_url)."
            )
            if stats["downloaded"]:
                catalogue.refresh()
        except Exception as exc:  # noqa: BLE001 — never take the server down
            log.warning("wardrobe_browser: image fetch failed: %s", exc)

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    return t


def run(
    *,
    port: int = 8787,
    host: str = "127.0.0.1",
    open_browser: bool = True,
    image_dir: Path | None = None,
    fetch_images_first: bool = False,
    refresh_images: bool = False,
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

    if fetch_images_first and image_dir:
        print("Fetching product images in the background ...")
        _fetch_images_in_background(
            catalogue, gist_id, token, image_dir, refresh=refresh_images)

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
        "--image-dir", type=Path, default=Path("images"),
        help="folder of cached product images named <item_id>.<ext> "
             "(default: ./images, gitignored)",
    )
    parser.add_argument(
        "--fetch-images", action="store_true",
        help="download + cache the product photo of every item carrying an "
             "image_url (stamped by order_scan / --reharvest-images, issue #19) "
             "into --image-dir, in the background while the app serves; ids "
             "already cached are skipped",
    )
    parser.add_argument(
        "--refresh-images", action="store_true",
        help="with --fetch-images: re-download images that are already cached",
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
        fetch_images_first=args.fetch_images,
        refresh_images=args.refresh_images,
    )


if __name__ == "__main__":
    main()
