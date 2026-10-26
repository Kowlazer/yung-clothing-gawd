"""Collapse duplicate confirmations of the same order into one wardrobe order.

Why this exists
---------------
Wardrobe item ids are ``sha256(order_email_id + index)``, so ``_merge_items``'
id-dedupe only catches the *same email* scanned twice. A shop that sends two
confirmation emails for one purchase therefore stored every item twice. Two
shapes were seen live (2026-08/09):

* **Same email re-sent** — a WooCommerce store re-saved an order a day later and
  re-fired its "Order N confirmed" email (identical subject, body and order
  number; only the discount rounding moved a cent). Caught *before* Claude by
  the ``(shop_domain, order_number)`` key — :func:`dedupe_records_by_order_number`.
* **Two systems, one purchase** — the store's own receipt ("order confirmed",
  ``Order N``) plus a marketing-flow "Thanks for your order!" a minute later
  from a different sender address on the same domain, which carries the item
  list but no usable order number. Caught *after* extraction by matching the
  order's item multiset — :func:`drop_duplicate_orders`.

The same item-multiset matcher drives the one-off ``order_scan --dedupe-orders``
cleanup of duplicates already stored (:func:`apply_dedupe`).

Conservative by design: a false merge silently loses a real purchase, a missed
one just shows a duplicate card. So a duplicate needs the same shop domain, the
**exact** same multiset of (name, size, colour, qty), purchase dates within
``window_days``, and no *conflicting* order numbers (two different known order
numbers are two genuine orders, however identical their contents). Pure logic —
no I/O.
"""

from __future__ import annotations

import re
from datetime import date

DEFAULT_WINDOW_DAYS = 3

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# Fields carried from a duplicate onto the kept copy when the kept copy lacks
# them (user work or enrichment that landed on the "wrong" copy). Never
# overwrites a value the kept copy already has.
_CARRY_FIELDS = (
    "fit_review", "body_comp", "body_comp_at_purchase",
    "image_url", "product_url",
    "shipping_email_id", "shipped_at", "tracking_url",
    "order_number",
)


def _norm(value) -> str:
    return " ".join(_NON_ALNUM_RE.split(str(value or "").lower())).strip()


def _qty(value) -> int:
    try:
        return int(value or 1)
    except (TypeError, ValueError):
        return 1


def item_key(item: dict) -> tuple:
    """What makes two item rows "the same line on the receipt".

    Uses the *original* extracted name when the user renamed the item in the
    wardrobe browser, so a hand-edit on one copy can't hide its twin."""
    name = item.get("item_name_original") or item.get("item_name")
    return (_norm(name), _norm(item.get("size")), _norm(item.get("color")),
            _qty(item.get("qty")))


def order_signature(items: list[dict]) -> tuple:
    """Order-independent multiset of :func:`item_key` — equal iff the two orders
    list exactly the same lines (duplicates included)."""
    return tuple(sorted(item_key(it) for it in items))


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def _msgid_rank(email_id: str | None) -> tuple[int, str]:
    """Gmail X-GM-MSGIDs grow over time; use them as the tie-break for "earliest"."""
    s = str(email_id or "")
    return (int(s), s) if s.isdigit() else (2**63, s)


def _order_sort_key(purchased_at: str | None, email_id: str | None) -> tuple:
    return ((purchased_at or "9999-99-99")[:10], _msgid_rank(email_id))


def numbers_conflict(a: str | None, b: str | None) -> bool:
    """Two known, different order numbers ⇒ two genuine orders."""
    a, b = (a or "").strip().upper(), (b or "").strip().upper()
    return bool(a and b and a != b)


# ---------------------------------------------------------------------------
# Pre-Claude: collapse order-email records that share (domain, order number)
# ---------------------------------------------------------------------------

def dedupe_records_by_order_number(
    records: list[dict], existing_items: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Split ``_run_scan`` order records into ``(kept, duplicates)``.

    A record is a duplicate when another record in this batch — or an item
    already in the wardrobe — has the same ``shop_domain`` and
    ``order_number``. The earliest record per key is kept (purchase date, then
    msgid). Records with no domain or no order number always pass through; the
    post-extraction :func:`drop_duplicate_orders` covers those. Input order of
    the kept records is preserved.
    """
    # (domain, number) -> email ids already stored under it. A record whose own
    # email is the stored one (a --reprocess re-scan) isn't a duplicate of
    # itself; re-adding its items is _merge_items' (id-dedupe) business.
    known: dict[tuple[str, str], set[str]] = {}
    for it in existing_items or []:
        num = (it.get("order_number") or "").strip().upper()
        dom = (it.get("shop_domain") or "").strip().lower()
        if num and dom:
            known.setdefault((dom, num), set()).add(str(it.get("order_email_id") or ""))

    def _key(r: dict) -> tuple[str, str] | None:
        num = (r.get("order_number") or "").strip().upper()
        dom = (r.get("shop_domain") or "").strip().lower()
        return (dom, num) if num and dom else None

    def _stored_elsewhere(r: dict, key: tuple[str, str]) -> bool:
        return bool(known.get(key, set()) - {str(r.get("email_id") or "")})

    winners: dict[tuple[str, str], dict] = {}
    for r in sorted(records, key=lambda r: _order_sort_key(r.get("purchased_at"), r.get("email_id"))):
        key = _key(r)
        if key and not _stored_elsewhere(r, key):
            winners.setdefault(key, r)

    kept: list[dict] = []
    dups: list[dict] = []
    for r in records:
        key = _key(r)
        if key is None:
            kept.append(r)
        elif _stored_elsewhere(r, key) or winners.get(key) is not r:
            dups.append(r)
        else:
            kept.append(r)
    return kept, dups


# ---------------------------------------------------------------------------
# Item-multiset matching (post-extraction + cleanup)
# ---------------------------------------------------------------------------

def group_orders(items: list[dict]) -> dict[str, list[dict]]:
    """``{order_email_id: [items...]}`` in first-seen order."""
    out: dict[str, list[dict]] = {}
    for it in items or []:
        out.setdefault(str(it.get("order_email_id") or ""), []).append(it)
    out.pop("", None)
    return out


class _Order:
    __slots__ = ("email_id", "items", "domain", "signature", "day", "number", "sort_key")

    def __init__(self, email_id: str, items: list[dict]):
        first = items[0]
        self.email_id = email_id
        self.items = items
        self.domain = (first.get("shop_domain") or "").strip().lower()
        self.signature = order_signature(items)
        self.day = _parse_date(first.get("purchased_at"))
        self.number = next(
            ((it.get("order_number") or "").strip() for it in items if it.get("order_number")),
            "",
        )
        self.sort_key = _order_sort_key(first.get("purchased_at"), email_id)


def _is_dup(cand: _Order, ref: _Order, window_days: int) -> bool:
    if not cand.domain or cand.domain != ref.domain:
        return False
    if cand.signature != ref.signature or not cand.signature:
        return False
    if cand.day is None or ref.day is None:
        return False
    if abs((cand.day - ref.day).days) > window_days:
        return False
    return not numbers_conflict(cand.number, ref.number)


def _find_duplicates(
    reference: list[_Order], candidates: list[_Order], window_days: int,
) -> list[tuple[_Order, _Order]]:
    """Walk ``candidates`` oldest-first; each one that duplicates something in
    the growing reference set is returned as ``(dup, kept)``, otherwise it joins
    the reference set."""
    index: dict[tuple, list[_Order]] = {}
    for o in reference:
        index.setdefault((o.domain, o.signature), []).append(o)
    pairs: list[tuple[_Order, _Order]] = []
    for cand in sorted(candidates, key=lambda o: o.sort_key):
        bucket = index.setdefault((cand.domain, cand.signature), [])
        match = next((ref for ref in sorted(bucket, key=lambda o: o.sort_key)
                      if _is_dup(cand, ref, window_days)), None)
        if match is not None:
            pairs.append((cand, match))
        else:
            bucket.append(cand)
    return pairs


def drop_duplicate_orders(
    new_items: list[dict],
    existing_items: list[dict] | None = None,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> tuple[list[dict], list[tuple[str, str]]]:
    """Drop newly extracted orders that duplicate an existing or earlier order.

    Returns ``(kept_items, dropped)`` where ``dropped`` is a list of
    ``(duplicate_email_id, kept_email_id)``. Existing wardrobe orders are never
    dropped — they're the reference set. Among new orders the earliest wins."""
    existing_orders = [
        _Order(eid, its) for eid, its in group_orders(existing_items or []).items()
    ]
    new_ids = {str(it.get("order_email_id") or "") for it in new_items or []}
    # An email already stored can't be its own duplicate (re-scan of the same
    # email is _merge_items' job), so keep it out of the reference set.
    existing_orders = [o for o in existing_orders if o.email_id not in new_ids]
    new_orders = [_Order(eid, its) for eid, its in group_orders(new_items).items()]
    pairs = _find_duplicates(existing_orders, new_orders, window_days)
    drop_ids = {dup.email_id for dup, _ in pairs}
    kept = [it for it in new_items or []
            if str(it.get("order_email_id") or "") not in drop_ids]
    return kept, [(dup.email_id, ref.email_id) for dup, ref in pairs]


def find_stored_duplicates(
    items: list[dict], *, window_days: int = DEFAULT_WINDOW_DAYS,
) -> list[tuple[str, str]]:
    """``(duplicate_email_id, kept_email_id)`` pairs among already-stored items,
    keeping the earliest order of each duplicate set."""
    orders = [_Order(eid, its) for eid, its in group_orders(items).items()]
    return [(dup.email_id, ref.email_id)
            for dup, ref in _find_duplicates([], orders, window_days)]


# ---------------------------------------------------------------------------
# Cleanup: merge + remove stored duplicates
# ---------------------------------------------------------------------------

def _has_value(v) -> bool:
    return v not in (None, "", [], {})


def merge_item_into(kept: dict, dup: dict) -> list[str]:
    """Copy anything the user (or a backfill) put on ``dup`` that ``kept`` lacks.

    Returns the names of the fields carried over. Never overwrites a value
    ``kept`` already has, with one exception: a watchlist removal *decision*
    (``approved_for_removal`` True/False) on the duplicate beats an undecided
    (None) one on the kept copy, so an approval made against the twin isn't
    lost and the item doesn't start nagging again."""
    carried: list[str] = []
    for f in _CARRY_FIELDS:
        if not _has_value(kept.get(f)) and _has_value(dup.get(f)):
            kept[f] = dup[f]
            carried.append(f)

    if not kept.get("category") and dup.get("category"):
        kept["category"] = dup["category"]
        carried.append("category")
        if "is_clothing" in dup and "is_clothing" not in kept:
            kept["is_clothing"] = dup["is_clothing"]

    if dup.get("name_edited_at") and not kept.get("name_edited_at"):
        for f in ("item_name", "item_name_original", "name_edited_at"):
            if f in dup:
                kept[f] = dup[f]
        carried.append("item_name")

    km, dm = kept.get("watchlist_match"), dup.get("watchlist_match")
    if not km and dm:
        kept["watchlist_match"] = dict(dm)
        carried.append("watchlist_match")
    elif km and dm and km.get("approved_for_removal") is None \
            and dm.get("approved_for_removal") is not None:
        km["approved_for_removal"] = dm["approved_for_removal"]
        carried.append("watchlist_match.approved_for_removal")
    return carried


def _pair_items(kept_items: list[dict], dup_items: list[dict]) -> list[tuple[dict, dict]]:
    """Pair each duplicate row with an unpaired kept row of the same key."""
    pool: dict[tuple, list[dict]] = {}
    for it in kept_items:
        pool.setdefault(item_key(it), []).append(it)
    pairs = []
    for d in dup_items:
        bucket = pool.get(item_key(d)) or []
        if bucket:
            pairs.append((bucket.pop(0), d))
    return pairs


def apply_dedupe(
    wardrobe: dict, *, window_days: int = DEFAULT_WINDOW_DAYS,
) -> list[dict]:
    """Remove stored duplicate orders from ``wardrobe`` in place.

    For each duplicate order: carry user data onto the kept twin
    (:func:`merge_item_into`), re-point any ``watchlist_exclusions`` row at the
    kept id, then drop the duplicate's items. ``scan_state.processed_email_ids``
    is left untouched so the next scan doesn't re-ingest the dropped email.
    Returns one report dict per removed order (for the CLI to print)."""
    items: list[dict] = wardrobe.get("items") or []
    pairs = find_stored_duplicates(items, window_days=window_days)
    if not pairs:
        return []
    by_order = group_orders(items)
    reports: list[dict] = []
    removed_ids: set[str] = set()
    id_map: dict[str, str] = {}
    for dup_eid, kept_eid in pairs:
        kept_items, dup_items = by_order[kept_eid], by_order[dup_eid]
        carried: dict[str, list[str]] = {}
        for k, d in _pair_items(kept_items, dup_items):
            fields = merge_item_into(k, d)
            if fields:
                carried[k["id"]] = fields
            id_map[d["id"]] = k["id"]
        removed_ids.update(d["id"] for d in dup_items)
        first = dup_items[0]
        reports.append({
            "shop": first.get("shop"),
            "purchased_at": first.get("purchased_at"),
            "kept_purchased_at": kept_items[0].get("purchased_at"),
            "duplicate_email_id": dup_eid,
            "kept_email_id": kept_eid,
            "items": [f"{it.get('item_name')} ({it.get('size') or '-'})" for it in dup_items],
            "carried": carried,
        })
    wardrobe["items"] = [it for it in items if it.get("id") not in removed_ids]
    for ex in wardrobe.get("watchlist_exclusions") or []:
        if ex.get("item_id") in id_map:
            ex["item_id"] = id_map[ex["item_id"]]
    return reports

