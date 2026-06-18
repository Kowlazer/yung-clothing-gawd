"""One-shot: drop wardrobe items whose source email is no longer classified
as an order under the current ``_classify`` rules.

The classifier was tightened in late 2026-05 to:
  * Detect payment-failure / cancellation emails ("Update: Payment Failed",
    "your order has been cancelled") that previously slipped through and
    got their item list extracted as if a real order had happened.
  * Detect post-order status updates ("A note has been added to your order",
    "Your order has been updated") that re-include the full order summary.
  * Properly match ``Shipped:`` subject lines (a regex bug previously meant
    Amazon's shipping emails got misclassified as orders).
  * Catch ``has been shipped`` (not just ``has shipped``).

For each unique ``order_email_id`` in the wardrobe:
  1. Re-fetch the email body from Gmail via IMAP (``X-GM-MSGID`` search).
  2. Re-run ``_classify``.
  3. If the new label is NOT ``"order"`` → drop every item with this
     ``order_email_id`` from ``wardrobe.items`` AND remove the email_id
     from ``scan_state.processed_email_ids`` so the next
     ``python -m src.order_scan`` run will re-evaluate it (and, in cases
     like the missed Amazon ``Ordered:`` confirmation, may now extract it).

Items whose source email is no longer in Gmail (deleted / wrong account)
are kept as-is — we can't tell whether they were valid or not.

Usage::

    python -m src.purge_failed_orders --dry-run     # report only
    python -m src.purge_failed_orders               # apply + write Gist
"""
from __future__ import annotations

import argparse
import email
import logging
import os
import sys
from collections import Counter, defaultdict

from src.config import load_config
from src.gmail import _connect, _extract_body_text, _header, _parse_fetch_response
from src.order_parse import sender_domain
from src.order_scan import _classify
from src.state import read_state, write_state

log = logging.getLogger(__name__)


def _fetch_email(client, gm_msgid: str) -> dict | None:
    """Fetch one email by X-GM-MSGID. Returns ``{subject, from, body_text}``
    or None on miss / parse failure."""
    typ, data = client.uid("SEARCH", "X-GM-MSGID", gm_msgid)
    if typ != "OK" or not data or not data[0]:
        return None
    uids = data[0].split()
    if not uids:
        return None
    uid = uids[0]
    typ, msg_data = client.uid("FETCH", uid, "(X-GM-MSGID BODY.PEEK[])")
    if typ != "OK":
        return None
    parsed = _parse_fetch_response(msg_data)
    if not parsed:
        return None
    _, raw = parsed
    try:
        msg = email.message_from_bytes(raw)
    except Exception:  # noqa: BLE001 — defensive
        return None
    return {
        "subject": _header(msg, "Subject"),
        "from": _header(msg, "From"),
        "body_text": _extract_body_text(msg),
    }


def run(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="purge_failed_orders")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would be dropped without writing back.")
    args = p.parse_args(argv)

    cfg = load_config()
    log.info("purge: reading state from gist")
    state = read_state(cfg.gist_id, cfg.github_token)
    wardrobe = state.get("wardrobe") or {}
    items = wardrobe.get("items") or []
    if not items:
        log.error("purge: no wardrobe items to process")
        return 1
    scan_state = wardrobe.get("scan_state") or {}
    processed = scan_state.get("processed_email_ids") or {}

    # Group items by source email.
    items_by_email: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        items_by_email[it.get("order_email_id") or ""].append(it)
    unique_emails = sorted(eid for eid in items_by_email if eid)
    log.info("purge: %d items across %d unique order emails",
             len(items), len(unique_emails))

    # Re-classify each email.
    log.info("purge: re-fetching + re-classifying — this will take a few minutes")
    client = _connect(cfg.gmail_username, cfg.gmail_app_password)
    drop_emails: dict[str, str] = {}      # email_id -> reason ("other"/"shipping"/"missing")
    drop_examples: dict[str, list[str]] = defaultdict(list)  # reason -> [subjects]
    try:
        client.select("INBOX", readonly=True)
        for i, eid in enumerate(unique_emails, 1):
            if i % 50 == 0:
                log.info("purge: reclassified %d/%d", i, len(unique_emails))
            em = _fetch_email(client, eid)
            if em is None:
                # Email gone from inbox — leave items alone.
                continue
            label = _classify(em)
            if label != "order":
                drop_emails[eid] = label
                if len(drop_examples[label]) < 8:
                    drop_examples[label].append(
                        f"{em['subject'][:70]}  (from {sender_domain(em['from']) or '?'})"
                    )
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass

    # Compute what would be dropped.
    items_to_drop = [it for it in items if it.get("order_email_id") in drop_emails]
    items_to_keep = [it for it in items if it.get("order_email_id") not in drop_emails]

    # Summary by (new_label, shop)
    by_reason: Counter[tuple[str, str]] = Counter()
    for it in items_to_drop:
        eid = it.get("order_email_id") or ""
        reason = drop_emails.get(eid, "?")
        by_reason[(reason, it.get("shop") or "")] += 1

    print()
    print("=" * 70)
    print(f"Re-classified {len(unique_emails)} unique order emails.")
    print(f"  emails newly NOT-order: {len(drop_emails)}")
    print(f"  items to drop:          {len(items_to_drop)} of {len(items)}")
    print(f"  items to keep:          {len(items_to_keep)}")
    print(f"  email_ids to remove from processed_email_ids: {len(drop_emails)}")
    print("=" * 70)

    print()
    print("Drops broken out by new classification + shop:")
    for (reason, shop), n in by_reason.most_common(40):
        print(f"  {reason:10s} | {shop:25s} | {n}")

    print()
    print("Sample subjects per reason:")
    for reason, subjects in drop_examples.items():
        print(f"  --- {reason} ---")
        for s in subjects:
            print(f"    {s}")

    if args.dry_run:
        log.info("purge: --dry-run; not writing to Gist")
        return 0

    if not drop_emails:
        log.info("purge: nothing to drop")
        return 0

    # Apply.
    wardrobe["items"] = items_to_keep
    for eid in drop_emails:
        processed.pop(eid, None)
    scan_state["processed_email_ids"] = processed
    wardrobe["scan_state"] = scan_state

    log.info("purge: writing updated wardrobe to gist")
    write_state(
        cfg.gist_id, cfg.github_token,
        prices=state.get("prices") or {},
        aliases=state.get("aliases") or {},
        codes=state.get("codes") or [],
        wardrobe=wardrobe,
    )
    log.info("purge: done")
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
