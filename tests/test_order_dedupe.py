"""Duplicate-order collapsing (src/order_dedupe.py) + its order_scan wiring.

All shops, order numbers and email ids are synthetic.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src import order_scan
from src.order_dedupe import (
    apply_dedupe,
    dedupe_records_by_order_number,
    drop_duplicate_orders,
    find_stored_duplicates,
    item_key,
    merge_item_into,
    order_signature,
)
from src.order_scan import _normalise


def _item(eid, idx, name, size="M", *, color="", qty=1, date="2026-08-12",
          domain="kitsunethreads.com", shop="Kitsune Threads", **extra):
    it = {
        "id": f"{eid}-{idx}", "shop": shop, "shop_domain": domain,
        "item_name": name, "size": size, "color": color, "qty": qty,
        "purchased_at": date, "order_email_id": eid,
        "fit_review": None, "watchlist_match": None,
        "image_url": None, "product_url": None,
    }
    it.update(extra)
    return it


def _order(eid, *, date="2026-08-12", domain="kitsunethreads.com", **extra):
    """A 5-line order (three sizes of one tee + two other items)."""
    lines = [("Ronin Akira Tee", "S"), ("Ronin Akira Tee", "M"),
             ("Ronin Akira Tee", "L"), ("Infinite Blade Hoodie", "M"),
             ("Chain Hunter Tee", "L")]
    return [_item(eid, i, n, s, date=date, domain=domain, **extra)
            for i, (n, s) in enumerate(lines)]


def _record(eid, number, *, date="2026-09-15", domain="moonfox.store"):
    return {"email_id": eid, "order_number": number,
            "purchased_at": date, "shop_domain": domain}


# ---------------------------------------------------------------------------
# Keys / signatures
# ---------------------------------------------------------------------------

class TestSignature:
    def test_order_independent(self):
        a = _order("100")
        assert order_signature(a) == order_signature(list(reversed(_order("200"))))

    def test_normalises_case_and_punctuation(self):
        assert item_key({"item_name": "Ronin Akira | T-shirt", "size": " l "}) == \
            item_key({"item_name": "ronin akira t shirt", "size": "L"})

    def test_uses_original_name_when_renamed(self):
        edited = {"item_name": "My nicer name", "item_name_original": "Ronin Akira Tee",
                  "size": "M"}
        assert item_key(edited) == item_key({"item_name": "Ronin Akira Tee", "size": "M"})

    def test_multiset_counts_repeated_lines(self):
        one = [_item("1", 0, "Tee", "M")]
        two = [_item("2", 0, "Tee", "M"), _item("2", 1, "Tee", "M")]
        assert order_signature(one) != order_signature(two)


# ---------------------------------------------------------------------------
# Pre-Claude: (domain, order number)
# ---------------------------------------------------------------------------

class TestDedupeRecordsByOrderNumber:
    def test_resent_confirmation_collapses_to_earliest(self):
        # Same "Order 4821 confirmed" email re-sent a day later.
        later = _record("200", "4821", date="2026-09-16")
        earlier = _record("100", "4821", date="2026-09-15")
        kept, dups = dedupe_records_by_order_number([later, earlier])
        assert kept == [earlier]
        assert dups == [later]

    def test_case_insensitive_number_and_domain(self):
        a = _record("100", "abc123", domain="MoonFox.store")
        b = _record("200", "ABC123")
        kept, dups = dedupe_records_by_order_number([a, b])
        assert [r["email_id"] for r in kept] == ["100"]

    def test_different_numbers_both_kept(self):
        a, b = _record("100", "4821"), _record("200", "4822")
        kept, dups = dedupe_records_by_order_number([a, b])
        assert kept == [a, b] and dups == []

    def test_same_number_different_shop_both_kept(self):
        a = _record("100", "1001", domain="moonfox.store")
        b = _record("200", "1001", domain="otherpeak.com")
        kept, dups = dedupe_records_by_order_number([a, b])
        assert len(kept) == 2 and dups == []

    def test_missing_number_passes_through(self):
        a, b = _record("100", None), _record("200", None)
        kept, dups = dedupe_records_by_order_number([a, b])
        assert len(kept) == 2 and dups == []

    def test_already_stored_order_number_is_duplicate(self):
        stored = [_item("100", 0, "Jacket", domain="moonfox.store", order_number="4821")]
        kept, dups = dedupe_records_by_order_number([_record("200", "4821")], stored)
        assert kept == [] and [r["email_id"] for r in dups] == ["200"]

    def test_reprocessing_the_stored_email_is_not_a_duplicate(self):
        stored = [_item("100", 0, "Jacket", domain="moonfox.store", order_number="4821")]
        rec = _record("100", "4821")
        kept, dups = dedupe_records_by_order_number([rec], stored)
        assert kept == [rec] and dups == []


# ---------------------------------------------------------------------------
# Post-extraction: same items, same shop, close dates
# ---------------------------------------------------------------------------

class TestDropDuplicateOrders:
    def test_two_systems_one_purchase_within_batch(self):
        receipt = _order("1000", order_number="7710")
        marketing = _order("1001")            # no usable order number
        kept, dropped = drop_duplicate_orders(receipt + marketing)
        assert {it["order_email_id"] for it in kept} == {"1000"}
        assert dropped == [("1001", "1000")]

    def test_earliest_wins_regardless_of_input_order(self):
        kept, dropped = drop_duplicate_orders(_order("1001") + _order("1000"))
        assert {it["order_email_id"] for it in kept} == {"1000"}
        assert dropped == [("1001", "1000")]

    def test_duplicate_of_stored_order_dropped(self):
        stored = _order("1000")
        kept, dropped = drop_duplicate_orders(_order("1001", date="2026-08-13"), stored)
        assert kept == [] and dropped == [("1001", "1000")]

    def test_outside_window_is_a_new_purchase(self):
        kept, dropped = drop_duplicate_orders(_order("1001", date="2026-08-20"), _order("1000"))
        assert len(kept) == 5 and dropped == []

    def test_conflicting_order_numbers_are_two_orders(self):
        a = _order("1000", order_number="5001")
        b = _order("1001", order_number="5002")
        kept, dropped = drop_duplicate_orders(a + b)
        assert len(kept) == 10 and dropped == []

    def test_different_size_is_not_a_duplicate(self):
        a = [_item("1000", 0, "Ronin Akira Tee", "M")]
        b = [_item("1001", 0, "Ronin Akira Tee", "L")]
        kept, dropped = drop_duplicate_orders(a + b)
        assert len(kept) == 2 and dropped == []

    def test_subset_is_not_a_duplicate(self):
        full = _order("1000")
        partial = _order("1001")[:3]
        kept, dropped = drop_duplicate_orders(full + partial)
        assert len(kept) == 8 and dropped == []

    def test_different_shop_is_not_a_duplicate(self):
        kept, dropped = drop_duplicate_orders(
            _order("1000") + _order("1001", domain="otherpeak.com"))
        assert len(kept) == 10 and dropped == []

    def test_missing_date_is_never_a_duplicate(self):
        kept, dropped = drop_duplicate_orders(_order("1000", date="") + _order("1001", date=""))
        assert len(kept) == 10 and dropped == []

    def test_same_email_already_stored_is_left_to_merge_items(self):
        # A --reprocess re-scan re-extracts an email that's already stored; the
        # id-dedupe in _merge_items handles that, so don't drop it here.
        kept, dropped = drop_duplicate_orders(_order("1000"), _order("1000"))
        assert len(kept) == 5 and dropped == []


# ---------------------------------------------------------------------------
# Cleanup of already-stored duplicates
# ---------------------------------------------------------------------------

class TestMergeItemInto:
    def test_carries_missing_fields_only(self):
        kept = _item("1", 0, "Tee", image_url="keep.jpg")
        dup = _item("2", 0, "Tee", image_url="dup.jpg", product_url="https://x/p",
                    fit_review={"fit": "tts"})
        carried = merge_item_into(kept, dup)
        assert kept["image_url"] == "keep.jpg"
        assert kept["product_url"] == "https://x/p"
        assert kept["fit_review"] == {"fit": "tts"}
        assert set(carried) == {"product_url", "fit_review"}

    def test_removal_decision_on_twin_wins_over_undecided(self):
        kept = _item("1", 0, "Hoodie", watchlist_match={"matched_line": "L", "approved_for_removal": None})
        dup = _item("2", 0, "Hoodie", watchlist_match={"matched_line": "L", "approved_for_removal": True})
        merge_item_into(kept, dup)
        assert kept["watchlist_match"]["approved_for_removal"] is True

    def test_kept_decision_not_overwritten(self):
        kept = _item("1", 0, "Hoodie", watchlist_match={"matched_line": "L", "approved_for_removal": False})
        dup = _item("2", 0, "Hoodie", watchlist_match={"matched_line": "L", "approved_for_removal": True})
        merge_item_into(kept, dup)
        assert kept["watchlist_match"]["approved_for_removal"] is False

    def test_name_edit_carried(self):
        kept = _item("1", 0, "Ronin Akira Tee")
        dup = _item("2", 0, "Akira tee (white)", item_name_original="Ronin Akira Tee",
                    name_edited_at="2026-09-01T00:00:00Z")
        merge_item_into(kept, dup)
        assert kept["item_name"] == "Akira tee (white)"
        assert kept["item_name_original"] == "Ronin Akira Tee"


class TestApplyDedupe:
    def _wardrobe(self):
        keep = _order("1000")
        dup = _order("1001")
        dup[3]["fit_review"] = {"fit": "large"}              # reviewed the twin
        dup[3]["watchlist_match"] = {"matched_line": "line-x", "approved_for_removal": True}
        other = [_item("2000", 0, "Moon Jacket", "L", domain="moonfox.store",
                       shop="Moon Fox", date="2026-09-15")]
        other_dup = [_item("2001", 0, "Moon Jacket", "L", domain="moonfox.store",
                           shop="Moon Fox", date="2026-09-16")]
        return _normalise({
            "items": keep + dup + other + other_dup,
            "watchlist_exclusions": [{"matched_line": "line-x", "item_id": "1001-3"}],
            "scan_state": {"processed_email_ids": {"1000": "t", "1001": "t"}},
        })

    def test_removes_duplicates_keeps_earliest(self):
        w = self._wardrobe()
        reports = apply_dedupe(w)
        assert {it["order_email_id"] for it in w["items"]} == {"1000", "2000"}
        assert len(w["items"]) == 6
        assert [(r["duplicate_email_id"], r["kept_email_id"]) for r in reports] == \
            [("1001", "1000"), ("2001", "2000")]

    def test_carries_review_and_decision_and_repoints_exclusion(self):
        w = self._wardrobe()
        apply_dedupe(w)
        hoodie = next(it for it in w["items"] if it["id"] == "1000-3")
        assert hoodie["fit_review"] == {"fit": "large"}
        assert hoodie["watchlist_match"]["approved_for_removal"] is True
        assert w["watchlist_exclusions"][0]["item_id"] == "1000-3"

    def test_processed_ids_left_alone(self):
        w = self._wardrobe()
        apply_dedupe(w)
        assert set(w["scan_state"]["processed_email_ids"]) == {"1000", "1001"}

    def test_idempotent(self):
        w = self._wardrobe()
        apply_dedupe(w)
        assert apply_dedupe(w) == []

    def test_no_duplicates_is_noop(self):
        w = _normalise({"items": _order("1000")})
        assert apply_dedupe(w) == [] and len(w["items"]) == 5

    def test_find_stored_duplicates(self):
        assert find_stored_duplicates(self._wardrobe()["items"]) == \
            [("1001", "1000"), ("2001", "2000")]


# ---------------------------------------------------------------------------
# order_scan wiring
# ---------------------------------------------------------------------------

def _scan_email(eid, frm, subject, body, date="Wed, 12 Aug 2026 05:03:25 +0000"):
    return {"id": eid, "from": frm, "subject": subject, "body_text": body, "date": date}


_RECEIPT_BODY = ("Kitsune Threads Order 7710 Your order is confirmed. Order Summary "
                 "Ronin Akira Tee M x 1 USD $29.99 Subtotal: $29.99")
_MARKETING_BODY = ("We've received your order and are already working on it. "
                   "Order summary: Ronin Akira Tee M x 1 $29.99. Welcome to the clan!")


def _fake_extract(seen):
    def _extract(claude_input, client=None):
        seen.append([r["email_id"] for r in claude_input])
        return {"orders": [
            {"email_id": r["email_id"],
             "items": [{"name": "Ronin Akira Tee", "size": "M", "qty": 1,
                        "price": {"amount": 29.99, "currency": "USD"}}]}
            for r in claude_input
        ], "usage": None}
    return _extract


def _cfg():
    return SimpleNamespace(gmail_username="me@example.com", gmail_app_password="p",
                           excluded_shops=())


class TestRunScanDedupe:
    def _run(self, monkeypatch, emails, wardrobe=None):
        seen: list = []
        monkeypatch.setattr(order_scan, "_fetch_emails", lambda *a, **k: emails)
        monkeypatch.setattr("src.order_extract.extract_items", _fake_extract(seen))
        items, processed = order_scan._run_scan(
            _cfg(), wardrobe or _normalise({}), datetime(2026, 1, 1, tzinfo=timezone.utc),
            shop_filter=None, max_emails=None, shop_aliases={},
        )
        return items, processed, seen

    def test_resent_email_skipped_before_claude(self, monkeypatch):
        emails = [
            _scan_email("100", "Moon Fox <no-reply@moonfox.store>", "Order 4821 confirmed",
                        "your order 4821 has been received! Order summary Order #4821 "
                        "Moon Jacket - L x1 $60.00 Subtotal: $60.00",
                        date="Tue, 15 Sep 2026 01:17:26 GMT"),
            _scan_email("200", "Moon Fox <no-reply@moonfox.store>", "Order 4821 confirmed",
                        "your order 4821 has been received! Order summary Order #4821 "
                        "Moon Jacket - L x1 $60.00 Subtotal: $60.00",
                        date="Wed, 16 Sep 2026 04:19:26 GMT"),
        ]
        items, processed, seen = self._run(monkeypatch, emails)
        assert seen == [["100"]]                       # only one went to Claude
        assert {it["order_email_id"] for it in items} == {"100"}
        assert set(processed) == {"100", "200"}        # duplicate never re-fetched
        assert items[0]["order_number"] == "4821"      # persisted for later scans

    def test_marketing_twin_dropped_after_extraction(self, monkeypatch):
        emails = [
            _scan_email("1000", "Kitsune Threads <support@kitsunethreads.com>",
                        "Kitsune Threads order confirmed", _RECEIPT_BODY),
            _scan_email("1001", "Kitsune Threads <info@kitsunethreads.com>",
                        "Thanks for your order!", _MARKETING_BODY,
                        date="Wed, 12 Aug 2026 05:04:24 +0000"),
        ]
        items, processed, seen = self._run(monkeypatch, emails)
        assert sorted(seen[0]) == ["1000", "1001"]     # no number on the twin
        assert {it["order_email_id"] for it in items} == {"1000"}
        assert set(processed) == {"1000", "1001"}
        assert "_order_number" not in items[0]

    def test_duplicate_of_already_stored_order_dropped(self, monkeypatch):
        stored = _normalise({"items": [_item(
            "1000", 0, "Ronin Akira Tee", "M", date="2026-08-12",
            order_number="7710")]})
        emails = [_scan_email("1001", "Kitsune Threads <info@kitsunethreads.com>",
                              "Thanks for your order!", _MARKETING_BODY)]
        items, processed, _ = self._run(monkeypatch, emails, stored)
        assert items == [] and processed == ["1001"]


class TestDedupeOrdersCommand:
    def test_dry_run_writes_nothing(self, monkeypatch, capsys):
        writes = []
        monkeypatch.setattr(order_scan, "write_state", lambda *a, **k: writes.append(k))
        w = _normalise({"items": _order("1000") + _order("1001")})
        rc = order_scan._run_dedupe_orders(SimpleNamespace(), {}, w, dry_run=True)
        assert rc == 0 and writes == []
        out = capsys.readouterr().out
        assert "1 duplicate order" in out and "DRY RUN" in out

    def test_apply_writes_pruned_wardrobe(self, monkeypatch):
        writes = []
        monkeypatch.setattr(order_scan, "write_state", lambda *a, **k: writes.append(k))
        w = _normalise({"items": _order("1000") + _order("1001")})
        cfg = SimpleNamespace(gist_id="g", github_token="t")
        rc = order_scan._run_dedupe_orders(cfg, {"prices": {"u": {}}}, w, dry_run=False)
        assert rc == 0 and len(writes) == 1
        assert len(writes[0]["wardrobe"]["items"]) == 5
        assert writes[0]["prices"] == {"u": {}}

    def test_nothing_to_do(self, monkeypatch, capsys):
        monkeypatch.setattr(order_scan, "write_state", lambda *a, **k: 1 / 0)
        w = _normalise({"items": _order("1000")})
        assert order_scan._run_dedupe_orders(SimpleNamespace(), {}, w, dry_run=False) == 0
        assert "No duplicate orders" in capsys.readouterr().out

    def test_flag_parses(self):
        assert order_scan._parse_args(["--dedupe-orders"]).dedupe_orders is True
