"""Deterministic parsers for order- and shipping-confirmation emails.

Everything in here is pure code — no Claude calls. Used by
``src/order_scan.py`` to fill the wardrobe-item fields that don't need a
language model:

  * ``shop`` and ``shop_canonical_name`` — from sender domain via the
    existing ``shop_aliases.json`` reverse index, falling back to the
    sender's apex domain when no alias matches.
  * ``total`` and ``currency`` — regex against the email body. Covers
    the templates seen in testing (Shopify, Amazon, WooCommerce, plain
    text) plus generic ``Total: $X.XX`` / ``EUR 99,00`` formats.
  * ``tracking_url`` — regex pulling the first carrier-domain link from
    the body (UPS, FedEx, USPS, DHL, Canada Post, aftership, route.com,
    shipstation, etc.).
  * ``purchased_at`` / ``shipped_at`` — already done in
    ``order_scan._date_from_header``; re-exported here for symmetry.

Each helper returns ``None`` rather than guessing when the signal isn't
present. That keeps the wardrobe schema honest — a missing field is
better than a hallucinated one when the user will later filter sale
signals against this data.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Shop name resolution
# ---------------------------------------------------------------------------

_FROM_DOMAIN_RE = re.compile(r"@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")


def sender_domain(from_header: str) -> str:
    """Extract the lowercased apex domain from an RFC 2822 From header.

    "Norse Projects <hi@norseprojects.com>" -> "norseprojects.com"
    "" -> ""
    """
    m = _FROM_DOMAIN_RE.search(from_header or "")
    if not m:
        return ""
    return m.group(1).lower()


def _aliases_by_domain(shop_aliases: dict[str, str]) -> dict[str, str]:
    """Build ``{domain → canonical_shop_name}`` from ``shop_aliases.json``.

    Same logic as ``gmail._aliases_by_domain`` — duplicated here so this
    module has no dependencies on gmail.py.
    """
    out: dict[str, str] = {}
    for shop, url in (shop_aliases or {}).items():
        if not url:
            continue
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        if netloc and netloc not in out:
            out[netloc] = shop
    return out


# Common transactional-email subdomains that should fold up to the apex
# (e.g. mail.norseprojects.com → norseprojects.com, delivery.hm.com → hm.com).
# The final `[a-z]\.` catches single-letter marketing/CDN subdomains like
# `s.greyfox.com` or `t.shopifyemail.com` that Shopify ESPs use.
_TRANSACTIONAL_SUBDOMAIN_RE = re.compile(
    r"^(mail|email|e|order|orders|shop|store|noreply|no-reply|hi|hello|"
    r"info|notifications?|transactional|sender|"
    # Shipping / delivery / marketing senders (e.g. H&M's us@delivery.hm.com,
    # which used to resolve to the shop "Delivery").
    r"delivery|deliveries|shipping|ship|tracking|track|news|updates?|"
    r"marketing|mailer|mailing|members?|account|cs|click|links?|go|"
    r"[a-z])\.",
    re.I,
)


def _strip_transactional_prefix(apex: str) -> str:
    """Strip a known transactional subdomain prefix, but only when the
    remainder still has at least one dot. Prevents pathological inputs
    like ``o.com`` from collapsing down to the bare TLD ``com``.
    """
    m = _TRANSACTIONAL_SUBDOMAIN_RE.match(apex)
    if not m:
        return apex
    stripped = apex[m.end():]
    if "." not in stripped:
        return apex
    return stripped


# Second-level labels that form part of a multi-part public suffix
# (".co.uk", ".com.au", ".co.jp", …). When the label just left of the TLD is one
# of these, the brand label sits one further left. A small curated set — enough
# for the ccTLD shapes a storefront uses — so we avoid a Public Suffix List
# dependency for what is only a last-resort name synthesis.
_SECOND_LEVEL_SUFFIXES = frozenset({
    "co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go",
})


def _registrable(apex: str) -> tuple[str, str]:
    """Split an apex into ``(brand_label, registrable_domain)``.

    The brand label is the registrable-domain label — *not* a surviving
    subdomain that no transactional-prefix rule stripped — so the no-alias
    synthesis names the shop after the brand, not an infra prefix:

        accounts.acmestore.com → ("acmestore", "acmestore.com")
        shop.brand.co.uk       → ("brand", "brand.co.uk")
        junkbrands.com         → ("junkbrands", "junkbrands.com")
    """
    parts = [p for p in apex.split(".") if p]
    if len(parts) < 2:
        return (parts[0] if parts else "", apex)
    n = 2
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL_SUFFIXES:
        n = 3
    return (parts[-n], ".".join(parts[-n:]))


# Apexes used by transactional/marketing email providers shared across
# many shops. When the sender's apex matches one of these, the shop
# identity is not in the domain — it lives in the From display name
# (e.g. "Dattehameha <noreply@t.shopifyemail.com>"). resolve_shop falls
# back to the display name for these cases.
_SHARED_TRANSACTIONAL_APEXES = frozenset({
    "shopifyemail.com",
    "myshopify.com",
    "sendgrid.net",
    "mailgun.org",
    "klaviyomail.com",
    "amazonses.com",
    "sparkpostmail.com",
    "mcsv.net",
    "rsgsv.net",
    "list-manage.com",
    "mailchimpapp.com",
})


# Captures the display name portion of an RFC 2822 From header:
#   "Dattehameha" <noreply@t.shopifyemail.com>  -> Dattehameha
#   Dattehameha <noreply@t.shopifyemail.com>    -> Dattehameha
_FROM_DISPLAY_NAME_RE = re.compile(r'^\s*"?([^"<>]+?)"?\s*<')

# Display-name strings that don't identify a shop. When the display name
# matches one of these (case-insensitive), the shared-sender fallback
# treats it as missing.
_GENERIC_DISPLAY_NAMES = frozenset({
    "order", "orders", "order confirmation", "order confirmations",
    "shop", "store", "team", "support", "customer service",
    "customer support", "hello", "hi", "info",
    "no reply", "noreply", "no-reply",
    "shipping", "shipping confirmation", "your order",
})


def _from_display_name(from_header: str) -> str:
    """Return the From-header display name, or ``""`` if absent/generic."""
    if not from_header:
        return ""
    m = _FROM_DISPLAY_NAME_RE.match(from_header)
    if not m:
        return ""
    name = m.group(1).strip()
    if not name or name.lower() in _GENERIC_DISPLAY_NAMES:
        return ""
    return name


def _normalise_case(name: str) -> str:
    """Title-case names that are uniformly upper or lower; leave mixed
    case alone (preserves intentional brand casing like 'theanimecollective').
    """
    has_upper = any(c.isupper() for c in name)
    has_lower = any(c.islower() for c in name)
    if has_upper and has_lower:
        return name
    return name.title()


def resolve_shop(
    from_header: str,
    shop_aliases: dict[str, str],
) -> tuple[str, str]:
    """Return ``(shop_canonical_name, shop_domain)`` for a parsed email.

    Strategy:
      1. Reverse-lookup the sender's apex domain against ``shop_aliases``.
      2. If no exact match, walk up subdomains (mail.shop.com → shop.com).
      3. If the apex is a shared transactional sender (shopifyemail.com,
         sendgrid.net, etc.) — the shop identity is in the From display
         name, not the domain. Fall back to that.
      4. If still no match, synthesise the canonical name from the
         *registrable* domain's brand label (``norseprojects.com`` →
         ``Norseprojects``; ``accounts.acmestore.com`` → ``Acmestore``).

    Always returns a non-empty ``shop_canonical_name`` when the sender
    has any parseable domain. ``shop_domain`` is the matched alias domain,
    the shared-sender apex, or the registrable domain — a surviving infra
    subdomain is never returned in the synthesis path.
    """
    domain = sender_domain(from_header)
    if not domain:
        return ("", "")

    domain_index = _aliases_by_domain(shop_aliases)
    apex = _strip_transactional_prefix(domain)

    if apex in domain_index:
        return (domain_index[apex], apex)

    # Walk up subdomains.
    parts = apex.split(".")
    for i in range(1, len(parts) - 1):
        parent = ".".join(parts[i:])
        if parent in domain_index:
            return (domain_index[parent], parent)

    # Shared transactional sender: shop is in the display name.
    if apex in _SHARED_TRANSACTIONAL_APEXES:
        display = _from_display_name(from_header)
        if display:
            return (_normalise_case(display), apex)

    # No alias hit — synthesise a canonical name from the *registrable* domain.
    # "norseprojects.com" → "Norseprojects", "junkbrands.com" → "Junkbrands",
    # and "accounts.acmestore.com" → "Acmestore" (the brand label, not the
    # "accounts" subdomain — the old parts[0] synthesis mis-named these
    # "Accounts"). The domain returned is the registrable domain so a surviving
    # infra subdomain doesn't leak into shop_domain. Crude but stable; user can
    # add a proper alias in shop_aliases.json later.
    label, registrable = _registrable(apex)
    canonical = label.replace("-", " ").title() if label else ""
    return (canonical, registrable)


# ---------------------------------------------------------------------------
# Excluded-shop matching (privacy filter for the wardrobe)
# ---------------------------------------------------------------------------

_EXCLUDE_NORMALISE_RE = re.compile(r"[\s\-._]+")


def _normalise_for_exclude(value: str | None) -> str:
    """Lowercase and strip spaces/hyphens/dots/underscores.

    Collapses ``"Nocturne Goods"``, ``"nocturne-goods.com"`` and
    ``"nocturne_goods"`` all to ``"nocturnegoods"`` so a single configured token
    matches a shop's display name and its domain regardless of punctuation."""
    return _EXCLUDE_NORMALISE_RE.sub("", (value or "").lower())


def is_excluded_shop(
    shop_name: str | None,
    shop_domain: str | None,
    excluded: tuple[str, ...],
) -> bool:
    """True when ``shop_name`` or ``shop_domain`` matches a configured exclusion.

    ``excluded`` is the lowercased token tuple from ``config.EXCLUDED_SHOPS``.
    Each token is matched as a normalised substring (see
    ``_normalise_for_exclude``) against both the shop name and the domain, so
    ``"nocturne goods"`` catches shop name ``"Nocturne Goods"`` and domain
    ``nocturne-goods.com``/``nocturnegoods.com`` alike. Empty ``excluded`` → False."""
    if not excluded:
        return False
    name_n = _normalise_for_exclude(shop_name)
    domain_n = _normalise_for_exclude(shop_domain)
    for token in excluded:
        tok_n = _normalise_for_exclude(token)
        if not tok_n:
            continue
        if (name_n and tok_n in name_n) or (domain_n and tok_n in domain_n):
            return True
    return False


# ---------------------------------------------------------------------------
# Total + currency
# ---------------------------------------------------------------------------

# Currency symbols (single-char) → ISO code.
_CURRENCY_SYMBOL_TO_CODE = {
    "$": "USD",   # ambiguous (AUD/CAD/NZD also use $) but defaults to USD;
                  # overridden when explicit code is in the same line
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₹": "INR",
    "₩": "KRW",
}
_CURRENCY_CODE_RE = re.compile(
    r"\b(USD|EUR|GBP|JPY|CAD|AUD|NZD|CHF|SEK|NOK|DKK|INR|KRW|HKD|SGD|MXN)\b",
)

# Matches "Order Total" / "Grand Total" / "Total" — followed by an amount.
# We require "total" to be a standalone word so we don't accidentally pick
# up "Subtotal" (handled by the alternate _SUBTOTAL_RE for fallback).
_TOTAL_LINE_RE = re.compile(
    r"(?:^|\W)(?:order\s+total|grand\s+total|total\s+amount|amount\s+(?:due|paid|charged)|total)"
    r"\s*[:\-]?\s*"
    r"(?P<symbol>[\$€£¥₹₩])?\s*"
    r"(?P<amount>\d{1,3}(?:[,.\s]\d{3})*(?:[.,]\d{2})?|\d+(?:[.,]\d{2})?)"
    r"\s*(?P<code>[A-Z]{3})?",
    re.I | re.M,
)
_SUBTOTAL_RE = re.compile(
    r"subtotal\s*[:\-]?\s*"
    r"(?P<symbol>[\$€£¥₹₩])?\s*"
    r"(?P<amount>\d{1,3}(?:[,.\s]\d{3})*(?:[.,]\d{2})?|\d+(?:[.,]\d{2})?)",
    re.I,
)


def _parse_amount(raw: str) -> float | None:
    """Convert a printed amount ("1,234.56", "1.234,56", "99") to a float.

    Handles both US-style (``,`` as thousands, ``.`` as decimal) and
    European-style (``.`` thousands, ``,`` decimal). When ambiguous,
    assumes US.
    """
    if not raw:
        return None
    s = raw.replace(" ", "")
    # If the string has both . and ,, the last one is the decimal sep.
    if "." in s and "," in s:
        if s.rindex(",") > s.rindex("."):
            # European: 1.234,56
            s = s.replace(".", "").replace(",", ".")
        else:
            # US: 1,234.56
            s = s.replace(",", "")
    elif "," in s:
        # Only commas. Two digits after comma = decimal; else thousands.
        last_comma = s.rfind(",")
        if len(s) - last_comma - 1 == 2:
            s = s[:last_comma] + "." + s[last_comma + 1:]
        else:
            s = s.replace(",", "")
    # else: only dots, leave as-is
    try:
        return float(s)
    except ValueError:
        return None


def extract_total(body: str) -> dict | None:
    """Return ``{"amount": float, "currency": str}`` or None.

    Tries the explicit-total regex first; falls back to subtotal when no
    total line is present (some Shopify themes show "Subtotal" only on
    digital-only orders). The "Total" regex prefers the LAST total line
    in the body — order totals usually appear after item lines and
    summaries, and "Total" mid-email is sometimes for a coupon or section
    sub-total.
    """
    if not body:
        return None

    total_matches = list(_TOTAL_LINE_RE.finditer(body))
    chosen = total_matches[-1] if total_matches else None
    if chosen is None:
        sub = _SUBTOTAL_RE.search(body)
        if sub is None:
            return None
        chosen = sub

    amount = _parse_amount(chosen.group("amount"))
    if amount is None:
        return None

    # Currency precedence: explicit ISO code on the same line > symbol >
    # global ISO code anywhere in the body > USD default.
    code = chosen.groupdict().get("code")
    symbol = chosen.groupdict().get("symbol")
    if not code:
        # Look on the same line as the match.
        line_start = body.rfind("\n", 0, chosen.start()) + 1
        line_end = body.find("\n", chosen.end())
        if line_end == -1:
            line_end = len(body)
        line = body[line_start:line_end]
        line_code = _CURRENCY_CODE_RE.search(line)
        if line_code:
            code = line_code.group(1)
    if not code and symbol:
        code = _CURRENCY_SYMBOL_TO_CODE.get(symbol)
    if not code:
        # Body-wide ISO scan as a last resort.
        body_code = _CURRENCY_CODE_RE.search(body)
        if body_code:
            code = body_code.group(1)
    if not code:
        code = "USD"

    return {"amount": amount, "currency": code}


# ---------------------------------------------------------------------------
# Tracking URL
# ---------------------------------------------------------------------------

# Carrier domains and known third-party tracking platforms. Anchored
# permissively so per-account subdomains (e.g. shop.aftership.com,
# track.route.com) all match.
_TRACKING_URL_RE = re.compile(
    r"""https?://[^\s<>"')]*?
        (?:
            ups\.com | fedex\.com | usps\.com | dhl\.com | dhl\.de |
            canadapost(?:-postescanada)?\.ca | royalmail\.com |
            australiapost\.com\.au | nzpost\.co\.nz |
            aftership\.com | shipstation\.com | shipbob\.com |
            route\.com | narvar\.com | parcel\.app | parcelsapp\.com |
            17track\.net | easypost\.com | trackingmore\.com |
            dpd\.co\.uk | hermesworld\.com | gls-group\.eu |
            shopify\.com/.*?track | shop\.app/track
        )
        [^\s<>"')]*
    """,
    re.I | re.X,
)
# Carrier tracking-number patterns — for emails where the URL is missing
# but the body shows the bare number (the user can copy-paste it).
_TRACKING_NUMBER_RE = re.compile(
    r"\b("
    r"1Z[0-9A-Z]{16}|"                            # UPS
    r"\d{12}|\d{15}|\d{20}|\d{22}|"               # FedEx / USPS / DHL
    r"[A-Z]{2}\d{9}[A-Z]{2}"                      # USPS international
    r")\b"
)


def extract_tracking_url(body: str) -> str | None:
    """Return the first carrier/tracking URL in the body, or None.

    Prefers explicit URLs over bare tracking numbers because URLs land
    you directly on the carrier's tracking page without copy-paste. If
    no URL matches but a bare tracking number does, we still return None
    — bare numbers without a carrier hint aren't very useful and dirty
    the schema. (Could be added later if it becomes a real need.)
    """
    if not body:
        return None
    m = _TRACKING_URL_RE.search(body)
    if not m:
        return None
    # Trim trailing punctuation that may have come from a sentence.
    url = m.group(0).rstrip(".,;:!?)")
    return url


# ---------------------------------------------------------------------------
# Order ↔ shipment linking (moved out of order_scan.py for symmetry)
# ---------------------------------------------------------------------------

# Match "Order #ABC123" / "Order number: 1234" / "Order id 99887" — used
# to link a shipping email back to its original order email when both
# bodies mention the same number.
_ORDER_NUMBER_RE = re.compile(
    r"order\s*(?:#|number|id|no\.?)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-]{3,})",
    re.I,
)


def extract_order_number(body: str) -> str | None:
    """Return the first order-number string in the body, or None.

    Looks for "Order #..." / "Order number: ..." / "Order ID ..." with a
    minimum 4-character alphanumeric suffix to filter out false matches
    like "Order #1" or "Order: us" headings. Used to link shipping
    emails back to their original order.
    """
    if not body:
        return None
    m = _ORDER_NUMBER_RE.search(body)
    if not m:
        return None
    return m.group(1).upper()
