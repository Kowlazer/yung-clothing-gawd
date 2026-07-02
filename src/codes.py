"""Harvest promo codes from raw watchlist text."""
from __future__ import annotations

import re

# Single source of truth for the "Shops and URLs:" marker that splits the
# free-form Notes section from the structured shop entries. Imported (not
# re-defined) so codes harvesting honours the exact same split classify()
# does. classify imports nothing from codes, so this is cycle-free.
from src.classify import _SHOPS_AND_URLS_HEADER_RE

# Lines containing code-adjacent keywords
_CODE_CONTEXT_RE = re.compile(
    r'(?:code|discount|coupon|off|promo)\b',
    re.I,
)

# A promo code: alphanumeric, 3-19 chars, may end with !
# Leading character is [A-Za-z0-9] (not just [A-Za-z]) because real-world
# Postscript / Klaviyo / Shopify Discounts often issue digit-leading codes
# like 7KXQ4PMV. Lowercase letters are allowed because shops increasingly
# display codes in marketing-friendly mixed case (e.g. ``SummerSale15``,
# ``BlackFriday2026``); checkouts accept them case-insensitively. The
# stricter ``_is_valid_code`` filter compensates for the broader shape match.
# Use lookaround instead of \w so the optional trailing ! and internal hyphens
# are handled correctly. Internal hyphens are allowed so multi-segment codes
# like QRST-UVWX-YZAB match as one token instead of three.
# Token-shape match only — call _is_valid_code() to filter out marketing
# acronyms (SMS, STOP, REPLY, SHOP, FREE...) and bare year numbers (2025).
_CODE_TOKEN_RE = re.compile(
    r'(?<![\w-])([A-Za-z0-9][A-Za-z0-9]{2,18}(?:-[A-Za-z0-9]+){0,4}!?)(?![\w-])'
)


# Words that ARE the context signal — they describe a code, they're not a
# code. Surfaced 2026-05-25 by the Anime Ape email which has a "CLAIM
# DISCOUNT" button on its own line: the line satisfies the context regex
# (contains "discount") AND the all-uppercase ≥6-letter token "DISCOUNT"
# passes the no-digit-no-hyphen branch of _is_valid_code. Without this
# deny-set the matcher would emit DISCOUNT as a code.
_NON_CODE_MARKETING_WORDS = frozenset({
    "DISCOUNT", "DISCOUNTS",
    "COUPON", "COUPONS",
    "PROMOTION", "PROMOTIONS",  # PROMO is too short to pass the >=6 rule
    "UNSUBSCRIBE",
    "CHECKOUT",
})


# HTML / XML / template structural keywords that leak in when a sender ships
# raw HTML in a text/plain MIME part (gmail.py strips that now, but this is
# defense in depth). Unlike marketing shout-words, these are NEVER real promo
# codes, so they're hard-rejected rather than soft-denied. Hex colours
# (#F8F8F8) are handled separately by ``_looks_like_hex_color``.
_HTML_ARTIFACT_WORDS = frozenset({
    "DOCTYPE", "DTD", "XHTML", "PUBLIC", "CDATA", "NBSP",
})


# Larger soft-deny set used by `_classify_confidence` — these still ship to
# the digest but bucket into "low confidence" so the user can scan past them.
# Goal is *not* to enumerate every English word; just the ones we've actually
# observed in production unattributed-codes output, plus the most common
# marketing-shout vocabulary.
#
# Why soft-deny and not hard-reject: missing a real promo code is a strictly
# worse failure mode than displaying a low-confidence word. Some shops *do*
# use words like CHANCE or SUMMER as real codes; the digest's confidence
# grouping lets the user spot them without us having to be right.
_LOW_CONFIDENCE_WORDS = frozenset({
    # Echo of _NON_CODE_MARKETING_WORDS so a token in either set lands here.
    "DISCOUNT", "DISCOUNTS",
    "COUPON", "COUPONS",
    "PROMOTION", "PROMOTIONS",
    "UNSUBSCRIBE",
    "CHECKOUT",
    # Marketing shout-words observed in unattributed_codes on the prod Gist
    # (2026-05-25): tokens that pass _is_valid_code's all-letter-≥6 branch
    # purely by virtue of being in CAPS near "off"/"discount".
    "SITEWIDE",
    "CLEARANCE",
    "CHANCE",
    "SELECTED",
    "REDEEM",
    "MYSTERY", "MYSTERIES",
    "SCRIPT",
    "UNIQUE",
    "SUMMER", "WINTER", "SPRING", "AUTUMN", "HOLIDAY",
    "MEMORIAL",
    "EVERYTHING",
    "DOLLAR", "DOLLARS",
    "FOUNTAINS",
    "NIGHTSTANDS",
    "FAVORITES", "FAVORITE",
    "WEEKEND",
    "EXCLUSIVE",
    "LIMITED",
    "SPECIAL",
    "OFFER", "OFFERS",
    "SAVINGS",
    "ARRIVALS", "ARRIVAL",
    "DELIVERY", "SHIPPING",
    "PRIVATE",
    "UNIQID",  # Unfilled {{UNIQID}} template placeholder
    # More generic marketing / urgency words observed in the 2026-07-01
    # unattributed output (Wayfair / Ticketmaster / shopify-email senders).
    "TONIGHT", "TOMORROW", "STARTS", "STARTING",
    "RUNNING", "RESTOCK", "SECONDS", "STANDING", "STANDS",
    "COLLECTIONS", "CLEAROUT", "ENTERTAINMENT", "BIGGEST",
    "TABLES", "CENTERS", "APPLIED", "ENTERED", "BOARDING",
})


# Month-name + day tokens ("MAY-15", "JUNE-30", "DEC-25") are sale *deadlines*,
# not promo codes. The internal hyphen the token regex now tolerates (so
# multi-segment codes like OKRK-RVKAJ-NSZN match whole) would otherwise harvest
# them from marketing copy such as "use code by MAY-15" (issue #5). A
# hyphen-LESS "MAY15" stays a valid digit-bearing code; only the dated
# MONTH-DD shape is rejected.
_MONTH_DAY_RE = re.compile(
    r"^(?:JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUNE?|JULY?"
    r"|AUG(?:UST)?|SEP(?:T(?:EMBER)?)?|OCT(?:OBER)?|NOV(?:EMBER)?|DEC(?:EMBER)?)"
    r"-\d{1,2}$",
    re.IGNORECASE,
)


def _looks_like_hex_color(core: str) -> bool:
    """6-char hex-only tokens (F8F8F8, FFFFFF) are CSS color values bleeding
    out of inline styles, not promo codes."""
    return len(core) == 6 and all(c in "0123456789ABCDEFabcdef" for c in core)


def _looks_like_date(core: str) -> bool:
    """A month-name + day token ("MAY-15", "JUNE-30") is a deadline, not a code."""
    return bool(_MONTH_DAY_RE.match(core))


# --- Hard-reject shapes: URLs bleed machine identifiers into the token stream --
#
# When a marketing email renders a tracking / unsubscribe / view-in-browser URL
# as *visible* text, BeautifulSoup.get_text keeps the raw URL, and the token
# regex — which splits on the non-word "%", "/", "?" — harvests URL fragments,
# per-recipient tracking blobs, UUID event ids and query-param timestamps as if
# they were promo codes. None of these is ever a real code. They're rejected
# here (the primary defence is _strip_urls, which removes URLs *before*
# tokenizing; these token-level checks are the backstop for fragments that leak
# through incomplete URL detection and let cleanup_codes.py purge stored junk).

# Real promo codes are short. The longest we've observed top out ~17 chars
# (VILLAGE108CDG5DRD); anything past 24 is a tracking blob / URL slug (Staples
# ships 40+ char per-recipient link tokens like ETD0Q1O-...-OBXLVQZU8JJL0Q5MRL).
_MAX_CODE_LEN = 24

# Canonical 8-4-4-4-12 hex UUID — ESP/message event ids, Klaviyo/Shopify
# line-item keys. ShawnCraft's emails alone leaked ~35 of these; they match the
# hyphen-tolerant token shape and (having a hyphen) even classify as "high".
_UUID_RE = re.compile(
    r'^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-'
    r'[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$'
)

# ISO date / datetime fragment from a URL query param
# (email.oldnavy.com's "...?ts=2026-06-19T00..." -> 2026-06-19T00).
_ISO_DATETIME_RE = re.compile(r'^\d{4}-\d{2}-\d{2}(?:T\d{2}(?::\d{2})?)?$')

# Ordinals ("250TH") and decade words ("1900S", "1950S") lifted from marketing
# prose ("our 250th year", "1950s-inspired"). The existing length<5 digit rule
# already kills "11TH"/"30TH"; these are the 5-char cases that slip past. A
# standalone ordinal/decade is essentially never a promo code.
_ORDINAL_DECADE_RE = re.compile(r'^\d+(?:ST|ND|RD|TH)$|^(?:19|20)\d0S$', re.I)

# Percent-encoding leftovers. The token regex splits on the "%" and harvests
# the "<hexpair><rest>" tail:  %2522Max -> 2522MAX (double-encoded "),
# %253A100 -> 253A100 (double-encoded :), %3Dmmkdude -> 3DMMKDUDE (= + a query
# value), %3Fads -> 3FADS (? + a query), %E2%80%8Bthe -> 8BTHE (zero-width-space
# UTF-8 tail). The double-encoding (%25XX) prefixes plus the "=", "?", ";" and
# UTF-8 lead/continuation single-byte encodings never begin a real code, so
# they're hard-rejected. The *ambiguous* single encodings are deliberately
# excluded — %2F "/", %40 "@", %2B "+", %20 " ", %25 "%" collide with genuine
# numeric codes (2FOR1, 40OFF, 20OFF) — and are handled by _strip_urls instead.
_URL_ENCODED_RE = re.compile(
    r'^(?:'
    r'25(?:20|22|23|26|2C|2F|3A|3B|3D|3F|40)'   # double-encoded %25XX
    r'|3D|3F'                                     # = ?  (unambiguous single)
    r'|8B|A0|C2|C3|E2'                            # UTF-8 lead/continuation bytes
    r')[0-9A-Za-z]',
    re.I,
)

# Visible URLs, stripped before tokenizing (see _strip_urls).
_URL_RE = re.compile(r'https?://\S+|www\.\S+', re.I)


def _strip_urls(text: str) -> str:
    """Remove visible URLs from a line before code tokenizing.

    A marketing email that renders a tracking / unsubscribe URL as visible text
    leaks its percent-encoded query params, path slugs and per-recipient
    tracking blobs into the token stream (2FBEST-SELLER, 40GMAIL,
    ETD0Q1O-...-OBXLVQZU8JJL0Q5MRLFK, 2026-06-19T00). None of those are promo
    codes, and a real "use code SAVE20" always lives in prose, never inside a
    URL — so dropping URL runs is safe and kills the whole class at the source.
    """
    return _URL_RE.sub(" ", text or "")


def _looks_like_uuid(core: str) -> bool:
    return bool(_UUID_RE.match(core))


def _looks_like_url_fragment(core: str) -> bool:
    return bool(_URL_ENCODED_RE.match(core))


def _looks_like_timestamp(core: str) -> bool:
    return bool(_ISO_DATETIME_RE.match(core))


def _looks_like_ordinal_or_decade(core: str) -> bool:
    return bool(_ORDINAL_DECADE_RE.match(core))


def _looks_like_hex_blob(core: str) -> bool:
    """An all-hex token >=12 chars is a hash / opaque id (A2F0FD2D453F4F314),
    not a code. Real all-letter/alnum codes almost always carry a non-hex
    letter (G-Z minus A-F: the K/X/Q/V in 7KXQ4PMV, the V/I/L/G in
    VILLAGE108CDG5DRD), so this never touches a genuine code. 12 is the floor
    so an 8-char code that happens to be all-hex (DEADBEEF) isn't caught; the
    6-char CSS-colour case is handled separately by _looks_like_hex_color."""
    return len(core) >= 12 and all(c in "0123456789ABCDEFabcdef" for c in core)


def _classify_confidence(token: str) -> str:
    """Return ``"high"`` / ``"medium"`` / ``"low"`` for a code-shaped token.

    Used by the digest to group unattributed codes so likely-marketing words
    sit visually separated from real-looking codes. Does NOT reject — every
    token that passes :func:`_is_valid_code` still ends up in ``codes.json``,
    just with a confidence tag.

    Buckets:
      * ``high`` — has both a digit and a letter and ≥5 chars (DENIM40,
        ARTHUR5, MEMORIAL20, 60FORYOU, 85N62WY9GHJ6), or hyphenated all-caps
        (QRST-UVWX-YZAB), or ends with ``!`` (BRANDECHO!, FREESHIP!).
        These shapes basically never appear by accident in marketing copy.
      * ``low`` — token (case-insensitive) is in :data:`_LOW_CONFIDENCE_WORDS`
        OR looks like a hex color (F8F8F8). Marketing shout-words and HTML
        artifacts live here.
      * ``medium`` — everything else, mostly all-letter all-caps tokens like
        BRANDECHO, PEAKVIP, BRANDVIP. Could be a real brand-themed code, or
        a marketing word we haven't catalogued.
    """
    core = token.rstrip("!")
    upper = core.upper()
    if upper in _LOW_CONFIDENCE_WORDS:
        return "low"
    if _looks_like_hex_color(core):
        return "low"
    if token.endswith("!"):
        return "high"
    if "-" in core:
        return "high"
    has_digit = any(c.isdigit() for c in core)
    has_letter = any(c.isalpha() for c in core)
    if has_digit and has_letter and len(core) >= 5:
        return "high"
    return "medium"


def _is_valid_code(token: str) -> bool:
    """Filter token-shape matches down to plausible promo codes.

    A token must be at least one of:
      * has at least one digit, e.g. ``SPRING30``, ``SummerSale15``,
        ``7KXQ4PMV``, ``Welcome2025`` (mixed case allowed since the regex now
        accepts lowercase letters), or
      * all-letter, all-uppercase, hyphenated (``QRST-UVWX-YZAB``), or
      * all-letter, all-uppercase, and >=6 chars (``PEAKVIP``, ``WELCOME``,
        ``IMPROV``).

    Rejects:
      * machine identifiers bled in from URLs rendered as visible text —
        UUIDs (``0420AAE2-4D91-4C10-89D3-CF3680E36783``), opaque hex blobs
        (``A2F0FD2D453F4F314``), percent-encoding fragments (``2522MAX``,
        ``3DHTTPS``, ``8BTHE``), ISO timestamps (``2026-06-19T00``), and any
        token over ``_MAX_CODE_LEN`` chars (Staples' 40-char tracking blobs).
        These are the dominant false-positive class in real inboxes;
      * bare marketing acronyms (SMS, STOP, REPLY, OPT, SHOP, FREE, JOIN,
        HELP, MORE) — fail the >=6-char rule;
      * bare numeric tokens like ``2025`` — fail the has-letter rule;
      * plain English words / URL slugs (``kitchen``, ``promise``,
        ``arrivals``, ``off-script-red-embroidered-beanie``) that the
        mixed-case regex now lets through as token-shape — fail the
        "must have uppercase or digit" rule.
      * Mixed-case codes without a digit (``BlackFriday``) — rejected for the
        same reason; in practice nearly every real all-letter code is shipped
        UPPERCASE.
    """
    core = token.rstrip("!")
    if len(core) > _MAX_CODE_LEN:
        return False  # tracking blobs / URL slugs, never a real code
    if core.upper() in _NON_CODE_MARKETING_WORDS:
        return False  # rejects "DISCOUNT", "COUPON" as code candidates
    if core.upper() in _HTML_ARTIFACT_WORDS:
        return False  # rejects "DOCTYPE", "PUBLIC", "XHTML" from leaked HTML
    if _looks_like_hex_color(core):
        return False  # rejects "F8F8F8" CSS colours bleeding out of styles
    if _looks_like_hex_blob(core):
        return False  # rejects "A2F0FD2D453F4F314" opaque hex ids
    if _looks_like_uuid(core):
        return False  # rejects "0420AAE2-4D91-4C10-89D3-CF3680E36783" ESP ids
    if _looks_like_url_fragment(core):
        return False  # rejects "2522MAX" / "3DHTTPS" / "8BTHE" URL fragments
    if _looks_like_timestamp(core):
        return False  # rejects "2026-06-19T00" ISO date/time from a URL param
    if _looks_like_ordinal_or_decade(core):
        return False  # rejects "250TH" ordinals / "1950S" decades from prose
    if _looks_like_date(core):
        return False  # rejects "MAY-15" / "JUNE-30" deadlines (issue #5)
    letters = [c for c in core if c.isalpha()]
    if not letters:
        return False  # rejects "2025", "30"
    has_digit = any(c.isdigit() for c in core)
    if has_digit:
        # Real promo codes with digits are essentially always >=5 chars
        # (SPRING30, VIP25, SMS25, 7KXQ4PMV, SummerSale15). Anything
        # shorter is almost certainly an ordinal or time-of-day token
        # ("11TH", "30TH", "12PM", "4FOR") picked up by the sliding-window
        # context check from a Memorial-Day marketing email; rejecting
        # length<5 kills that whole class of false positives without
        # dropping any known-good code.
        return len(core) >= 5
    has_lower = any(c.islower() for c in letters)
    if has_lower:
        # No digit and contains lowercase: this is the regex's expanded
        # surface area. Reject everything here to filter out URL slugs
        # ("off-script-red-embroidered-beanie") and plain English words
        # ("kitchen", "promise", "BlackFriday"). Real all-letter codes
        # without digits are conventionally ALL-CAPS.
        return False
    # All-letter, all-uppercase from here on.
    if "-" in core:
        return True  # QRST-UVWX-YZAB
    return len(letters) >= 6  # PEAKVIP, WELCOME, IMPROV pass; SMS/STOP fail


def _canonicalise_code(token: str) -> str:
    """Uppercase the code for storage / dedupe so ``SummerSale15`` and
    ``SUMMERSALE15`` collapse to one entry in ``codes.json``. The trailing
    ``!`` (if any) is preserved — some shops issue codes like ``FREESHIP!``."""
    return token.upper()

# Look-back to find which shop a code belongs to — scan backwards for the nearest
# "ShopName:" header or known shop name
_SHOP_HEADER_RE = re.compile(r'^([A-Z][A-Za-z0-9\s&]{1,40}?):\s*$', re.M)


def harvest_codes(text: str) -> list[dict]:
    """Return list of {shop, code, context} dicts found in watchlist text.

    Honours the same ``Shops and URLs:`` split that ``classify()`` uses (issue
    #3). Codes in the free-form Notes section *above* the marker are still
    harvested, but left **unattributed** (``shop=""``): the headings there
    ("Orders to make next:") are scratch notes the user keeps, not real shops,
    and attributing a code to one of them is misleading. Codes *below* the
    marker are attributed to the nearest ``ShopName:`` header as before. When
    the marker is absent (older docs without the split) the whole text is
    treated as the attributed section, preserving legacy behaviour.
    """
    marker = _SHOPS_AND_URLS_HEADER_RE.search(text)
    if marker:
        notes_text, shops_text = text[:marker.start()], text[marker.end():]
    else:
        notes_text, shops_text = "", text

    results: list[dict] = []
    results.extend(_harvest_section(notes_text, attribute=False))
    results.extend(_harvest_section(shops_text, attribute=True))
    return results


def _harvest_section(text: str, *, attribute: bool) -> list[dict]:
    """Harvest codes from one section of the watchlist.

    ``attribute=True`` tracks the nearest ``ShopName:`` header and stamps it on
    each code; ``attribute=False`` (the Notes section) leaves ``shop=""`` since
    its headings aren't shops.
    """
    results: list[dict] = []
    current_shop = ""
    for line in text.splitlines():
        stripped = line.strip()

        # Update current shop context (attributed section only). Shop headers
        # ("ShopName:") never contain a URL, so match on the raw line.
        if attribute:
            m = _SHOP_HEADER_RE.match(stripped)
            if m:
                current_shop = m.group(1).strip()

        # Strip visible URLs before scanning so a pasted product/tracking URL
        # can't leak percent-encoded fragments or slugs as fake codes; the
        # stored context keeps the original line for readability.
        scan_line = _strip_urls(stripped)

        # Only scan lines that look code-adjacent
        if not _CODE_CONTEXT_RE.search(scan_line):
            continue

        for token in _CODE_TOKEN_RE.finditer(scan_line):
            raw = token.group(1)
            if not _is_valid_code(raw):
                continue
            results.append({
                "shop": current_shop if attribute else "",
                "code": _canonicalise_code(raw),
                "context": stripped,
                "confidence": _classify_confidence(raw),
            })

    return results
