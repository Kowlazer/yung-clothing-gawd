"""Auto-signup for shop newsletters — Phase 3 (heuristic, email + phone).

Manual entry point — not part of the daily cron. For each unique shop
homepage on the watchlist:

  1. Open headless Chromium (Playwright) and load the page.
  2. Wait for popups to fire; fake exit-intent by moving mouse to (0,0).
  3. Try the ``popup_detect`` vendor-selector library (Klaviyo, Privy, etc.)
     and a generic ``[role="dialog"]`` fallback.
  4. If a popup with an email and/or phone field + submit button is found,
     fill the configured signup email (``GMAIL_USERNAME``) and/or the Google
     Voice number (``SIGNUP_PHONE``) and click submit.
  5. Detect success (URL change / popup disappeared / success message),
     extract any visible promo code, and record the result per channel.

**Phase 3 adds the phone channel.** A single visit can sign up on both
channels: a popup carrying email + phone fields is filled and submitted in
one shot; an email-first / phone-second (SMS opt-in) popup is handled by a
second fill+submit after the email step succeeds. The phone number is tried
in E.164 first, falling back to national / bare-digit formats if the input's
HTML5 validity rejects it. A post-submit OTP / "confirm your number" prompt
is out of scope (the code lands on the user's phone) — it's recorded as
``requires_otp`` and skipped. A popup that offers no SMS field at all records
``no_phone_field``, which marks the phone channel *unavailable* so ``both`` /
``phone`` runs stop re-visiting that shop.

State persists in ``signup_state.json`` (8th Gist state file). On real
success the channel record is written with ``signed_up_at`` (plus
``code_received`` for email); subsequent runs skip the shop once every
requested channel is done (or, for phone, marked unavailable) unless
``--retry-failed`` is passed.

Usage::

    python -m src.newsletter_signup [--shop URL] [--channel email|phone|both]
                                    [--dry-run] [--max-shops N]
                                    [--retry-failed]
                                    [--screenshot-dir DIR]

``--channel both`` (the default) fills whichever of email/phone the popup
exposes. ``--dry-run`` runs popup detection but **does not** fill or submit
forms, and skips the Gist write at the end. Useful for tuning vendor
selectors and spotting CAPTCHA-blocked shops without polluting live state.
(Note: dry-run can't reveal an email-first popup's *second-step* phone field
since it never submits, so phone-second forms show ``no_phone_field`` in a
dry run.)

State schema (``signup_state.json``)::

    {
      "https://shop.com": {
        "email": {"signed_up_at": ISO, "code_received": str|null} | null,
        "phone": {"signed_up_at": ISO}
                 | {"unavailable": true, "checked_at": ISO} | null,
        "attempts": [
          {"at": ISO, "channel": str, "result": str,
           "vendor": str|null, "code_received": str|null,
           "dry_run": bool|null}, ...
        ]
      }
    }

``result`` enum: ``"success" | "no_popup_detected" | "captcha_blocked" |
"form_fill_failed" | "network_error" | "already_subscribed" |
"requires_otp" | "no_phone_field"``.
"""
from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from src import popup_claude
from src.classify import classify
from src.config import Config, load_config
from src.gmail import subscribed_shop_domains
from src.popup_claude import DEFAULT_MODEL
from src.popup_detect import (
    check_consent_if_present,
    detect_bot_block,
    detect_popup,
    detect_success,
    find_email_field,
    find_phone_field,
    find_submit_button,
    looks_like_otp,
    phone_formats,
    fill_phone_field as _fill_phone,
    visible_text as _visible_text,
)
from src.state import read_state, write_state
from src.watchlist import fetch_watchlist

log = logging.getLogger(__name__)

DEFAULT_SCREENSHOT_DIR = "signup_screenshots"
# Throttle between shops — Cloudflare-style fingerprinting watches request
# cadence across the batch, not just within a single visit.
_INTER_SHOP_JITTER = (5.0, 15.0)
_PAGE_TIMEOUT_MS = 20_000
# Delay before scanning for vendor-popup. Klaviyo popups are commonly
# configured with 5-10s show delays: a live probe (2026-07-05, issue #14)
# found several real shops whose popup a 3s wait misses but a ~10s+ wait
# catches. Scroll-triggered popups are handled by detect_popup's
# scroll-nudge stage, not this wait.
_POPUP_WAIT_MS = 12_000
# Settle wait before re-scanning a detected popup whose fields/submit were
# not visible on the first pass (entrance animation still running).
_FORM_SETTLE_MS = 2_000
_POST_SUBMIT_WAIT_MS = 4_000    # delay after clicking submit before success-detect
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# URL + shop list helpers
# ---------------------------------------------------------------------------

def _homepage_url(url: str) -> str:
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        return url
    return f"{p.scheme}://{p.netloc}"


def _shops_from_watchlist(text: str, aliases: dict[str, str]) -> list[str]:
    """Unique shop homepages from the watchlist, preserving first-seen order."""
    entries = classify(text)
    homepages: dict[str, None] = {}
    for e in entries:
        if e.category in ("PRODUCT_URL", "SHOP_URL"):
            home = _homepage_url(e.value)
            if home:
                homepages.setdefault(home, None)
        elif e.category == "SHOP_NAME":
            cached = aliases.get(e.value)
            if cached:
                homepages.setdefault(_homepage_url(cached), None)
    return list(homepages.keys())


def _shop_domain(url: str) -> str | None:
    """Bare host for a shop homepage, ``www.`` stripped.

    ``https://www.aniqi.com`` -> ``aniqi.com``. Gmail's ``from:aniqi.com``
    matches subdomains (``news.aniqi.com``) on its own, so the bare host is a
    good enough key for the subscription search.
    """
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc or None


def _collect_shops(args: argparse.Namespace, cfg: Config, aliases: dict) -> list[str]:
    """The shop homepages this invocation targets: the single ``--shop`` URL,
    else every unique shop on the watchlist."""
    if args.shop:
        return [_homepage_url(args.shop)]
    log.info("fetching watchlist")
    text = fetch_watchlist(cfg.watchlist_url)
    return _shops_from_watchlist(text, aliases)


# ---------------------------------------------------------------------------
# Gmail-inference auto-skip — don't re-sign-up where you already get the mail
# ---------------------------------------------------------------------------

_INFERRED_SUBSCRIBED_SOURCE = "gmail_inferred"


def _infer_subscribed(shops: list[str], cfg: Config) -> set[str]:
    """Domains among ``shops`` you already receive marketing mail from.

    Failure-isolated: an IMAP/Gmail error logs a warning and returns an empty
    set so the signup run proceeds (worst case: we attempt a shop you're
    already subscribed to — harmless, just redundant)."""
    domains = [d for d in (_shop_domain(s) for s in shops) if d]
    if not domains:
        return set()
    try:
        return subscribed_shop_domains(
            cfg.gmail_username, cfg.gmail_app_password, domains,
        )
    except Exception as exc:  # noqa: BLE001 — never let inference abort the run
        log.warning(
            "subscription inference failed (%s); proceeding without skip", exc,
        )
        return set()


def _seed_inferred_subscriptions(
    shops: list[str],
    signup_state: dict,
    subscribed_domains: set[str],
    *,
    now_iso: str,
) -> list[str]:
    """Mark shops whose domain is in ``subscribed_domains`` as already
    subscribed on email, in-place. Returns the shops seeded.

    Writes an ``email`` channel record (so ``_should_skip`` skips it) tagged
    ``inferred`` + an ``already_subscribed`` attempt — clearly flagged as
    inference, not a signup we actually performed. A real prior signup record
    is never overwritten.
    """
    seeded: list[str] = []
    for shop in shops:
        domain = _shop_domain(shop)
        if not domain or domain not in subscribed_domains:
            continue
        entry = signup_state.setdefault(
            shop, {"email": None, "phone": None, "attempts": []},
        )
        if (entry.get("email") or {}).get("signed_up_at"):
            continue  # already have a real (or prior inferred) record
        entry["email"] = {
            "signed_up_at": now_iso,
            "inferred": True,
            "source": _INFERRED_SUBSCRIBED_SOURCE,
        }
        entry.setdefault("attempts", []).append({
            "at": now_iso,
            "channel": "email",
            "result": "already_subscribed",
            "source": _INFERRED_SUBSCRIBED_SOURCE,
        })
        seeded.append(shop)
    return seeded


# ---------------------------------------------------------------------------
# Skip logic
# ---------------------------------------------------------------------------

_RETRY_RESULTS = frozenset({"no_popup_detected", "network_error"})


def _channel_done(rec: dict | None) -> bool:
    """A channel is "done" (no need to re-attempt) when it has a real signup
    record, or — for phone — has been marked *unavailable* (the shop's popup
    offers no SMS field, so re-visiting it would never succeed)."""
    if not rec:
        return False
    return bool(rec.get("signed_up_at") or rec.get("unavailable"))


def _should_skip(
    shop: str,
    state: dict,
    channels: list[str],
    *,
    retry_failed: bool,
) -> bool:
    """Skip a shop iff every requested channel is already done.

    "Done" means a real signup (``signed_up_at``) or — for phone — an
    ``unavailable`` marker (popup had no SMS field). ``retry_failed`` forces a
    re-run of an otherwise-skippable shop whose last attempt was a transient
    failure (``no_popup_detected`` / ``network_error``).
    """
    entry = state.get(shop) or {}
    for ch in channels:
        if not _channel_done(entry.get(ch)):
            return False
    if retry_failed:
        attempts = entry.get("attempts") or []
        if attempts and attempts[-1].get("result") in _RETRY_RESULTS:
            return False
    return True


def _record_attempt(state: dict, shop: str, attempt: dict) -> None:
    """Append an attempt and reflect its outcome into the channel record.

    Two outcomes touch the channel record (both gate ``_should_skip`` on
    subsequent runs):

      * ``success`` mirrors ``{signed_up_at[, code_received]}`` (code only on
        the email channel). ``code_received`` for phone is never meaningful.
      * ``no_phone_field`` marks the phone channel ``{unavailable, checked_at}``
        — the popup carried no SMS field, so a ``both`` / ``phone`` run should
        stop re-visiting this shop. Never overwrites a real prior signup.

    Dry-run attempts (``attempt["dry_run"] is True``) are NOT reflected —
    they're informational only and the script wouldn't have written to the
    Gist anyway, but this keeps the in-memory state honest.
    """
    entry = state.setdefault(shop, {"email": None, "phone": None, "attempts": []})
    entry.setdefault("attempts", []).append(attempt)

    if attempt.get("dry_run"):
        return
    channel = attempt.get("channel")
    if channel not in ("email", "phone"):
        return
    result = attempt.get("result")
    if result == "success":
        record: dict = {"signed_up_at": attempt["at"]}
        if channel == "email" and attempt.get("code_received"):
            record["code_received"] = attempt["code_received"]
        entry[channel] = record
    elif result == "no_phone_field" and channel == "phone":
        # Terminal for phone: this popup has no SMS signup. Don't clobber a
        # real signup if one somehow exists.
        if not (entry.get("phone") or {}).get("signed_up_at"):
            entry["phone"] = {"unavailable": True, "checked_at": attempt["at"]}


# ---------------------------------------------------------------------------
# Channel + phone helpers (Phase 3)
# ---------------------------------------------------------------------------

# The channels each --channel choice fills. ``both`` opportunistically fills
# whichever fields the popup exposes.
_CHANNELS_FOR_ARG: dict[str, list[str]] = {
    "email": ["email"],
    "phone": ["phone"],
    "both": ["email", "phone"],
}


def _subscribed_channels(entry: dict | None) -> set[str]:
    """Channels at a shop that already have a real signup (``signed_up_at``).

    Used to avoid re-submitting a channel we've already subscribed — distinct
    from ``_channel_done`` (which also counts phone ``unavailable``): we never
    re-fill a subscribed field, but an ``unavailable`` phone has no field to
    fill anyway.
    """
    out: set[str] = set()
    for ch in ("email", "phone"):
        if (entry or {}).get(ch, {}) and (entry or {})[ch].get("signed_up_at"):
            out.add(ch)
    return out


# ---------------------------------------------------------------------------
# Claude vision/DOM fallback (Phase 4)
# ---------------------------------------------------------------------------

# Heuristic outcomes that mean "found nothing usable" — the only results that
# warrant paying for a Claude fallback. A captcha block can't be helped by
# Claude, a network error means the page never loaded, and requires_otp /
# already_subscribed are real (partial) outcomes, not misses.
_CLAUDE_RETRY_RESULTS = frozenset(
    {"no_popup_detected", "form_fill_failed", "no_phone_field"}
)

_CLAUDE_MAX_CALLS_DEFAULT = 30


class _ClaudeFallback:
    """Per-run Claude fallback context + call budget.

    Bounds spend: each shop that falls back costs one batched vision/DOM call
    (~$0.02-0.05), so a run full of unrecognised popups is capped at
    ``max_calls`` calls total. Mirrors ``BROWSER_FALLBACK_MAX_ITEMS`` in
    ``browser_fetch``.
    """

    def __init__(
        self,
        client: object,
        model: str,
        *,
        want_screenshot: bool,
        max_calls: int,
    ) -> None:
        self.client = client
        self.model = model
        self.want_screenshot = want_screenshot
        self.max_calls = max_calls
        self.used = 0

    def take(self) -> bool:
        """Reserve one call; False once the budget is exhausted."""
        if self.used >= self.max_calls:
            return False
        self.used += 1
        return True


def _anthropic_client(cfg: Config) -> object:
    """Construct an Anthropic client from config (lazy import — the SDK is only
    needed when the fallback actually fires)."""
    import anthropic
    return anthropic.Anthropic(api_key=cfg.anthropic_api_key)


def _heuristic_missed(attempts: list[dict]) -> bool:
    """True iff the heuristic path found nothing usable (so a Claude fallback
    is warranted): no channel succeeded and every outcome is a recoverable
    miss (``_CLAUDE_RETRY_RESULTS``)."""
    if any(a.get("result") == "success" for a in attempts):
        return False
    return bool(attempts) and all(
        a.get("result") in _CLAUDE_RETRY_RESULTS for a in attempts
    )


def _signup_via_claude(
    page: object,
    *,
    email: str,
    phone: str,
    channels: list[str],
    dry_run: bool,
    now_iso: str,
    shop: str,
    claude: _ClaudeFallback,
    post_submit_wait_ms: int = _POST_SUBMIT_WAIT_MS,
    post_path: str | None = None,
) -> list[dict] | None:
    """Phase 4 fallback: ask Claude to locate the signup form, then fill it.

    Returns per-channel attempt records (``vendor="claude"``), or ``None`` when
    Claude finds no usable form — in which case the caller keeps the heuristic
    miss it already recorded. Resolves Claude's stamped-element indices to
    locators via ``popup_claude.index_locator`` and hands them to the shared
    ``_fill_and_submit`` core (``allow_phone_step2=False`` — Claude reports both
    fields from one snapshot).
    """
    want_email = "email" in channels
    want_phone = "phone" in channels

    form = popup_claude.locate_form(
        page, client=claude.client, model=claude.model,
        want_screenshot=claude.want_screenshot,
    )
    if form is None:
        log.info("claude fallback found no form at %s", shop)
        return None

    email_field = (
        popup_claude.index_locator(page, form.email_index) if want_email else None
    )
    phone_field = (
        popup_claude.index_locator(page, form.phone_index) if want_phone else None
    )
    submit_btn = popup_claude.index_locator(page, form.submit_index)
    container = popup_claude.stamp_container(page, form.submit_index)

    def rec(channel: str, result: str, *, code: str | None = None) -> dict:
        return {
            "at": now_iso, "channel": channel, "result": result,
            "vendor": "claude", "code_received": code,
            "dry_run": True if dry_run else None,
        }

    # Claude claimed a form but its indices didn't resolve to live elements.
    if submit_btn is None or (email_field is None and phone_field is None):
        log.info("claude form at %s not resolvable (submit/fields missing)", shop)
        out: list[dict] = []
        if want_email:
            out.append(rec("email", "form_fill_failed"))
        if want_phone:
            out.append(rec("phone",
                           "form_fill_failed" if phone_field is not None
                           else "no_phone_field"))
        return out

    if dry_run:
        present = "+".join(
            c for c in ("email", "phone")
            if (c == "email" and email_field is not None)
            or (c == "phone" and phone_field is not None)
        )
        log.info("DRY_RUN: %s — claude would fill %s + submit",
                 shop, present or "<nothing>")
        out = []
        if email_field is not None:
            out.append(rec("email", "success"))
        if phone_field is not None:
            out.append(rec("phone", "success"))
        elif want_phone:
            out.append(rec("phone", "no_phone_field"))
        return out

    attempts = _fill_and_submit(
        page, container if container is not None else submit_btn,
        vendor="claude", email=email, phone=phone,
        email_field=email_field, phone_field=phone_field, submit_btn=submit_btn,
        now_iso=now_iso, shop=shop,
        want_phone=want_phone, allow_phone_step2=False,
        post_submit_wait_ms=post_submit_wait_ms, post_path=post_path,
    )
    # Claude reports the whole form from one snapshot, so a phone channel it
    # didn't locate has no SMS field here — mark it unavailable (as the
    # heuristic path's phone-second re-detect does) so ``both`` runs stop
    # re-visiting an email-only shop. There's no multi-step re-detect to try.
    if (want_phone and phone_field is None
            and not any(a["channel"] == "phone" for a in attempts)):
        attempts.append(rec("phone", "no_phone_field"))
    return attempts


# ---------------------------------------------------------------------------
# Visit — headless Chromium → popup detect → fill + submit (Phase 3)
# ---------------------------------------------------------------------------

def _safe_filename(url: str, *, suffix: str = "") -> str:
    host = urlparse(url).netloc or url
    safe = host.replace(":", "_").replace("/", "_")
    return f"{safe}{suffix}.png"


def _screenshot(page: object, path: str) -> None:
    """Best-effort screenshot — never let an I/O hiccup kill the visit."""
    try:
        page.screenshot(path=path, full_page=False)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        log.info("screenshot failed for %s: %s", path, exc)


def _signup_in_popup(
    page: object,
    popup: object,
    vendor: str | None,
    *,
    email: str,
    phone: str,
    channels: list[str],
    dry_run: bool,
    now_iso: str,
    shop: str,
    post_submit_wait_ms: int = _POST_SUBMIT_WAIT_MS,
    post_path: str | None = None,
) -> list[dict]:
    """Fill + submit the detected popup for the requested ``channels``.

    Returns a list of per-channel attempt records (one per channel attempted).
    Pure with respect to the browser surface — it only calls ``popup_detect``
    helpers and ``_fill_phone`` / ``_visible_text`` on the passed ``page`` /
    ``popup`` objects, so it's exercised in tests with Playwright fakes.

    Handles three popup shapes:
      * single-form (email + phone fields together) → fill both, one submit;
      * email-only → fill email, submit; phone (if wanted) is recorded
        ``no_phone_field`` so the shop is marked SMS-unavailable;
      * email-first / phone-second (SMS opt-in) → after the email step
        succeeds, re-find a phone field and submit it.

    Phone uses ``_fill_phone`` (E.164 → national → bare fallback). A
    post-submit OTP / "confirm your number" prompt records ``requires_otp``.
    """
    want_email = "email" in channels
    want_phone = "phone" in channels

    def rec(channel: str, result: str, *, code: str | None = None) -> dict:
        return {
            "at": now_iso, "channel": channel, "result": result,
            "vendor": vendor, "code_received": code,
            "dry_run": True if dry_run else None,
        }

    email_field = find_email_field(popup) if want_email else None
    phone_field = find_phone_field(popup) if want_phone else None
    submit_btn = find_submit_button(popup)

    # A popup container can match its vendor selector while the inputs are
    # still animating in, so the first field scan may run against a
    # half-rendered form (observed live, issue #14). Settle briefly and
    # re-find once before declaring the popup unusable.
    if submit_btn is None or (email_field is None and phone_field is None):
        page.wait_for_timeout(_FORM_SETTLE_MS)
        email_field = find_email_field(popup) if want_email else None
        phone_field = find_phone_field(popup) if want_phone else None
        submit_btn = find_submit_button(popup)

    # Not a usable form: no submit button, or none of our target fields.
    if submit_btn is None or (email_field is None and phone_field is None):
        missing = "submit button" if submit_btn is None else "target fields"
        log.info("popup at %s (vendor=%s) not a fillable form for %s (missing %s)",
                 shop, vendor, ",".join(channels), missing)
        out: list[dict] = []
        if want_email:
            out.append(rec("email", "form_fill_failed"))
        if want_phone:
            # A phone field with no submit is a fill failure; no phone field at
            # all is terminal (mark the channel unavailable).
            out.append(rec("phone",
                           "form_fill_failed" if phone_field is not None
                           else "no_phone_field"))
        return out

    # Dry-run: report which present channels we *would* fill; never submit.
    if dry_run:
        present = "+".join(
            c for c in ("email", "phone")
            if (c == "email" and email_field is not None)
            or (c == "phone" and phone_field is not None)
        )
        log.info("DRY_RUN: %s — would fill %s + submit (vendor=%s)",
                 shop, present or "<nothing>", vendor)
        out = []
        if email_field is not None:
            out.append(rec("email", "success"))
        if phone_field is not None:
            out.append(rec("phone", "success"))
        elif want_phone:
            # Phone wanted but no field visible now (might be a phone-second
            # step we can't reach without submitting in a dry run).
            out.append(rec("phone", "no_phone_field"))
        return out

    # ---- Real submission ----
    return _fill_and_submit(
        page, popup,
        vendor=vendor, email=email, phone=phone,
        email_field=email_field, phone_field=phone_field, submit_btn=submit_btn,
        now_iso=now_iso, shop=shop,
        want_phone=want_phone, allow_phone_step2=True,
        post_submit_wait_ms=post_submit_wait_ms, post_path=post_path,
    )


def _fill_and_submit(
    page: object,
    container: object,
    *,
    vendor: str | None,
    email: str,
    phone: str,
    email_field: object | None,
    phone_field: object | None,
    submit_btn: object,
    now_iso: str,
    shop: str,
    want_phone: bool,
    allow_phone_step2: bool,
    post_submit_wait_ms: int = _POST_SUBMIT_WAIT_MS,
    post_path: str | None = None,
) -> list[dict]:
    """Fill the resolved fields, submit, and record per-channel outcomes.

    The shared core of the heuristic (``_signup_in_popup``) and Claude
    (``_signup_via_claude``) paths — everything from field-fill through
    success/OTP detection is identical once the fields + submit button are
    resolved, so both callers pass their already-resolved locators here.

    ``container`` is the popup / dialog / form used for post-submit success
    detection and OTP text (``detect_success`` / ``_visible_text`` fall back to
    the page body when it isn't visible). ``allow_phone_step2`` gates the
    email-first / phone-second re-detect — enabled for the heuristic popup path
    (a real multi-screen popup), disabled for the Claude path (which is handed
    both fields in one shot from a single page snapshot).
    """
    def rec(channel: str, result: str, *, code: str | None = None) -> dict:
        return {
            "at": now_iso, "channel": channel, "result": result,
            "vendor": vendor, "code_received": code, "dry_run": None,
        }

    results: list[dict] = []

    email_filled = False
    if email_field is not None:
        try:
            email_field.fill(email)  # type: ignore[attr-defined]
            email_filled = True
        except Exception as exc:  # noqa: BLE001
            log.warning("email fill failed at %s: %s", shop, exc)
            results.append(rec("email", "form_fill_failed"))

    phone_done = False
    phone_filled = False
    if phone_field is not None:
        if _fill_phone(phone_field, phone) is not None:
            phone_filled = True
        else:
            log.warning("phone fill failed at %s", shop)
            results.append(rec("phone", "form_fill_failed"))
            phone_done = True

    check_consent_if_present(container)

    if not (email_filled or phone_filled):
        return results

    try:
        submit_btn.click()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        log.warning("submit click failed at %s: %s", shop, exc)
        if email_filled:
            results.append(rec("email", "form_fill_failed"))
        if phone_filled:
            results.append(rec("phone", "form_fill_failed"))
        return results

    success, code = detect_success(
        page, container, original_url=shop, post_submit_wait_ms=post_submit_wait_ms,
    )
    if post_path:
        _screenshot(page, post_path)

    if email_filled:
        if success:
            log.info("email signup at %s (vendor=%s, code=%s)",
                     shop, vendor, code or "<none>")
            results.append(rec("email", "success", code=code))
        else:
            results.append(rec("email", "form_fill_failed"))

    if phone_filled:
        if looks_like_otp(_visible_text(page, container)):
            log.info("phone signup at %s needs OTP — skipping", shop)
            results.append(rec("phone", "requires_otp"))
        elif success:
            log.info("phone signup at %s (vendor=%s)", shop, vendor)
            results.append(rec("phone", "success"))
        else:
            results.append(rec("phone", "form_fill_failed"))
        phone_done = True

    # Email-first / phone-second (SMS opt-in) popup: only when phone is wanted,
    # wasn't in step 1, and the email step succeeded — the SMS step is revealed
    # after the email submit. Try the same container first, then one re-detect.
    if want_phone and allow_phone_step2 and not phone_done and success:
        results.append(_signup_phone_step2(
            page, container, vendor,
            phone=phone, now_iso=now_iso, shop=shop,
            post_submit_wait_ms=post_submit_wait_ms,
        ))

    return results


def _signup_phone_step2(
    page: object,
    popup: object,
    vendor: str | None,
    *,
    phone: str,
    now_iso: str,
    shop: str,
    post_submit_wait_ms: int,
) -> dict:
    """Fill + submit a second-step phone field revealed after the email step.

    Returns a single phone attempt record. ``no_phone_field`` when no SMS step
    appears (so the channel is marked unavailable).
    """
    def rec(result: str) -> dict:
        return {"at": now_iso, "channel": "phone", "result": result,
                "vendor": vendor, "code_received": None, "dry_run": None}

    field = find_phone_field(popup)
    step2 = popup
    if field is None:
        step2, _ = detect_popup(
            page, initial_wait_ms=500,
            trigger_exit_intent=False, trigger_scroll=False,
        )
        field = find_phone_field(step2) if step2 is not None else None
    if field is None:
        log.info("no phone step at %s (vendor=%s)", shop, vendor)
        return rec("no_phone_field")

    submit2 = find_submit_button(step2)
    if submit2 is None or _fill_phone(field, phone) is None:
        return rec("form_fill_failed")

    check_consent_if_present(step2)
    try:
        submit2.click()  # type: ignore[attr-defined]
        ok2, _ = detect_success(page, step2, original_url=shop,
                                post_submit_wait_ms=post_submit_wait_ms)
    except Exception as exc:  # noqa: BLE001
        log.warning("phone step-2 submit failed at %s: %s", shop, exc)
        return rec("form_fill_failed")

    if looks_like_otp(_visible_text(page, step2)):
        log.info("phone step-2 at %s needs OTP — skipping", shop)
        return rec("requires_otp")
    return rec("success") if ok2 else rec("form_fill_failed")


def _visit(
    shop: str,
    email: str,
    phone: str,
    *,
    channels: list[str],
    dry_run: bool,
    screenshot_dir: str,
    claude: _ClaudeFallback | None = None,
    page_timeout_ms: int = _PAGE_TIMEOUT_MS,
    popup_wait_ms: int = _POPUP_WAIT_MS,
    post_submit_wait_ms: int = _POST_SUBMIT_WAIT_MS,
) -> list[dict]:
    """Open ``shop``, detect the popup, fill the requested ``channels``, submit.

    Returns a list of per-channel attempt records (see ``_signup_in_popup``).
    Visit-level outcomes that precede field interaction (no popup, captcha,
    network error) return a single record attributed to the primary requested
    channel — its ``result`` is what matters, not the channel.

    Failure-isolated: any exception (Playwright timeout, navigation error,
    detached locator, ...) is caught and surfaced as a ``"network_error"``
    attempt so a single bad shop never aborts the batch.
    """
    # Lazy import — Playwright pulls in a 300MB browser binary; keep it
    # out of module load so the rest of the package stays usable when only
    # the daily cron is being run.
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    now_iso = datetime.now(timezone.utc).isoformat()
    primary = "email" if "email" in channels else "phone"

    def visit_rec(result: str, *, vendor: str | None = None) -> list[dict]:
        return [{
            "at": now_iso, "channel": primary, "result": result,
            "vendor": vendor, "code_received": None,
            "dry_run": True if dry_run else None,
        }]

    os.makedirs(screenshot_dir, exist_ok=True)
    pre_path = os.path.join(screenshot_dir, _safe_filename(shop, suffix="_pre"))
    post_path = os.path.join(screenshot_dir, _safe_filename(shop, suffix="_post"))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(user_agent=_USER_AGENT)
                page = ctx.new_page()
                # ``wait_until="load"`` waits for the ``load`` event (all
                # subresources fetched) rather than just ``DOMContentLoaded``.
                # On-slice and other JS-heavy themes need this extra time
                # before any popup heuristics run; without it we get
                # half-rendered pages.
                page.goto(shop, timeout=page_timeout_ms, wait_until="load")
                # Settling wait — lets popups fire, lets JS-rendered bot-block
                # interstitials (Etsy/Cloudflare) actually render their text
                # so ``looks_like_captcha`` can see it.
                page.wait_for_timeout(popup_wait_ms)

                # Capture pre-popup screenshot AFTER the wait so it reflects
                # what detection actually sees.
                _screenshot(page, pre_path)

                # Short-circuit Cloudflare / Etsy DataDome bot blocks.
                # ``detect_bot_block`` checks body text, iframe URLs, and
                # JS-evaluated innerText — catching both the easy CSS-text
                # blocks and the iframe-rendered DataDome-style challenges.
                if detect_bot_block(page):
                    log.info("captcha / bot-block at %s", shop)
                    return visit_rec("captcha_blocked")

                # The popup wait has already happened, so detect_popup only
                # needs to run its staged scans (scroll nudge + exit-intent).
                popup, vendor = detect_popup(page, initial_wait_ms=0)
                if popup is None:
                    log.info("no popup at %s", shop)
                    attempts = visit_rec("no_popup_detected")
                else:
                    attempts = _signup_in_popup(
                        page, popup, vendor,
                        email=email, phone=phone, channels=channels,
                        dry_run=dry_run, now_iso=now_iso, shop=shop,
                        post_submit_wait_ms=post_submit_wait_ms,
                        post_path=post_path,
                    )

                # Phase 4 — Claude vision/DOM fallback when the heuristics
                # missed (no popup, or a popup with no fillable form). Budgeted
                # per run; a bot-block / network error never reaches here.
                if (claude is not None and _heuristic_missed(attempts)
                        and claude.take()):
                    log.info("heuristics missed at %s — trying Claude fallback",
                             shop)
                    claude_attempts = _signup_via_claude(
                        page, email=email, phone=phone, channels=channels,
                        dry_run=dry_run, now_iso=now_iso, shop=shop,
                        claude=claude, post_submit_wait_ms=post_submit_wait_ms,
                        post_path=post_path,
                    )
                    if claude_attempts is not None:
                        attempts = claude_attempts

                return attempts
            finally:
                browser.close()
    except PlaywrightError as exc:
        log.warning("playwright error on %s: %s", shop, exc)
        return visit_rec("network_error")
    except Exception as exc:  # noqa: BLE001 — never let one shop kill the batch
        log.warning("unexpected error on %s: %s", shop, exc)
        return visit_rec("network_error")


# ---------------------------------------------------------------------------
# CLI + orchestration
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="newsletter_signup")
    p.add_argument("--shop", help="Sign up at one shop only (homepage URL).")
    p.add_argument(
        "--channel",
        choices=("email", "phone", "both"),
        default="both",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Visit + screenshot, but skip the Gist write at the end.",
    )
    p.add_argument(
        "--max-shops",
        type=int,
        default=None,
        help="Cap the number of shops visited this invocation.",
    )
    p.add_argument(
        "--retry-failed",
        action="store_true",
        help=(
            "Force re-try of shops whose last attempt was a transient "
            "failure (no_popup_detected / network_error)."
        ),
    )
    p.add_argument(
        "--report-only",
        action="store_true",
        help=(
            "Infer which watchlist shops you already get marketing mail from "
            "and print the subscribed/target split, then exit. No browser, no "
            "writes; bypasses SIGNUP_ENABLED (read-only)."
        ),
    )
    p.add_argument(
        "--no-infer-subscribed",
        dest="infer_subscribed",
        action="store_false",
        help=(
            "Disable the Gmail 'already subscribed?' inference and attempt a "
            "signup at every shop (overrides the default auto-skip)."
        ),
    )
    p.set_defaults(infer_subscribed=True)
    p.add_argument(
        "--no-claude-fallback",
        dest="claude_fallback",
        action="store_false",
        help=(
            "Disable the Phase 4 Claude vision/DOM fallback that fires when the "
            "heuristic popup detection misses. On by default when "
            "ANTHROPIC_API_KEY is set."
        ),
    )
    p.set_defaults(claude_fallback=True)
    p.add_argument(
        "--claude-no-screenshot",
        dest="claude_screenshot",
        action="store_false",
        help=(
            "Send only the DOM digest (no screenshot) to the Claude fallback — "
            "cheaper, slightly less accurate."
        ),
    )
    p.set_defaults(claude_screenshot=True)
    p.add_argument(
        "--claude-max-calls",
        type=int,
        default=_CLAUDE_MAX_CALLS_DEFAULT,
        help=(
            "Cap the number of Claude fallback API calls this run "
            f"(default {_CLAUDE_MAX_CALLS_DEFAULT}). 0 disables the fallback."
        ),
    )
    p.add_argument("--screenshot-dir", default=DEFAULT_SCREENSHOT_DIR)
    return p.parse_args(argv)


def _report_only(cfg: Config, args: argparse.Namespace) -> int:
    """Print which watchlist shops you already get marketing mail from.

    Read-only: no browser, no Gist write. The lowest-risk first look — it tells
    you how many shops the live signup pass would actually target."""
    log.info("reading state from gist")
    state = read_state(cfg.gist_id, cfg.github_token)
    aliases = state.get("aliases") or {}
    shops = _collect_shops(args, cfg, aliases)
    log.info("found %d unique shop homepages", len(shops))

    subscribed = _infer_subscribed(shops, cfg)
    sub_shops: list[str] = []
    target_shops: list[str] = []
    no_domain: list[str] = []
    for shop in shops:
        domain = _shop_domain(shop)
        if domain is None:
            no_domain.append(shop)
        elif domain in subscribed:
            sub_shops.append(shop)
        else:
            target_shops.append(shop)

    print(f"Subscription report ({len(shops)} shops):")
    print(f"  already subscribed (marketing mail found): {len(sub_shops)}")
    for s in sub_shops:
        print(f"    - {s}")
    print(f"  signup targets (no marketing mail found): {len(target_shops)}")
    for s in target_shops:
        print(f"    - {s}")
    if no_domain:
        print(f"  unresolved (no domain parsed): {len(no_domain)}")
        for s in no_domain:
            print(f"    - {s}")
    return 0


def run(argv: list[str] | None = None, cfg: Config | None = None) -> int:
    args = _parse_args(argv)
    cfg = cfg or load_config()

    # Read-only report bypasses the master toggle: it performs no signup, only
    # tells you which shops you already get marketing mail from.
    if args.report_only:
        return _report_only(cfg, args)

    # Master toggle. Defaults to off so this command never silently fires when
    # the user just wanted to test environment / config. The daily cron does
    # NOT consult this flag — it's only checked here.
    if not cfg.signup_enabled:
        log.warning(
            "newsletter_signup is disabled. "
            "Set SIGNUP_ENABLED=1 in .env (or repo Actions secrets) to enable."
        )
        return 0

    # Channels to fill this run. ``both`` (default) fills whichever of email /
    # phone the popup exposes; the skip gate is the same set, with phone
    # counting as "done" once subscribed OR marked unavailable (no SMS field).
    channels_to_fill = list(_CHANNELS_FOR_ARG[args.channel])
    if "phone" in channels_to_fill and not cfg.signup_phone:
        if channels_to_fill == ["phone"]:
            log.warning(
                "--channel=phone needs SIGNUP_PHONE set in .env; nothing to do."
            )
            return 0
        log.warning("SIGNUP_PHONE not set — proceeding email-only.")
        channels_to_fill = [c for c in channels_to_fill if c != "phone"]
    channels_for_skip = list(channels_to_fill)

    # Phase 4 — Claude vision/DOM fallback context. On by default when an API
    # key is configured and the budget is positive; the daily cron never runs
    # this command so there's no unattended spend.
    claude_fb: _ClaudeFallback | None = None
    if (args.claude_fallback and args.claude_max_calls > 0
            and cfg.anthropic_api_key):
        claude_fb = _ClaudeFallback(
            _anthropic_client(cfg), DEFAULT_MODEL,
            want_screenshot=args.claude_screenshot,
            max_calls=args.claude_max_calls,
        )
        log.info("Claude fallback enabled (budget=%d, screenshot=%s)",
                 args.claude_max_calls, args.claude_screenshot)
    elif args.claude_fallback and not cfg.anthropic_api_key:
        log.info("Claude fallback unavailable — ANTHROPIC_API_KEY not set")

    log.info("reading state from gist")
    state = read_state(cfg.gist_id, cfg.github_token)
    signup_state = dict(state.get("signup") or {})
    aliases = state.get("aliases") or {}

    shops = _collect_shops(args, cfg, aliases)
    log.info("found %d unique shop homepages", len(shops))

    # Gmail-inference auto-skip — EMAIL channel only (it asks "do you already
    # get marketing mail from this shop?"). Seeds matched shops as email-done so
    # the loop never re-submits your address where you're already subscribed.
    # Skipped for an explicit --shop, and when email isn't a requested channel.
    if args.infer_subscribed and not args.shop and "email" in channels_to_fill:
        pending = [
            s for s in shops
            if not _should_skip(
                s, signup_state, ["email"], retry_failed=args.retry_failed,
            )
        ]
        if pending:
            log.info("inferring existing subscriptions for %d shop(s) via Gmail", len(pending))
            subscribed = _infer_subscribed(pending, cfg)
            seeded = _seed_inferred_subscriptions(
                pending, signup_state, subscribed,
                now_iso=datetime.now(timezone.utc).isoformat(),
            )
            log.info(
                "Gmail inference: %d shop(s) already subscribed on email",
                len(seeded),
            )

    visited = 0
    skipped = 0
    successes = 0
    for shop in shops:
        if args.max_shops is not None and visited >= args.max_shops:
            log.info("--max-shops=%d reached, stopping", args.max_shops)
            break
        # Explicit --shop bypasses the skip gate so you can re-target on demand.
        if not args.shop and _should_skip(
            shop, signup_state, channels_for_skip, retry_failed=args.retry_failed,
        ):
            log.info("skip %s — already done on %s", shop, "+".join(channels_for_skip))
            skipped += 1
            continue
        # Don't re-fill a channel already subscribed (re-submitting your address
        # where you're subscribed is pointless + extra bot-detection surface).
        # An explicit --shop re-targets every requested channel.
        already = set() if args.shop else _subscribed_channels(signup_state.get(shop))
        effective = [c for c in channels_to_fill if c not in already]
        if not effective:
            log.info("skip %s — requested channels already subscribed", shop)
            skipped += 1
            continue
        if visited > 0:
            time.sleep(random.uniform(*_INTER_SHOP_JITTER))
        attempts = _visit(
            shop,
            cfg.gmail_username,
            cfg.signup_phone,
            channels=effective,
            dry_run=args.dry_run,
            screenshot_dir=args.screenshot_dir,
            claude=claude_fb,
        )
        for attempt in attempts:
            _record_attempt(signup_state, shop, attempt)
        visited += 1
        if any(a.get("result") == "success" for a in attempts):
            successes += 1

    log.info(
        "done — visited=%d (success=%d) skipped=%d total_shops=%d",
        visited, successes, skipped, len(shops),
    )
    if claude_fb is not None:
        log.info("Claude fallback used %d/%d calls",
                 claude_fb.used, claude_fb.max_calls)

    if args.dry_run:
        log.info("DRY_RUN: skipping write_state")
    else:
        log.info("writing signup_state to gist")
        write_state(
            cfg.gist_id,
            cfg.github_token,
            prices=state.get("prices") or {},
            aliases=state.get("aliases") or {},
            codes=state.get("codes") or [],
            signup=signup_state,
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
        log.exception("newsletter_signup run failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
