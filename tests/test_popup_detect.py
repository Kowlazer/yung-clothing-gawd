"""Tests for src/popup_detect.py — pure-Python logic + locator-fake integration.

Playwright isn't spun up here. Instead, ``FakeLocator``/``FakePage`` mimic the
sync Playwright API surface our detection helpers actually call (
``is_visible``, ``locator``, ``inner_text``, ``count``, ``first``, ``url``,
``mouse.move``, ``wait_for_timeout``). This keeps the suite fast and lets
tests target one branch of the heuristic at a time.

The HTML-fixture flavor of tests (a real Playwright page loading saved popup
HTML) lives in a Phase 2-late TODO and is not yet wired — the selector
constants themselves are the highest-risk pieces and they're covered here
by membership assertions.
"""
from __future__ import annotations

from typing import Any

import pytest

from src import popup_detect as pd


# ---------------------------------------------------------------------------
# Fake Playwright Locator / Page
# ---------------------------------------------------------------------------

class FakeLocator:
    """Minimal stand-in for a Playwright Locator.

    Visibility is configured by the constructor or the parent FakePage via
    a ``selector -> visible`` mapping. Inner-text and checkbox state are
    likewise scripted. ``first`` returns ``self`` so chains like
    ``page.locator(sel).first`` work transparently.
    """

    def __init__(
        self,
        *,
        selector: str = "",
        visible: bool = True,
        text: str = "",
        checked: bool = False,
        count: int = 1,
        children: dict[str, "FakeLocator"] | None = None,
    ) -> None:
        self.selector = selector
        self._visible = visible
        self._text = text
        self._checked = checked
        self._count = count
        self._children: dict[str, FakeLocator] = children or {}
        self.fill_calls: list[str] = []
        self.click_count = 0
        self.check_count = 0

    # --- attribute chain ---
    @property
    def first(self) -> "FakeLocator":
        return self

    # --- locator(selector) inside a popup ---
    def locator(self, selector: str) -> "FakeLocator":
        if selector in self._children:
            return self._children[selector]
        # Default: returns a not-visible empty locator so callers fall through.
        return FakeLocator(selector=selector, visible=False)

    def nth(self, _idx: int) -> "FakeLocator":
        return self

    # --- introspection ---
    def is_visible(self, timeout: int | None = None) -> bool:  # noqa: ARG002
        return self._visible

    def is_checked(self) -> bool:
        return self._checked

    def count(self) -> int:
        return self._count

    def inner_text(self, timeout: int | None = None) -> str:  # noqa: ARG002
        return self._text

    def evaluate(self, _js: str) -> str:
        return self._text

    # --- actions ---
    def fill(self, text: str) -> None:
        self.fill_calls.append(text)

    def click(self, timeout: int | None = None) -> None:  # noqa: ARG002
        self.click_count += 1

    def check(self, timeout: int | None = None) -> None:  # noqa: ARG002
        self.check_count += 1
        self._checked = True


class FakeMouse:
    def __init__(self) -> None:
        self.moves: list[tuple[int, int]] = []
        self.wheels: list[tuple[int, int]] = []

    def move(self, x: int, y: int) -> None:
        self.moves.append((x, y))

    def wheel(self, dx: int, dy: int) -> None:
        self.wheels.append((dx, dy))


class FakeFrame:
    def __init__(self, url: str) -> None:
        self.url = url


class FakePage:
    """Maps selector strings → ``FakeLocator``. Anything unmapped resolves
    to a not-visible empty locator. ``frames`` and ``evaluate`` are also
    stubbed for bot-block detection tests.
    """

    def __init__(
        self,
        *,
        url: str = "https://shop.com",
        body_text: str = "",
        selectors: dict[str, FakeLocator] | None = None,
        frame_urls: list[str] | None = None,
        eval_text: str | None = None,
    ) -> None:
        self.url = url
        self._body_text = body_text
        self._selectors = dict(selectors or {})
        self.frames = [FakeFrame(u) for u in (frame_urls or [url])]
        self._eval_text = eval_text if eval_text is not None else body_text
        self.mouse = FakeMouse()
        self.waits: list[int] = []

    def locator(self, selector: str) -> FakeLocator:
        if selector in self._selectors:
            return self._selectors[selector]
        if selector == "body":
            return FakeLocator(selector="body", text=self._body_text)
        return FakeLocator(selector=selector, visible=False)

    def evaluate(self, _js: str) -> str:
        return self._eval_text

    def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)


# ---------------------------------------------------------------------------
# Selector inventory — sanity assertions on the constants
# ---------------------------------------------------------------------------

class TestVendorSelectors:
    def test_includes_all_documented_vendors(self):
        # The HANDOFF_SIGNUP plan lists exactly these seven vendors.
        assert set(pd.VENDOR_SELECTORS.keys()) == {
            "klaviyo", "privy", "justuno", "attentive",
            "postscript", "mailchimp", "shopify",
        }

    def test_klaviyo_selector_matches_class_pattern(self):
        # Klaviyo class names look like ``klaviyo-form-XYZ`` — the wildcard
        # match in our selector must cover the suffixed form.
        assert "klaviyo-form-" in pd.VENDOR_SELECTORS["klaviyo"]

    def test_email_input_selector_covers_common_patterns(self):
        sel = pd.EMAIL_INPUT_SELECTOR
        assert "input[type='email']" in sel
        assert "email' i" in sel  # case-insensitive name/id/placeholder match


# ---------------------------------------------------------------------------
# detect_popup
# ---------------------------------------------------------------------------

class TestDetectPopup:
    def test_returns_first_vendor_match(self):
        klaviyo_loc = FakeLocator(selector="klaviyo", visible=True)
        page = FakePage(
            selectors={pd.VENDOR_SELECTORS["klaviyo"]: klaviyo_loc},
        )
        popup, vendor = pd.detect_popup(page, initial_wait_ms=10)
        assert vendor == "klaviyo"
        assert popup is klaviyo_loc

    def test_falls_back_to_generic_dialog(self):
        generic = FakeLocator(selector="dialog", visible=True)
        page = FakePage(selectors={pd.GENERIC_DIALOG_SELECTOR: generic})
        popup, vendor = pd.detect_popup(page, initial_wait_ms=10)
        assert vendor == "generic"
        assert popup is generic

    def test_returns_none_when_nothing_visible(self):
        page = FakePage()
        popup, vendor = pd.detect_popup(page, initial_wait_ms=10)
        assert popup is None and vendor is None

    def test_triggers_exit_intent_by_default(self):
        page = FakePage()
        pd.detect_popup(page, initial_wait_ms=10)
        assert (0, 0) in page.mouse.moves

    def test_skips_exit_intent_when_disabled(self):
        page = FakePage()
        pd.detect_popup(page, initial_wait_ms=10, trigger_exit_intent=False)
        assert page.mouse.moves == []

    def test_vendor_match_wins_over_generic(self):
        """If both a vendor selector and a generic dialog match, attribute
        to the vendor — useful for tuning logs."""
        klaviyo = FakeLocator(selector="klaviyo", visible=True)
        generic = FakeLocator(selector="generic", visible=True)
        page = FakePage(
            selectors={
                pd.VENDOR_SELECTORS["klaviyo"]: klaviyo,
                pd.GENERIC_DIALOG_SELECTOR: generic,
            },
        )
        _, vendor = pd.detect_popup(page, initial_wait_ms=10)
        assert vendor == "klaviyo"

    def test_already_visible_popup_needs_no_mouse_activity(self):
        """The first scan runs before any nudge, so an on-load popup is
        found without wheel scrolls or exit-intent mouse moves."""
        klaviyo = FakeLocator(selector="klaviyo", visible=True)
        page = FakePage(selectors={pd.VENDOR_SELECTORS["klaviyo"]: klaviyo})
        _, vendor = pd.detect_popup(page, initial_wait_ms=10)
        assert vendor == "klaviyo"
        assert page.mouse.moves == []
        assert page.mouse.wheels == []

    def test_scroll_nudge_reveals_scroll_triggered_popup(self):
        loc = FakeLocator(selector="klaviyo", visible=False)
        page = FakePage(selectors={pd.VENDOR_SELECTORS["klaviyo"]: loc})
        page.mouse.wheel = lambda dx, dy: setattr(loc, "_visible", True)
        popup, vendor = pd.detect_popup(page, initial_wait_ms=10)
        assert vendor == "klaviyo"
        assert popup is loc

    def test_scrolls_before_exit_intent(self):
        page = FakePage()
        pd.detect_popup(page, initial_wait_ms=10)
        assert page.mouse.wheels == [
            (0, pd._SCROLL_NUDGE_PX), (0, -pd._SCROLL_NUDGE_PX),
        ]
        assert (0, 0) in page.mouse.moves

    def test_skips_scroll_when_disabled(self):
        page = FakePage()
        pd.detect_popup(page, initial_wait_ms=10, trigger_scroll=False)
        assert page.mouse.wheels == []

    def test_reveal_stage_opens_teaser_popup(self):
        """A collapsed 'GET 10% OFF' teaser tab is clicked to reveal the popup
        after the earlier stages find nothing (issue #14)."""
        klaviyo = FakeLocator(selector="klaviyo", visible=False)
        trigger = FakeLocator(visible=True, text="GET 10% OFF")
        trigger.click = lambda: setattr(klaviyo, "_visible", True)
        page = FakePage(selectors={
            pd._REVEAL_TRIGGER_SELECTOR: trigger,
            pd.VENDOR_SELECTORS["klaviyo"]: klaviyo,
        })
        popup, vendor = pd.detect_popup(page, initial_wait_ms=10)
        assert vendor == "klaviyo"
        assert popup is klaviyo

    def test_reveal_stage_skipped_when_disabled(self):
        klaviyo = FakeLocator(selector="klaviyo", visible=False)
        trigger = FakeLocator(visible=True, text="GET 10% OFF")
        trigger.click = lambda: setattr(klaviyo, "_visible", True)
        page = FakePage(selectors={
            pd._REVEAL_TRIGGER_SELECTOR: trigger,
            pd.VENDOR_SELECTORS["klaviyo"]: klaviyo,
        })
        popup, vendor = pd.detect_popup(
            page, initial_wait_ms=10, trigger_reveal=False,
        )
        assert popup is None and vendor is None


# ---------------------------------------------------------------------------
# Field-finding helpers
# ---------------------------------------------------------------------------

class TestFindFields:
    def _popup_with(self, *, email=False, phone=False, submit=False):
        children = {}
        if email:
            children[pd.EMAIL_INPUT_SELECTOR] = FakeLocator(visible=True)
        if phone:
            children[pd.PHONE_INPUT_SELECTOR] = FakeLocator(visible=True)
        if submit:
            children[pd.SUBMIT_BUTTON_SELECTOR] = FakeLocator(visible=True)
        return FakeLocator(visible=True, children=children)

    def test_finds_email_field(self):
        popup = self._popup_with(email=True)
        assert pd.find_email_field(popup) is not None

    def test_email_field_missing(self):
        popup = self._popup_with(email=False)
        assert pd.find_email_field(popup) is None

    def test_finds_phone_field(self):
        popup = self._popup_with(phone=True)
        assert pd.find_phone_field(popup) is not None

    def test_finds_submit_button(self):
        popup = self._popup_with(submit=True)
        assert pd.find_submit_button(popup) is not None


class _ButtonList:
    """Locator fake holding N distinct button locators (FakeLocator.nth
    returns self, which can't model per-index differences)."""

    def __init__(self, buttons: list[FakeLocator]) -> None:
        self._buttons = buttons

    def count(self) -> int:
        return len(self._buttons)

    def nth(self, idx: int) -> FakeLocator:
        return self._buttons[idx]


class TestSoleActionableButtonFallback:
    """find_submit_button's last tier: vendor buttons with campaign-specific
    text ('Get 10% Off') that the enumerated selectors can't cover."""

    def _popup(self, buttons: list[FakeLocator]) -> FakeLocator:
        return FakeLocator(
            visible=True,
            children={pd._BUTTONISH_SELECTOR: _ButtonList(buttons)},
        )

    def test_sole_labelled_button_wins(self):
        # Shape observed live: empty close ×, decline, campaign-text accept.
        accept = FakeLocator(visible=True, text="Get 10% Off")
        popup = self._popup([
            FakeLocator(visible=True, text=""),
            FakeLocator(visible=True, text="No, thanks"),
            accept,
        ])
        assert pd.find_submit_button(popup) is accept

    def test_two_candidates_is_ambiguous(self):
        popup = self._popup([
            FakeLocator(visible=True, text="Get 10% Off"),
            FakeLocator(visible=True, text="Shop bestsellers"),
        ])
        assert pd.find_submit_button(popup) is None

    def test_all_decline_or_unlabelled_yields_none(self):
        popup = self._popup([
            FakeLocator(visible=True, text="✕"),
            FakeLocator(visible=True, text="Not now"),
            FakeLocator(visible=False, text="Get 10% Off"),  # hidden
        ])
        assert pd.find_submit_button(popup) is None

    def test_selector_tier_still_preferred(self):
        selector_btn = FakeLocator(visible=True)
        fallback_btn = FakeLocator(visible=True, text="Get 10% Off")
        popup = FakeLocator(
            visible=True,
            children={
                pd.SUBMIT_BUTTON_SELECTOR: selector_btn,
                pd._BUTTONISH_SELECTOR: _ButtonList([fallback_btn]),
            },
        )
        assert pd.find_submit_button(popup) is selector_btn


class TestFindRevealTrigger:
    """find_reveal_trigger: the collapsed-teaser / two-step CTA opener (#14)."""

    def _scope(self, buttons: list[FakeLocator]) -> FakeLocator:
        return FakeLocator(
            visible=True,
            children={pd._REVEAL_TRIGGER_SELECTOR: _ButtonList(buttons)},
        )

    def test_matches_get_percent_off(self):
        trig = FakeLocator(visible=True, text="GET 10% OFF")
        assert pd.find_reveal_trigger(self._scope([trig])) is trig

    def test_matches_claim_now(self):
        trig = FakeLocator(visible=True, text="CLAIM NOW")
        assert pd.find_reveal_trigger(self._scope([trig])) is trig

    def test_matches_unlock_reward_verb_alone(self):
        trig = FakeLocator(visible=True, text="Unlock my code")
        assert pd.find_reveal_trigger(self._scope([trig])) is trig

    def test_skips_decline_button(self):
        assert pd.find_reveal_trigger(
            self._scope([FakeLocator(visible=True, text="No, thanks")]),
        ) is None

    def test_skips_plain_nav_and_bare_submit(self):
        # "Shop now" has no reward verb; "Sign up" is a submit, not a reveal.
        scope = self._scope([
            FakeLocator(visible=True, text="Shop now"),
            FakeLocator(visible=True, text="Sign up"),
        ])
        assert pd.find_reveal_trigger(scope) is None

    def test_weak_verb_needs_discount_noun(self):
        # "Get help now" — weak verb, no discount noun nearby → not a trigger.
        assert pd.find_reveal_trigger(
            self._scope([FakeLocator(visible=True, text="Get help now")]),
        ) is None

    def test_skips_hidden_candidate(self):
        assert pd.find_reveal_trigger(
            self._scope([FakeLocator(visible=False, text="GET 10% OFF")]),
        ) is None

    def test_returns_first_match_among_many(self):
        first = FakeLocator(visible=True, text="Grab 15% off")
        scope = self._scope([
            FakeLocator(visible=True, text="Menu"),
            first,
            FakeLocator(visible=True, text="Unlock deal"),
        ])
        assert pd.find_reveal_trigger(scope) is first

    def test_no_candidates_returns_none(self):
        assert pd.find_reveal_trigger(self._scope([])) is None


# ---------------------------------------------------------------------------
# check_consent_if_present
# ---------------------------------------------------------------------------

class TestConsent:
    def test_no_checkboxes_is_noop(self):
        popup = FakeLocator(
            visible=True,
            children={"input[type='checkbox']": FakeLocator(visible=False, count=0)},
        )
        assert pd.check_consent_if_present(popup) is False

    def test_checks_keyword_box(self):
        box = FakeLocator(visible=True, text="I agree to marketing emails", checked=False)
        popup = FakeLocator(
            visible=True,
            children={"input[type='checkbox']": box},
        )
        # nth(0) returns the same FakeLocator (our fake's simplification).
        assert pd.check_consent_if_present(popup) is True
        assert box.check_count == 1

    def test_skips_non_keyword_box(self):
        box = FakeLocator(visible=True, text="Save my preferences", checked=False)
        popup = FakeLocator(
            visible=True,
            children={"input[type='checkbox']": box},
        )
        assert pd.check_consent_if_present(popup) is False
        assert box.check_count == 0


# ---------------------------------------------------------------------------
# looks_like_captcha
# ---------------------------------------------------------------------------

class TestLooksLikeCaptcha:
    @pytest.mark.parametrize("text", [
        "Performing security verification — Cloudflare",
        "Please verify you are human",
        "Access is temporarily restricted",
        "Are you a robot?",
        "Solve the captcha to continue",
    ])
    def test_matches_known_phrases(self, text):
        assert pd.looks_like_captcha(text) is True

    @pytest.mark.parametrize("text", [
        "",
        None,
        "Welcome to our shop!",
        "Get 15% off your first order",
    ])
    def test_does_not_match_normal_text(self, text):
        assert pd.looks_like_captcha(text) is False


class TestLooksLikeOtp:
    @pytest.mark.parametrize("text", [
        "Enter the verification code we sent to your phone",
        "We just texted you a code — reply with the code to confirm",
        "Confirm your number to finish subscribing",
        "Check your phone for a code",
        "Please verify your mobile number",
        "Enter the 6-digit confirmation code",
    ])
    def test_matches_otp_prompts(self, text):
        assert pd.looks_like_otp(text) is True

    @pytest.mark.parametrize("text", [
        "",
        None,
        "Thanks! Here's your code: WELCOME15",
        "You're subscribed — check your email for your discount",
        "Welcome to the club!",
    ])
    def test_does_not_match_success_or_empty(self, text):
        # A "here's your discount code" success must NOT read as an OTP prompt.
        assert pd.looks_like_otp(text) is False


class TestDetectBotBlock:
    def test_body_text_fires(self):
        page = FakePage(body_text="Access is temporarily restricted")
        assert pd.detect_bot_block(page) is True

    def test_datadome_iframe_fires(self):
        """Etsy: parent page is empty, block UI is in a DataDome iframe."""
        page = FakePage(
            body_text="",
            frame_urls=[
                "https://www.etsy.com/",
                "https://geo.captcha-delivery.com/captcha/?initialCid=ABC",
            ],
            eval_text="",
        )
        assert pd.detect_bot_block(page) is True

    def test_cloudflare_turnstile_iframe_fires(self):
        page = FakePage(
            body_text="",
            frame_urls=[
                "https://www.teepublic.com/",
                "https://challenges.cloudflare.com/turnstile/v0/api.js",
            ],
            eval_text="",
        )
        assert pd.detect_bot_block(page) is True

    def test_recaptcha_iframe_fires(self):
        page = FakePage(
            body_text="",
            frame_urls=["https://shop.com/", "https://www.google.com/recaptcha/api2"],
            eval_text="",
        )
        assert pd.detect_bot_block(page) is True

    def test_normal_shop_passes(self):
        page = FakePage(
            body_text="Welcome to ShopName — get 10% off!",
            frame_urls=["https://shop.com/"],
            eval_text="",
        )
        assert pd.detect_bot_block(page) is False

    def test_js_evaluated_text_fires_when_body_empty(self):
        """Some blocks render via pseudo-elements that inner_text misses."""
        page = FakePage(
            body_text="",
            frame_urls=["https://shop.com/"],
            eval_text="Are you a robot? Please verify.",
        )
        assert pd.detect_bot_block(page) is True


# ---------------------------------------------------------------------------
# detect_success + extract_code_from_text
# ---------------------------------------------------------------------------

class TestExtractCode:
    def test_finds_plausible_code(self):
        assert pd.extract_code_from_text("Use code WELCOME15 at checkout!") == "WELCOME15"

    def test_finds_alphanumeric_klaviyo_code(self):
        assert pd.extract_code_from_text("Your code: 7KXQ4PMV") == "7KXQ4PMV"

    def test_rejects_marketing_acronyms(self):
        assert pd.extract_code_from_text("Reply STOP to unsubscribe") is None

    def test_rejects_bare_year(self):
        assert pd.extract_code_from_text("Copyright 2025") is None

    def test_returns_none_on_empty(self):
        assert pd.extract_code_from_text("") is None
        assert pd.extract_code_from_text(None) is None


class TestDetectSuccess:
    def test_success_message_wins(self):
        popup = FakeLocator(visible=True, text="Thanks! Your code: WELCOME15")
        page = FakePage(url="https://shop.com")
        success, code = pd.detect_success(page, popup, original_url="https://shop.com")
        assert success is True
        assert code == "WELCOME15"

    def test_url_change_signals_success(self):
        popup = FakeLocator(visible=True, text="")
        page = FakePage(url="https://shop.com/thanks", body_text="")
        success, code = pd.detect_success(page, popup, original_url="https://shop.com")
        assert success is True
        assert code is None

    def test_popup_closed_signals_success(self):
        popup = FakeLocator(visible=False, text="")
        page = FakePage(url="https://shop.com", body_text="")
        success, code = pd.detect_success(page, popup, original_url="https://shop.com")
        assert success is True

    def test_no_signals_means_failure(self):
        # Popup still visible, URL unchanged, no success text → failure.
        popup = FakeLocator(visible=True, text="Please enter a valid email")
        page = FakePage(url="https://shop.com", body_text="Please enter a valid email")
        success, code = pd.detect_success(page, popup, original_url="https://shop.com")
        assert success is False
        assert code is None

    def test_error_dialog_overrides_popup_closed(self):
        # Live regression (#14, aniqi): Klaviyo re-rendered its popup into an
        # error dialog, detaching the original locator — "popup closed" must
        # not win over a visible submission-error message.
        popup = FakeLocator(visible=False, text="")
        page = FakePage(
            url="https://shop.com",
            body_text="An error occurred when submitting. Please try again later.",
        )
        success, code = pd.detect_success(page, popup, original_url="https://shop.com")
        assert success is False
        assert code is None

    def test_error_dialog_overrides_success_text(self):
        # If both an error and a stray success-ish word are visible, the
        # error (specific to this submit) wins over the generic match.
        popup = FakeLocator(
            visible=True,
            text="Welcome to our shop — something went wrong, try again later",
        )
        page = FakePage(url="https://shop.com")
        success, code = pd.detect_success(page, popup, original_url="https://shop.com")
        assert success is False

    def test_no_code_mined_from_body_without_success_message(self):
        # Live regression (#14, aniqi): popup-closed success fell back to the
        # full homepage body for text and mined "SHIPPING" out of a
        # free-shipping banner. No success message → no code extraction.
        popup = FakeLocator(visible=False, text="")
        page = FakePage(
            url="https://shop.com",
            body_text="FREE SHIPPING ON ALL ORDERS OVER $50",
        )
        success, code = pd.detect_success(page, popup, original_url="https://shop.com")
        assert success is True   # popup closed, no error visible
        assert code is None


# ---------------------------------------------------------------------------
# Cookie / consent banner dismissal (Phase 5)
# ---------------------------------------------------------------------------

class TestDismissCookieBanner:
    def test_none_when_no_banner(self):
        assert pd.dismiss_cookie_banner(FakePage()) is None

    def test_prefers_reject_over_accept(self):
        reject = FakeLocator(visible=True)
        accept = FakeLocator(visible=True)
        page = FakePage(selectors={
            "#onetrust-reject-all-handler": reject,
            "#onetrust-accept-btn-handler": accept,
        })
        assert pd.dismiss_cookie_banner(page) == "reject"
        assert reject.click_count == 1
        assert accept.click_count == 0

    def test_falls_back_to_accept(self):
        accept = FakeLocator(visible=True)
        page = FakePage(selectors={"#onetrust-accept-btn-handler": accept})
        assert pd.dismiss_cookie_banner(page) == "accept"
        assert accept.click_count == 1

    def test_invisible_control_is_skipped(self):
        page = FakePage(selectors={
            "#onetrust-reject-all-handler": FakeLocator(visible=False),
        })
        assert pd.dismiss_cookie_banner(page) is None

    def test_click_failure_is_isolated(self):
        class _Boom(FakeLocator):
            def click(self, timeout=None):
                raise RuntimeError("intercepted by overlay")
        page = FakePage(selectors={
            "#onetrust-reject-all-handler": _Boom(visible=True),
        })
        assert pd.dismiss_cookie_banner(page) is None

    def test_reject_selectors_are_privacy_first(self):
        # Every reject selector must be tried before any accept selector so the
        # privacy-preserving option wins when both are present.
        assert pd.COOKIE_REJECT_SELECTORS
        assert pd.COOKIE_ACCEPT_SELECTORS
        assert not (set(pd.COOKIE_REJECT_SELECTORS)
                    & set(pd.COOKIE_ACCEPT_SELECTORS))
