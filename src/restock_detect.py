"""Heuristic restock-notification ("email me when back in stock") detection.

Used by ``src/restock_signup.py``. Unlike the newsletter popup (which fires on
its own over the whole homepage), a restock form is attached to a single
out-of-stock product/variant and usually has to be *revealed*: select the OOS
size, then a "Notify me when available" control surfaces the email field
(inline, in a drawer, or in a small modal).

The flow per product page is therefore:

1. (caller) select the out-of-stock size variant — :func:`select_size`.
2. :func:`reveal_restock_form` — click any "notify me" trigger to surface the
   form (no-op when the form is already inline).
3. :func:`detect_restock_form` — vendor selectors first, generic email-near-
   restock-text fallback second.
4. fill the email field (:func:`find_email_field`, reused from popup_detect),
   submit (:func:`find_restock_submit`), then :func:`detect_restock_success`.

Like ``popup_detect``, this module keeps its CSS-string constants and pure
Python helpers importable without loading Playwright, so the regex/parsing
logic is unit-testable with plain fakes. Playwright-typed helpers accept a
``page`` / ``Locator`` and are exercised with mock-style fakes.
"""
from __future__ import annotations

import logging
import re
from typing import Any

# Canonical size matching shared with the daily run (Medium↔M, X-Large↔XL).
from src.extract import _normalize_size
# Reuse the popup helpers verbatim — the email-field selector, the visibility
# guard, the consent-box check and the bot-block detector are identical needs.
from src.popup_detect import (  # noqa: F401 — re-exported for restock_signup
    check_consent_if_present,
    detect_bot_block,
    fill_phone_field,
    find_email_field,
    find_phone_field,
    looks_like_otp,
    visible_text,
    _try_visible,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------

# Known back-in-stock app containers. Order = most-specific first so a vendor
# wins attribution over the generic fallback. Values are CSS selector lists.
RESTOCK_VENDOR_SELECTORS: dict[str, str] = {
    # Klaviyo Back in Stock (modal + inline embed)
    "klaviyo_bis": "#klaviyo-bis-modal, .klaviyo-bis-modal, [class*='klaviyo-bis']",
    # Swym Back in Stock Alerts
    "swym_bis":    "#swym-bis, [class*='swym-bis'], #swym-email",
    # Back in Stock (backinstock.org)
    "backinstock": "#BIS_form, .bis-form, form[action*='back_in_stock']",
    # Appikon "Back in Stock - Restock Alerts"
    "appikon":     "[id*='AppikonBIS'], [class*='product-bis'], #BISModal, #BISForm",
    # Restock Rocket
    "restock_rocket": "[class*='restock-rocket'], [id*='restock-rocket']",
    # Generic Shopify "notify when available" form actions
    "notify_form": "form[action*='notify'], form[action*='restock']",
}

# Generic fallback container — any visible dialog/drawer/form region. Narrowed
# to a restock context by ``looks_like_restock_text`` on its inner text.
GENERIC_RESTOCK_SELECTOR = (
    "dialog[open], [role='dialog'], [aria-modal='true'], "
    "form[id*='notify' i], form[class*='notify' i], "
    "[class*='back-in-stock' i], [class*='backinstock' i], "
    "[class*='restock' i], [class*='notify' i]"
)

# A button/link that reveals the restock form when clicked (the email field is
# often hidden until the shopper asks to be notified). Playwright ``:has-text``
# is case-insensitive.
NOTIFY_TRIGGER_SELECTOR = (
    "button:has-text('notify me'), "
    "button:has-text('email me'), "
    "button:has-text('email when available'), "
    "button:has-text('notify me when available'), "
    "button:has-text('back in stock'), "
    "button:has-text('let me know'), "
    "a:has-text('notify me'), "
    "a:has-text('email when available'), "
    "[class*='notify' i] button, "
    "[class*='bis' i] button"
)

# Submit control inside a revealed restock form. type=submit first, then the
# restock-specific button wording (distinct from the newsletter SUBMIT set).
RESTOCK_SUBMIT_SELECTOR = (
    "button[type='submit'], "
    "input[type='submit'], "
    "[data-testid*='submit' i], "
    "button:has-text('notify me'), "
    "button:has-text('notify'), "
    "button:has-text('email me'), "
    "button:has-text('send'), "
    "button:has-text('submit'), "
    "button:has-text('subscribe')"
)

# Text that means we're looking at a restock context (gates the generic
# container fallback so a random newsletter form isn't mistaken for one).
_RESTOCK_RE = re.compile(
    r"notify me|email me when|email when available|back in stock|"
    r"when (?:it'?s |this is )?available|when available|let me know|"
    r"restock|notify when|in[- ]stock alert|sold out",
    re.I,
)

# Post-submit success wording specific to restock signups.
RESTOCK_SUCCESS_RE = re.compile(
    r"we'?ll (?:notify|email|let you know|send|text)|"
    r"you'?re on the list|you will be notified|notify you when|"
    r"we will (?:notify|email|let you know)|"
    r"signed up|registered|request received|got it|"
    r"thanks|thank you|you'?re all set|we'?ll be in touch",
    re.I,
)


# ---------------------------------------------------------------------------
# Pure text/parse helpers (unit-tested)
# ---------------------------------------------------------------------------

def looks_like_restock_text(text: str | None) -> bool:
    """True iff ``text`` reads like a restock-notification context."""
    return bool(text and _RESTOCK_RE.search(text))


def looks_like_restock_success(text: str | None) -> bool:
    """True iff ``text`` reads like a successful restock signup confirmation."""
    return bool(text and RESTOCK_SUCCESS_RE.search(text))


def size_matches(option_text: str | None, size: str) -> bool:
    """True iff a variant option's visible text denotes ``size``.

    Canonicalises both sides with ``extract._normalize_size`` (the same map the
    daily run uses, so ``"Medium"``↔``"M"``, ``"X-Large"``↔``"XL"``), then
    matches on exact equality or whole-token membership (``"Medium / Black"``
    matches ``"Medium"``). Token-boundary matching guards substring traps —
    ``"S"`` never matches ``"XS"``."""
    opt_c = _normalize_size(option_text)
    size_c = _normalize_size(size)
    if not opt_c or not size_c:
        return False
    if opt_c == size_c:
        return True
    # Token match against the raw option split on common separators, so
    # "Medium / Black" -> {"M","BLACK"} matches "Medium" but "S" never hits "XS".
    tokens = {
        _normalize_size(t)
        for t in re.split(r"[\s/|,_\-]+", option_text or "") if t.strip()
    }
    return size_c in tokens


# ---------------------------------------------------------------------------
# Playwright-typed helpers (mock-tested)
# ---------------------------------------------------------------------------

def reveal_restock_form(page: Any, *, timeout_ms: int = 1_000) -> bool:
    """Click a visible "notify me" trigger to surface the restock form.

    Best-effort and idempotent-ish: returns True iff a trigger was clicked.
    No-op (returns False) when the form is already inline or no trigger is
    visible. Never raises — a detached/stale handle just yields False."""
    try:
        trigger = page.locator(NOTIFY_TRIGGER_SELECTOR).first
    except Exception:  # noqa: BLE001
        return False
    if not _try_visible(trigger, timeout_ms):
        return False
    try:
        trigger.click(timeout=timeout_ms)
        page.wait_for_timeout(800)  # let the form/drawer render
        return True
    except Exception:  # noqa: BLE001 — revealing is optional
        return False


def detect_restock_form(
    page: Any, *, per_selector_timeout_ms: int = 500,
) -> tuple[Any, str | None]:
    """Find a restock-notification form on ``page``.

    Returns ``(locator, vendor)`` or ``(None, None)``. ``vendor`` is a key of
    ``RESTOCK_VENDOR_SELECTORS`` or ``"generic"`` (the latter only when the
    container both is visible AND contains an email field AND its text reads
    like a restock context, so a stray newsletter form isn't misclaimed)."""
    for vendor, selector in RESTOCK_VENDOR_SELECTORS.items():
        loc = page.locator(selector).first
        if _try_visible(loc, per_selector_timeout_ms):
            log.info("restock form: vendor=%s selector=%s", vendor, selector)
            return loc, vendor

    loc = page.locator(GENERIC_RESTOCK_SELECTOR).first
    if _try_visible(loc, per_selector_timeout_ms):
        if find_email_field(loc) is None:
            return None, None
        try:
            text = loc.inner_text(timeout=500) or ""
        except Exception:  # noqa: BLE001
            text = ""
        if looks_like_restock_text(text):
            log.info("restock form: vendor=generic selector=%s", GENERIC_RESTOCK_SELECTOR)
            return loc, "generic"
    return None, None


def find_restock_submit(form: Any, *, timeout_ms: int = 500) -> Any | None:
    btn = form.locator(RESTOCK_SUBMIT_SELECTOR).first
    return btn if _try_visible(btn, timeout_ms) else None


# Variant-size selection -----------------------------------------------------

# Common ways a Shopify/Woo product exposes its size options.
_SIZE_SELECT_SELECTOR = (
    "select[name*='size' i], select[id*='size' i], "
    "select[data-option*='size' i], select[name*='Size']"
)
_SIZE_SWATCH_SELECTOR = (
    "[data-option-name*='size' i] [data-value], "
    "[class*='swatch' i] [data-value], "
    "fieldset:has(legend:has-text('Size')) label, "
    "[class*='size' i] label, "
    "[class*='size' i] button, "
    "input[type='radio'][name*='size' i], "  # Shopify variant radios (name="Size")
    "[data-value], "
    "label[for*='size' i]"
)


def _swatch_label(sw: Any) -> str:
    """Best-effort visible label for a size swatch/radio/button."""
    try:
        return (
            sw.inner_text(timeout=200)
            or sw.get_attribute("data-value")
            or sw.get_attribute("value")  # radio inputs carry the size in value
            or ""
        )
    except Exception:  # noqa: BLE001
        return ""


def _select_size_in(scope: Any, size: str, *, timeout_ms: int) -> bool:
    """Pick ``size`` within ``scope`` (a page or a form Locator).

    Tries a size-ish ``<select>`` first, then **any** ``<select>`` whose options
    include the target size (restock popups — e.g. Swym's
    ``#swym-remind-me-oos-options`` — name their variant select with no "size"
    in the id), then clickable swatches / radios / labels. Never raises."""
    if not size:
        return False
    for sel in (_SIZE_SELECT_SELECTOR, "select"):
        try:
            selects = scope.locator(sel)
            for si in range(min(selects.count(), 6)):
                select = selects.nth(si)
                if not _try_visible(select, timeout_ms):
                    continue
                options = select.locator("option")
                for i in range(options.count()):
                    label = _swatch_label(options.nth(i))
                    if size_matches(label, size):
                        try:
                            select.select_option(label=label, timeout=timeout_ms)
                            return True
                        except Exception:  # noqa: BLE001
                            break  # found the size but couldn't pick it; try next selector
        except Exception:  # noqa: BLE001
            pass
    try:
        swatches = scope.locator(_SIZE_SWATCH_SELECTOR)
        for i in range(min(swatches.count(), 40)):
            sw = swatches.nth(i)
            if not _try_visible(sw, 200):
                continue
            if size_matches(_swatch_label(sw), size):
                try:
                    sw.click(timeout=timeout_ms)
                    return True
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        pass
    return False


def select_size(page: Any, size: str, *, timeout_ms: int = 800) -> bool:
    """Best-effort: select the OOS variant ``size`` so its restock form appears.

    Tries a native ``<select>`` (incl. any select whose options denote the
    size), then clickable swatches / radios (Shopify ``input[type=radio]
    [name=Size]``) / labels. Returns True iff a selection succeeded, after a
    short settle wait so the variant change registers. Never raises."""
    if _select_size_in(page, size, timeout_ms=timeout_ms):
        try:
            page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            pass
        return True
    return False


def select_size_in_form(form: Any, size: str, *, timeout_ms: int = 800) -> bool:
    """Best-effort: choose ``size`` in the restock form's *own* size control.

    Some back-in-stock widgets carry their own variant selector inside the popup
    independent of the product page's — e.g. Steady Hands' Swym "Notify me when
    available!" modal uses ``<select id="swym-remind-me-oos-options">`` (no
    "size" in the id), which the size-specific selector misses but the
    any-select-with-a-matching-option fallback catches. Scoped to the ``form``
    locator (no page waits); never raises. Returns True iff a selection
    succeeded."""
    return _select_size_in(form, size, timeout_ms=timeout_ms)


def detect_restock_success(
    page: Any,
    form: Any,
    *,
    original_url: str,
    post_submit_wait_ms: int = 4_000,
) -> bool:
    """Decide whether the restock signup succeeded.

    Heuristic, any one of: a restock-success message appears (on the form if
    still up, else the page body), the form disappeared, or the URL changed.
    Mirrors ``popup_detect.detect_success`` minus the promo-code extraction
    (restock signups confirm by email, not an inline code)."""
    page.wait_for_timeout(post_submit_wait_ms)

    text = ""
    if _try_visible(form, 200):
        try:
            text = form.inner_text(timeout=500) or ""
        except Exception:  # noqa: BLE001
            text = ""
    if not text:
        try:
            text = page.locator("body").inner_text(timeout=500) or ""
        except Exception:  # noqa: BLE001
            text = ""
    if looks_like_restock_success(text):
        return True

    if not _try_visible(form, 200):
        return True

    try:
        current = page.url or ""
    except Exception:  # noqa: BLE001
        current = ""
    return bool(current and current != original_url)
