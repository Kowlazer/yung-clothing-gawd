"""Heuristic newsletter-popup detection for ``newsletter_signup``.

Two-pass approach when scanning a shop homepage:

1. Try each known popup-vendor CSS selector. First visible hit wins.
2. Fall back to generic dialog patterns (``<dialog>``, ``[role="dialog"]``,
   ``[aria-modal="true"]``) for custom-built popups.

When neither finds a popup the caller falls through to the Claude vision/DOM
fallback (Phase 4 — not yet wired in Phase 2).

The module is deliberately Playwright-aware but exposes its CSS-string
constants and Python helpers (``extract_code_from_text``) without importing
playwright at module-load, so unit tests can exercise the pure logic without
spinning up Chromium. Functions that take a ``page`` or ``popup`` argument
expect a Playwright sync API ``Page`` / ``Locator`` and are tested with
``unittest.mock``-style fakes.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from src.codes import _CODE_TOKEN_RE, _canonicalise_code, _is_valid_code

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------

# Order matters — most-specific vendor selectors first so a popup wrapped in
# both a vendor container and a generic ``[role="dialog"]`` gets attributed
# to the vendor (useful for tuning logs even if the result is identical).
VENDOR_SELECTORS: dict[str, str] = {
    "klaviyo":    ".needsclick.klaviyo-form, [class*='klaviyo-form-']",
    "privy":      ".privy-popup, #privy-popup-container, .privy-modal",
    "justuno":    ".junoStandard, .juno-popup-container",
    "attentive":  "[id^='attentive_overlay'], .attentive-modal",
    "postscript": ".ps__widget, [data-ps-widget]",
    "mailchimp":  ".mc-modal, .mc-modal-content",
    "shopify":    ".shopify-section [class*='popup'], .shopify-section [class*='modal']",
}

# Generic fallback — visible dialog-shaped element when no vendor matched.
GENERIC_DIALOG_SELECTOR = "dialog[open], [role='dialog'], [aria-modal='true']"

# Field-finding selectors — applied within a popup Locator.
EMAIL_INPUT_SELECTOR = (
    "input[type='email'], "
    "input[name*='email' i], "
    "input[id*='email' i], "
    "input[placeholder*='email' i]"
)

# Phone (not used in Phase 2, kept here for Phase 3).
PHONE_INPUT_SELECTOR = (
    "input[type='tel'], "
    "input[name*='phone' i], "
    "input[id*='phone' i], "
    "input[placeholder*='phone' i]"
)

# Submit button — type=submit first, then text-based fallbacks. Playwright
# ``:has-text()`` is case-insensitive.
SUBMIT_BUTTON_SELECTOR = (
    "button[type='submit'], "
    "input[type='submit'], "
    "[data-testid*='submit' i], "
    "button:has-text('subscribe'), "
    "button:has-text('sign up'), "
    "button:has-text('join'), "
    "button:has-text('get code'), "
    "button:has-text('get my'), "
    "button:has-text('unlock')"
)

# Consent checkboxes — label or surrounding text contains a keyword.
_CONSENT_KEYWORD_RE = re.compile(r"marketing|consent|agree|terms|privacy|opt[- ]?in", re.I)

# Success-message regex — matches anywhere in the post-submit popup or page.
SUCCESS_MESSAGE_RE = re.compile(
    r"thank you|thanks!|subscribed|welcome|check your email|"
    r"check your inbox|sent|success|congrat|here'?s your code|here is your code",
    re.I,
)

# Captcha / bot-block indicators — used to short-circuit submission attempts.
CAPTCHA_INDICATORS_RE = re.compile(
    r"captcha|cloudflare|verify you are human|access (?:is )?temporarily restricted|"
    r"performing security verification|are you a robot",
    re.I,
)

# OTP / verification-code prompt — appears after a phone (SMS) signup when the
# shop wants you to confirm the number before subscribing. We can't complete
# these headlessly (the code lands on the user's phone), so we detect and skip.
# Deliberately specific: requires a verification-flavored phrase, not a bare
# "code", so a "here's your discount code WELCOME15" success message doesn't
# read as an OTP challenge.
OTP_PROMPT_RE = re.compile(
    r"verification code|confirmation code|enter the code|"
    r"code we (?:just )?sent|we (?:just )?(?:sent|texted) you a code|"
    r"sent a (?:\d-digit )?code|confirm your (?:phone|number|mobile)|"
    r"verify your (?:phone|number|mobile)|reply (?:with )?(?:the )?code|"
    r"check your (?:phone|texts|messages) for (?:a |the )?code",
    re.I,
)

# Iframe-source domains that mean the parent page is a bot-detection challenge.
# Some vendors (DataDome on Etsy, Cloudflare Turnstile on TeePublic) render the
# entire block UI inside a third-party frame, so the parent's ``body.inner_text``
# is empty and the regex above misses it. Frame URL is a reliable signal.
BOT_BLOCK_FRAME_DOMAINS: tuple[str, ...] = (
    "captcha-delivery.com",        # DataDome (Etsy and others)
    "challenges.cloudflare.com",   # Cloudflare Turnstile
    "hcaptcha.com",
    "recaptcha.net",
    "google.com/recaptcha",
    "perimeterx.net",              # PerimeterX / HUMAN
    "akamai",
    "px-cdn.net",                  # PerimeterX CDN
)


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def _try_visible(locator: Any, timeout_ms: int) -> bool:
    """Return True if ``locator`` is visible within ``timeout_ms``.

    Wrapped because Playwright's ``is_visible`` raises rather than returning
    False on certain detached-element states, and we don't want those to
    propagate out of detection helpers.
    """
    try:
        return bool(locator.is_visible(timeout=timeout_ms))
    except Exception:  # noqa: BLE001 — defensive against Playwright detached / stale handles
        return False


def detect_popup(
    page: Any,
    *,
    initial_wait_ms: int = 3_000,
    per_selector_timeout_ms: int = 500,
    trigger_exit_intent: bool = True,
) -> tuple[Any, str | None]:
    """Look for a newsletter popup on ``page``.

    Returns ``(locator, vendor_name)`` for the matched popup, or ``(None, None)``
    if nothing fired. ``vendor_name`` is one of the keys of ``VENDOR_SELECTORS``
    or ``"generic"`` when the dialog fallback matched.

    Strategy:
      1. ``page.wait_for_timeout(initial_wait_ms)`` — let scroll-/time-trigger
         popups fire on their own.
      2. Optionally move the mouse to ``(0, 0)`` to fake exit-intent.
      3. Try each vendor selector with a short timeout. First visible wins.
      4. Fall back to the generic dialog selector.
    """
    page.wait_for_timeout(initial_wait_ms)

    if trigger_exit_intent:
        try:
            page.mouse.move(0, 0)
            page.wait_for_timeout(700)
        except Exception:  # noqa: BLE001 — exit-intent is best-effort
            pass

    for vendor, selector in VENDOR_SELECTORS.items():
        loc = page.locator(selector).first
        if _try_visible(loc, per_selector_timeout_ms):
            log.info("popup detected: vendor=%s selector=%s", vendor, selector)
            return loc, vendor

    loc = page.locator(GENERIC_DIALOG_SELECTOR).first
    if _try_visible(loc, per_selector_timeout_ms):
        log.info("popup detected: vendor=generic selector=%s", GENERIC_DIALOG_SELECTOR)
        return loc, "generic"

    return None, None


def find_email_field(popup: Any, *, timeout_ms: int = 500) -> Any | None:
    field = popup.locator(EMAIL_INPUT_SELECTOR).first
    return field if _try_visible(field, timeout_ms) else None


def find_phone_field(popup: Any, *, timeout_ms: int = 500) -> Any | None:
    field = popup.locator(PHONE_INPUT_SELECTOR).first
    return field if _try_visible(field, timeout_ms) else None


def find_submit_button(popup: Any, *, timeout_ms: int = 500) -> Any | None:
    btn = popup.locator(SUBMIT_BUTTON_SELECTOR).first
    return btn if _try_visible(btn, timeout_ms) else None


# ---------------------------------------------------------------------------
# Phone-field fill helpers (shared by newsletter_signup + restock_signup)
# ---------------------------------------------------------------------------

def phone_formats(phone: str) -> list[str]:
    """Candidate string formats to try for a phone field, most-canonical first.

    A US / NANP number (E.164 ``+15555550100``, 11-digit ``15555550100``, or
    bare 10-digit) expands to E.164, national ``(555) 555-0100`` and bare
    ``5555550100`` — forms validate against different shapes, so we try each
    until the input accepts one. A non-US / unparseable value is returned
    verbatim as a single candidate. Empty input → empty list.
    """
    raw = (phone or "").strip()
    if not raw:
        return []
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+1") and len(digits) == 11:
        ten = digits[1:]
    elif len(digits) == 11 and digits.startswith("1"):
        ten = digits[1:]
    elif len(digits) == 10:
        ten = digits
    else:
        return [raw]  # non-US / unknown — only try the value as given
    e164 = f"+1{ten}"
    national = f"({ten[0:3]}) {ten[3:6]}-{ten[6:10]}"
    out: list[str] = []
    for fmt in (e164, national, ten):
        if fmt not in out:
            out.append(fmt)
    return out


def fill_phone_field(field: Any, phone: str) -> str | None:
    """Fill ``field`` with the first phone format the input accepts.

    Tries each candidate from :func:`phone_formats` in order; after filling,
    consults the field's HTML5 ``checkValidity()`` and advances to the next
    shape if the browser reports the value invalid. Returns the value left in
    the field (the last one tried if none validated), or None if no candidate
    could be filled at all.
    """
    last: str | None = None
    for value in phone_formats(phone):
        try:
            field.fill(value)
        except Exception:  # noqa: BLE001 — a rejected format shouldn't abort
            continue
        last = value
        try:
            valid = bool(field.evaluate(
                "el => (el.checkValidity ? el.checkValidity() : true)"
            ))
        except Exception:  # noqa: BLE001 — no validity API → accept the fill
            valid = True
        if valid:
            return value
    return last


def visible_text(page: Any, popup: Any) -> str:
    """Best-effort visible text — the still-open popup if present, else body."""
    if popup is not None and _try_visible(popup, 200):
        try:
            txt = popup.inner_text(timeout=500) or ""
            if txt:
                return txt
        except Exception:  # noqa: BLE001
            pass
    try:
        return page.locator("body").inner_text(timeout=500) or ""
    except Exception:  # noqa: BLE001
        return ""


def check_consent_if_present(popup: Any, *, timeout_ms: int = 300) -> bool:
    """Check the first visible consent-style checkbox inside ``popup``.

    Best-effort: silently no-ops if no consent box, the box is already
    checked, or the click fails. Returns True iff a box was clicked.
    """
    boxes = popup.locator("input[type='checkbox']")
    try:
        count = boxes.count()
    except Exception:  # noqa: BLE001
        return False
    for i in range(count):
        box = boxes.nth(i)
        if not _try_visible(box, timeout_ms):
            continue
        # Read nearby text — Playwright doesn't have a direct "label of" so
        # we inspect the parent's text content as a cheap heuristic.
        try:
            label_text = box.evaluate(
                "el => (el.closest('label') || el.parentElement).innerText || ''"
            ) or ""
        except Exception:  # noqa: BLE001
            label_text = ""
        if not _CONSENT_KEYWORD_RE.search(label_text):
            continue
        try:
            if not box.is_checked():
                box.check(timeout=timeout_ms)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


# ---------------------------------------------------------------------------
# Result detection
# ---------------------------------------------------------------------------

def looks_like_captcha(text: str | None) -> bool:
    """True iff page text looks like a CAPTCHA / bot-challenge interstitial."""
    return bool(text and CAPTCHA_INDICATORS_RE.search(text))


def looks_like_otp(text: str | None) -> bool:
    """True iff ``text`` is asking for an SMS verification code (OTP).

    Used after a phone-channel submit: an OTP prompt means the shop won't
    finish the subscription until the user types a code that landed on their
    phone — out of scope for headless signup, so we record ``requires_otp``
    and move on.
    """
    return bool(text and OTP_PROMPT_RE.search(text))


def _frame_urls(page: Any) -> list[str]:
    """Return all frame URLs on ``page``, robust to Playwright errors."""
    try:
        return [(f.url or "") for f in (page.frames or [])]
    except Exception:  # noqa: BLE001
        return []


def detect_bot_block(page: Any) -> bool:
    """True iff ``page`` is a CAPTCHA / bot-detection interstitial.

    Three signals, any one fires:
      1. ``body.inner_text`` text matches ``CAPTCHA_INDICATORS_RE``.
      2. A subframe URL matches a known bot-detection vendor domain
         (DataDome on Etsy, Cloudflare Turnstile on TeePublic, etc.).
      3. ``document.body.innerText`` via direct JS evaluate matches the
         regex — catches blocks that hide their text from the standard
         Playwright text APIs (some pseudo-element / shadow-DOM cases).
    """
    body_text = ""
    try:
        body_text = page.locator("body").inner_text(timeout=2_000) or ""
    except Exception:  # noqa: BLE001
        pass
    if looks_like_captcha(body_text):
        return True

    for url in _frame_urls(page):
        url_l = url.lower()
        if any(domain in url_l for domain in BOT_BLOCK_FRAME_DOMAINS):
            return True

    try:
        eval_text = page.evaluate(
            "() => document.body ? document.body.innerText || '' : ''"
        ) or ""
    except Exception:  # noqa: BLE001
        eval_text = ""
    return looks_like_captcha(eval_text)


def detect_success(
    page: Any,
    popup: Any,
    *,
    original_url: str,
    post_submit_wait_ms: int = 4_000,
) -> tuple[bool, str | None]:
    """Decide whether submission succeeded; extract a code if one is visible.

    Returns ``(success, code_or_None)``. Heuristic — any one of:
      * URL changed to a different path (success page).
      * Popup container disappeared (no error toast visible).
      * Visible text on the page or popup matches ``SUCCESS_MESSAGE_RE``.

    Code extraction reuses the watchlist code-token regex from ``codes.py``,
    so anything that looks like a promo code (``SPRING30``, ``WELCOME15``,
    hyphenated multi-segment, etc.) is picked up.
    """
    page.wait_for_timeout(post_submit_wait_ms)

    # URL changed → likely a success / redirect.
    current_url = ""
    try:
        current_url = page.url or ""
    except Exception:  # noqa: BLE001
        pass
    url_changed = bool(current_url and current_url != original_url
                       and not current_url.startswith(original_url.rstrip("/") + "/?"))

    # Pull any text we can see — popup if still up, otherwise full page body.
    success_text = ""
    if _try_visible(popup, 200):
        try:
            success_text = popup.inner_text(timeout=500) or ""
        except Exception:  # noqa: BLE001
            success_text = ""
    if not success_text:
        try:
            success_text = page.locator("body").inner_text(timeout=500) or ""
        except Exception:  # noqa: BLE001
            success_text = ""

    message_match = bool(success_text and SUCCESS_MESSAGE_RE.search(success_text))

    # Popup disappeared (no longer visible).
    popup_closed = not _try_visible(popup, 200)

    success = message_match or url_changed or popup_closed
    code = extract_code_from_text(success_text) if success else None
    return success, code


def extract_code_from_text(text: str | None) -> str | None:
    """Return the first plausible promo code in ``text``, or None.

    Reuses ``_CODE_TOKEN_RE`` + ``_is_valid_code`` from ``codes.py`` so the
    same definition of "looks like a promo code" applies to watchlist text,
    marketing emails, SMS, and post-signup success messages.
    """
    if not text:
        return None
    for token in _CODE_TOKEN_RE.finditer(text):
        raw = token.group(1)
        if _is_valid_code(raw):
            return _canonicalise_code(raw)
    return None
