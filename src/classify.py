"""Parse raw watchlist text into categorized chunks."""
from __future__ import annotations

import re
from typing import Literal, NamedTuple

Category = Literal[
    "PRODUCT_URL", "SHOP_URL", "UNTRACKED_URL", "SHOP_NAME", "LOOSE_MENTION", "IGNORE"
]


class Entry(NamedTuple):
    category: Category
    value: str
    context: str  # shop name for URLs/loose mentions; raw line for SHOP_NAME
    is_clothing: bool = True
    priority: bool = False  # line carried an inline priority marker (⭐ / [priority])


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r'https?://\S+')

# Inline "watch this closely" marker. The user stars a product-URL line in the
# watchlist Doc and that URL gets pinned to a "Watching now" block at the top of
# the daily digest (full price / sale / stock status, shown every day). Read from
# the plain-text Doc export, which strips all formatting — so the marker must be
# literal text. ⭐ (U+2B50, with or without the U+FE0F emoji-presentation selector)
# is the recommended form; the textual ``[priority]`` tag is accepted as a
# typo-proof fallback. Matched anywhere on the URL's line.
_PRIORITY_MARKER_RE = re.compile(r'⭐|\[priority\]', re.IGNORECASE)

# Domains whose URLs are always ignored
_IGNORE_DOMAIN_RE = re.compile(r'reddit\.com|redbubble\.com', re.I)

# Amazon product pages can't be price-checked: a plain httpx GET (what
# extract.py does) hits a CAPTCHA/robot wall, and from GitHub Actions' data
# centre IPs it's blocked even harder. So Amazon product URLs are classified
# as UNTRACKED_URL — surfaced (titled + clickable) in their own digest block,
# but never crawled and never pushed through the homepage sale check. A bare
# amazon.com homepage / search URL (no product path) is left as SHOP_URL.
_AMAZON_URL_RE = re.compile(r'^https?://(?:[\w-]+\.)*amazon\.[a-z.]{2,6}/', re.I)
_AMAZON_PRODUCT_PATH_RE = re.compile(
    r'/(?:dp|gp/product|gp/aw/d|product)/[A-Z0-9]{10}\b', re.I
)

# Path patterns that indicate a single product page.
# Covers: /products/slug, /product/slug (not /product-category/),
#         Etsy /listing/id, WooCommerce /p/slug
_PRODUCT_PATH_RE = re.compile(
    r'/products?/(?!category)[^/?#]+[/?#]?(?:$|[?#])'
    r'|/listing/\d+'
    r'|/p/[^/?#]+',
)


def _classify_url(url: str) -> Category:
    if _IGNORE_DOMAIN_RE.search(url):
        return "IGNORE"
    # Strip trailing punctuation that may have been grabbed by the URL regex
    url = url.rstrip('.,;)')
    # Un-crawlable Amazon product page → surface-only (see _AMAZON_* above).
    if _AMAZON_URL_RE.match(url) and _AMAZON_PRODUCT_PATH_RE.search(url):
        return "UNTRACKED_URL"
    try:
        path = url.split('/', 3)[3] if url.count('/') >= 3 else ""
        path = "/" + path
    except IndexError:
        path = "/"
    if _PRODUCT_PATH_RE.search(path):
        return "PRODUCT_URL"
    return "SHOP_URL"


# ---------------------------------------------------------------------------
# Line-level patterns
# ---------------------------------------------------------------------------

# Lines that are definitely IGNORE regardless of context
_IGNORE_LINE_RE = re.compile(
    r"""
    ^ \s* (?:
        orders\ to\ make
        | places\ to\ (?:buy|find)
        | etsy\ embroidery\ shops
        | animecollective\ stuff\ from\ sale
        | need\ to\ find
        | look\ for\ (?:a|some|good|some\ good|toji)
        | look\ at\ the\ threadheads
        | look\ some\ actual
        | look\ for\ a\ ring
        | find\ some
        | think\ about
        | buy\ organic
        | look\ league
        | look\ (?:at\ )?(?:pommel|nexusink|teepublic)
        | i\ want
        | a\ few\ more
        | esports\ brands
        | \[.*\]          # [Riven Shirts]
        | \(.*\)          # (15% off offer popup)
        | \^              # ^ Cool button-up shirts
        | -{3,}           # --- separator
        | -\s*(?:jiraya|todoroki|the\ great\ wave|100t|clothes\ from)
        | youngla
        | lise\ lab
    )
    """,
    re.I | re.VERBOSE,
)

# Generic section headers that look like shop-name format but aren't
_GENERIC_SECTION_RE = re.compile(
    r'^(?:orders\ to\ make|places\ to\ (?:buy|find)|etsy\ embroidery\ shops'
    r'|animecollective\ stuff\ from\ sale)',
    re.I,
)

# "ShopName:" or "ShopName / AltName:" on its own line (optional trailing parenthetical)
_SHOP_HEADER_RE = re.compile(
    r'^([A-Z0-9][A-Za-z0-9\s&\'/\-]{0,50}?):\s*(?:\([^)]*\))?\s*$',
)

# "look ShopName" / "look ShopName again" → SHOP_NAME
# Does not match "look for", "look at the", "look some", etc.
_LOOK_SHOP_RE = re.compile(
    r'^look\s+(?!for\b|at\s+the\b|some\b|league\b|at\s+pommel\b|nexusink\b|teepublic\b)'
    r'([A-Za-z][A-Za-z0-9\s]{2,35}?)(?:\s*,|\s+again\b|\s+has\b|\s+have\b|\s*$)',
    re.I,
)

# "Item(s) from ShopName" → LOOSE_MENTION
_FROM_SHOP_RE = re.compile(r'^(.+?)\s+from\s+([A-Za-z][\w\s]{1,40})$', re.I)

# "BrandName item item ..." where brand is one+ TitleCase words (w/ optional "and"/"&"),
# and the item portion starts with a lowercase word.
# Requires a clothing/item keyword somewhere in the rest.
_BRAND_FIRST_RE = re.compile(
    r'^([A-Z]\w*(?:(?:\s+(?:and|&)\s+|\s+)[A-Z]\w+)*)\s+([a-z].+)$',
)

_CLOTHING_RE = re.compile(
    r'\b(?:shirt|hoodie|sweater|cardigan|jacket|pants|joggers|shorts|tee|hat'
    r'|beanie|sweatshirt|button.?up|crewneck|kimono|coat|windbreaker|tank'
    r'|zip.?up|polo|clothes|merch|collection|design|sneaker|shoe|sock'
    r'|dress|skirt|vest|pant|top|sweat)\b',
    re.I,
)

# Bullet / dash item lines: "- item" or "• item"
_DASH_ITEM_RE = re.compile(r'^[-•]\s*(.+)$')

# Pure CamelCase standalone word → looks like a brand
_CAMELCASE_RE = re.compile(r'^[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+$')

# Section marker that separates a free-form "Notes:" section from the
# structured shop entries. If present, everything above it is ignored.
# The optional ``Clothing`` prefix lets the user keep parallel headers
# (``Clothing Shops and URLs:`` / ``Non-clothing Shops and URLs:``)
# without breaking the legacy ``Shops and URLs:`` form.
_SHOPS_AND_URLS_HEADER_RE = re.compile(
    r'^\s*(?:Clothing\s+)?Shops\s+and\s+URLs\s*:\s*$',
    re.IGNORECASE | re.MULTILINE,
)

# Optional second-section marker that flips entries to is_clothing=False.
# Lives below the main "Shops and URLs:" block; everything beneath it is
# treated as non-clothing (gadgets, kitchenware, anything where size/fit
# review and clothing-keyword gates don't apply). Order_scan uses the same
# marker to skip fit-review prompts on matched purchases.
_NON_CLOTHING_HEADER_RE = re.compile(
    r'^\s*Non[-\s]?clothing\s+Shops\s+and\s+URLs\s*:\s*$',
    re.IGNORECASE | re.MULTILINE,
)

# "Shops to track sales for:" section — the SMS-sale allowlist, managed in the
# Doc instead of the SMS_SALE_SHOPS env var (the two are unioned downstream).
# Lines beneath it are bare shop names the user gets marketing texts from but
# hasn't watchlisted; they are NEVER crawled / homepage-checked — they only
# widen voice/email sale *attribution* (see main._voice_pipeline). classify()
# strips this section so its names don't leak in as SHOP_NAME entries (the
# header itself even matches the generic ``ShopName:`` shape); sales_tracking_shops()
# extracts them. The section ends at a blank line, the next known section header,
# or end of doc — so it can live anywhere in the Doc.
_SALES_TRACK_HEADER_RE = re.compile(
    r'^\s*Shops?\s+to\s+track\s+sales\s+for\s*:\s*$',
    re.IGNORECASE | re.MULTILINE,
)

# Leading list bullet to tolerate on a sales-track name ("- Vitaly" → "Vitaly").
_SALES_TRACK_BULLET_RE = re.compile(r'^[-•*]\s*')


def _is_known_section_header(line: str) -> bool:
    """A line that opens one of the Doc's recognised top-level sections — used to
    terminate the flat "Shops to track sales for:" list when the user puts it
    above another section without a separating blank line."""
    return bool(
        _SHOPS_AND_URLS_HEADER_RE.match(line)
        or _NON_CLOTHING_HEADER_RE.match(line)
        or _SALES_TRACK_HEADER_RE.match(line)
    )


def _clean_sales_track_name(line: str) -> str:
    """Normalise one sales-track line to a bare shop name (drop a leading bullet
    and a trailing list comma), so the user can format the list however reads
    best."""
    name = _SALES_TRACK_BULLET_RE.sub('', line.strip())
    return name.rstrip(',;').strip()


def _split_sales_track_section(text: str) -> tuple[str, list[str]]:
    """Pull the "Shops to track sales for:" section out of ``text``.

    Returns ``(text_without_that_section, [shop names])``. The shop list is
    de-duplicated case-insensitively, preserving first-seen order and the
    user's original casing (the name is shown as-is in the digest). The header
    and the names are removed from the returned text so ``classify()`` never
    emits entries for them.
    """
    out_lines: list[str] = []
    shops: list[str] = []
    in_section = False
    for raw in text.splitlines():
        line = raw.strip()
        if in_section:
            if not line:
                in_section = False
                out_lines.append(raw)
                continue
            if not _is_known_section_header(line):
                name = _clean_sales_track_name(line)
                if name:
                    shops.append(name)
                continue
            in_section = False
            # A new section header ends the list; fall through so the header
            # stays in the returned text and is handled normally.
        if _SALES_TRACK_HEADER_RE.match(line):
            in_section = True
            continue
        out_lines.append(raw)

    seen: set[str] = set()
    uniq: list[str] = []
    for s in shops:
        key = s.lower()
        if key not in seen:
            seen.add(key)
            uniq.append(s)
    return "\n".join(out_lines), uniq


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify(text: str) -> list[Entry]:
    """Parse raw watchlist text into a list of Entry tuples.

    The watchlist may begin with a free-form ``Notes:`` section, separated
    from the structured shop entries by a ``Shops and URLs:`` header line.
    When that header is present, everything above it is ignored (the user
    uses it as scratch space for unstructured thoughts). When absent (older
    docs without the split), the whole text is parsed.

    A second optional header — ``Non-clothing Shops and URLs:`` — can sit
    below the main section. Entries beneath it are tagged
    ``is_clothing=False`` and bypass the clothing-keyword gate on the
    brand-first and shop-section fall-through rules (so a bare "Logitech G
    Pro X Superlight" line attaches as a LOOSE_MENTION). The current_shop
    is reset at the section boundary so a shop header inside one section
    can't leak into the other.

    Bare shop headers — ``ShopName:`` with no URLs or items beneath them —
    are dropped after parsing. These are post-purchase placeholders the user
    keeps as a memory aid, and we don't want to spend DDG queries / Claude
    tokens resolving them or surface them in the digest.
    """
    marker = _SHOPS_AND_URLS_HEADER_RE.search(text)
    if marker:
        text = text[marker.end():]

    # Strip the "Shops to track sales for:" section so its bare shop names are
    # never parsed as watchlist entries (they'd otherwise resolve as SHOP_NAMEs
    # and get pointlessly homepage-checked). sales_tracking_shops() reads them.
    text, _ = _split_sales_track_section(text)

    entries: list[Entry] = []
    current_shop: str = ""
    is_clothing: bool = True

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Non-clothing section boundary. Reset current_shop so an entry
        # above the marker can't accidentally adopt a shop header below it
        # (or vice-versa).
        if _NON_CLOTHING_HEADER_RE.match(line):
            is_clothing = False
            current_shop = ""
            continue

        # 1. Handle URL lines — extract all URLs, classify each, then skip to next line.
        # A priority marker anywhere on the line tags every URL on it as priority
        # (only PRODUCT_URLs flow into the digest's "Watching now" block downstream).
        urls = _URL_RE.findall(line)
        if urls:
            priority = bool(_PRIORITY_MARKER_RE.search(line))
            for url in urls:
                cat = _classify_url(url)
                if cat != "IGNORE":
                    # Drop a marker that was typed flush against the URL (no
                    # space), so the stored URL is still fetchable.
                    clean = _PRIORITY_MARKER_RE.sub('', url).rstrip('.,;)')
                    entries.append(
                        Entry(cat, clean, current_shop, is_clothing, priority)
                    )
            continue

        # 1.5. A generic section divider ("Animecollective stuff from sale:",
        # "Orders to make next:", "Places to buy Rugs:") looks like a
        # "ShopName:" header but actually opens a new non-shop section. Reset
        # current_shop so URLs/items beneath it don't inherit the *previous*
        # shop's context (issue #4: animecollective product URLs were being
        # attributed to the "100moons" header above them). The line itself is
        # then dropped, same as before. Must run before the _IGNORE_LINE_RE
        # check below, which also matches these dividers but continues without
        # resetting.
        if _GENERIC_SECTION_RE.match(line):
            current_shop = ""
            continue

        # 2. Definitely-ignore lines
        if _IGNORE_LINE_RE.search(line):
            continue

        # 3. Shop header: "ShopName:" possibly with trailing note
        m = _SHOP_HEADER_RE.match(line)
        if m and not _GENERIC_SECTION_RE.match(line):
            current_shop = m.group(1).strip()
            entries.append(Entry("SHOP_NAME", current_shop, line, is_clothing))
            continue

        # 4. Bullet/dash item under current shop context
        m = _DASH_ITEM_RE.match(line)
        if m and current_shop:
            item = m.group(1).strip().rstrip('?')
            if item and not _IGNORE_LINE_RE.search(item):
                entries.append(Entry("LOOSE_MENTION", item, current_shop, is_clothing))
            continue

        # 5. "Look ShopName" → SHOP_NAME (updates current_shop)
        m = _LOOK_SHOP_RE.match(line)
        if m:
            shop = m.group(1).strip()
            current_shop = shop
            entries.append(Entry("SHOP_NAME", shop, line, is_clothing))
            continue

        # 6. "Item from ShopName" → LOOSE_MENTION
        # Does NOT update current_shop — only explicit headers/look-patterns do.
        m = _FROM_SHOP_RE.match(line)
        if m:
            item, shop = m.group(1).strip(), m.group(2).strip()
            entries.append(Entry("LOOSE_MENTION", item, shop, is_clothing))
            continue

        # 6.5. Line under an established shop section — accepted as an item
        # mention. In the clothing section we require a clothing-vocab hit
        # (the catch-all that picks up CatgirlRiot's "Tax evasion tank" /
        # "bafu meido shirt" lines without losing unrelated free-form
        # notes). In the non-clothing section that gate would drop every
        # gadget name, so we accept any line as a LOOSE_MENTION.
        if current_shop and (not is_clothing or _CLOTHING_RE.search(line)):
            entries.append(Entry("LOOSE_MENTION", line, current_shop, is_clothing))
            continue

        # 7. "BrandName item..." → LOOSE_MENTION. Clothing section keeps the
        # clothing-keyword gate (the rest of the line must mention a garment
        # word); non-clothing accepts any brand-first line.
        m = _BRAND_FIRST_RE.match(line)
        if m and (not is_clothing or _CLOTHING_RE.search(m.group(2))):
            shop = m.group(1).strip()
            entries.append(Entry("LOOSE_MENTION", line, shop, is_clothing))
            continue

        # 8. Pure CamelCase word → SHOP_NAME
        if _CAMELCASE_RE.match(line):
            current_shop = line
            entries.append(Entry("SHOP_NAME", line, line, is_clothing))
            continue

        # Default: IGNORE

    # Drop SHOP_NAME entries that gained no children — these are
    # placeholder headers the user keeps as a memory aid for shops they've
    # already bought from. Resolving them via DDG/Claude is pure waste.
    shop_names_with_children: set[str] = set()
    for e in entries:
        if e.category != "SHOP_NAME" and e.context:
            shop_names_with_children.add(e.context)
    return [
        e for e in entries
        if e.category != "SHOP_NAME" or e.value in shop_names_with_children
    ]


def sales_tracking_shops(text: str) -> list[str]:
    """Shop names listed under a "Shops to track sales for:" header in the Doc.

    These widen the SMS/email sale *attribution* set (so a texted/emailed sale
    from a shop the user doesn't watchlist still surfaces) without ever being
    crawled. Searches the whole Doc, so the section can sit anywhere — including
    above the "Shops and URLs:" marker that ``classify()`` otherwise discards.
    Unioned with the ``SMS_SALE_SHOPS`` env var in ``main`` (Doc is the primary
    surface; the env var remains a supplement).
    """
    _, shops = _split_sales_track_section(text)
    return shops
