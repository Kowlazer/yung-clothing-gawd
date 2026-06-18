"""Pull the Gmail Promotions tab to surface early sale announcements and
exclusive discount codes that don't appear on shop homepages.

Outputs per run:
  * Attributed promo codes — merged into codes.json with source="email".
  * Unattributed promo codes (sender domain not in shop_aliases) — merged
    into codes.json with source="email_unattributed", shop=sender_domain.
  * Sale-announcement signals — list of {email_id, shop, subject,
    body_excerpt, email_date} fed into the existing resolve_fuzzy batched
    Anthropic call as a 4th task type. Only emitted when the email is
    attributable (we can't ask Claude whether a shop is on sale without
    knowing which shop). ``email_date`` (ISO YYYY-MM-DD, "" if unparseable)
    anchors Claude's resolution of relative sale-window phrases.

Auth: IMAP with a Google App Password.

  Why IMAP and not the Gmail REST API + OAuth?
    OAuth refresh tokens for "External + Testing" apps expire after 7 days,
    and publishing to "In Production" requires Google verification of the
    `gmail.readonly` restricted scope (demo video + third-party security
    assessment). For a personal-cron tool that's pure overhead. App passwords
    are static credentials that don't expire until revoked.

  How to generate the app password (one-time):
    1. Enable 2-Step Verification on the Google account.
    2. Visit https://myaccount.google.com/apppasswords
    3. Generate a 16-character app password — paste it (with or without the
       display-spaces; we strip them) into GMAIL_APP_PASSWORD.
    4. Set GMAIL_USERNAME to the full Gmail address.

Failure isolation: every error surfaces to main.py via exception so the
caller can degrade the whole Gmail step to empty results. A Gmail outage
must never block the rest of the run.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from src.codes import (
    _CODE_CONTEXT_RE,
    _CODE_TOKEN_RE,
    _canonicalise_code,
    _classify_confidence,
    _is_valid_code,
)

log = logging.getLogger(__name__)

_IMAP_HOST = "imap.gmail.com"
_IMAP_PORT = 993
# Gmail's X-GM-RAW extension supports the full Gmail search-bar syntax,
# so the same `category:promotions newer_than:2d` filter we'd have used in
# the REST API works verbatim here.
DEFAULT_QUERY = "category:promotions newer_than:2d"
_MAX_MESSAGES = 50           # safety cap; 2-day Promotions tab is well under
_MAX_REVIEW_MESSAGES = 100   # safety cap for the 30-day review-request fetch
_MAX_RESTOCK_MESSAGES = 100  # safety cap for the back-in-stock email fetch
_BODY_EXCERPT_LIMIT = 1500   # chars sent to Claude per email
_TIMEOUT = 30.0              # imaplib timeout (seconds) for socket-level reads
# Default look-back for the "am I already on this shop's list?" inference.
# 18 months catches seasonal-only senders without counting brands you
# unsubscribed from years ago.
_SUBSCRIBED_WINDOW_DAYS = 540

# Match the trailing domain of an email address: "Foo <bar@aniqi.com>" -> "aniqi.com"
_FROM_DOMAIN_RE = re.compile(r"@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
# Parse "<UID> (X-GM-MSGID <id> BODY[] {<n>}" preamble from imaplib fetch responses.
_FETCH_META_RE = re.compile(rb"X-GM-MSGID\s+(\d+)")


# ---------------------------------------------------------------------------
# IMAP connection + fetch
# ---------------------------------------------------------------------------

def _connect(username: str, app_password: str) -> imaplib.IMAP4_SSL:
    """Open an authenticated IMAP4 SSL connection to Gmail. The caller owns the
    returned object and is responsible for closing it (or letting the OS clean
    up on process exit — Gmail tolerates orphaned connections fine)."""
    imap = imaplib.IMAP4_SSL(_IMAP_HOST, _IMAP_PORT, timeout=_TIMEOUT)
    # Google displays app passwords as "abcd efgh ijkl mnop" with spaces;
    # the actual credential is the 16 chars with the spaces stripped.
    imap.login(username, app_password.replace(" ", ""))
    return imap


def _xgmraw_quote(query: str) -> str:
    """Wrap a Gmail-search query as an IMAP quoted-string for ``X-GM-RAW``.

    Internal backslashes and double-quotes are backslash-escaped per RFC 3501
    so phrase queries (``"how did it go"`` in the review-request search) survive
    the wrapper intact. Quote-free queries (the Promotions filter) are unchanged.
    """
    escaped = query.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _search_and_fetch(
    client: imaplib.IMAP4,
    query: str,
    max_messages: int,
    skip: set[str],
) -> list[dict]:
    """Run one ``X-GM-RAW`` search against the selected mailbox and return parsed
    messages. Shared by ``fetch_promotions`` and ``fetch_review_requests``;
    caller owns mailbox selection and connection lifecycle."""
    # X-GM-RAW lets us pass a Gmail-search-syntax query as-is rather than
    # rewriting it into the limited IMAP SEARCH grammar.
    typ, data = client.uid("SEARCH", "X-GM-RAW", _xgmraw_quote(query))
    if typ != "OK" or not data or not data[0]:
        return []
    uids = data[0].split()
    if not uids:
        return []
    # Gmail returns oldest-first; keep the most-recent slice when over the cap.
    uids = uids[-max_messages:]

    out: list[dict] = []
    for uid in uids:
        try:
            typ, msg_data = client.uid("FETCH", uid, "(X-GM-MSGID BODY.PEEK[])")
        except imaplib.IMAP4.error as exc:
            log.info("gmail: fetch uid %s failed: %s", uid, exc)
            continue
        if typ != "OK":
            continue
        parsed = _parse_fetch_response(msg_data)
        if not parsed:
            continue
        gm_msgid, raw_message = parsed
        if gm_msgid in skip:
            continue
        try:
            msg = email.message_from_bytes(raw_message)
        except Exception as exc:  # noqa: BLE001 — defensive
            log.info("gmail: parse uid %s failed: %s", uid, exc)
            continue
        out.append(_parse_message(gm_msgid, msg))
    return out


def fetch_promotions(
    username: str,
    app_password: str,
    *,
    imap_client: imaplib.IMAP4 | None = None,
    query: str = DEFAULT_QUERY,
    max_messages: int = _MAX_MESSAGES,
    skip_ids: set[str] | None = None,
) -> list[dict]:
    """Fetch matching Promotions-tab messages and return their parsed contents.

    Each returned dict: ``{id, from, subject, snippet, body_text, date,
    message_id}``. ``id`` is the Gmail X-GM-MSGID — a 64-bit identifier stable
    across the entire account (the same email seen under different labels has one
    ID), making it the right key for dedup.

    ``skip_ids``: message IDs already processed in prior runs (dedup state
    from ``gmail_state.processed_ids``). Their bodies aren't fetched.

    ``imap_client`` is injectable for tests; when omitted, a fresh
    connection is opened with ``username`` + ``app_password`` and closed at
    the end of the call.
    """
    skip = skip_ids or set()
    own_client = imap_client is None
    client = imap_client or _connect(username, app_password)
    try:
        client.select("INBOX", readonly=True)
        return _search_and_fetch(client, query, max_messages, skip)
    finally:
        if own_client:
            try:
                client.logout()
            except Exception:  # noqa: BLE001 — connection may already be dead
                pass


def fetch_review_requests(
    username: str,
    app_password: str,
    *,
    imap_client: imaplib.IMAP4 | None = None,
    query: str | None = None,
    days: int = 30,
    max_messages: int = _MAX_REVIEW_MESSAGES,
) -> list[dict]:
    """Fetch recent post-purchase *review-request* emails (last ``days`` days).

    Returns the same parsed-message dicts as ``fetch_promotions`` (now carrying
    ``message_id`` for deep-linking). Unlike the promo fetch this is **stateless**
    — no ``skip_ids``: the daily run re-fetches the whole window every time so
    the digest's review-request section reflects the last ``days`` days afresh and
    can dedupe follow-ups. The query defaults to
    ``review_requests.search_query(days)`` (overridable for tests). Selecting
    INBOX covers both the Promotions tab and the main inbox (Gmail tabs are
    inbox sub-categories); the digest's all-time link searches everything.
    """
    # Imported lazily to avoid a module-import cycle (review_requests is pure and
    # has no IMAP deps, but keeping the dependency one-directional is cleaner).
    from src.review_requests import search_query

    q = query if query is not None else search_query(days)
    own_client = imap_client is None
    client = imap_client or _connect(username, app_password)
    try:
        client.select("INBOX", readonly=True)
        return _search_and_fetch(client, q, max_messages, set())
    finally:
        if own_client:
            try:
                client.logout()
            except Exception:  # noqa: BLE001 — connection may already be dead
                pass


def fetch_restock_emails(
    username: str,
    app_password: str,
    *,
    imap_client: imaplib.IMAP4 | None = None,
    query: str | None = None,
    days: int = 7,
    max_messages: int = _MAX_RESTOCK_MESSAGES,
) -> list[dict]:
    """Fetch recent *back-in-stock* notification emails (last ``days`` days).

    Same parsed-message dicts as ``fetch_promotions`` (carrying ``message_id``
    for deep-linking) and, like ``fetch_review_requests``, **stateless** — no
    ``skip_ids``; the daily run re-fetches the whole window and dedupes. The
    query defaults to ``restock_emails.search_query(days)`` (overridable for
    tests). INBOX covers both the Promotions tab and the main inbox.
    """
    from src.restock_emails import search_query

    q = query if query is not None else search_query(days)
    own_client = imap_client is None
    client = imap_client or _connect(username, app_password)
    try:
        client.select("INBOX", readonly=True)
        return _search_and_fetch(client, q, max_messages, set())
    finally:
        if own_client:
            try:
                client.logout()
            except Exception:  # noqa: BLE001 — connection may already be dead
                pass


def subscribed_shop_domains(
    username: str,
    app_password: str,
    domains,
    *,
    imap_client: imaplib.IMAP4 | None = None,
    days: int = _SUBSCRIBED_WINDOW_DAYS,
    category: str = "promotions",
) -> set[str]:
    """Return the subset of ``domains`` you already receive marketing mail from.

    For each domain runs one cheap Gmail ``X-GM-RAW`` SEARCH
    (``from:<domain> category:<category> newer_than:<days>d``) — UIDs only, no
    bodies fetched. A non-empty result means a marketing email from that brand
    landed in your Promotions tab inside the window, i.e. you're on their list.

    Used by ``newsletter_signup`` to skip shops you're already subscribed to so
    it never re-submits your address (pointless activity + extra bot-detection
    surface). ``category`` is the Gmail tab used as the "this is marketing"
    proxy, so one-off order confirmations (which land in Updates/Primary) don't
    count as a newsletter subscription. Domains are de-duplicated and lowercased.

    ``imap_client`` is injectable for tests; otherwise a connection is opened
    and closed here. A per-domain SEARCH error is logged and skipped (so one bad
    domain never aborts the batch), but a connection-level failure propagates so
    the caller can decide to proceed without skipping.
    """
    wanted: list[str] = []
    seen: set[str] = set()
    for d in domains or []:
        dl = (d or "").strip().lower()
        if dl and dl not in seen:
            seen.add(dl)
            wanted.append(dl)
    if not wanted:
        return set()

    own_client = imap_client is None
    client = imap_client or _connect(username, app_password)
    try:
        client.select("INBOX", readonly=True)
        subscribed: set[str] = set()
        for domain in wanted:
            query = f"from:{domain} category:{category} newer_than:{days}d"
            try:
                typ, data = client.uid("SEARCH", "X-GM-RAW", _xgmraw_quote(query))
            except imaplib.IMAP4.error as exc:
                log.info("gmail: subscription search for %s failed: %s", domain, exc)
                continue
            if typ == "OK" and data and data[0] and data[0].split():
                subscribed.add(domain)
        return subscribed
    finally:
        if own_client:
            try:
                client.logout()
            except Exception:  # noqa: BLE001 — connection may already be dead
                pass


def _parse_fetch_response(msg_data: list) -> tuple[str, bytes] | None:
    """Pull (X-GM-MSGID, raw_message_bytes) out of an imaplib UID FETCH response.

    imaplib's fetch returns a list with this shape (the literal mode):

        [
          (b'1 (X-GM-MSGID 17012345678901234 BODY[] {2345}', b'<raw RFC-822>'),
          b')',
        ]

    Older / simpler IMAP servers might inline everything as one bytes blob;
    we handle both. Returns None on malformed responses (a defensive bail-out
    so one bad message doesn't kill the batch)."""
    for item in msg_data:
        if isinstance(item, tuple) and len(item) >= 2:
            meta, body = item[0], item[1]
            m = _FETCH_META_RE.search(meta or b"")
            if not m:
                continue
            return (m.group(1).decode("ascii"), body)
    return None


# ---------------------------------------------------------------------------
# Message parsing (email.Message → flat dict)
# ---------------------------------------------------------------------------

def _parse_message(gm_msgid: str, msg: Message) -> dict:
    """Flatten an ``email.Message`` into the shape downstream code expects."""
    subject = _header(msg, "Subject")
    return {
        "id": gm_msgid,
        "from": _header(msg, "From"),
        "subject": subject,
        # Gmail's REST API gave us a "snippet" field — IMAP doesn't, but we
        # can synthesize a short preview from the body for parity / logging.
        "snippet": "",
        "body_text": _extract_body_text(msg),
        "date": _header(msg, "Date"),
        # RFC-822 Message-ID — used by review_requests.email_permalink to build a
        # `#search/rfc822msgid:` Gmail deep link. Ignored by the promo pipeline.
        "message_id": _header(msg, "Message-ID"),
    }


def _header(msg: Message, name: str) -> str:
    """Return a header value, decoded to a plain string. Returns '' if missing.

    Gmail sometimes encodes non-ASCII headers (subject with emoji, etc.) using
    RFC 2047 word-encoding. ``email.header.decode_header`` handles that."""
    raw = msg.get(name)
    if not raw:
        return ""
    try:
        parts = email.header.decode_header(raw)
        out: list[str] = []
        for chunk, enc in parts:
            if isinstance(chunk, bytes):
                out.append(chunk.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(chunk)
        return "".join(out).strip()
    except Exception:  # noqa: BLE001 — header decoding is best-effort
        return str(raw).strip()


# Markers that indicate the text/plain part is just a "this email is HTML-
# only, view in browser" stub and the real content lives in text/html.
# Anchored at word boundaries so we don't over-match (e.g. ``onlyview``).
_PLAIN_STUB_MARKER_RE = re.compile(
    r"\b(?:"
    r"html[\s-]?only"               # "HTML-only", "HTML only"
    # "view in browser" / "view this email in your browser" / "view online" /
    # "view the web version" / "view this message from <shop> in a web browser"
    # (Old Navy). A few words (shop name, "this message from X") may sit between
    # the verb and the destination, so allow a short word run in between.
    r"|view\b[\w\s,]{0,40}?\b(?:in\s+(?:your\s+|a\s+)?(?:web\s+)?browser|online|web\s+version)"
    r")\b",
    re.IGNORECASE,
)
# Click-tracking URLs (often hundreds of chars) and decorative divider runs
# inflate an otherwise-empty stub past the length ceiling — Old Navy's
# "view in browser" link alone is ~900 chars. Stripped before measuring the
# human-readable remainder in _is_stub_plain.
_STUB_NOISE_RE = re.compile(r"https?://\S+|[-=_*.]{3,}", re.IGNORECASE)
# A "stub" text/plain part is one whose human-readable content is short AND
# mentions the markers above. ~800 chars chosen empirically: the Anime Ape stub
# is ~500, and the shortest faithful plain we've observed (Wooj VIP) is ~1.5KB.
_PLAIN_STUB_MAX_CHARS = 800
# A *near-empty* text/plain part is a stub even without a marker phrase: some
# senders ship a bare placeholder (e.g. a literal "backup", 6 chars) in the
# plain slot while the real receipt lives in text/html. When essentially
# no human-readable content sits in the plain slot but a full text/html
# alternative exists (guaranteed by the _extract_body_text call site), the HTML
# is the real message. Kept tight so a genuine short plain part is never mistaken
# for a stub: the shortest substantive plain we keep is "plain version" (13
# chars), so the ceiling sits a clear margin below that. Raise only if a longer
# near-empty placeholder variant turns up.
_PLAIN_STUB_EMPTY_MAX_CHARS = 10


def _extract_body_text(msg: Message) -> str:
    """Walk MIME parts and return text. Prefers text/plain; falls back to
    text/html → stripped via BeautifulSoup.

    **Stub detection** (added 2026-05-25 after the Anime Ape MEMORL20 miss):
    some shops send a token text/plain part (a few hundred chars saying
    ``This email is HTML-only — view in browser``) alongside a real
    text/html body that holds the actual promo code. The legacy "plain wins
    whenever present" rule meant we read the stub and never reached the
    code. When text/plain is both short AND contains a stub marker — or is
    near-empty (a bare placeholder like "backup", caught on length alone) —
    we now use the HTML body instead. A faithful long text/plain (Wooj VIP,
    Wayfair, …) still wins, preserving the current Claude-payload signal.
    """
    plain: list[str] = []
    html_chunks: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        # Skip attachments and inline images.
        disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            decoded = payload.decode("utf-8", errors="replace")
        if ctype == "text/plain":
            plain.append(decoded)
        else:
            html_chunks.append(decoded)

    plain_text = "\n".join(plain) if plain else ""

    # Some senders (Staples among them) ship raw HTML in the text/plain part
    # instead of a faithful plaintext rendering. Strip it so the code regex
    # never sees `<!DOCTYPE …>` / hex-color artifacts as bogus promo codes.
    if plain_text and _looks_like_html(plain_text):
        plain_text = _html_to_text(plain_text)

    # When BOTH parts exist, watch for the stub pattern.
    if plain_text and html_chunks and _is_stub_plain(plain_text):
        return _html_to_text("\n".join(html_chunks))

    if plain_text:
        return plain_text
    if html_chunks:
        return _html_to_text("\n".join(html_chunks))
    return ""


def _is_stub_plain(text: str) -> bool:
    """True if a text/plain MIME part looks like a "this email is HTML-only,
    view in browser" stub rather than a faithful plain-text rendering of
    the message body. See ``_extract_body_text`` for the rationale.

    Length is measured on the URL/divider-stripped remainder: a stub padded by
    one long click-tracking link (Old Navy's is ~900 chars) would otherwise read
    as "long" and slip past the ceiling, so its HTML body — the real receipt —
    never gets read.

    Two shapes count as a stub: a *near-empty* plain part (a bare placeholder
    like "backup" — caught on length alone, since no real receipt is that
    short), or a *short* plain part carrying an explicit "view in browser /
    HTML-only" marker."""
    meaningful = _STUB_NOISE_RE.sub(" ", text)
    meaningful = re.sub(r"\s+", " ", meaningful).strip()
    if len(meaningful) < _PLAIN_STUB_EMPTY_MAX_CHARS:
        return True
    return (
        len(meaningful) < _PLAIN_STUB_MAX_CHARS
        and bool(_PLAIN_STUB_MARKER_RE.search(text))
    )


# Structural HTML tags that betray a text/plain part actually carrying HTML.
# A faithful plaintext body never contains these. Requiring a real tag shape
# (`<tag` / `</tag` / `<!doctype`) avoids tripping on prose like "5 < 10".
_HTML_IN_PLAIN_RE = re.compile(
    r"<!doctype\b|</?(?:html|head|body|table|tr|td|div|span|br|img|style|font)\b",
    re.IGNORECASE,
)


def _looks_like_html(text: str) -> bool:
    """True if a text/plain part actually contains HTML markup (a misconfigured
    sender shipping HTML in the plain slot). See ``_extract_body_text``."""
    return bool(_HTML_IN_PLAIN_RE.search(text))


def _html_to_text(html: str) -> str:
    """Strip HTML to readable text, keeping paragraph breaks so line-based
    code-context matching downstream still works."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    # Collapse runs of blank lines but preserve single newlines as segment breaks.
    return re.sub(r"\n{2,}", "\n", text)


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

def _sender_domain(from_header: str) -> str | None:
    m = _FROM_DOMAIN_RE.search(from_header or "")
    if not m:
        return None
    return m.group(1).lower()


def _aliases_by_domain(shop_aliases: dict[str, str]) -> dict[str, str]:
    """Build a ``{domain → canonical_shop_name}`` reverse index from
    ``shop_aliases.json`` (whose values are homepage URLs)."""
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


def _attribute(
    email: dict,
    domain_index: dict[str, str],
    known_shops: list[str],
) -> str | None:
    """Return the canonical shop name for ``email``, or None.

    Two-pass:
      1. Sender domain reverse-lookup (try exact, then parent domains so
         ``mail.aniqi.com`` resolves to ``aniqi.com``).
      2. Case-insensitive substring match of any known shop name in the
         subject (handles emails sent from a marketing-platform domain like
         ``klaviyomail.com`` where the brand is only in the subject).
    """
    domain = _sender_domain(email.get("from", ""))
    if domain:
        if domain in domain_index:
            return domain_index[domain]
        parts = domain.split(".")
        # Try walking up the subdomain chain (mail.aniqi.com -> aniqi.com).
        for i in range(1, len(parts) - 1):
            parent = ".".join(parts[i:])
            if parent in domain_index:
                return domain_index[parent]

    subject = (email.get("subject") or "").lower()
    for shop in known_shops or []:
        if shop and shop.lower() in subject:
            return shop
    return None


# ---------------------------------------------------------------------------
# Code extraction (reuses codes.py regexes)
# ---------------------------------------------------------------------------

def _extract_codes_from_text(text: str) -> list[dict]:
    """Find ``{code, context}`` pairs in arbitrary email body text.

    Uses a sliding ±1-line context window: a code-context word (``code`` /
    ``promo`` / ``coupon`` / ``off`` / ``discount``) may appear on the line
    above, the line itself, or the line below the token. Promo emails
    frequently render the code as its own visual block (centered div, button,
    coloured panel), and BS4's ``get_text(separator="\\n")`` puts that on a
    line by itself — divorced from the surrounding "Use code … at checkout"
    wrapper. The window bridges those splits.

    Tokens must still live on a single line — the window only relaxes WHERE
    the context word can appear, not WHERE the token appears, so false
    positives are bounded by ``_is_valid_code``.

    Codes are canonicalised to uppercase for dedupe: ``SummerSale15`` and
    ``SUMMERSALE15`` collapse to one entry per text.
    """
    lines = [ln.strip() for ln in (text or "").splitlines()]
    results: list[dict] = []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        if not line:
            continue
        # Context may appear on i-1, i, or i+1 — promo emails commonly split
        # "Use code" from the code itself across newlines after HTML strip.
        ctx_start = max(0, i - 1)
        ctx_end = min(len(lines), i + 2)
        ctx_window = " ".join(lines[ctx_start:ctx_end])
        if not _CODE_CONTEXT_RE.search(ctx_window):
            continue
        for token in _CODE_TOKEN_RE.finditer(line):
            raw = token.group(1)
            if not _is_valid_code(raw):
                continue
            code = _canonicalise_code(raw)
            if code in seen:
                continue
            seen.add(code)
            results.append({
                "code": code,
                "ctx_window": ctx_window[:200],
                "confidence": _classify_confidence(raw),
            })
    return results


# ---------------------------------------------------------------------------
# Public signal extraction
# ---------------------------------------------------------------------------

def extract_signals(
    emails: list[dict],
    shop_aliases: dict[str, str],
    known_shops: list[str],
    *,
    now: datetime | None = None,
) -> dict:
    """Mine ``emails`` for promo codes and sale-announcement signals.

    Returns a dict::

        {
          "codes":         list of attributed code entries (source="email"),
          "unattributed":  list of code entries from unknown senders
                            (source="email_unattributed",
                             shop=<sender-domain>),
          "sale_signals":  list of {email_id, shop, subject, body_excerpt} —
                            only for attributable emails (no point asking
                            Claude about a sale at an unknown shop),
          "processed_ids": list of every email id seen this batch (main.py
                            persists these to gmail_state.processed_ids for
                            dedupe on the next run),
        }

    Code entries carry ``first_seen`` and ``last_seen`` timestamps so state.py
    can persist them across runs and prune ones not re-seen in 30 days.
    """
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    domain_index = _aliases_by_domain(shop_aliases)
    codes: list[dict] = []
    unattributed: list[dict] = []
    sale_signals: list[dict] = []
    processed_ids: list[str] = []

    for em in emails or []:
        eid = em.get("id")
        if not eid:
            continue
        processed_ids.append(eid)

        shop = _attribute(em, domain_index, known_shops)
        subject = em.get("subject") or ""
        body = em.get("body_text") or ""
        email_date = _email_date_iso(em.get("date"))
        # Searching subject + body together — the code usually sits in one
        # or the other, and the line-based context heuristic still applies.
        search_text = f"{subject}\n{body}"
        email_codes = _extract_codes_from_text(search_text)

        if shop is not None:
            for c in email_codes:
                codes.append({
                    "shop": shop,
                    "code": c["code"],
                    "context": _format_context(subject, c.get("ctx_window")),
                    "confidence": c.get("confidence", "medium"),
                    "source": "email",
                    "email_id": eid,
                    "first_seen": now_iso,
                    "last_seen": now_iso,
                })
            sale_signals.append({
                "email_id": eid,
                "shop": shop,
                "subject": subject,
                "body_excerpt": _excerpt(body),
                # Anchor for resolving relative sale-window phrases to absolute
                # dates downstream (claude_fuzzy Task 4). "" when unparseable.
                "email_date": email_date,
            })
        else:
            domain = _sender_domain(em.get("from", "")) or "(unknown)"
            for c in email_codes:
                unattributed.append({
                    "shop": domain,
                    "code": c["code"],
                    "context": _format_context(subject, c.get("ctx_window")),
                    "confidence": c.get("confidence", "medium"),
                    "source": "email_unattributed",
                    "email_id": eid,
                    "first_seen": now_iso,
                    "last_seen": now_iso,
                })

    return {
        "codes": codes,
        "unattributed": unattributed,
        "sale_signals": sale_signals,
        "processed_ids": processed_ids,
    }


def _email_date_iso(raw: str | None) -> str:
    """Parse an RFC-2822 ``Date`` header into an ISO ``YYYY-MM-DD`` string.

    Returns "" for a missing or unparseable header — downstream this means
    "no anchor date", and Claude leaves the resolved sale window null rather
    than guessing.
    """
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return ""
    if dt is None:
        return ""
    return dt.date().isoformat()


def _excerpt(text: str, limit: int = _BODY_EXCERPT_LIMIT) -> str:
    """Truncate body to a cost-friendly single-line excerpt for Claude."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + " ...[truncated]"


def _format_context(subject: str, ctx_window: str | None) -> str:
    """Render the context string stored on each code dict.

    Carries both the email subject (for shop attribution at a glance) and the
    actual line where the token was found (so when the digest later shows a
    low-confidence code, the user can see exactly what triggered the match
    instead of being stuck guessing at the subject alone).
    """
    subj = (subject or "").strip()
    win = re.sub(r"\s+", " ", (ctx_window or "")).strip()
    if win and win.lower() != subj.lower():
        return f"from email: {subj} | {win[:160]}"
    return f"from email: {subj}".strip()
