"""Detect *back-in-stock* notification emails for the daily digest.

When a shop (often via a Back-in-Stock app like Klaviyo BIS / Swym) emails "your
item is back in stock", this module turns the fetched candidate emails into a
deduped render list that the digest folds into its existing **"Back in stock"**
section, tagged as an email alert (distinct from the scrape-driven restocks the
daily price check already produces).

Like ``review_requests`` this is **stateless** — the daily run re-fetches a
recent window and recomputes the list every time, so there's no Gist file. The
pure helpers here are unit-tested directly; the IMAP fetch lives in ``gmail.py``
(``fetch_restock_emails``) and the rendering in ``digest.py``.

Email shape consumed by ``dedupe`` matches ``gmail._parse_message``::

    {"id": <X-GM-MSGID>, "from": ..., "subject": ..., "body_text": ...,
     "date": <RFC-2822 Date header>, "message_id": <RFC-822 Message-ID>}
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from urllib.parse import quote

# Reuse the Gmail deep-link builder verbatim (#all/<hex> permalink + fallback).
from src.review_requests import email_permalink

_MAIL_BASE = "https://mail.google.com/mail/u/0/#"

# --- Recall: Gmail subject net for the IMAP fetch + all-time link ------------
_SUBJECT_TERMS: tuple[str, ...] = (
    "restock", "restocked",
    '"back in stock"', '"now available"', '"available again"',
    '"in stock again"', '"back in your size"', '"it\'s back"',
    # "The sold-out tees are back" (observed live 2026-07-02): fetch the
    # scarcity-word class so the sold-out…back positive has something to judge;
    # plain scarcity mail ("almost sold out!") carries no positive and is
    # rejected by the classifier as before.
    '"sold out"',
)
_SUBJECT_QUERY = "subject:(" + " OR ".join(_SUBJECT_TERMS) + ")"

# Phrases that read like a *future* alert / scarcity / signup-ack, excluded from
# the all-time link so it roughly mirrors the digest section.
_ALL_TIME_QUERY = (
    "subject:(" + " OR ".join(
        f'"{p}"' for p in ("back in stock", "now available", "restocked",
                            "available again", "in stock again")) + ")"
    + ' -"back in stock soon" -"coming soon" -"pre-order" -"preorder"'
    + ' -"sign up" -"you\'ll be notified"'
)

# --- Precision (the authority, applied in dedupe) ----------------------------
#
# Positive: present-tense "it is back" wording. Negative: future-tense alerts
# ("back in stock soon"), scarcity marketing ("almost gone", "selling fast"),
# new-product launches, and the signup *acknowledgement* a Back-in-Stock app
# sends ("you'll be notified when X is back in stock") — which would otherwise
# match the positive phrase inside a future-tense sentence.
#
# Tuned against a real 365-day inbox 2026-07-02 (issue #15) — the classes below
# were all observed; don't loosen casually:
#   * "back in stock" is often HYPHENATED in personal BIS alerts ("Your
#     back-in-stock notification just came through") — the very emails this
#     feature exists to catch — so the phrase allows either separator.
#   * Curly apostrophes (U+2018/2019) are the norm in marketing subjects
#     ("It’s back", "you’ll be notified"): they broke BOTH the it's-back
#     positive and the you'll-be-notified excludes, so subjects are normalised
#     to ASCII quotes before matching (``_normalize_subject_text``).
#   * Live restock-noun announcements ("RESTOCK! <item>", "<item> Restock",
#     "Restock is Now Live!", "a huge restock just hit") carried no positive
#     phrase at all; matched now via ``_STANDALONE_RESTOCK_RE`` + the
#     restock-verb patterns, with excludes guarding the noisy noun.
#   * bare "now available" flooded in from non-shopping mail (bank statements,
#     movie tickets, software "now available on your plan / on the Platform")
#     → document/ticket word excludes + the "now available on/in your|the"
#     platform-launch shape.
_RESTOCK_RE = re.compile(
    r"\bback[- ]in[- ]stock\b"
    r"|\bnow available\b"
    r"|\brestocked\b"
    r"|\bavailable again\b"
    r"|\bin stock again\b"
    r"|\bback in (?:your )?size\b"
    r"|\bit'?s back\b"
    r"|\bwe (?:found|got|have) more\b"
    r"|\brestock\b[^.\n]{0,20}\b(?:now\s+)?live\b"   # "Restock is Now Live!"
    r"|\brestock (?:just )?(?:hits?|dropped|landed)\b"  # "restock just hit"
    r"|\brestock you'?ve been waiting for\b"
    r"|\ba (?:huge|massive|big|major) restock\b"
    r"|\bsold[- ]?out\b[^.\n]{0,30}\bback\b",        # "the sold-out tees are back"
    re.IGNORECASE,
)
# A subject that *leads or ends* with the bare noun ("RESTOCK! <item>",
# "<item> Restock") is an announcement; mid-sentence "restock" (support-ticket
# replies, "time to restock" replenishment nudges) is not. Checked after emoji
# stripping so a trailing 🚨/💡 doesn't hide the tail position.
_STANDALONE_RESTOCK_RE = re.compile(
    r"^restock\b|\brestock\s*[!:]*\s*$", re.IGNORECASE,
)
# Disqualifiers: future ("not yet back"), launch (different email type), the
# signup *acknowledgement*, support-thread replies, replenishment marketing,
# and non-product "now available" mail. Deliberately NOT scarcity ("low stock",
# "selling fast", "almost gone") — those never match a positive on their own, so
# excluding them only does harm by dropping legit "back in stock — selling fast"
# emails. And NOT a bare "signed up": the most common BIS subject is "the item
# you signed up for is back in stock", so only the explicit ack phrasings are
# excluded ("thanks for signing up", "sign up to/for", "you'll be notified").
_EXCLUDE_RE = re.compile(
    r"back in stock soon|coming soon|pre-?order|new arrival|almost back\b"
    r"|join the (?:waitlist|list)|sign up (?:to|for|now|today)"
    r"|thank(?:s| you)[^.\n]{0,40}signing up"
    r"|you'?ll be notified|you are now signed|you'?re signed up"
    r"|we'?ll (?:notify|email|let you know|text)|notify you when"
    r"|will be back|when (?:it'?s|this is|they'?re) back"
    # observed 2026-07-02 (issue #15):
    r"|^\s*(?:re|fwd|fw)\s*:"            # support-thread replies
    r"|question about"
    r"|time to restock"                   # replenishment marketing, not BIS
    r"|upcoming restock|restock (?:is )?coming|\bincoming\b"
    r"|drops? tomorrow|date confirmed|get early access"
    r"|\b(?:statements?|invoices?|bills?|tax (?:forms?|documents?)|1099|w-?2"
    r"|tickets?)\b"                       # documents + movie/support tickets
    r"|now available (?:on|in) (?:your|the)\b",  # platform/feature launches
    re.IGNORECASE,
)


def _normalize_subject_text(subject: str) -> str:
    """Normalise a subject for classification: unify curly apostrophes to
    ASCII (they broke both positives and excludes — observed live), strip
    emoji so decorations don't hide a leading/trailing token, and collapse
    whitespace."""
    s = (subject or "").replace("’", "'").replace("‘", "'")
    s = _EMOJI_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def is_restock_email(subject: str, from_header: str = "", body: str = "") -> bool:
    """True when a message announces an item is *currently* back in stock.

    Precision authority over the broad Gmail fetch: the subject must carry a
    present-tense restock phrase (``_RESTOCK_RE``, or the standalone
    restock-noun announcement shape) and must not read as a future alert /
    scarcity / signup acknowledgement / support thread (``_EXCLUDE_RE``).
    ``from_header`` / ``body`` are accepted for parity / future tuning but
    classification is subject-anchored (restock emails always signal there)."""
    s = _normalize_subject_text(subject)
    if _EXCLUDE_RE.search(s):
        return False
    return bool(_RESTOCK_RE.search(s) or _STANDALONE_RESTOCK_RE.search(s))


# --- Shop / item / size parsing ---------------------------------------------

_SHOP_VIA_RE = re.compile(r"\s+via\s+.*$", re.IGNORECASE)
_GENERIC_NAMES = frozenset({
    "no-reply", "no reply", "noreply", "donotreply", "do-not-reply",
    "info", "support", "hello", "hi", "team", "orders", "order",
    "notifications", "notification", "mail", "email", "store", "shop",
    "sales", "help", "contact", "service", "news", "newsletter", "alerts",
})


def _shop_from_sender(from_header: str) -> str:
    """Best-effort shop name from a ``From`` header (display name, else domain)."""
    name, addr = parseaddr(from_header or "")
    name = (name or "").strip().strip('"').strip()
    if name:
        name = _SHOP_VIA_RE.sub("", name).strip()
        if name and name.lower() not in _GENERIC_NAMES:
            return name
    domain = (addr.split("@", 1)[1].strip().lower() if "@" in (addr or "") else "")
    if domain.startswith("www."):
        domain = domain[4:]
    return domain or "(unknown shop)"


# Subject decorations stripped before pulling out the item name.
_LEADING_FILLER_RE = re.compile(
    r"^\s*(?:good news|great news|guess what|psst|hey|hooray|woohoo|yay|"
    r"the wait is over|you'?re in luck|it'?s back|notice that|"
    r"(?:just a )?heads up)\s*[!,:\-–—]*\s*",
    re.IGNORECASE,
)
# "Back in stock: Item" / "RESTOCK! Item" / "[Restocked] Item" → trailing item.
_AFTER_COLON_RE = re.compile(
    r"(?:back[- ]in[- ]stock|now available|restock(?:ed)?|available again"
    r"|in stock again)"
    r"\s*[:!\-–—\]]\s*(.+)$",
    re.IGNORECASE,
)
# "Item is back in stock" / "Item is now available" → capture the leading item.
_BEFORE_PHRASE_RE = re.compile(
    r"^(.+?)\s+is\s+(?:now\s+)?(?:back[- ]in[- ]stock|now available"
    r"|available again|back|in stock again|restocked)\b",
    re.IGNORECASE,
)
# "Item Restocked" / "Ransom Chain: Restocked" → capture the leading item.
# An adverb before the verb ("Tees Just Restocked!") is eaten, not captured.
_TRAILING_RESTOCK_RE = re.compile(
    r"^(.+?)[\s:\-–—,]+(?:just|now|finally|officially)?\s*restock(?:ed)?"
    r"\s*[!.]*\s*$",
    re.IGNORECASE,
)
# A leading capture that ends in a pronoun is sentence debris ("You asked, we
# restocked"), not a product name.
_PRONOUN_TAIL_RE = re.compile(r"\b(?:you|we|they|it|i)$", re.IGNORECASE)
# Placeholder nouns that name no actual product ("your item is back in stock");
# better to show the subject line than a bogus item called "item".
_GENERIC_ITEMS = frozenset({
    "item", "items", "product", "products", "order", "favorites", "favourites",
    "favorite", "favourite",
})


def _finalize_item(item: str) -> str | None:
    """Last-pass tidy of an extracted item: drop a dangling conjunction
    ("Website Unlocked &") and refuse placeholder nouns."""
    item = re.sub(r"(?:\s*(?:&|\+|and))+\s*$", "", item, flags=re.IGNORECASE)
    item = item.strip()
    if not item or item.lower() in _GENERIC_ITEMS:
        return None
    return item
# Size tokens. "size: X" / "size - X" trusts a separator; bare "size X" requires
# the value to look like a size (digits or 1-4 of X/S/M/L) so "size guide" /
# "size up?" don't read as a size.
_SIZE_RE = re.compile(
    r"\bin (?:a )?size\s+([A-Za-z0-9]{1,5})\b"
    r"|\bsize\s*[:\-]\s*([A-Za-z0-9]{1,5})\b"
    r"|\bsize\s+(\d{1,2}|[XSML]{1,4})\b"
    r"|\(\s*(?:size\s*)?([0-9]{1,2}|XX?S|S|M|L|XX?X?L)\s*\)",
    re.IGNORECASE,
)
# Covers the main emoji planes plus ‼/⁉ and the ⭐-class symbol block, all
# observed as subject decoration ("‼️One Piece Tees Just Restocked!").
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF☀-➿️‼⁉⬀-⯿]")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _EMOJI_RE.sub("", text or "")).strip(" \t—–-:!,")


def _strip_leadin(item: str) -> str:
    """Drop a possessive lead-in ("Your <item>", "The <item>")."""
    return re.sub(r"^(?:your|the|a|an)\s+", "", item, flags=re.IGNORECASE).strip()


def extract_item(subject: str) -> str | None:
    """Best-effort product name from a restock subject, or ``None``.

    Handles the three common shapes ("Back in stock: <item>", "<item> is back
    in stock", "<item> Restocked"), stripping leading filler ("Good news —")
    and emoji. Returns ``None`` when nothing recognisable remains."""
    s = _normalize_subject_text(subject)
    s = _LEADING_FILLER_RE.sub("", s)
    m = _AFTER_COLON_RE.search(s)
    if m:
        return _finalize_item(_clean(m.group(1)))
    m = _BEFORE_PHRASE_RE.search(s)
    if m:
        return _finalize_item(_strip_leadin(_clean(m.group(1))))
    m = _TRAILING_RESTOCK_RE.search(s)
    if m:
        item = _strip_leadin(_clean(m.group(1)))
        # A colon inside the capture is sentence debris ("IT'S HERE: Our July
        # Restock"), as is a pronoun tail — this shape is the loosest of the
        # three, so it gets the extra guards.
        if item and ":" not in item and not _PRONOUN_TAIL_RE.search(item):
            return _finalize_item(item)
    return None


def extract_size(subject: str, body: str = "") -> str | None:
    """Best-effort size token from the subject (then body), or ``None``."""
    for text in (subject or "", (body or "")[:1000]):
        m = _SIZE_RE.search(text)
        if m:
            tok = next((g for g in m.groups() if g), None)
            if tok:
                return tok.strip().upper()
    return None


# --- Query + link builders ---------------------------------------------------

def search_query(days: int = 7) -> str:
    """Gmail-search expression for the IMAP ``X-GM-RAW`` fetch (recent window)."""
    return f"{_SUBJECT_QUERY} newer_than:{days}d"


def all_restocks_url() -> str:
    """Web link to a Gmail search for back-in-stock emails (no date bound)."""
    return _MAIL_BASE + "search/" + quote(_ALL_TIME_QUERY, safe="")


# --- Dedupe + render ---------------------------------------------------------

_SUBJECT_PREFIX_RE = re.compile(r"^\s*(re|fwd|fw)\b\s*[:\-!]*\s*", re.IGNORECASE)
_MAX_SUBJECT = 120
_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


def _normalize_subject(subject: str) -> str:
    s = (subject or "").strip()
    while True:
        stripped = _SUBJECT_PREFIX_RE.sub("", s)
        if stripped == s:
            break
        s = stripped
    s = re.sub(r"[#\d]+", " ", s.lower())
    s = re.sub(r"[^a-z\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _dedupe_key(shop: str, item: str | None, subject: str) -> tuple[str, str]:
    base = (shop or "").strip().lower()
    if item:
        return base, "item:" + item.strip().lower()
    return base, "subj:" + _normalize_subject(subject)


def _parse_dt(raw: str | None) -> datetime | None:
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
    return s if len(s) <= _MAX_SUBJECT else s[: _MAX_SUBJECT - 1].rstrip() + "…"


def _is_newer(a: datetime | None, b: datetime | None) -> bool:
    return (a or _MIN_DT) > (b or _MIN_DT)


def dedupe(emails: list[dict], *, now: datetime | None = None) -> list[dict]:
    """Collapse back-in-stock emails to one entry per ``(shop, item)``, newest
    first. Returns render dicts the digest consumes directly::

        {"shop": str, "item": str | None, "size": str | None,
         "subject": str, "date_iso": str | None, "days_ago": int | None,
         "url": str | None}
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()

    best: dict[tuple[str, str], dict] = {}
    for em in emails or []:
        if not isinstance(em, dict):
            continue
        subject = em.get("subject") or ""
        from_header = em.get("from", "")
        body = em.get("body_text") or ""
        if not is_restock_email(subject, from_header, body):
            continue
        shop = _shop_from_sender(from_header)
        item = extract_item(subject)
        size = extract_size(subject, body)
        key = _dedupe_key(shop, item, subject)
        dt = _parse_dt(em.get("date"))
        entry = {
            "shop": shop, "item": item, "size": size,
            "subject": subject, "dt": dt,
            "url": email_permalink(em.get("message_id"), em.get("id")),
        }
        prior = best.get(key)
        if prior is None or _is_newer(dt, prior["dt"]):
            best[key] = entry

    ordered = sorted(best.values(), key=lambda e: (e["dt"] or _MIN_DT, e["shop"].lower()),
                     reverse=True)
    out: list[dict] = []
    for e in ordered:
        dt = e["dt"]
        out.append({
            "shop": e["shop"],
            "item": e["item"],
            "size": e["size"],
            "subject": _clip_subject(e["subject"]),
            "date_iso": dt.date().isoformat() if dt else None,
            "days_ago": (today - dt.date()).days if dt else None,
            "url": e["url"],
        })
    return out
