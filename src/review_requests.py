"""Aggregate post-purchase *review-request* emails for the daily digest.

Shops (directly or via review platforms like Loox / Yotpo / Judge.me) send
"how did it go? leave a review" emails after a purchase, then fire repeated
follow-ups / reminders for the same order. This module turns a batch of
fetched candidate emails into a tidy, deduped render list for the digest:

  * **one entry per order** — reminders for the same purchase collapse to the
    most-recent email (keyed by a parsed order number, or a normalized subject
    when no order number is present);
  * each entry carries a deep link straight to the email in Gmail;
  * a separate all-time Gmail-search link lets the user see every review
    request ever (the digest section only shows a recent window).

This is **stateless** — the daily run re-fetches the recent window and
recomputes the list every time, so there's no Gist file and no persistence
(unlike ``email_sales.py``, whose structure this otherwise mirrors). The pure
helpers here are unit-tested directly; the IMAP fetch lives in ``gmail.py``
(``fetch_review_requests``) and the rendering in ``digest.py``.

The email shape consumed by ``dedupe`` matches ``gmail._parse_message``::

    {"id": <X-GM-MSGID>, "from": ..., "subject": ..., "body_text": ...,
     "date": <RFC-2822 Date header>, "message_id": <RFC-822 Message-ID>}
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from urllib.parse import quote

# Gmail web base for account index 0 (the user's primary account — matches the
# permalink format they shared). The fragment after ``#`` is parsed client-side.
_MAIL_BASE = "https://mail.google.com/mail/u/0/#"

# --- What counts as a review request ---------------------------------------
#
# Matching is anchored on the *subject* (real review requests always signal
# there) in two layers:
#
#   1. Gmail-side recall — ``subject:(...)`` terms (words Gmail token-matches +
#      a few phrases) keep the IMAP fetch and the all-time link cheap and
#      roughly on-topic. Shared by ``search_query`` and ``all_requests_url``.
#   2. Python-side precision — ``_REQUEST_RE`` / ``_EXCLUDE_RE`` are the final
#      authority applied in ``dedupe``. They drop the two big noise classes the
#      body-matching Gmail search drags in: already-reviewed confirmations
#      ("Thank you for reviewing X") and order-lifecycle mail that only mentions
#      reviews in a footer ("Your order has arrived", "Order #123 confirmed").
#
# Tuned against a real 30-day inbox sample (Loox / Okendo / Judge.me / Yotpo +
# Shopify/Amazon), so the term/regex lists track what these shops actually send.

# Gmail ``subject:`` OR-group — the recall net for the IMAP fetch. Bare words
# rely on Gmail's loose token matching (``review`` also hits "reviews"); the
# Python pass below tightens precision. ASCII-only.
_SUBJECT_TERMS: tuple[str, ...] = (
    "review", "reviews", "rate", "rated", "rating", "feedback",
    "enjoying", "liking", "expectations",
    '"how did it go"', '"how did we do"', '"how was your"', '"how is your"',
    '"what do you think"', '"share your thoughts"', '"one more favor"',
)
_SUBJECT_QUERY = "subject:(" + " OR ".join(_SUBJECT_TERMS) + ")"

# Curated *product-review* phrases for the all-time "see all" link — precise
# enough that the landing search shows shop review requests, not bank/security
# "review your account" mail. Mirrors _REQUEST_RE's intent in Gmail phrase form.
_LINK_PHRASES: tuple[str, ...] = (
    "leave a review", "write a review", "your review", "review it",
    "review your order", "review your purchase", "item to review",
    "how did it go", "how did we do", "rate your order", "rate your purchase",
    "rate your transaction", "rate and review", "rate us", "share your thoughts",
    "share your experience", "what do you think", "tell us what you think",
    "meet your expectations", "how was your", "one more favor",
    "how are you enjoying", "how are you liking",
)
_ALL_TIME_QUERY = (
    "subject:(" + " OR ".join(f'"{p}"' for p in _LINK_PHRASES) + ")"
    + ' -"thank you for reviewing" -"thanks for reviewing"'
    + ' -"thank you for your review"'
)

# Python positive signal — *product-review-specific* phrases, not bare words.
# Bare "review"/"rate"/"feedback" appear in far too much non-shopping mail
# (loan rates, "review your account settings", security alerts, newsletters), so
# matching is phrase-anchored. "review your X" is scoped to purchase nouns so
# "review your Google account" doesn't fire; "your review" (Loox's "add a photo
# to your review of …") is safe because it reads the other word order.
_REQUEST_RE = re.compile(
    r"\bleave (?:a |us a |your |a quick )?review"
    r"|\bwrite (?:a |your )?review"
    r"|\byour review\b"
    r"|\breview your (?:order|purchase|recent|item|product|experience)"
    r"|\breview it\b"
    r"|\bitem to review\b"
    r"|\brate your (?:order|purchase|transaction|experience|recent|stay|visit)"
    r"|\brate and review\b|\brate us\b|\brated us\b"
    r"|how did it go|how did we do"
    r"|how(?:'s|s|’s| is| was)\s+your"
    r"|how are you (?:enjoying|liking)"
    r"|\bwhat do you think|tell us what you think"
    r"|share your (?:thoughts|experience|review|feedback)"
    r"|meet your expectations"
    r"|one more favor",
    re.IGNORECASE,
)
# Already-reviewed / not-a-request confirmations. Anchored on the *confirmation*
# shape "thank(s|you) … for [your/the/…] review[ing]" rather than a loose
# "thank … review" bridge — the loose form wrongly swallowed genuine requests
# like "Thanks for your order — leave a review!" (where "review" trails an
# unrelated "for"). Plus explicit "you('ve) reviewed" / "already reviewed".
_EXCLUDE_RE = re.compile(
    r"thank(?:s| you)\b.{0,25}\bfor\s+(?:your |the |that |this |recent )*review"
    r"|you(?:'ve| have| ’ve)?\s+reviewed"
    r"|already reviewed",
    re.IGNORECASE,
)

# A looser review-ish word, only trusted when the sender is a known review
# platform — rescues genuine shop nudges with off-pattern subjects (e.g. Catgirl
# Riot's "Fast review ➡️ furious discount", sent via Loox).
_REVIEWISH_RE = re.compile(r"\breviews?\b|\brate[ds]?\b|\bfeedback\b", re.IGNORECASE)
_PLATFORM_DOMAINS = frozenset({
    "loox.io", "okendo.io", "yotpo.com", "judge.me", "stamped.io",
    "junip.co", "reviews.io", "fera.ai", "opinew.com", "judgeme.email",
    "judgeme-worker-ecs.mail", "kudobuzz.com", "trustpilot.com",
})


def _sender_is_review_platform(from_header: str) -> bool:
    """True when the sender domain (or a parent) is a known review platform."""
    _, addr = parseaddr(from_header or "")
    domain = addr.split("@", 1)[1].lower() if "@" in (addr or "") else ""
    if not domain:
        return False
    if domain in _PLATFORM_DOMAINS:
        return True
    parts = domain.split(".")
    for i in range(1, len(parts) - 1):
        if ".".join(parts[i:]) in _PLATFORM_DOMAINS:
            return True
    return False


def is_review_request(subject: str, from_header: str = "") -> bool:
    """True when a message is an outstanding *product* review request.

    Precision authority over the broad Gmail fetch. A subject must carry a
    product-review phrase (``_REQUEST_RE``) and not be an already-reviewed
    confirmation (``_EXCLUDE_RE``). As a fallback, a looser review-ish subject
    is accepted when the sender is a known review platform (``from_header``),
    which rescues off-pattern shop nudges without re-admitting bank/security mail.
    """
    s = subject or ""
    if _EXCLUDE_RE.search(s):
        return False
    if _REQUEST_RE.search(s):
        return True
    if from_header and _sender_is_review_platform(from_header) and _REVIEWISH_RE.search(s):
        return True
    return False

# Order-number patterns. ``_ORDER_RE`` is keyword-anchored ("Order #138880",
# "order no. 138880", "order number 138880") and safe to run over the body;
# ``_HASH_RE`` is a bare "#138880" and only trusted in the subject (where these
# emails put the order number) to avoid matching stray "#123" in body prose.
# Both require >=3 digits so "#1" / "step 2" don't read as orders.
_ORDER_RE = re.compile(
    r"\border\s*(?:#|no\.?|number|num\.?|id)?\s*[:#\-]?\s*"
    r"([A-Za-z]{0,5}\d{3,}[A-Za-z0-9\-]*)",
    re.IGNORECASE,
)
_HASH_RE = re.compile(r"#\s*([A-Za-z]{0,5}\d{3,}[A-Za-z0-9\-]*)")

# How far into the body we hunt for an order number — they appear near the top.
_BODY_ORDER_SCAN = 2000

# Subject prefixes stripped when building the no-order-number dedupe signature.
_SUBJECT_PREFIX_RE = re.compile(r"^\s*(re|fwd|fw|reminder)\b\s*[:\-!]*\s*", re.IGNORECASE)

# Trailing decorations on a sender display name that aren't part of the shop.
_SHOP_VIA_RE = re.compile(r"\s+via\s+.*$", re.IGNORECASE)
_SHOP_SUFFIX_RE = re.compile(r"\s+(reviews?|team)$", re.IGNORECASE)
# Display names that are role-addresses, not a shop — fall back to the domain.
_GENERIC_NAMES = frozenset({
    "no-reply", "no reply", "noreply", "donotreply", "do-not-reply",
    "info", "support", "hello", "hi", "team", "orders", "order",
    "feedback", "reviews", "review", "notifications", "notification",
    "mail", "email", "store", "shop", "sales", "help", "contact",
    "service", "customer service", "news", "newsletter",
})

_MAX_SUBJECT = 120


# ---------------------------------------------------------------------------
# Query + link builders (shared shape for the recent window and the all-time link)
# ---------------------------------------------------------------------------

def search_query(days: int = 30) -> str:
    """Gmail-search expression for the IMAP ``X-GM-RAW`` fetch (recent window).

    Subject-anchored for recall; ``dedupe`` applies ``is_review_request`` for the
    final precision pass.
    """
    return f"{_SUBJECT_QUERY} newer_than:{days}d"


def all_requests_url() -> str:
    """Web link to a Gmail search for *every* review request (no date bound).

    Subject-anchored with the already-reviewed confirmations excluded so the
    landing search roughly mirrors the digest section. Searches all mail
    client-side, so it surfaces requests beyond the fetched recent window and
    outside the inbox.
    """
    return _MAIL_BASE + "search/" + quote(_ALL_TIME_QUERY, safe="")


def email_permalink(message_id: str | None, gm_id: str | None = None) -> str | None:
    """Deep link to a single Gmail message.

    Primary: ``#all/<hex>`` where ``<hex>`` is the 64-bit ``X-GM-MSGID`` in hex —
    Gmail opens the message **directly** (no intermediate search-results list).
    ``X-GM-MSGID`` is always present (it's our IMAP fetch key), so this is the
    path taken in practice. Fallback when it's somehow missing/non-numeric:
    ``#search/rfc822msgid:<Message-ID>`` — the standard single-message search
    permalink (resolves to the one message, but lands on a 1-result list).
    Returns ``None`` when neither id is available.
    """
    if gm_id:
        try:
            return _MAIL_BASE + "all/" + format(int(gm_id), "x")
        except (TypeError, ValueError):
            pass
    mid = (message_id or "").strip().strip("<>").strip()
    if mid:
        # Keep ``rfc822msgid:`` literal so Gmail recognizes the operator; only
        # the id value is encoded (and message-ids are plain ASCII tokens).
        return _MAIL_BASE + "search/rfc822msgid:" + quote(mid, safe="@.-_")
    return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _shop_from_sender(from_header: str) -> str:
    """Best-effort shop name from a ``From`` header.

    Review-platform emails arrive as ``Store Name <no-reply@loox.io>`` — the
    display name is the real shop, the domain is the platform. So prefer the
    display name (lightly cleaned of "via X" / trailing "Reviews"/"Team"),
    falling back to the sender domain when there's no display name.
    """
    name, addr = parseaddr(from_header or "")
    name = (name or "").strip().strip('"').strip()
    if name:
        name = _SHOP_VIA_RE.sub("", name)
        name = _SHOP_SUFFIX_RE.sub("", name).strip()
        # Role addresses ("no-reply", "info", …) aren't the shop — use the domain.
        if name and name.lower() not in _GENERIC_NAMES:
            return name
    domain = (addr.split("@", 1)[1].strip().lower() if "@" in (addr or "") else "")
    if domain.startswith("www."):
        domain = domain[4:]
    return domain or "(unknown shop)"


def _order_id(subject: str, body: str) -> str | None:
    """Parse an order number from the subject (then body), normalized upper.

    Subject is searched with both the keyword pattern and a bare ``#NNN``;
    the body only with the keyword pattern (a bare hash in body prose is too
    noisy). Returns ``None`` when no plausible order number is found.
    """
    subj = subject or ""
    m = _ORDER_RE.search(subj) or _HASH_RE.search(subj)
    if not m:
        m = _ORDER_RE.search((body or "")[:_BODY_ORDER_SCAN])
    if not m:
        return None
    return m.group(1).strip().upper() or None


def _normalize_subject(subject: str) -> str:
    """Collapse a subject to a stable dedupe signature (no order number path).

    Strips leading ``Re:/Fwd:/Reminder:`` prefixes, drops digits and ``#``
    (order numbers etc.), removes remaining punctuation, and lowercases — so
    repeated reminders with an identical generic subject collapse together.
    """
    s = (subject or "").strip()
    while True:
        stripped = _SUBJECT_PREFIX_RE.sub("", s)
        if stripped == s:
            break
        s = stripped
    s = s.lower()
    s = re.sub(r"[#\d]+", " ", s)
    s = re.sub(r"[^a-z\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _dedupe_key(shop: str, order: str | None, subject: str) -> tuple[str, str]:
    """``(shop, "order:<id>")`` when an order number is known, else
    ``(shop, "subj:<signature>")``. Shop is lowercased so casing never splits
    a group."""
    base = (shop or "").strip().lower()
    if order:
        return (base, "order:" + order)
    return (base, "subj:" + _normalize_subject(subject))


def _parse_dt(raw: str | None) -> datetime | None:
    """Parse an RFC-2822 ``Date`` header into an aware datetime, or ``None``."""
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _clip_subject(subject: str) -> str:
    s = re.sub(r"\s+", " ", (subject or "").strip())
    if len(s) <= _MAX_SUBJECT:
        return s
    return s[: _MAX_SUBJECT - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Dedupe + render
# ---------------------------------------------------------------------------

def dedupe(emails: list[dict], *, now: datetime | None = None) -> list[dict]:
    """Collapse review-request emails to one entry per order, newest first.

    For each ``(shop, order|subject)`` group the most-recent email wins.
    Returns render dicts the digest consumes directly::

        {"shop": str, "subject": str, "date_iso": str | None,
         "days_ago": int | None, "url": str | None}

    Emails missing a parseable ``Date`` sort last (treated as oldest) and carry
    ``days_ago = None``. ``now`` is injectable for tests.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()

    best: dict[tuple[str, str], dict] = {}
    for em in emails or []:
        if not isinstance(em, dict):
            continue
        subject = em.get("subject") or ""
        from_header = em.get("from", "")
        # Precision pass: drop already-reviewed confirmations, lifecycle mail,
        # and non-shopping "review/rate/feedback" noise (see is_review_request).
        if not is_review_request(subject, from_header):
            continue
        shop = _shop_from_sender(from_header)
        order = _order_id(subject, em.get("body_text") or "")
        key = _dedupe_key(shop, order, subject)
        dt = _parse_dt(em.get("date"))

        entry = {
            "shop": shop,
            "subject": subject,
            "dt": dt,
            "url": email_permalink(em.get("message_id"), em.get("id")),
        }
        prior = best.get(key)
        if prior is None or _is_newer(dt, prior["dt"]):
            best[key] = entry

    ordered = sorted(best.values(), key=_sort_key, reverse=True)

    out: list[dict] = []
    for e in ordered:
        dt = e["dt"]
        out.append({
            "shop": e["shop"],
            "subject": _clip_subject(e["subject"]),
            "date_iso": dt.date().isoformat() if dt else None,
            "days_ago": (today - dt.date()).days if dt else None,
            "url": e["url"],
        })
    return out


# Sentinel for missing dates: sorts before any real datetime (i.e. "oldest").
_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


def _is_newer(a: datetime | None, b: datetime | None) -> bool:
    return (a or _MIN_DT) > (b or _MIN_DT)


def _sort_key(entry: dict) -> tuple:
    dt = entry["dt"] or _MIN_DT
    return (dt, entry["shop"].lower())
