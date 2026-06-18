"""One-shot: re-resolve shop names in wardrobe.json using current resolve_shop.

After fixing resolve_shop for single-letter marketing subdomains and
shared transactional senders (shopifyemail.com et al.), wardrobe items
extracted before the fix still carry the broken shop assignments
("T", "S", "Send", ...). This script patches them in place without
re-running Claude.

For each item we feed a synthesised From header (``<noreply@DOMAIN>``)
into ``resolve_shop``. That handles 90% of the cases — single-letter
prefixes get stripped correctly.

For items whose stored ``shop_domain`` collapses to a shared
transactional apex (``shopifyemail.com``, ``sendgrid.net``, ...), the
real shop identity lives in the From display name, which the
synthesised header doesn't have. We re-fetch those original order
emails from Gmail via IMAP (``X-GM-MSGID`` search) so the display-name
fallback engages.

Usage::

    python -m src.relabel_shops --dry-run   # print summary, no Gist write
    python -m src.relabel_shops             # apply + write Gist
"""
from __future__ import annotations

import argparse
import email
import logging
import os
import sys
from collections import Counter

from src.config import load_config
from src.gmail import _connect, _header
from src.order_parse import (
    _SHARED_TRANSACTIONAL_APEXES,
    _strip_transactional_prefix,
    resolve_shop,
)
from src.state import read_state, write_state

log = logging.getLogger(__name__)


def _faux_from(shop_domain: str) -> str:
    return f"<noreply@{shop_domain}>"


def _fetch_from_header(client, gm_msgid: str) -> str:
    """Fetch one email's From header by X-GM-MSGID. Returns '' on miss."""
    typ, data = client.uid("SEARCH", "X-GM-MSGID", gm_msgid)
    if typ != "OK" or not data or not data[0]:
        return ""
    uids = data[0].split()
    if not uids:
        return ""
    uid = uids[0]
    typ, msg_data = client.uid(
        "FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM)])"
    )
    if typ != "OK":
        return ""
    for item in msg_data:
        if isinstance(item, tuple) and len(item) >= 2:
            raw = item[1]
            try:
                msg = email.message_from_bytes(raw)
            except Exception:  # noqa: BLE001 — defensive
                return ""
            return _header(msg, "From")
    return ""


def _needs_imap_refetch(shop_domain: str) -> bool:
    apex = _strip_transactional_prefix((shop_domain or "").lower())
    return apex in _SHARED_TRANSACTIONAL_APEXES


def _relabel(items: list[dict], aliases: dict, cfg) -> list[tuple[dict, str, str]]:
    """Return a list of (item, old_shop, new_shop) for items whose shop changed.

    Mutates ``items`` in place — successful re-resolutions overwrite
    ``shop`` and ``shop_domain``.
    """
    changes: list[tuple[dict, str, str]] = []

    fast_path: list[dict] = []
    imap_path: list[dict] = []
    for it in items:
        domain = it.get("shop_domain") or ""
        if _needs_imap_refetch(domain):
            imap_path.append(it)
        else:
            fast_path.append(it)

    log.info(
        "relabel: %d items via synthesised From; %d via IMAP re-fetch",
        len(fast_path), len(imap_path),
    )

    for it in fast_path:
        old_shop = it.get("shop") or ""
        new_shop, new_domain = resolve_shop(
            _faux_from(it.get("shop_domain") or ""), aliases,
        )
        if new_shop and (new_shop != old_shop or new_domain != it.get("shop_domain")):
            changes.append((it, old_shop, new_shop))
            it["shop"] = new_shop
            it["shop_domain"] = new_domain

    if imap_path:
        log.info("relabel: connecting to Gmail IMAP for %d re-fetches", len(imap_path))
        client = _connect(cfg.gmail_username, cfg.gmail_app_password)
        try:
            client.select("INBOX", readonly=True)
            for i, it in enumerate(imap_path, 1):
                eid = it.get("order_email_id") or ""
                if not eid:
                    continue
                from_header = _fetch_from_header(client, eid)
                if i % 20 == 0:
                    log.info("relabel: imap refetch %d/%d", i, len(imap_path))
                if not from_header:
                    log.info("relabel: no From for msgid=%s", eid)
                    continue
                old_shop = it.get("shop") or ""
                new_shop, new_domain = resolve_shop(from_header, aliases)
                if new_shop and (new_shop != old_shop
                                 or new_domain != it.get("shop_domain")):
                    changes.append((it, old_shop, new_shop))
                    it["shop"] = new_shop
                    it["shop_domain"] = new_domain
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass

    return changes


def _print_summary(items: list[dict], changes: list[tuple[dict, str, str]]) -> None:
    print()
    print("=" * 70)
    print(f"Changed shop name on {len(changes)} of {len(items)} items.")
    print("=" * 70)

    # Group changes by (old → new) pair.
    pair_counts: Counter[tuple[str, str]] = Counter()
    for _, old, new in changes:
        pair_counts[(old, new)] += 1
    print()
    print("Reassignments (old -> new, count):")
    for (old, new), n in pair_counts.most_common():
        print(f"  {old!r:20s} -> {new!r:30s} ({n})")

    # Show a few samples per pair (max 2).
    seen_pairs: dict[tuple[str, str], int] = {}
    print()
    print("Sample reassigned items (up to 2 per pair):")
    for it, old, new in changes:
        key = (old, new)
        seen_pairs[key] = seen_pairs.get(key, 0) + 1
        if seen_pairs[key] > 2:
            continue
        print(f"  [{old} -> {new}]  {it['item_name']!r}  "
              f"(domain={it['shop_domain']})")

    print()
    print("New shop distribution:")
    final = Counter(i.get("shop") for i in items)
    for s, n in final.most_common():
        print(f"  {n:4d}  {s}")


def run(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="relabel_shops")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the diff but don't write back to Gist.")
    args = p.parse_args(argv)

    cfg = load_config()
    log.info("relabel: reading state from gist")
    state = read_state(cfg.gist_id, cfg.github_token)
    wardrobe = state.get("wardrobe") or {}
    items = wardrobe.get("items") or []
    if not items:
        log.error("relabel: no wardrobe items to process")
        return 1

    aliases = state.get("aliases") or {}
    changes = _relabel(items, aliases, cfg)
    _print_summary(items, changes)

    if not changes:
        log.info("relabel: nothing changed; not writing")
        return 0

    if args.dry_run:
        log.info("relabel: --dry-run; not writing to Gist")
        return 0

    log.info("relabel: writing updated wardrobe to gist")
    wardrobe["items"] = items
    write_state(
        cfg.gist_id, cfg.github_token,
        prices=state.get("prices") or {},
        aliases=aliases,
        codes=state.get("codes") or [],
        wardrobe=wardrobe,
    )
    log.info("relabel: done")
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
