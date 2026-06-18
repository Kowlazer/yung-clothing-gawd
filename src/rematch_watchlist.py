"""One-shot: recompute ``watchlist_match`` for every wardrobe item using
the current ``_match_watchlist`` logic.

The matching policy in ``order_scan._match_watchlist`` was tightened to
require both a shop link (domain substring or shop-name substring with
``len(shop) >= 3``) AND a Jaccard score above ``_JACCARD_THRESHOLD``.
Items extracted under the old logic still carry the old, looser matches
on the Gist — many of them point at unrelated products on different
shops' URLs.

This script:
  1. Reads ``wardrobe`` from the Gist.
  2. Resets every item's ``watchlist_match`` to ``None``
     (and ``approved_for_removal`` decisions along with it — they were
     never going to be relevant once the match itself was bogus).
  3. Re-runs the matcher against the live watchlist Doc.
  4. Prints before/after counts and a sample of dropped matches, then
     writes back to the Gist unless ``--dry-run``.

This does NOT re-fetch any Gmail emails or call Claude — it operates
purely on the data already in the Gist plus the watchlist Doc.

Usage::

    python -m src.rematch_watchlist --dry-run    # show diff only
    python -m src.rematch_watchlist              # apply + write Gist
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from src.config import load_config
from src.order_scan import _match_watchlist
from src.state import read_state, write_state
from src.watchlist import fetch_watchlist

log = logging.getLogger(__name__)


def _summarise(label: str, items: list[dict]) -> None:
    n_match = sum(1 for it in items if it.get("watchlist_match"))
    print(f"{label}: {n_match} items have watchlist_match (of {len(items)})")


def run(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rematch_watchlist")
    p.add_argument("--dry-run", action="store_true",
                   help="Print before/after summary but don't write to Gist.")
    args = p.parse_args(argv)

    cfg = load_config()
    log.info("rematch: reading state from gist")
    state = read_state(cfg.gist_id, cfg.github_token)
    wardrobe = state.get("wardrobe") or {}
    items = wardrobe.get("items") or []
    if not items:
        log.error("rematch: no wardrobe items to process")
        return 1

    # Snapshot the OLD matches so we can show what got dropped.
    old_match = {
        it["id"]: dict(it["watchlist_match"]) if it.get("watchlist_match") else None
        for it in items
    }
    _summarise("before", items)

    # Reset every item — the rematcher only sets watchlist_match when it
    # finds something, so we have to clear stale state first.
    approved_count = sum(
        1 for it in items
        if it.get("watchlist_match")
        and it["watchlist_match"].get("approved_for_removal") is True
    )
    if approved_count:
        log.warning(
            "rematch: %d items had approved_for_removal=True — these will "
            "also be reset. Pre-existing watchlist_exclusions entries are "
            "left intact.", approved_count,
        )
    for it in items:
        it["watchlist_match"] = None

    log.info("rematch: fetching watchlist Doc")
    watchlist_text = fetch_watchlist(cfg.watchlist_url)
    if not watchlist_text:
        log.error("rematch: watchlist Doc fetched empty — aborting")
        return 1

    _match_watchlist(items, watchlist_text)
    _summarise("after", items)

    dropped = []
    survived = []
    new = []
    for it in items:
        old = old_match.get(it["id"])
        new_m = it.get("watchlist_match")
        if old and not new_m:
            dropped.append((it, old))
        elif old and new_m:
            survived.append((it, old, new_m))
        elif not old and new_m:
            new.append((it, new_m))

    print()
    print(f"dropped (had match before, none now): {len(dropped)}")
    print(f"survived (matched under both):        {len(survived)}")
    print(f"newly matched (none before, has now): {len(new)}")

    def _show(label, rows, fmt):
        print()
        print(f"--- sample {label} (up to 10) ---")
        for row in rows[:10]:
            print("  " + fmt(*row))

    _show(
        "dropped",
        dropped,
        lambda it, old: f"{it.get('shop'):20s} | {it.get('item_name')[:50]:50s} "
                       f"[was: {old.get('matched_line','')[:60]}]",
    )
    _show(
        "survived",
        survived,
        lambda it, old, new: f"{it.get('shop'):20s} | "
                            f"{it.get('item_name')[:40]:40s} "
                            f"[score {new.get('score')}: "
                            f"{new.get('matched_line','')[:50]}]",
    )

    if args.dry_run:
        log.info("rematch: --dry-run; not writing to Gist")
        return 0

    log.info("rematch: writing updated wardrobe to gist")
    wardrobe["items"] = items
    write_state(
        cfg.gist_id, cfg.github_token,
        prices=state.get("prices") or {},
        aliases=state.get("aliases") or {},
        codes=state.get("codes") or [],
        wardrobe=wardrobe,
    )
    log.info("rematch: done")
    return 0


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    logging.basicConfig(
        level=os.environ.get("SALE_CHECK_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        from dotenv import load_dotenv
        load_dotenv(override=True)
    except ImportError:
        pass
    sys.exit(run())


if __name__ == "__main__":
    main()
