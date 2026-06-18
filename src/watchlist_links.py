"""Signed deep-links + pending-item selection for the watchlist-removal web form.

When an order scan matches a purchased wardrobe item to a line on the watchlist
Google Doc, the daily digest emails a link per pending candidate. Clicking it
opens the same Apps Script web app that powers fit feedback (see ``apps_script/``)
in *removal* mode: it shows the item and how it's listed in the Doc, and on
approval **deletes that line from the Doc** and records the removal in
``wardrobe.json``. Because that link *edits* the Doc and the wardrobe, it must not
be guessable or forgeable, so every link carries an HMAC-SHA256 signature the web
app verifies before it renders or accepts anything.

Signing contract (the Apps Script verifier MUST match this byte-for-byte)
------------------------------------------------------------------------
* secret  : the shared ``FIT_LINK_SECRET`` string (UTF-8 bytes) — the *same*
            secret the fit-feedback links use.
* message : for a per-item removal link, ``"remove:" + item_id``
            (e.g. ``"remove:a1b2c3d4e5f6"``).
            for the "review every pending removal" link, the literal
            ``"remove-all"``.
* sig     : ``HMAC_SHA256(secret, message)`` rendered as **lowercase hex**.
* URL     : ``{base}?remove={item_id}&sig={sig}``  (per item)
            ``{base}?removeall=1&sig={sig}``        (review-all; message="remove-all")

The ``"remove:"`` prefix and the ``removeall``/``remove`` query params namespace
these links away from the fit-feedback links (which sign the bare ``item_id`` and
use ``item``/``all`` params) so a leaked fit link can never trigger a Doc deletion
and vice-versa. ``base`` is the Apps Script ``/exec`` URL (``FIT_FORM_BASE_URL``,
reused — it's the same web app). The actual HMAC lives in ``fit_links`` and is
imported here so both link families stay in lock-step with the Apps Script
verifier; this module is otherwise pure stdlib (no project imports beyond that,
no network) so the cron digest builder and the manual ``order_scan`` CLI can
import it without any coupling or import cycle.
"""

from __future__ import annotations

from urllib.parse import urlencode

from src.fit_links import sign  # re-used so both link families share one HMAC

# Message signed for the "review every pending removal on one page" link. A
# constant token (never a real ``"remove:"`` per-item message) so the review-all
# link can't collide with any per-item removal link.
REMOVAL_ALL_TOKEN = "remove-all"

# Prefix that turns a bare item id into the per-item removal message. Keeps
# removal links from colliding with fit links, which sign the bare id.
_REMOVAL_PREFIX = "remove:"


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------

def removal_message(item_id: str) -> str:
    """The signed message for a per-item removal link: ``"remove:" + item_id``."""
    return _REMOVAL_PREFIX + item_id


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------

def removal_url(item_id: str, base_url: str, secret: str) -> str:
    """Signed per-item removal link: ``{base}?remove=<id>&sig=<hmac>``."""
    sig = sign(removal_message(item_id), secret)
    return f"{base_url}?{urlencode({'remove': item_id, 'sig': sig})}"


def removal_all_url(base_url: str, secret: str) -> str:
    """Signed "review every pending removal" link: ``{base}?removeall=1&sig=<hmac>``."""
    sig = sign(REMOVAL_ALL_TOKEN, secret)
    return f"{base_url}?{urlencode({'removeall': '1', 'sig': sig})}"


# ---------------------------------------------------------------------------
# Pending-item selection (shared by the digest section AND the CLI fallback)
# ---------------------------------------------------------------------------

def is_removal_pending(item: dict) -> bool:
    """True when a purchased item is awaiting a remove-from-Doc decision.

    The canonical predicate, mirroring ``order_scan._interactive_watchlist_approval``'s
    selection: the item matched a watchlist Doc line (``watchlist_match`` set) and
    no decision has been made yet (``approved_for_removal is None``). Items the
    user already approved (``True``) or declined (``False``) are excluded so they
    aren't re-surfaced. Applies to non-clothing matches too — removal isn't tied
    to whether the item has a fit to review.
    """
    match = item.get("watchlist_match")
    return bool(match) and match.get("approved_for_removal") is None


def pending_removal_items(items: list[dict]) -> list[dict]:
    """All items awaiting a remove-from-Doc decision, in input order."""
    return [it for it in (items or []) if is_removal_pending(it)]
