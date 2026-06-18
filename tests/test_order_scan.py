"""Tests for src/order_scan.py.

Focus on the pure helpers (classify, watchlist match, shipment linking,
item materialisation, state normalisation, date parsing). IMAP fetch and
the interactive flows are not exercised here — those are smoke-tested
manually per the plan.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from types import SimpleNamespace

from src import bodyspec, order_scan
from src.order_scan import (
    _backfill_target,
    _classify,
    _date_from_header,
    _drop_excluded_items,
    _empty_wardrobe,
    _excerpt,
    _interactive_fit_review,
    _item_id,
    _jaccard,
    _link_shipments_to_orders,
    _links_for_domain,
    _match_product_url,
    _match_watchlist,
    _materialise_items,
    _merge_items,
    _needs_backfill,
    _needs_classify,
    _normalise,
    _run_bodycomp_backfill,
    _run_classify,
    _select_backfill_items,
    _sender_domain,
    _since_from_state,
    _tokens,
)


# ---------------------------------------------------------------------------
# Heuristic classifier
# ---------------------------------------------------------------------------

class TestClassify:
    # Realistic body fragments used to satisfy the body-marker requirement
    # introduced after the first smoke test surfaced ~500 false positives
    # over a 1-year window.
    _ORDER_BODY = "Order #ABC123\nSubtotal: $50.00\nOrder total: $55.00\n"
    _SHIP_BODY = (
        "Your package is on its way.\n"
        "Tracking number: 1Z999AA10123456784\n"
        "Track your package: https://ups.com/track?n=1Z999AA10123456784\n"
    )

    def _em(self, subject="", from_="shop@example.com", body=""):
        return {"subject": subject, "from": from_, "body_text": body}

    def test_order_confirmation(self):
        em = self._em(subject="Order confirmation #1234", body=self._ORDER_BODY)
        assert _classify(em) == "order"

    def test_your_order(self):
        em = self._em(subject="Your order from Norse Projects", body=self._ORDER_BODY)
        assert _classify(em) == "order"

    def test_thanks_for_your_order(self):
        em = self._em(subject="Thanks for your order!", body=self._ORDER_BODY)
        assert _classify(em) == "order"

    def test_we_got_your_order(self):
        em = self._em(subject="We got your order", body=self._ORDER_BODY)
        assert _classify(em) == "order"

    def test_shipping_has_shipped(self):
        em = self._em(subject="Your order has shipped!", body=self._SHIP_BODY)
        assert _classify(em) == "shipping"

    def test_shipping_is_on_its_way(self):
        em = self._em(subject="Your package is on its way", body=self._SHIP_BODY)
        assert _classify(em) == "shipping"

    def test_shipping_tracking(self):
        em = self._em(subject="Tracking info for your shipment", body=self._SHIP_BODY)
        assert _classify(em) == "shipping"

    def test_other_marketing(self):
        assert _classify(self._em(subject="50% off everything!")) == "other"

    def test_other_newsletter(self):
        assert _classify(self._em(subject="New arrivals this week")) == "other"

    def test_body_fallback_for_order(self):
        em = self._em(
            subject="Hi from us",
            body="Hi! Order #ABC123\nSubtotal: $50.00\nOrder total: $55.00",
        )
        assert _classify(em) == "order"

    def test_shipping_beats_order_when_both_match(self):
        # "your order has shipped" — must be classified as shipping, not order.
        em = self._em(subject="Your order has shipped", body=self._SHIP_BODY)
        assert _classify(em) == "shipping"

    def test_ignored_sender(self):
        em = self._em(subject="Your order has shipped",
                      from_="noreply@linkedin.com",
                      body=self._SHIP_BODY)
        assert _classify(em) == "other"

    def test_order_subject_without_body_marker_is_other(self):
        # "Your order awaits" marketing nudge: ORDER_SUBJECT matches but
        # body has no order-receipt structure.
        em = self._em(
            subject="Your order awaits — finish checkout",
            body="We hope you're excited! Don't forget your saved items.",
        )
        assert _classify(em) == "other"

    # --- H&M order confirmations (2026-06-02) ----------------------------
    # H&M's us@delivery.hm.com "Order Confirmation" bodies carry an itemised
    # list + prices but none of the conventional receipt headers (no order #,
    # subtotal, "order summary", "items ordered"), so they fell through the
    # order branch to "other" and never extracted. The "we have received your
    # order" phrase is the body marker that rescues them.
    def test_hm_order_confirmation_received_your_order_body(self):
        em = self._em(
            subject="Order Confirmation",
            from_="H&M <us@delivery.hm.com>",
            body=("We have received your order and will send you another email "
                  "when your package ships. Find detailed information below. "
                  "Regular Fit Tapered Joggers $ 24.99 S Dark gray 1245348003002"),
        )
        assert _classify(em) == "order"

    def test_received_your_order_contraction_variants(self):
        for phrase in ("we've received your order",
                       "We have received your order",
                       "we received your order"):
            em = self._em(subject="Order Confirmation",
                          body=f"Hello! {phrase}. Your items are below.")
            assert _classify(em) == "order", phrase

    def test_hm_shipping_still_beats_order_marker(self):
        # The H&M "on its way" email also literally contains nothing matching
        # the new order marker, and its ship subject must keep winning anyway.
        em = self._em(
            subject="Your order is on its way",
            from_="H&M <us@delivery.hm.com>",
            body=("Good news! Your order is on its way. "
                  "Track your delivery: https://ups.com/track?n=1ZE04806YW23875718 "
                  "1ZE04806YW23875718"),
        )
        assert _classify(em) == "shipping"

    def test_post_purchase_review_request_is_other(self):
        em = self._em(
            subject="How was your order?",
            body=self._ORDER_BODY,  # even with order-receipt structure
        )
        assert _classify(em) == "other"

    def test_rate_your_purchase_is_other(self):
        em = self._em(
            subject="Rate your recent purchase",
            body="Order #123 — please leave a review.",
        )
        assert _classify(em) == "other"

    def test_shipping_subject_without_tracking_marker_is_other(self):
        em = self._em(
            subject="Your order shipment update",
            body="Thanks for ordering! We'll send tracking soon.",
        )
        assert _classify(em) == "other"

    # --- Shipping-update emails that echo the order summary (2026-05-28) -
    # Shopify's standard fulfillment subject is "Shipping update for order
    # #N" and the body echoes the full order summary. The ship regex only
    # matched "shipped"/"shipment", so these fell through to the order path
    # and got re-extracted as duplicate purchases (~42 dupes across
    # several Shopify shops).
    _SHIP_BODY_WITH_ORDER_SUMMARY = (
        "Your order is on its way!\n"
        "Order #14021\nSubtotal: $48.00\nOrder total: $48.00\n"
        "Track your order: https://aftership.com/track/abc123\n"
    )

    def test_shopify_shipping_update_with_order_summary_is_shipping(self):
        em = self._em(
            subject="Shipping update for order #14021",
            body=self._SHIP_BODY_WITH_ORDER_SUMMARY,
        )
        assert _classify(em) == "shipping"

    def test_winging_its_way_subject_is_shipping(self):
        em = self._em(
            subject="Your order is winging its way to you!",
            body=self._SHIP_BODY_WITH_ORDER_SUMMARY,
        )
        assert _classify(em) == "shipping"

    def test_order_confirmed_still_classifies_as_order(self):
        # Guard: the genuine confirmation must stay "order" after the
        # shipping-update broadening above.
        em = self._em(subject="Order #14021 confirmed", body=self._ORDER_BODY)
        assert _classify(em) == "order"

    # --- More re-list patterns surfaced by the dup audit (2026-05-29) ---
    # Each of these echoes the order summary but is not a fresh purchase:
    # a delivery notice, a cancellation, or a status-update re-confirmation.
    def test_order_has_arrived_is_shipping(self):
        em = self._em(subject="Your Azazie Order Has Arrived!", body=self._SHIP_BODY)
        assert _classify(em) == "shipping"

    def test_amazon_successful_cancellation_is_other(self):
        em = self._em(
            subject="Successful cancellation of 1 item from your Amazon.com order",
            body=self._ORDER_BODY,  # cancellation emails re-list the order
        )
        assert _classify(em) == "other"

    def test_shopify_order_updated_is_other(self):
        em = self._em(subject="Order #146015 updated", body=self._ORDER_BODY)
        assert _classify(em) == "other"

    def test_ebay_order_update_is_other(self):
        em = self._em(subject="Order update: Naruto Keyring", body=self._ORDER_BODY)
        assert _classify(em) == "other"

    def test_order_update_does_not_swallow_confirmation(self):
        # Guard: "Order #N confirmed" must NOT match the new update pattern.
        em = self._em(subject="Order #SH-319667 confirmed", body=self._ORDER_BODY)
        assert _classify(em) == "order"

    # --- Cutesy dispatch subjects (2026-06-05) -------------------------
    # A cutesy dispatch email "🛫 Your shipping arc begins, Alex! |
    # Order #7663" uses the bare word "shipping" (not in _SHIP_SUBJECT_RE), so
    # the "Order #7663" in the subject won the order match and the live
    # tracking number got re-extracted as a duplicate, price-less order. A
    # body that proves shipment (transit phrase + hard tracking marker) now
    # overrides the order-subject false positive.
    def test_cutesy_shipping_arc_dispatch_is_shipping(self):
        em = self._em(
            subject="Your shipping arc begins, Alex! | Order #7663",
            from_="Riot Apparel <support@riotapparel.example>",
            body=(
                "Your order is on the way\nOrder #7663\n"
                "Carrier: OnTrac\nTracking number: 1LSCYM0000X80H4\n"
                "Track your shipment here "
                "https://easyordertracking.aftership.com/1LSCYM0000X80H4\n"
            ),
        )
        assert _classify(em) == "shipping"

    def test_oldnavy_ship_notification_is_shipping_not_duplicate_order(self):
        # Old Navy regression (2026-06-13): Gap-family "Ship Notification"
        # emails carry the generic subject "An update to your order #N" and
        # re-list the whole order summary (subtotal + every item). The carrier
        # link is stripped during HTML->text, so the strict-marker override
        # (3b) can't fire — but the past-tense "N items have shipped" phrase
        # plus the tracking number must still classify them as shipping, so
        # they enrich the order instead of re-extracting as a duplicate.
        em = self._em(
            subject="An update to your order #1KGGB5P",
            from_="Old Navy <orders@email.oldnavy.com>",
            body=(
                "Ship Notification & Receipt\nHi Alex,\n"
                "7 items have shipped. Your other items will ship separately.\n"
                "Package 1 (7 of 8 items)\nOrder #1KGGB5P\nSubtotal $51.92\n"
                "Crew-Neck T-Shirt L | In the Navy $6.49\n"
                "Tracking 1Z31350WYW68781988\n"
            ),
        )
        assert _classify(em) == "shipping"

    def test_oldnavy_single_item_has_shipped_is_shipping(self):
        # The split-shipment variant: "1 item has shipped" (singular has).
        em = self._em(
            subject="An update to your order #1KGGB5P",
            from_="Old Navy <orders@email.oldnavy.com>",
            body=(
                "Ship Notification & Receipt\n1 item has shipped.\n"
                "Order #1KGGB5P\nSubtotal $6.49\n"
                "Crew-Neck T-Shirt L | Oatmeal Heather $6.49\n"
                "Shipping Information UPS 92612909841038\n"
            ),
        )
        assert _classify(em) == "shipping"

    def test_oldnavy_order_confirmation_future_ship_stays_order(self):
        # Guard: the original confirmation says items "will ship" (future) and
        # carries no tracking number — even though a 13-digit product SKU could
        # trip the loose ship marker, the absence of a past-tense "have shipped"
        # phrase keeps it an order so its items are extracted.
        em = self._em(
            subject="Order Confirmation #1KGGB5P",
            from_="Old Navy <orders@email.oldnavy.com>",
            body=(
                "Order Confirmation\nYour order #1KGGB5P has been received.\n"
                "We'll send you an email as soon as your order ships. Use that "
                "Ship Notification email as your receipt.\nSubtotal $51.92\n"
                "Crew-Neck T-Shirt 8554280620003 L | In the Navy $6.49\n"
            ),
        )
        assert _classify(em) == "order"

    def test_real_order_confirmation_without_tracking_stays_order(self):
        # Guard: the genuine 5/23 confirmation (item list + "Order #N", no
        # transit phrase, no tracking marker) must keep classifying as order.
        em = self._em(
            subject="Order placed: Quick address check | Order #7663",
            from_="Catgirl Riot <support@catgirlriot.com>",
            body=(
                "Order locked. New gear secured.\nOrder #7663\n"
                "Prep & Dispatch: 3-5 business days\n"
                "Tax Evasion Tank L White $32.99\n"
            ),
        )
        assert _classify(em) == "order"

    def test_transit_phrase_without_tracking_marker_is_not_hijacked(self):
        # Guard: the override needs BOTH halves. A confirmation that merely
        # mentions "on the way" but carries no tracking marker stays an order.
        em = self._em(
            subject="Thanks for your order!",
            body="Your order is on the way soon.\n" + self._ORDER_BODY,
        )
        assert _classify(em) == "order"

    def test_confirmation_promising_future_shipping_stays_order(self):
        # Guard (Suzushii regression): a genuine "Your Order Is Confirmed"
        # whose body says the parcel will ship later ("as soon as your order
        # is on the way") and links the shop's own order-view URL — NOT a
        # carrier — must stay an order. The strict marker ignores the shop URL
        # and the future-tense transit phrase, so it isn't flipped to shipping.
        em = self._em(
            subject="Your Order Is Confirmed",
            from_="Suzushii Clothing <support@suzushiiclothing.com>",
            body=(
                "Thanks for your order!\nOrder #138880\n"
                "Your order has been successfully received and is now being "
                "prepared. You'll receive a shipping confirmation email as soon "
                "as your order is on the way.\n"
                # The base64 auth key contains carrier substrings ('ups','dhl')
                # by chance — the host-anchored carrier pattern must ignore them.
                "View your order https://suzushiiclothing.com/63613862123/"
                "orders/8e3e57/authenticate?key=shcct_T29upsVaGdhlNreA29\n"
            ),
        )
        assert _classify(em) == "order"

    # --- More dispatch/status subjects mis-read as orders (2026-06-05) ---
    # Each tripped _ORDER_SUBJECT_RE via "order #N" and got re-extracted as a
    # duplicate, price-less order. Verified against real Gmail messages.
    def test_coppertist_hasshipped_no_space_is_shipping(self):
        # The missing space in "hasshipped" defeated `has\s+shipped`; \s* fixes
        # it. Body carries a YunExpress tracking number → shipping.
        em = self._em(
            subject="Your COPPERTIST.WU order #101108980 hasshipped！",
            from_='"COPPERTIST.WU" <info@coppertistwu.com>',
            body=("Your COPPERTIST.WU order #101108980 is packed and ready to go\n"
                  "YunExpress tracking number: YT2531201002160206\n"
                  "Mechanical Heart Pendant x 1\n"),
        )
        assert _classify(em) == "shipping"

    def test_fabletics_almost_on_its_way_is_not_order(self):
        # Pre-ship nudge, no tracking yet → "other" (not a duplicate order).
        em = self._em(
            subject="Your Fabletics Order Is Almost on Its Way",
            from_="Fabletics <orders@email.fabletics.com>",
            body="Order #HEA148916\nThe One Jogger\nThe 24-7 Tee\nGet ready!",
        )
        assert _classify(em) == "other"

    def test_shipping_label_created_is_not_order(self):
        # "The shipping label for order #N has been created" notification.
        em = self._em(
            subject="The shipping label for order #7040 has been created",
            body="Order #7040\nMecha Wings Headphone Stand\nWe'll notify you.",
        )
        assert _classify(em) == "other"

    def test_update_regarding_your_order_is_other(self):
        # SparkTrendz "ACTION REQUIRED - Update Regarding Your Order #N" re-lists
        # the order summary but isn't a fresh purchase.
        em = self._em(
            subject="ACTION REQUIRED - Update Regarding Your Order #68312",
            body=self._ORDER_BODY,
        )
        assert _classify(em) == "other"

    def test_fabletics_real_order_confirmation_stays_order(self):
        # Guard: the genuine Fabletics confirmation must still classify as order.
        em = self._em(
            subject="Does This Look Right? Order #HEA148916",
            from_="Fabletics <orders@email.fabletics.com>",
            body="Order #HEA148916\nSubtotal: $59.97\nThe One Jogger\n",
        )
        assert _classify(em) == "order"

    # --- New classifier rules (2026-05-24) -----------------------------

    def test_amazon_ordered_subject(self):
        # Amazon's actual order-confirmation subjects start with "Ordered:".
        # Previously slipped through and we missed the item details.
        em = self._em(
            subject='Ordered: "PROGO USA Men\'s Joggers..." and 4 more items',
            from_='"Amazon.com" <auto-confirm@amazon.com>',
            body=self._ORDER_BODY,
        )
        assert _classify(em) == "order"

    def test_amazon_shipped_colon_subject_is_shipping(self):
        # Was misclassified as "order" because the SHIP_SUBJECT regex was
        # missing 'Shipped:'-with-colon — the trailing \b broke the match.
        em = self._em(
            subject='Shipped: "Pants" and 4 more items',
            from_='"Amazon.com" <shipment-tracking@amazon.com>',
            body=(
                "Your package was shipped!\n"
                "Order #113-1824646-8641068\n"
                "Track package https://www.amazon.com/progress-tracker/package?orderId=X\n"
            ),
        )
        assert _classify(em) == "shipping"

    def test_amazon_shipped_no_tracking_is_other_not_order(self):
        # Even if the body has order markers, a Shipped: subject without a
        # tracking marker is "other" — not order. Previously this fell into
        # the body-marker fallback and got extracted as an order.
        em = self._em(
            subject='Shipped: "Pants"',
            body="Order #123\nSubtotal: $10\n",
        )
        assert _classify(em) == "other"

    def test_payment_failed_subject_is_other(self):
        # Dattehameha-style: "Update: Payment Failed for order 8863".
        # The body has a full order summary but the order was cancelled.
        em = self._em(
            subject="Update: Payment Failed for order 8863",
            body="Payment for your order 8863 was not completed in time.\n"
                 "Order #8863\nSubtotal: $123.25\nTotal: $136.55\n",
        )
        assert _classify(em) == "other"

    def test_payment_failed_body_marker_overrides_order_subject(self):
        # Subject says "Your order" but body shows payment failure.
        em = self._em(
            subject="Your order #8863",
            body="Your order has been cancelled as the payment was not "
                 "completed.\nOrder #8863\nSubtotal: $50.00\n",
        )
        assert _classify(em) == "other"

    def test_note_has_been_added_is_other(self):
        # Shopify's "A note has been added to your order from Dattehameha".
        # Re-confirmation email — includes the full order summary but
        # represents no new purchase event.
        em = self._em(
            subject="A note has been added to your order from Dattehameha",
            body="An update has been added to your order.\nOrder #8869\n"
                 "Subtotal: $123.25\nTotal: $136.55\n",
        )
        assert _classify(em) == "other"

    def test_order_has_been_updated_is_other(self):
        em = self._em(
            subject="Your order has been updated",
            body="Order #X\nSubtotal: $50\n",
        )
        assert _classify(em) == "other"

    def test_has_been_shipped_subject_matches_shipping(self):
        # Previously only "has shipped" matched — "has been shipped" slipped
        # through to the order-body fallback.
        em = self._em(
            subject="Order 8869 has been shipped - Dattehameha",
            body="Your package is on its way.\nTracking: SG32506163186090\n",
        )
        assert _classify(em) == "shipping"

    def test_shipglobal_tracking_url_is_shipping(self):
        em = self._em(
            subject="Your order has been shipped",
            body="Track at https://shipglobal.in/tracking/?awb=SG32506163186090",
        )
        assert _classify(em) == "shipping"

    def test_boxlunch_arriving_soon_is_shipping(self):
        # BoxLunch / narvar delivery update — subject contains "your order"
        # but we should classify as shipping, not order, even though body
        # has an order summary.
        em = self._em(
            subject="Your BoxLunch order is arriving soon!",
            from_='"BoxLunch" <notifications@boxlunch.narvar.com>',
            body="Order Shipped\nOrder Number DL4168778046\n"
                 "TRACK YOUR ORDER at https://boxlunch.narvar.com/track/x\n"
                 "Marvel Spider-Man Jogger\nSize: XL\nQty 1\n",
        )
        assert _classify(em) == "shipping"

    def test_boxlunch_almost_here_is_shipping(self):
        em = self._em(
            subject="Your BoxLunch order is almost here!",
            from_='"BoxLunch" <notifications@boxlunch.narvar.com>',
            body="Out for Delivery\nTrack at https://boxlunch.narvar.com/track\n",
        )
        assert _classify(em) == "shipping"

    def test_order_is_here_delivered_is_shipping(self):
        em = self._em(
            subject="Your BoxLunch Order is here!",
            from_='"BoxLunch" <notifications@boxlunch.narvar.com>',
            body="Order Delivered\nhttps://boxlunch.narvar.com/track\n"
                 "Order #X\nSubtotal: $50\n",
        )
        assert _classify(em) == "shipping"

    def test_collection_is_here_is_other_marketing(self):
        # "Your fall collection is here" is marketing — SHIP_SUBJECT matches
        # but no SHIP_BODY_MARKER → falls through to "other".
        em = self._em(
            subject="Your fall collection is here",
            body="Shop the new arrivals! 25% off this week only.",
        )
        assert _classify(em) == "other"


# ---------------------------------------------------------------------------
# Watchlist matching
# ---------------------------------------------------------------------------

class TestWatchlistMatch:
    def _item(self, shop="Norse Projects", domain="norseprojects.com",
              name="Aros Chino"):
        return {
            "id": "x", "shop": shop, "shop_domain": domain,
            "item_name": name, "watchlist_match": None,
        }

    def test_domain_match(self):
        items = [self._item()]
        watchlist = (
            "Stuff to buy:\n"
            "https://norseprojects.com aros chino\n"
            "https://uniqlo.com linen shirt\n"
        )
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is not None
        assert "norseprojects.com" in items[0]["watchlist_match"]["matched_line"]

    def test_no_match_when_shop_not_present(self):
        items = [self._item()]
        watchlist = "uniqlo linen shirt\napc petit standard\n"
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is None

    def test_picks_best_jaccard_when_multiple_lines_link(self):
        items = [self._item(name="Aros Light Stretch Chino")]
        watchlist = (
            "norseprojects.com hoodie\n"
            "norseprojects.com aros light stretch chino\n"
        )
        _match_watchlist(items, watchlist)
        assert "aros" in items[0]["watchlist_match"]["matched_line"]

    def test_shop_name_substring_match(self):
        items = [self._item(domain="", shop="Norse Projects")]
        watchlist = "Norse Projects — Aros Chino\n"
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is not None


class TestWatchlistMatchNonClothingSection:
    """The Non-clothing section header tags matches with is_clothing=False
    so order_scan can skip fit-review prompts on gadget purchases."""

    def _item(self, shop="Logitech", domain="logitech.com",
              name="G Pro X Superlight"):
        return {
            "id": "x", "shop": shop, "shop_domain": domain,
            "item_name": name, "watchlist_match": None,
        }

    def test_match_above_marker_is_clothing_true(self):
        items = [self._item(shop="Norse Projects",
                            domain="norseprojects.com",
                            name="Aros Chino")]
        watchlist = (
            "Shops and URLs:\n"
            "https://norseprojects.com aros chino\n"
            "Non-clothing Shops and URLs:\n"
            "https://logitech.com/products/g-pro-x-superlight\n"
        )
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"]["is_clothing"] is True
        assert items[0].get("is_clothing") is not False

    def test_match_below_marker_is_clothing_false(self):
        items = [self._item()]
        watchlist = (
            "Shops and URLs:\n"
            "https://norseprojects.com aros chino\n"
            "Non-clothing Shops and URLs:\n"
            "https://logitech.com/products/g-pro-x-superlight\n"
        )
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is not None
        assert items[0]["watchlist_match"]["is_clothing"] is False
        assert items[0]["is_clothing"] is False

    def test_no_marker_defaults_to_clothing_true(self):
        items = [self._item(shop="Norse Projects",
                            domain="norseprojects.com",
                            name="Aros Chino")]
        watchlist = "https://norseprojects.com aros chino\n"
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"]["is_clothing"] is True
        assert items[0].get("is_clothing") is not False


class TestWatchlistMatchRegressionsFromRematch20260525:
    """Specific items from the 2026-05-25 ``rematch_watchlist --dry-run``.

    Of the nine items that the previous (overlap=2, no slug path)
    matcher dropped, the user marked cases 7–9 as legitimate matches
    that should be kept, and cases 1–6 as correctly-dropped junk.
    These tests pin both directions so we never regress.
    """

    def _item(self, shop, domain, name):
        return {
            "id": "x", "shop": shop, "shop_domain": domain,
            "item_name": name, "watchlist_match": None,
        }

    # ----- 7/8/9: should match -----

    def test_case_7_xsekai_sukuna_slug_match(self):
        items = [self._item("Xsekai", "xsekai.com", "Sukuna Oversize tee")]
        watchlist = "https://xsekai.com/collections/jjk/products/sukuna-oversize-tee\n"
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is not None
        assert "sukuna" in items[0]["watchlist_match"]["matched_line"].lower()

    def test_case_8_bosuman_raijin_slug_match(self):
        items = [self._item("Bosuman", "bosuman.com", "Raijin")]
        watchlist = "https://bosuman.com/products/raijin\n"
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is not None

    def test_case_9_pomel_king_of_counters_via_mesh_shorts_alias(self):
        items = [self._item("Pomel", "pomelclothing.com",
                            "KING OF COUNTERS MESH SHORTS")]
        watchlist = (
            "https://pomelclothing.com/products/miyata-mesh-shorts"
            "?variant=12345678901234\n"
        )
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is not None
        assert "miyata-mesh-shorts" in items[0]["watchlist_match"]["matched_line"]

    # ----- 1/4/5/6: Xsekai berserk-cluster, should NOT match -----

    @pytest.mark.parametrize("item_name", [
        "Inuske Oversize Tee",
        "Plus Ultra oversize tee",
        "Inosuke Oversize Tee",
        "Nanami Oversize Tee",
    ])
    def test_cases_1456_xsekai_berserk_url_rejected(self, item_name):
        items = [self._item("Xsekai", "xsekai.com", item_name)]
        watchlist = (
            "https://xsekai.com/collections/berserk/products/berserk-oversize-tee\n"
        )
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is None, (
            f"{item_name!r} should not match a berserk-oversize-tee URL"
        )

    # ----- 2/3: Opthemes character mismatches, should NOT match -----

    def test_case_2_opthemes_rocklee_vs_shikamaru_rejected(self):
        items = [self._item(
            "Opthemes", "opthemes.com",
            "Rock Lee - Naruto Anime Double Printed Vintage Washed Unisex Tee",
        )]
        watchlist = (
            "https://www.opthemes.com/products/shikamaru-nara-unisex-vintage-washed-tee\n"
        )
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is None

    def test_case_3_opthemes_bakugo_vs_law_one_piece_rejected(self):
        items = [self._item(
            "Opthemes", "opthemes.com",
            "Katsuki Bakugo, My Hero Academia Anime Washed Unisex Tee",
        )]
        watchlist = (
            "https://www.opthemes.com/products/law-surgeon-of-death-one-piece-tee\n"
        )
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is None


class TestWatchlistMatchGarmentCategoryGate:
    """Slug-path acceptance now requires that, when the item name names
    a garment category (tee, hoodie, beanie, shorts, ...), the slug
    mention at least one matching garment category. Cross-category
    sibling SKUs from the same shop don't fulfil watchlist intent."""

    def _item(self, shop, domain, name):
        return {
            "id": "x", "shop": shop, "shop_domain": domain,
            "item_name": name, "watchlist_match": None,
        }

    def test_bee_beanie_does_not_match_bee_hoodie_url(self):
        items = [self._item("Shirtz", "shirtz.cool", "The Bee Beanie")]
        watchlist = "https://shirtz.cool/products/the-bee-hoodie\n"
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is None

    def test_shorts_does_not_match_sweatpants_url(self):
        items = [self._item(
            "KillCrew", "killcrew.co",
            "COTTON SHORTS (MID THIGH CUT) DAISY",
        )]
        watchlist = (
            "https://killcrew.co/collections/daisy/products/"
            "heavyweight-lux-daisy-sweatpants-black\n"
        )
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is None

    def test_tee_does_not_match_slug_without_garment_category(self):
        # Pomel IPPO SPAR BACKPRINT TEE bought; watchlist URL is for a
        # different Hajime no Ippo design ("boxing-gloves-hajime-no-ippo")
        # whose slug doesn't say "tee" anywhere. Sibling SKU from the
        # same series but a different design — should not match.
        items = [self._item(
            "Pomel", "pomelclothing.com",
            "IPPO SPAR BACKPRINT TEE",
        )]
        watchlist = "https://pomelclothing.com/products/boxing-gloves-hajime-no-ippo\n"
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is None

    def test_aligned_garment_categories_still_match(self):
        # Sanity: slug-path still works when the slug DOES name a
        # matching garment category. Case 7/8 from the rematch
        # regression — the whole reason the slug path exists.
        items = [
            self._item("Xsekai", "xsekai.com", "Sukuna Oversize tee"),
            self._item("Bosuman", "bosuman.com", "Raijin"),  # no item cat
        ]
        watchlist = (
            "https://xsekai.com/collections/jjk/products/sukuna-oversize-tee\n"
            "https://bosuman.com/products/raijin\n"
        )
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is not None
        assert items[1]["watchlist_match"] is not None

    def test_item_without_garment_category_unaffected_by_gate(self):
        # If the item name has no garment-category word, the gate
        # doesn't fire — single-token slug match still wins.
        items = [self._item("Bosuman", "bosuman.com", "Raijin")]
        # Even with a "hoodie" slug — Raijin item doesn't say "tee" or
        # "shorts" so we can't assert category conflict.
        watchlist = "https://bosuman.com/products/raijin-hoodie\n"
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is not None

    def test_multi_token_overlap_overrides_cat_mismatch(self):
        # Same-design-different-cut: user bought the hoodie version
        # of a "Hinata x Kageyama Limits" design that's listed on the
        # watchlist as the tee SKU. Three shared distinctive tokens
        # (hinata, kageyama, limits) is strong enough evidence to
        # override the tee/hoodie cat mismatch.
        items = [self._item(
            "theanimecollective", "theanimecollective.com",
            "Hinata x Kageyama Limits Hoodie",
        )]
        watchlist = (
            "https://theanimecollective.com/products/"
            "hinata-x-kageyama-limits-tee-kakugo-collection\n"
        )
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is not None


class TestWatchlistMatchFalsePositiveAudit20260615:
    """Audit of the 2026-06-15 digest surfaced two false-positive removals,
    pinned here so we never regress:

      * #3 — a purchase whose name reduces to just the shop name
        ("THE OTISHI 2.0" → {otishi}) matched a bare "Otishi:" section
        header at Jaccard 1.0, because the shop name double-counted as a
        content token. The shop name is the *link* gate, not match evidence.
      * #2 — a design-less generic name ("Baggy Cargo Unisex Pants") matched
        a *specific* design's URL on the shared cut/style tokens alone
        (the distinguishing design "urban flora embroidery" was absent from
        the order email's generic item name). "baggy"/"cargo" are now
        stopwords so cut/style words can't carry a match on their own.
    """

    def _item(self, shop, domain, name):
        return {
            "id": "x", "shop": shop, "shop_domain": domain,
            "item_name": name, "watchlist_match": None,
        }

    def test_shop_named_item_does_not_match_bare_shop_header(self):
        items = [self._item("Otishi", "otishi.com", "THE OTISHI 2.0")]
        watchlist = (
            "Shops and URLs:\n"
            "Otishi:\n"
            "https://otishi.com/products/some-other-sneaker\n"
        )
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is None

    def test_shop_named_item_does_not_match_sibling_product_url(self):
        # With no design token of its own there's nothing to disambiguate
        # on, so it must not latch onto a *different* product from the shop.
        items = [self._item("Otishi", "otishi.com", "THE OTISHI 2.0")]
        watchlist = "https://otishi.com/products/some-other-sneaker\n"
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is None

    def test_generic_cut_name_does_not_match_specific_design_url(self):
        items = [self._item(
            "Streetzen", "streetzen.co", "Baggy Cargo Unisex Pants")]
        watchlist = (
            "https://streetzen.co/collections/frontpage/products/"
            "urban-flora-embroidery-pants-unisex-baggy-cargo\n"
        )
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is None

    def test_shop_named_item_with_design_token_still_matches(self):
        # Stripping the shop name must not break a legit match that also
        # carries a real design token ("wave"/"runner").
        items = [self._item("Otishi", "otishi.com", "Otishi Wave Runner")]
        watchlist = "https://otishi.com/products/otishi-wave-runner\n"
        _match_watchlist(items, watchlist)
        assert items[0]["watchlist_match"] is not None
        assert "wave-runner" in items[0]["watchlist_match"]["matched_line"]


class TestSlugTokens:
    """``_slug_tokens`` extracts the last path segment of every URL in a
    line, splits on hyphens/underscores, and applies the standard
    stopword filter."""

    def test_extracts_slug_and_strips_stopwords(self):
        from src.order_scan import _slug_tokens
        toks = _slug_tokens(
            "https://xsekai.com/collections/jjk/products/sukuna-oversize-tee"
        )
        # oversize+tee in _STOPWORDS, sukuna survives
        assert toks == {"sukuna"}

    def test_handles_query_string(self):
        from src.order_scan import _slug_tokens
        toks = _slug_tokens(
            "https://pomelclothing.com/products/miyata-mesh-shorts?variant=42"
        )
        assert "miyata" in toks and "mesh" in toks
        assert "shorts" not in toks  # stopword

    def test_no_url_yields_empty(self):
        from src.order_scan import _slug_tokens
        assert _slug_tokens("just some text") == set()

    def test_multiple_urls_unioned(self):
        from src.order_scan import _slug_tokens
        toks = _slug_tokens(
            "https://a.com/products/raijin https://a.com/products/fujin"
        )
        assert toks == {"raijin", "fujin"}


# ---------------------------------------------------------------------------
# Jaccard + token helpers
# ---------------------------------------------------------------------------

class TestTokenHelpers:
    def test_tokens_drops_stopwords(self):
        toks = _tokens("the red shirt for me")
        assert "red" in toks
        assert "the" not in toks
        assert "shirt" not in toks  # in stopwords

    def test_tokens_lowercases(self):
        assert _tokens("Aros Chino") == {"aros", "chino"}

    def test_jaccard_identical(self):
        assert _jaccard({"a", "b"}, {"a", "b"}) == 1.0

    def test_jaccard_disjoint(self):
        assert _jaccard({"a"}, {"b"}) == 0.0

    def test_jaccard_empty(self):
        assert _jaccard(set(), {"a"}) == 0.0


# ---------------------------------------------------------------------------
# Shipment linking
# ---------------------------------------------------------------------------

class TestShipmentLink:
    """Shipments are pure-code dicts now (no Claude). Linkage prefers an
    exact ``order_number`` match between the shipping email body and the
    order email body, then falls back to (shop_domain, date-proximity)."""

    def _item(self, oid, shop="Norse Projects", domain="norseprojects.com",
              purchased="2026-04-15", order_number=None):
        return {
            "id": "x", "shop": shop, "shop_domain": domain,
            "purchased_at": purchased,
            "order_email_id": oid, "shipping_email_id": None,
            "shipped_at": None, "tracking_url": None,
            "_order_number": order_number,
        }

    def test_links_via_order_number_match(self):
        items = [
            self._item("gm1", order_number="ABC123"),
            self._item("gm2", order_number="XYZ999"),
        ]
        ships = [{
            "email_id": "gm_ship",
            "shop": "Norse Projects",
            "shop_domain": "norseprojects.com",
            "order_number": "ABC123",
            "tracking_url": "https://ups.com/track",
            "shipped_at": "2026-04-18",
        }]
        _link_shipments_to_orders(items, ships)
        assert items[0]["shipping_email_id"] == "gm_ship"
        assert items[0]["tracking_url"] == "https://ups.com/track"
        assert items[1]["shipping_email_id"] is None

    def test_falls_back_to_domain_plus_date_proximity(self):
        items = [
            self._item("gm_old", purchased="2025-12-01"),
            self._item("gm_new", purchased="2026-04-15"),
        ]
        ships = [{
            "email_id": "gm_ship",
            "shop": "Norse Projects",
            "shop_domain": "norseprojects.com",
            "order_number": None,
            "tracking_url": None,
            "shipped_at": "2026-04-18",
        }]
        _link_shipments_to_orders(items, ships)
        # Newer purchase is closer in date — should be linked.
        assert items[1]["shipping_email_id"] == "gm_ship"
        assert items[0]["shipping_email_id"] is None

    def test_no_match_when_domain_differs(self):
        items = [self._item("gm1", domain="norseprojects.com")]
        ships = [{
            "email_id": "gm_ship",
            "shop": "Uniqlo",
            "shop_domain": "uniqlo.com",
            "order_number": None,
            "tracking_url": None,
            "shipped_at": "2026-04-18",
        }]
        _link_shipments_to_orders(items, ships)
        assert items[0]["shipping_email_id"] is None


# ---------------------------------------------------------------------------
# Item materialisation
# ---------------------------------------------------------------------------

class TestMaterialiseItems:
    """_materialise_items now takes a dict {email_id: meta} where meta
    holds the deterministic fields (shop, shop_domain, purchased_at).
    Claude only contributes the ``items`` array."""

    def test_flattens_extracted_orders(self):
        extracted = [{
            "email_id": "gm1",
            "items": [
                {"name": "Aros Chino", "size": "32", "color": "Black",
                 "qty": 1, "price": {"amount": 120.0, "currency": "USD"}},
                {"name": "Falun Hoodie", "size": "M", "color": "Grey",
                 "qty": 1, "price": {"amount": 200.0, "currency": "USD"}},
            ],
        }]
        meta = {"gm1": {
            "shop": "Norse Projects",
            "shop_domain": "norseprojects.com",
            "purchased_at": "2026-04-15",
        }}
        items = _materialise_items(extracted, meta)
        assert len(items) == 2
        assert items[0]["shop"] == "Norse Projects"
        assert items[0]["shop_domain"] == "norseprojects.com"
        assert items[0]["purchased_at"] == "2026-04-15"
        assert items[0]["item_name"] == "Aros Chino"
        assert items[1]["item_name"] == "Falun Hoodie"
        assert items[0]["fit_review"] is None
        # IDs must be unique even though they came from the same email.
        assert items[0]["id"] != items[1]["id"]

    def test_skips_empty_names(self):
        extracted = [{
            "email_id": "gm1",
            "items": [{"name": ""}, {"name": "Real Item"}],
        }]
        meta = {"gm1": {"shop": "Shop", "shop_domain": "shop.com",
                        "purchased_at": "2026-04-15"}}
        items = _materialise_items(extracted, meta)
        assert len(items) == 1
        assert items[0]["item_name"] == "Real Item"

    def test_defaults_qty_to_1(self):
        extracted = [{
            "email_id": "gm1",
            "items": [{"name": "Thing", "qty": None}],
        }]
        meta = {"gm1": {"shop": "Shop", "shop_domain": "shop.com",
                        "purchased_at": "2026-04-15"}}
        items = _materialise_items(extracted, meta)
        assert items[0]["qty"] == 1

    def test_missing_meta_yields_empty_shop_fields(self):
        # If we somehow get an extracted order with an email_id we don't
        # have meta for, fall back to empty strings rather than raising.
        extracted = [{
            "email_id": "gm_unknown",
            "items": [{"name": "Item"}],
        }]
        items = _materialise_items(extracted, {})
        assert items[0]["shop"] == ""
        assert items[0]["shop_domain"] == ""
        assert items[0]["purchased_at"] == ""

    def test_stores_valid_category_from_extraction(self):
        extracted = [{
            "email_id": "gm1",
            "items": [{"name": "Falun Hoodie", "category": "hoodie"}],
        }]
        meta = {"gm1": {"shop": "Shop", "shop_domain": "shop.com",
                        "purchased_at": "2026-04-15"}}
        items = _materialise_items(extracted, meta)
        assert items[0]["category"] == "hoodie"
        # A garment category leaves is_clothing absent (treated as clothing).
        assert "is_clothing" not in items[0]

    def test_non_clothing_category_sets_is_clothing_false(self):
        extracted = [{
            "email_id": "gm1",
            "items": [{"name": "USB-C Charger", "category": "non_clothing"}],
        }]
        meta = {"gm1": {"shop": "Shop", "shop_domain": "shop.com",
                        "purchased_at": "2026-04-15"}}
        items = _materialise_items(extracted, meta)
        assert items[0]["category"] == "non_clothing"
        assert items[0]["is_clothing"] is False

    def test_unknown_or_missing_category_is_omitted(self):
        extracted = [{
            "email_id": "gm1",
            "items": [
                {"name": "Mystery Item", "category": "bogus"},
                {"name": "No Category Item"},
            ],
        }]
        meta = {"gm1": {"shop": "Shop", "shop_domain": "shop.com",
                        "purchased_at": "2026-04-15"}}
        items = _materialise_items(extracted, meta)
        assert "category" not in items[0]
        assert "category" not in items[1]

    def test_no_links_yields_null_product_url(self):
        extracted = [{"email_id": "gm1", "items": [{"name": "Aros Chino"}]}]
        meta = {"gm1": {"shop": "Shop", "shop_domain": "shop.com",
                        "purchased_at": "2026-04-15"}}
        items = _materialise_items(extracted, meta)
        assert items[0]["product_url"] is None

    def test_matches_product_url_by_slug(self):
        extracted = [{
            "email_id": "gm1",
            "items": [
                {"name": "Sukuna Oversize Tee"},
                {"name": "Gojo Hoodie"},
            ],
        }]
        meta = {"gm1": {"shop": "XSekai", "shop_domain": "xsekai.com",
                        "purchased_at": "2026-04-15"}}
        links = {"gm1": [
            "https://xsekai.com/products/sukuna-oversize-tee",
            "https://xsekai.com/products/gojo-hoodie",
        ]}
        items = _materialise_items(extracted, meta, links)
        assert items[0]["product_url"] == "https://xsekai.com/products/sukuna-oversize-tee"
        assert items[1]["product_url"] == "https://xsekai.com/products/gojo-hoodie"


# ---------------------------------------------------------------------------
# Per-item product URLs (harvest / domain-filter / match)
# ---------------------------------------------------------------------------

class TestMatchProductUrl:
    def test_picks_best_slug_overlap(self):
        cands = [
            "https://x.com/products/raijin-tee",
            "https://x.com/products/fujin-tee",
        ]
        assert _match_product_url("Raijin Tee", cands) == cands[0]

    def test_no_overlap_returns_none(self):
        assert _match_product_url("Raijin", ["https://x.com/products/fujin-tee"]) is None

    def test_empty_inputs(self):
        assert _match_product_url("", ["https://x.com/products/a-tee"]) is None
        assert _match_product_url("Raijin", []) is None

    def test_ambiguous_tie_returns_none(self):
        # Two candidates share the single design token equally → bail out
        # rather than guess (don't link a sibling SKU's page).
        cands = [
            "https://x.com/products/raijin-black-tee",
            "https://x.com/products/raijin-white-tee",
        ]
        assert _match_product_url("Raijin Tee", cands) is None

    def test_garment_category_gate(self):
        # A "tee" item must not slug-match a "-hoodie" URL on one shared token.
        cands = ["https://x.com/products/raijin-hoodie"]
        assert _match_product_url("Raijin Tee", cands) is None

    def test_strong_overlap_crosses_category_gate(self):
        # Two shared design tokens → same-design match across cuts is allowed.
        cands = ["https://x.com/products/hinata-kageyama-limits-hoodie"]
        assert _match_product_url("Hinata Kageyama Limits Tee", cands) == cands[0]


class TestLinksForDomain:
    def test_keeps_same_domain_drops_others(self):
        urls = [
            "https://shop.com/products/a",
            "https://www.shop.com/products/b",
            "https://click.email.shop.com/r/123",  # tracker subdomain — kept
            "https://other.com/products/c",
        ]
        out = _links_for_domain(urls, "shop.com")
        assert "https://shop.com/products/a" in out
        assert "https://www.shop.com/products/b" in out
        assert "https://other.com/products/c" not in out

    def test_strips_www_on_both_sides(self):
        out = _links_for_domain(["https://shop.com/products/a"], "www.shop.com")
        assert out == ["https://shop.com/products/a"]

    def test_keeps_myshopify_backend(self):
        # A tracker-unwrapped Shopify link is on the store's myshopify backend,
        # not the custom domain — keep it (slug-match + liveness gate the rest).
        urls = ["https://kingmnty.myshopify.com/products/x"]
        assert _links_for_domain(urls, "otishi.com") == urls

    def test_no_domain_keeps_all(self):
        urls = ["https://a.com/products/x"]
        assert _links_for_domain(urls, "") == urls


class TestHarvestAnchorUrls:
    @staticmethod
    def _msg(html):
        import email as _email
        raw = (
            "From: shop <no-reply@shop.com>\r\n"
            "Subject: Your order\r\n"
            "Content-Type: text/html; charset=utf-8\r\n\r\n"
        ) + html
        return _email.message_from_string(raw)

    def test_keeps_product_paths_only(self):
        from src.order_scan import _harvest_anchor_urls
        html = """
          <a href="https://shop.com/products/raijin-tee">Raijin Tee</a>
          <a href="https://shop.com/collections/all">Shop all</a>
          <a href="https://shop.com/account">Account</a>
          <a href="https://shop.com/products/raijin-tee">image dup</a>
          <a href="/products/relative">relative skipped</a>
          <a href="https://amazon.com/dp/B0ABC12345">Amazon item</a>
        """
        out = _harvest_anchor_urls(self._msg(html))
        assert out == [
            "https://shop.com/products/raijin-tee",
            "https://amazon.com/dp/B0ABC12345",
        ]

    def test_drops_fragment_keeps_query(self):
        from src.order_scan import _harvest_anchor_urls
        html = '<a href="https://shop.com/products/tee?variant=42#reviews">Tee</a>'
        out = _harvest_anchor_urls(self._msg(html))
        assert out == ["https://shop.com/products/tee?variant=42"]

    def test_unwraps_click_tracker(self):
        from src.order_scan import _harvest_anchor_urls
        # Real-world shape: the product link is wrapped in an ESP tracker.
        html = ('<a href="https://x.r.us-east-1.awstrack.me/L0/'
                'https:%2F%2Fkingmnty.myshopify.com%2Fproducts%2Fcrew-socks-3-pack'
                '%3Fvariant=42/1/0100abc">Crew Socks</a>'
                '<a href="https://otaku.studio/_t/c/v3/AADopaque">opaque</a>')
        out = _harvest_anchor_urls(self._msg(html))
        assert out == ["https://kingmnty.myshopify.com/products/crew-socks-3-pack?variant=42"]

    def test_empty_when_no_html(self):
        from src.order_scan import _harvest_anchor_urls
        import email as _email
        msg = _email.message_from_string(
            "Subject: x\r\nContent-Type: text/plain\r\n\r\nplain body https://shop.com/products/a"
        )
        assert _harvest_anchor_urls(msg) == []


# ---------------------------------------------------------------------------
# Product-URL re-harvest backfill (--reharvest-urls)
# ---------------------------------------------------------------------------

class TestReharvestTargets:
    @staticmethod
    def _it(**kw):
        base = {"id": "x", "item_name": "Tee", "shop_domain": "s.com",
                "order_email_id": "100", "purchased_at": "2026-01-01"}
        base.update(kw)
        return base

    def test_skips_non_clothing_and_no_email_id(self):
        items = [
            self._it(id="a"),
            self._it(id="b", is_clothing=False),
            self._it(id="c", order_email_id=None),
        ]
        out = order_scan._reharvest_targets(items, refresh=False, limit=None, since=None)
        assert [i["id"] for i in out] == ["a"]

    def test_skips_already_stamped_unless_refresh(self):
        items = [self._it(id="a", product_url="https://s.com/products/x"),
                 self._it(id="b")]
        out = order_scan._reharvest_targets(items, refresh=False, limit=None, since=None)
        assert [i["id"] for i in out] == ["b"]
        refreshed = order_scan._reharvest_targets(items, refresh=True, limit=None, since=None)
        assert {i["id"] for i in refreshed} == {"a", "b"}

    def test_since_and_limit_newest_first(self):
        items = [self._it(id="old", purchased_at="2024-01-01"),
                 self._it(id="new", purchased_at="2026-06-01"),
                 self._it(id="mid", purchased_at="2025-06-01")]
        out = order_scan._reharvest_targets(items, refresh=False, limit=2, since="2025-01-01")
        assert [i["id"] for i in out] == ["new", "mid"]


class TestUnwrapTrackingUrl:
    def test_aws_ses_path_embedded(self):
        href = ("https://abc.r.us-east-1.awstrack.me/L0/"
                "https:%2F%2Fkingmnty.myshopify.com%2Fproducts%2Fcrew-socks-3-pack"
                "%3Fvariant=42/1/0100abc")
        assert order_scan._unwrap_tracking_url(href) == (
            "https://kingmnty.myshopify.com/products/crew-socks-3-pack?variant=42")

    def test_query_param_url(self):
        href = "https://track.esp.com/click?url=https%3A%2F%2Fshop.com%2Fproducts%2Ftee&id=9"
        assert order_scan._unwrap_tracking_url(href) == "https://shop.com/products/tee"

    def test_opaque_tracker_unrecoverable(self):
        href = "https://otaku.studio/_t/c/v3/AADmBoE63VUd5St4IXBy1OO5"
        assert order_scan._unwrap_tracking_url(href) is None

    def test_plain_url_has_nothing_to_unwrap(self):
        assert order_scan._unwrap_tracking_url("https://shop.com/products/tee") is None


class TestCleanProductUrl:
    def test_strips_utm_and_pii_keeps_variant(self):
        url = ("https://shop.com/products/tee?variant=42&utm_source=redo"
               "&utm_contact=eyJlbWFpbCI6Im1ta2R1ZGVAZ21haWwuY29tIn0%3D#hero")
        assert order_scan._clean_product_url(url) == "https://shop.com/products/tee?variant=42"

    def test_no_query_unchanged(self):
        assert order_scan._clean_product_url(
            "https://shop.com/products/tee") == "https://shop.com/products/tee"

    def test_none_passes_through(self):
        assert order_scan._clean_product_url(None) is None


class TestResolveIfLive:
    class _Resp:
        def __init__(self, status, url):
            self.status_code = status
            self.url = url

    class _Client:
        def __init__(self, resp=None, exc=None):
            self._resp, self._exc = resp, exc

        def get(self, url):
            if self._exc:
                raise self._exc
            return self._resp

    def test_live_returns_final_url(self):
        # myshopify link redirects to the custom domain → store the canonical one.
        c = self._Client(self._Resp(200, "https://shop.com/products/x"))
        assert order_scan._resolve_if_live(
            "https://shop.myshopify.com/products/x", c) == "https://shop.com/products/x"

    def test_404_is_dead(self):
        c = self._Client(self._Resp(404, "https://s.com/products/x"))
        assert order_scan._resolve_if_live("https://s.com/products/x", c) is None

    def test_redirect_to_homepage_is_dead(self):
        c = self._Client(self._Resp(200, "https://s.com/"))
        assert order_scan._resolve_if_live("https://s.com/products/x", c) is None

    def test_network_error_is_dead(self):
        c = self._Client(exc=RuntimeError("boom"))
        assert order_scan._resolve_if_live("https://s.com/products/x", c) is None


class TestRunReharvest:
    @staticmethod
    def _wardrobe():
        return {"items": [
            {"id": "1", "item_name": "Raijin Tee", "shop_domain": "bosuman.com",
             "order_email_id": "100", "purchased_at": "2026-01-01"},
            {"id": "2", "item_name": "Fujin Hoodie", "shop_domain": "bosuman.com",
             "order_email_id": "100", "purchased_at": "2026-01-02"},
            {"id": "3", "item_name": "Mystery", "shop_domain": "bosuman.com",
             "order_email_id": "200", "purchased_at": "2026-01-03"},
        ]}

    @staticmethod
    def _patch_fetch(monkeypatch, links):
        monkeypatch.setattr(order_scan, "_fetch_product_links_by_msgids",
                            lambda cfg, msgids, **kw: links)

    def test_stamps_live_skips_dead(self, monkeypatch):
        w = self._wardrobe()
        self._patch_fetch(monkeypatch, {
            "100": ["https://bosuman.com/products/raijin-tee",
                    "https://bosuman.com/products/fujin-hoodie"],
            "200": ["https://bosuman.com/products/mystery"],
        })
        # Fujin's page is dead (None); the other two resolve live (final URL).
        validator = lambda urls: {u: (u if "fujin" not in u else None) for u in urls}
        stats = order_scan._run_reharvest_urls(None, w, url_validator=validator)
        assert stats == {"targeted": 3, "emails": 2, "matched": 3,
                         "stamped": 2, "dead": 1, "no_candidate": 0}
        by_id = {it["id"]: it for it in w["items"]}
        assert by_id["1"]["product_url"] == "https://bosuman.com/products/raijin-tee"
        assert by_id["3"]["product_url"] == "https://bosuman.com/products/mystery"
        # The dead one is left unstamped → browser keeps the search link, and a
        # rerun re-selects it (no product_url) so it can be re-tried later.
        assert "product_url" not in by_id["2"]
        rerun_targets = order_scan._reharvest_targets(
            w["items"], refresh=False, limit=None, since=None)
        assert [t["id"] for t in rerun_targets] == ["2"]

    def test_no_validate_stamps_all_matches(self, monkeypatch):
        w = self._wardrobe()
        self._patch_fetch(monkeypatch, {
            "100": ["https://bosuman.com/products/raijin-tee",
                    "https://bosuman.com/products/fujin-hoodie"],
            "200": ["https://bosuman.com/products/mystery"],
        })
        stats = order_scan._run_reharvest_urls(None, w, validate=False)
        assert stats["stamped"] == 3
        assert stats["dead"] == 0

    def test_no_candidate_counted(self, monkeypatch):
        w = self._wardrobe()
        # No harvested link matches the item names.
        self._patch_fetch(monkeypatch, {"100": [], "200": []})
        stats = order_scan._run_reharvest_urls(None, w, validate=False)
        assert stats["matched"] == 0
        assert stats["no_candidate"] == 3
        assert stats["stamped"] == 0

    def test_empty_targets_short_circuits(self, monkeypatch):
        called = {"n": 0}
        def _fetch(*a, **k):
            called["n"] += 1
            return {}
        monkeypatch.setattr(order_scan, "_fetch_product_links_by_msgids", _fetch)
        w = {"items": [{"id": "1", "is_clothing": False, "order_email_id": "1"}]}
        stats = order_scan._run_reharvest_urls(None, w)
        assert stats["targeted"] == 0
        assert called["n"] == 0  # never hit Gmail when nothing's pending


class TestFetchProductLinksByMsgids:
    """The re-fetch helper must select All Mail, not INBOX — old order emails
    (2023–2024 purchases) are usually archived, and an INBOX-scoped X-GM-MSGID
    search would silently miss them, leaving those items with no candidate."""

    @staticmethod
    def _html_message(href: str) -> bytes:
        return (
            "Subject: Your order has shipped\r\n"
            "From: shop@bosuman.com\r\n"
            "Date: Sun, 19 May 2026 14:00:00 +0000\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            f'<html><body><a href="{href}">View item</a></body></html>\r\n'
        ).encode("utf-8")

    class _FakeIMAP:
        def __init__(self, uid_by_msgid, body_by_uid):
            self._uid_by_msgid = uid_by_msgid  # {msgid_str: uid_bytes}
            self._body_by_uid = body_by_uid    # {uid_bytes: raw_bytes}
            self.calls = []

        def select(self, mailbox, readonly=False):
            self.calls.append(("select", mailbox, readonly))
            return ("OK", [b""])

        def uid(self, command, *args):
            self.calls.append(("uid", command, args))
            if command == "SEARCH":
                uid = self._uid_by_msgid.get(args[-1])
                return ("OK", [uid or b""])
            if command == "FETCH":
                uid = args[0]
                body = self._body_by_uid.get(uid)
                if body is None:
                    return ("NO", [b""])
                meta = b"%b (X-GM-MSGID 999 BODY[] {500}" % uid
                return ("OK", [(meta, body), b")"])
            return ("BAD", [b"?"])

        def logout(self):
            self.calls.append(("logout",))

    def test_selects_all_mail_and_harvests(self):
        fake = self._FakeIMAP(
            uid_by_msgid={"100": b"7"},
            body_by_uid={b"7": self._html_message(
                "https://bosuman.com/products/raijin-tee")},
        )
        cfg = SimpleNamespace(gmail_username="u", gmail_app_password="p")
        out = order_scan._fetch_product_links_by_msgids(cfg, ["100"], imap_client=fake)
        assert out == {"100": ["https://bosuman.com/products/raijin-tee"]}
        # The whole point of the fix: select All Mail (quoted — the name has a
        # space), readonly so reads don't mark messages seen.
        select_call = [c for c in fake.calls if c[0] == "select"][0]
        assert select_call[1] == '"[Gmail]/All Mail"'
        assert select_call[2] is True

    def test_missing_message_is_skipped(self):
        # An msgid with no search hit yields no entry, not a crash.
        fake = self._FakeIMAP(uid_by_msgid={}, body_by_uid={})
        cfg = SimpleNamespace(gmail_username="u", gmail_app_password="p")
        out = order_scan._fetch_product_links_by_msgids(cfg, ["404"], imap_client=fake)
        assert out == {}


# ---------------------------------------------------------------------------
# Wardrobe normalisation
# ---------------------------------------------------------------------------

class TestNormalise:
    def test_none_yields_empty(self):
        w = _normalise(None)
        assert w["items"] == []
        assert w["watchlist_exclusions"] == []
        assert w["scan_state"]["processed_email_ids"] == {}
        assert w["scan_state"]["last_scanned_at"] is None

    def test_preserves_existing_fields(self):
        existing = {
            "items": [{"id": "x", "item_name": "Y"}],
            "scan_state": {"processed_email_ids": {"gm1": "2026-01-01"}},
            "watchlist_exclusions": [{"matched_line": "z"}],
        }
        w = _normalise(existing)
        assert w["items"][0]["item_name"] == "Y"
        assert "gm1" in w["scan_state"]["processed_email_ids"]
        assert w["watchlist_exclusions"][0]["matched_line"] == "z"


# ---------------------------------------------------------------------------
# Since calculation
# ---------------------------------------------------------------------------

class TestSinceFromState:
    def test_explicit_override_wins(self):
        wardrobe = _normalise({"scan_state": {"last_scanned_at": "2026-01-01T00:00:00+00:00"}})
        override = datetime(2025, 6, 1, tzinfo=timezone.utc)
        assert _since_from_state(wardrobe, override) == override

    def test_uses_last_scanned_at_when_present(self):
        wardrobe = _normalise({"scan_state": {"last_scanned_at": "2026-01-15T12:00:00+00:00"}})
        result = _since_from_state(wardrobe, None)
        assert result.year == 2026
        assert result.month == 1
        assert result.day == 15

    def test_defaults_to_3_years_when_empty(self):
        wardrobe = _normalise(None)
        result = _since_from_state(wardrobe, None)
        delta = datetime.now(timezone.utc) - result
        # Allow a day of jitter around the 3-year mark.
        assert timedelta(days=365 * 3 - 1) < delta < timedelta(days=365 * 3 + 2)


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

class TestMisc:
    def test_item_id_stable_and_unique(self):
        assert _item_id("gm1", 0) == _item_id("gm1", 0)
        assert _item_id("gm1", 0) != _item_id("gm1", 1)
        assert _item_id("gm1", 0) != _item_id("gm2", 0)
        assert len(_item_id("gm1", 0)) == 12

    def test_excerpt_truncates(self):
        text = "x " * 5000
        out = _excerpt(text)
        assert out.endswith("...[truncated]")

    def test_excerpt_collapses_whitespace(self):
        assert _excerpt("a   b\n\nc") == "a b c"

    def test_sender_domain(self):
        assert _sender_domain("Shop <hi@norseprojects.com>") == "norseprojects.com"
        assert _sender_domain("malformed") == ""

    def test_date_from_header(self):
        # Standard RFC 2822 date.
        assert _date_from_header("Sat, 15 Apr 2026 12:00:00 -0500") == "2026-04-15"

    def test_date_from_header_empty(self):
        assert _date_from_header("") == ""

    def test_merge_items_dedupes(self):
        existing = [{"id": "a", "name": "old"}]
        new = [{"id": "a", "name": "shouldnt-overwrite"}, {"id": "b", "name": "new"}]
        merged = _merge_items(existing, new)
        ids = [it["id"] for it in merged]
        assert ids == ["a", "b"]
        assert merged[0]["name"] == "old"


# ---------------------------------------------------------------------------
# State round-trip (uses pytest-httpx for the Gist API)
# ---------------------------------------------------------------------------

class TestStateRoundTrip:
    def test_wardrobe_written_when_provided(self, httpx_mock):
        import json
        from src.state import write_state

        httpx_mock.add_response(
            method="PATCH",
            url="https://api.github.com/gists/test_gist",
            json={"files": {}},
        )
        wardrobe = _empty_wardrobe()
        wardrobe["items"].append({"id": "x", "item_name": "Y"})
        write_state(
            "test_gist", "tok",
            prices={}, aliases={}, codes=[],
            wardrobe=wardrobe,
        )
        req = httpx_mock.get_requests()[0]
        body = json.loads(req.content)
        assert "wardrobe.json" in body["files"]
        content = json.loads(body["files"]["wardrobe.json"]["content"])
        assert content["items"][0]["item_name"] == "Y"

    def test_wardrobe_none_skips_writing(self, httpx_mock):
        import json
        from src.state import write_state

        httpx_mock.add_response(
            method="PATCH",
            url="https://api.github.com/gists/test_gist",
            json={"files": {}},
        )
        write_state("test_gist", "tok", prices={}, aliases={}, codes=[], wardrobe=None)
        body = json.loads(httpx_mock.get_requests()[0].content)
        assert "wardrobe.json" not in body["files"]

    def test_wardrobe_read_back(self, httpx_mock):
        import json
        from src.state import read_state

        wardrobe = {"items": [{"id": "x"}], "scan_state": {}, "watchlist_exclusions": []}
        httpx_mock.add_response(
            url="https://api.github.com/gists/test_gist",
            json={"files": {"wardrobe.json": {"content": json.dumps(wardrobe)}}},
        )
        state = read_state("test_gist", "tok")
        assert state["wardrobe"] == wardrobe

    def test_missing_wardrobe_file_returns_empty(self, httpx_mock):
        from src.state import read_state

        httpx_mock.add_response(
            url="https://api.github.com/gists/test_gist",
            json={"files": {}},
        )
        state = read_state("test_gist", "tok")
        assert state["wardrobe"] == {}


class TestFitReviewSkipsNonClothing:
    """``_interactive_fit_review`` returns before importing questionary
    when every pending item is flagged is_clothing=False."""

    def _item(self, **overrides) -> dict:
        base = {
            "id": "x",
            "shop": "Logitech",
            "item_name": "G Pro X Superlight",
            "fit_review": None,
        }
        base.update(overrides)
        return base

    def test_non_clothing_items_not_prompted(self, monkeypatch):
        # If questionary were touched, importing the broken stub would
        # blow up — proves the function short-circuited.
        import sys
        sentinel = object()
        monkeypatch.setitem(sys.modules, "questionary", sentinel)
        items = [self._item(is_clothing=False)]
        _interactive_fit_review(items)  # must not raise
        assert items[0]["fit_review"] is None

    def test_mixed_pool_still_processes_clothing(self, monkeypatch):
        # Mixed pool: one non-clothing (should be ignored) and one
        # clothing (should hit questionary). We swap questionary for a
        # stub that records the call and answers "skip" so the function
        # returns without trying to actually prompt.
        calls: list[str] = []

        class _Choice:
            def __init__(self, title=None, value=None, **_):
                self.title, self.value = title, value

        class _Ask:
            def __init__(self, value): self._value = value
            def ask(self): return self._value

        class _Stub:
            Choice = _Choice
            @staticmethod
            def select(prompt, choices=None, **_):
                calls.append("select")
                return _Ask("skip")
            @staticmethod
            def text(prompt, **_):
                calls.append("text")
                return _Ask("")

        import sys
        monkeypatch.setitem(sys.modules, "questionary", _Stub)
        items = [
            self._item(id="mouse", is_clothing=False),
            self._item(id="tee", shop="Aniqi", item_name="Aros Chino", size="M"),
        ]
        _interactive_fit_review(items)
        # questionary.select fires exactly once — only for the clothing item.
        assert calls == ["select"]


# ---------------------------------------------------------------------------
# BodySpec body-comp backfill
# ---------------------------------------------------------------------------

_FAKE_COMPOSITION = {
    "result_id": "R1",
    "total": {
        "fat_mass_kg": 14.1, "lean_mass_kg": 58.0, "bone_mass_kg": 3.1,
        "total_mass_kg": 75.2, "tissue_fat_pct": 17.9, "region_fat_pct": 18.4,
    },
    "regions": {"trunk": {"lean_mass_kg": 17.3}},
    "android_gynoid_ratio": 0.91,
}


def _wardrobe_item(**kw):
    base = {
        "id": kw.get("id", "x"),
        "shop": "Aniqi",
        "item_name": "Aros Chino",
        "size": "M",
        "purchased_at": "2026-04-15",
    }
    base.update(kw)
    return base


class TestSelectBackfillItems:
    def test_skips_non_clothing_and_unparseable_dates(self):
        items = [
            _wardrobe_item(id="a", purchased_at="2026-04-15"),
            _wardrobe_item(id="rug", is_clothing=False),
            _wardrobe_item(id="nodate", purchased_at=""),
        ]
        got = _select_backfill_items(items, limit=100, refresh=False)
        assert [it["id"] for it in got] == ["a"]

    def test_orders_newest_first_and_respects_limit(self):
        items = [
            _wardrobe_item(id="old", purchased_at="2025-01-01"),
            _wardrobe_item(id="new", purchased_at="2026-05-01"),
            _wardrobe_item(id="mid", purchased_at="2026-02-01"),
        ]
        got = _select_backfill_items(items, limit=2, refresh=False)
        assert [it["id"] for it in got] == ["new", "mid"]

    def test_skips_already_stamped_unless_refresh(self):
        items = [
            _wardrobe_item(id="done", body_comp={"result_id": "R0"}),
            _wardrobe_item(id="todo"),
        ]
        assert [it["id"] for it in _select_backfill_items(items, 100, False)] == ["todo"]
        # --refresh re-includes the already-stamped item.
        assert {it["id"] for it in _select_backfill_items(items, 100, True)} == {"done", "todo"}

    def test_skips_homeware_by_name(self):
        items = [
            _wardrobe_item(id="quilt", item_name="Organic Airy Gauze Dream Quilt"),
            _wardrobe_item(id="towel", item_name="Turkish Waffle Terry Bath Towel (Set of 2)"),
            _wardrobe_item(id="pillowcase", item_name="Queen Pillow Cases Set of 2"),
            _wardrobe_item(id="throwpillow", item_name="OtGalk Flower Throw Pillow"),
            _wardrobe_item(id="blanket", item_name="Chunky Knit Blanket Throw"),
            _wardrobe_item(id="tee", item_name="Attack On Titan Scout Tank"),
        ]
        assert [it["id"] for it in _select_backfill_items(items, 100, False)] == ["tee"]

    def test_homeware_filter_keeps_garments_with_substring_collisions(self):
        # Word-boundary matching: "quilted"/"throwback" are garments, not homeware.
        items = [
            _wardrobe_item(id="jacket", item_name="Quilted Bomber Jacket"),
            _wardrobe_item(id="throwback", item_name="Throwback Logo Hoodie"),
            _wardrobe_item(id="socks", item_name="COOVAN Ankle Socks 12 Pack"),
        ]
        assert {it["id"] for it in _select_backfill_items(items, 100, False)} == {
            "jacket", "throwback", "socks",
        }


# ---------------------------------------------------------------------------
# Category classification backfill (issue #18)
# ---------------------------------------------------------------------------

class TestNeedsClassify:
    def test_unclassified_needs_it(self):
        assert _needs_classify({}, refresh=False) is True

    def test_valid_category_skipped(self):
        assert _needs_classify({"category": "hoodie"}, refresh=False) is False

    def test_invalid_stored_category_needs_it(self):
        assert _needs_classify({"category": "bogus"}, refresh=False) is True

    def test_refresh_forces_it(self):
        assert _needs_classify({"category": "hoodie"}, refresh=True) is True


class TestRunClassify:
    """``_run_classify`` with a stubbed ``classify_items`` (no Anthropic)."""

    @staticmethod
    def _cfg():
        return SimpleNamespace(gist_id="g", github_token="t")

    def _stub(self, monkeypatch, name_to_cat, recorder=None):
        """Patch order_classify.classify_items to answer each input by name."""
        def fake(inputs, *, client=None, batch_size=None, **_):
            if recorder is not None:
                recorder.extend(inputs)
            return {
                "results": [
                    {"item_id": it["item_id"],
                     "category": name_to_cat.get(it["name"], "other")}
                    for it in inputs
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        monkeypatch.setattr(order_scan.order_classify, "classify_items", fake)

    def test_stamps_categories_and_derives_is_clothing(self, monkeypatch):
        self._stub(monkeypatch, {
            "Kitsune": "tshirt",
            "Hyken Task Chair": "non_clothing",
        })
        wardrobe = _normalise({"items": [
            _wardrobe_item(id="a", item_name="Kitsune"),
            _wardrobe_item(id="b", item_name="Hyken Task Chair"),
        ]})
        stats = _run_classify(self._cfg(), wardrobe, refresh=False,
                              anthropic_client=object())
        a = next(it for it in wardrobe["items"] if it["id"] == "a")
        b = next(it for it in wardrobe["items"] if it["id"] == "b")
        assert a["category"] == "tshirt"
        assert "is_clothing" not in a              # garment -> flag left absent
        assert b["category"] == "non_clothing"
        assert b["is_clothing"] is False           # hidden + skipped by nudges
        assert stats["classified"] == 2

    def test_watchlist_non_clothing_stamped_locally_without_claude(self, monkeypatch):
        recorder: list = []
        self._stub(monkeypatch, {}, recorder=recorder)
        wardrobe = _normalise({"items": [
            _wardrobe_item(id="gadget", item_name="Gaming Mouse", is_clothing=False),
        ]})
        stats = _run_classify(self._cfg(), wardrobe, refresh=False,
                              anthropic_client=object())
        # Never sent to Claude — stamped non_clothing from the watchlist flag.
        assert recorder == []
        assert wardrobe["items"][0]["category"] == "non_clothing"
        assert stats["local_non_clothing"] == 1
        assert stats["considered"] == 0

    def test_idempotent_skips_already_classified(self, monkeypatch):
        recorder: list = []
        self._stub(monkeypatch, {"Aros Chino": "pants"}, recorder=recorder)
        wardrobe = _normalise({"items": [
            _wardrobe_item(id="done", category="hoodie"),
        ]})
        stats = _run_classify(self._cfg(), wardrobe, refresh=False,
                              anthropic_client=object())
        assert recorder == []                      # nothing re-sent
        assert wardrobe["items"][0]["category"] == "hoodie"  # untouched
        assert stats["considered"] == 0

    def test_refresh_reclassifies(self, monkeypatch):
        self._stub(monkeypatch, {"Aros Chino": "pants"})
        wardrobe = _normalise({"items": [
            _wardrobe_item(id="done", category="hoodie"),
        ]})
        stats = _run_classify(self._cfg(), wardrobe, refresh=True,
                              anthropic_client=object())
        assert wardrobe["items"][0]["category"] == "pants"
        assert stats["classified"] == 1

    def test_limit_caps_newest_first(self, monkeypatch):
        recorder: list = []
        self._stub(monkeypatch, {}, recorder=recorder)
        wardrobe = _normalise({"items": [
            _wardrobe_item(id="old", purchased_at="2025-01-01"),
            _wardrobe_item(id="new", purchased_at="2026-05-01"),
            _wardrobe_item(id="mid", purchased_at="2026-02-01"),
        ]})
        _run_classify(self._cfg(), wardrobe, refresh=False, limit=2,
                      anthropic_client=object())
        sent = {it["item_id"] for it in recorder}
        assert sent == {"new", "mid"}              # oldest dropped by the cap

    def test_only_category_scopes_to_one_bucket(self, monkeypatch):
        recorder: list = []
        self._stub(monkeypatch, {
            "Mesh Short": "shorts_athletic",
            "Cargo Short": "shorts_casual",
        }, recorder=recorder)
        wardrobe = _normalise({"items": [
            _wardrobe_item(id="s1", item_name="Mesh Short", category="shorts"),
            _wardrobe_item(id="s2", item_name="Cargo Short", category="shorts"),
            _wardrobe_item(id="tee", item_name="Graphic Tee", category="tshirt"),
            _wardrobe_item(id="gadget", item_name="Mouse",
                           category="non_clothing", is_clothing=False),
        ]})
        stats = _run_classify(self._cfg(), wardrobe, refresh=False,
                              only_category="shorts", anthropic_client=object())
        by_id = {it["id"]: it for it in wardrobe["items"]}
        # Only the two `shorts` items were re-sent and retyped.
        assert {it["name"] for it in recorder} == {"Mesh Short", "Cargo Short"}
        assert by_id["s1"]["category"] == "shorts_athletic"
        assert by_id["s2"]["category"] == "shorts_casual"
        # Other buckets — including the hidden non_clothing item — untouched.
        assert by_id["tee"]["category"] == "tshirt"
        assert by_id["gadget"]["category"] == "non_clothing"
        assert by_id["gadget"]["is_clothing"] is False
        assert stats["considered"] == 2 and stats["classified"] == 2
        assert stats["local_non_clothing"] == 0


class TestRunBodycompBackfill:
    @staticmethod
    def _cfg():
        return SimpleNamespace(
            bodyspec_username="me@example.com", bodyspec_password="pw",
            gist_id="g", github_token="t",
        )

    def _patch_bodyspec(self, monkeypatch, scans, comp_calls):
        monkeypatch.setattr(bodyspec, "authenticate", lambda u, p: "tok")
        monkeypatch.setattr(bodyspec, "list_results", lambda tok: scans)

        def _get_comp(tok, rid):
            comp_calls.append(rid)
            return dict(_FAKE_COMPOSITION, result_id=rid)

        monkeypatch.setattr(bodyspec, "get_composition", _get_comp)

    def test_stamps_nearest_scan_and_caches_composition(self, monkeypatch):
        scans = [{"result_id": "R1", "start_time": "2026-04-12T09:00:00Z"}]
        comp_calls = []
        self._patch_bodyspec(monkeypatch, scans, comp_calls)
        wardrobe = _normalise({"items": [
            _wardrobe_item(id="a", purchased_at="2026-04-15"),
            _wardrobe_item(id="b", purchased_at="2026-04-10"),
        ]})
        stats, _cache = _run_bodycomp_backfill(
            self._cfg(), wardrobe, limit=100, max_gap_days=90, refresh=False)
        assert stats == {"considered": 2, "stamped": 2, "skipped_no_scan": 0, "scans_used": 1}
        # Both items map to the same scan → composition fetched once.
        assert comp_calls == ["R1"]
        a = next(it for it in wardrobe["items"] if it["id"] == "a")
        assert a["body_comp"]["result_id"] == "R1"
        assert a["body_comp"]["weight_kg"] == 75.2
        assert a["body_comp"]["days_from_event"] == -3   # scan 3d before purchase

    def test_skips_items_outside_gap(self, monkeypatch):
        scans = [{"result_id": "R1", "start_time": "2026-04-12T09:00:00Z"}]
        comp_calls = []
        self._patch_bodyspec(monkeypatch, scans, comp_calls)
        wardrobe = _normalise({"items": [
            _wardrobe_item(id="near", purchased_at="2026-04-15"),
            _wardrobe_item(id="far", purchased_at="2024-01-01"),
        ]})
        stats, _cache = _run_bodycomp_backfill(
            self._cfg(), wardrobe, limit=100, max_gap_days=90, refresh=False)
        assert stats["stamped"] == 1
        assert stats["skipped_no_scan"] == 1
        far = next(it for it in wardrobe["items"] if it["id"] == "far")
        assert "body_comp" not in far

    def test_no_scans_attaches_nothing(self, monkeypatch):
        self._patch_bodyspec(monkeypatch, [], [])
        wardrobe = _normalise({"items": [_wardrobe_item(id="a")]})
        stats, _cache = _run_bodycomp_backfill(
            self._cfg(), wardrobe, limit=100, max_gap_days=90, refresh=False)
        assert stats == {"considered": 1, "stamped": 0, "skipped_no_scan": 1, "scans_used": 0}

    def test_no_candidates_short_circuits_without_auth(self, monkeypatch):
        # authenticate must NOT be called when there's nothing to do.
        def _boom(*a, **k):
            raise AssertionError("authenticate should not be called")
        monkeypatch.setattr(bodyspec, "authenticate", _boom)
        wardrobe = _normalise({"items": [_wardrobe_item(id="rug", is_clothing=False)]})
        stats, _cache = _run_bodycomp_backfill(
            self._cfg(), wardrobe, limit=100, max_gap_days=90, refresh=False)
        assert stats == {"considered": 0, "stamped": 0, "skipped_no_scan": 0, "scans_used": 0}


# ---------------------------------------------------------------------------
# Phase B — body-comp matched to reviewed_at when an item has a fit review
# ---------------------------------------------------------------------------

def _reviewed(reviewed_at: str) -> dict:
    return {"fit": "tts", "reviewed_at": reviewed_at, "source": "web"}


class TestBackfillTarget:
    def test_purchase_when_no_review(self):
        it = _wardrobe_item(purchased_at="2026-04-15")
        assert _backfill_target(it) == ("2026-04-15", "purchase")

    def test_reviewed_at_when_review_present(self):
        it = _wardrobe_item(purchased_at="2026-01-01", fit_review=_reviewed("2026-04-20"))
        assert _backfill_target(it) == ("2026-04-20", "fit_review")

    def test_falls_back_to_purchase_when_reviewed_at_unparseable(self):
        it = _wardrobe_item(purchased_at="2026-04-15", fit_review=_reviewed(""))
        assert _backfill_target(it) == ("2026-04-15", "purchase")


class TestNeedsBackfill:
    def test_unstamped_always_needs(self):
        assert _needs_backfill(_wardrobe_item(), refresh=False) is True

    def test_purchase_stamped_no_review_does_not_need(self):
        it = _wardrobe_item(body_comp={"matched_to": "purchase", "matched_date": "2026-04-15"})
        assert _needs_backfill(it, refresh=False) is False

    def test_purchase_stamped_with_new_review_needs_rematch(self):
        it = _wardrobe_item(
            body_comp={"matched_to": "purchase", "matched_date": "2026-04-15"},
            fit_review=_reviewed("2026-06-01"),
        )
        assert _needs_backfill(it, refresh=False) is True

    def test_already_fit_matched_to_same_date_does_not_need(self):
        it = _wardrobe_item(
            body_comp={"matched_to": "fit_review", "matched_date": "2026-06-01"},
            fit_review=_reviewed("2026-06-01"),
        )
        assert _needs_backfill(it, refresh=False) is False

    def test_refresh_forces_need(self):
        it = _wardrobe_item(body_comp={"matched_to": "purchase", "matched_date": "2026-04-15"})
        assert _needs_backfill(it, refresh=True) is True


class TestSelectIncludesReviewedRematch:
    def test_purchase_stamped_reviewed_item_is_reselected(self):
        items = [
            _wardrobe_item(id="stable", body_comp={"matched_to": "purchase",
                                                   "matched_date": "2026-04-15"}),
            _wardrobe_item(id="reviewed",
                           body_comp={"matched_to": "purchase", "matched_date": "2026-04-15"},
                           fit_review=_reviewed("2026-06-01")),
        ]
        got = {it["id"] for it in _select_backfill_items(items, 100, refresh=False)}
        assert got == {"reviewed"}  # only the newly-reviewed one needs a re-match


class TestRunBackfillPhaseB:
    @staticmethod
    def _cfg():
        return SimpleNamespace(
            bodyspec_username="me@example.com", bodyspec_password="pw",
            gist_id="g", github_token="t",
        )

    def _patch_bodyspec(self, monkeypatch, scans, comp_calls):
        monkeypatch.setattr(bodyspec, "authenticate", lambda u, p: "tok")
        monkeypatch.setattr(bodyspec, "list_results", lambda tok: scans)

        def _get_comp(tok, rid):
            comp_calls.append(rid)
            return dict(_FAKE_COMPOSITION, result_id=rid)

        monkeypatch.setattr(bodyspec, "get_composition", _get_comp)

    def test_matches_reviewed_at_keeps_both_and_summarises(self, monkeypatch):
        # Two scans: one near purchase (Jan), one near the review (Jun).
        scans = [
            {"result_id": "RP", "start_time": "2026-01-02T09:00:00Z"},
            {"result_id": "RF", "start_time": "2026-06-02T09:00:00Z"},
        ]
        comp_calls = []
        self._patch_bodyspec(monkeypatch, scans, comp_calls)
        purchase_bc = {"matched_to": "purchase", "matched_date": "2026-01-01",
                       "result_id": "RP"}
        wardrobe = _normalise({"items": [
            _wardrobe_item(id="x", purchased_at="2026-01-01",
                           fit_review=_reviewed("2026-06-01"),
                           body_comp=dict(purchase_bc)),
        ]})
        stats, _cache = _run_bodycomp_backfill(
            self._cfg(), wardrobe, limit=100, max_gap_days=90, refresh=False)
        assert stats["stamped"] == 1
        it = wardrobe["items"][0]
        # body_comp now points at the review-time scan...
        assert it["body_comp"]["matched_to"] == "fit_review"
        assert it["body_comp"]["result_id"] == "RF"
        # ...the original purchase-time block is preserved...
        assert it["body_comp_at_purchase"] == purchase_bc
        # ...and a compact summary is mirrored onto the review.
        summ = it["fit_review"]["body_comp_summary"]
        assert summ["matched_to"] == "fit_review"
        assert summ["weight_kg"] == 75.2
        assert set(summ) >= {"weight_kg", "body_fat_pct", "lean_mass_kg", "fat_mass_kg"}

    def test_purchase_match_does_not_touch_review_or_keepboth(self, monkeypatch):
        scans = [{"result_id": "R1", "start_time": "2026-04-12T09:00:00Z"}]
        self._patch_bodyspec(monkeypatch, scans, [])
        wardrobe = _normalise({"items": [_wardrobe_item(id="a", purchased_at="2026-04-15")]})
        _run_bodycomp_backfill(self._cfg(), wardrobe, limit=100, max_gap_days=90, refresh=False)
        it = wardrobe["items"][0]
        assert it["body_comp"]["matched_to"] == "purchase"
        assert "body_comp_at_purchase" not in it


class TestBackfillFromCache:
    """Backfill matches from the cached body_scans records without hitting
    BodySpec; --refresh-scans (or an empty cache) is the only live-pull path."""

    @staticmethod
    def _cfg():
        return SimpleNamespace(
            bodyspec_username="me@example.com", bodyspec_password="pw",
            gist_id="g", github_token="t",
        )

    @staticmethod
    def _record(start_time, result_id):
        return bodyspec.build_scan_record(_FAKE_COMPOSITION, start_time, result_id=result_id)

    def test_uses_cache_without_authenticating(self, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("cached path must not authenticate or fetch")
        monkeypatch.setattr(bodyspec, "authenticate", _boom)
        monkeypatch.setattr(bodyspec, "list_results", _boom)
        monkeypatch.setattr(bodyspec, "get_composition", _boom)
        monkeypatch.setattr(bodyspec, "build_scan_cache", _boom)
        records = [self._record("2026-04-12T09:00:00Z", "R1")]
        wardrobe = _normalise({"items": [_wardrobe_item(id="a", purchased_at="2026-04-15")]})
        stats, cache_out = _run_bodycomp_backfill(
            self._cfg(), wardrobe, limit=100, max_gap_days=90, refresh=False,
            scans=records, refresh_scans=False)
        assert stats["stamped"] == 1
        assert cache_out is None            # nothing rebuilt → no cache write-back
        bc = wardrobe["items"][0]["body_comp"]
        assert bc["result_id"] == "R1"
        assert bc["days_from_event"] == -3  # scan 3d before purchase

    def test_refresh_scans_forces_live_pull(self, monkeypatch):
        built = {"refreshed_at": "now", "scans": [self._record("2026-04-12T09:00:00Z", "R1")]}
        auth_calls = []
        monkeypatch.setattr(bodyspec, "authenticate",
                            lambda u, p: (auth_calls.append(1), "tok")[1])
        monkeypatch.setattr(bodyspec, "build_scan_cache", lambda tok: built)
        # A stale cache is passed, but --refresh-scans ignores it and rebuilds.
        stale = [self._record("2020-01-01T00:00:00Z", "OLD")]
        wardrobe = _normalise({"items": [_wardrobe_item(id="a", purchased_at="2026-04-15")]})
        stats, cache_out = _run_bodycomp_backfill(
            self._cfg(), wardrobe, limit=100, max_gap_days=90, refresh=False,
            scans=stale, refresh_scans=True)
        assert auth_calls == [1]
        assert cache_out == built           # rebuilt cache returned for persistence
        assert wardrobe["items"][0]["body_comp"]["result_id"] == "R1"

    def test_empty_cache_bootstraps_via_live_pull(self, monkeypatch):
        built = {"refreshed_at": "now", "scans": [self._record("2026-04-12T09:00:00Z", "R1")]}
        monkeypatch.setattr(bodyspec, "authenticate", lambda u, p: "tok")
        monkeypatch.setattr(bodyspec, "build_scan_cache", lambda tok: built)
        wardrobe = _normalise({"items": [_wardrobe_item(id="a", purchased_at="2026-04-15")]})
        stats, cache_out = _run_bodycomp_backfill(
            self._cfg(), wardrobe, limit=100, max_gap_days=90, refresh=False,
            scans=None, refresh_scans=False)
        assert cache_out == built           # bootstrapped + handed back to persist
        assert wardrobe["items"][0]["body_comp"]["result_id"] == "R1"


class TestNormaliseShopFitNotes:
    def test_adds_empty_shop_fit_notes(self):
        assert _normalise({})["shop_fit_notes"] == {}

    def test_preserves_existing_shop_fit_notes(self):
        notes = {"Toka": "buy XL sweatshirts"}
        assert _normalise({"shop_fit_notes": notes})["shop_fit_notes"] == notes


# ---------------------------------------------------------------------------
# Excerpt robustness (emoji-spacer collapse so buried ORDER SUMMARY survives)
# ---------------------------------------------------------------------------

class TestExcerpt:
    def test_collapses_emoji_run_keeps_order_summary(self):
        body = "Welcome " + ("\U0001F389" * 300) + " ORDER SUMMARY: The One Jogger SIZE M"
        out = order_scan._excerpt(body)
        assert "ORDER SUMMARY" in out
        assert "\U0001F389" not in out
        assert len(out) < 100  # the 300-emoji block collapsed to a space

    def test_keeps_accented_product_names(self):
        assert "Café" in order_scan._excerpt("Café Joggers \U0001F389")

    def test_truncates_at_limit(self):
        out = order_scan._excerpt("a" * (order_scan._BODY_EXCERPT_LIMIT + 500))
        assert out.endswith("...[truncated]")
        assert len(out) <= order_scan._BODY_EXCERPT_LIMIT + len(" ...[truncated]")


# ---------------------------------------------------------------------------
# processed_email_ids "burn" fix — record after --shop / --max-emails filtering
# ---------------------------------------------------------------------------

def _scan_email(eid, frm, subject, body):
    return {"id": eid, "from": frm, "subject": subject,
            "body_text": body, "date": "Mon, 01 Jan 2026 00:00:00 +0000"}


class TestRunScanProcessedIds:
    @staticmethod
    def _cfg():
        return SimpleNamespace(
            gmail_username="u", gmail_app_password="p", excluded_shops=(),
        )

    def _emails(self):
        return [
            _scan_email("o_aniqi", "Aniqi <orders@aniqi.com>", "Your order #111",
                        "Order #111 confirmed. Item A"),
            _scan_email("o_toka", "Toka <orders@toka.com>", "Order confirmation #222",
                        "Order summary: Item B"),
            _scan_email("junk", "News <news@example.com>", "Weekly newsletter", "hello"),
        ]

    def _patch(self, monkeypatch, emails):
        monkeypatch.setattr(order_scan, "_fetch_emails", lambda *a, **k: emails)
        monkeypatch.setattr(
            "src.order_extract.extract_items",
            lambda claude_input, client=None: {"orders": [], "usage": None},
        )

    def _scan(self, monkeypatch, *, shop_filter=None, max_emails=None):
        self._patch(monkeypatch, self._emails())
        wardrobe = _normalise({})
        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        _items, processed = order_scan._run_scan(
            self._cfg(), wardrobe, since,
            shop_filter=shop_filter, max_emails=max_emails, shop_aliases={},
        )
        return processed

    def test_filtered_out_order_not_burned(self, monkeypatch):
        processed = self._scan(monkeypatch, shop_filter="Toka")
        assert "o_toka" in processed       # survived the --shop filter
        assert "junk" in processed         # "other" always recorded
        assert "o_aniqi" not in processed  # filtered out -> left for a later run

    def test_unfiltered_records_all(self, monkeypatch):
        processed = self._scan(monkeypatch)
        assert set(processed) == {"o_aniqi", "o_toka", "junk"}

    def test_max_emails_dropped_order_not_burned(self, monkeypatch):
        processed = self._scan(monkeypatch, max_emails=1)
        # Only one order survives the cap; the other isn't recorded as processed.
        order_ids = {"o_aniqi", "o_toka"} & set(processed)
        assert len(order_ids) == 1
        assert "junk" in processed


class TestRunScanExcludedShops:
    """EXCLUDED_SHOPS keeps a shop's purchases out of the wardrobe at ingestion."""

    def _emails(self):
        return [
            _scan_email("o_peak", "PeakWear <orders@peakwear.com>", "Your order #111",
                        "Order #111 confirmed. Item A"),
            _scan_email("o_bd", "Nocturne Goods <orders@nocturne-goods.com>",
                        "Your order #999", "Order summary: Item X"),
            _scan_email("s_bd", "Nocturne Goods <ship@nocturne-goods.com>",
                        "Your order has shipped", "Tracking: https://ups.com/track"),
        ]

    def test_excluded_order_not_sent_to_claude_but_recorded(self, monkeypatch):
        seen_inputs = []

        def _fake_extract(claude_input, client=None):
            seen_inputs.append([r["email_id"] for r in claude_input])
            return {"orders": [], "usage": None}

        monkeypatch.setattr(order_scan, "_fetch_emails", lambda *a, **k: self._emails())
        monkeypatch.setattr("src.order_extract.extract_items", _fake_extract)

        cfg = SimpleNamespace(
            gmail_username="u", gmail_app_password="p",
            excluded_shops=("nocturne goods",),
        )
        wardrobe = _normalise({})
        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        items, processed = order_scan._run_scan(
            cfg, wardrobe, since, shop_filter=None, max_emails=None, shop_aliases={},
        )

        # Nocturne Goods order never reached Claude...
        assert seen_inputs == [["o_peak"]]
        # ...and neither the order nor the shipment materialised.
        assert all(it["order_email_id"] != "o_bd" for it in items)
        # ...but both are recorded processed so they're never re-fetched.
        assert "o_bd" in processed
        assert "s_bd" in processed
        assert "o_peak" in processed


class TestDropExcludedItems:
    def test_removes_matching_and_keeps_others(self):
        wardrobe = {"items": [
            {"id": "1", "shop": "Nocturne Goods", "shop_domain": "nocturne-goods.com"},
            {"id": "2", "shop": "PeakWear", "shop_domain": "peakwear.com"},
            {"id": "3", "shop": "NocturneGoods", "shop_domain": "nocturnegoods.com"},
        ]}
        removed = _drop_excluded_items(wardrobe, ("nocturne goods",))
        assert removed == 2
        assert [it["id"] for it in wardrobe["items"]] == ["2"]

    def test_empty_exclusions_is_noop(self):
        wardrobe = {"items": [{"id": "1", "shop": "Nocturne Goods", "shop_domain": "nocturne-goods.com"}]}
        assert _drop_excluded_items(wardrobe, ()) == 0
        assert len(wardrobe["items"]) == 1


# ---------------------------------------------------------------------------
# --reprocess un-skip helper
# ---------------------------------------------------------------------------

class TestUnskipMatching:
    class _FakeIMAP:
        def __init__(self, uids, msgid_by_uid):
            self._uids = uids
            self._msgid = msgid_by_uid

        def select(self, *a, **k):
            return ("OK", [b"1"])

        def uid(self, cmd, *args):
            if cmd == "SEARCH":
                return ("OK", [self._uids])
            if cmd == "FETCH":
                u = args[0]
                return ("OK", [b"%b (X-GM-MSGID %b)" % (u, self._msgid[u])])
            return ("NO", [None])

        def logout(self):
            pass

    @staticmethod
    def _cfg():
        return SimpleNamespace(gmail_username="u", gmail_app_password="p")

    def test_removes_matching_processed_ids(self):
        fake = self._FakeIMAP(b"1 2", {b"1": b"111", b"2": b"222"})
        wardrobe = _normalise({"scan_state": {"processed_email_ids": {"111": "t", "333": "t"}}})
        removed = order_scan._unskip_matching(self._cfg(), wardrobe, "Fabletics", imap_client=fake)
        assert removed == 1  # 111 present (removed); 222 not in skip-set; 333 untouched
        pids = wardrobe["scan_state"]["processed_email_ids"]
        assert "111" not in pids
        assert "333" in pids

    def test_no_search_hits_returns_zero(self):
        fake = self._FakeIMAP(b"", {})
        wardrobe = _normalise({"scan_state": {"processed_email_ids": {"111": "t"}}})
        removed = order_scan._unskip_matching(self._cfg(), wardrobe, "Nope", imap_client=fake)
        assert removed == 0
        assert "111" in wardrobe["scan_state"]["processed_email_ids"]


# ---------------------------------------------------------------------------
# Targeted scrape (forwarded-email ingestion)
# ---------------------------------------------------------------------------

# A realistic Gmail-forward body: dashed marker + plain-text header block +
# the original receipt. Synthetic shop/addresses only (privacy rules).
_GMAIL_FORWARD = """\
---------- Forwarded message ---------
From: Bosuman <store-noreply@mail.bosuman.com>
Date: Mon, Jun 3, 2024 at 5:14 PM
Subject: Your Bosuman order confirmation
To: Old Account <olduser@example.com>

Order #BS-12345
Raijin Hoodie  Size L  $60.00
Subtotal: $60.00
"""

_OUTLOOK_FORWARD = """\
From: Bosuman <store@bosuman.com>
Sent: Monday, June 3, 2024 5:14 PM
To: olduser@example.com
Subject: Your order

Order #999
Fujin Tee  M  $25.00
"""


class TestForwardedOrigin:
    def test_gmail_format(self):
        o = order_scan._forwarded_origin(_GMAIL_FORWARD)
        assert o["from"] == "Bosuman <store-noreply@mail.bosuman.com>"
        assert o["date"] == "2024-06-03"
        assert o["subject"] == "Your Bosuman order confirmation"

    def test_outlook_format(self):
        # No dashed marker, but a tight From/Sent/Subject block → trusted.
        o = order_scan._forwarded_origin(_OUTLOOK_FORWARD)
        assert o["from"] == "Bosuman <store@bosuman.com>"
        assert o["date"] == "2024-06-03"
        assert o["subject"] == "Your order"

    def test_apple_format(self):
        body = (
            "Begin forwarded message:\n\n"
            "From: Bosuman <store@bosuman.com>\n"
            "Date: June 3, 2024 at 5:14:00 PM PDT\n"
            "Subject: Your order\n"
            "To: me@example.com\n\nOrder #1\n"
        )
        o = order_scan._forwarded_origin(body)
        assert o["from"] == "Bosuman <store@bosuman.com>"
        assert o["date"] == "2024-06-03"

    def test_non_forward_returns_none(self):
        # A stray "From: the team" in a footer must NOT read as a forward
        # (no marker, and no Date/Subject backing it).
        body = ("Thanks for shopping!\nFrom: the team\n"
                "We hope you enjoy your purchase.\nOrder #123 subtotal $10\n")
        assert order_scan._forwarded_origin(body) is None

    def test_empty_returns_none(self):
        assert order_scan._forwarded_origin("") is None


class TestParseForwardedDate:
    @pytest.mark.parametrize("raw,expected", [
        ("Mon, 3 Jun 2024 17:14:00 -0700", "2024-06-03"),   # RFC 2822
        ("Mon, Jun 3, 2024 at 5:14 PM", "2024-06-03"),       # Gmail web
        ("Monday, June 3, 2024 5:14 PM", "2024-06-03"),      # Outlook
        ("June 3, 2024 at 5:14:00 PM PDT", "2024-06-03"),    # Apple
        ("3 June 2024", "2024-06-03"),                       # day-first
        ("6/3/2024", "2024-06-03"),                          # numeric US m/d/y
        ("2024-06-03", "2024-06-03"),                        # ISO
        ("not a date", ""),                                  # garbage
        ("", ""),
        ("Feb 30, 2024", ""),                                # invalid day → ""
    ])
    def test_parse(self, raw, expected):
        assert order_scan._parse_forwarded_date(raw) == expected


class TestNormaliseMsgid:
    @pytest.mark.parametrize("token,expected", [
        ("17699123456789", "17699123456789"),   # decimal as stored
        ("0x18d2a3", str(0x18d2a3)),             # hex with prefix
        ("18d2a3", str(0x18d2a3)),               # bare hex (has letters)
        ("#all/18d2a3", str(0x18d2a3)),          # pasted digest permalink tail
        ("https://mail.google.com/mail/u/0/#all/18d2a3", str(0x18d2a3)),
        ("  17699123456789  ", "17699123456789"),
        ("not-an-id ???", None),
        ("", None),
    ])
    def test_normalise(self, token, expected):
        assert order_scan._normalise_msgid(token) == expected


class TestStripForwardPreamble:
    def test_strips_gmail_header(self):
        out = order_scan._strip_forward_preamble(_GMAIL_FORWARD)
        assert out.startswith("Order #")
        assert "Forwarded message" not in out

    def test_non_forward_unchanged(self):
        body = "Order #5\nSubtotal: $10\n"
        assert order_scan._strip_forward_preamble(body) == body


class TestIsSelfForward:
    def test_positive(self):
        assert order_scan._is_self_forward(
            "Me <testuser@example.com>", "testuser@example.com") is True

    def test_case_insensitive(self):
        assert order_scan._is_self_forward(
            "ME <TestUser@Example.com>", "testuser@example.com") is True

    def test_negative(self):
        assert order_scan._is_self_forward(
            "Bosuman <store@bosuman.com>", "testuser@example.com") is False

    def test_no_username(self):
        assert order_scan._is_self_forward("anyone@x.com", None) is False


class TestRunMessageScan:
    """``_run_message_scan`` with ``_fetch_targeted`` + ``extract_items`` stubbed
    (no IMAP, no Anthropic)."""

    @staticmethod
    def _cfg(excluded=()):
        return SimpleNamespace(
            gmail_username="testuser@example.com", gmail_app_password="p",
            excluded_shops=excluded)

    @staticmethod
    def _fwd_email(eid="900"):
        return {
            "id": eid,
            "from": "Me <testuser@example.com>",          # the forward (self)
            "subject": "Fwd: Your Bosuman order confirmation",
            "body_text": _GMAIL_FORWARD,
            "date": "Sat, 22 Jun 2026 09:00:00 -0500",    # the forward's "now"
            "product_links": [],
        }

    def _patch(self, monkeypatch, emails, orders):
        monkeypatch.setattr(order_scan, "_fetch_targeted",
                            lambda cfg, **kw: emails)
        monkeypatch.setattr(
            "src.order_extract.extract_items",
            lambda claude_input, **kw: {"orders": orders, "usage": None})

    def test_forward_recovers_shop_and_date(self, monkeypatch):
        self._patch(
            monkeypatch, [self._fwd_email()],
            [{"email_id": "900", "items": [
                {"name": "Raijin Hoodie", "size": "L", "category": "hoodie"}]}])
        items, processed = order_scan._run_message_scan(
            self._cfg(), _empty_wardrobe(), query="newer_than:1d from:me",
            msgids=[], shop_aliases={}, anthropic_client=object(), prompt=False)
        assert processed == ["900"]
        assert len(items) == 1
        it = items[0]
        # Shop/domain from the forwarded header (not the forward's own From).
        assert it["shop_domain"] == "bosuman.com"
        assert it["shop"] == "Bosuman"
        # Purchase date from the ORIGINAL email, not the forward date.
        assert it["purchased_at"] == "2024-06-03"
        assert it["item_name"] == "Raijin Hoodie"
        assert it["order_email_id"] == "900"

    def test_overrides_win_over_header(self, monkeypatch):
        self._patch(
            monkeypatch, [self._fwd_email()],
            [{"email_id": "900", "items": [{"name": "Raijin Hoodie"}]}])
        items, _ = order_scan._run_message_scan(
            self._cfg(), _empty_wardrobe(), query="q", msgids=[],
            shop_name="Riot Games", shop_domain="RiotGames.com",
            purchased_at="2023-01-15", shop_aliases={},
            anthropic_client=object(), prompt=False)
        it = items[0]
        assert it["shop"] == "Riot Games"
        assert it["shop_domain"] == "riotgames.com"
        assert it["purchased_at"] == "2023-01-15"

    def test_excluded_shop_skipped(self, monkeypatch):
        self._patch(
            monkeypatch, [self._fwd_email()],
            [{"email_id": "900", "items": [{"name": "Raijin Hoodie"}]}])
        items, processed = order_scan._run_message_scan(
            self._cfg(excluded=("Bosuman",)), _empty_wardrobe(),
            query="q", msgids=[], shop_aliases={},
            anthropic_client=object(), prompt=False)
        assert items == []
        assert processed == ["900"]   # still recorded so the daily sweep skips it

    def test_no_match_short_circuits(self, monkeypatch):
        called = {"n": 0}

        def _extract(claude_input, **kw):
            called["n"] += 1
            return {"orders": [], "usage": None}
        monkeypatch.setattr(order_scan, "_fetch_targeted", lambda cfg, **kw: [])
        monkeypatch.setattr("src.order_extract.extract_items", _extract)
        items, processed = order_scan._run_message_scan(
            self._cfg(), _empty_wardrobe(), query="q", msgids=[],
            shop_aliases={}, anthropic_client=object(), prompt=False)
        assert items == [] and processed == []
        assert called["n"] == 0   # Claude never called when nothing matched

    def test_prompt_used_when_origin_unparseable(self, monkeypatch):
        # An email with no forward header + no overrides → prompt is invoked.
        plain = {"id": "901", "from": "Me <testuser@example.com>",
                 "subject": "Fwd: receipt", "body_text": "Order #5\n$10\n",
                 "date": "Sat, 22 Jun 2026 09:00:00 -0500", "product_links": []}
        self._patch(monkeypatch, [plain],
                    [{"email_id": "901", "items": [{"name": "Mystery Tee"}]}])
        captured = {}

        def _fake_prompt(em, origin, **kw):
            captured["called"] = True
            return ("Manual Shop", "manualshop.com", "2022-12-25")
        monkeypatch.setattr(order_scan, "_prompt_origin", _fake_prompt)
        items, _ = order_scan._run_message_scan(
            self._cfg(), _empty_wardrobe(), query="q", msgids=[],
            shop_aliases={}, anthropic_client=object(), prompt=True)
        assert captured.get("called") is True
        assert items[0]["shop"] == "Manual Shop"
        assert items[0]["purchased_at"] == "2022-12-25"


class TestSelfForwardGuardInScan:
    """The daily INBOX sweep (`_run_scan`) must drop a self-forwarded order
    email instead of ingesting it as a bogus "Gmail" purchase."""

    def test_self_forward_yields_no_item(self, monkeypatch):
        self_fwd = {
            "id": "777", "from": "Zoro <testuser@example.com>",
            "subject": "Fwd: Your order #5",
            "body_text": "Order #5\nSubtotal: $10.00\nOrder total: $10.00\n",
            "date": "Sat, 22 Jun 2026 09:00:00 -0500",
        }
        monkeypatch.setattr(order_scan, "_fetch_emails",
                            lambda cfg, since, skip_ids, **kw: [self_fwd])
        cfg = SimpleNamespace(
            gmail_username="testuser@example.com", gmail_app_password="p",
            excluded_shops=())
        items, processed = order_scan._run_scan(
            cfg, _empty_wardrobe(), datetime(2024, 1, 1, tzinfo=timezone.utc),
            shop_filter=None)
        assert items == []
        assert processed == ["777"]   # recorded so it isn't re-fetched forever
