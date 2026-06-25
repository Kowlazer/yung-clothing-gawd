"""Wardrobe scanner — Gmail order/shipping confirmations -> catalogue.

Manual entry point — not part of the daily cron. Scans Gmail for order
confirmation and shipping confirmation emails, extracts purchased items
via Claude, then walks the user through (a) approving which watchlist
entries to mark as purchased and (b) recording fit/sizing reviews.

The deterministic 90% (IMAP fetch, subject/sender heuristic
classification, fuzzy watchlist matching, interactive prompts) lives
here. Claude is used **only** for structured extraction of the unruly
parts (item name, size, color, price) — see ``src/order_extract.py``.

Usage::

    python -m src.order_scan                  # full flow: scan + both interactive passes
    python -m src.order_scan --scan-only      # scan + persist; no prompts
    python -m src.order_scan --no-scan        # both interactive passes; no Gmail fetch
    python -m src.order_scan --match-watchlist  # ONLY watchlist approval pass (no scan, no fits)
    python -m src.order_scan --review-fits    # ONLY fit-review pass (no scan, no watchlist)
    python -m src.order_scan --classify       # stamp garment category on items (issue #18)
    python -m src.order_scan --reharvest-urls # backfill product_url on old items (issue #23)
    python -m src.order_scan --since 2023-01-01
    python -m src.order_scan --shop "Norse Projects"
    python -m src.order_scan --reprocess "Fabletics"  # recover a burned shop (see below)
    python -m src.order_scan --dry-run        # also honors SALE_CHECK_DRY_RUN

State persists in ``wardrobe.json`` (9th Gist file). Three sections:

  * ``items``: list of purchased-item dicts (see schema below)
  * ``scan_state``: ``{last_scanned_at, processed_email_ids}`` — used to
    skip already-seen emails on subsequent runs. NOT pruned (scans are
    infrequent; dedupe must survive years).
  * ``watchlist_exclusions``: lines the user approved as "remove from the
    Google Doc". Stored for future automation; the script also prints
    them each run so the user can paste them straight into the Doc.

Item schema::

    {
      "id": str,                          # sha256(email_id + index)[:12]
      "shop": str,
      "shop_domain": str,
      "item_name": str,
      "size": str | null,
      "color": str | null,
      "qty": int,
      "price_paid": {"amount": float, "currency": str} | null,
      "purchased_at": "YYYY-MM-DD",
      "order_email_id": str,
      "shipping_email_id": str | null,
      "shipped_at": "YYYY-MM-DD" | null,
      "tracking_url": str | null,
      "fit_review": {                     # null until reviewed. Only `fit` is
                                          # required; everything else is optional
                                          # (a "quick review" is just `fit`).
          "fit": "too_small"|"small"|"tts"|"large"|"too_large",  # 5-point scale;
                                          # legacy "small"/"large"/"tts" still valid.
          "areas": {                      # optional per-region detail, any subset:
              "length": "short"|"good"|"long",
              "shoulders_chest": "tight"|"good"|"loose",
              "sleeves": "short"|"good"|"long",
              "sleeve_opening": "tight"|"good"|"wide",
              "waist_hips": "tight"|"good"|"loose",
              "inseam": "short"|"good"|"long"},
          "inseam_inches": float | null,  # numeric inseam, pants
          "next_time": "size_down"|"same"|"size_up"|"buy_again"|"avoid" | null,
          "verdict": "keep"|"return"|"tailor" | null,
          "notes": str,
          "reviewed_at": ISO,
          "source": "web"|"cli",          # provenance
          "body_comp_summary": {          # compact body state at review time,
              "weight_kg": float, "body_fat_pct": float,   # filled by the Phase B
              "lean_mass_kg": float, "fat_mass_kg": float, # backfill (matched to
              "scan_date": "YYYY-MM-DD", "matched_to": "fit_review",
              "matched_date": "YYYY-MM-DD", "days_from_event": int} | absent
      } | null,
      # Sentinel: fit_review={"fit": "dropped", ...} marks a mis-extracted /
      # not-clothing item so it's excluded from pending without re-prompting.
      "category": "hoodie",               # durable garment-type key from
                                          # src/wardrobe_categories.py (issue #18);
                                          # absent until classified at scan time or
                                          # by --classify. non_clothing = sentinel.
      "is_clothing": false,               # absent (treat as true) unless matched
                                          # against a Non-clothing section line, or
                                          # derived False from category=non_clothing
      "watchlist_match": {"matched_line": str,
                          "approved_for_removal": bool | null,
                          "is_clothing": bool} | null,
      "body_comp": {                      # absent until --backfill-bodycomp runs.
          "result_id": str,               # BodySpec DEXA scan nearest the event,
          "scan_date": "YYYY-MM-DD",      # capped at --max-gap-days (default 90).
          "matched_to": "purchase"|"fit_review",  # which event date it matched
          "matched_date": "YYYY-MM-DD",   # purchased_at (or reviewed_at)
          "days_from_event": int,         # signed; negative = scan before event
          "weight_kg": float, "body_fat_pct": float, "tissue_fat_pct": float,
          "lean_mass_kg": float, "fat_mass_kg": float, "bone_mass_kg": float,
          "android_gynoid_ratio": float | null,
          "regions": {region: {fat_mass_kg, lean_mass_kg, bone_mass_kg,
                               total_mass_kg, tissue_fat_pct, region_fat_pct}},
                                          # arms/legs/trunk/android/gynoid
          "fetched_at": ISO} | absent,    # see src/bodyspec.py
      "body_comp_at_purchase": {...} | absent  # the original purchase-time
                                          # body_comp, preserved when body_comp
                                          # is re-matched to reviewed_at (Phase B).
    }

A top-level ``shop_fit_notes`` map ({shop_name: free-text note}) lives alongside
``items``/``scan_state``/``watchlist_exclusions`` — per-shop sizing reminders
("Toka: buy XL sweatshirts") surfaced in the fit form and the CLI walk.
"""
from __future__ import annotations

import argparse
import email
import hashlib
import imaplib
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlparse, urlunparse

from src import bodyspec, order_classify
from src.classify import _NON_CLOTHING_HEADER_RE
from src.config import Config, load_config
from src.wardrobe_categories import NON_CLOTHING, normalise_category
from src.fit_links import pending_fit_items
from src.gmail import (
    _FETCH_META_RE,
    _connect,
    _extract_body_text,
    _header,
    _parse_fetch_response,
    _parse_message,
)
from src.order_parse import (
    extract_order_number,
    extract_total,
    extract_tracking_url,
    is_excluded_shop,
    resolve_shop,
    sender_domain,
)
from src.state import read_state, write_state
from src.watchlist import fetch_watchlist

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

_IMAP_MAX_MESSAGES = 10000         # ample headroom for a 5-year first run
# Body excerpt size. Larger than gmail.py's 1500 (order receipts legitimately
# need their item lists) but smaller than the initial 4000 — 30K input-tokens-
# per-minute Anthropic tier means each token counts. 2000 chars ≈ 500 tokens
# per email and still captures the items-table region of every templated
# Shopify / Amazon / WooCommerce receipt seen in testing.
# Body chars sent to Claude per order email. Generous because order_scan is a
# manual, infrequent command and a multi-item receipt (e.g. a 4-item Fabletics
# order) needs room — and because some shops bury the itemised ORDER SUMMARY
# after long decorative/emoji preambles (collapsed by _excerpt before this cap).
_BODY_EXCERPT_LIMIT = 6000
_DEFAULT_LOOKBACK_YEARS = 3

# Truthy values for SALE_CHECK_DRY_RUN (mirrors main.py)
_TRUTHY = frozenset({"1", "true", "yes", "on"})


# ---------------------------------------------------------------------------
# Heuristic classification
# ---------------------------------------------------------------------------

# Subject-line patterns. Order matters — `_SHIP_SUBJECT_RE` is checked
# first since "your order has shipped" matches both buckets.
_SHIP_SUBJECT_RE = re.compile(
    # Trailing \b dropped on purpose — `shipped:` (with the colon) was
    # never matching because `:` followed by quote/space/EOL doesn't sit
    # on a word boundary. Now `\bshipped\b` catches the bare word in
    # subjects like `Shipped: "Pants" and 4 more items`.
    #
    # 2026-05-24: broadened to catch BoxLunch/narvar-style delivery
    # updates ("order is arriving soon", "almost here", "is here",
    # "has been delivered"). Without these, shipping-update emails for
    # an existing order match `your order` in ORDER_SUBJECT_RE and get
    # extracted as duplicate orders.
    #
    # 2026-06-05: catch (a) COPPERTIST's "order #N hasshipped" — the missing
    # space defeated `has\s+shipped`, so `\s*` now allows zero space; (b)
    # Fabletics' pre-ship nudge "Your Order Is Almost on Its Way" (no tracking
    # yet → falls to "other"); (c) "shipping label … has been created"
    # notifications (Hello Oregano). All three were tripping ORDER_SUBJECT_RE
    # via "order #N" and getting re-extracted as duplicate, price-less orders.
    r"\b("
    r"has\s*(?:been\s+)?shipped|"      # also "has been shipped" / "hasshipped"
    r"was\s+shipped|"
    r"has\s+(?:been\s+)?delivered|"
    r"is\s+on\s+(?:its|the)\s+way|"
    r"almost\s+on\s+(?:its|the)\s+way|"  # Fabletics pre-ship nudge
    r"on\s+the\s+way|"
    r"shipping\s+label|"               # "shipping label … has been created"
    r"is\s+arriving|"
    r"arriving\s+(?:soon|today|tomorrow)|"
    r"has\s+arrived|"                  # delivery: "Your order has arrived!"
    r"out\s+for\s+delivery|"
    r"is\s+(?:almost\s+)?here|"        # delivered: "order is here", "almost here"
    r"almost\s+here|"
    # 2026-05-28: Shopify's standard fulfillment subject is
    # "Shipping update for order #N" — the bare participle "shipping"
    # wasn't matched (only "shipped"/"shipment"), so these fell through to
    # ORDER_SUBJECT_RE ("order #N") and got re-extracted as duplicate
    # orders. "winging its way" is one shop's shipping-nudge phrasing.
    r"shipping\s+update|"
    r"winging\s+its\s+way|"
    r"shipped|"                         # bare "shipped" (incl. "Shipped:")
    r"shipment|"
    r"tracking|"
    r"delivered"
    r")",
    re.I,
)
_ORDER_SUBJECT_RE = re.compile(
    r"\b("
    r"order\s+confirmation|"
    r"your\s+order|"
    r"thanks?\s+(you\s+)?for\s+your\s+(order|purchase)|"
    r"order\s+received|"
    r"we\s+(got|received)\s+your\s+order|"
    r"order\s+#|"
    r"receipt|"
    r"purchase\s+confirmation|"
    r"ordered:|"                        # Amazon: 'Ordered: "PROGO Men's Joggers"…'
    r"order\s+placed|"
    r"new\s+order"
    r")",
    re.I,
)

_FROM_DOMAIN_RE = re.compile(r"@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")

# Body markers that confirm an email is a real order receipt. Required for
# the "order" bucket — subject lines like "your order" or "thanks for your
# order" appear all over post-purchase marketing ("how was your order?",
# "rate your recent purchase", "your order is arriving soon"). The body
# signal cuts ~90% of those false positives.
_BODY_ORDER_MARKER_RE = re.compile(
    r"(order\s+#\s*\w+|order\s+number[:\s]|"
    r"order\s+total[:\s]|subtotal[:\s]|"
    r"order\s+summary|items?\s+ordered|"
    r"your\s+items?[:\s]|"
    # "We('ve)? (have )?received your order" — H&M's order-confirmation
    # bodies (us@delivery.hm.com) carry an itemised list + prices but none
    # of the conventional headers above, so they fell through to "other".
    # Safe: shipping is classified first (ship subject wins), and the
    # marketing / non-receipt gates run ahead of the order branch.
    r"we(?:'ve|\s+have)?\s+received\s+your\s+order)",
    re.I,
)

# Transit phrases that prove a parcel is in motion. Paired with a STRICT ship
# marker (_BODY_SHIP_STRICT_RE) in the body, these reclassify an email as
# shipping even when its subject only tripped the *order* regex. The trigger
# case: CatGirlRiot's dispatch email "🛫 Your shipping arc begins … | Order
# #7663" — "shipping" (in "shipping arc") isn't in _SHIP_SUBJECT_RE, so the
# bare "Order #" won the subject match and the live tracking number got
# re-extracted as a duplicate order (no prices, since dispatch emails don't
# itemise money). An *original* receipt never carries both a transit phrase
# and a real tracking number, so requiring both keeps combined
# "order confirmed + here's your tracking" emails (rare) classified as orders
# and still extracted.
_BODY_SHIP_TRANSIT_RE = re.compile(
    r"\b("
    r"on\s+(?:its|the)\s+way|"
    r"out\s+for\s+delivery|"
    r"(?:has\s+been|has|was|is)\s+shipped|"
    r"(?:your\s+)?order\s+shipped|"
    r"has\s+(?:been\s+)?dispatched|"
    r"shipping\s+arc"                  # CatGirlRiot's dispatch phrasing
    r")\b",
    re.I,
)

# STRICT ship markers — an explicit tracking-number label, a "track your
# package/shipment" phrase, or a real *carrier* tracking URL. Used only by the
# order-subject override below (paired with a transit phrase). Deliberately
# omits the loose long-digit / 1Z / 2-letter-prefix patterns in
# _BODY_SHIP_MARKER_RE: those also match order numbers, SKUs and a shop's own
# order-view URL (e.g. suzushiiclothing.com/<digits>/orders/<hex>), which would
# wrongly flip a genuine "Your Order Is Confirmed" receipt to shipping. The
# looser _BODY_SHIP_MARKER_RE stays in use for the ship-*subject* branch, where
# the subject has already proven shipping intent.
_BODY_SHIP_STRICT_RE = re.compile(
    r"(tracking\s+(?:number|#|code|id)\b|"
    r"track\s+your\s+(?:package|shipment)\b|"
    # Carrier tracking URL — the carrier must be a domain label in the host
    # (anchored before the first path slash via [^/\s]*) so a carrier token
    # that happens to appear inside a base64 query key can't false-match a
    # shop's own order-view link (e.g. suzushiiclothing.com/.../authenticate
    # ?key=<base64 containing 'ups'>).
    r"https?://(?:[^/\s]*\.)?(?:ups|fedex|usps|dhl|canadapost|aftership|"
    r"shipbob|shipstation|easypost|narvar|route|shipglobal|ontrac|"
    r"lasership|17track|parcelsapp|goshippo)\.[a-z]{2,})",
    re.I,
)

# Explicit past-tense shipment phrasing — every alternative asserts the parcel
# has ALREADY shipped, language an original order receipt never uses (a receipt
# says items "will ship" / "as soon as your order ships"). Paired with a ship
# marker in _classify, this reclassifies Gap-family "Ship Notification" emails —
# whose subject is the generic "An update to your order #N" and whose carrier
# link is dropped during HTML->text — as shipping. Without it they re-list the
# full order summary and re-extract as a duplicate order.
#
# Shape: a shipment noun (item/order/package/parcel/shipment, optional "your")
# + a perfect/passive auxiliary (has/have/was/were) + an optional "been" +
# shipped/sent/dispatched. Issue #13 broadened this from the original
# "(has|have) shipped" so the "have been shipped" / "were sent" / partial-
# shipment variants (e.g. Old Navy #1LCBWLK) also hit. The auxiliary +
# past-participle requirement keeps it genuinely past-tense: an original
# confirmation ("as soon as your order ships", "your order has been *received*",
# "is on the way") never matches, so pairing it with the loose
# _BODY_SHIP_MARKER_RE can't flip a real receipt to shipping.
_BODY_SHIPPED_PAST_RE = re.compile(
    r"\b(?:your\s+)?(?:items?|orders?|packages?|parcels?|shipments?)"
    r"\s+(?:has|have|was|were)\s+(?:been\s+)?(?:shipped|sent|dispatched)\b",
    re.I,
)

# Body markers that confirm a real shipping notification — tracking number
# or carrier link. Without one, "shipped" / "tracking" / "delivered"
# subject lines are almost always marketing nudges.
_BODY_SHIP_MARKER_RE = re.compile(
    r"(tracking\s+(?:number|#|code|id)|"
    r"track\s+your\s+(?:package|order|shipment)|"
    r"https?://\S*(?:ups|fedex|usps|dhl|canadapost|aftership|shipbob|"
    r"shipstation|easypost|narvar|route\.com|shipglobal\.in|"
    r"17track|parcelsapp|shippo)\S+|"
    r"amazon\.com/(?:progress-tracker|gp/your-account/order-details)|"
    r"\b1Z[0-9A-Z]{16}\b|"               # UPS tracking pattern
    r"\b\d{12,22}\b|"                    # USPS/FedEx-style long digit IDs
    r"\b[A-Z]{2}\d{9,18}\b)",            # 2-letter prefix + digits (intl: SG/RR/etc.)
    re.I,
)

# Senders we always ignore — these are common false positives the subject
# regex would otherwise catch.
_IGNORE_SENDER_RE = re.compile(
    r"(noreply@(?:linkedin|twitter|x|facebook|meta|instagram|google|github)\.com|"
    r"@(?:mailer-daemon|paypal\.com))",
    re.I,
)

# Markers that prove an email is NOT an original order confirmation, even
# though it may contain an order summary (item list, subtotal, total) that
# would otherwise pass _BODY_ORDER_MARKER_RE. Two categories:
#
#   1. Payment failures / cancellations. The shop sends a "your order #X
#      was received" email that gets cancelled minutes later when the card
#      declines — both emails have the same item list, but only the
#      successful one represents a real purchase. Without this filter we
#      double-count (or worse).
#   2. Post-order status updates. Shopify-themed shops send "A note has
#      been added to your order" or "Your order has been updated" emails
#      that re-include the entire order summary. These create N copies of
#      every item per order if not filtered.
#
# Checked against subject AND first 2KB of body. Wins over both order and
# shipping classification (return "other" outright).
_NON_RECEIPT_MARKERS_RE = re.compile(
    r"\b("
    # Payment failures / cancellations
    r"payment\s+(?:failed|declined|was\s+not\s+completed|was\s+not\s+received|"
        r"unsuccessful|could\s+not\s+be\s+(?:processed|completed))|"
    r"unable\s+to\s+(?:process|complete)\s+(?:your\s+)?payment|"
    r"(?:your\s+)?(?:card|payment)\s+(?:was\s+)?declined|"
    r"transaction\s+(?:failed|declined)|"
    r"order\s+(?:has\s+been|was)\s+cancell?ed|"
    r"successful\s+cancellation|"            # Amazon: "Successful cancellation of 1 item…"
    r"cancellation\s+of\s+\d+\s+items?|"
    r"didn'?t\s+go\s+through|"
    r"could\s+not\s+(?:process|complete|charge)\s+your|"
    # Post-order status updates (re-confirmations w/ full order summary)
    r"(?:a\s+)?note\s+has\s+been\s+added\s+to\s+your\s+order|"
    r"(?:a\s+)?note\s+has\s+been\s+added|"
    r"an?\s+update\s+(?:has\s+been\s+)?added\s+to\s+your\s+order|"
    r"order\s+has\s+been\s+updated|"
    # SparkTrendz "ACTION REQUIRED - Update Regarding Your Order #N" — a
    # post-order status email (OOS/substitution) that re-lists the summary.
    r"update\s+regarding\s+your\s+order|"
    # Shopify "Order #N updated" + eBay "Order update:" re-list the order
    r"order\s+(?:#\s*\S+\s+)?update(?:d)?"
    r")\b",
    re.I,
)

# Subjects/bodies that signal an email is post-purchase marketing rather
# than the original receipt or shipment notification.
_POST_PURCHASE_MARKETING_RE = re.compile(
    r"\b("
    r"how\s+(was|did)\s+(your|the)|"
    r"rate\s+your|leave\s+a\s+review|review\s+your\s+(order|purchase)|"
    r"share\s+your\s+experience|"
    r"come\s+back|miss\s+you|haven't\s+seen|"
    r"refer\s+a\s+friend|invite\s+your\s+friends|"
    r"loyalty\s+points|reward(s)?\s+update|"
    r"happy\s+birthday|anniversary"
    r")\b",
    re.I,
)


def _classify(em: dict) -> str:
    """Return ``"order"`` | ``"shipping"`` | ``"other"`` for one parsed email.

    Heuristics-first per design — Claude is only invoked for extraction
    of confirmed orders/shipments, not for classification.

    Rules:
      1. Hard sender blocklist (LinkedIn, PayPal, etc.) → other.
      2. Non-receipt markers (payment failure, "note has been added to
         your order", "order has been updated") → other. These emails
         carry the original order summary but don't represent a real
         purchase event; without this filter we double-count.
      3. Post-purchase-marketing language anywhere in subject or first 2KB
         of body ("rate your order", "miss you") → other.
      3b. Body-proven shipment (hard tracking marker + in-transit phrase) →
         shipping, even if the subject only tripped the order regex. Stops
         cutesy dispatch subjects ("Your shipping arc begins … | Order #N")
         from being re-extracted as duplicate orders.
      3c. Past-tense shipment ("N items have shipped" + a tracking number) →
         shipping. Catches Gap-family "Ship Notification" emails (subject "An
         update to your order #N") whose carrier link was stripped in HTML->text
         and which re-list the order summary — same duplicate-order risk as 3b.
      4. Subject must match the shipping or order regex (shipping wins
         when both match — "your order has shipped" is shipping).
      5. Order: body MUST contain an order-receipt marker (order #,
         subtotal, order summary, items ordered). Subject alone is too
         leaky — "Your order" appears in dozens of post-purchase
         marketing variants.
      6. Shipping: body MUST contain a tracking marker (tracking number,
         carrier URL, UPS/USPS-style ID). Same reason as orders.
    """
    subject = em.get("subject") or ""
    sender = em.get("from") or ""
    body = em.get("body_text") or ""
    # Only the head of the body is worth inspecting for marketing-language
    # filters; promo footers often contain "leave a review" links even on
    # legit receipts.
    body_head = body[:2000]

    if _IGNORE_SENDER_RE.search(sender):
        return "other"

    # Non-receipt markers (payment failure, order-status update) override
    # everything else — these emails carry order summaries but don't
    # represent a real purchase event.
    if _NON_RECEIPT_MARKERS_RE.search(subject):
        return "other"
    if _NON_RECEIPT_MARKERS_RE.search(body_head):
        return "other"

    if _POST_PURCHASE_MARKETING_RE.search(subject):
        return "other"
    if _POST_PURCHASE_MARKETING_RE.search(body_head):
        return "other"

    # Body-proven shipment overrides an order-subject false positive. A STRICT
    # tracking marker plus an in-transit phrase means the parcel has shipped;
    # an original receipt has neither. Catches dispatch emails whose cutesy
    # subject ("Your shipping arc begins … | Order #N") trips _ORDER_SUBJECT_RE
    # via the bare "Order #" instead of _SHIP_SUBJECT_RE — they were being
    # re-extracted as duplicate, price-less orders. The strict marker (not the
    # looser _BODY_SHIP_MARKER_RE) keeps a genuine confirmation whose body only
    # mentions a future "as soon as your order is on the way" + a shop order URL
    # classified as an order.
    if _BODY_SHIP_STRICT_RE.search(body) and _BODY_SHIP_TRANSIT_RE.search(body_head):
        return "shipping"

    # 3c. Past-tense shipment whose carrier link was stripped during HTML->text.
    # A body that explicitly says items "have shipped" + carries a tracking
    # number is a shipment, not a receipt — Gap-family "Ship Notification" emails
    # (subject "An update to your order #N") re-list the whole order summary and
    # would otherwise re-extract as a duplicate of the original confirmation.
    # The past-tense phrase never appears on an original receipt, so pairing it
    # with the looser _BODY_SHIP_MARKER_RE (the strict carrier-URL / "tracking
    # number" markers don't survive HTML->text here) is safe.
    if _BODY_SHIPPED_PAST_RE.search(body_head) and _BODY_SHIP_MARKER_RE.search(body):
        return "shipping"

    if _SHIP_SUBJECT_RE.search(subject):
        if _BODY_SHIP_MARKER_RE.search(body):
            return "shipping"
        return "other"

    if _ORDER_SUBJECT_RE.search(subject):
        if _BODY_ORDER_MARKER_RE.search(body):
            return "order"
        return "other"

    # Subject didn't trip either bucket — fall back to body markers for
    # the order case (handles oddly-subjected receipts like "Hi from us!").
    if _BODY_ORDER_MARKER_RE.search(body):
        return "order"

    return "other"


# ---------------------------------------------------------------------------
# IMAP fetch
# ---------------------------------------------------------------------------

# Order/shipping subject markers — the coarse Gmail pre-filter. Must cover the
# actual shop subject patterns in the wild (Amazon's `Ordered:` / `Shipped:` /
# `Delivered:` prefixes look nothing like the conventional "Your order" wording).
# Missing a pattern means those emails are never fetched. Shared by _build_query
# (forward scans) and _unskip_matching (the --reprocess recovery search).
_SUBJECT_MARKERS = (
    'subject:("order confirmation" OR "your order" OR '
    '"thanks for your order" OR "thank you for your order" OR '
    '"order received" OR "we got your order" OR "order #" OR '
    '"has shipped" OR "is on its way" OR "out for delivery" OR '
    '"tracking" OR "shipment" OR "shipped:" OR "purchase confirmation" OR '
    '"ordered:" OR "delivered:" OR "order placed" OR "new order")'
)


def _build_query(since: datetime) -> str:
    """Construct the X-GM-RAW query string.

    Restricts to Primary + Updates + Promotions categories (the user-confirmed
    Gmail scope) and to the order/shipping subject markers. Heuristics still run
    on each match — the query is a coarse pre-filter, not the final classifier.
    """
    after = since.strftime("%Y/%m/%d")
    return (
        '(category:primary OR category:updates OR category:promotions) '
        f'after:{after} '
        f'({_SUBJECT_MARKERS})'
    )


def _unskip_matching(
    cfg: Config,
    wardrobe: dict,
    term: str,
    *,
    imap_client: imaplib.IMAP4 | None = None,
) -> int:
    """Drop a shop's order/shipping emails from the processed (skip) set.

    Searches Gmail for ``(term)`` ANDed with the order/shipping subject markers
    (``_SUBJECT_MARKERS``), collects the matching ``X-GM-MSGID``s, and removes
    them from ``wardrobe["scan_state"]["processed_email_ids"]`` so a following
    scan re-fetches and re-extracts them. Returns the number removed.

    This is the recovery half of ``--reprocess``: order emails that earlier
    ``--shop`` / ``--max-emails`` runs marked processed without ever extracting
    (the "burn") are otherwise skipped forever (see ``_run_scan``).
    """
    query = f'({term}) ({_SUBJECT_MARKERS})'
    own_client = imap_client is None
    client = imap_client or _connect(cfg.gmail_username, cfg.gmail_app_password)
    matched_ids: set[str] = set()
    try:
        client.select("INBOX", readonly=True)
        escaped = query.replace("\\", "\\\\").replace('"', '\\"')
        typ, data = client.uid("SEARCH", "X-GM-RAW", f'"{escaped}"')
        if typ != "OK" or not data or not data[0]:
            return 0
        for uid in data[0].split():
            try:
                typ, msg_data = client.uid("FETCH", uid, "(X-GM-MSGID)")
            except imaplib.IMAP4.error:
                continue
            if typ != "OK":
                continue
            for item in msg_data or []:
                blob = item[0] if isinstance(item, tuple) else item
                m = _FETCH_META_RE.search(blob or b"")
                if m:
                    matched_ids.add(m.group(1).decode("ascii"))
                    break
    finally:
        if own_client:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass

    processed = wardrobe["scan_state"].get("processed_email_ids", {})
    removed = 0
    for eid in matched_ids:
        if eid in processed:
            del processed[eid]
            removed += 1
    return removed


def _fetch_emails(
    cfg: Config,
    since: datetime,
    skip_ids: set[str],
    *,
    imap_client: imaplib.IMAP4 | None = None,
    max_messages: int = _IMAP_MAX_MESSAGES,
) -> list[dict]:
    """Fetch order/shipping-candidate emails from Gmail via IMAP.

    Returns a list of ``{id, from, subject, snippet, body_text, date}`` dicts
    (same shape as ``gmail.fetch_promotions``). Skips any id in ``skip_ids``.
    """
    query = _build_query(since)
    log.info("order_scan: gmail query: %s", query)
    own_client = imap_client is None
    client = imap_client or _connect(cfg.gmail_username, cfg.gmail_app_password)
    try:
        client.select("INBOX", readonly=True)
        # X-GM-RAW lets us pass Gmail-search-syntax directly. The query
        # itself contains quoted phrases (e.g. subject:"has shipped"), so we
        # must backslash-escape those inner quotes before wrapping the whole
        # thing in IMAP quoted-string delimiters.
        escaped = query.replace("\\", "\\\\").replace('"', '\\"')
        typ, data = client.uid("SEARCH", "X-GM-RAW", f'"{escaped}"')
        if typ != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()
        if not uids:
            return []
        uids = uids[-max_messages:]
        log.info("order_scan: %d candidate emails to fetch", len(uids))
        out: list[dict] = []
        for uid in uids:
            try:
                typ, msg_data = client.uid(
                    "FETCH", uid, "(X-GM-MSGID BODY.PEEK[])"
                )
            except imaplib.IMAP4.error as exc:
                log.info("order_scan: fetch uid %s failed: %s", uid, exc)
                continue
            if typ != "OK":
                continue
            parsed = _parse_fetch_response(msg_data)
            if not parsed:
                continue
            gm_msgid, raw_message = parsed
            if gm_msgid in skip_ids:
                continue
            try:
                msg = email.message_from_bytes(raw_message)
            except Exception as exc:  # noqa: BLE001 — defensive
                log.info("order_scan: parse uid %s failed: %s", uid, exc)
                continue
            parsed_em = _parse_message(gm_msgid, msg)
            # Per-item product links survive nowhere else — `_html_to_text`
            # strips hrefs — so harvest them off the raw HTML here, before the
            # message is discarded. Filtered to the shop domain + matched to
            # items downstream (see _match_product_url).
            parsed_em["product_links"] = _harvest_anchor_urls(msg)
            out.append(parsed_em)
        return out
    finally:
        if own_client:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass


# Runs of emoji / decorative symbols used as email spacers. Marketing-heavy
# order emails (e.g. Fabletics) pad hundreds of these before the itemised ORDER
# SUMMARY; left in, they'd eat the whole excerpt budget and Claude would see no
# items. Collapsed to a single space. Isolated accented letters in product names
# are outside these ranges, so brand names survive.
# (lo, hi) Unicode code-point ranges of emoji / decorative spacer glyphs.
# Kept as hex + chr() so the source stays pure ASCII (no literal emoji or
# invisible joiners/selectors in the file).
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # emoji, pictographs, flags, symbol supplements
    (0x2190, 0x21FF),    # arrows
    (0x2300, 0x23FF),    # misc technical (watch, alarm clock)
    (0x2460, 0x24FF),    # enclosed alphanumerics
    (0x2500, 0x27BF),    # box drawing -> geometric -> misc symbols -> dingbats
    (0x2900, 0x297F),    # supplemental arrows-B
    (0x2B00, 0x2BFF),    # misc symbols & arrows (star)
    (0xFE00, 0xFE0F),    # variation selectors
    (0x200D, 0x200D),    # zero-width joiner
)
_EMOJI_RUN_RE = re.compile(
    "[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in _EMOJI_RANGES) + "]+"
)


def _excerpt(text: str, limit: int = _BODY_EXCERPT_LIMIT) -> str:
    """Compress the body to a single-line excerpt that Claude can ingest.

    Collapses emoji/decorative spacer runs (see ``_EMOJI_RUN_RE``) then runs of
    whitespace to single spaces — Claude reconstructs structure fine, and this
    keeps the meaningful order content (item lines, ``ORDER SUMMARY``) inside the
    char budget even when a shop pads the email with long decorative preambles.
    """
    text = _EMOJI_RUN_RE.sub(" ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + " ...[truncated]"


# sender_domain lives in order_parse — re-exported via the import above as
# a module-level alias for callers/tests that previously used _sender_domain.
_sender_domain = sender_domain


# ---------------------------------------------------------------------------
# Watchlist matching
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset({
    # English fillers
    "the", "a", "an", "and", "or", "of", "with", "for", "in", "on", "to",
    "by", "at", "from", "buy", "your", "you", "us", "my",
    # Apparel categories — too broad to count as a match signal on their
    # own. Two items from the same shop both being "hoodies" or "tees"
    # tells us nothing about whether they're the same product.
    "shirt", "shirts", "pants", "pant", "tee", "tees", "tshirt", "tshirts",
    "hoodie", "hoodies", "sweater", "sweaters", "sweatshirt", "sweatshirts",
    "sweatpant", "sweatpants", "pullover", "pullovers",
    "crewneck", "crewnecks", "jacket", "jackets", "jogger", "joggers",
    "shorts", "short", "tank", "tanks", "polo", "polos",
    "cardigan", "cardigans", "vest", "vests", "coat", "coats",
    "beanie", "beanies", "hat", "hats", "cap", "caps",
    "sock", "socks", "scarf", "scarves", "glove", "gloves",
    "shoe", "shoes", "sneaker", "sneakers", "boot", "boots",
    "rug", "rugs", "blanket", "blankets", "mat", "mats", "pillow", "pillows",
    # Fit / cut / silhouette descriptors — almost every shop sells some
    # variant of these so they appear on both sides constantly. "baggy" /
    # "cargo" are cut/style words shared across a shop's whole bottoms range,
    # so a design-less generic name ("Baggy Cargo Unisex Pants") must not match
    # a specific design's URL on those alone (the distinguishing design token
    # was absent from the purchase email's generic item name).
    "oversize", "oversized", "fitted", "slim", "regular", "relaxed",
    "loose", "boxy", "baggy", "cargo", "crop", "cropped", "drop", "shoulder",
    "fit", "unisex", "mens", "womens", "kids",
    # Sizing words.
    "size", "color", "colour", "small", "medium", "large",
    "xs", "xl", "xxl", "xxxl",
    # Generic category descriptors that appear across many products.
    "premium", "exclusive", "limited", "edition", "version", "style",
    "anime", "manga", "graphic", "printed", "embroidered", "embroidery",
    # Style / aesthetic / origin descriptors. Same problem as
    # "anime"/"manga": appear across many products from the same shop
    # without distinguishing them.
    "streetwear", "japanese", "harajuku", "korean",
    # Fabric / finish descriptors. Common across many shops' product
    # lines — two "vintage washed" tees from the same shop is not a
    # match signal in itself.
    "vintage", "washed", "wash", "faded", "distressed", "knit", "knitted",
    "woven", "fleece", "cotton", "linen", "denim", "leather",
    # URL / web-page noise that pollutes Jaccard math when watchlist
    # lines are product URLs.
    "https", "http", "www", "com", "net", "org", "io", "co", "store",
    "shop", "shops", "product", "products", "collections", "collection",
    "item", "items", "cart", "checkout", "page",
})

# Minimum shop-name length before we'll use it as a substring match
# signal. Single- and two-letter shop names ("T", "S", "On") generate
# huge numbers of false-positive matches because their lowercased form
# appears in almost any watchlist line. Domain matches are exempt —
# they're inherently specific.
_MIN_SHOP_NAME_LEN = 3
# Require at least this many shared content tokens between the item
# name and the watchlist line. The expanded _STOPWORDS set (apparel
# categories + URL noise) is what actually keeps "same shop, different
# product" matches out — once filler tokens are stripped, character-
# themed apparel from different products overlaps at 0. Keeping this
# at 1 lets slug-literal matches survive when the item name reduces to
# a single content token (e.g. "Sukuna Oversize Tee" → {sukuna}).
_MIN_ITEM_OVERLAP = 1
# Final acceptance threshold. 0.2 ≈ 1 shared meaningful token out of
# ~5 unique terms — captures real product matches while rejecting
# "same shop, different product" noise.
_JACCARD_THRESHOLD = 0.2


def _tokens(text: str) -> set[str]:
    return {
        t for t in _TOKEN_RE.findall((text or "").lower())
        if t not in _STOPWORDS and len(t) > 1
    }


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


_URL_RE = re.compile(r"https?://[^\s)>\]]+", re.IGNORECASE)

# Subset of _STOPWORDS that are specifically *garment categories* (as
# opposed to fit/finish/style descriptors). When both an item name and
# a URL slug mention a garment category, the categories must overlap —
# "Bee Beanie" buying a /bee-hoodie URL is not the same product even
# though they share the design token "bee".
_APPAREL_CATEGORIES = frozenset({
    "shirt", "shirts", "tee", "tees", "tshirt", "tshirts",
    "hoodie", "hoodies", "sweater", "sweaters", "sweatshirt", "sweatshirts",
    "sweatpant", "sweatpants", "pullover", "pullovers",
    "crewneck", "crewnecks", "jacket", "jackets", "jogger", "joggers",
    "pants", "pant", "shorts", "short", "tank", "tanks", "polo", "polos",
    "cardigan", "cardigans", "vest", "vests", "coat", "coats",
    "beanie", "beanies", "hat", "hats", "cap", "caps",
    "sock", "socks", "scarf", "scarves", "glove", "gloves",
    "shoe", "shoes", "sneaker", "sneakers", "boot", "boots",
    "rug", "rugs", "blanket", "blankets", "mat", "mats", "pillow", "pillows",
})


def _garment_categories(text: str) -> set[str]:
    """Raw category tokens present in ``text`` (no stopword strip).

    Used to detect category-conflict on the slug acceptance path —
    e.g. a "tee" item shouldn't slug-match a URL ending in "-hoodie".
    """
    return {
        t for t in _TOKEN_RE.findall((text or "").lower())
        if t in _APPAREL_CATEGORIES
    }


def _slug_text(line: str) -> str:
    """Concatenation of every URL slug (last path segment) in ``line``,
    space-joined, with hyphens/underscores normalised to spaces.

    Shared helper between ``_slug_tokens`` and ``_garment_categories``
    over the slug — ensures both look at the same source text."""
    out: list[str] = []
    for url in _URL_RE.findall(line):
        path = urlparse(url).path
        parts = [p for p in path.split("/") if p]
        if not parts:
            continue
        out.append(parts[-1].replace("-", " ").replace("_", " "))
    return " ".join(out)


def _slug_tokens(line: str) -> set[str]:
    """Tokens from the last path segment of every URL in ``line``.

    The product slug (e.g. ``/products/sukuna-oversize-tee`` →
    ``sukuna-oversize-tee``) is a high-precision signal — the merchant
    deliberately put the product's name in the URL. We apply the same
    ``_STOPWORDS`` filter as item-name tokens so apparel-category
    fillers don't generate spurious overlaps (item ``{plus, ultra}``
    must not slug-match ``berserk-oversize-tee`` just because both
    sides have "tee").
    """
    return _tokens(_slug_text(line))


# ---------------------------------------------------------------------------
# Per-item product URLs (harvested from the order email's HTML anchors)
# ---------------------------------------------------------------------------
#
# ``_html_to_text`` strips every ``href`` before Claude (or anything else) sees
# the body, so the per-item product links exist nowhere downstream. We harvest
# them straight off the raw email HTML here and match each extracted item to the
# best candidate by slug-token overlap — the same high-precision signal the
# watchlist matcher leans on. The result is stamped as ``product_url`` so the
# wardrobe browser can offer a real "view product" link for new purchases.

# Paths that denote a single product page (vs. a collection / homepage / cart).
_PRODUCT_PATH_RE = re.compile(
    r"/(?:products?|dp|gp/product|gp/aw/d|listing|item)(?:/|$)", re.IGNORECASE,
)
# Cap on harvested links per email — order emails rarely link more than a
# handful of products; the rest is footer/cross-sell noise.
_MAX_HARVESTED_LINKS = 60
# A product URL must share at least this many content tokens with the item name.
_PRODUCT_URL_MIN_OVERLAP = 1
# Query-param names ESPs use to carry the real destination of a wrapped link.
_TRACKER_URL_PARAMS = ("url", "u", "destination", "redirect", "target", "ult")

# The only query params worth keeping on a stored product URL. Everything else —
# utm_*, click ids, and notably ``utm_contact`` (a base64 blob of the recipient's
# EMAIL) — is marketing tracking that must NOT be persisted or shown. ``variant``
# is kept because it selects the exact Shopify variant the item is.
_KEEP_QUERY_PARAMS = frozenset({"variant"})


def _clean_product_url(url: str | None) -> str | None:
    """Strip tracking cruft from a product URL before it's stored.

    Drops the fragment and every query param except ``_KEEP_QUERY_PARAMS`` —
    critically removing ``utm_contact`` (which carries the recipient's email) and
    the rest of the utm/click-id noise, so neither lands in ``wardrobe.json`` nor
    the "view product" link. Returns the input unchanged when there's nothing to
    clean; ``None`` passes through."""
    if not url:
        return url
    parsed = urlparse(url)
    kept = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() in _KEEP_QUERY_PARAMS
    ]
    return urlunparse(parsed._replace(query=urlencode(kept), fragment=""))


def _unwrap_tracking_url(href: str) -> str | None:
    """Recover the real destination from an email-service click-tracker link.

    Order/marketing emails almost always wrap links in an ESP click-tracker, so
    the anchor host is the tracker (``awstrack.me``, ``sendgrid``, …) and the
    product path never shows on it. **Transparent** trackers embed the real URL,
    percent-encoded, in a path segment (AWS SES: ``/L0/<enc-url>/1/<id>``) or a
    query param (``?url=<enc-url>``) — those we decode here. **Opaque** trackers
    (Klaviyo/Sendinblue/Mailchimp base64 blobs) carry no recoverable URL → return
    ``None`` (the item falls back to a search link). We decode rather than follow
    the redirect because these click tokens are often single-use / expired by the
    time we re-fetch an old order email, so a live GET 400s or bounces to the
    homepage."""
    if not href:
        return None
    parsed = urlparse(href)
    # 1) Destination carried in a query param.
    qs = parse_qs(parsed.query)
    for key in _TRACKER_URL_PARAMS:
        for val in qs.get(key, []):
            decoded = unquote(val)
            if decoded.lower().startswith(("http://", "https://")):
                return decoded
    # 2) Destination as an encoded path segment (AWS SES /L0/<enc-url>/...).
    for seg in parsed.path.split("/"):
        if not seg:
            continue
        decoded = unquote(seg)
        if decoded.lower().startswith(("http://", "https://")):
            return decoded
    return None


def _harvest_anchor_urls(msg: "email.message.Message") -> list[str]:
    """Absolute product-page URLs linked from an order email's HTML body.

    BeautifulSoup over every ``text/html`` MIME part; keeps ``<a href>`` values
    that are absolute http(s) URLs whose path looks like a single product page
    (``_PRODUCT_PATH_RE``). Order-preserving, deduped, capped. The fragment is
    dropped but the query is kept (it can carry a ``?variant=`` id). These are
    matched to extracted items in ``_match_product_url`` — host filtering to the
    shop domain happens later (the shop isn't resolved at fetch time)."""
    from bs4 import BeautifulSoup

    seen: set[str] = set()
    out: list[str] = []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_type() != "text/html":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            html = payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            html = payload.decode("utf-8", errors="replace")
        for anchor in BeautifulSoup(html, "lxml").find_all("a", href=True):
            href = (anchor["href"] or "").strip()
            if not href.lower().startswith(("http://", "https://")):
                continue
            # Either a direct product URL, or one unwrapped from a click-tracker
            # (most order-email links are ESP-wrapped — see _unwrap_tracking_url).
            url = href
            if not _PRODUCT_PATH_RE.search(urlparse(url).path):
                unwrapped = _unwrap_tracking_url(href)
                if not (unwrapped and _PRODUCT_PATH_RE.search(urlparse(unwrapped).path)):
                    continue
                url = unwrapped
            clean = _clean_product_url(url)
            if clean in seen:
                continue
            seen.add(clean)
            out.append(clean)
            if len(out) >= _MAX_HARVESTED_LINKS:
                return out
    return out


def _links_for_domain(urls: list[str], domain: str) -> list[str]:
    """Keep only the URLs hosted on the shop's own domain (or its Shopify
    backend).

    Drops cross-sell links to other stores, so an item is only ever linked to a
    page on the shop it was bought from. ``www.`` is normalised off both sides; a
    subdomain of the shop domain still counts. A ``*.myshopify.com`` host is also
    kept: a tracker-unwrapped Shopify link points at the store's myshopify
    backend (e.g. ``kingmnty.myshopify.com``) rather than its custom domain, and
    within one order email that backend is always the merchant's own — the
    subsequent slug-match + liveness check are the real precision gates."""
    domain = (domain or "").lower()
    if domain.startswith("www."):
        domain = domain[4:]
    out: list[str] = []
    for url in urls or []:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if not domain or host == domain or host.endswith("." + domain) \
                or host.endswith(".myshopify.com"):
            out.append(url)
    return out


def _match_product_url(item_name: str, candidates: list[str]) -> str | None:
    """Best product URL for an item, by slug-token overlap with its name.

    Mirrors the watchlist matcher's slug path: tokenise the item name and each
    candidate URL's slug (shared ``_STOPWORDS`` strip), score by shared-token
    count, and apply the same garment-category gate (a "tee" item won't match a
    "-hoodie" slug unless ≥2 design tokens overlap). Returns the single
    unambiguous best — a top score reached by exactly one candidate, overlap ≥
    ``_PRODUCT_URL_MIN_OVERLAP``; a tie at the top yields ``None`` so a
    multi-item order never mis-assigns a sibling's link (the browser then falls
    back to a search link)."""
    name_tokens = _tokens(item_name)
    if not name_tokens or not candidates:
        return None
    item_cats = _garment_categories(item_name)
    scored: list[tuple[int, str]] = []
    for url in candidates:
        overlap = len(name_tokens & _slug_tokens(url))
        if overlap < _PRODUCT_URL_MIN_OVERLAP:
            continue
        if item_cats:
            url_cats = _garment_categories(_slug_text(url))
            if url_cats and not (item_cats & url_cats) and overlap < 2:
                continue
        scored.append((overlap, url))
    if not scored:
        return None
    best = max(score for score, _ in scored)
    top = [url for score, url in scored if score == best]
    return top[0] if len(top) == 1 else None


# ---------------------------------------------------------------------------
# Liveness validation (for the re-harvest backfill — see _run_reharvest_urls)
# ---------------------------------------------------------------------------
#
# Products bought years ago are often discontinued, so a harvested URL may be
# dead. We never want to stamp a dead "view product" link — the browser's search
# fallback always works, a 404 doesn't. So the backfill validates each candidate
# live and only stamps the ones that still resolve to a product page.

# Realistic browser UA so a courtesy GET isn't refused out of hand. Mirrors
# extract._HEADERS without importing a private name across modules.
_VALIDATE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
_VALIDATE_TIMEOUT = 15.0
_VALIDATE_MAX_WORKERS = 8


def _resolve_if_live(url: str, client) -> str | None:
    """The canonical (post-redirect) URL if ``url`` is a live product page, else
    ``None``.

    Live = HTTP 200 **and** the *final* URL (after redirects) still looks like a
    product page (``_PRODUCT_PATH_RE``). Both common "gone" shapes therefore read
    as dead: a removed product that 404s (status check), and one that 301s to the
    homepage or a collection (the final path no longer matches). Returning the
    *final* URL means we stamp the shop's canonical link (e.g. a
    ``…myshopify.com`` link that redirects to the custom domain is stored as the
    custom-domain URL). Any network error ⇒ ``None`` — conservative, since a
    false "live" would stamp a broken link. (Soft-404s that 200 on the original
    URL aren't caught; that needs per-shop body parsing — a known limitation.)"""
    try:
        resp = client.get(url)
    except Exception:  # noqa: BLE001 — any fetch failure ⇒ treat as dead
        return None
    if resp.status_code != 200:
        return None
    final = str(resp.url)
    if not _PRODUCT_PATH_RE.search(urlparse(final).path):
        return None
    return _clean_product_url(final)


def _validate_urls(urls: list[str], *, client=None) -> dict[str, str | None]:
    """Concurrently resolve which of ``urls`` are live, mapping each input to its
    canonical final URL (or ``None`` if dead — see ``_resolve_if_live``).

    Per-host serialization + jitter mirrors the price extractor's courtesy
    policy, so re-validating a whole catalogue doesn't burst one shop and trip
    its bot detection. De-duplicates inputs; ``client`` is injectable for tests."""
    import random
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    import httpx

    uniq = list(dict.fromkeys(u for u in urls if u))
    if not uniq:
        return {}
    own = client is None
    client = client or httpx.Client(
        headers=_VALIDATE_HEADERS, timeout=_VALIDATE_TIMEOUT, follow_redirects=True,
    )
    host_locks: dict[str, threading.Lock] = {}
    guard = threading.Lock()

    def _host_lock(host: str) -> threading.Lock:
        with guard:
            return host_locks.setdefault(host, threading.Lock())

    def _check(url: str) -> tuple[str, str | None]:
        with _host_lock(urlparse(url).netloc.lower()):
            time.sleep(random.uniform(0.3, 0.9))
            return url, _resolve_if_live(url, client)

    try:
        with ThreadPoolExecutor(max_workers=_VALIDATE_MAX_WORKERS) as pool:
            return dict(pool.map(_check, uniq))
    finally:
        if own:
            client.close()


def _match_watchlist(items: list[dict], watchlist_text: str) -> None:
    """For each item, set ``item["watchlist_match"]`` in place.

    Match policy:
      * Shop-link required: the line must contain the item's
        ``shop_domain`` substring, OR contain the lowercased shop name
        as a substring (only when ``len(shop) >= _MIN_SHOP_NAME_LEN`` —
        single- and two-letter shop names produce mass false positives).
      * Acceptance evidence (at least one must hold): either
          (a) **Jaccard path** — line tokens share at least
              ``_MIN_ITEM_OVERLAP`` tokens with the item name AND
              Jaccard >= ``_JACCARD_THRESHOLD``, OR
          (b) **Slug path** — the URL's product slug (e.g.
              ``/products/sukuna-oversize-tee``) shares at least one
              non-stopword token with the item name. Catches
              slug-literal matches and product-line SKU aliases.
      * **Garment-category gate** (applied after either path): if the
        item name mentions a garment category (tee, hoodie, beanie,
        shorts, ...), the line must mention a matching one — unless
        the shared content tokens are strong (>= 2) which indicates a
        same-design match across cuts. Single-token overlap + cat
        mismatch is almost always a sibling SKU (Bee Beanie ↔ bee-hoodie,
        Ippo Spar Tee ↔ boxing-gloves-hajime-no-ippo).
      * The single best-scoring line wins (ties broken by line order;
        slug-only matches use Jaccard purely as a ranking signal).
      * Items with no name tokens are skipped (we can't score them).
    """
    raw_lines = (watchlist_text or "").splitlines()
    # Find the non-clothing section boundary (if present) and collect the
    # stripped lines that fall below it. Items matched against those lines
    # get tagged is_clothing=False so fit-review can skip them.
    non_clothing_start: int | None = None
    for idx, raw in enumerate(raw_lines):
        if _NON_CLOTHING_HEADER_RE.match(raw):
            non_clothing_start = idx
            break
    if non_clothing_start is None:
        non_clothing_lines: set[str] = set()
    else:
        non_clothing_lines = {
            ln.strip() for ln in raw_lines[non_clothing_start + 1:] if ln.strip()
        }
    lines = [ln.strip() for ln in raw_lines if ln.strip()]
    for item in items:
        domain = (item.get("shop_domain") or "").lower()
        shop = (item.get("shop") or "").lower()
        item_name = item.get("item_name") or ""
        item_tokens = _tokens(item_name)
        item_cats = _garment_categories(item_name)
        if not item_tokens:
            continue
        # The shop name is the *link* gate (domain / shop-name substring), not a
        # content-match signal. Counting it as overlap too lets an item whose
        # name reduces to just the shop ("THE OTISHI 2.0" → {otishi}) match a
        # bare "Otishi:" header — or any of that shop's lines — at Jaccard 1.0.
        # Strip the shop's own tokens so acceptance needs a real product token.
        shop_tokens = _tokens(shop)
        best_line: str | None = None
        best_score = 0.0
        for line in lines:
            line_l = line.lower()
            line_tokens = _tokens(line)
            domain_link = bool(domain and domain in line_l)
            shop_link = (
                len(shop) >= _MIN_SHOP_NAME_LEN
                and shop in line_l
            )
            if not (domain_link or shop_link):
                continue
            score = _jaccard(item_tokens, line_tokens)
            content_overlap = (item_tokens & line_tokens) - shop_tokens
            jaccard_accept = (
                len(content_overlap) >= _MIN_ITEM_OVERLAP
                and score >= _JACCARD_THRESHOLD
            )
            slug_accept = bool((item_tokens & _slug_tokens(line)) - shop_tokens)
            if not (jaccard_accept or slug_accept):
                continue
            # Garment-category gate. Item naming a garment category
            # requires the line to mention a matching garment category
            # — UNLESS the shared content tokens (after stopwords) are
            # strong enough (>= 2) to indicate a same-design match
            # across cuts (e.g. Hinata x Kageyama Limits Hoodie ↔ ...
            # Limits Tee). One shared design token + cat mismatch is
            # almost always a sibling SKU, not the same product.
            if item_cats:
                line_cats = _garment_categories(line)
                shared = len(content_overlap)
                if not (item_cats & line_cats) and shared < 2:
                    continue
            if best_line is None or score > best_score:
                best_score = score
                best_line = line
        if best_line:
            line_is_clothing = best_line not in non_clothing_lines
            item["watchlist_match"] = {
                "matched_line": best_line,
                "approved_for_removal": None,
                "score": round(best_score, 3),
                "is_clothing": line_is_clothing,
            }
            # Stamp the item too so fit-review can skip non-clothing
            # purchases without re-deriving the section membership.
            if not line_is_clothing:
                item["is_clothing"] = False


# ---------------------------------------------------------------------------
# Shipment ↔ order linking (post-process Claude's guesses)
# ---------------------------------------------------------------------------

def _link_shipments_to_orders(
    items: list[dict],
    shipments: list[dict],
) -> None:
    """Annotate ``items`` in place with ``shipping_email_id`` / ``shipped_at`` /
    ``tracking_url`` when a shipment matches.

    ``shipments`` is now a list of pure-Python parses (no Claude)::

        {"email_id": str,
         "shop": str, "shop_domain": str,
         "shipped_at": str (ISO date) | "",
         "tracking_url": str | None,
         "order_number": str | None}

    Strategy:
      1. If the shipment has an ``order_number`` that also appears in an
         order email's body, link via that match. (Exact string equality
         after upper-casing.)
      2. Otherwise, fall back to (shop_domain, nearest-order-by-date)
         within a 30-day window.
      3. Otherwise, leave the order unlinked.
    """
    by_order_id: dict[str, list[dict]] = {}
    for item in items:
        by_order_id.setdefault(item["order_email_id"], []).append(item)

    # Build an order_number → order_email_id index for step (1). Items
    # carry the order's parsed order_number on the meta dict (we stash it
    # on the first item per order email below).
    by_order_number: dict[str, str] = {}
    for oid, oitems in by_order_id.items():
        num = oitems[0].get("_order_number")
        if num:
            by_order_number.setdefault(num, oid)

    for ship in shipments or []:
        target_items: list[dict] = []
        order_num = ship.get("order_number")
        if order_num and order_num in by_order_number:
            target_items = by_order_id[by_order_number[order_num]]
        else:
            # Fallback: match on shop_domain + date proximity within 30 days.
            ship_domain = (ship.get("shop_domain") or "").lower()
            ship_date_s = ship.get("shipped_at") or ""
            try:
                ship_date = (
                    datetime.fromisoformat(ship_date_s)
                    if ship_date_s else None
                )
            except ValueError:
                ship_date = None
            best_order_id = None
            best_dt = timedelta(days=31)  # must be < 30d
            for oid, oitems in by_order_id.items():
                domain = (oitems[0].get("shop_domain") or "").lower()
                if not ship_domain or domain != ship_domain:
                    continue
                purchased = oitems[0].get("purchased_at") or ""
                try:
                    pdate = datetime.fromisoformat(purchased)
                except ValueError:
                    continue
                if ship_date is None:
                    best_order_id = oid
                    break
                dt = abs(ship_date - pdate)
                if dt < best_dt:
                    best_dt = dt
                    best_order_id = oid
            if best_order_id:
                target_items = by_order_id[best_order_id]

        for it in target_items:
            it["shipping_email_id"] = ship.get("email_id")
            it["shipped_at"] = ship.get("shipped_at") or None
            it["tracking_url"] = ship.get("tracking_url")


# ---------------------------------------------------------------------------
# Item construction
# ---------------------------------------------------------------------------

def _item_id(email_id: str, index: int) -> str:
    h = hashlib.sha256(f"{email_id}::{index}".encode("utf-8")).hexdigest()
    return h[:12]


def _materialise_items(
    extracted_orders: list[dict],
    order_meta_by_id: dict[str, dict],
    links_by_id: dict[str, list[str]] | None = None,
) -> list[dict]:
    """Flatten Claude's items array into wardrobe item dicts.

    Claude is only responsible for the ``items`` field. Everything else
    (shop, shop_domain, purchased_at, order_total, currency) is filled
    deterministically from ``order_meta_by_id``, which the caller built
    via ``order_parse`` helpers on the original email.

    ``links_by_id`` (optional) maps each email id to the product-page URLs
    harvested from its HTML body; each item is matched to its best one
    (``_match_product_url``) and stamped as ``product_url`` so the wardrobe
    browser can link straight to the product page. Absent/no-match → ``None``.

    ``order_meta_by_id``: ``{email_id: {shop, shop_domain, purchased_at,
    order_total: {amount, currency} | None}}``.
    """
    out: list[dict] = []
    for o in extracted_orders or []:
        email_id = o.get("email_id", "")
        meta = order_meta_by_id.get(email_id, {})
        email_links = (links_by_id or {}).get(email_id) or []
        for idx, raw_item in enumerate(o.get("items") or []):
            name = (raw_item.get("name") or "").strip()
            if not name:
                continue
            item = {
                "id": _item_id(email_id, idx),
                "shop": meta.get("shop") or "",
                "shop_domain": meta.get("shop_domain") or "",
                "item_name": name,
                "size": raw_item.get("size"),
                "color": raw_item.get("color"),
                "qty": raw_item.get("qty") or 1,
                # Per-item price comes from Claude when shown in the body;
                # the order_total (deterministic from regex) is stored
                # separately on the order if we want it later.
                "price_paid": raw_item.get("price"),
                "purchased_at": meta.get("purchased_at") or "",
                # Direct product-page link harvested from the email HTML and
                # matched to this item by slug (issue #23); None when no
                # unambiguous match. The browser falls back to a search link.
                "product_url": _match_product_url(name, email_links),
                "order_email_id": email_id,
                "shipping_email_id": None,
                "shipped_at": None,
                "tracking_url": None,
                "fit_review": None,
                "watchlist_match": None,
            }
            # Durable garment category, when Claude supplied a valid one
            # (issue #18). The browser reads this instead of name-guessing;
            # a non_clothing verdict also sets is_clothing so it's hidden +
            # skipped by the body-comp / fit nudges. Garment categories leave
            # is_clothing absent (treated as clothing), keeping items lean.
            category = normalise_category(raw_item.get("category"))
            if category:
                item["category"] = category
                if category == NON_CLOTHING:
                    item["is_clothing"] = False
            out.append(item)
    return out


def _date_from_header(date_header: str) -> str:
    """Convert an RFC 2822 Date header to ISO YYYY-MM-DD. Best-effort."""
    if not date_header:
        return ""
    try:
        dt = email.utils.parsedate_to_datetime(date_header)
        if dt is None:
            return ""
        return dt.date().isoformat()
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# Forwarded-email parsing (targeted-scrape mode)
# ---------------------------------------------------------------------------
#
# When the user forwards an old order email to themselves, the new message's
# From is the user and its Date is "now" — both wrong for the wardrobe. The
# original sender / date / subject live as plain text in a forwarded-header
# block near the top of the body. These helpers recover them; resolve_shop +
# the deterministic date parser then fill the item meta exactly as a direct
# scan would. See _run_message_scan and the CLAUDE.md targeted-scrape section.

# Month names → number, for the dependency-free forwarded-date parser
# (python-dateutil is intentionally not a dependency here).
_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ("January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"), start=1)
}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})  # "jan".."dec"

_FWD_DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_FWD_DATE_MONTHNAME_RE = re.compile(  # "Jun 3, 2024" / "June 3rd 2024"
    r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")
_FWD_DATE_DAYFIRST_RE = re.compile(   # "3 June 2024"
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
_FWD_DATE_NUMERIC_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")  # US m/d/y


def _safe_iso(y: int, mo: int, d: int) -> str:
    try:
        return datetime(y, mo, d).date().isoformat()
    except ValueError:
        return ""


def _parse_forwarded_date(raw: str) -> str:
    """Parse a forwarded-header Date/Sent line into ISO ``YYYY-MM-DD``.

    Tolerant + dependency-free. Handles the real shapes across forward
    templates: RFC 2822 ("Mon, 3 Jun 2024 17:14:00 -0700"), Gmail web
    ("Mon, Jun 3, 2024 at 5:14 PM"), Apple ("June 3, 2024 at 5:14:00 PM PDT"),
    Outlook ("Monday, June 3, 2024 5:14 PM"), and numeric US ("6/3/2024").
    Returns "" when nothing parseable is found.
    """
    if not raw:
        return ""
    s = raw.strip()
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt is not None:
            return dt.date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    m = _FWD_DATE_ISO_RE.search(s)
    if m:
        iso = _safe_iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if iso:
            return iso
    m = _FWD_DATE_MONTHNAME_RE.search(s)
    if m and m.group(1).lower() in _MONTHS:
        iso = _safe_iso(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
        if iso:
            return iso
    m = _FWD_DATE_DAYFIRST_RE.search(s)
    if m and m.group(2).lower() in _MONTHS:
        iso = _safe_iso(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
        if iso:
            return iso
    m = _FWD_DATE_NUMERIC_RE.search(s)
    if m:
        mo, d, y = (int(g) for g in m.groups())
        if y < 100:
            y += 2000
        iso = _safe_iso(y, mo, d)
        if iso:
            return iso
    return ""


# Forwarded-header markers (Gmail dashed line / Apple "Begin forwarded
# message:"). Outlook emits no marker — just a From/Sent/To/Subject block.
_FWD_MARKER_RE = re.compile(
    r"-+\s*Forwarded message\s*-+|Begin forwarded message:", re.IGNORECASE)
_FWD_FROM_RE = re.compile(r"^\s*From:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_FWD_DATE_RE = re.compile(
    r"^\s*(?:Date|Sent):\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_FWD_SUBJECT_RE = re.compile(
    r"^\s*Subject:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def _forwarded_origin(body_text: str) -> dict | None:
    """Recover ``{from, date, subject}`` from a forwarded email's body.

    A forward embeds the original headers as plain text near the top::

        ---------- Forwarded message ---------
        From: Riot Games <store@mail.riotgames.com>
        Date: Mon, Jun 3, 2024 at 5:14 PM
        Subject: Your Riot Games order
        To: olduser@gmail.com

    Gmail / Apple / Outlook each emit a recognisable variant. ``date`` is
    normalised to ISO; ``from`` / ``subject`` are returned verbatim. Returns
    ``None`` when no usable forward header is found (the caller then prompts).

    Without an explicit marker we only trust a *tight* From + (Date|Sent) +
    Subject block, so a stray "From: the team" in a footer isn't mistaken for a
    forward. Note: this relies on the cleanly-rendered text/plain part Gmail
    ships with every forward; a mangled HTML-only fallback yields no match and
    falls through to the interactive prompt by design.
    """
    if not body_text:
        return None
    head = body_text[:4000]
    marker = _FWD_MARKER_RE.search(head)
    region = head[marker.start():] if marker else head
    fm = _FWD_FROM_RE.search(region)
    if not fm:
        return None
    after = region[fm.end():fm.end() + 600]
    dm = _FWD_DATE_RE.search(after)
    sm = _FWD_SUBJECT_RE.search(after)
    if not marker and not (dm and sm):
        return None
    return {
        "from": fm.group(1).strip(),
        "date": _parse_forwarded_date(dm.group(1)) if dm else "",
        "subject": sm.group(1).strip() if sm else "",
    }


def _strip_forward_preamble(body_text: str) -> str:
    """Drop the leading forwarded-header block so the Claude excerpt budget
    goes to the original receipt. Best-effort: returns the body unchanged when
    no recognisable preamble / trailing blank line is found."""
    if not body_text:
        return body_text
    marker = _FWD_MARKER_RE.search(body_text[:4000])
    if not marker:
        return body_text
    region = body_text[marker.start():]
    m = re.search(r"\n\s*\n", region)
    return region[m.end():] if m else body_text


def _normalise_msgid(token: str) -> str | None:
    """Normalise a user-supplied message id into the decimal ``X-GM-MSGID``
    that IMAP's ``X-GM-MSGID`` search wants.

    Accepts the stored decimal id ("17699..."), a hex id with/without ``0x``,
    or a pasted ``#all/<hex>`` Gmail permalink tail (the ``<hex>`` segment is
    the X-GM-MSGID in hex — the form review_requests builds). All-digit tokens
    are read as decimal; anything with hex letters is read as hex. Returns the
    decimal string, or ``None`` when nothing parseable is left.
    """
    if not token:
        return None
    t = token.strip()
    if "#" in t or "/" in t:
        t = t.rsplit("/", 1)[-1].strip()
    if not t:
        return None
    if t.lower().startswith("0x"):
        try:
            return str(int(t, 16))
        except ValueError:
            return None
    if t.isdigit():
        return t
    try:
        return str(int(t, 16))
    except ValueError:
        return None


def _is_self_forward(from_header: str, gmail_username: str | None) -> bool:
    """True when an email's From is the user's own Gmail address — i.e. a
    self-forward meant for the targeted-scrape command, not a real receipt.
    The daily INBOX sweep skips these so they aren't ingested as "Gmail"."""
    if not gmail_username:
        return False
    return gmail_username.strip().lower() in (from_header or "").lower()


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _empty_wardrobe() -> dict:
    return {"items": [], "scan_state": {}, "watchlist_exclusions": [],
            "shop_fit_notes": {}}


def _normalise(wardrobe: dict | None) -> dict:
    w = dict(wardrobe or {})
    w.setdefault("items", [])
    w.setdefault("scan_state", {})
    w.setdefault("watchlist_exclusions", [])
    w.setdefault("shop_fit_notes", {})
    scan_state = dict(w["scan_state"] or {})
    scan_state.setdefault("processed_email_ids", {})
    scan_state.setdefault("last_scanned_at", None)
    w["scan_state"] = scan_state
    return w


def _since_from_state(wardrobe: dict, since_override: datetime | None) -> datetime:
    if since_override is not None:
        return since_override
    last = wardrobe["scan_state"].get("last_scanned_at")
    if last:
        try:
            return datetime.fromisoformat(last.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc) - timedelta(days=365 * _DEFAULT_LOOKBACK_YEARS)


# ---------------------------------------------------------------------------
# Interactive flows (questionary)
# ---------------------------------------------------------------------------

class _InteractiveAbort(Exception):
    """User pressed Ctrl-C (or otherwise cancelled) inside an interactive
    prompt. Propagated up to ``run`` so we skip the Gist write — anything
    short of an explicit "save" is treated as "throw away this session".

    Distinct from a clean per-item "skip" (which is a real choice on the
    select prompt) so the caller can tell the difference.
    """


def _interactive_watchlist_approval(items: list[dict], wardrobe: dict) -> None:
    """Surface every item with a pending watchlist_match for user approval.

    Approved items get ``approved_for_removal=True`` and a row in
    ``watchlist_exclusions``. Declined items get ``approved_for_removal=False``
    so we won't re-prompt next run.
    """
    pending = [
        it for it in items
        if it.get("watchlist_match")
        and it["watchlist_match"].get("approved_for_removal") is None
    ]
    if not pending:
        log.info("order_scan: no pending watchlist matches to review")
        return

    import questionary

    choices = []
    for it in pending:
        size = f", {it['size']}" if it.get("size") else ""
        color = f", {it['color']}" if it.get("color") else ""
        label = (
            f"{it['shop']} — {it['item_name']}{size}{color}  "
            f"[matched: {it['watchlist_match']['matched_line']}]"
        )
        choices.append(questionary.Choice(title=label, value=it["id"], checked=True))

    print()
    print("These purchased items appear to match lines on your watchlist Doc.")
    print("Toggle off any you do NOT want to mark as purchased.")
    print("(Ctrl-C to abort the whole run without saving.)")
    print()
    selected_ids: list[str] | None = questionary.checkbox(
        "Approve removals (Space to toggle, Enter to confirm):",
        choices=choices,
    ).ask()
    if selected_ids is None:
        raise _InteractiveAbort("watchlist approval cancelled")

    selected = set(selected_ids)
    now_iso = datetime.now(timezone.utc).isoformat()
    approved_lines: list[str] = []
    for it in pending:
        approved = it["id"] in selected
        it["watchlist_match"]["approved_for_removal"] = approved
        if approved:
            line = it["watchlist_match"]["matched_line"]
            approved_lines.append(line)
            wardrobe["watchlist_exclusions"].append({
                "matched_line": line,
                "added_at": now_iso,
                "item_id": it["id"],
            })

    if approved_lines:
        print()
        print("=" * 60)
        print("Remove these lines from the watchlist Google Doc:")
        print("=" * 60)
        for ln in approved_lines:
            print(f"  • {ln}")
        print("=" * 60)
        print()


# Overall fit, in spectrum order (too small → too big), then the two actions.
# Values match the schema; legacy tts/small/large are a subset so old entries
# stay valid. Shown by the CLI fallback only — the web form is the primary path.
_FIT_CHOICES = [
    ("too small / can't wear", "too_small"),
    ("runs small / snug", "small"),
    ("true to size", "tts"),
    ("runs large / roomy", "large"),
    ("too big", "too_large"),
    ("skip (review later)", "skip"),
    ("drop from wardrobe (not clothing / wrong)", "drop"),
]

# Optional per-area detail. Each is a 3-way select prefixed with a skip option.
_AREA_PROMPTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("length", "Length", ("short", "good", "long")),
    ("shoulders_chest", "Shoulders / chest", ("tight", "good", "loose")),
    ("sleeves", "Sleeves (length)", ("short", "good", "long")),
    ("sleeve_opening", "Sleeve opening", ("tight", "good", "wide")),
    ("waist_hips", "Waist / hips", ("tight", "good", "loose")),
    ("inseam", "Inseam", ("short", "good", "long")),
)
_NEXT_TIME_CHOICES = [
    ("size down next time", "size_down"),
    ("same size", "same"),
    ("size up next time", "size_up"),
    ("buy again (this size)", "buy_again"),
    ("avoid / don't rebuy", "avoid"),
]
_VERDICT_CHOICES = [("keep", "keep"), ("return", "return"), ("tailor", "tailor")]

# Sentinel value for the "leave this optional field blank" choice. Distinct from
# ``None`` (which ``questionary.ask()`` returns on Ctrl-C) so a skipped field and
# an aborted prompt can't be confused.
_FIELD_SKIP = "__skip__"


def _select_or_abort(questionary, prompt: str, choices: list[tuple[str, str]]):
    """Run a questionary select; raise ``_InteractiveAbort`` on Ctrl-C."""
    qc = [questionary.Choice(label, value=val) for label, val in choices]
    ans = questionary.select(prompt, choices=qc).ask()
    if ans is None:
        raise _InteractiveAbort("fit review cancelled")
    return ans


def _optional_select(questionary, prompt: str, values: tuple[str, ...]):
    """Optional select (prepends a skip choice). Returns the value, or ``None``
    when the user skips it; raises ``_InteractiveAbort`` on Ctrl-C."""
    choices = [("— skip —", _FIELD_SKIP)] + [(v, v) for v in values]
    ans = _select_or_abort(questionary, prompt, choices)
    return None if ans == _FIELD_SKIP else ans


def _collect_fit_detail(questionary, review: dict) -> None:
    """Ask the optional detailed-fit prompts and merge any answers into
    ``review`` in place. Each is skippable; absent answers leave no key."""
    areas: dict[str, str] = {}
    for key, label, values in _AREA_PROMPTS:
        val = _optional_select(questionary, f"    {label}:", values)
        if val is not None:
            areas[key] = val
    if areas:
        review["areas"] = areas

    inseam_raw = questionary.text("    Inseam (inches, optional):").ask()
    if inseam_raw is None:
        raise _InteractiveAbort("fit review cancelled")
    inseam_raw = inseam_raw.strip()
    if inseam_raw:
        try:
            review["inseam_inches"] = float(inseam_raw)
        except ValueError:
            pass  # ignore a non-numeric entry rather than abort the whole run

    next_time = _optional_select(
        questionary, "    Next time:", tuple(v for _, v in _NEXT_TIME_CHOICES)
    )
    if next_time is not None:
        review["next_time"] = next_time
    verdict = _optional_select(
        questionary, "    Verdict:", tuple(v for _, v in _VERDICT_CHOICES)
    )
    if verdict is not None:
        review["verdict"] = verdict


def _interactive_fit_review(
    items: list[dict], shop_fit_notes: dict | None = None,
) -> None:
    """Walk items missing a fit_review, grouped by shop, with prior-shop fit data
    and any saved per-shop note surfaced as context.

    The overall ``fit`` is the only required answer (a fast "runs small" review);
    the per-area detail, next-time action, inseam, and verdict are all optional
    prompts gated behind a single confirm, so a quick pass stays quick. Items
    flagged ``is_clothing=False`` (from a Non-clothing watchlist match) are
    skipped — gadgets have no fit to review. This is the offline fallback; the
    emailed web form is the primary capture path.
    """
    pending = pending_fit_items(items)
    if not pending:
        log.info("order_scan: no items waiting for fit review")
        return

    import questionary

    shop_fit_notes = shop_fit_notes or {}

    # Index prior reviews per shop so we can surface context.
    prior_by_shop: dict[str, list[dict]] = {}
    for it in items:
        review = it.get("fit_review")
        if review:
            prior_by_shop.setdefault(it.get("shop") or "", []).append(it)

    # Group pending by shop.
    pending_by_shop: dict[str, list[dict]] = {}
    for it in pending:
        pending_by_shop.setdefault(it.get("shop") or "(unknown)", []).append(it)

    print()
    print(f"Fit review — {len(pending)} item(s) across {len(pending_by_shop)} shop(s)")
    print("(Ctrl-C at any prompt aborts the whole run without saving.)")
    print()

    for shop, shop_items in pending_by_shop.items():
        priors = prior_by_shop.get(shop, [])
        header_extra = (
            f" (prior: {len(priors)} reviewed)" if priors else " (no prior fit data)"
        )
        print(f"=== {shop}{header_extra} ===")
        note = shop_fit_notes.get(shop)
        if note:
            print(f"  📝 note: {note}")
        for prev in priors[:3]:
            review = prev["fit_review"]
            print(
                f"  ↳ context: {prev['item_name']} "
                f"size {prev.get('size') or '?'} → {review['fit']}"
                + (f" — {review['notes']}" if review.get("notes") else "")
            )

        for it in shop_items:
            size = it.get("size") or "?"
            color = f", {it['color']}" if it.get("color") else ""
            print()
            print(f"  {it['item_name']} — size {size}{color}, "
                  f"purchased {it.get('purchased_at') or '?'}")
            fit = _select_or_abort(questionary, "  Fit:", _FIT_CHOICES)
            # "skip (review later)" is the explicit-choice way to skip one item.
            if fit == "skip":
                continue
            if fit == "drop":
                it["dropped"] = True
                # Sentinel fit_review so we don't re-prompt next run.
                it["fit_review"] = {
                    "fit": "dropped",
                    "notes": "user marked as not clothing / mis-extracted",
                    "reviewed_at": datetime.now(timezone.utc).isoformat(),
                }
                continue

            review = {
                "fit": fit,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
                "source": "cli",
            }
            add_detail = questionary.confirm(
                "  Add detailed fit notes (areas, next-time, verdict)?",
                default=False,
            ).ask()
            if add_detail is None:
                raise _InteractiveAbort("fit review cancelled")
            if add_detail:
                _collect_fit_detail(questionary, review)

            notes_raw = questionary.text("  Notes (optional):").ask()
            if notes_raw is None:
                raise _InteractiveAbort("fit-review notes cancelled")
            if notes_raw.strip():
                review["notes"] = notes_raw.strip()
            it["fit_review"] = review
        print()


# ---------------------------------------------------------------------------
# Scan pipeline
# ---------------------------------------------------------------------------

def _run_scan(
    cfg: Config,
    wardrobe: dict,
    since: datetime,
    shop_filter: str | None,
    *,
    max_emails: int | None = None,
    imap_client: imaplib.IMAP4 | None = None,
    anthropic_client=None,
    shop_aliases: dict[str, str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Fetch emails, classify, parse deterministic fields, extract items
    via Claude, materialise wardrobe items.

    Returns ``(new_items, processed_email_ids)``. Caller merges these into
    the wardrobe dict.

    Only the ``items`` array is pulled by Claude. ``shop``, ``shop_domain``,
    ``purchased_at``, ``shipped_at``, ``tracking_url``, and shipment ↔
    order linkage are all done deterministically via ``order_parse`` +
    the Gmail Date header. Shipping emails never reach Claude.
    """
    shop_aliases = shop_aliases or {}
    skip_ids = set(wardrobe["scan_state"].get("processed_email_ids", {}).keys())
    emails = _fetch_emails(cfg, since, skip_ids, imap_client=imap_client)
    if not emails:
        log.info("order_scan: nothing new")
        return [], []

    # Pre-classify + pre-parse every email. Each email becomes either an
    # "order" record (sent to Claude for items) or a "shipping" record
    # (pure code — never sent to Claude).
    order_records: list[dict] = []
    ship_records: list[dict] = []
    # Ids of emails classified "other" (junk). These ARE recorded as processed
    # so we don't re-fetch them forever. Order/ship ids are deliberately NOT
    # recorded here — they're only marked processed once they survive the
    # --shop / --max-emails filters below (see the processed_ids computation
    # before the return). That stops a filtered/capped run from "burning" an
    # order email it fetched but never extracted.
    other_ids: list[str] = []
    # Excluded-shop emails (EXCLUDED_SHOPS): recorded as processed so they're
    # never re-fetched, but never sent to Claude or materialised. Kept in their
    # own list only for the log count; folded into processed_ids on return.
    excluded_ids: list[str] = []
    for em in emails:
        eid = em.get("id")
        if not eid:
            continue
        # Self-forwarded order emails (From == our own address) belong to the
        # targeted-scrape command (--message-query), not this date-windowed
        # INBOX sweep — which would otherwise ingest the forward attributed to
        # "Gmail". Skip + record as processed. A real shop never sends a receipt
        # from the user's own address, so this can't drop a genuine order.
        if _is_self_forward(em.get("from", ""), cfg.gmail_username):
            other_ids.append(eid)
            continue
        label = _classify(em)
        if label in ("order", "shipping"):
            shop, domain = resolve_shop(em.get("from", ""), shop_aliases)
            if is_excluded_shop(shop, domain, cfg.excluded_shops):
                # Privacy filter — this shop's purchases stay out of the wardrobe.
                excluded_ids.append(eid)
                continue
            body = em.get("body_text", "")
            if label == "order":
                order_records.append({
                    "email_id": eid,
                    "from": em.get("from", ""),
                    "subject": em.get("subject", ""),
                    "body_text": body,
                    "body_excerpt": _excerpt(body),
                    "purchased_at": _date_from_header(em.get("date") or ""),
                    "shop": shop,
                    "shop_domain": domain,
                    "order_total": extract_total(body),
                    "order_number": extract_order_number(body),
                    "product_links": _links_for_domain(
                        em.get("product_links") or [], domain,
                    ),
                })
            else:  # shipping
                ship_records.append({
                    "email_id": eid,
                    "from": em.get("from", ""),
                    "subject": em.get("subject", ""),
                    "shipped_at": _date_from_header(em.get("date") or ""),
                    "shop": shop,
                    "shop_domain": domain,
                    "tracking_url": extract_tracking_url(body),
                    "order_number": extract_order_number(body),
                })
        else:
            other_ids.append(eid)

    log.info(
        "order_scan: classified — orders=%d shipments=%d skipped_other=%d excluded=%d",
        len(order_records), len(ship_records), len(other_ids), len(excluded_ids),
    )

    if not order_records and not ship_records:
        return [], other_ids + excluded_ids

    if shop_filter:
        sf = shop_filter.lower()
        order_records = [
            o for o in order_records
            if sf in (o["from"] + o["subject"] + (o.get("shop") or "")).lower()
        ]
        ship_records = [
            s for s in ship_records
            if sf in (s["from"] + s["subject"] + (s.get("shop") or "")).lower()
        ]
        log.info(
            "order_scan: post-filter — orders=%d shipments=%d (--shop=%r)",
            len(order_records), len(ship_records), shop_filter,
        )

    if max_emails is not None:
        # Cap the number of emails sent to Claude (orders only — shipments
        # are pure code and free). Keep newest first.
        if len(order_records) > max_emails:
            order_records = list(reversed(order_records))[:max_emails]
            log.info(
                "order_scan: --max-emails=%d → orders=%d (shipments uncapped: %d)",
                max_emails, len(order_records), len(ship_records),
            )

    items: list[dict] = []
    if order_records:
        log.info("order_scan: calling Claude for items extraction")
        from src.order_extract import extract_items
        # Pass only the fields Claude needs — keeps the prompt tight.
        claude_input = [{
            "email_id": r["email_id"],
            "from": r["from"],
            "subject": r["subject"],
            "body_excerpt": r["body_excerpt"],
        } for r in order_records]
        extracted = extract_items(claude_input, client=anthropic_client)
        log.info("order_scan: claude usage = %s", extracted.get("usage"))

        # Build the meta-by-id index for _materialise_items.
        meta_by_id = {
            r["email_id"]: {
                "shop": r["shop"],
                "shop_domain": r["shop_domain"],
                "purchased_at": r["purchased_at"],
            }
            for r in order_records
        }
        links_by_id = {
            r["email_id"]: r.get("product_links") or [] for r in order_records
        }
        items = _materialise_items(
            extracted.get("orders", []), meta_by_id, links_by_id,
        )

        # Stash order_number on the first item per order so shipment
        # linkage can match by number. Internal-only field; stripped
        # before persistence.
        order_number_by_eid = {r["email_id"]: r["order_number"] for r in order_records}
        for it in items:
            it["_order_number"] = order_number_by_eid.get(it["order_email_id"])

    if ship_records:
        _link_shipments_to_orders(items, ship_records)

    # Strip the internal _order_number field so it doesn't leak into the Gist.
    for it in items:
        it.pop("_order_number", None)

    # Mark processed: junk ("other") plus the order/ship emails that survived
    # the --shop / --max-emails filters above. Order/ship emails dropped by a
    # filter are intentionally left unrecorded, so a later unfiltered run
    # re-fetches and extracts them instead of permanently skipping them.
    processed_ids = (
        other_ids
        + excluded_ids
        + [r["email_id"] for r in order_records]
        + [s["email_id"] for s in ship_records]
    )
    return items, processed_ids


def _merge_items(existing: list[dict], new: list[dict]) -> list[dict]:
    """Append new items, deduping by ``id``. Existing entries win."""
    seen = {it["id"] for it in existing}
    out = list(existing)
    for it in new:
        if it["id"] in seen:
            continue
        out.append(it)
        seen.add(it["id"])
    return out


def _drop_excluded_items(wardrobe: dict, excluded: tuple[str, ...]) -> int:
    """Hard-delete wardrobe items whose shop is in ``EXCLUDED_SHOPS``, in place.

    The privacy filter is enforced at ingestion too (see ``_run_scan``), but
    items stored before a shop was excluded must be purged retroactively. Called
    on every non-dry-run ``order_scan`` so the exclusion self-heals. Returns the
    number of items removed. Empty ``excluded`` → no-op."""
    if not excluded:
        return 0
    items = wardrobe.get("items") or []
    kept = [
        it for it in items
        if not is_excluded_shop(it.get("shop"), it.get("shop_domain"), excluded)
    ]
    removed = len(items) - len(kept)
    if removed:
        wardrobe["items"] = kept
    return removed


# ---------------------------------------------------------------------------
# BodySpec body-comp backfill
# ---------------------------------------------------------------------------

# Homeware / décor keywords. The ``is_clothing`` flag only catches items
# matched to a Non-clothing watchlist line; homeware that was never matched
# (towels, bedding, throw pillows, a quilt) would otherwise eat backfill slots
# meant for garments. Word-boundary matched on purpose so garments survive:
# ``\bquilt\b`` won't hit "quilted jacket", ``\bthrow\b`` won't hit "throwback".
# Bedsheets are caught via "bed sheet"; bare "sheet" is left out to avoid
# clipping things like "sheer". Socks/hats/scarves are clothing and stay.
_HOMEWARE_RE = re.compile(
    r"\b("
    r"towels?|washcloths?|bedding|bed\s?sheets?|"
    r"pillows?|pillowcases?|shams?|quilts?|duvets?|comforters?|"
    r"blankets?|throws?|rugs?|curtains?|cushions?|"
    r"tablecloths?|napkins?|coasters?|placemats?|"
    r"mugs?|candles?|vases?|mattress(?:es)?"
    r")\b",
    re.I,
)


def _looks_like_homeware(name: str | None) -> bool:
    return bool(_HOMEWARE_RE.search(name or ""))


def _backfill_target(item: dict) -> tuple[object, str]:
    """The date a ``body_comp`` stamp should be matched against, and its label.

    Phase B: once an item carries a fit review, the body state that matters is
    the one when the user actually *tried it on* (``fit_review.reviewed_at``),
    not when they bought it — slow shipping / made-to-order can put months
    between the two. So a reviewed item matches against ``reviewed_at``
    (``matched_to="fit_review"``); everything else against ``purchased_at``
    (``matched_to="purchase"``).
    """
    fr = item.get("fit_review")
    reviewed_at = fr.get("reviewed_at") if isinstance(fr, dict) else None
    if reviewed_at and bodyspec._to_date(reviewed_at) is not None:
        return reviewed_at, "fit_review"
    return item.get("purchased_at"), "purchase"


def _needs_backfill(item: dict, refresh: bool) -> bool:
    """Whether an item should be (re)stamped this run.

    Unstamped items always qualify. An already-stamped item is only re-stamped
    when a fit review now wants a ``reviewed_at`` match that hasn't been applied
    yet (purchase-time stamp, or a stamp pointing at a different review date) —
    that's the Phase B re-match. ``refresh`` forces a re-stamp regardless. This
    keeps the backfill idempotent: running it twice with no new reviews is a
    no-op.
    """
    if refresh:
        return True
    bc = item.get("body_comp")
    if not bc:
        return True
    _, matched_to = _backfill_target(item)
    if matched_to != "fit_review":
        return False  # purchase-only item, already stamped — leave it.
    reviewed_date = bodyspec._to_date((item.get("fit_review") or {}).get("reviewed_at"))
    already_fit_matched = (
        bc.get("matched_to") == "fit_review"
        and bodyspec._to_date(bc.get("matched_date")) == reviewed_date
    )
    return not already_fit_matched


def _summarise_body_comp(bc: dict) -> dict:
    """Compact body-comp snapshot stored on ``fit_review.body_comp_summary``.

    The four metrics the user reasons about (weight, body-fat %, lean, fat) plus
    provenance, copied off the full item-level ``body_comp`` block. Lets a fit
    review carry the body state it was matched against without duplicating the
    whole per-region breakdown.
    """
    return {
        "weight_kg": bc.get("weight_kg"),
        "body_fat_pct": bc.get("body_fat_pct"),
        "lean_mass_kg": bc.get("lean_mass_kg"),
        "fat_mass_kg": bc.get("fat_mass_kg"),
        "scan_date": bc.get("scan_date"),
        "matched_to": bc.get("matched_to"),
        "matched_date": bc.get("matched_date"),
        "days_from_event": bc.get("days_from_event"),
    }


def _select_backfill_items(
    items: list[dict], limit: int, refresh: bool,
) -> list[dict]:
    """Most-recently-purchased ``limit`` clothing items eligible for a
    ``body_comp`` stamp.

    Eligible = ``is_clothing`` not False (body comp is meaningless for rugs /
    gadgets), not obvious homeware by name (towels / bedding / pillows — see
    ``_HOMEWARE_RE``), a parseable target date (``reviewed_at`` when reviewed,
    else ``purchased_at`` — see ``_backfill_target``), and in need of a (re)stamp
    (see ``_needs_backfill`` — covers unstamped items and the Phase B re-match).
    Sorted by ``purchased_at`` descending (ISO dates sort lexically) so the
    newest purchases win the limited budget.
    """
    eligible = [
        it for it in items
        if it.get("is_clothing") is not False
        and not _looks_like_homeware(it.get("item_name"))
        and bodyspec._to_date(_backfill_target(it)[0]) is not None
        and _needs_backfill(it, refresh)
    ]
    eligible.sort(key=lambda it: it.get("purchased_at") or "", reverse=True)
    return eligible[:limit]


def _run_bodycomp_backfill(
    cfg: Config,
    wardrobe: dict,
    *,
    limit: int,
    max_gap_days: int,
    refresh: bool,
    scans: list[dict] | None = None,
    refresh_scans: bool = False,
) -> tuple[dict, dict | None]:
    """Stamp ``body_comp`` (nearest DEXA scan) onto recent clothing items.

    Mutates ``wardrobe["items"]`` in place. Returns ``(stats, cache_out)`` where
    ``cache_out`` is a freshly built ``body_scans`` cache to persist (only when
    the scan cache was rebuilt this run via ``--refresh-scans`` or an empty
    cache), else ``None``.

    Scan data normally comes from the cached ``body_scans.json`` records passed
    in as ``scans`` — pre-shaped, so matching is a pure ``nearest_result`` +
    ``body_comp_from_record`` with no BodySpec auth or composition fetch. A live
    pull happens only when ``refresh_scans`` is set or no cache exists yet.
    """
    targets = _select_backfill_items(wardrobe["items"], limit, refresh)
    if not targets:
        log.info("order_scan: no clothing items eligible for body-comp backfill")
        return (
            {"considered": 0, "stamped": 0, "skipped_no_scan": 0, "scans_used": 0},
            None,
        )

    cache_out: dict | None = None
    records = list(scans or [])
    if refresh_scans or not records:
        log.info(
            "order_scan: %s BodySpec scan cache via live pull",
            "refreshing" if records else "building (no cache yet)",
        )
        token = bodyspec.authenticate(cfg.bodyspec_username, cfg.bodyspec_password)
        cache_out = bodyspec.build_scan_cache(token)
        records = cache_out.get("scans") or []

    if not records:
        log.warning("order_scan: no BodySpec scans available — nothing to attach")
        return (
            {
                "considered": len(targets), "stamped": 0,
                "skipped_no_scan": len(targets), "scans_used": 0,
            },
            cache_out,
        )

    log.info(
        "order_scan: body-comp backfill — %d candidate item(s) against %d scan(s)",
        len(targets), len(records),
    )
    used_scan_ids: set = set()
    stamped = skipped = 0
    for it in targets:
        target_date, matched_to = _backfill_target(it)
        scan = bodyspec.nearest_result(
            records, target_date, max_gap_days=max_gap_days
        )
        if scan is None:
            skipped += 1
            log.info(
                "order_scan: no scan within %dd of %s (%s) — skipping %r",
                max_gap_days, target_date, matched_to, it.get("item_name"),
            )
            continue
        # Phase B keep-both: when re-matching a previously purchase-stamped item
        # to its review date, preserve the purchase-time block rather than lose
        # it — body_comp becomes the fit-time scan, body_comp_at_purchase the old.
        existing = it.get("body_comp")
        if (
            matched_to == "fit_review"
            and isinstance(existing, dict)
            and existing.get("matched_to") == "purchase"
        ):
            it["body_comp_at_purchase"] = existing
        it["body_comp"] = bodyspec.body_comp_from_record(
            scan, target_date, matched_to,
        )
        # Mirror a compact snapshot onto the fit review itself, so the review
        # carries the body state it was matched against.
        if matched_to == "fit_review" and isinstance(it.get("fit_review"), dict):
            it["fit_review"]["body_comp_summary"] = _summarise_body_comp(it["body_comp"])
        used_scan_ids.add(scan.get("result_id"))
        stamped += 1

    stats = {
        "considered": len(targets), "stamped": stamped,
        "skipped_no_scan": skipped, "scans_used": len(used_scan_ids),
    }
    log.info("order_scan: body-comp backfill done — %s", stats)
    return stats, cache_out


# ---------------------------------------------------------------------------
# Category classification backfill (issue #18)
# ---------------------------------------------------------------------------

def _needs_classify(item: dict, refresh: bool) -> bool:
    """Whether an item should be (re)classified this run.

    Items already carrying a valid stored ``category`` are skipped unless
    ``refresh`` forces a re-run, so a second ``--classify`` pass with no new
    items is a no-op.
    """
    if refresh:
        return True
    return normalise_category(item.get("category")) is None


def _run_classify(
    cfg: Config,
    wardrobe: dict,
    *,
    refresh: bool,
    limit: int | None = None,
    only_category: str | None = None,
    anthropic_client=None,
    batch_size: int = order_classify.DEFAULT_BATCH_SIZE,
) -> dict:
    """Stamp a durable ``category`` (+ derived ``is_clothing``) onto items.

    Two passes:
      1. Items already flagged ``is_clothing is False`` (they matched a
         Non-clothing watchlist line — user-authoritative) are stamped
         ``category="non_clothing"`` locally, for free, and never sent to
         Claude.
      2. Every remaining item lacking a valid stored category (or all of them
         under ``refresh``) goes to Claude, which returns one category key
         each. ``is_clothing`` is derived (``category == "non_clothing"`` ->
         False); garment categories leave the flag absent (treated as
         clothing) to keep items lean.

    ``only_category`` scopes the run to items **currently** stored as that one
    category and re-classifies just them (e.g. moving the old generic
    ``shorts`` bucket into ``shorts_athletic`` / ``shorts_casual`` after a
    taxonomy split). It re-asks Claude regardless of ``refresh`` (the items
    already have a valid category), skips the Non-clothing pass entirely, and
    is the cheap way to retype one bucket without re-billing the whole
    catalogue. ``non_clothing`` items are never eligible.

    Mutates ``wardrobe["items"]`` in place; returns a stats dict.
    """
    items = wardrobe.get("items") or []

    if only_category is not None:
        # Scoped retype: just the items currently in this one bucket. No
        # Non-clothing pass (we're not touching hidden items), and `refresh`
        # is implied — they all have a valid category we're deliberately redoing.
        local_non_clothing = 0
        targets = [
            it for it in items
            if it.get("is_clothing") is not False
            and normalise_category(it.get("category")) == only_category
        ]
    else:
        # Pass 1 — local, free: honour the watchlist Non-clothing flag.
        local_non_clothing = 0
        for it in items:
            if it.get("is_clothing") is False:
                if refresh or normalise_category(it.get("category")) != NON_CLOTHING:
                    it["category"] = NON_CLOTHING
                    local_non_clothing += 1

        # Pass 2 — Claude for the rest. Newest purchases first so a --limit budget
        # covers the most relevant items.
        targets = [
            it for it in items
            if it.get("is_clothing") is not False and _needs_classify(it, refresh)
        ]
    targets.sort(key=lambda it: it.get("purchased_at") or "", reverse=True)
    if limit is not None:
        targets = targets[:limit]

    if not targets:
        stats = {
            "considered": 0, "classified": 0,
            "local_non_clothing": local_non_clothing, "usage": None,
        }
        log.info("order_classify: nothing to classify — %s", stats)
        return stats

    client = anthropic_client or order_classify._get_client(None)
    inputs = [
        {
            "item_id": it["id"],
            "name": it.get("item_name") or "",
            "shop": it.get("shop") or "",
            "size": it.get("size") or "",
            "color": it.get("color") or "",
        }
        for it in targets
    ]
    log.info("order_classify: classifying %d item(s) via Claude", len(inputs))
    result = order_classify.classify_items(inputs, client=client, batch_size=batch_size)
    by_id = {r["item_id"]: r["category"] for r in result.get("results") or []}

    classified = 0
    cat_counts: dict[str, int] = {}
    for it in targets:
        cat = by_id.get(it["id"])
        if not cat:
            continue
        it["category"] = cat
        if cat == NON_CLOTHING:
            it["is_clothing"] = False
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        classified += 1

    stats = {
        "considered": len(targets),
        "classified": classified,
        "local_non_clothing": local_non_clothing,
        "by_category": dict(sorted(cat_counts.items(), key=lambda kv: -kv[1])),
        "usage": result.get("usage"),
    }
    log.info("order_classify: done — %s", stats)
    return stats


# ---------------------------------------------------------------------------
# Product-URL re-harvest backfill (--reharvest-urls)
# ---------------------------------------------------------------------------

def _reharvest_targets(
    items: list[dict], *, refresh: bool, limit: int | None, since: str | None,
) -> list[dict]:
    """Clothing items eligible for a product_url re-harvest, newest first.

    Skips non-clothing (hidden in the browser anyway) and items with no
    ``order_email_id`` (can't re-fetch). Already-stamped items are skipped unless
    ``refresh``. ``since`` (``YYYY-MM-DD``) bounds by ``purchased_at`` and
    ``limit`` caps the count after the newest-first sort."""
    out: list[dict] = []
    for it in items:
        if it.get("is_clothing") is False:
            continue
        if not it.get("order_email_id"):
            continue
        if not refresh and (it.get("product_url") or "").strip():
            continue
        if since and (it.get("purchased_at") or "") < since:
            continue
        out.append(it)
    out.sort(key=lambda it: it.get("purchased_at") or "", reverse=True)
    return out[:limit] if limit is not None else out


def _fetch_product_links_by_msgids(
    cfg: Config, msgids: list[str], *, imap_client: imaplib.IMAP4 | None = None,
) -> dict[str, list[str]]:
    """Re-fetch each order email by X-GM-MSGID and harvest its product anchors.

    Returns ``{msgid: [product_url, ...]}`` (raw, un-domain-filtered — the caller
    filters per item's shop). One ``X-GM-MSGID`` SEARCH + ``BODY.PEEK`` FETCH per
    id; failure-isolated so one unreadable/expired message can't kill the batch.
    The msgid is the same decimal ``X-GM-MSGID`` stored as ``order_email_id``."""
    own = imap_client is None
    client = imap_client or _connect(cfg.gmail_username, cfg.gmail_app_password)
    out: dict[str, list[str]] = {}
    try:
        # Select All Mail, not INBOX: old order emails (2023–2024 purchases) are
        # usually archived, so an INBOX-scoped X-GM-MSGID search misses them and
        # those items silently get no candidate. X-GM-MSGID is globally unique, so
        # All Mail finds the message regardless of archive/label state. The mailbox
        # name has a space → it MUST be quoted (an unquoted select returns
        # "BAD Could not parse command").
        client.select('"[Gmail]/All Mail"', readonly=True)
        for msgid in msgids:
            try:
                typ, data = client.uid("SEARCH", "X-GM-MSGID", str(msgid))
            except imaplib.IMAP4.error as exc:
                log.info("reharvest: search msgid %s failed: %s", msgid, exc)
                continue
            if typ != "OK" or not data or not data[0]:
                continue
            uids = data[0].split()
            if not uids:
                continue
            try:
                typ, msg_data = client.uid("FETCH", uids[-1], "(X-GM-MSGID BODY.PEEK[])")
            except imaplib.IMAP4.error as exc:
                log.info("reharvest: fetch msgid %s failed: %s", msgid, exc)
                continue
            if typ != "OK":
                continue
            parsed = _parse_fetch_response(msg_data)
            if not parsed:
                continue
            _gm, raw_message = parsed
            try:
                msg = email.message_from_bytes(raw_message)
            except Exception as exc:  # noqa: BLE001 — defensive
                log.info("reharvest: parse msgid %s failed: %s", msgid, exc)
                continue
            out[msgid] = _harvest_anchor_urls(msg)
        return out
    finally:
        if own:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass


def _run_reharvest_urls(
    cfg: Config,
    wardrobe: dict,
    *,
    refresh: bool = False,
    limit: int | None = None,
    since: str | None = None,
    validate: bool = True,
    imap_client: imaplib.IMAP4 | None = None,
    url_validator=None,
) -> dict:
    """Backfill ``product_url`` on existing items by re-fetching their order
    emails, harvesting + matching the product link, and (by default) validating
    it's still live before stamping.

    Mutates ``wardrobe["items"]`` in place. A matched-but-dead URL is **not**
    stamped (left absent) so the browser keeps its always-working search link;
    rerunning re-tries it. ``url_validator`` / ``imap_client`` are injectable for
    tests. Returns counts: targeted / emails / matched / stamped / dead /
    no_candidate."""
    items = wardrobe.get("items") or []
    targets = _reharvest_targets(items, refresh=refresh, limit=limit, since=since)
    stats = {"targeted": len(targets), "emails": 0, "matched": 0,
             "stamped": 0, "dead": 0, "no_candidate": 0}
    if not targets:
        return stats

    msgids = list(dict.fromkeys(it["order_email_id"] for it in targets))
    log.info("reharvest: re-fetching %d order email(s) for %d item(s)",
             len(msgids), len(targets))
    links_by_msgid = _fetch_product_links_by_msgids(cfg, msgids, imap_client=imap_client)
    stats["emails"] = sum(1 for m in msgids if links_by_msgid.get(m))

    # Match each target to a candidate URL on its own shop domain.
    candidates: dict[str, str] = {}  # item_id -> url
    for it in targets:
        raw = links_by_msgid.get(it["order_email_id"]) or []
        cands = _links_for_domain(raw, it.get("shop_domain") or "")
        url = _match_product_url(it.get("item_name") or "", cands)
        if url:
            candidates[it["id"]] = url
        else:
            stats["no_candidate"] += 1
    stats["matched"] = len(candidates)

    # Validate liveness (default on) so we never stamp a dead link. The validator
    # maps each candidate to its canonical final URL (post-redirect) or None when
    # dead; --no-validate stamps the harvested URL as-is.
    if validate and candidates:
        validator = url_validator or _validate_urls
        resolved = validator(list(candidates.values()))
    else:
        resolved = {url: url for url in candidates.values()}

    by_id = {it["id"]: it for it in targets}
    for item_id, url in candidates.items():
        final = resolved.get(url)
        if final:
            by_id[item_id]["product_url"] = final
            stats["stamped"] += 1
        else:
            stats["dead"] += 1

    log.info("reharvest: done — %s", stats)
    return stats


# ---------------------------------------------------------------------------
# Targeted scrape (one hand-picked / forwarded email)
# ---------------------------------------------------------------------------

def _fetch_targeted(
    cfg: Config,
    *,
    query: str | None = None,
    msgids: list[str] | None = None,
    imap_client: imaplib.IMAP4 | None = None,
    max_messages: int = _IMAP_MAX_MESSAGES,
) -> list[dict]:
    """Fetch a hand-picked set of emails for the targeted-scrape mode.

    Searches ``"[Gmail]/All Mail"`` (not INBOX) so an archived / labelled
    forward is still found, and ignores the daily scan's subject pre-filter and
    dedup set — the caller has explicitly chosen these messages. ``query`` is a
    raw Gmail search string (X-GM-RAW); ``msgids`` are decimal X-GM-MSGIDs
    (already normalised by ``_normalise_msgid``). Returns the same
    ``{id, from, subject, body_text, date, message_id, product_links}`` dicts
    as ``_fetch_emails``; deduped by X-GM-MSGID across query + id matches.
    """
    own_client = imap_client is None
    client = imap_client or _connect(cfg.gmail_username, cfg.gmail_app_password)
    out: list[dict] = []
    seen: set[str] = set()
    try:
        # All Mail (quoted — the name has a space) so archived forwards resolve.
        client.select('"[Gmail]/All Mail"', readonly=True)

        uid_batches: list[list[bytes]] = []
        if query:
            log.info("order_scan: targeted query: %s", query)
            escaped = query.replace("\\", "\\\\").replace('"', '\\"')
            typ, data = client.uid("SEARCH", "X-GM-RAW", f'"{escaped}"')
            if typ == "OK" and data and data[0]:
                uids = data[0].split()
                if uids:
                    uid_batches.append(uids[-max_messages:])
        for mid in msgids or []:
            try:
                typ, data = client.uid("SEARCH", "X-GM-MSGID", str(mid))
            except imaplib.IMAP4.error as exc:
                log.info("order_scan: targeted search msgid %s failed: %s", mid, exc)
                continue
            if typ == "OK" and data and data[0]:
                uids = data[0].split()
                if uids:
                    uid_batches.append(uids)

        for uids in uid_batches:
            for uid in uids:
                try:
                    typ, msg_data = client.uid(
                        "FETCH", uid, "(X-GM-MSGID BODY.PEEK[])")
                except imaplib.IMAP4.error as exc:
                    log.info("order_scan: targeted fetch uid %s failed: %s", uid, exc)
                    continue
                if typ != "OK":
                    continue
                parsed = _parse_fetch_response(msg_data)
                if not parsed:
                    continue
                gm_msgid, raw_message = parsed
                if gm_msgid in seen:
                    continue
                seen.add(gm_msgid)
                try:
                    msg = email.message_from_bytes(raw_message)
                except Exception as exc:  # noqa: BLE001 — defensive
                    log.info("order_scan: targeted parse uid %s failed: %s", uid, exc)
                    continue
                parsed_em = _parse_message(gm_msgid, msg)
                parsed_em["product_links"] = _harvest_anchor_urls(msg)
                out.append(parsed_em)
        return out
    finally:
        if own_client:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass


def _prompt_origin(
    em: dict,
    origin: dict | None,
    *,
    default_shop: str = "",
    default_domain: str = "",
    default_date: str = "",
) -> tuple[str, str, str]:
    """Ask for shop name / domain / purchase date when a forward's origin
    couldn't be auto-parsed. Returns ``(shop, domain, purchased_at)``; raises
    ``_InteractiveAbort`` on Ctrl-C (so the run writes nothing)."""
    import questionary

    subject = (origin or {}).get("subject") or em.get("subject") or ""
    print(f"\n  Couldn't fully auto-detect this forwarded order:")
    print(f"    subject: {subject!r}")
    if origin and origin.get("from"):
        print(f"    sender:  {origin['from']}")

    shop = questionary.text("  Shop name:", default=default_shop).ask()
    if shop is None:
        raise _InteractiveAbort("targeted scrape cancelled")
    domain = questionary.text(
        "  Shop domain (e.g. riotgames.com):", default=default_domain).ask()
    if domain is None:
        raise _InteractiveAbort("targeted scrape cancelled")
    date = questionary.text(
        "  Purchase date (YYYY-MM-DD):", default=default_date).ask()
    if date is None:
        raise _InteractiveAbort("targeted scrape cancelled")
    date = date.strip()
    return (shop.strip(), domain.strip().lower(), _parse_forwarded_date(date) or date)


def _run_message_scan(
    cfg: Config,
    wardrobe: dict,
    *,
    query: str | None,
    msgids: list[str] | None,
    shop_name: str | None = None,
    shop_domain: str | None = None,
    purchased_at: str | None = None,
    shop_aliases: dict[str, str] | None = None,
    imap_client: imaplib.IMAP4 | None = None,
    anthropic_client=None,
    prompt: bool = True,
) -> tuple[list[dict], list[str]]:
    """Targeted-scrape mode: ingest a hand-picked set of (usually forwarded)
    emails into the wardrobe by Gmail query and/or message id.

    Every matched email is treated as an order (the user hand-picked it — we
    bypass the ``_classify`` heuristic so a ``Fwd:`` subject can't drop it).
    Shop + purchase date come from explicit overrides, else the forwarded
    header (``_forwarded_origin``), else an interactive prompt. Returns
    ``(new_items, processed_email_ids)`` for the caller to merge; matched ids
    are always recorded so the daily INBOX sweep can't re-ingest them.
    """
    shop_aliases = shop_aliases or {}
    emails = _fetch_targeted(cfg, query=query, msgids=msgids, imap_client=imap_client)
    if not emails:
        log.info("order_scan: targeted scrape matched no emails")
        return [], []
    log.info("order_scan: targeted scrape — %d email(s) matched", len(emails))

    order_records: list[dict] = []
    processed_ids: list[str] = []
    for em in emails:
        eid = em.get("id")
        if not eid:
            continue
        processed_ids.append(eid)
        body = em.get("body_text", "") or ""
        origin = _forwarded_origin(body)

        # Shop: explicit override → forwarded header → (prompt below).
        shop = domain = ""
        if shop_name or shop_domain:
            shop = (shop_name or "").strip()
            domain = (shop_domain or "").strip().lower()
        elif origin and origin.get("from"):
            shop, domain = resolve_shop(origin["from"], shop_aliases)

        # Purchase date: override → forwarded header → (prompt below).
        pdate = (purchased_at or "").strip()
        if not pdate and origin:
            pdate = origin.get("date") or ""

        if not shop or not domain or not pdate:
            if prompt:
                shop, domain, pdate = _prompt_origin(
                    em, origin, default_shop=shop,
                    default_domain=domain, default_date=pdate)
            else:
                # Non-interactive last-ditch fallback so a headless run still
                # produces something rather than aborting.
                if not shop and origin and origin.get("from"):
                    shop, domain = resolve_shop(origin["from"], shop_aliases)
                if not pdate:
                    pdate = _date_from_header(em.get("date") or "")
                log.warning(
                    "order_scan: no TTY to prompt — using best-effort "
                    "shop=%r domain=%r date=%r", shop, domain, pdate)

        if is_excluded_shop(shop, domain, cfg.excluded_shops):
            log.info("order_scan: skipping excluded shop %r", shop)
            continue

        # The original subject gives Claude better context than "Fwd: …", and
        # stripping the forward preamble lets the excerpt cover the item list.
        subject = (origin or {}).get("subject") or em.get("subject", "")
        item_body = _strip_forward_preamble(body)
        order_records.append({
            "email_id": eid,
            "from": (origin or {}).get("from") or em.get("from", ""),
            "subject": subject,
            "body_excerpt": _excerpt(item_body),
            "purchased_at": pdate,
            "shop": shop,
            "shop_domain": domain,
            "product_links": _links_for_domain(
                em.get("product_links") or [], domain),
        })

    if not order_records:
        return [], processed_ids

    from src.order_extract import extract_items
    claude_input = [{
        "email_id": r["email_id"], "from": r["from"],
        "subject": r["subject"], "body_excerpt": r["body_excerpt"],
    } for r in order_records]
    log.info("order_scan: calling Claude for items extraction (targeted)")
    extracted = extract_items(claude_input, client=anthropic_client)
    log.info("order_scan: claude usage = %s", extracted.get("usage"))

    meta_by_id = {
        r["email_id"]: {
            "shop": r["shop"], "shop_domain": r["shop_domain"],
            "purchased_at": r["purchased_at"],
        }
        for r in order_records
    }
    links_by_id = {r["email_id"]: r.get("product_links") or [] for r in order_records}
    items = _materialise_items(extracted.get("orders", []), meta_by_id, links_by_id)
    log.info("order_scan: targeted scrape — materialised %d item(s)", len(items))
    return items, processed_ids


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="order_scan")
    p.add_argument(
        "--scan-only", action="store_true",
        help="Scan Gmail + persist items; skip interactive prompts.",
    )
    p.add_argument(
        "--review-fits", action="store_true",
        help="Skip scanning; walk pending fit reviews only.",
    )
    p.add_argument(
        "--match-watchlist", action="store_true",
        help="Skip scanning; walk pending watchlist-match approvals only.",
    )
    p.add_argument(
        "--no-scan", action="store_true",
        help="Skip the Gmail scan; run both interactive passes "
             "(watchlist approvals + fit review) against existing items.",
    )
    p.add_argument(
        "--since", default=None,
        help="ISO date (YYYY-MM-DD) override for the scan window start. "
             "Default: last_scanned_at from state, or 3 years ago on first run.",
    )
    p.add_argument(
        "--shop", default=None,
        help="Narrow this run to emails whose From/Subject contains this string.",
    )
    p.add_argument(
        "--message-query", default=None, metavar="GMAIL_QUERY",
        help="Targeted-scrape mode: ingest the email(s) matching this raw Gmail "
             "search string (searched over All Mail), bypassing the date window "
             "/ subject pre-filter / interactive passes. Built for forwarding an "
             "old order email to yourself then scraping just it — the original "
             "shop + purchase date are recovered from the forwarded header (else "
             "you're prompted). e.g. --message-query \"newer_than:1d from:me\".",
    )
    p.add_argument(
        "--message-id", action="append", default=None, metavar="X_GM_MSGID",
        help="Targeted-scrape mode: ingest the email with this exact X-GM-MSGID "
             "(decimal, hex, or a pasted #all/<hex> digest permalink). Repeatable "
             "and/or comma-separated. Combines with --message-query.",
    )
    p.add_argument(
        "--shop-name", default=None,
        help="Targeted scrape: force the shop name (skips forwarded-header "
             "auto-detect + the prompt). Pair with --shop-domain.",
    )
    p.add_argument(
        "--shop-domain", default=None,
        help="Targeted scrape: force the shop domain (e.g. riotgames.com).",
    )
    p.add_argument(
        "--purchased-at", default=None, metavar="YYYY-MM-DD",
        help="Targeted scrape: force the purchase date (skips auto-detect + "
             "the prompt for the date).",
    )
    p.add_argument(
        "--reprocess", default=None, metavar="STRING",
        help="Recover a shop whose order emails got stuck in the skip-set: "
             "un-skip its order/shipping emails (those matching STRING) and "
             "re-scan them, filtered to STRING, over a wide window. Use when a "
             "prior --shop/--max-emails run marked emails processed without "
             "extracting them. Honors --since (else 3 years back); leaves "
             "last_scanned_at untouched.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Run everything but skip the final Gist write.",
    )
    p.add_argument(
        "--max-emails", type=int, default=None,
        help="Cap how many classified emails are sent to Claude this run. "
             "Useful for smoke tests and incremental backfills. Caps orders "
             "and shipments proportionally — newest first.",
    )
    p.add_argument(
        "--backfill-bodycomp", action="store_true",
        help="Skip scanning; stamp BodySpec DEXA body-composition onto recent "
             "clothing items, matched to each item's fit-review date when it has "
             "one (else its purchase date). Re-runs re-match newly reviewed "
             "items. Matches from the cached scan store (body_scans.json); pass "
             "--refresh-scans to pull from BodySpec first (the only path that "
             "needs BODYSPEC_USERNAME / BODYSPEC_PASSWORD).",
    )
    p.add_argument(
        "--classify", action="store_true",
        help="Skip scanning; stamp a durable garment `category` (and derived "
             "`is_clothing`) onto stored items via Claude, so the wardrobe "
             "browser reads a real category instead of guessing from the name "
             "(issue #18). Already-classified items are skipped unless "
             "--refresh is given; --limit caps how many are sent to Claude "
             "(default: all, newest first). Needs ANTHROPIC_API_KEY.",
    )
    p.add_argument(
        "--reharvest-urls", action="store_true",
        help="Skip scanning; backfill `product_url` on existing items by "
             "re-fetching their order emails, harvesting the product link, and "
             "validating it's still live before stamping (issue #23). Items "
             "already stamped are skipped unless --refresh; --limit caps how "
             "many (newest first), --since bounds by purchase date. A dead / "
             "redirected link is NOT stamped (the browser keeps its search "
             "link). Needs GMAIL_USERNAME / GMAIL_APP_PASSWORD.",
    )
    p.add_argument(
        "--no-validate", action="store_true",
        help="With --reharvest-urls: stamp every matched URL without the live "
             "HTTP check (faster, hits no shops — but may stamp a dead link).",
    )
    p.add_argument(
        "--reclassify-category", default=None, metavar="KEY",
        help="With --classify: re-run Claude over ONLY the items currently "
             "stored as this taxonomy category KEY (e.g. `shorts`), retyping "
             "just that bucket — the cheap way to redistribute one category "
             "after a taxonomy split, without re-billing the whole catalogue. "
             "Composes with --limit; ignores --refresh.",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Cap how many items a backfill processes, newest first. "
             "--backfill-bodycomp defaults to 100; --classify defaults to all.",
    )
    p.add_argument(
        "--max-gap-days", type=int, default=90,
        help="Skip an item when the closest DEXA scan is more than this many "
             "days from its purchase date (default 90).",
    )
    p.add_argument(
        "--refresh", action="store_true",
        help="Re-stamp body_comp even on items that already have it "
             "(e.g. after a newer scan lands).",
    )
    p.add_argument(
        "--refresh-scans", action="store_true",
        help="Force a live BodySpec pull to rebuild the cached scan store "
             "(body_scans.json) before backfilling. Without this, backfill "
             "matches from the existing cache and never authenticates. "
             "Requires BODYSPEC_USERNAME / BODYSPEC_PASSWORD.",
    )
    return p.parse_args(argv)


def _is_dry_run(cli_flag: bool) -> bool:
    if cli_flag:
        return True
    return (os.environ.get("SALE_CHECK_DRY_RUN") or "").strip().lower() in _TRUTHY


def run(argv: list[str] | None = None, cfg: Config | None = None) -> int:
    args = _parse_args(argv)
    cfg = cfg or load_config()
    dry_run = _is_dry_run(args.dry_run)

    log.info("order_scan: reading state from gist")
    state = read_state(cfg.gist_id, cfg.github_token)
    wardrobe = _normalise(state.get("wardrobe"))

    # Privacy filter: hard-delete any already-stored items from EXCLUDED_SHOPS
    # before doing anything else, so the exclusion self-heals on every run mode
    # (the ingestion guard in _run_scan keeps new ones out). The standard write
    # paths below persist the pruned wardrobe.
    dropped = _drop_excluded_items(wardrobe, cfg.excluded_shops)
    if dropped:
        log.info("order_scan: dropped %d item(s) from excluded shops", dropped)

    # Category classification backfill is its own mode — never touches Gmail or
    # the interactive passes. Short-circuit before any scanning happens.
    if args.classify:
        only_category = args.reclassify_category
        if only_category is not None:
            only_category = normalise_category(only_category)
            if only_category is None:
                log.error(
                    "order_scan: --reclassify-category must be a known taxonomy "
                    "key (e.g. shorts). Got %r.", args.reclassify_category,
                )
                return 1
        stats = _run_classify(
            cfg, wardrobe, refresh=args.refresh, limit=args.limit,
            only_category=only_category,
        )
        if dry_run:
            log.info("order_scan: DRY_RUN — skipping write_state")
            previews = [
                {k: it.get(k) for k in ("item_name", "shop", "size", "category", "is_clothing")}
                for it in wardrobe["items"] if it.get("category")
            ][:20]
            log.info(
                "order_scan: category preview (first %d):\n%s",
                len(previews), json.dumps(previews, indent=2, default=str)[:4000],
            )
        else:
            log.info("order_scan: writing wardrobe to gist")
            write_state(
                cfg.gist_id,
                cfg.github_token,
                prices=state.get("prices") or {},
                aliases=state.get("aliases") or {},
                codes=state.get("codes") or [],
                wardrobe=wardrobe,
            )
        log.info("order_scan: classify summary — %s", stats)
        return 0

    # Product-URL re-harvest is its own mode — re-fetches existing items' order
    # emails (no forward scan, no Claude, no interactive passes) to backfill
    # product_url. Short-circuit before any scanning happens.
    if args.reharvest_urls:
        since = None
        if args.since:
            try:
                datetime.fromisoformat(args.since)  # validate shape only
            except ValueError:
                log.error("invalid --since value (need YYYY-MM-DD): %r", args.since)
                return 2
            since = args.since
        stats = _run_reharvest_urls(
            cfg, wardrobe, refresh=args.refresh, limit=args.limit,
            since=since, validate=not args.no_validate,
        )
        if dry_run:
            log.info("order_scan: DRY_RUN — skipping write_state")
            previews = [
                {"item_name": it.get("item_name"), "shop": it.get("shop"),
                 "product_url": it.get("product_url")}
                for it in wardrobe["items"] if (it.get("product_url") or "").strip()
            ][:20]
            log.info(
                "order_scan: product_url preview (first %d stamped):\n%s",
                len(previews), json.dumps(previews, indent=2, default=str)[:4000],
            )
        else:
            log.info("order_scan: writing wardrobe to gist")
            write_state(
                cfg.gist_id,
                cfg.github_token,
                prices=state.get("prices") or {},
                aliases=state.get("aliases") or {},
                codes=state.get("codes") or [],
                wardrobe=wardrobe,
            )
        log.info("order_scan: reharvest summary — %s", stats)
        return 0

    # Targeted scrape is its own mode — ingest a hand-picked set of (usually
    # forwarded) emails by Gmail query and/or message id, without the date
    # window / subject pre-filter / interactive review passes. The user opted
    # for "just store" here, so it writes the items and stops (run --review-fits
    # later for fit reviews). Short-circuit before any forward scanning happens.
    if args.message_query or args.message_id:
        if args.purchased_at:
            try:
                datetime.fromisoformat(args.purchased_at)
            except ValueError:
                log.error(
                    "invalid --purchased-at (need YYYY-MM-DD): %r", args.purchased_at)
                return 2
        msgids: list[str] = []
        for raw in (args.message_id or []):
            for tok in str(raw).split(","):
                norm = _normalise_msgid(tok)
                if norm:
                    msgids.append(norm)
                elif tok.strip():
                    log.warning(
                        "order_scan: ignoring unparseable --message-id %r", tok)
        try:
            new_items, processed_ids = _run_message_scan(
                cfg, wardrobe,
                query=args.message_query, msgids=msgids,
                shop_name=args.shop_name, shop_domain=args.shop_domain,
                purchased_at=args.purchased_at,
                shop_aliases=state.get("aliases") or {},
                prompt=sys.stdin.isatty(),
            )
        except (_InteractiveAbort, KeyboardInterrupt) as exc:
            log.warning(
                "order_scan: targeted scrape aborted (%s) — not writing to gist",
                exc or "Ctrl-C")
            return 1
        log.info("order_scan: targeted scrape extracted %d new item(s)", len(new_items))

        if new_items:
            watchlist_text = ""
            try:
                watchlist_text = fetch_watchlist(cfg.watchlist_url)
            except Exception as exc:  # noqa: BLE001 — watchlist fetch is best-effort
                log.warning("order_scan: watchlist fetch failed: %s", exc)
            if watchlist_text:
                _match_watchlist(new_items, watchlist_text)

        wardrobe["items"] = _merge_items(wardrobe["items"], new_items)
        now_iso = datetime.now(timezone.utc).isoformat()
        for eid in processed_ids:
            wardrobe["scan_state"]["processed_email_ids"][eid] = now_iso
        # last_scanned_at deliberately untouched — this is a targeted backfill,
        # not a forward scan of the window.

        if dry_run:
            log.info("order_scan: DRY_RUN — skipping write_state")
            preview = [
                {k: it.get(k) for k in (
                    "item_name", "shop", "size", "color", "price_paid",
                    "purchased_at", "category")}
                for it in new_items
            ][:20]
            log.info(
                "order_scan: targeted preview (%d new):\n%s",
                len(preview), json.dumps(preview, indent=2, default=str)[:4000])
        else:
            log.info("order_scan: writing wardrobe to gist")
            write_state(
                cfg.gist_id,
                cfg.github_token,
                prices=state.get("prices") or {},
                aliases=state.get("aliases") or {},
                codes=state.get("codes") or [],
                wardrobe=wardrobe,
            )
        log.info(
            "order_scan: targeted scrape done — %d new item(s), total %d",
            len(new_items), len(wardrobe["items"]))
        return 0

    # BodySpec body-comp backfill is its own mode — never touches Gmail or the
    # interactive passes. Short-circuit before any scanning happens.
    if args.backfill_bodycomp:
        stats, body_scans_out = _run_bodycomp_backfill(
            cfg, wardrobe,
            limit=args.limit if args.limit is not None else 100,
            max_gap_days=args.max_gap_days, refresh=args.refresh,
            scans=(state.get("body_scans") or {}).get("scans"),
            refresh_scans=args.refresh_scans,
        )
        if dry_run:
            log.info("order_scan: DRY_RUN — skipping write_state")
            previews = [it for it in wardrobe["items"] if it.get("body_comp")][:3]
            log.info(
                "order_scan: body_comp preview (first %d stamped):\n%s",
                len(previews), json.dumps(previews, indent=2, default=str)[:4000],
            )
        else:
            log.info("order_scan: writing wardrobe to gist")
            write_state(
                cfg.gist_id,
                cfg.github_token,
                prices=state.get("prices") or {},
                aliases=state.get("aliases") or {},
                codes=state.get("codes") or [],
                wardrobe=wardrobe,
                body_scans=body_scans_out,
            )
        log.info("order_scan: backfill summary — %s", stats)
        return 0

    do_scan = not (args.review_fits or args.match_watchlist or args.no_scan)

    if do_scan:
        since_override: datetime | None = None
        if args.since:
            try:
                since_override = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
            except ValueError:
                log.error("invalid --since value (need YYYY-MM-DD): %r", args.since)
                return 2

        if args.reprocess:
            # Recovery mode: un-skip this shop's previously-burned order emails,
            # then scan a wide window filtered to it. last_scanned_at is left
            # untouched — this is a targeted backfill, not a forward scan.
            unskipped = _unskip_matching(cfg, wardrobe, args.reprocess)
            log.info(
                "order_scan: --reprocess %r — un-skipped %d email(s)",
                args.reprocess, unskipped,
            )
            since = since_override or (
                datetime.now(timezone.utc)
                - timedelta(days=365 * _DEFAULT_LOOKBACK_YEARS)
            )
            shop_filter = args.reprocess
        else:
            since = _since_from_state(wardrobe, since_override)
            shop_filter = args.shop

        log.info(
            "order_scan: scanning Gmail since %s%s",
            since.date().isoformat(),
            f" (shop filter {shop_filter!r})" if shop_filter else "",
        )

        new_items, processed_ids = _run_scan(
            cfg, wardrobe, since, shop_filter=shop_filter,
            max_emails=args.max_emails,
            shop_aliases=state.get("aliases") or {},
        )
        log.info("order_scan: extracted %d new item(s)", len(new_items))

        if new_items:
            watchlist_text = ""
            try:
                watchlist_text = fetch_watchlist(cfg.watchlist_url)
            except Exception as exc:  # noqa: BLE001 — watchlist fetch is best-effort
                log.warning("order_scan: watchlist fetch failed: %s", exc)
            if watchlist_text:
                _match_watchlist(new_items, watchlist_text)

        wardrobe["items"] = _merge_items(wardrobe["items"], new_items)
        now_iso = datetime.now(timezone.utc).isoformat()
        for eid in processed_ids:
            wardrobe["scan_state"]["processed_email_ids"][eid] = now_iso
        if not args.reprocess:
            wardrobe["scan_state"]["last_scanned_at"] = now_iso

    aborted = False
    if not args.scan_only:
        try:
            if args.match_watchlist or not args.review_fits:
                _interactive_watchlist_approval(wardrobe["items"], wardrobe)
            if args.review_fits or not args.match_watchlist:
                _interactive_fit_review(
                    wardrobe["items"], wardrobe.get("shop_fit_notes"),
                )
        except (_InteractiveAbort, KeyboardInterrupt) as exc:
            log.warning(
                "order_scan: interactive review aborted (%s) — NOT writing "
                "to gist. Any partial decisions you made this session will "
                "not persist.", exc or "Ctrl-C",
            )
            aborted = True

    if aborted:
        return 1

    if dry_run:
        log.info("order_scan: DRY_RUN — skipping write_state")
        log.info("order_scan: wardrobe preview:\n%s",
                 json.dumps(wardrobe, indent=2, default=str)[:4000])
    else:
        log.info("order_scan: writing wardrobe to gist")
        write_state(
            cfg.gist_id,
            cfg.github_token,
            prices=state.get("prices") or {},
            aliases=state.get("aliases") or {},
            codes=state.get("codes") or [],
            wardrobe=wardrobe,
        )

    log.info(
        "order_scan: done — total items=%d (with fit_review=%d, "
        "approved-removals=%d)",
        len(wardrobe["items"]),
        sum(1 for it in wardrobe["items"] if it.get("fit_review")),
        len(wardrobe["watchlist_exclusions"]),
    )
    return 0


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("SALE_CHECK_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        from dotenv import load_dotenv
        load_dotenv(override=True)
    except ImportError:
        pass
    try:
        sys.exit(run())
    except Exception:
        log.exception("order_scan run failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
