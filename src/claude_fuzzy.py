"""Batched Claude API call for the four fuzzy steps in CLAUDE.md.

One call per run handles:
  * Step 5 — shop-homepage sale detection (yes / no / unclear + description)
  * Step 2 — shop-name -> URL resolution (when not cached in shop_aliases.json)
  * Step 6 — loose-mention -> product URL matching
  * Gmail — judge whether each Promotions-tab email announces a real sale
            (issue #9). Codes are extracted deterministically; only the
            sale-signal interpretation needs Claude.

Architecture
------------
Python gathers candidates (homepage HTML, DuckDuckGo results for shop names,
on-site /search?q= hits for loose mentions) and feeds them to Claude as a
single JSON payload. Claude responds via a forced ``submit_results`` tool call
whose ``input_schema`` matches the result shape exactly — no free-form JSON
parsing, no markdown stripping.

The system prompt carries the full rubric and is marked with
``cache_control: ephemeral`` so daily runs hit the 5-minute prompt cache when
the run is re-invoked (e.g. retries) and pay roughly nothing for the rubric
across the batch of three task types in the same call.

Public API
----------
    resolve_fuzzy(
        shops_to_check, shops_to_resolve, loose_mentions,
        email_signals=None, *, client=None, model="claude-sonnet-4-6",
    ) -> FuzzyResult

Inputs are plain lists of dicts:

    shops_to_check    = [{"shop": str, "url": str}, ...]
    shops_to_resolve  = [str, ...]                       # shop-name strings
    loose_mentions    = [{"mention": str, "shop": str,
                          "shop_domain": str}, ...]
    email_signals     = [{"email_id": str, "shop": str,
                          "subject": str, "body_excerpt": str,
                          "email_date": str}, ...]   # email_date: ISO date

Output:

    {
        "shop_sales":    [{"shop", "status": yes|no|unclear,
                           "description": str|None}],
        "resolutions":   [{"shop_name", "url": str|None,
                           "confidence": high|low|none}],
        "loose_matches": [{"mention", "shop", "matched_url": str|None,
                           "confidence": high|low|none}],
        "email_sales":   [{"email_id", "shop",
                           "status": yes|no|unclear,
                           "description": str|None,
                           "starts_on": str|None,    # ISO YYYY-MM-DD
                           "ends_on": str|None}],
        "unresolved":    [str, ...],   # shop names where no candidates surfaced
        "shop_verdicts": [{"shop", "hash", "status", "description"}],
                          # fresh homepage verdicts judged this run (cache
                          # misses) for the caller to persist — cost lever #3
        "usage":         {"input_tokens", "output_tokens",
                          "cache_read_input_tokens",
                          "cache_creation_input_tokens"} | None,
    }

If all four input lists are empty the function short-circuits and returns the
empty-result skeleton without calling the API.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from src import extract, shop_verdicts
from src.http_util import RateLimiter, get_with_retry

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-sonnet-4-6"
# Output cap for the single batched submit_results call. 4096 was being hit
# exactly on heavy days, truncating the tool-call JSON mid-object; 8192 doubles
# the headroom and is still safe non-streaming (Sonnet 4.6 caps at 64K output,
# but the SDK risks HTTP timeouts above ~16K without streaming). _call_claude
# logs a warning if a response ever hits this cap so future truncation is loud.
MAX_TOKENS = 8192

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
_TIMEOUT = 15.0

# chars of cleaned visible text per shop. This is the dominant input-token
# cost — one excerpt per watchlist shop, on-sale or not. Promo/announcement
# bars sit at the top of body text (the first ~1500 chars), so the head slice
# carries almost every real signal — and the excerpt is promo-region-
# prioritised (cost lever #4): sale-signal matches PAST the head slice are
# appended as small context windows, so shrinking the head from the old flat
# 2500 loses no recall (it gains some — a promo past the old cutoff was
# invisible before).
_HOMEPAGE_TEXT_LIMIT = 1500
# Context kept either side of a sale-signal match found past the head slice.
_PROMO_WINDOW_RADIUS = 120
# Combined budget for those appended windows. 900 fits ~3-4 distinct promo
# regions; worst case the excerpt totals head + windows ≈ the old flat 2500,
# and only on signal-dense pages that genuinely need the space.
_PROMO_WINDOWS_LIMIT = 900
_SEARCH_RESULT_LIMIT = 5      # candidates per shop-resolve / loose-mention task

_DDG_HTML = "https://html.duckduckgo.com/html/"

# Homepage + on-site-search fetches are sequential (one shop at a time) but had
# no inter-request gap, so a run of Shopify-hosted shops bursts past the same
# per-IP rate limit that hits product extraction. Gated at 5 s to match the
# product path — deliberately conservative, since the homepage path is a single
# request per shop (vs the product extract's ~2.3-request burst), so 5 s here is
# actually *more* cautious than the product gate in averaged-rate terms. The
# Retry-After-aware retry inside get_with_retry absorbs any residual 429
# adaptively. Disabled in tests by the conftest fixture that zeroes the interval.
_HOMEPAGE_LIMITER = RateLimiter(5.0)


# ---------------------------------------------------------------------------
# Homepage fetch + text cleanup (Step 5 candidate gathering)
# ---------------------------------------------------------------------------

def _http_get(
    url: str, *, client: httpx.Client | None = None,
) -> httpx.Response | None:
    """GET ``url`` through the homepage limiter; return the Response or None.

    Paces sequential fetches through ``_HOMEPAGE_LIMITER`` and retries 429/503
    honoring ``Retry-After`` (``get_with_retry``) so a burst of same-platform
    (Shopify) shops doesn't trip a per-IP rate limit. Returns None only on a
    transport-level error (the caller classifies the HTTP status itself).
    """
    _HOMEPAGE_LIMITER.acquire()
    try:
        if client is not None:
            return get_with_retry(client, url, headers=_HEADERS, timeout=_TIMEOUT,
                                  follow_redirects=True)
        with httpx.Client(timeout=_TIMEOUT) as c:
            return get_with_retry(c, url, headers=_HEADERS, follow_redirects=True)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        log.info("claude_fuzzy: fetch %s failed: %s", url, exc)
        return None


def _fetch(url: str, *, client: httpx.Client | None = None) -> str | None:
    """GET ``url`` and return the response text, or None on any failure."""
    resp = _http_get(url, client=client)
    if resp is None:
        return None
    if resp.status_code >= 400:
        log.info("claude_fuzzy: fetch %s -> %s", url, resp.status_code)
        return None
    return resp.text


def _fetch_via_reader_proxy(url: str) -> str | None:
    """Recover a Cloudflare-blocked homepage's visible text via the reader proxy.

    Some shops run Cloudflare Bot Fight Mode, which 403/503s the GitHub Actions
    datacenter IP (where the cron runs) while serving residential IPs normally,
    so the homepage sale-check resolves to "could not fetch homepage" every run
    (issues #1/#2). The reader proxy fetches from its own un-blocked egress and
    returns the page's *visible text* — which is exactly what the homepage
    excerpt needs (no HTML/anchor parsing required, unlike the on-site search) —
    so a blocked shop's promo signal is recovered instead of being lost. Shares
    the ``READER_PROXY_*`` config + kill-switch with the product-path fallback
    in ``extract.py`` (referenced through the module so the env toggle and test
    monkeypatches both apply). Failure-isolated: returns None on any miss.
    """
    if not extract._PROXY_FALLBACK_ENABLED:
        return None
    return extract._fetch_via_proxy(url)


def _fetch_homepage(url: str, *, client: httpx.Client | None = None) -> str | None:
    """Fetch a shop homepage's text, with a reader-proxy fallback on a block.

    Same direct GET as ``_fetch``, but when the shop returns a Cloudflare-style
    block (403/503 — which blocks the datacenter IP while serving residential
    IPs fine) we retry through the reader proxy and return the recovered visible
    text, so the homepage sale-check still gets a signal instead of resolving to
    "could not fetch homepage" on every run (issues #1/#2). Non-block failures
    (404, DNS, timeout) return None as before — a different egress wouldn't fix
    those. The proxy hop fires off-path (only after a real block), so the happy
    path pays nothing.
    """
    resp = _http_get(url, client=client)
    if resp is None:
        return None
    if resp.status_code < 400:
        return resp.text
    log.info("claude_fuzzy: fetch %s -> %s", url, resp.status_code)
    if resp.status_code in (403, 503):
        text = _fetch_via_reader_proxy(url)
        if text:
            log.info("claude_fuzzy: recovered blocked homepage via reader proxy: %s",
                     url)
        return text
    return None


def _homepage_excerpt(html: str) -> str:
    """Reduce homepage HTML to a Claude-friendly visible-text excerpt.

    Strips <script>/<style>/<noscript>/<svg>, collapses whitespace, and keeps
    the first ``_HOMEPAGE_TEXT_LIMIT`` chars — promo/announcement bars sit at
    the top of body text, so the head slice carries almost every real signal.

    Text past the head isn't blindly dropped though (cost lever #4, the
    promo-region-prioritised excerpt): every ``_SALE_SIGNAL_RE`` match beyond
    the slice is appended as a small context window (``_promo_windows``), so a
    promo announced mid-page — invisible under the old flat slice — is still
    seen by the pre-filter, the verdict hash, and Claude, at a fraction of the
    cost of a bigger flat cap.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    body = soup.body or soup
    text = body.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    if len(text) <= _HOMEPAGE_TEXT_LIMIT:
        return text
    excerpt = text[:_HOMEPAGE_TEXT_LIMIT] + " ...[truncated]"
    windows = _promo_windows(text, _HOMEPAGE_TEXT_LIMIT)
    if windows:
        excerpt += (" [sale mentions further down the page:] "
                    + " ... ".join(windows))
    return excerpt


def _promo_windows(text: str, head_end: int) -> list[str]:
    """Context windows around sale-signal matches past the head slice.

    Returns document-order snippets of ±``_PROMO_WINDOW_RADIUS`` chars around
    each ``_SALE_SIGNAL_RE`` match not already fully inside ``text[:head_end]``
    (a match straddling the boundary keeps its whole window so the lexeme isn't
    split in half). Overlapping/adjacent windows are merged so a promo region
    with several signals costs one snippet. Combined length is capped at
    ``_PROMO_WINDOWS_LIMIT``: whole windows are appended until one doesn't fit;
    if even the FIRST window overflows the budget it's clipped rather than
    dropped, so an out-of-head signal is never invisible to the pre-filter.
    """
    spans: list[list[int]] = []
    for m in _SALE_SIGNAL_RE.finditer(text):
        if m.end() <= head_end:
            continue  # already fully visible in the head slice
        start = max(0, m.start() - _PROMO_WINDOW_RADIUS)
        end = min(len(text), m.end() + _PROMO_WINDOW_RADIUS)
        if spans and start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])

    out: list[str] = []
    used = 0
    for start, end in spans:
        remaining = _PROMO_WINDOWS_LIMIT - used
        if remaining <= 0:
            break
        if end - start > remaining:
            if not out:
                out.append(text[start:start + remaining])
            break
        out.append(text[start:end])
        used += end - start
    return out


# Deterministic "no-signal" pre-filter (cost lever #1). A homepage whose
# visible text contains NO sale lexeme can only ever be judged "no" — the model
# sees the same text-only excerpt and would have nothing to go on — so we skip
# the Claude task for it and record "no" locally. The digest is byte-identical;
# the only difference is we don't pay to confirm the obvious. The lexicon is
# deliberately a SUPERSET of every signal SYSTEM_PROMPT treats as sale-ish:
# over-matching only sends an extra shop to Claude (harmless), while a miss here
# would silently drop a real sale, so err broad. Bare percentages are excluded
# on purpose ("100% cotton"/"100% organic" must NOT count) — a percentage only
# signals when followed by "off".
_SALE_SIGNAL_RE = re.compile(
    r"""(?:
        \d+\s*%\s*off                      # 30% off, up to 50% off
      | \bsales?\b                         # sale / sales / "Spring Sale"
      | \bbogo\b                           # buy-one-get-one
      | \bclearance\b
      | \bdiscount(?:s|ed)?\b
      | \bcoupons?\b
      | \bpromo(?:tion|tions|tional)?\b
      | \bdeals?\b
      | \bsave\s+(?:\$?\d|up\sto)          # save 20 / save $20 / save up to
      | \boutlet\b
      | \bmarkdowns?\b
      | \bblowout\b
      | \bflash\s+sale\b
      | \b(?:promo|coupon|discount)\s*code\b
      | \buse\s+code\b
      | \btoday\s+only\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _has_sale_signal(text: str) -> bool:
    """True if the homepage excerpt contains any sale-ish lexeme.

    See ``_SALE_SIGNAL_RE`` — when this returns False the homepage is recorded
    "no" without a Claude round-trip.
    """
    return bool(_SALE_SIGNAL_RE.search(text or ""))


def _verdict_hash(excerpt: str) -> str:
    """Content fingerprint for the homepage verdict cache (cost lever #3).

    Hashes only the **sale-signal substrings** ``_SALE_SIGNAL_RE`` matches in
    the excerpt — not the whole text — so volatile non-promo junk (live cart
    counts, "12 people viewing", rotating carousels) doesn't change the hash,
    while any change to the actual promo wording (a sale starting, the discount
    going 30→50%, the sale ending) does. Each match is lowercased and inner
    whitespace collapsed before joining so trivial spacing differences don't
    bust the cache. ``src/shop_verdicts.py`` consumes the hash; this is only
    reached after ``_has_sale_signal`` is True, so there's always ≥1 match.

    Uses ``finditer``/``group(0)`` rather than ``findall`` so it stays correct
    if a capturing group is ever added to the shared ``_SALE_SIGNAL_RE``
    (``findall`` would then return group tuples and silently change the hash).
    """
    signals = [m.group(0) for m in _SALE_SIGNAL_RE.finditer(excerpt or "")]
    normalized = " ".join(
        re.sub(r"\s+", " ", s.strip().lower()) for s in signals
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Shop-name resolution via DuckDuckGo HTML search (Step 2 candidate gathering)
# ---------------------------------------------------------------------------

def _ddg_search(query: str, *, client: httpx.Client | None = None) -> list[dict]:
    """Return up to ``_SEARCH_RESULT_LIMIT`` candidate sites for ``query``.

    Each candidate: {"url": str, "title": str, "snippet": str}. Empty list on
    failure (network error, parse error, blocked, empty results) — caller
    treats that as "could not resolve".
    """
    params = {"q": query}
    try:
        if client is not None:
            resp = client.post(_DDG_HTML, data=params, headers=_HEADERS,
                               timeout=_TIMEOUT, follow_redirects=True)
        else:
            with httpx.Client(timeout=_TIMEOUT) as c:
                resp = c.post(_DDG_HTML, data=params, headers=_HEADERS,
                              follow_redirects=True)
        if resp.status_code >= 400:
            return []
        html = resp.text
    except httpx.HTTPError as exc:
        log.info("claude_fuzzy: ddg search %r failed: %s", query, exc)
        return []

    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    for result in soup.select("div.result"):
        a = result.select_one("a.result__a")
        if not a:
            continue
        href = a.get("href") or ""
        title = a.get_text(" ", strip=True)
        snippet_tag = result.select_one(".result__snippet")
        snippet = snippet_tag.get_text(" ", strip=True) if snippet_tag else ""
        # DDG sometimes wraps URLs in a /l/?uddg=<target> redirect — extract it.
        href = _unwrap_ddg(href)
        if not href or not title:
            continue
        out.append({"url": href, "title": title, "snippet": snippet})
        if len(out) >= _SEARCH_RESULT_LIMIT:
            break
    return out


def _unwrap_ddg(href: str) -> str:
    """Pull the real URL out of a /l/?uddg=... DuckDuckGo redirect, if any."""
    if "uddg=" not in href:
        return href
    from urllib.parse import parse_qs, urlsplit, unquote
    qs = parse_qs(urlsplit(href).query)
    real = qs.get("uddg", [""])[0]
    return unquote(real) if real else href


# ---------------------------------------------------------------------------
# On-site search for loose mentions (Step 6 candidate gathering)
# ---------------------------------------------------------------------------

_PRODUCT_PATH_RE = re.compile(r"/products?/[^/?#]+|/listing/\d+|/p/[^/?#]+")


def _onsite_search(
    shop_domain: str, query: str, *, client: httpx.Client | None = None,
) -> list[dict]:
    """Search ``<shop_domain>/search?q=<query>`` for product-page candidates.

    Works on Shopify, WooCommerce, BigCommerce — anything whose search page
    renders anchor tags into product pages. Returns up to ``_SEARCH_RESULT_LIMIT``
    {"url", "title", "snippet"} dicts. Empty list on any failure.
    """
    base = shop_domain if shop_domain.startswith("http") else f"https://{shop_domain}"
    # Strip any trailing slash so we don't end up with //search (which 301s on
    # some Shopify stores and then 404s on the bare /search path).
    base = base.rstrip("/")
    url = f"{base}/search?q={quote_plus(query)}"
    html = _fetch(url, client=client)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    out: list[dict] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not _PRODUCT_PATH_RE.search(href):
            continue
        absolute = urljoin(base, href.split("?")[0].split("#")[0])
        if absolute in seen:
            continue
        seen.add(absolute)
        title = a.get_text(" ", strip=True)
        if not title:
            # Anchor with no text — try its title attribute or an image alt.
            img = a.find("img")
            title = (a.get("title") or (img.get("alt") if img else "") or "").strip()
        if not title:
            continue
        out.append({"url": absolute, "title": title, "snippet": ""})
        if len(out) >= _SEARCH_RESULT_LIMIT:
            break
    return out


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the fuzzy-judgement step of a personal sale-check tool.

A Python script handles the deterministic 90% of the work (price extraction,
sale math, state I/O). It hands you three small batched tasks — the parts that
need human-like judgement — and asks for one structured response via the
`submit_results` tool. You MUST respond by calling that tool exactly once;
never reply with free-form text.

# Task type 1: shop_sales — homepage sale detection

For each shop you'll receive its name, homepage URL, and a visible-text
excerpt of the homepage. A long page is truncated; snippets after a
"[sale mentions further down the page:]" marker are context windows around
sale-like wording found past the truncation point — judge them with the same
rules as the main excerpt (they are often just footer/nav links). Decide
whether the shop is running a meaningful sitewide or major-section sale
right now.

Status values:
  - "yes"     A real promotion is clearly active. Set `description` to a
              terse summary (≤ 120 chars): the discount, code, and end-date
              if visible (e.g. "30% off sitewide with code SPRING30, ends Sunday").
  - "no"      No sale signal, or only non-promotional banners (free shipping,
              free returns, "new arrivals"). `description` should be null.
  - "unclear" The page mentions sale-like language but it's ambiguous — a
              persistent "Sale" nav link with no percentage, a year-round
              clearance section, or text you can't interpret. `description`
              may briefly say why.

Sale signals (count as "yes"):
  * Promo bars or hero banners with explicit discount language
    ("30% off", "Up to 50% off", "Spring Sale", "BOGO 50%")
  * A "Sale" navigation entry with a percentage attached
  * Actively-advertised sitewide promo codes

NOT a sale (count as "no" or "unclear"):
  * A persistent "Sale" link with no discount info — that's just a category
  * Year-round clearance / outlet sections
  * "Free shipping over $X", "Free returns", "New collection" — these are
    not discounts on existing items
  * "Sale price" labels on a Shopify product card — Shopify's default theme
    uses that text even when nothing is discounted

Be CONSERVATIVE. False positives are worse than misses — the user will
spot-check yes results and a false yes wastes their attention.

# Task type 2: resolutions — shop-name to URL

For each shop name you'll receive 3-5 candidate websites from a web search
(URL, title, snippet). Pick the candidate that is the shop's official
storefront for the brand named.

  - url:        The canonical homepage URL of the official shop, or null if
                no candidate is the real shop.
  - confidence: "high" if the candidate is unambiguously the brand's own
                site (matching domain name, shop-y title); "low" if it looks
                plausible but you're not certain; "none" if you couldn't pick.

Reject candidates that are clearly fan pages, marketplaces hosting the brand
(Etsy listings, Amazon product pages), social media profiles, review sites,
or "shop name" matches on unrelated domains. Prefer the brand's own
top-level domain over collection pages on third-party marketplaces.

When the search returned zero candidates the task won't appear in your
input — it's already been recorded as unresolved before the call.

# Task type 3: loose_matches — loose mention to product URL

For each loose mention you'll receive the mention text (e.g. "Aniqi Law
pants"), the shop name, and 0-5 candidate product URLs scraped from the
shop's own /search?q= page (URL, title, optional snippet).

  - matched_url: The candidate URL that is a confident match for the
                 specific item mentioned, or null if none is a clear match.
  - confidence:  "high" if the title clearly matches the named item; "low"
                 if it's plausible but the user should verify; "none" if
                 no candidate clearly fits.

A "confident match" means the candidate's title contains the key noun(s)
from the mention or a recognisable synonym. Example: mention "Threat Level
Midnight Shirt" matches a title containing "Threat Level Midnight" even if
the word "Shirt" is implicit. Mention "Law pants" matches a title
containing "Law Joggers" or "Trafalgar Law Pants" (Law is a character
name) — character-name matching is allowed when one obviously-correct
candidate stands out.

If no candidate is a clear match, return null. The script will surface
nothing rather than guess wrong — the user prefers misses over false
positives here too.

# Task type 4: email_sales — Gmail Promotions sale-announcement judgement

For each email you'll receive the shop the email was attributed to, the
subject line, a body excerpt, and `email_date` (the date the email was sent,
ISO `YYYY-MM-DD`). Decide whether the email announces or implies a real sale
at that shop.

Status values:
  - "yes"     The email announces an active sale or a soon-to-start sale
              with a specific window. Set `description` to a terse summary
              (≤ 120 chars): the discount, code, and dates if visible
              (e.g. "25% off ends Sunday, code SUMMER25" or
              "Memorial Day sale starts May 24, 30% off sitewide").
              Upcoming sales with a named date count as "yes" — the whole
              point of reading email is to catch sales before they hit the
              homepage.
  - "no"      Brand marketing with no discount: new product drops, restock
              announcements, lifestyle content, "join our community"
              newsletters.
  - "unclear" Ambiguous — phrases like "members get early access" without a
              clear discount, or vague urgency without specifics.

Be CONSERVATIVE. False positives waste the user's attention.

## Sale window (`starts_on` / `ends_on`)

When status is "yes" and the email states a sale window, resolve it to
absolute calendar dates **relative to `email_date`** and return them as ISO
`YYYY-MM-DD` strings. This lets the tool keep reminding the user until the
sale ends.

  - starts_on: the first day the discount is active. Use null when the sale is
               already active ("sale is on now", "today only") or when no start
               is stated. Resolve relative phrases against `email_date`:
               "this Friday", "starts tomorrow", "May 24" → the concrete date.
  - ends_on:   the last day the discount is active. "ends Sunday", "through
               5/26", "48 hours left" → the concrete date. Null when no end is
               stated.

Only resolve dates you can actually ground in the email's wording — never
guess a window. Both fields are null for "no" / "unclear", and for "yes"
emails that give no dates at all.

# Identifiers

Every task carries an `id` field. Echo the same `id` back in each result so
the script can match responses to requests. Return one result per input
task; do not invent extras.

# Output

Call `submit_results` with four arrays:
  - shop_sales:    one entry per input shop_homepage task
  - resolutions:   one entry per input shop_resolve task
  - loose_matches: one entry per input loose_mention task
  - email_sales:   one entry per input email_sales task

Arrays may be empty if the corresponding input list was empty. Do not write
any text outside the tool call."""


TOOL_SCHEMA: dict[str, Any] = {
    "name": "submit_results",
    "description": "Submit structured decisions for all batched fuzzy tasks.",
    "input_schema": {
        "type": "object",
        "properties": {
            "shop_sales": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "shop": {"type": "string"},
                        "status": {"type": "string",
                                   "enum": ["yes", "no", "unclear"]},
                        "description": {"type": ["string", "null"]},
                    },
                    "required": ["id", "shop", "status"],
                },
            },
            "resolutions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "shop_name": {"type": "string"},
                        "url": {"type": ["string", "null"]},
                        "confidence": {"type": "string",
                                       "enum": ["high", "low", "none"]},
                    },
                    "required": ["id", "shop_name", "url", "confidence"],
                },
            },
            "loose_matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "mention": {"type": "string"},
                        "shop": {"type": "string"},
                        "matched_url": {"type": ["string", "null"]},
                        "confidence": {"type": "string",
                                       "enum": ["high", "low", "none"]},
                    },
                    "required": ["id", "mention", "shop", "matched_url",
                                 "confidence"],
                },
            },
            "email_sales": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "email_id": {"type": "string"},
                        "shop": {"type": "string"},
                        "status": {"type": "string",
                                   "enum": ["yes", "no", "unclear"]},
                        "description": {"type": ["string", "null"]},
                        "starts_on": {"type": ["string", "null"],
                                      "description": "ISO YYYY-MM-DD; first active day, or null"},
                        "ends_on": {"type": ["string", "null"],
                                    "description": "ISO YYYY-MM-DD; last active day, or null"},
                    },
                    "required": ["id", "email_id", "shop", "status"],
                },
            },
        },
        "required": ["shop_sales", "resolutions", "loose_matches"],
    },
}


def _build_payload(
    shop_tasks: list[dict],
    resolve_tasks: list[dict],
    loose_tasks: list[dict],
    email_tasks: list[dict],
) -> str:
    """Build the user-message JSON payload describing all four task batches."""
    return json.dumps({
        "shop_homepage_tasks": shop_tasks,
        "shop_resolve_tasks": resolve_tasks,
        "loose_mention_tasks": loose_tasks,
        "email_sales_tasks": email_tasks,
    }, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Claude API call
# ---------------------------------------------------------------------------

def _call_claude(
    client: Any,
    model: str,
    payload: str,
) -> tuple[dict, Any]:
    """Send the batched call. Return (parsed tool input, usage object)."""
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "submit_results"},
        messages=[{"role": "user", "content": payload}],
    )

    if getattr(response, "stop_reason", None) == "max_tokens":
        log.warning(
            "claude_fuzzy: response hit max_tokens cap (%d) — submit_results "
            "JSON may be truncated and entries dropped; raise MAX_TOKENS or "
            "cap description lengths",
            MAX_TOKENS,
        )

    tool_input: dict | None = None
    for block in response.content:
        block_type = getattr(block, "type", None)
        if block_type == "tool_use" and getattr(block, "name", None) == "submit_results":
            tool_input = getattr(block, "input", None)
            break
    if tool_input is None:
        raise RuntimeError("claude_fuzzy: model did not call submit_results")
    return tool_input, getattr(response, "usage", None)


def _usage_dict(usage: Any) -> dict | None:
    """Coerce the SDK usage object to a plain dict (for logging / cost trace)."""
    if usage is None:
        return None
    out = {}
    for field in ("input_tokens", "output_tokens",
                  "cache_creation_input_tokens", "cache_read_input_tokens"):
        val = getattr(usage, field, None)
        if val is not None:
            out[field] = val
    return out or None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _empty_result() -> dict:
    return {
        "shop_sales": [],
        "resolutions": [],
        "loose_matches": [],
        "email_sales": [],
        "unresolved": [],
        "shop_verdicts": [],
        "usage": None,
    }


def _get_client(client: Any | None) -> Any:
    if client is not None:
        return client
    import anthropic
    return anthropic.Anthropic()


def resolve_fuzzy(
    shops_to_check: list[dict],
    shops_to_resolve: list[str],
    loose_mentions: list[dict],
    email_signals: list[dict] | None = None,
    *,
    client: Any | None = None,
    model: str = DEFAULT_MODEL,
    http_client: httpx.Client | None = None,
    prior_verdicts: list[dict] | None = None,
    today: date | None = None,
) -> dict:
    """Run the batched fuzzy-judgement step.

    Parameters:
        shops_to_check    [{"shop": str, "url": str}, ...]   Step 5
        shops_to_resolve  [str, ...]                         Step 2 fallback
        loose_mentions    [{"mention": str, "shop": str,
                            "shop_domain": str}, ...]        Step 6
        email_signals     [{"email_id", "shop", "subject",
                            "body_excerpt"}, ...]            Gmail (issue #9)
        client            Optional preconstructed anthropic.Anthropic — pass
                          one to inject a mock in tests. Defaults to a fresh
                          client built from the env (ANTHROPIC_API_KEY).
        model             Claude model ID. Default ``claude-sonnet-4-6``.
        http_client       Optional shared httpx.Client for candidate gathering
                          (lets callers re-use a connection pool / inject
                          mocks). A per-call client is created if omitted.
        prior_verdicts    Persisted homepage verdict cache from last run
                          (``shop_verdicts.json``; cost lever #3). A homepage
                          whose sale-signal hash matches a still-fresh cached
                          entry is reused locally instead of re-sent to Claude.
                          Default None → empty → every signal-bearing homepage
                          is judged (pre-#3 behaviour). The fresh judgements are
                          returned under ``shop_verdicts`` for the caller to
                          persist.
        today             Reference date for the verdict-cache freshness check.
                          Defaults to today (UTC).

    Returns the dict described in the module docstring. Never raises for
    candidate-gathering failures — those degrade into empty candidate lists
    or the ``unresolved`` bucket. Will raise if the API call itself fails or
    the model refuses to use the tool.
    """
    email_signals = email_signals or []
    if (not shops_to_check and not shops_to_resolve
            and not loose_mentions and not email_signals):
        return _empty_result()

    verdict_idx = shop_verdicts.index(prior_verdicts or [])
    today = today or datetime.now(timezone.utc).date()

    # --- Step 5: gather homepage excerpts ---------------------------------
    shop_tasks: list[dict] = []
    skipped_shop_sales: list[dict] = []
    no_signal_shop_sales: list[dict] = []
    cached_shop_sales: list[dict] = []
    task_hash_by_id: dict[str, str] = {}
    for idx, entry in enumerate(shops_to_check):
        shop, url = entry["shop"], entry["url"]
        html = _fetch_homepage(url, client=http_client)
        if not html:
            skipped_shop_sales.append({
                "shop": shop, "status": "unclear",
                "description": "could not fetch homepage",
            })
            continue
        excerpt = _homepage_excerpt(html)
        if not _has_sale_signal(excerpt):
            # No sale lexeme in the visible text → can only be "no". Skip the
            # Claude task and record the verdict locally (the model would see
            # this same excerpt and conclude the same thing). Cost lever #1.
            no_signal_shop_sales.append({
                "shop": shop, "status": "no", "description": None,
            })
            continue
        content_hash = _verdict_hash(excerpt)
        cached = shop_verdicts.lookup(verdict_idx, shop, content_hash, today)
        if cached is not None:
            # The homepage's sale-signal text is unchanged since the last Claude
            # judgement and that judgement is still fresh → reuse it instead of
            # paying to re-confirm. Cost lever #3; see src/shop_verdicts.py.
            cached_shop_sales.append({
                "shop": shop,
                "status": cached.get("status"),
                "description": cached.get("description"),
            })
            continue
        task_id = f"shop_{idx}"
        task_hash_by_id[task_id] = content_hash
        shop_tasks.append({
            "id": task_id,
            "shop": shop,
            "url": url,
            "html_excerpt": excerpt,
        })
    if no_signal_shop_sales:
        log.info(
            "claude_fuzzy: pre-filtered %d/%d homepages with no sale signal "
            "(skipped Claude)",
            len(no_signal_shop_sales), len(shops_to_check),
        )
    if cached_shop_sales:
        log.info(
            "claude_fuzzy: reused %d cached homepage verdict(s) (signal hash "
            "match, still fresh; skipped Claude)",
            len(cached_shop_sales),
        )

    # --- Step 2: gather DDG candidates ------------------------------------
    resolve_tasks: list[dict] = []
    unresolved: list[str] = []
    for idx, name in enumerate(shops_to_resolve):
        candidates = _ddg_search(f"{name} official store", client=http_client)
        if not candidates:
            unresolved.append(name)
            continue
        resolve_tasks.append({
            "id": f"resolve_{idx}",
            "shop_name": name,
            "candidates": candidates,
        })

    # --- Step 6: gather on-site candidates --------------------------------
    loose_tasks: list[dict] = []
    skipped_loose: list[dict] = []
    for idx, entry in enumerate(loose_mentions):
        mention = entry["mention"]
        shop = entry["shop"]
        domain = entry["shop_domain"]
        candidates = _onsite_search(domain, mention, client=http_client)
        if not candidates:
            # No candidates → still send to Claude with empty list? No — short
            # circuit; nothing to judge. Surface as a low-confidence skip.
            skipped_loose.append({
                "mention": mention, "shop": shop,
                "matched_url": None, "confidence": "none",
            })
            continue
        loose_tasks.append({
            "id": f"loose_{idx}",
            "mention": mention,
            "shop": shop,
            "shop_domain": domain,
            "candidates": candidates,
        })

    # --- Gmail email-sale tasks (no candidate gathering needed) -----------
    email_tasks: list[dict] = []
    for idx, sig in enumerate(email_signals):
        email_tasks.append({
            "id": f"email_{idx}",
            "email_id": sig.get("email_id", ""),
            "shop": sig.get("shop", ""),
            "subject": sig.get("subject", ""),
            "body_excerpt": sig.get("body_excerpt", ""),
            # Anchor for resolving relative sale-window phrases ("this Friday")
            # to absolute dates. Empty when the signal carried no date (e.g. an
            # SMS forward) — Claude then leaves starts_on/ends_on null.
            "email_date": sig.get("email_date", ""),
        })

    # If candidate gathering left nothing for Claude to judge, skip the API call.
    # (cached_shop_sales can be non-empty here — a quiet day where every
    # signal-bearing homepage was a cache hit costs zero Claude tokens.)
    if not shop_tasks and not resolve_tasks and not loose_tasks and not email_tasks:
        return {
            "shop_sales": skipped_shop_sales + no_signal_shop_sales + cached_shop_sales,
            "resolutions": [],
            "loose_matches": skipped_loose,
            "email_sales": [],
            "unresolved": unresolved,
            "shop_verdicts": [],
            "usage": None,
        }

    payload = _build_payload(shop_tasks, resolve_tasks, loose_tasks, email_tasks)
    api_client = _get_client(client)
    tool_input, usage = _call_claude(api_client, model, payload)

    # Strip ids from the response (callers don't need them) and merge with
    # the pre-API skipped buckets.
    def _strip(items: list[dict], drop: str) -> list[dict]:
        out = []
        for it in items or []:
            clean = {k: v for k, v in it.items() if k != drop}
            out.append(clean)
        return out

    # Fresh verdicts for the homepages actually judged this run (cache misses),
    # matched back to their signal hash by task id. Returned for the caller to
    # upsert into shop_verdicts.json (cost lever #3). Cache hits are NOT here —
    # their prior entry rides along unchanged so the freshness ceiling counts
    # from the last real judgement.
    fresh_verdicts: list[dict] = []
    for s in tool_input.get("shop_sales", []) or []:
        h = task_hash_by_id.get(s.get("id"))
        if h and s.get("shop"):
            fresh_verdicts.append({
                "shop": s.get("shop"),
                "hash": h,
                "status": s.get("status"),
                "description": s.get("description"),
            })

    return {
        "shop_sales":    skipped_shop_sales + no_signal_shop_sales + cached_shop_sales + _strip(tool_input.get("shop_sales", []), "id"),
        "resolutions":   _strip(tool_input.get("resolutions", []),   "id"),
        "loose_matches": skipped_loose + _strip(tool_input.get("loose_matches", []), "id"),
        "email_sales":   _strip(tool_input.get("email_sales", []),   "id"),
        "unresolved":    unresolved,
        "shop_verdicts": fresh_verdicts,
        "usage":         _usage_dict(usage),
    }


# ---------------------------------------------------------------------------
# Manual smoke test
# ---------------------------------------------------------------------------

def _smoke() -> None:
    """Run a tiny end-to-end check against the real Claude API.

    Costs roughly a fraction of a cent. Requires ANTHROPIC_API_KEY in the env.
    Picks one example for each of the three task types so you can eyeball the
    output and the usage numbers.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set — aborting smoke test.")
        return

    shops_to_check = [{"shop": "Aniqi", "url": "https://aniqi.com"}]
    shops_to_resolve = ["Black Rabbit Originals"]
    loose_mentions = [{
        "mention": "Trafalgar Joggers",
        "shop": "Aniqi",
        "shop_domain": "aniqi.com",
    }]

    print("Calling resolve_fuzzy with 1 task of each type...")
    result = resolve_fuzzy(shops_to_check, shops_to_resolve, loose_mentions)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    _smoke()
