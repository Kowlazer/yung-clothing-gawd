"""Apply sale rules: compare current price to compare-at and Gist history."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

from src import price_history, variant_history

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class PriceRules:
    """Knobs for the change-point price-history sale classifier.

    ``retention_days``    — how far back ``prices.json`` keeps change-points.
    ``baseline_days``     — trailing-max window the "real drop vs standing
                            discount" call compares today's price against.
    ``min_history_days``  — minimum tracking age before we trust the baseline;
                            below it a page markdown is taken at face value
                            (behaves exactly as the pre-history detector).
    ``drop_margin_pct``   — how far below the baseline today's price must sit to
                            count as a genuine markdown, in percent — absorbs
                            penny/rounding wobble so a flat price isn't called a
                            drop.
    ``variant_retention_days`` — how far back each per-variant (size/colour)
                            in/low/out change-point series is kept; mirrors
                            ``retention_days`` for ``variant_history``.
    """

    retention_days: int = 365
    baseline_days: int = 90
    min_history_days: int = 7
    drop_margin_pct: float = 2.0
    variant_retention_days: int = 365


def _entry_date(value: object, fallback: date) -> date:
    """Parse an ISO timestamp/date string to a ``date``; ``fallback`` if unpar-seable."""
    if not isinstance(value, str) or not value:
        return fallback
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return fallback


def detect_sale(
    url: str,
    extracted: dict,
    history: dict,
    *,
    today: date | None = None,
    rules: PriceRules | None = None,
) -> dict:
    """
    Compare extracted price/stock data against a prior prices.json entry.

    Parameters:
        url       — product URL (used for logging; not included in output)
        extracted — result from extract.parse() / extract.extract()
        history   — prior prices.json entry for this URL, or {} if first time seen
        today     — observation date (defaults to today UTC); injectable for tests
        rules     — PriceRules knobs for the change-point history classifier

    Returns:
        sale_signal    "on_sale_per_page" | "price_dropped" | "standing_discount" | "no_change" | None
        stock_signal   "newly_out_of_stock" | "back_in_stock" | "newly_low_stock" | None
        error_signal   "could_not_check" | "removed_from_shop" | None
        prior_price    float | None  — set when the price fell vs. the last check
        baseline_price float | None  — trailing-max over the baseline window (success path)
        baseline_days  int   | None  — the window width used (for digest wording)
        variant_changes dict          — per-dimension list of per-value in/low/out
                                        transitions seen this run (empty on errors)
        last_known     dict | None   — prior entry; set on error for digest rendering
        updated_entry  dict | None   — None means prune this URL from state

    "standing_discount" is the year-round-fake-sale signal: the page advertises a
    markdown but our own observed price history shows it's never actually been
    higher than this across the baseline window (it's the permanent list-vs-sale
    anchor, not a real drop). It only fires once we have ``min_history_days`` of
    tracking; before that a page markdown is reported as "on_sale_per_page" just
    like the pre-history detector, so nothing regresses on cold start.
    """
    rules = rules or PriceRules()
    today = today or price_history.today_utc()
    now = _now_iso()
    error_kind: str | None = extracted.get("error_kind")

    # --- Error: 404 → one-strike prune ---
    if error_kind == "not_found":
        log.info("detect: %s returned 404 — marking for removal", url)
        return {
            "sale_signal": None,
            "stock_signal": None,
            "error_signal": "removed_from_shop",
            "prior_price": None,
            "last_known": history or None,
            "updated_entry": None,
        }

    # --- Error: transient (blocked, timeout, server_error, other) ---
    if error_kind:
        updated = {
            **history,
            "consecutive_failures": history.get("consecutive_failures", 0) + 1,
            "last_error_kind": error_kind,
            "last_seen": now,
        }
        return {
            "sale_signal": None,
            "stock_signal": None,
            "error_signal": "could_not_check",
            "prior_price": None,
            "last_known": history or None,
            "updated_entry": updated,
        }

    # --- Success path ---
    # Legacy entries without in_stock/low_stock default to safe values so the
    # first post-deploy run doesn't false-alarm on stock transitions.
    prev_in_stock: bool = history.get("in_stock", True)
    prev_low_stock: bool = history.get("low_stock", False)
    now_in_stock: bool = not extracted.get("out_of_stock", False)
    now_low_stock: bool = bool(extracted.get("low_stock", False))

    # Sale signal — on_sale_per_page is the primary signal when the page itself
    # advertises a markdown. prior_price is computed independently so the digest
    # can render BOTH "on sale per page" AND "dropped from $X last checked"
    # when both are true (the two numbers can differ — e.g. on sale at $40 with
    # page compare-at $80, but $50 last time we checked).
    cur_price = extracted.get("current_price")
    prior_price: float | None = None
    if (
        cur_price is not None
        and history.get("current_price") is not None
        and cur_price < history["current_price"]
    ):
        prior_price = history["current_price"]

    # Day-to-day price history (change-point series). Append today's observation,
    # prune to the retention window, then take the trailing-max baseline over the
    # baseline window. ``first_seen`` is stamped once and preserved so we know how
    # long we've actually been tracking this URL.
    prev_first_seen = history.get("first_seen")
    first_seen = prev_first_seen or now
    new_history: list[str] = list(history.get("price_history") or [])
    baseline: float | None = None
    if cur_price is not None:
        new_history = price_history.append_observation(new_history, cur_price, today)
        new_history = price_history.prune(new_history, today, rules.retention_days)
        baseline = price_history.baseline_max(new_history, today, rules.baseline_days)

    # Enough tracking history to trust the baseline? On a brand-new (or legacy,
    # pre-history) entry first_seen was just stamped, so we fall back to taking
    # the page markdown at face value until min_history_days have elapsed.
    tracking_days = (today - _entry_date(prev_first_seen, today)).days
    enough_history = prev_first_seen is not None and tracking_days >= rules.min_history_days

    if extracted.get("on_sale"):
        sale_signal: str = _classify_page_sale(
            cur_price, baseline, prior_price, enough_history, rules.drop_margin_pct
        )
    elif prior_price is not None:
        sale_signal = "price_dropped"
    else:
        sale_signal = "no_change"

    # Stock signal — independent of sale signal (both can fire on the same item)
    stock_signal: str | None = None
    if prev_in_stock and not now_in_stock:
        stock_signal = "newly_out_of_stock"
    elif not prev_in_stock and now_in_stock:
        stock_signal = "back_in_stock"
    elif not prev_low_stock and now_low_stock and now_in_stock:
        stock_signal = "newly_low_stock"

    # Per-variant (size/colour) state history + transitions. Each offered value
    # of each tracked dimension carries its own in/low/out change-point series
    # (see src/variant_history.py), mirroring the price-history machinery above.
    # We diff today's state against the prior series' tail to surface per-value
    # transitions ("M just sold out", "Black back in stock"); the appended series
    # also encodes how long the current state has held, for "low for 5d" wording.
    # A value's first-ever sighting has no prior state, so it emits no transition
    # — matching how prev_in_stock defaults True to avoid cold-start false alarms.
    extracted_variants: dict = extracted.get("variants") or {}
    prev_variant_history: dict = history.get("variant_history") or {}
    new_variant_history, variant_changes = _track_variants(
        extracted_variants, prev_variant_history, today, rules.variant_retention_days
    )

    updated_entry = {
        "label": extracted.get("label") or history.get("label"),
        "current_price": extracted.get("current_price"),
        "original_price": extracted.get("original_price"),
        "currency": extracted.get("currency"),
        "in_stock": now_in_stock,
        "low_stock": now_low_stock,
        # Size-aware OOS note: populated when the page has a Size option, the
        # user configured PREFERRED_SIZES, and none of those sizes is in stock
        # but other sizes still are. Empty otherwise. The digest reads this to
        # render "still available in S, XL" alongside the OOS line.
        "unpreferred_available_sizes": list(
            extracted.get("unpreferred_available_sizes") or []
        ),
        # Full per-item size snapshot — lets the digest render "only in L" /
        # "in stock in M, L" notes when SOME (but not all) preferred sizes
        # are currently available. ``preferred_sizes_applied`` echoes the
        # per-URL preference (pants vs. tops) selected upstream so the digest
        # doesn't need to re-derive garment category from the URL.
        "size_options": list(extracted.get("size_options") or []),
        "available_sizes": list(extracted.get("available_sizes") or []),
        "preferred_sizes_applied": list(
            extracted.get("preferred_sizes_applied") or []
        ),
        # Per-variant availability snapshot + its in/low/out change-point history
        # (one series per offered size/colour). The snapshot drives the digest's
        # size/colour notes; the history drives durations and next-run diffs.
        "variants": extracted_variants,
        "variant_history": new_variant_history,
        "last_checked": now,
        "last_seen": now,
        # Change-point price history + first-seen, for real-drop vs standing-
        # discount classification across runs (see src/price_history.py).
        "first_seen": first_seen,
        "price_history": new_history,
        "consecutive_failures": 0,
        "last_error_kind": None,
    }

    return {
        "sale_signal": sale_signal,
        "stock_signal": stock_signal,
        "error_signal": None,
        "prior_price": prior_price,
        "baseline_price": baseline,
        "baseline_days": rules.baseline_days,
        # Per-variant transitions seen this run, grouped by dimension:
        # {"size": [{"value": "M", "from": "in", "to": "out"}, ...], "color": [...]}.
        "variant_changes": variant_changes,
        "last_known": None,
        "updated_entry": updated_entry,
    }


def _variant_state(value: str, available: set, low: set) -> str:
    """Today's in/low/out state for one variant value from the snapshot."""
    if value not in available:
        return "out"
    if value in low:
        return "low"
    return "in"


def _track_variants(
    extracted_variants: dict,
    prev_variant_history: dict,
    today: date,
    retention_days: int,
) -> tuple[dict, dict]:
    """Advance each variant value's change-point series and collect transitions.

    For every offered value of every tracked dimension we derive today's state,
    append it to that value's series (a no-op unless it changed), prune to the
    retention window, and record a transition when the value had a known prior
    state that differs from today's. Values no longer offered are dropped from
    the series — discontinued is not the same as sold out, so we don't fabricate
    an "out". When this run produced no per-variant data at all, the prior
    history is carried forward untouched rather than wiped, so a one-off parse
    miss can't erase the timeline (mirrors how price history carries forward when
    no price was read).

    Returns ``(new_variant_history, variant_changes)``.
    """
    if not extracted_variants:
        return prev_variant_history, {}

    new_variant_history: dict[str, dict[str, list]] = {}
    variant_changes: dict[str, list] = {}
    for dim, dim_data in extracted_variants.items():
        available = set(dim_data.get("available") or [])
        low = set(dim_data.get("low") or [])
        prev_dim = prev_variant_history.get(dim) or {}
        new_dim: dict[str, list] = {}
        changes: list[dict] = []
        for value in dim_data.get("options") or []:
            state = _variant_state(value, available, low)
            prior_series = prev_dim.get(value)
            prior_state = variant_history.current_state(prior_series)
            series = variant_history.append_observation(prior_series, state, today)
            new_dim[value] = variant_history.prune(series, today, retention_days)
            if prior_state is not None and prior_state != state:
                changes.append({"value": value, "from": prior_state, "to": state})
        if new_dim:
            new_variant_history[dim] = new_dim
        if changes:
            variant_changes[dim] = changes
    return new_variant_history, variant_changes


def _classify_page_sale(
    cur_price: float | None,
    baseline: float | None,
    prior_price: float | None,
    enough_history: bool,
    drop_margin_pct: float,
) -> str:
    """Decide whether a page-advertised markdown is real or a standing discount.

    Genuine ("on_sale_per_page") when EITHER the price actually fell since the
    last check (``prior_price`` set — an observed move always counts), OR it sits
    a margin below its own trailing-max baseline. A standing discount is the
    leftover case: the page shows a markdown but the price is parked at its
    observed high, i.e. it's never really been cheaper — the year-round anchor.
    Until we have enough history (or lack a price/baseline) we can't judge, so we
    trust the page exactly as the pre-history detector did.
    """
    if prior_price is not None:
        return "on_sale_per_page"
    if not enough_history or baseline is None or cur_price is None:
        return "on_sale_per_page"
    threshold = baseline * (1.0 - drop_margin_pct / 100.0)
    if cur_price < threshold:
        return "on_sale_per_page"
    return "standing_discount"
