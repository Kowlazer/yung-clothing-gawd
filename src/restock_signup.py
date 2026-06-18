"""Auto-signup for restock ("email me when back in stock") notifications.

Manual entry point — **not** part of the daily cron and **off by default**
(``RESTOCK_SIGNUP_ENABLED``), exactly like ``newsletter_signup``. For each
watchlist product that the daily run recorded as out of stock — in particular
out of stock *in one of the user's preferred sizes* — this:

  1. reads the OOS targets straight from ``prices.json`` (the daily cron already
     stores per-URL ``in_stock`` / ``size_options`` / ``available_sizes`` /
     ``preferred_sizes_applied``), so no extra scrape is needed to *find* them;
  2. opens each product page in headless Chromium (Playwright), selects the OOS
     size, reveals + detects the restock form (Klaviyo BIS / Swym / Back in
     Stock / Appikon / generic), and fills the configured **email**
     (``GMAIL_USERNAME``) and/or **phone** (``SIGNUP_PHONE``, the Google Voice
     number) — whichever the form exposes — then submits;
  3. records the result per ``(url, size, channel)`` in ``restock_state.json``
     so reruns skip a size+channel already signed up for.

``--channel {email,phone,both}`` (default ``both``) picks which fields to fill.
Many back-in-stock widgets are email-only; phone is opportunistic — filled only
where the form actually carries a tel field. Phone tries E.164 →
national → bare digits (HTML5 ``checkValidity()`` fallback), records
``requires_otp`` when the shop wants an SMS code (out of scope, the code lands
on the user's phone), and ``no_phone_field`` → marks the phone channel
*unavailable* for that ``(url, size)`` so ``both``/``phone`` reruns terminate.

The payoff lands back in the **daily digest**: the shop's "back in stock" email
is detected by ``src/restock_emails.py`` and shown in the "Back in stock"
section, tagged as an email alert. (An SMS restock alert forwarded by Google
Voice is ingested by ``src/voice.py`` the same way.)

Usage::

    python -m src.restock_signup [--url URL] [--size SIZE]
                                 [--channel email|phone|both] [--dry-run]
                                 [--max-items N] [--retry-failed]
                                 [--list-targets] [--screenshot-dir DIR]

``--list-targets`` is read-only (no browser, no Gist write) and bypasses
``RESTOCK_SIGNUP_ENABLED`` — the lowest-risk first look at what the live run
would attempt. ``--dry-run`` detects forms + screenshots but never submits or
writes state.

State schema (``restock_state.json``)::

    {
      "https://shop.com/products/x": {
        "sizes": {
          "M": {"email": {"signed_up_at": ISO, "vendor": str|null} | null,
                "phone": {"signed_up_at": ISO, "vendor": str|null}
                         | {"unavailable": true, "checked_at": ISO} | null}
               | null,
          "__product__": {...} | null
        },
        "attempts": [{"at": ISO, "size": str, "channel": str, "result": str,
                      "vendor": str|null, "dry_run": bool|null}, ...]
      }
    }

A legacy flat slot (``sizes["M"] = {"signed_up_at", "vendor"}``, written before
the phone channel existed) is read as an **email** signup and migrated to the
nested shape on the next write.

``result`` enum: ``"success" | "no_form_detected" | "size_not_found" |
"captcha_blocked" | "form_fill_failed" | "network_error" |
"already_signed_up" | "requires_otp" | "no_phone_field"``.
"""
from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone

from src.config import Config, load_config
from src.extract import _normalize_size
from src.restock_detect import (
    check_consent_if_present,
    detect_bot_block,
    detect_restock_form,
    detect_restock_success,
    fill_phone_field,
    find_email_field,
    find_phone_field,
    find_restock_submit,
    looks_like_otp,
    reveal_restock_form,
    select_size,
    select_size_in_form,
    visible_text,
)
from src.state import read_state, write_state

log = logging.getLogger(__name__)

DEFAULT_SCREENSHOT_DIR = "restock_screenshots"
# Whole-product (no specific size) signup key in the per-URL ``sizes`` map.
PRODUCT_LEVEL = "__product__"

_INTER_ITEM_JITTER = (5.0, 15.0)
_PAGE_TIMEOUT_MS = 20_000
_SETTLE_WAIT_MS = 2_500
_POST_SUBMIT_WAIT_MS = 4_000
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Transient results that ``--retry-failed`` re-attempts.
_RETRY_RESULTS = frozenset({"no_form_detected", "network_error"})

# The channels each --channel choice fills. ``both`` fills whichever of
# email/phone the restock form exposes.
_CHANNELS_FOR_ARG: dict[str, list[str]] = {
    "email": ["email"],
    "phone": ["phone"],
    "both": ["email", "phone"],
}


# ---------------------------------------------------------------------------
# Target selection — from prices.json (no extra scrape)
# ---------------------------------------------------------------------------

def _oos_target(url: str, entry: dict) -> tuple[str, list[str]] | None:
    """``(url, oos_sizes)`` if this product is a restock target, else None.

    ``oos_sizes`` = the user's preferred sizes that the product *offers* but
    that are currently out of stock (kept in the product's own option casing so
    they can be matched on the page). An empty list means the whole product is
    out of stock with no specific size to target. Size comparison goes through
    ``extract._normalize_size`` so "Medium"/"M"/"X-Large"/"XL" line up exactly
    as they do in the daily run."""
    size_options = entry.get("size_options") or []
    available = entry.get("available_sizes") or []
    preferred = entry.get("preferred_sizes_applied") or []

    avail_n = {_normalize_size(s) for s in available}
    offered_by_norm: dict[str, str] = {}
    for s in size_options:
        offered_by_norm.setdefault(_normalize_size(s), s)

    pref_offered: list[str] = []
    oos_pref: list[str] = []
    for s in preferred:
        n = _normalize_size(s)
        if n not in offered_by_norm:
            continue
        pref_offered.append(offered_by_norm[n])
        if n not in avail_n:
            oos_pref.append(offered_by_norm[n])

    if oos_pref:
        return url, oos_pref
    # The product offers a preferred size and none are OOS ⇒ the user's size is
    # available; nothing to sign up for even if the page reads OOS overall.
    if pref_offered:
        return None
    # No preferred size in this product's size space (or none configured): a
    # whole-product OOS is still a product-level restock target.
    if entry.get("in_stock") is False:
        return url, []
    return None


def _collect_targets(
    args: argparse.Namespace, prices: dict,
) -> list[tuple[str, list[str]]]:
    """The ``(url, sizes)`` targets this invocation considers.

    With ``--url`` it's that single product (its OOS sizes from ``prices.json``,
    or ``--size`` if given, or a product-level attempt as a last resort). Without
    it, every OOS product in ``prices.json``. ``--size`` narrows each target to
    that one size when present."""
    if args.url:
        url = args.url
        if args.size:
            return [(url, [args.size])]
        t = _oos_target(url, prices.get(url) or {})
        return [t] if t else [(url, [])]

    targets: list[tuple[str, list[str]]] = []
    for url, entry in prices.items():
        t = _oos_target(url, entry)
        if not t:
            continue
        _url, sizes = t
        if args.size:
            want = _normalize_size(args.size)
            sizes = [s for s in sizes if _normalize_size(s) == want]
            if not sizes:
                continue
        targets.append((url, sizes))
    return targets


# ---------------------------------------------------------------------------
# Skip + record
# ---------------------------------------------------------------------------

def _size_keys(sizes: list[str]) -> list[str]:
    """The per-size state keys for a target. Empty ``sizes`` → product-level."""
    return list(sizes) if sizes else [PRODUCT_LEVEL]


def _nested_slot(slot: dict | None) -> dict:
    """A size slot in the nested ``{"email":…, "phone":…}`` shape.

    Migrates a legacy flat slot (``{"signed_up_at", "vendor"}`` — an email
    signup written before the phone channel existed) into the email sub-record.
    """
    if slot and ("email" in slot or "phone" in slot):
        return slot
    if slot and slot.get("signed_up_at"):
        return {"email": slot, "phone": None}
    return {"email": None, "phone": None}


def _channel_rec(slot: dict | None, channel: str) -> dict | None:
    """The per-channel record from a size slot, tolerant of the legacy shape."""
    if not slot:
        return None
    if "email" in slot or "phone" in slot:
        return slot.get(channel)
    # Legacy flat slot == an email signup.
    if channel == "email" and slot.get("signed_up_at"):
        return slot
    return None


def _is_done(
    url: str, size_key: str, channel: str, state: dict, *, retry_failed: bool,
) -> bool:
    """True iff ``(url, size_key, channel)`` is done — a real signup, or (phone
    only) marked ``unavailable`` because the form had no SMS field.

    ``retry_failed`` doesn't un-skip a real success; it's accepted for symmetry
    with ``newsletter_signup`` but a recorded success/terminal is honoured."""
    entry = state.get(url) or {}
    rec = _channel_rec((entry.get("sizes") or {}).get(size_key), channel)
    if not rec:
        return False
    return bool(rec.get("signed_up_at") or rec.get("unavailable"))


def _effective_channels(
    url: str, size_key: str, channels: list[str], state: dict, *, retry_failed: bool,
) -> list[str]:
    """Requested channels still needing an attempt for ``(url, size_key)``."""
    return [
        c for c in channels
        if not _is_done(url, size_key, c, state, retry_failed=retry_failed)
    ]


def _record_attempt(state: dict, url: str, attempt: dict) -> None:
    """Append an attempt and reflect its outcome into the per-(size,channel) slot.

    ``success`` mirrors ``{signed_up_at, vendor}`` into the channel sub-record;
    ``no_phone_field`` marks ``phone`` ``{unavailable, checked_at}`` so reruns
    stop re-visiting. Dry-run and transient/failed results don't mutate state.
    """
    entry = state.setdefault(url, {"sizes": {}, "attempts": []})
    entry.setdefault("sizes", {})
    entry.setdefault("attempts", []).append(attempt)

    if attempt.get("dry_run"):
        return
    channel = attempt.get("channel")
    result = attempt.get("result")
    if channel not in ("email", "phone") or result not in ("success", "no_phone_field"):
        return
    size_key = attempt.get("size") or PRODUCT_LEVEL
    slot = _nested_slot(entry["sizes"].get(size_key))
    if result == "success":
        slot[channel] = {"signed_up_at": attempt["at"], "vendor": attempt.get("vendor")}
    elif channel == "phone":  # no_phone_field — terminal for SMS at this shop
        if not (slot.get("phone") or {}).get("signed_up_at"):
            slot["phone"] = {"unavailable": True, "checked_at": attempt["at"]}
    entry["sizes"][size_key] = slot


# ---------------------------------------------------------------------------
# Visit — Playwright product page → select size → restock form → submit
# ---------------------------------------------------------------------------

def _safe_filename(url: str, size_key: str, *, suffix: str = "") -> str:
    from urllib.parse import urlparse
    host = (urlparse(url).netloc or "site") + urlparse(url).path
    safe = "".join(c if c.isalnum() else "_" for c in host)[:80]
    sz = "".join(c if c.isalnum() else "_" for c in size_key)
    return f"{safe}__{sz}{suffix}.png"


def _screenshot(page: object, path: str) -> None:
    try:
        page.screenshot(path=path, full_page=False)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        log.info("screenshot failed for %s: %s", path, exc)


def _attempt_one(
    page,
    PlaywrightError,
    url: str,
    size_key: str,
    email: str,
    phone: str,
    *,
    channels: list[str],
    dry_run: bool,
    screenshot_dir: str,
    post_submit_wait_ms: int,
) -> list[dict]:
    """One ``(url, size)`` signup attempt against an already-open page.

    Assumes ``page`` has just navigated to ``url`` fresh. Fills whichever of the
    requested ``channels`` the restock form exposes and submits once. Returns a
    list of per-channel attempt records (size/channel-level outcomes that
    precede field interaction — ``size_not_found``/``no_form_detected`` — return
    a single record attributed to the primary requested channel). Never raises —
    Playwright errors surface as ``network_error``."""
    now_iso = datetime.now(timezone.utc).isoformat()
    primary = "email" if "email" in channels else "phone"
    want_email = "email" in channels
    want_phone = "phone" in channels

    def rec(channel: str, result: str, *, vendor: str | None = None) -> dict:
        return {"at": now_iso, "size": size_key, "channel": channel,
                "result": result, "vendor": vendor, "dry_run": dry_run or None}

    try:
        # Select the variant on the product page (best-effort) — some shops
        # reveal a per-variant notify form, others a single form with its own
        # size dropdown. We don't hard-fail here; the popup's own selector (if
        # any) gets a second chance below.
        page_ok = select_size(page, size_key) if size_key != PRODUCT_LEVEL else True

        reveal_restock_form(page)
        form, vendor = detect_restock_form(page)
        if form is None:
            log.info("%s (size=%s) — no restock form", url, size_key)
            return [rec(primary, "no_form_detected")]

        # Second size selector inside the popup (e.g. Steady Hands' dropdown),
        # so the alert targets the right variant. Only when a specific size is
        # wanted and it wasn't already set on the page.
        if size_key != PRODUCT_LEVEL:
            form_ok = select_size_in_form(form, size_key)
            if not (page_ok or form_ok):
                log.info("%s — size %s not selectable on page or in form",
                         url, size_key)
                return [rec(primary, "size_not_found", vendor=vendor)]

        email_field = find_email_field(form) if want_email else None
        phone_field = find_phone_field(form) if want_phone else None
        submit_btn = find_restock_submit(form)

        # Not a usable form: no submit, or none of our target fields present.
        if submit_btn is None or (email_field is None and phone_field is None):
            log.info("%s (size=%s) — form missing fields/submit for %s",
                     url, size_key, ",".join(channels))
            out: list[dict] = []
            if want_email:
                out.append(rec("email", "form_fill_failed", vendor=vendor))
            if want_phone:
                out.append(rec("phone",
                               "form_fill_failed" if phone_field is not None
                               else "no_phone_field", vendor=vendor))
            return out

        if dry_run:
            _screenshot(
                page, os.path.join(
                    screenshot_dir, _safe_filename(url, size_key, suffix="_dry")))
            present = "+".join(
                c for c in ("email", "phone")
                if (c == "email" and email_field is not None)
                or (c == "phone" and phone_field is not None))
            log.info("DRY_RUN: %s (size=%s) — would submit %s (vendor=%s)",
                     url, size_key, present or "<nothing>", vendor)
            out = []
            if email_field is not None:
                out.append(rec("email", "success", vendor=vendor))
            if phone_field is not None:
                out.append(rec("phone", "success", vendor=vendor))
            elif want_phone:
                out.append(rec("phone", "no_phone_field", vendor=vendor))
            return out

        # ---- Real submission ----
        results: list[dict] = []
        email_filled = False
        if email_field is not None:
            try:
                email_field.fill(email)
                email_filled = True
            except PlaywrightError as exc:
                log.warning("email fill failed at %s (size=%s): %s", url, size_key, exc)
                results.append(rec("email", "form_fill_failed", vendor=vendor))

        phone_filled = False
        if phone_field is not None:
            if fill_phone_field(phone_field, phone) is not None:
                phone_filled = True
            else:
                log.warning("phone fill failed at %s (size=%s)", url, size_key)
                results.append(rec("phone", "form_fill_failed", vendor=vendor))

        check_consent_if_present(form)

        if not (email_filled or phone_filled):
            if want_phone and phone_field is None:
                results.append(rec("phone", "no_phone_field", vendor=vendor))
            return results

        try:
            submit_btn.click()
        except PlaywrightError as exc:
            log.warning("submit failed at %s (size=%s): %s", url, size_key, exc)
            if email_filled:
                results.append(rec("email", "form_fill_failed", vendor=vendor))
            if phone_filled:
                results.append(rec("phone", "form_fill_failed", vendor=vendor))
            return results

        ok = detect_restock_success(
            page, form, original_url=url, post_submit_wait_ms=post_submit_wait_ms)
        _screenshot(
            page, os.path.join(
                screenshot_dir, _safe_filename(url, size_key, suffix="_post")))

        if email_filled:
            if ok:
                log.info("restock email signup OK: %s (size=%s, vendor=%s)",
                         url, size_key, vendor)
                results.append(rec("email", "success", vendor=vendor))
            else:
                results.append(rec("email", "form_fill_failed", vendor=vendor))
        if phone_filled:
            if looks_like_otp(visible_text(page, form)):
                log.info("restock phone signup at %s needs OTP — skipping", url)
                results.append(rec("phone", "requires_otp", vendor=vendor))
            elif ok:
                log.info("restock phone signup OK: %s (size=%s, vendor=%s)",
                         url, size_key, vendor)
                results.append(rec("phone", "success", vendor=vendor))
            else:
                results.append(rec("phone", "form_fill_failed", vendor=vendor))
        # Wanted phone but the form carried no SMS field → terminal.
        if want_phone and phone_field is None:
            results.append(rec("phone", "no_phone_field", vendor=vendor))
        return results
    except PlaywrightError as exc:
        log.warning("playwright error at %s (size=%s): %s", url, size_key, exc)
        return [rec(primary, "network_error")]
    except Exception as exc:  # noqa: BLE001 — never let one size kill the batch
        log.warning("unexpected error at %s (size=%s): %s", url, size_key, exc)
        return [rec(primary, "network_error")]


def _visit(
    url: str,
    size_specs: list[tuple[str, list[str]]],
    email: str,
    phone: str,
    *,
    dry_run: bool,
    screenshot_dir: str,
    page_timeout_ms: int = _PAGE_TIMEOUT_MS,
    settle_wait_ms: int = _SETTLE_WAIT_MS,
    post_submit_wait_ms: int = _POST_SUBMIT_WAIT_MS,
) -> list[dict]:
    """Sign up for restock alerts on ``url`` for each ``(size_key, channels)``.

    Opens one Chromium context and re-navigates fresh per size (so selecting a
    different variant starts from a clean page). Returns the flattened list of
    per-channel attempt records. Failure-isolated: a launch/navigation error
    yields a ``network_error`` record for each requested size's primary
    channel."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    os.makedirs(screenshot_dir, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat()

    def _visit_level(size_key: str, channels: list[str], result: str) -> dict:
        primary = "email" if "email" in channels else "phone"
        return {"at": now_iso, "size": size_key, "channel": primary,
                "vendor": None, "dry_run": dry_run or None, "result": result}

    results: list[dict] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(user_agent=_USER_AGENT)
                page = ctx.new_page()
                for size_key, channels in size_specs:
                    try:
                        page.goto(url, timeout=page_timeout_ms, wait_until="load")
                        page.wait_for_timeout(settle_wait_ms)
                    except PlaywrightError as exc:
                        log.warning("nav error at %s: %s", url, exc)
                        results.append(_visit_level(size_key, channels, "network_error"))
                        continue
                    if detect_bot_block(page):
                        log.info("captcha / bot-block at %s", url)
                        results.append(_visit_level(size_key, channels, "captcha_blocked"))
                        continue
                    results.extend(_attempt_one(
                        page, PlaywrightError, url, size_key, email, phone,
                        channels=channels, dry_run=dry_run,
                        screenshot_dir=screenshot_dir,
                        post_submit_wait_ms=post_submit_wait_ms,
                    ))
            finally:
                browser.close()
    except PlaywrightError as exc:
        log.warning("playwright launch error on %s: %s", url, exc)
        return [_visit_level(k, ch, "network_error") for k, ch in size_specs]
    except Exception as exc:  # noqa: BLE001
        log.warning("unexpected error on %s: %s", url, exc)
        return [_visit_level(k, ch, "network_error") for k, ch in size_specs]
    return results


# ---------------------------------------------------------------------------
# CLI + orchestration
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="restock_signup")
    p.add_argument("--url", help="Sign up at one product URL only.")
    p.add_argument("--size", help="Limit to one size (label as the shop spells it).")
    p.add_argument(
        "--channel", choices=("email", "phone", "both"), default="both",
        help="Which fields to fill in the restock form (default both). 'phone' "
             "needs SIGNUP_PHONE set; 'both' fills it where the form exposes it.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Detect forms + screenshot, but never submit or write state.",
    )
    p.add_argument(
        "--max-items", type=int, default=None,
        help="Cap the number of products visited this invocation.",
    )
    p.add_argument(
        "--retry-failed", action="store_true",
        help="Re-attempt sizes whose last result was transient "
             "(no_form_detected / network_error).",
    )
    p.add_argument(
        "--list-targets", action="store_true",
        help="Print the OOS targets from prices.json and exit. No browser, no "
             "writes; bypasses RESTOCK_SIGNUP_ENABLED (read-only).",
    )
    p.add_argument("--screenshot-dir", default=DEFAULT_SCREENSHOT_DIR)
    return p.parse_args(argv)


def _list_targets(targets: list[tuple[str, list[str]]]) -> int:
    print(f"Restock targets ({len(targets)} product(s)):")
    for url, sizes in targets:
        label = ", ".join(sizes) if sizes else "(whole product OOS)"
        print(f"  - {url}\n      sizes: {label}")
    return 0


def run(argv: list[str] | None = None, cfg: Config | None = None) -> int:
    args = _parse_args(argv)
    cfg = cfg or load_config()

    log.info("reading state from gist")
    state = read_state(cfg.gist_id, cfg.github_token)
    prices = state.get("prices") or {}
    targets = _collect_targets(args, prices)
    log.info("found %d restock target(s)", len(targets))

    # Read-only target listing bypasses the master toggle.
    if args.list_targets:
        return _list_targets(targets)

    if not cfg.restock_signup_enabled:
        log.warning(
            "restock_signup is disabled. Set RESTOCK_SIGNUP_ENABLED=1 in .env "
            "(or repo Actions secrets) to enable. Use --list-targets to preview."
        )
        return 0

    if not targets:
        log.info("no OOS targets — nothing to do")
        return 0

    # Channels to fill. ``both`` fills whichever fields the form exposes; phone
    # needs SIGNUP_PHONE (the Google Voice number) — drop it (or bail) if unset.
    channels_to_fill = list(_CHANNELS_FOR_ARG[args.channel])
    if "phone" in channels_to_fill and not cfg.signup_phone:
        if channels_to_fill == ["phone"]:
            log.warning("--channel=phone needs SIGNUP_PHONE set in .env; nothing to do.")
            return 0
        log.warning("SIGNUP_PHONE not set — proceeding email-only.")
        channels_to_fill = [c for c in channels_to_fill if c != "phone"]

    restock_state = dict(state.get("restock") or {})
    email = cfg.gmail_username
    phone = cfg.signup_phone

    visited = 0
    successes = 0
    skipped = 0
    for url, sizes in targets:
        if args.max_items is not None and visited >= args.max_items:
            log.info("--max-items=%d reached, stopping", args.max_items)
            break
        # Per size, the requested channels still needing an attempt. An explicit
        # --url re-targets every requested channel (you clearly want to retry).
        specs: list[tuple[str, list[str]]] = []
        for size_key in _size_keys(sizes):
            eff = (
                list(channels_to_fill) if args.url
                else _effective_channels(
                    url, size_key, channels_to_fill, restock_state,
                    retry_failed=args.retry_failed)
            )
            if eff:
                specs.append((size_key, eff))
        if not specs:
            log.info("skip %s — all target sizes already done on %s",
                     url, "+".join(channels_to_fill))
            skipped += 1
            continue
        if visited > 0:
            time.sleep(random.uniform(*_INTER_ITEM_JITTER))
        attempts = _visit(
            url, specs, email, phone,
            dry_run=args.dry_run, screenshot_dir=args.screenshot_dir,
        )
        for attempt in attempts:
            _record_attempt(restock_state, url, attempt)
            if attempt.get("result") == "success":
                successes += 1
        visited += 1

    log.info(
        "done — visited=%d (size-successes=%d) skipped=%d total_targets=%d",
        visited, successes, skipped, len(targets),
    )

    if args.dry_run:
        log.info("DRY_RUN: skipping write_state")
    else:
        log.info("writing restock_state to gist")
        write_state(
            cfg.gist_id,
            cfg.github_token,
            prices=state.get("prices") or {},
            aliases=state.get("aliases") or {},
            codes=state.get("codes") or [],
            restock=restock_state,
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
        log.exception("restock_signup run failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
