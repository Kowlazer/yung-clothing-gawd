"""Load and validate required environment variables.

In the GitHub Actions runtime these are populated from repository secrets.
For local runs, dotenv-style loading is delegated to the caller (e.g. main.py
calls ``dotenv.load_dotenv()`` before ``load_config()``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

_REQUIRED: tuple[str, ...] = (
    "WATCHLIST_URL",
    "RESEND_API_KEY",
    "FROM_EMAIL",
    "TO_EMAIL",
    "GITHUB_TOKEN",
    "GIST_ID",
    "ANTHROPIC_API_KEY",
    "GMAIL_USERNAME",
    "GMAIL_APP_PASSWORD",
)

# Truthy values for boolean env-var flags. Mirrors SALE_CHECK_DRY_RUN parsing
# in main.py so the user's mental model is consistent across the project.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def _truthy_or(value: str | None, default: bool) -> bool:
    """Parse a boolean flag that defaults to ``default`` when unset/blank.

    Unlike ``_truthy`` (which treats blank as False), an *unset or blank* value
    yields ``default`` — so a flag can ship enabled-by-default yet still be
    turned off with ``0``/``false``/``no``/``off``.
    """
    raw = (value or "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _parse_weekday(value: str | None, default: str = "fri") -> str:
    """Normalise a weekday flag to a 3-letter lowercase code (mon..sun).

    Accepts full names or 3+ letter prefixes (``Friday``, ``fri``, ``FRI``).
    Falls back to ``default`` when unset/blank/unrecognised so a typo can never
    silently disable the weekly email by matching no day."""
    raw = (value or "").strip().lower()[:3]
    return raw if raw in _WEEKDAYS else default


class ConfigError(RuntimeError):
    """Raised when required env vars are missing."""


@dataclass(frozen=True)
class Config:
    watchlist_url: str
    resend_api_key: str
    from_email: str
    to_email: str
    github_token: str
    gist_id: str
    anthropic_api_key: str
    gmail_username: str
    gmail_app_password: str
    # Newsletter auto-signup is opt-in. ``signup_enabled`` defaults to False
    # so the daily cron never accidentally tries to fill forms, and the
    # ``newsletter_signup`` manual command refuses to run until the user
    # flips it on. ``signup_phone`` is only consumed when enabled.
    signup_enabled: bool
    signup_phone: str
    # Sizes the user actually buys. Drives size-aware OOS detection in
    # ``extract.parse``: when a Shopify product has a Size option AND none of
    # the user's preferred sizes are available (even if other sizes are), the
    # item is treated as out of stock with a "still available in X, Y" note.
    # Empty tuple → existing page-level OOS behaviour, unchanged.
    preferred_sizes: tuple[str, ...]
    # Per-category override for items detected as pants/trousers/joggers etc.
    # (see ``main._is_pants_url``). When set, replaces ``preferred_sizes`` for
    # those URLs only — useful when bottom sizing runs different from tops
    # (e.g. a different size shortlist for shirts vs. pants).
    # Empty tuple → falls back to ``preferred_sizes`` for all items.
    preferred_sizes_pants: tuple[str, ...]
    # BodySpec account credentials for the wardrobe body-comp backfill
    # (``order_scan --backfill-bodycomp``). Optional — NOT in ``_REQUIRED`` so
    # the daily cron runs without them; the backfill command checks them itself
    # and errors clearly when blank. See ``src/bodyspec.py``. Defaulted so
    # existing ``Config(...)`` test fixtures keep constructing cleanly.
    bodyspec_username: str = ""
    bodyspec_password: str = ""
    # Max age (days) before the daily cron refreshes the cached BodySpec scans
    # (``body_scans.json``). Age-gated rather than weekday-gated so a missed cron
    # day self-heals; the refresh only runs when BodySpec creds are set. Default
    # 7 — the user scans at most twice a week, so weekly cached data stays fresh.
    body_scan_max_age_days: int = 7
    # Fit-feedback web form (Apps Script). ``fit_form_base_url`` is the deployed
    # ``/exec`` URL; ``fit_link_secret`` is the HMAC secret shared with the
    # Apps Script Script Properties (see ``src/fit_links.py`` for the signing
    # contract). Both blank → the feature is dormant: no "Fit feedback wanted"
    # section is rendered and no weekly email is sent (the digest builder and
    # main.run() short-circuit), so the cron is unaffected until they're set.
    fit_form_base_url: str = ""
    fit_link_secret: str = ""
    # Independent on/off switches for the two nudge surfaces, default ON (each
    # also self-suppresses when nothing is pending). ``fit_feedback_weekly_day``
    # is the UTC weekday (mon..sun) the standalone fit email fires on.
    fit_feedback_daily: bool = True
    fit_feedback_weekly: bool = True
    fit_feedback_weekly_day: str = "fri"
    # Daily "Bought — remove from watchlist?" section: signed links for purchased
    # items still listed on the watchlist Doc, approved via the same Apps Script
    # web app (reuses ``fit_form_base_url`` + ``fit_link_secret``; see
    # ``src/watchlist_links.py``). Default ON; self-suppresses when nothing is
    # pending and stays dormant until the fit-form secrets are set.
    watchlist_removal_daily: bool = True
    # Daily "Review requests" section: aggregates post-purchase review-request
    # emails (Loox/Yotpo/etc.), deduped one-per-order over a recent window, each
    # linking to the email. Stateless — recomputed from Gmail each run, no Gist
    # file. Default ON; self-suppresses when nothing matches.
    # ``review_requests_days`` is the recent-window length the digest section
    # covers (a separate all-time Gmail-search link is always shown).
    review_requests_daily: bool = True
    review_requests_days: int = 30
    # Day-to-day price-history classifier (see src/price_history.py). Distinguishes
    # a genuine markdown from a year-round "always 50% off" anchor by tracking each
    # URL's observed price as a change-point series and comparing today's price to
    # its trailing-max baseline. ``price_history_retention_days`` is how far back
    # the per-URL change-points are kept; ``price_baseline_days`` is the trailing-
    # max window the real-drop-vs-standing-discount call uses; ``price_history_min_days``
    # is the minimum tracking age before that call is trusted (below it a page
    # markdown is taken at face value, as before); ``price_drop_margin_pct`` is how
    # far below baseline today's price must sit to count as a real drop. All
    # defaulted, so the feature ships on with no secrets to set.
    price_history_retention_days: int = 365
    price_baseline_days: int = 90
    price_history_min_days: int = 7
    price_drop_margin_pct: float = 2.0
    # How far back each per-variant (size/colour) in/low/out change-point series
    # is kept (see src/variant_history.py) — the timeline that lets the digest say
    # "L low 5d" and diff today's stock against the last check. Change-point
    # storage means a flat stock state costs ~1 token, so a year is essentially
    # free, mirroring price_history_retention_days.
    variant_history_retention_days: int = 365
    # Shops whose purchases are kept out of the wardrobe entirely (privacy). The
    # order scanner never ingests an order/shipping email from one of these, and
    # hard-deletes any already-stored items on its next run; the daily cron also
    # filters them out of the fit/removal nudges. Matched case-insensitively as a
    # normalised substring against both the shop name and domain — see
    # ``order_parse.is_excluded_shop``. Empty tuple → nothing excluded (default).
    excluded_shops: tuple[str, ...] = ()
    # Shops the user gets marketing SMS from (on the Google Voice number) but
    # that aren't on the watchlist. Their texted sales would otherwise be
    # dropped — voice.py only attributes an SMS to a watchlist shop or one of
    # these. Display case is preserved (used as the shop name in the digest)
    # and matched case-insensitively as a body substring. The digest's
    # "Untracked SMS senders" section surfaces candidates to add. The PRIMARY
    # surface is now the Doc's "Shops to track sales for:" section
    # (classify.sales_tracking_shops); main unions it with this env var, so
    # this remains as a supplement. Empty tuple + no Doc section → SMS
    # attribution is watchlist-only (default).
    sms_sale_shops: tuple[str, ...] = ()
    # Restock-notification auto-signup is opt-in and manual, exactly like the
    # newsletter signup. ``restock_signup_enabled`` defaults False so the
    # ``restock_signup`` command refuses to run until the user flips it on; the
    # daily cron never consults it. Signup email reuses ``gmail_username``.
    restock_signup_enabled: bool = False
    # Back-in-stock email detection runs in the daily cron (read-only Gmail pass)
    # and is on by default. ``restock_email_days`` is the recent-window length the
    # digest's email-sourced "Back in stock" lines cover.
    restock_emails_daily: bool = True
    restock_email_days: int = 7
    # Shadow A/B model for the daily fuzzy call (cost lever #5, issue #16).
    # When set (e.g. "claude-haiku-4-5-20251001"), resolve_fuzzy also sends the
    # identical payload to this model and the verdict diff accumulates in
    # shadow_runs.json for `python -m src.shadow_report` to review before any
    # model swap. The digest is never affected. Blank/unset → no shadow call.
    shadow_model: str = ""


def _parse_sizes(raw: str | None) -> tuple[str, ...]:
    """Split ``PREFERRED_SIZES="S,M"`` into ``("S","M")``. Uppercased,
    whitespace-stripped, deduped while preserving order. Empty/unset → ()."""
    if not raw or not raw.strip():
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for tok in raw.split(","):
        norm = tok.strip().upper()
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return tuple(out)


def _parse_excluded(raw: str | None) -> tuple[str, ...]:
    """Split ``EXCLUDED_SHOPS="Nocturne Goods, Other"`` into lowercased tokens.

    Lowercased (not uppercased like ``_parse_sizes``) because the values are
    shop names / domains matched case-insensitively as substrings by
    ``order_parse.is_excluded_shop``. Whitespace-stripped, deduped, order
    preserved. Empty/unset → () which disables exclusion entirely."""
    if not raw or not raw.strip():
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for tok in raw.split(","):
        norm = tok.strip().lower()
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return tuple(out)


def _parse_shop_list(raw: str | None) -> tuple[str, ...]:
    """Split ``SMS_SALE_SHOPS="Harborlight, Greyfox"`` into ``("Harborlight","Greyfox")``.

    Unlike ``_parse_excluded`` the **display case is preserved** (these become
    shop names shown in the digest); dedup is case-insensitive, order preserved.
    Empty/unset → ()."""
    if not raw or not raw.strip():
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for tok in raw.split(","):
        norm = tok.strip()
        if norm and norm.lower() not in seen:
            seen.add(norm.lower())
            out.append(norm)
    return tuple(out)


def _parse_int_or(raw: str | None, default: int) -> int:
    """Parse a positive int env var, falling back to ``default`` when unset,
    blank, non-numeric, or <= 0 (a zero/negative refresh age makes no sense)."""
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def _parse_float_or(raw: str | None, default: float) -> float:
    """Parse a non-negative float env var, falling back to ``default`` when unset,
    blank, non-numeric, or < 0 (a negative drop margin makes no sense)."""
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return val if val >= 0 else default


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Read required env vars from ``env`` (defaults to ``os.environ``).

    Raises ``ConfigError`` listing any missing or blank keys.
    """
    src: Mapping[str, str] = env if env is not None else os.environ
    missing = [k for k in _REQUIRED if not (src.get(k) or "").strip()]
    if missing:
        raise ConfigError(
            "missing required env vars: " + ", ".join(missing)
        )
    return Config(
        watchlist_url=src["WATCHLIST_URL"],
        resend_api_key=src["RESEND_API_KEY"],
        from_email=src["FROM_EMAIL"],
        to_email=src["TO_EMAIL"],
        github_token=src["GITHUB_TOKEN"],
        gist_id=src["GIST_ID"],
        anthropic_api_key=src["ANTHROPIC_API_KEY"],
        gmail_username=src["GMAIL_USERNAME"],
        gmail_app_password=src["GMAIL_APP_PASSWORD"],
        signup_enabled=_truthy(src.get("SIGNUP_ENABLED")),
        signup_phone=(src.get("SIGNUP_PHONE") or "").strip(),
        preferred_sizes=_parse_sizes(src.get("PREFERRED_SIZES")),
        preferred_sizes_pants=_parse_sizes(src.get("PREFERRED_SIZES_PANTS")),
        bodyspec_username=(src.get("BODYSPEC_USERNAME") or "").strip(),
        bodyspec_password=(src.get("BODYSPEC_PASSWORD") or "").strip(),
        body_scan_max_age_days=_parse_int_or(src.get("BODY_SCAN_MAX_AGE_DAYS"), 7),
        fit_form_base_url=(src.get("FIT_FORM_BASE_URL") or "").strip(),
        fit_link_secret=(src.get("FIT_LINK_SECRET") or "").strip(),
        fit_feedback_daily=_truthy_or(src.get("FIT_FEEDBACK_DAILY"), True),
        fit_feedback_weekly=_truthy_or(src.get("FIT_FEEDBACK_WEEKLY"), True),
        fit_feedback_weekly_day=_parse_weekday(src.get("FIT_FEEDBACK_WEEKLY_DAY")),
        watchlist_removal_daily=_truthy_or(src.get("WATCHLIST_REMOVAL_DAILY"), True),
        review_requests_daily=_truthy_or(src.get("REVIEW_REQUESTS_DAILY"), True),
        review_requests_days=_parse_int_or(src.get("REVIEW_REQUESTS_DAYS"), 30),
        price_history_retention_days=_parse_int_or(src.get("PRICE_HISTORY_RETENTION_DAYS"), 365),
        price_baseline_days=_parse_int_or(src.get("PRICE_BASELINE_DAYS"), 90),
        price_history_min_days=_parse_int_or(src.get("PRICE_HISTORY_MIN_DAYS"), 7),
        price_drop_margin_pct=_parse_float_or(src.get("PRICE_DROP_MARGIN_PCT"), 2.0),
        variant_history_retention_days=_parse_int_or(
            src.get("VARIANT_HISTORY_RETENTION_DAYS"), 365),
        excluded_shops=_parse_excluded(src.get("EXCLUDED_SHOPS")),
        sms_sale_shops=_parse_shop_list(src.get("SMS_SALE_SHOPS")),
        restock_signup_enabled=_truthy(src.get("RESTOCK_SIGNUP_ENABLED")),
        restock_emails_daily=_truthy_or(src.get("RESTOCK_EMAILS_DAILY"), True),
        restock_email_days=_parse_int_or(src.get("RESTOCK_EMAIL_DAYS"), 7),
        shadow_model=(src.get("SHADOW_MODEL") or "").strip(),
    )
