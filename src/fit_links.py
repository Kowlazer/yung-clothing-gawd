"""Signed deep-links + pending-item selection for the fit-feedback web form.

The daily/weekly digest emails a link per wardrobe item that still needs a fit
review. Clicking it opens an Apps Script web app (see ``apps_script/``) that
writes the review straight back into ``wardrobe.json`` on the Gist. Because that
link *writes* to the wardrobe, it must not be guessable or forgeable, so every
link carries an HMAC-SHA256 signature the web app verifies before it renders or
accepts anything.

Signing contract (the Apps Script verifier MUST match this byte-for-byte)
------------------------------------------------------------------------
* secret  : the shared ``FIT_LINK_SECRET`` string (UTF-8 bytes).
* message : for a per-item link, the raw ``item_id`` (e.g. ``"a1b2c3d4e5f6"``).
            for the "review everything pending" link, the literal ``"__all__"``.
* sig     : ``HMAC_SHA256(secret, message)`` rendered as **lowercase hex**.
* URL     : ``{base}?item={item_id}&sig={sig}``  (per item)
            ``{base}?all=1&sig={sig}``           (review-all; message="__all__")

``base`` is the Apps Script ``/exec`` URL (``FIT_FORM_BASE_URL``). The module is
pure stdlib (no project imports, no network) so both the cron digest builder and
the manual ``order_scan`` CLI can import it without any coupling or import cycle.
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode

# Message signed for the "review all pending items on one page" link. A constant
# token (never a real item id — item ids are 12 hex chars) so the review-all
# link can't collide with any per-item link.
REVIEW_ALL_TOKEN = "__all__"


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def sign(message: str, secret: str) -> str:
    """Return ``HMAC_SHA256(secret, message)`` as lowercase hex.

    ``message`` and ``secret`` are encoded as UTF-8. Mirrors Apps Script's
    ``Utilities.computeHmacSha256Signature(message, secret)`` followed by a
    lowercase-hex render of the resulting byte array.
    """
    return hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify(message: str, sig: str, secret: str) -> bool:
    """Constant-time check that ``sig`` is the valid signature for ``message``.

    Returns ``False`` (never raises) for a missing/blank ``sig`` or ``secret``
    so callers can treat any malformed link as simply unauthorised.
    """
    if not sig or not secret:
        return False
    return hmac.compare_digest(sign(message, secret), sig)


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------

def fit_url(item_id: str, base_url: str, secret: str) -> str:
    """Signed per-item form link: ``{base}?item=<id>&sig=<hmac>``."""
    sig = sign(item_id, secret)
    return f"{base_url}?{urlencode({'item': item_id, 'sig': sig})}"


def review_all_url(base_url: str, secret: str) -> str:
    """Signed "review every pending item" link: ``{base}?all=1&sig=<hmac>``."""
    sig = sign(REVIEW_ALL_TOKEN, secret)
    return f"{base_url}?{urlencode({'all': '1', 'sig': sig})}"


# ---------------------------------------------------------------------------
# Pending-item selection (shared by the digest section AND the CLI fallback)
# ---------------------------------------------------------------------------

def is_fit_pending(item: dict) -> bool:
    """True when an item still needs a fit review.

    The canonical predicate, used identically by the emailed digest section,
    the web form's review-all page, and the ``order_scan`` CLI walk so the three
    never drift: no ``fit_review`` yet, and not flagged ``is_clothing=False``
    (gadgets / homeware matched against a Non-clothing watchlist line have no fit
    to review). The ``dropped`` sentinel sets a non-null ``fit_review``, so
    dropped items are excluded automatically.
    """
    return item.get("fit_review") is None and item.get("is_clothing") is not False


def pending_fit_items(items: list[dict]) -> list[dict]:
    """All items awaiting a fit review, in input order."""
    return [it for it in (items or []) if is_fit_pending(it)]
