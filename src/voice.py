"""Pull Google-Voice-forwarded SMS out of Gmail to surface promo codes and
sale-announcement signals from texts the user receives on their GV number.

Outputs per run (parallel to gmail.extract_signals):
  * Attributed promo codes — merged into codes.json with source="sms".
  * Unattributed promo codes (sender phone number not in sms_aliases) — merged
    into codes.json with source="sms_unattributed", shop=<phone>.
  * Sale-announcement signals — list of {email_id, shop, subject, body_excerpt}
    fed into the existing resolve_fuzzy batched Anthropic call alongside the
    Gmail-derived signals (same shape, no prompt change needed).

Prerequisite (one-time, user side):
  1. At voice.google.com/settings → Messages, enable "Get email notifications
     for new text messages".
  2. Create a Gmail filter on ``from:txt.voice.google.com`` that skips the
     inbox and applies a label (default name expected: "GoogleVoice").

Auth + transport: reuses the existing Gmail IMAP App-Password connection
helpers from gmail.py — no second login.

Attribution: the sender's phone number is encoded directly in the GV-forward
From local-part as ``<user_GV_number>.<sender_number>.<id>@txt.voice.google.com``,
so no body parsing is needed to identify the sender. Two-pass:
  1. Phone-number lookup in sms_aliases (``{"+18334567890": "ShopName"}``).
  2. Case-insensitive substring match of any known shop name in the SMS body
     (many marketing SMS open with the brand name: "Aniqi: new drop...").

Failure isolation: every error surfaces as an exception so main.py can
degrade the whole voice step to empty results. A Gmail/IMAP outage must
never block the rest of the run.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
from datetime import datetime, timezone
from email.message import Message

# Reuse the IMAP-response and MIME helpers from gmail.py to avoid duplicating
# parsing logic. The underscore-prefix is internal-to-package, not private.
from src.codes import (
    _CODE_CONTEXT_RE,
    _CODE_TOKEN_RE,
    _canonicalise_code,
    _classify_confidence,
    _is_valid_code,
)
from src.gmail import (
    _connect,
    _email_date_iso,
    _extract_body_text,
    _header,
    _parse_fetch_response,
)

log = logging.getLogger(__name__)

DEFAULT_LABEL = "GoogleVoice"
# 7-day window vs gmail's 2-day: SMS marketing volume is far lower than email,
# so a wider window costs nothing and gives margin for missed cron runs.
DEFAULT_QUERY = "newer_than:7d"
_MAX_MESSAGES = 100
_BODY_EXCERPT_LIMIT = 1500
_TIMEOUT = 30.0

# Match the From local-part: "<gv_number>.<sender_number>.<id>@txt.voice.google.com"
# Both numbers are digits-only; sender is either a US 11-digit number (1NNNNNNNNNN)
# or a 4-7 digit short code (e.g. 21234). This is the OLDER GV-forward format.
_FROM_LOCAL_RE = re.compile(
    r"<\s*(\d+)\.(\d+)\.[^@>]+@txt\.voice\.google\.com\s*>",
    re.IGNORECASE,
)

# Newer GV-forward format (observed 2026): the From is the generic
# "Google Voice <voice-noreply@google.com>" and the sender's number is in the
# SUBJECT instead ("New text message from 49762" / "... from (844) 619-9172").
# Capture whatever follows "from " at the end of the subject.
_SUBJECT_SENDER_RE = re.compile(r"\bfrom\s+(.+?)\s*$", re.IGNORECASE)
# A GV forward is an SMS we care about unless its subject marks it as a
# voicemail or missed-call notification — those share the label but are not
# texts and must not be parsed as marketing.
_SUBJECT_NON_SMS_RE = re.compile(r"^\s*New (?:voicemail|missed call)\b", re.IGNORECASE)

# One-time-passcode / 2FA texts ("G-976717 is your Google verification code")
# arrive on the same label from short codes. They're never a sale and their
# numeric codes would otherwise pollute the unattributed-codes section, so they
# are skipped at signal extraction.
_VERIFICATION_RE = re.compile(
    r"\b(?:verification|security|one[\s-]?time|authentication|login|sign[\s-]?in)\s+code\b"
    r"|\b(?:2fa|otp)\b"
    r"|\bG-\d{5,8}\b",
    re.IGNORECASE,
)

# Discovery: a marketing SMS almost always opens with the brand name
# ("Harborlight: …", "Greyfox: …"). When such a text isn't attributed to a
# watchlist / allowlist shop, we surface the brand so the user can add it to
# SMS_SALE_SHOPS. Gate on a sale-ish lexeme so transactional/personal texts
# don't flood the discovery list.
_BRAND_LEAD_RE = re.compile(r"^\s*([A-Za-z][\w&'.+ -]{1,38}?):")
_SALE_HINT_RE = re.compile(
    r"(?:\d+%|\$\d+|\bsale\b|\bdeal\b|\boff\b|\bsave\b|\bclearance\b|\bbogo\b|"
    r"\bpromo\b|\bcoupon\b|\bdiscount\b|free ship|\bmarkdown\b|early access|"
    r"\bdrop\b|new (?:items|arrivals|collection|drop)|\bflash\b|today only|"
    r"ends (?:tonight|today|soon)|last chance|back in stock|\brestock\b)",
    re.IGNORECASE,
)

# Reused from gmail.py — extracts the X-GM-MSGID out of imaplib FETCH response.
_FETCH_META_RE = re.compile(rb"X-GM-MSGID\s+(\d+)")

# Body extraction anchors (verified against real GV forward samples, both the
# old and the 2026 templates). The text/plain forward looks like:
#   <blank>
#   <https://voice.google.com>
#   <ACTUAL SMS BODY — one or more lines>
#   To respond to this [text ]message, ...           <- footer (wording varies)
#   ...YOUR ACCOUNT ... HELP CENTER ... footer ...
# The footer marker was reworded from "To respond to this text message, reply
# to this email or visit Google Voice." to "To respond to this message, launch
# Google Voice (...)", so the regex tolerates the optional "text ".
_BODY_HEADER_RE = re.compile(r"<https?://voice\.google\.com>\s*", re.IGNORECASE)
_BODY_FOOTER_RE = re.compile(
    r"To respond to this (?:text )?message[\s\S]*", re.IGNORECASE,
)
# A second, older template variant ends with just the GV boilerplate trailer
# ("YOUR ACCOUNT … HELP CENTER … HELP FORUM …") and no "To respond" line. The
# "YOUR ACCOUNT" + "HELP CENTER" pairing is GV-specific enough not to collide
# with marketing copy; cut from there.
_BODY_TRAILER_RE = re.compile(
    r"\s*YOUR ACCOUNT\b[\s\S]{0,120}?HELP CENTER[\s\S]*", re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_voice_sms(
    username: str,
    app_password: str,
    *,
    imap_client: imaplib.IMAP4 | None = None,
    label: str = DEFAULT_LABEL,
    query: str = DEFAULT_QUERY,
    max_messages: int = _MAX_MESSAGES,
    skip_ids: set[str] | None = None,
) -> list[dict]:
    """Fetch GV-forward SMS messages from ``label`` and return parsed contents.

    Each returned dict::

        {id, from, subject, sms_from_number, sms_body, date}

    ``id`` is the Gmail X-GM-MSGID (64-bit account-stable identifier — same
    key gmail.py uses for dedup).

    ``label`` is the Gmail label whose folder the user routed GV forwards to.
    The user creates this filter themselves (see module docstring); ``"GoogleVoice"``
    is the documented default.

    ``skip_ids``: message IDs already processed in prior runs (dedup state
    from ``voice_state.processed_ids``). Their bodies aren't fetched.

    ``imap_client`` is injectable for tests; when omitted, a fresh
    connection is opened with ``username`` + ``app_password`` and closed at
    the end of the call.
    """
    skip = skip_ids or set()
    own_client = imap_client is None
    client = imap_client or _connect(username, app_password)
    try:
        # Selecting the label by name — Gmail exposes user labels as IMAP
        # folders. Wrap in quotes so labels with spaces also work. readonly=True
        # so reading messages does NOT mark them as read.
        typ, _ = client.select(f'"{label}"', readonly=True)
        if typ != "OK":
            log.warning("voice: cannot select label %r — does the Gmail filter exist?", label)
            return []
        typ, data = client.uid("SEARCH", "X-GM-RAW", f'"{query}"')
        if typ != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()
        if not uids:
            return []
        uids = uids[-max_messages:]

        out: list[dict] = []
        for uid in uids:
            try:
                typ, msg_data = client.uid(
                    "FETCH", uid, "(X-GM-MSGID BODY.PEEK[])"
                )
            except imaplib.IMAP4.error as exc:
                log.info("voice: fetch uid %s failed: %s", uid, exc)
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
                log.info("voice: parse uid %s failed: %s", uid, exc)
                continue
            out.append(_parse_voice_message(gm_msgid, msg))
        return out
    finally:
        if own_client:
            try:
                client.logout()
            except Exception:  # noqa: BLE001 — connection may already be dead
                pass


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------

def _parse_voice_message(gm_msgid: str, msg: Message) -> dict:
    """Flatten a GV-forward ``email.Message`` into the shape downstream
    code expects, with the SMS sender number and body broken out separately."""
    from_hdr = _header(msg, "From")
    subject = _header(msg, "Subject")
    raw_body = _extract_body_text(msg)
    return {
        "id": gm_msgid,
        "from": from_hdr,
        "subject": subject,
        "sms_from_number": _extract_sender_number(from_hdr, subject),
        "sms_body": _extract_sms_body(raw_body),
        "date": _header(msg, "Date"),
        # False for voicemail / missed-call forwards on the same label.
        "is_sms": _is_text_message(subject),
    }


def _is_text_message(subject: str | None) -> bool:
    """A GV forward is an SMS unless its subject marks it as a voicemail or
    missed-call notification (those share the GoogleVoice label)."""
    return not _SUBJECT_NON_SMS_RE.search(subject or "")


def _normalize_number(raw: str | None) -> str | None:
    """Normalise a raw sender token to a stable key.

    ``"(844) 619-9172"`` / ``"8446199172"`` / ``"18446199172"`` -> ``"+18446199172"``
    (E.164 for North-American numbers); a 4-7 digit short code (``"21234"``) is
    kept verbatim. Returns None when the token has no digits.
    """
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    return digits  # short code (4-7 digits) or other; kept verbatim


def _extract_sender_number(from_header: str, subject: str | None = None) -> str | None:
    """Pull the sender's phone number out of a GV forward.

    Two formats are handled:
      * OLD — the number is the second dotted segment of the From local-part:
        ``"<display>" <gv_number>.<sender_number>.<id>@txt.voice.google.com``
      * NEW (2026) — From is the generic ``voice-noreply@google.com`` and the
        number is in the Subject: ``"New text message from (844) 619-9172"`` /
        ``"New text message from 49762"``.

    Normalized output (see :func:`_normalize_number`): US numbers -> E.164
    ``+1…``; short codes kept as bare digits; None if neither format matches.
    """
    m = _FROM_LOCAL_RE.search(from_header or "")
    if m:
        return _normalize_number(m.group(2))
    sm = _SUBJECT_SENDER_RE.search(subject or "")
    if sm:
        return _normalize_number(sm.group(1))
    return None


def _extract_sms_body(raw_text: str) -> str:
    """Strip the GV-forward template wrapper to leave just the SMS body.

    The text/plain part of a GV forward looks like::

        <https://voice.google.com>
        <ACTUAL SMS BODY — one or more lines>
        To respond to this text message, reply to this email or visit Google Voice.
        ...footer...

    Returns the SMS body, stripped of leading/trailing whitespace. Returns
    the original text unchanged if neither marker is present (defensive: better
    to surface noisy text downstream than to swallow content silently).
    """
    if not raw_text:
        return ""
    text = raw_text
    # Cut at the earliest footer/trailer marker (templates vary in which they
    # use, and the older variant has only the "YOUR ACCOUNT … HELP CENTER"
    # trailer with no "To respond" line).
    cut = len(text)
    for pat in (_BODY_FOOTER_RE, _BODY_TRAILER_RE):
        m = pat.search(text)
        if m:
            cut = min(cut, m.start())
    text = _BODY_HEADER_RE.sub("", text[:cut])
    return text.strip()


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

def _attribute_sms(
    sms: dict,
    sms_aliases: dict[str, str],
    known_shops: list[str],
) -> str | None:
    """Return the canonical shop name for ``sms``, or None.

    Two-pass:
      1. Sender phone-number lookup in ``sms_aliases``
         (``{"+18334567890": "Aniqi"}``).
      2. Case-insensitive substring match of any known shop name in the SMS
         body (handles brands that always lead with the shop name in the text
         but whose phone number hasn't been added to sms_aliases yet).
    """
    number = sms.get("sms_from_number")
    if number and sms_aliases:
        hit = sms_aliases.get(number)
        if hit:
            return hit

    body = (sms.get("sms_body") or "").lower()
    for shop in known_shops or []:
        if shop and shop.lower() in body:
            return shop
    return None


# ---------------------------------------------------------------------------
# Code extraction (reuses codes.py regexes)
# ---------------------------------------------------------------------------

def _extract_codes_from_text(text: str) -> list[dict]:
    """Find ``{code, context}`` pairs in SMS body text.

    Mirrors ``gmail._extract_codes_from_text`` — sliding ±1-line context
    window plus uppercase canonicalisation. Duplicated here to keep voice.py
    self-contained (the regexes are shared via codes.py).

    SMS bodies are typically short and single-line, so the window rarely
    matters in practice — but keeping behaviour identical avoids future
    confusion when codes start arriving via SMS broadcasts that use the same
    multi-line styled-code pattern as Klaviyo emails.
    """
    lines = [ln.strip() for ln in (text or "").splitlines()]
    results: list[dict] = []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        if not line:
            continue
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

def extract_sms_signals(
    sms_list: list[dict],
    sms_aliases: dict[str, str],
    known_shops: list[str],
    *,
    now: datetime | None = None,
) -> dict:
    """Mine ``sms_list`` for promo codes and sale-announcement signals.

    Returns a dict::

        {
          "codes":         list of attributed code entries (source="sms"),
          "unattributed":  list of code entries from unknown numbers
                            (source="sms_unattributed",
                             shop=<sender phone number>),
          "sale_signals":  list of {email_id, shop, subject, body_excerpt,
                            email_date} — only for attributable SMS (no point
                            asking Claude about a sale at an unknown shop).
                            Shape matches gmail's so main.py merges them into
                            the same resolve_fuzzy queue without translation.
          "untracked_senders": list of {brand, number, excerpt, email_id} for
                            unattributed but clearly-marketing texts (a
                            "Brand: …" lead + a sale lexeme). Surfaced in the
                            digest so the user can add the brand to
                            SMS_SALE_SHOPS — the discovery loop that makes the
                            allowlist usable.
          "processed_ids": list of every email id seen this batch — INCLUDING
                            skipped voicemails / 2FA texts (main.py persists
                            these to voice_state.processed_ids so they aren't
                            re-fetched next run),
        }

    Voicemail / missed-call forwards and one-time-passcode (2FA) texts are
    recorded as processed but otherwise skipped — they're never a sale and
    their bodies would only add noise.

    Code entries carry ``first_seen``/``last_seen`` timestamps so state.py
    can persist them and prune ones not re-seen in 30 days, same as email.
    """
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    codes: list[dict] = []
    unattributed: list[dict] = []
    sale_signals: list[dict] = []
    untracked_senders: list[dict] = []
    processed_ids: list[str] = []

    for sms in sms_list or []:
        eid = sms.get("id")
        if not eid:
            continue
        processed_ids.append(eid)

        body = sms.get("sms_body") or ""
        subject = sms.get("subject") or ""
        # Skip non-SMS forwards (voicemail / missed call) and OTP/2FA texts —
        # marked processed above so they're not re-fetched, but they carry no
        # promo content and their digit codes would pollute the output.
        if not _is_text_message(subject) or _VERIFICATION_RE.search(body):
            continue

        shop = _attribute_sms(sms, sms_aliases, known_shops)
        # SMS body is the primary signal; subject ("New text message from X")
        # is GV's template wrapper and contains no promo content. Searching
        # body alone keeps the code-context heuristic from getting false
        # positives off the wrapper.
        sms_codes = _extract_codes_from_text(body)

        if shop is not None:
            for c in sms_codes:
                codes.append({
                    "shop": shop,
                    "code": c["code"],
                    "context": f"from SMS: {c.get('ctx_window', '')}".strip(),
                    "confidence": c.get("confidence", "medium"),
                    "source": "sms",
                    "email_id": eid,
                    "first_seen": now_iso,
                    "last_seen": now_iso,
                })
            sale_signals.append({
                "email_id": eid,
                "shop": shop,
                "subject": subject or f"SMS from {sms.get('sms_from_number') or 'unknown'}",
                "body_excerpt": _excerpt(body),
                # Anchor for relative sale-window phrases; "" if the Date header
                # was missing/unparseable, in which case Claude leaves the
                # window null (mirrors gmail's email_date).
                "email_date": _email_date_iso(sms.get("date")),
            })
        else:
            number = sms.get("sms_from_number") or "(unknown)"
            for c in sms_codes:
                unattributed.append({
                    "shop": number,
                    "code": c["code"],
                    "context": f"from SMS: {c.get('ctx_window', '')}".strip(),
                    "confidence": c.get("confidence", "medium"),
                    "source": "sms_unattributed",
                    "email_id": eid,
                    "first_seen": now_iso,
                    "last_seen": now_iso,
                })
            # Discovery: a "Brand: …" lead + a sale lexeme means a shop we
            # don't track just texted a deal — record it so the digest can
            # prompt the user to add it to SMS_SALE_SHOPS.
            lead = _BRAND_LEAD_RE.match(body)
            if lead and _SALE_HINT_RE.search(body):
                untracked_senders.append({
                    "brand": lead.group(1).strip(),
                    "number": number,
                    "excerpt": _excerpt(body, 140),
                    "email_id": eid,
                })

    return {
        "codes": codes,
        "unattributed": unattributed,
        "sale_signals": sale_signals,
        "untracked_senders": untracked_senders,
        "processed_ids": processed_ids,
    }


def _excerpt(text: str, limit: int = _BODY_EXCERPT_LIMIT) -> str:
    """Truncate body to a cost-friendly single-line excerpt for Claude.
    Mirror of gmail._excerpt."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + " ...[truncated]"
