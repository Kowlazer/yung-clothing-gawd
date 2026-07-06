"""Render a product page in headless Chromium and return its hydrated HTML.

Last rung of the Cloudflare-blocked recovery ladder in ``extract.extract()``
(issue #1): JS-hydrated SPA storefronts inject their Product JSON-LD
client-side, so neither the direct fetch (403/503 from the GitHub Actions
datacenter IP) nor the reader proxy's HTML snapshot (fetches fine, but
pre-hydration — confirmed live: Kotn's snapshot carries only an org-level
JSON-LD block, no offer) exposes a price. Only a real browser executing the
page's JS can surface it.

A Chromium launch per call is heavyweight (~2-4s), so this rung is:

- **Off the happy path** — the caller invokes it only after a direct fetch
  already 403/503'd AND every proxy recovery route missed.
- **Budgeted** — at most ``BROWSER_FALLBACK_MAX_ITEMS`` launches per process
  (default 8), so a pathological run full of blocked items can't burn the
  cron's 45-minute timeout on browser launches. Items past the budget just
  keep their prior "blocked" verdict.
- **Failure-isolated** — any error (playwright not importable, chromium
  binary not installed, navigation timeout, bot-block interstitial) returns
  None and the caller keeps the prior "blocked" verdict. The daily cron
  behaves exactly as before this module existed unless the render *succeeds*.

Thread-safety: ``extract()`` runs inside a ThreadPool and the sync Playwright
API must not be shared across threads, so each call runs a self-contained
``sync_playwright()`` block (its own driver + browser). The budget counter is
the only shared state, guarded by a lock.

Note the render happens from the *same* egress IP that was just blocked — a
real browser passes Cloudflare's JS challenge where plain httpx cannot, but
an IP-reputation hard block may still stop it. ``detect_bot_block`` catches
that case (the interstitial renders instead of the shop) so a challenge page
is never fed to the price extractors.
"""

from __future__ import annotations

import logging
import os
import threading
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Same realistic desktop-Chrome UA family the plain-httpx fetcher and the
# signup runners present. Playwright's default UA advertises "HeadlessChrome",
# which Cloudflare treats as an instant bot signal.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_PAGE_TIMEOUT_MS = 30_000   # goto() ceiling — a blocked-then-challenged load is slow
_NETWORK_IDLE_MS = 6_000    # wait for the SPA's product-data XHRs to finish
_SETTLE_MS = 1_500          # post-idle DOM-injection settle (JSON-LD appended late)

_DEFAULT_MAX_ATTEMPTS = 8


def _read_max_attempts() -> int:
    """Parse BROWSER_FALLBACK_MAX_ITEMS: non-negative int, default 8.

    0 is honoured (budget of zero = rung disabled); blank/garbage/negative
    falls back to the default.
    """
    raw = os.getenv("BROWSER_FALLBACK_MAX_ITEMS", "").strip()
    try:
        val = int(raw)
    except ValueError:
        return _DEFAULT_MAX_ATTEMPTS
    return val if val >= 0 else _DEFAULT_MAX_ATTEMPTS


_MAX_ATTEMPTS = _read_max_attempts()

_attempts_lock = threading.Lock()
_attempts = 0
_budget_warned = False

# Per-run negative cache: a domain whose render hit a bot-block interstitial
# will interstitial every sibling URL too (verified live: 8 same-domain
# blocked items would otherwise burn the whole launch budget on the same
# challenge page daily). First refusal short-circuits the rest of the domain.
_blocked_domains: set[str] = set()
_blocked_domains_lock = threading.Lock()


def _take_attempt() -> bool:
    """Consume one launch from the per-process budget; False when exhausted."""
    global _attempts, _budget_warned
    with _attempts_lock:
        if _attempts >= _MAX_ATTEMPTS:
            if not _budget_warned:
                _budget_warned = True
                log.warning(
                    "browser-render budget exhausted (%d launches); further "
                    "blocked items keep their last-known price this run",
                    _MAX_ATTEMPTS,
                )
            return False
        _attempts += 1
        return True


def fetch_rendered_html(url: str) -> str | None:
    """Load ``url`` in headless Chromium and return the post-hydration HTML.

    None on any failure (budget spent, domain already bot-blocked this run,
    playwright/chromium unavailable, navigation error, bot-block
    interstitial) — the caller treats None as "recovery missed" and keeps
    its prior verdict.
    """
    domain = (urlparse(url).netloc or "").lower()
    with _blocked_domains_lock:
        if domain in _blocked_domains:
            return None

    if not _take_attempt():
        return None

    # Lazy import — Playwright pulls in a 300MB browser binary; keep it out
    # of module load so the daily cron stays usable where only the plain
    # fetch paths are needed (and so unit tests never touch it).
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 — missing/broken install
        log.debug("playwright unavailable for %s: %s", url, exc)
        return None

    from src.popup_detect import detect_bot_block

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(user_agent=_USER_AGENT, locale="en-US")
                page = ctx.new_page()
                page.goto(url, timeout=_PAGE_TIMEOUT_MS, wait_until="load")
                # SPAs fetch product data over XHR after `load`, then inject
                # JSON-LD/meta into the DOM. networkidle is the right signal
                # but long-pollers/analytics can keep the wire busy forever,
                # so it's bounded and a timeout just means "render what we
                # have" — the settle wait below still gives injection a beat.
                try:
                    page.wait_for_load_state("networkidle",
                                             timeout=_NETWORK_IDLE_MS)
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(_SETTLE_MS)
                if detect_bot_block(page):
                    log.info("browser render bot-blocked at %s", url)
                    with _blocked_domains_lock:
                        _blocked_domains.add(domain)
                    return None
                html = page.content()
                return html if html and html.strip() else None
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 — chromium missing, nav timeout, ...
        log.debug("browser render failed for %s: %s", url, exc)
        return None
