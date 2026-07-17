"""Build the markdown digest from sale-detection results.

Input shape (built by main.py from upstream Phase 4/6 outputs):

    {
        "items": [
            {
                "url": str,
                "shop": str | None,
                "is_uncertain": bool,      # True for loose-mention (Step 6) results
                "priority": bool,          # user flagged the URL with an inline
                    # priority marker (⭐) — pinned to the top "⭐ Watching now"
                    # block with full status, and suppressed from the per-URL
                    # change sections (still shown in the roster). Absent ⇒ False.
                "result": <detect_sale output>,
            },
            ...
        ],
        "shop_sales": [
            {"shop": str, "status": "yes" | "no" | "unclear", "description": str | None},
            ...
        ],
        "non_clothing_shops": [str, ...],  # shop labels from the watchlist's
            # "Non-clothing Shops and URLs:" section. Items + shop_sales whose
            # shop is in this set break out into a "# Non-clothing" block at the
            # bottom of the digest (full mirror of the homepage-driven sections).
            # Empty/absent ⇒ everything renders as clothing (back-compat).
        "untracked_items": [  # product URLs we can't crawl (Amazon — bot wall);
            {"url": str, "shop": str | None, "is_clothing": bool},  # listed
            ...                # read-only in an "Amazon (price not tracked)" block
        ],
        "email_sales": [  # active persisted email sale announcements (src/email_sales.py)
            {"shop": str, "description": str | None,
             "starts_on": str | None, "ends_on": str | None, ...},
            ...
        ],
        "email_unclear": [  # this run's "unclear" email judgements (one-shot, not persisted)
            {"shop": str, "description": str | None, ...},
            ...
        ],
        "today": datetime.date | None,   # anchor for countdowns (defaults to utcnow)
        "codes": [{"shop": str, "code": str, "context": str}, ...],
        "unresolved_shops": [str, ...],
        "review_requests": [  # deduped per-order review requests (src/review_requests.py)
            {"shop": str, "subject": str, "days_ago": int | None, "url": str | None},
            ...
        ],
        "review_requests_all_url": str | None,  # Gmail-search link to all-time review requests
        "review_requests_days": int | None,     # recent-window length (labels the intro)
    }

Output: the digest markdown string, with empty sections omitted. Currency is
rendered as-is (no FX conversion — that's Phase 5b).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from urllib.parse import urlparse

from src import email_sales, variant_history
from src.codes import _classify_confidence
from src.extract import _normalize_size
from src.fx import convert_to_usd

_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


def _resolve_confidence(c: dict) -> str:
    """Return the code's confidence, backfilling for legacy entries.

    Old ``codes.json`` rows written before the confidence-rating feature
    won't have the field; classify them on the fly so the digest still
    bucket them correctly without needing a one-shot migration of the Gist.
    """
    conf = c.get("confidence")
    if conf in _CONFIDENCE_RANK:
        return conf
    return _classify_confidence(c.get("code", ""))

_ERROR_KIND_EN: dict[str, str] = {
    "blocked": "blocked by site",
    "timeout": "timed out",
    "server_error": "server error",
    "rate_limited": "rate limited",
    "other": "fetch failed",
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_amount(price: float | int) -> str:
    if float(price).is_integer():
        return f"${int(price)}"
    return f"${price:.2f}"


def _fmt_price(
    price: float | int | None,
    currency: str | None = None,
    fx_rates: dict | None = None,
) -> str | None:
    """Render a price for display.

    Native USD → '$X'.
    Non-USD with no FX rates → '$X CCY' (backward-compatible fallback).
    Non-USD with FX rates → '$X USD [CCY $Y]' where X is converted, Y is native.
    Brackets (not parens) for the native wrapper so nested sale annotations
    like '$X USD [CAD $Y] (was $Z USD [CAD $W])' stay readable.
    """
    if price is None:
        return None
    if not currency or currency == "USD":
        return _fmt_amount(price)
    usd = convert_to_usd(price, currency, fx_rates)
    if usd is None:
        return f"{_fmt_amount(price)} {currency}"
    return f"{_fmt_amount(usd)} USD [{currency} {_fmt_amount(price)}]"


# ---------------------------------------------------------------------------
# State markers + savings — the visual-emphasis layer (issue: "make drops /
# restocks pop"). Each change-section line is prefixed with a leading marker
# emoji; the email HTML converter (email_send._BADGE_BY_MARKER) turns it into a
# colored badge pill + tinted callout card, while the plain-text copy reads the
# emoji as-is. The two mappings MUST stay in sync — a cross-check test guards it.
# ---------------------------------------------------------------------------

_MARK_DROP = "\U0001F53B"   # 🔻 price drop / on sale
_MARK_STOCK = "✅"      # ✅ back in stock
_MARK_LOW = "⚠️"  # ⚠️ low stock
_MARK_OOS = "⛔"        # ⛔ newly out of stock
_MARK_FLAT = "\U0001F3F7️"  # 🏷️ standing discount (marked down, no real drop)


def _save_pct(current: float | int | None, reference: float | int | None) -> int | None:
    """Whole-percent savings of ``current`` off ``reference`` (e.g. 44), or None.

    None when either value is missing/unparseable, the reference is ≤ 0, or the
    current price isn't actually below the reference (no saving to advertise).
    """
    try:
        cur = float(current)
        ref = float(reference)
    except (TypeError, ValueError):
        return None
    if ref <= 0 or cur >= ref:
        return None
    return round((1 - cur / ref) * 100)


def _save_suffix(current: float | int | None, reference: float | int | None) -> str:
    """`` — save 44%`` appended after a "was $X" phrase; "" when there's no drop."""
    pct = _save_pct(current, reference)
    return f" — save {pct}%" if pct else ""


def _label(item: dict) -> str:
    r = item["result"]
    updated = r.get("updated_entry") or {}
    if updated.get("label"):
        return updated["label"]
    last = r.get("last_known") or {}
    if last.get("label"):
        return last["label"]
    path = urlparse(item["url"]).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1] if path else ""
    return slug or item["url"]


def _link(item: dict) -> str:
    return f"[link]({item['url']})"


def _shop_suffix(item: dict) -> str:
    shop = item.get("shop")
    return f" ({shop})" if shop else ""


def _size_note(item: dict) -> str | None:
    """Short note about size availability for the digest line.

    Three branches:
    1. ``unpreferred_available_sizes`` populated (preferred sizes are ALL
       out of stock but other sizes still are) → ``still available in S, XL``.
       This is the OOS-override case from extract.parse.
    2. Item has preferred-size data and at least one preferred size is in
       stock → ``only in L`` (singular) or ``in stock in M, L`` (plural).
       The note fires even when EVERY preferred size is in stock — the user
       opted into a full size matrix on each tracked item.
    3. None — the item has no Size option, or its sizes don't overlap the
       user's preferred shortlist (e.g. ring sizes 7-11 against a M/L/XL
       preference). No size info is meaningful in that case.
    """
    updated = (item.get("result") or {}).get("updated_entry") or {}
    unpreferred = updated.get("unpreferred_available_sizes") or []
    if unpreferred:
        return f"still available in {', '.join(unpreferred)}"

    preferred = updated.get("preferred_sizes_applied") or []
    available = updated.get("available_sizes") or []
    offered = updated.get("size_options") or []
    if not (preferred and offered and available):
        return None

    preferred_norm = {_normalize_size(s) for s in preferred}
    offered_norm = {_normalize_size(s) for s in offered}
    applicable = preferred_norm & offered_norm
    if not applicable:
        return None  # preferred filter not applicable (e.g. ring sizes)

    # Available labels filtered to those matching a preferred size, in the
    # shop's variant order. Preserves the shop's casing ("M" vs "Medium") so
    # the note reads exactly the way the product page does.
    in_stock = [s for s in available if _normalize_size(s) in applicable]
    if not in_stock:
        return None  # all preferred OOS — handled by the unpreferred branch

    base = (f"only in {in_stock[0]}" if len(in_stock) == 1
            else f"in stock in {', '.join(in_stock)}")
    # Flag any shown size that's down to its last few, with how long it's been
    # low ("in stock in M, L (L low 5d)") — the per-size early-warning.
    return base + _low_suffix(updated, "size", in_stock, normalize=True)


# ---------------------------------------------------------------------------
# Per-variant (size + colour) availability, low-stock, and transition notes
# ---------------------------------------------------------------------------

def _iso_to_date(value: object) -> date | None:
    """Parse an ISO timestamp/date string to a ``date``; ``None`` if unparseable."""
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _variant_age(updated: dict, dim: str, value: str) -> int | None:
    """How long ``value`` has held its current in/low/out state, in days.

    Anchored to the entry's own ``last_checked`` stamp (this run's date) so the
    duration is correct in production and deterministic in tests without
    threading a clock through every renderer. ``None`` when there's no history.
    """
    series = ((updated.get("variant_history") or {}).get(dim) or {}).get(value)
    if not series:
        return None
    anchor = _iso_to_date(updated.get("last_checked")) or datetime.now(timezone.utc).date()
    return variant_history.days_in_state(series, anchor)


def _age_suffix(updated: dict, dim: str, value: str) -> str:
    """`` 5d`` duration suffix for a value's current state; empty if same-day/unknown."""
    days = _variant_age(updated, dim, value)
    return f" {days}d" if days and days > 0 else ""


def _low_suffix(updated: dict, dim: str, shown: list[str], *, normalize: bool = False) -> str:
    """`` (L low 5d)`` marker for any shown value that's currently low stock.

    ``normalize`` canonicalises labels before matching (sizes: 'Medium' vs 'M');
    colours match exactly. Empty string when nothing shown is low.
    """
    low_vals = ((updated.get("variants") or {}).get(dim) or {}).get("low") or []
    if normalize:
        low_keys = {_normalize_size(s) for s in low_vals}
        low_shown = [s for s in shown if _normalize_size(s) in low_keys]
    else:
        low_set = set(low_vals)
        low_shown = [s for s in shown if s in low_set]
    if not low_shown:
        return ""
    marks = [f"{s} low{_age_suffix(updated, dim, s)}" for s in low_shown]
    return f" ({', '.join(marks)})"


# Per-value state transitions, mapped to digest wording. Keyed by (from, to).
_VARIANT_PHRASES = {
    ("in", "out"): "sold out",
    ("low", "out"): "sold out",
    ("out", "in"): "back in stock",
    ("low", "in"): "restocked",
    ("in", "low"): "now low",
    ("out", "low"): "now low",
}
_PHRASE_ORDER = ["sold out", "now low", "back in stock", "restocked"]


def _variant_transitions_note(item: dict) -> str | None:
    """This-run per-value transitions across both dimensions, grouped by phrase.

    e.g. ``"M, XL sold out; Black back in stock"``. Sizes and colours share one
    note (their value names are distinct), so a single clause carries the day's
    per-variant news. ``None`` when nothing transitioned.
    """
    changes = (item.get("result") or {}).get("variant_changes") or {}
    groups: dict[str, list[str]] = {}
    for dim_changes in changes.values():
        for c in dim_changes:
            phrase = _VARIANT_PHRASES.get((c.get("from"), c.get("to")))
            if phrase:
                groups.setdefault(phrase, []).append(c.get("value"))
    if not groups:
        return None
    ordered = _PHRASE_ORDER + [p for p in groups if p not in _PHRASE_ORDER]
    return "; ".join(f"{', '.join(groups[p])} {p}" for p in ordered if p in groups)


def _color_note(item: dict) -> str | None:
    """Colour availability matrix — only when a colour is constrained.

    Returns ``"colors: Black, Olive (Red sold out)"`` when at least one colour is
    sold out or low (with how long), else ``None`` so a fully-available product
    adds no clutter. Sizes are covered by ``_size_note``; this is the colour twin.
    """
    updated = (item.get("result") or {}).get("updated_entry") or {}
    color = (updated.get("variants") or {}).get("color") or {}
    options = color.get("options") or []
    available = color.get("available") or []
    if not options or not available:
        return None  # no colour dim, or all colours out (product-level OOS covers it)
    # Colours that flipped this run are already announced by the transition note;
    # the matrix carries only the ongoing (unchanged-today) constraints so a
    # fresh "Red sold out" isn't printed twice on the same line.
    changed = {c.get("value") for c in
               ((item.get("result") or {}).get("variant_changes") or {}).get("color", [])}
    low = set(color.get("low") or []) - changed
    out = [o for o in options if o not in available and o not in changed]
    if not out and not low:
        return None  # every colour fully in stock (or only fresh changes) — nothing to flag
    flags: list[str] = []
    low_avail = [o for o in available if o in low]
    if low_avail:
        flags.append(", ".join(f"{o} low{_age_suffix(updated, 'color', o)}" for o in low_avail))
    if out:
        flags.append(", ".join(f"{o} sold out{_age_suffix(updated, 'color', o)}" for o in out))
    return f"colors: {', '.join(available)} ({'; '.join(flags)})"


def _variant_extra_pieces(item: dict) -> list[str]:
    """Transition news + colour matrix, appended after the size note on a line."""
    pieces: list[str] = []
    transitions = _variant_transitions_note(item)
    if transitions:
        pieces.append(transitions)
    color = _color_note(item)
    if color:
        pieces.append(color)
    return pieces


# ---------------------------------------------------------------------------
# Primary-bucket assignment + suppression
# ---------------------------------------------------------------------------

def _primary_bucket(item: dict) -> str:
    """Each item lands in exactly one section. Cross-info is annotated in the
    line itself (e.g. an on-sale item that's also newly OOS goes to
    newly_oos with the sale facts noted)."""
    r = item["result"]
    if r.get("error_signal") == "removed_from_shop":
        return "removed_from_shop"
    if r.get("error_signal") == "could_not_check":
        return "could_not_check"
    if r.get("stock_signal") == "newly_out_of_stock":
        return "newly_oos"
    sale_signal = r.get("sale_signal")
    if sale_signal in ("on_sale_per_page", "price_dropped", "standing_discount"):
        if item.get("is_uncertain"):
            # Loose matches stay one low-confidence "verify link" bucket — we
            # don't split standing discounts out of unverified mentions.
            return "uncertain"
        return "standing_discount" if sale_signal == "standing_discount" else "on_sale"
    if r.get("stock_signal") == "back_in_stock":
        return "back_in_stock"
    if r.get("stock_signal") == "newly_low_stock":
        return "now_low_stock"
    updated = r.get("updated_entry") or {}
    if not updated.get("in_stock", True):
        return "still_oos"
    return "unchanged"


def _suppress_could_not_check(item: dict) -> bool:
    """Suppression policy (confirmed with user 2026-05-17):
    - never successfully checked → suppress (first-run Cloudflare noise)
    - 1st failure for transient kinds (blocked, timeout) → suppress (dampens blips)
    - 1st failure for server_error / other → SHOW (likely real breakage)
    - 2nd+ consecutive failure (any kind) → SHOW
    """
    r = item["result"]
    last = r.get("last_known") or {}
    if not last.get("last_checked"):
        return True
    updated = r.get("updated_entry") or {}
    failures = updated.get("consecutive_failures", 1)
    kind = updated.get("last_error_kind")
    if failures == 1 and kind in ("blocked", "timeout"):
        return True
    return False


# ---------------------------------------------------------------------------
# Item line renderers (one per primary bucket)
# ---------------------------------------------------------------------------

def _on_sale_line(item: dict, fx_rates: dict | None = None, marker: str = "") -> str:
    r = item["result"]
    updated = r.get("updated_entry") or {}
    cur = updated.get("current_price")
    currency = updated.get("currency")
    orig = updated.get("original_price")
    price_part = _fmt_price(cur, currency, fx_rates) or ""
    if r.get("sale_signal") == "on_sale_per_page" and orig:
        price_part = (
            f"{price_part} (was {_fmt_price(orig, currency, fx_rates)} "
            f"listed{_save_suffix(cur, orig)})"
        )

    pieces = [f"**{_label(item)}**"]
    if price_part:
        pieces.append(price_part)
    if r.get("prior_price") is not None:
        pieces.append(f"down from {_fmt_price(r['prior_price'], currency, fx_rates)} last checked")
    if r.get("stock_signal") == "newly_low_stock":
        pieces.append("low stock")
    if r.get("stock_signal") == "back_in_stock":
        pieces.append("back in stock")
    size_note = _size_note(item)
    # The on-sale bucket sits ahead of still_oos in _primary_bucket, so a sale
    # on a sold-out item lands here — spell out the OOS status (folding in the
    # size note the way _roster_line does) so a dead sale doesn't read as buyable.
    if not updated.get("in_stock", True):
        oos = "out of stock"
        if size_note:
            oos = f"{oos} ({size_note})"
            size_note = None
        pieces.append(oos)
    if size_note:
        pieces.append(size_note)
    pieces.extend(_variant_extra_pieces(item))
    pieces.append(_link(item))
    return "- " + marker + " — ".join(pieces)


def _standing_discount_line(
    item: dict, fx_rates: dict | None = None, marker: str = "",
) -> str:
    """Render a year-round "always marked down" item.

    The page advertises a markdown, but our observed price history shows the
    price has never actually been higher than this across the baseline window —
    so the "was $X" is an anchor, not a real drop. We spell that out: current
    price, the page's claimed "was", and how long it's actually held its level.
    """
    r = item["result"]
    updated = r.get("updated_entry") or {}
    cur = updated.get("current_price")
    currency = updated.get("currency")
    orig = updated.get("original_price")
    baseline = r.get("baseline_price")
    days = r.get("baseline_days")

    pieces = [f"**{_label(item)}**"]
    price_part = _fmt_price(cur, currency, fx_rates)
    if price_part:
        pieces.append(price_part)

    usual = _fmt_price(baseline, currency, fx_rates) or price_part
    span = f"the last {days}d" if days else "the whole time we've tracked it"
    if orig:
        note = (
            f'no real drop: marked "was {_fmt_price(orig, currency, fx_rates)}" '
            f"but held ~{usual} over {span}"
        )
    else:
        note = f"no real drop: held ~{usual} over {span}"
    pieces.append(note)

    # Stock transitions can co-fire on a standing-discount item (it lands in this
    # bucket ahead of the back-in-stock / low-stock buckets, just as on-sale items
    # do) — surface them on the line like _on_sale_line does, so the signal isn't
    # lost to the bucket choice.
    if r.get("stock_signal") == "newly_low_stock":
        pieces.append("low stock")
    if r.get("stock_signal") == "back_in_stock":
        pieces.append("back in stock")
    size_note = _size_note(item)
    # A standing-discount item that's sold out lands here too (this bucket sits
    # ahead of still_oos) — surface the OOS status so it doesn't read as buyable.
    if not updated.get("in_stock", True):
        oos = "out of stock"
        if size_note:
            oos = f"{oos} ({size_note})"
            size_note = None
        pieces.append(oos)
    if size_note:
        pieces.append(size_note)
    pieces.extend(_variant_extra_pieces(item))
    pieces.append(_link(item))
    return "- " + marker + " — ".join(pieces)


def _newly_oos_line(item: dict, fx_rates: dict | None = None, marker: str = "") -> str:
    r = item["result"]
    updated = r.get("updated_entry") or {}
    cur = updated.get("current_price")
    currency = updated.get("currency")
    orig = updated.get("original_price")
    price_part = _fmt_price(cur, currency, fx_rates)
    if r.get("sale_signal") == "on_sale_per_page" and orig and price_part:
        price_part = f"was {price_part} on sale (was {_fmt_price(orig, currency, fx_rates)} listed)"
    elif price_part:
        price_part = f"was {price_part}"

    pieces = [f"**{_label(item)}**"]
    if price_part:
        pieces.append(price_part)
    if r.get("prior_price") is not None:
        pieces.append(f"down from {_fmt_price(r['prior_price'], currency, fx_rates)} last checked")
    size_note = _size_note(item)
    if size_note:
        pieces.append(size_note)
    pieces.extend(_variant_extra_pieces(item))
    pieces.append(_link(item))
    return "- " + marker + " — ".join(pieces)


def _back_in_stock_line(item: dict, fx_rates: dict | None = None, marker: str = "") -> str:
    updated = item["result"].get("updated_entry") or {}
    price = _fmt_price(updated.get("current_price"), updated.get("currency"), fx_rates)
    pieces = [f"**{_label(item)}**"]
    if price:
        pieces.append(price)
    size_note = _size_note(item)
    if size_note:
        pieces.append(size_note)
    pieces.extend(_variant_extra_pieces(item))
    pieces.append(_link(item))
    return "- " + marker + " — ".join(pieces)


def _now_low_stock_line(item: dict, fx_rates: dict | None = None, marker: str = "") -> str:
    updated = item["result"].get("updated_entry") or {}
    price = _fmt_price(updated.get("current_price"), updated.get("currency"), fx_rates)
    pieces = [f"**{_label(item)}**"]
    if price:
        pieces.append(price)
    size_note = _size_note(item)
    if size_note:
        pieces.append(size_note)
    pieces.extend(_variant_extra_pieces(item))
    pieces.append(_link(item))
    return "- " + marker + " — ".join(pieces)


def _could_not_check_line(item: dict, fx_rates: dict | None = None) -> str:
    r = item["result"]
    last = r.get("last_known") or {}
    updated = r.get("updated_entry") or {}
    kind = updated.get("last_error_kind")
    err_en = _ERROR_KIND_EN.get(kind or "", kind or "unknown error")

    head = f"**{_label(item)}**{_shop_suffix(item)}"
    pieces = [head]
    cur = last.get("current_price")
    if cur is not None:
        price_str = _fmt_price(cur, last.get("currency"), fx_rates)
        stock_note = "" if last.get("in_stock", True) else ", was out of stock"
        pieces.append(f"last seen {price_str}{stock_note}")
    pieces.append(err_en)
    pieces.append(_link(item))
    return "- " + " — ".join(pieces)


def _removed_line(item: dict, fx_rates: dict | None = None) -> str:
    last = item["result"].get("last_known") or {}
    head = f"**{_label(item)}**{_shop_suffix(item)}"
    pieces = [head]
    cur = last.get("current_price")
    if cur is not None:
        pieces.append(f"was {_fmt_price(cur, last.get('currency'), fx_rates)}")
    pieces.append(_link(item))
    return "- " + " — ".join(pieces)


def _still_oos_line(item: dict) -> str:
    pieces = [f"**{_label(item)}**"]
    size_note = _size_note(item)
    if size_note:
        pieces.append(size_note)
    pieces.extend(_variant_extra_pieces(item))
    pieces.append(_link(item))
    return "- " + " — ".join(pieces)


def _roster_line(item: dict, fx_rates: dict | None = None) -> str:
    """One line for the 'All items by shop' roster. Includes inline tags for
    sale, stock, and error state. Always shows could-not-check items (even
    when suppressed from the main section) so the roster reflects every URL."""
    r = item["result"]
    label = _label(item)

    if r.get("error_signal") == "removed_from_shop":
        last = r.get("last_known") or {}
        pieces = [f"**{label}**"]
        cur = last.get("current_price")
        if cur is not None:
            pieces.append(f"was {_fmt_price(cur, last.get('currency'), fx_rates)}")
        pieces.append("removed from shop")
        pieces.append(_link(item))
        return "- " + " — ".join(pieces)

    if r.get("error_signal") == "could_not_check":
        last = r.get("last_known") or {}
        updated = r.get("updated_entry") or {}
        kind = updated.get("last_error_kind") or "other"
        err_en = _ERROR_KIND_EN.get(kind, kind)
        pieces = [f"**{label}**"]
        cur = last.get("current_price")
        if cur is not None:
            pieces.append(f"last seen {_fmt_price(cur, last.get('currency'), fx_rates)}")
        pieces.append(err_en)
        pieces.append(_link(item))
        return "- " + " — ".join(pieces)

    # Success path
    updated = r.get("updated_entry") or {}
    currency = updated.get("currency")
    price = _fmt_price(updated.get("current_price"), currency, fx_rates)
    orig = updated.get("original_price")

    sale_tag = ""
    sale_parts: list[str] = []
    if r.get("sale_signal") == "on_sale_per_page" and orig:
        sale_parts.append(f"on sale, was {_fmt_price(orig, currency, fx_rates)}")
    if r.get("prior_price") is not None:
        sale_parts.append(f"down from {_fmt_price(r['prior_price'], currency, fx_rates)}")
    if sale_parts:
        sale_tag = f" ({'; '.join(sale_parts)})"

    stock = r.get("stock_signal")
    if stock == "newly_out_of_stock":
        state_tag: str | None = "newly out of stock"
    elif stock == "back_in_stock":
        state_tag = "back in stock"
    elif stock == "newly_low_stock":
        state_tag = "low stock"
    elif not updated.get("in_stock", True):
        state_tag = "out of stock"
    elif updated.get("low_stock"):
        state_tag = "low stock"
    else:
        state_tag = None

    # Append size note inline with the OOS tag so the roster line reads
    # "out of stock (still available in S, XL)" instead of bolting on a
    # separate dashed segment. For in-stock items, the partial-stock note
    # ("only in L") rides as its own segment so it stands out in the roster.
    size_note = _size_note(item)
    if state_tag in ("newly out of stock", "out of stock") and size_note:
        state_tag = f"{state_tag} ({size_note})"
        size_note = None

    pieces = [f"**{label}**"]
    if price:
        pieces.append(f"{price}{sale_tag}")
    if state_tag:
        pieces.append(state_tag)
    if size_note:
        pieces.append(size_note)
    pieces.extend(_variant_extra_pieces(item))
    pieces.append(_link(item))
    return "- " + " — ".join(pieces)


def _priority_line(item: dict, fx_rates: dict | None = None) -> str:
    """One line for the top "Watching now" block.

    Unlike the per-state sections (which fire only on a *change*), a priority
    item is shown every day with its **full current status spelled out** —
    price, an explicit sale verdict, and an explicit stock verdict — so the user
    can glance at the items they're actively watching without hunting the roster.
    Error/removed paths mirror the roster's wording.
    """
    r = item["result"]
    head = f"**{_label(item)}**{_shop_suffix(item)}"
    pieces = [head]

    if r.get("error_signal") == "removed_from_shop":
        last = r.get("last_known") or {}
        cur = last.get("current_price")
        if cur is not None:
            pieces.append(f"was {_fmt_price(cur, last.get('currency'), fx_rates)}")
        pieces.append("removed from shop")
        pieces.append(_link(item))
        return "- " + " — ".join(pieces)

    if r.get("error_signal") == "could_not_check":
        last = r.get("last_known") or {}
        updated = r.get("updated_entry") or {}
        kind = updated.get("last_error_kind") or "other"
        err_en = _ERROR_KIND_EN.get(kind, kind)
        cur = last.get("current_price")
        if cur is not None:
            stock_note = "" if last.get("in_stock", True) else ", was out of stock"
            pieces.append(
                f"last seen {_fmt_price(cur, last.get('currency'), fx_rates)}{stock_note}"
            )
        pieces.append(f"couldn't check ({err_en})")
        pieces.append(_link(item))
        return "- " + " — ".join(pieces)

    updated = r.get("updated_entry") or {}
    currency = updated.get("currency")
    cur = updated.get("current_price")
    orig = updated.get("original_price")
    price_part = _fmt_price(cur, currency, fx_rates)
    if price_part:
        pieces.append(price_part)

    # Sale verdict — always explicit (the point of a watch block).
    sale_signal = r.get("sale_signal")
    if sale_signal == "on_sale_per_page":
        pieces.append(
            f"on sale, was {_fmt_price(orig, currency, fx_rates)}{_save_suffix(cur, orig)}"
            if orig else "on sale"
        )
    elif sale_signal == "standing_discount":
        pieces.append("marked down (no real drop)")
    elif sale_signal != "price_dropped":
        # price_dropped is conveyed by the "down from …" piece below.
        pieces.append("not on sale")
    if r.get("prior_price") is not None:
        pieces.append(
            f"down from {_fmt_price(r['prior_price'], currency, fx_rates)} last checked"
        )

    # Stock verdict — always explicit, with the change flavour when there is one.
    stock = r.get("stock_signal")
    if stock == "newly_out_of_stock":
        stock_tag = "newly out of stock"
    elif stock == "back_in_stock":
        stock_tag = "back in stock"
    elif stock == "newly_low_stock":
        stock_tag = "low stock"
    elif not updated.get("in_stock", True):
        stock_tag = "out of stock"
    elif updated.get("low_stock"):
        stock_tag = "low stock"
    else:
        stock_tag = "in stock"

    size_note = _size_note(item)
    if stock_tag in ("newly out of stock", "out of stock") and size_note:
        stock_tag = f"{stock_tag} ({size_note})"
        size_note = None
    pieces.append(stock_tag)
    if size_note:
        pieces.append(size_note)
    pieces.extend(_variant_extra_pieces(item))
    pieces.append(_link(item))
    return "- " + _priority_marker(sale_signal, stock) + " — ".join(pieces)


def _priority_marker(sale_signal: str | None, stock_signal: str | None) -> str:
    """Leading state marker for a "Watching now" line, most-actionable first.

    A watch line spells out both a sale and a stock verdict; the badge shows the
    one worth reacting to — a price move outranks a stock move, which outranks a
    year-round markdown. An unchanged in-stock item gets no marker so it stays
    visually quiet next to the items that actually changed.
    """
    if sale_signal in ("on_sale_per_page", "price_dropped"):
        return f"{_MARK_DROP} "
    if sale_signal == "standing_discount":
        return f"{_MARK_FLAT} "
    if stock_signal == "back_in_stock":
        return f"{_MARK_STOCK} "
    if stock_signal == "newly_out_of_stock":
        return f"{_MARK_OOS} "
    if stock_signal == "newly_low_stock":
        return f"{_MARK_LOW} "
    return ""


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _priority_section(
    items: list[dict], fx_rates: dict | None = None,
) -> str | None:
    """The pinned top "Watching now" block: every user-flagged priority item,
    shown with its full current status. ``None`` when nothing is flagged so the
    section self-suppresses like every other. Sorted by shop then label for a
    stable order regardless of watchlist position."""
    if not items:
        return None
    ordered = sorted(
        items, key=lambda i: ((i.get("shop") or "").lower(), _label(i).lower())
    )
    lines = [_priority_line(i, fx_rates) for i in ordered]
    return _section("⭐ Watching now", lines)


# Path tokens that precede an Amazon ASIN (/dp/<ASIN>, /gp/product/<ASIN>, …).
_AMAZON_ASIN_MARKERS = frozenset({"dp", "gp", "product", "d", "aw"})


def _untracked_label(url: str) -> str:
    """Human-readable name for an un-crawlable (Amazon) URL.

    We never fetch the page, so the name comes from the URL's own path slug:
    Amazon product URLs look like ``/<Descriptive-Slug>/dp/<ASIN>`` (or a bare
    ``/dp/<ASIN>``). Use the descriptive slug when present, else fall back to the
    ASIN, else the host.
    """
    segs = [s for s in urlparse(url).path.split("/") if s]
    title_seg: str | None = None
    asin: str | None = None
    for i, seg in enumerate(segs):
        if seg.lower() in _AMAZON_ASIN_MARKERS:
            asin = next(
                (t for t in segs[i + 1:] if len(t) == 10 and t.isalnum()), None
            )
            break
        title_seg = seg
    if title_seg:
        name = " ".join(title_seg.replace("-", " ").replace("_", " ").split())
        if name:
            return name
    if asin:
        return f"Amazon item {asin}"
    return urlparse(url).netloc or url


def _untracked_section(
    items: list[dict], title: str = "Amazon (price not tracked)",
) -> str | None:
    """Read-only block for product URLs we can't crawl (Amazon's bot wall blocks
    a plain fetch). We have no price/stock to report, so just list each item —
    titled from its URL slug + clickable — so it's visible instead of silently
    dropped. ``None`` when empty so the section self-suppresses like the rest."""
    if not items:
        return None
    ordered = sorted(items, key=lambda i: _untracked_label(i["url"]).lower())
    lines = [
        f"- **{_untracked_label(i['url'])}** — [link]({i['url']})" for i in ordered
    ]
    return _section(title, lines)


def _section(header: str, body_lines: list[str]) -> str | None:
    if not body_lines:
        return None
    return f"## {header}\n" + "\n".join(body_lines)


def _shops_on_sale_section(
    shop_sales: list[dict], title: str = "Shops on sale",
) -> str | None:
    lines: list[str] = []
    for s in shop_sales:
        if s.get("status") != "yes":
            continue
        desc = s.get("description")
        if desc:
            lines.append(f"- **{s['shop']}**: {desc}")
        else:
            lines.append(f"- **{s['shop']}**")
    return _section(title, lines)


def _email_sale_countdown(entry: dict, today: date) -> str:
    """Render the countdown suffix for an email sale entry ("" when none).

    "starts in 3 days (Sat May 24)" for an upcoming sale, "ends in 2 days
    (Sun May 26)" for one already underway with a known end, and "" for an
    ongoing/undated sale we can't count toward.
    """
    phase, days, on = email_sales.relative_days(entry, today)
    if phase not in ("upcoming", "ending") or on is None or days is None:
        return ""
    verb = "starts" if phase == "upcoming" else "ends"
    if days == 0:
        rel = "today"
    elif days == 1:
        rel = "tomorrow"
    else:
        rel = f"in {days} days"
    return f"{verb} {rel} ({on.strftime('%a %b %d')})"


def _email_sales_section(
    active_sales: list[dict], today: date | None = None,
) -> str | None:
    """Sales announced by email/SMS, persisted until they end (src/email_sales.py).

    Rendered upcoming-first (the caller pre-sorts) so the user sees what's
    coming before what's already live. Each line carries the shop, the terse
    description, and a countdown when a date is known.
    """
    if not active_sales:
        return None
    today = today or datetime.now(timezone.utc).date()
    lines: list[str] = []
    for e in active_sales:
        shop = (e.get("shop") or "(unknown shop)").strip()
        desc = (e.get("description") or "").strip()
        countdown = _email_sale_countdown(e, today)
        head = f"**{shop}**: {desc}" if desc else f"**{shop}**"
        lines.append(f"- {head} — {countdown}" if countdown else f"- {head}")
    return _section("Sales announced by email", lines)


def _possible_email_sales_section(unclear_sales: list[dict]) -> str | None:
    """Email/SMS signals Claude judged "unclear" — a possible-but-ambiguous sale.

    These are low-signal and one-shot (not persisted like the "yes" sales), so
    they surface only the day the email lands. Kept in their own section rather
    than mixed into the homepage "Sale status unclear" list so the user can tell
    an ambiguous *email* apart from a homepage check that came back unclear.
    Deduped by shop (first occurrence wins).
    """
    seen: set[str] = set()
    lines: list[str] = []
    for e in unclear_sales or []:
        shop = (e.get("shop") or "").strip()
        if not shop:
            continue
        key = shop.lower()
        if key in seen:
            continue
        seen.add(key)
        desc = (e.get("description") or "").strip()
        lines.append(f"- **{shop}**: {desc}" if desc else f"- **{shop}**")
    return _section("Possible sales (unclear)", lines)


def _untracked_sms_section(senders: list[dict]) -> str | None:
    """Brands that texted a deal to the user's Google Voice number but aren't on
    the watchlist or the sale-tracking allowlist — so their sale never reaches
    the digest. Surfaced (aggregated by brand, busiest first, with one example
    text) so the user can add the ones worth tracking to the Doc's "Shops to
    track sales for:" section.

    Input is one entry per un-attributed marketing SMS seen *this run*
    (incremental, since voice dedups against processed_ids), so the list stays
    short. Self-suppresses when empty.
    """
    agg: dict[str, dict] = {}
    for s in senders or []:
        brand = (s.get("brand") or "").strip()
        if not brand:
            continue
        key = brand.lower()
        entry = agg.setdefault(
            key, {"brand": brand, "count": 0, "excerpt": ""},
        )
        entry["count"] += 1
        if not entry["excerpt"] and (s.get("excerpt") or "").strip():
            entry["excerpt"] = s["excerpt"].strip()
    if not agg:
        return None
    ordered = sorted(agg.values(), key=lambda e: (-e["count"], e["brand"].lower()))
    lines = [
        "_Texted a deal but not tracked — add to the watchlist Doc's "
        '"Shops to track sales for:" section to surface their sales:_',
    ]
    for e in ordered:
        n = e["count"]
        plural = "" if n == 1 else "s"
        eg = f' — e.g. "{e["excerpt"]}"' if e["excerpt"] else ""
        lines.append(f"- **{e['brand']}** ({n} text{plural}){eg}")
    return _section("Untracked SMS senders", lines)


def _shops_unclear_section(
    shop_sales: list[dict], title: str = "Sale status unclear",
) -> str | None:
    names = [s["shop"] for s in shop_sales if s.get("status") == "unclear"]
    if not names:
        return None
    return f"## {title}\n{', '.join(names)}"


def _shops_no_sale_section(
    shop_sales: list[dict], title: str = "Shops with no sale",
) -> str | None:
    names = [s["shop"] for s in shop_sales if s.get("status") == "no"]
    if not names:
        return None
    return f"## {title}\n{', '.join(sorted(names, key=str.lower))}"


def _codes_section(codes: list[dict]) -> str | None:
    """Saved promo codes — attributed codes only (watchlist + email-attributed).

    Email-unattributed codes (source="email_unattributed") render in
    _unattributed_codes_section so the user can see which unknown senders
    are sending discounts that might be worth adding to the watchlist.

    Codes are grouped by confidence so likely-marketing words don't pollute
    the front of the section, but they're still shown — a real code that
    happens to look like a shouted English word would otherwise be dropped.
    """
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for c in codes:
        if c.get("source") == "email_unattributed":
            continue
        key = (c.get("shop", ""), c.get("code", ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return _grouped_codes_section("Saved promo codes", unique)


def _unattributed_codes_section(codes: list[dict]) -> str | None:
    """Promo codes from emails whose sender we couldn't map to a watchlist shop.

    Same confidence grouping as the attributed section. Low-confidence rows
    (marketing shout-words like SITEWIDE / CLEARANCE / DISCOUNT, HTML doctype
    artifacts) drop to a sub-section so the user can ignore them at a glance
    instead of trying every fake code at checkout.
    """
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for c in codes:
        if c.get("source") != "email_unattributed":
            continue
        key = (c.get("shop", ""), c.get("code", ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return _grouped_codes_section("Unattributed promo codes", unique)


_CONFIDENCE_HEADER = {
    "high": None,  # No sub-header — these are the codes worth reading first
    "medium": "**Uncertain** — code-shaped but not the canonical digit+letter form, may or may not be real",
    "low": (
        "**Low confidence** — likely marketing words or HTML artifacts, "
        "not real codes"
    ),
}


def _grouped_codes_section(title: str, unique: list[dict]) -> str | None:
    if not unique:
        return None
    buckets: dict[str, list[dict]] = {"high": [], "medium": [], "low": []}
    for c in unique:
        buckets.setdefault(_resolve_confidence(c), buckets["medium"]).append(c)
    for tier in buckets.values():
        tier.sort(key=lambda c: (c.get("shop", "").lower(), c.get("code", "")))

    lines: list[str] = []
    for tier in ("high", "medium", "low"):
        rows = buckets[tier]
        if not rows:
            continue
        header = _CONFIDENCE_HEADER[tier]
        if header:
            if lines:
                lines.append("")  # blank line between tiers
            lines.append(header)
        for c in rows:
            shop = c.get("shop") or "(unknown shop)"
            lines.append(f"- **{shop}**: {c['code']}")
    return _section(title, lines)


def _unresolved_section(names: list[str]) -> str | None:
    if not names:
        return None
    lines = [f"- {n}" for n in sorted(set(names), key=str.lower)]
    return _section("Could not resolve", lines)


def _fit_pending_line(p: dict) -> str:
    """One bullet for a wardrobe item awaiting a fit review.

    ``p`` is the render dict main.py builds (name/shop/size/color + a signed
    ``url``), not a raw wardrobe item — digest.py stays free of the HMAC/secret
    logic, which lives in ``src/fit_links.py``.
    """
    pieces = [f"**{p.get('name') or '(unnamed)'}**"]
    shop = p.get("shop")
    if shop:
        pieces[0] += f" ({shop})"
    size = p.get("size")
    color = p.get("color")
    detail = ", ".join(x for x in (f"size {size}" if size else None, color) if x)
    if detail:
        pieces.append(detail)
    pieces.append(f"[leave fit feedback]({p['url']})")
    return "- " + " — ".join(pieces)


# Daily digest shows at most this many fit links (newest first); the rest are
# reachable via the "Review all" link. The weekly fit email stays uncapped so
# nothing falls off the radar. Keeps the daily email readable while a large
# pending backlog drains through the form.
_DAILY_FIT_PENDING_CAP = 15


def _fit_feedback_section(
    pending: list[dict], review_all_url: str | None = None,
    total: int | None = None,
) -> str | None:
    """The "Fit feedback wanted" section: one signed link per pending item.

    Returns ``None`` when nothing is pending so the section self-suppresses
    (same omit-empty-sections contract as every other section). Each entry in
    ``pending`` already carries a signed ``url`` (see ``main._fit_feedback_data``).

    ``total`` is the count *before* any truncation by the caller. When it
    exceeds ``len(pending)`` the intro says so and leans on the "Review all"
    link for the remainder; when omitted (or equal) the intro is the plain
    count, so uncapped callers (e.g. the weekly email) read unchanged.
    """
    if not pending:
        return None
    shown = len(pending)
    total = shown if total is None else total
    if total > shown:
        intro = (f"{shown} of {total} items waiting for a fit review "
                 f"(newest shown).")
    else:
        intro = f"{total} item(s) waiting for a quick fit review."
    if review_all_url:
        intro += f" [Review all]({review_all_url})"
    lines = [intro, ""]
    lines.extend(_fit_pending_line(p) for p in pending)
    return _section("Fit feedback wanted", lines)


def _removal_pending_line(p: dict) -> str:
    """One bullet for a purchased item still listed on the watchlist Doc.

    ``p`` is the render dict main.py builds (name/shop/size/color + the exact
    Doc ``matched_line`` + a signed ``url``), not a raw wardrobe item — digest.py
    stays free of the HMAC/secret logic, which lives in ``src/watchlist_links.py``.
    Shows *how the item is listed in the Doc* so the user can sanity-check the
    match before approving the delete.
    """
    pieces = [f"**{p.get('name') or '(unnamed)'}**"]
    shop = p.get("shop")
    if shop:
        pieces[0] += f" ({shop})"
    size = p.get("size")
    color = p.get("color")
    detail = ", ".join(x for x in (f"size {size}" if size else None, color) if x)
    if detail:
        pieces.append(detail)
    line = (p.get("matched_line") or "").strip()
    if line:
        pieces.append(f"listed as `{line}`")
    pieces.append(f"[approve removal]({p['url']})")
    return "- " + " — ".join(pieces)


# Daily digest shows at most this many removal candidates (newest first); the
# rest are reachable via the "Review all" link. Mirrors _DAILY_FIT_PENDING_CAP.
_DAILY_REMOVAL_CAP = 15


def _removal_section(
    pending: list[dict], review_all_url: str | None = None,
    total: int | None = None,
) -> str | None:
    """The "Bought — remove from watchlist?" section: one signed approve link per
    purchased item still sitting on the watchlist Doc.

    Returns ``None`` when nothing is pending so the section self-suppresses (same
    omit-empty-sections contract as every other section). Each entry in
    ``pending`` already carries a signed ``url`` (see
    ``main._watchlist_removal_data``). ``total`` is the count *before* any
    truncation by the caller; when it exceeds ``len(pending)`` the intro says so
    and leans on the "Review all" link for the remainder.
    """
    if not pending:
        return None
    shown = len(pending)
    total = shown if total is None else total
    if total > shown:
        intro = (f"{shown} of {total} purchased items still on your watchlist Doc "
                 f"(newest shown) — approve to remove each line.")
    else:
        intro = (f"{total} purchased item(s) still on your watchlist Doc — "
                 f"approve to remove the line.")
    if review_all_url:
        intro += f" [Review all]({review_all_url})"
    lines = [intro, ""]
    lines.extend(_removal_pending_line(p) for p in pending)
    return _section("Bought — remove from watchlist?", lines)


def _review_age(days_ago: int | None) -> str:
    """Human-readable age for a review-request line ("today"/"yesterday"/"N days
    ago"). "" when the email's date couldn't be parsed."""
    if days_ago is None:
        return ""
    if days_ago <= 0:
        return "today"
    if days_ago == 1:
        return "yesterday"
    return f"{days_ago} days ago"


def _review_request_line(r: dict) -> str:
    """One bullet for a deduped review request.

    ``r`` is a render dict from ``review_requests.dedupe`` (shop / subject /
    days_ago / url) — digest.py stays free of the Gmail/dedup logic.
    """
    pieces = [f"**{r.get('shop') or '(unknown shop)'}**"]
    subject = (r.get("subject") or "").strip()
    if subject:
        pieces.append(subject)
    age = _review_age(r.get("days_ago"))
    if age:
        pieces.append(age)
    url = r.get("url")
    if url:
        pieces.append(f"[open]({url})")
    return "- " + " — ".join(pieces)


def _email_restock_line(r: dict) -> str:
    """One bullet for an email-sourced back-in-stock alert.

    ``r`` is a render dict from ``restock_emails.dedupe`` (shop / item / size /
    subject / days_ago / url). Tagged ``_(email alert)_`` so it's clearly
    distinguished from the scrape-driven restock lines it sits beside in the
    "Back in stock" section.
    """
    label = (r.get("item") or r.get("subject") or "item").strip()
    pieces = [f"**{r.get('shop') or '(unknown shop)'}**", label]
    size = r.get("size")
    if size:
        pieces.append(f"size {size}")
    pieces.append("back in stock _(email alert)_")
    age = _review_age(r.get("days_ago"))
    if age:
        pieces.append(age)
    url = r.get("url")
    if url:
        pieces.append(f"[open email]({url})")
    return "- " + f"{_MARK_STOCK} " + " — ".join(pieces)


def _email_restock_lines(
    email_restocks: list[dict], all_url: str | None = None,
) -> list[str]:
    """Render the email-sourced restock bullets, with an optional trailing
    "see all" Gmail-search link. ``[]`` when there are none, so the merge into
    the "Back in stock" section adds nothing."""
    if not email_restocks:
        return []
    lines = [_email_restock_line(r) for r in email_restocks]
    if all_url:
        lines.append(f"- _[See all back-in-stock emails]({all_url})_")
    return lines


def _review_requests_section(
    requests: list[dict],
    all_url: str | None = None,
    days: int | None = None,
) -> str | None:
    """The "Review requests" section: one deduped (per-order) review-request
    email per line, newest first, each linking straight to the email.

    Returns ``None`` when nothing matched so the section self-suppresses (same
    omit-empty contract as every other section). ``all_url`` is the always-shown
    Gmail-search link to *every* review request ever; ``days`` labels the recent
    window in the intro.
    """
    if not requests:
        return None
    n = len(requests)
    window = f"last {days} days" if days else "recent"
    intro = (f"{n} review request{'s' if n != 1 else ''} "
             f"({window}, one per order).")
    if all_url:
        intro += f" [See all review requests]({all_url})"
    lines = [intro, ""]
    lines.extend(_review_request_line(r) for r in requests)
    return _section("Review requests", lines)


def _all_items_section(
    items: list[dict], fx_rates: dict | None = None,
    title: str = "All items by shop",
) -> str | None:
    if not items:
        return None
    by_shop: dict[str, list[dict]] = {}
    for item in items:
        shop = item.get("shop") or "(unknown shop)"
        by_shop.setdefault(shop, []).append(item)
    blocks = [f"## {title}"]
    for shop in sorted(by_shop.keys(), key=str.lower):
        in_shop = sorted(by_shop[shop], key=lambda i: _label(i).lower())
        block = f"### {shop}\n" + "\n".join(_roster_line(i, fx_rates) for i in in_shop)
        blocks.append(block)
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Per-item bucket rendering (shared by the clothing + non-clothing passes)
# ---------------------------------------------------------------------------

def _render_item_buckets(
    items: list[dict], fx_rates: dict | None = None,
) -> dict[str, list[str]]:
    """Bucket ``items`` by primary state and render each bucket's digest lines.

    Returns ``{bucket_name: [rendered_line, ...]}``. The ``unchanged`` bucket is
    intentionally omitted from the output — those items only surface in the
    "All items by shop" roster, never a dedicated section. Same suppression
    policy for first-time ``could_not_check`` blips as the main digest.

    Factored out so the clothing sections and the non-clothing block (which
    mirrors the same per-item sections) share one bucketing + rendering path.
    """
    buckets: dict[str, list[dict]] = {
        "on_sale": [],
        "standing_discount": [],
        "uncertain": [],
        "newly_oos": [],
        "back_in_stock": [],
        "now_low_stock": [],
        "could_not_check": [],
        "removed_from_shop": [],
        "unchanged": [],
        "still_oos": [],
    }
    for item in items:
        bucket = _primary_bucket(item)
        if bucket == "could_not_check" and _suppress_could_not_check(item):
            # Suppressed from the main section; still appears in the roster below.
            continue
        buckets[bucket].append(item)

    buckets["still_oos"].sort(key=lambda i: _label(i).lower())

    return {
        "on_sale": [
            _on_sale_line(i, fx_rates, marker=f"{_MARK_DROP} ") for i in buckets["on_sale"]
        ],
        "standing_discount": [
            _standing_discount_line(i, fx_rates, marker=f"{_MARK_FLAT} ")
            for i in buckets["standing_discount"]
        ],
        # Loose mentions stay unmarked — a "verify link" match shouldn't wear a
        # confident PRICE DROP badge until the user confirms it.
        "uncertain": [_on_sale_line(i, fx_rates) for i in buckets["uncertain"]],
        "newly_oos": [
            _newly_oos_line(i, fx_rates, marker=f"{_MARK_OOS} ") for i in buckets["newly_oos"]
        ],
        "back_in_stock": [
            _back_in_stock_line(i, fx_rates, marker=f"{_MARK_STOCK} ")
            for i in buckets["back_in_stock"]
        ],
        "now_low_stock": [
            _now_low_stock_line(i, fx_rates, marker=f"{_MARK_LOW} ")
            for i in buckets["now_low_stock"]
        ],
        "could_not_check": [_could_not_check_line(i, fx_rates) for i in buckets["could_not_check"]],
        "removed_from_shop": [_removed_line(i, fx_rates) for i in buckets["removed_from_shop"]],
        "still_oos": [_still_oos_line(i) for i in buckets["still_oos"]],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_digest(data: dict) -> str:
    items = data.get("items", [])
    shop_sales = data.get("shop_sales", [])
    non_clothing_shops = data.get("non_clothing_shops") or []
    # Product URLs we can't crawl (Amazon) — listed read-only, no price/stock.
    untracked_items = data.get("untracked_items") or []
    email_sale_announcements = data.get("email_sales") or []
    email_unclear = data.get("email_unclear") or []
    # Un-attributed marketing SMS (shops not on the watchlist/allowlist) — a
    # discovery surface prompting the user to add brands to SMS_SALE_SHOPS.
    untracked_sms = data.get("untracked_sms") or []
    today = data.get("today") or datetime.now(timezone.utc).date()
    codes = data.get("codes", [])
    unresolved = data.get("unresolved_shops", [])
    fx_rates = data.get("fx_rates")
    fit_pending = data.get("fit_pending") or []
    fit_review_all_url = data.get("fit_review_all_url")
    removal_pending = data.get("removal_pending") or []
    removal_review_all_url = data.get("removal_review_all_url")
    review_requests = data.get("review_requests") or []
    review_requests_all_url = data.get("review_requests_all_url")
    review_requests_days = data.get("review_requests_days")
    # Email-sourced "back in stock" alerts — merged into the scrape-driven
    # "Back in stock" section below, tagged as email alerts.
    email_restocks = data.get("email_restocks") or []
    email_restocks_all_url = data.get("email_restocks_all_url")

    # Split items + homepage sale status into clothing vs non-clothing by shop.
    # Non-clothing breaks out into its own block at the bottom (see below); the
    # clothing sections stay on top. Matching is case-insensitive on the shop
    # label, which is shared across items, shop_sales, and the watchlist-derived
    # non-clothing set. Empty set ⇒ everything is clothing (back-compat).
    nc_set = {s.strip().lower() for s in non_clothing_shops if s and s.strip()}

    def _is_nc_shop_name(shop: str | None) -> bool:
        return bool(nc_set) and (shop or "").strip().lower() in nc_set

    clothing_items = [i for i in items if not _is_nc_shop_name(i.get("shop"))]
    nc_items = [i for i in items if _is_nc_shop_name(i.get("shop"))]
    clothing_shop_sales = [s for s in shop_sales if not _is_nc_shop_name(s.get("shop"))]
    nc_shop_sales = [s for s in shop_sales if _is_nc_shop_name(s.get("shop"))]

    # Priority items get a pinned "Watching now" block at the very top with their
    # full status, so they're excluded from the per-URL change sections below to
    # avoid listing the same item twice. They stay in the "All items by shop"
    # roster, which is the exhaustive bottom reference. Spans clothing AND
    # non-clothing (a single block, regardless of section).
    priority_items = [i for i in items if i.get("priority")]
    priority_section = _priority_section(priority_items, fx_rates)
    cl_buckets_items = [i for i in clothing_items if not i.get("priority")]
    nc_buckets_items = [i for i in nc_items if not i.get("priority")]

    cl = _render_item_buckets(cl_buckets_items, fx_rates)

    # Merge email-sourced restock alerts into the scrape-driven "Back in stock"
    # lines (email lines tagged _(email alert)_); the section self-suppresses
    # only when BOTH sources are empty.
    back_in_stock_lines = cl["back_in_stock"] + _email_restock_lines(
        email_restocks, email_restocks_all_url,
    )

    sections: list[str | None] = [
        priority_section,
        _shops_on_sale_section(clothing_shop_sales),
        _email_sales_section(email_sale_announcements, today),
        _possible_email_sales_section(email_unclear),
        _untracked_sms_section(untracked_sms),
        _section("Items on sale (specific URLs)", cl["on_sale"]),
        _section("Uncertain matches (loose mentions, verify link)", cl["uncertain"]),
        _section("Newly out of stock", cl["newly_oos"]),
        _section("Back in stock", back_in_stock_lines),
        _section("Now low stock", cl["now_low_stock"]),
        _section("Could not check", cl["could_not_check"]),
        _section("Removed from shop", cl["removed_from_shop"]),
        _section("Standing discounts (always marked down)", cl["standing_discount"]),
        _untracked_section(untracked_items),
        _shops_unclear_section(clothing_shop_sales),
        _unresolved_section(unresolved),
        _codes_section(codes),
        _unattributed_codes_section(codes),
        _fit_feedback_section(
            fit_pending[:_DAILY_FIT_PENDING_CAP], fit_review_all_url,
            total=len(fit_pending),
        ),
        _removal_section(
            removal_pending[:_DAILY_REMOVAL_CAP], removal_review_all_url,
            total=len(removal_pending),
        ),
        _review_requests_section(
            review_requests, review_requests_all_url, review_requests_days,
        ),
        _section("Still out of stock", cl["still_oos"]),
        _shops_no_sale_section(clothing_shop_sales),
        _all_items_section(clothing_items, fx_rates),
    ]

    # Non-clothing block — a full mirror of the homepage-driven sections
    # (shop sale status + per-item state + roster), grouped under a single
    # "# Non-clothing" divider at the very bottom. Email-announced sales,
    # promo codes, fit feedback, and unresolved shops stay above (fit is
    # clothing-only by nature; the rest aren't tracked items/shops). The whole
    # block self-suppresses when there's no non-clothing content.
    nc = _render_item_buckets(nc_buckets_items, fx_rates)
    nc_sections: list[str | None] = [
        _shops_on_sale_section(nc_shop_sales, "Non-clothing shops on sale"),
        _section("Items on sale (non-clothing)", nc["on_sale"]),
        _section("Uncertain matches (non-clothing)", nc["uncertain"]),
        _section("Newly out of stock (non-clothing)", nc["newly_oos"]),
        _section("Back in stock (non-clothing)", nc["back_in_stock"]),
        _section("Now low stock (non-clothing)", nc["now_low_stock"]),
        _section("Could not check (non-clothing)", nc["could_not_check"]),
        _section("Removed from shop (non-clothing)", nc["removed_from_shop"]),
        _section("Standing discounts (non-clothing)", nc["standing_discount"]),
        _shops_unclear_section(nc_shop_sales, "Non-clothing sale status unclear"),
        _section("Still out of stock (non-clothing)", nc["still_oos"]),
        _shops_no_sale_section(nc_shop_sales, "Non-clothing shops with no sale"),
        _all_items_section(nc_items, fx_rates, "All non-clothing items by shop"),
    ]
    nc_body = [s for s in nc_sections if s]
    if nc_body:
        sections.append("# Non-clothing")
        sections.extend(nc_body)

    return "\n\n".join(s for s in sections if s)


def build_fit_digest(
    pending: list[dict], review_all_url: str | None = None,
) -> str:
    """Body for the standalone weekly fit-feedback email.

    Just the "Fit feedback wanted" section under a top-level heading. Returns
    ``""`` when nothing is pending so the caller can skip the send entirely.
    """
    section = _fit_feedback_section(pending, review_all_url)
    if not section:
        return ""
    return "# Fit feedback\n\n" + section
