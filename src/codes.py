"""Harvest promo codes from raw watchlist text."""
from __future__ import annotations

import re

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
})


def _looks_like_hex_color(core: str) -> bool:
    """6-char hex-only tokens (F8F8F8, FFFFFF) are CSS color values bleeding
    out of inline styles, not promo codes."""
    return len(core) == 6 and all(c in "0123456789ABCDEFabcdef" for c in core)


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
    if core.upper() in _NON_CODE_MARKETING_WORDS:
        return False  # rejects "DISCOUNT", "COUPON" as code candidates
    if core.upper() in _HTML_ARTIFACT_WORDS:
        return False  # rejects "DOCTYPE", "PUBLIC", "XHTML" from leaked HTML
    if _looks_like_hex_color(core):
        return False  # rejects "F8F8F8" CSS colours bleeding out of styles
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
    """Return list of {shop, code, context} dicts found in watchlist text."""
    results: list[dict] = []
    lines = text.splitlines()

    current_shop = ""
    for line in lines:
        stripped = line.strip()

        # Update current shop context
        m = _SHOP_HEADER_RE.match(stripped)
        if m:
            current_shop = m.group(1).strip()

        # Only scan lines that look code-adjacent
        if not _CODE_CONTEXT_RE.search(stripped):
            continue

        for token in _CODE_TOKEN_RE.finditer(stripped):
            raw = token.group(1)
            if not _is_valid_code(raw):
                continue
            results.append({
                "shop": current_shop,
                "code": _canonicalise_code(raw),
                "context": stripped,
                "confidence": _classify_confidence(raw),
            })

    return results
