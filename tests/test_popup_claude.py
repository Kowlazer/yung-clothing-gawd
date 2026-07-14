"""Tests for src/popup_claude.py — the Phase 4 Claude vision/DOM fallback.

Playwright and the Anthropic SDK are both faked. ``parse_result`` /
``_coerce_index`` are pure and tested directly; the impure browser + API
helpers are driven with minimal fakes that mimic only the surface the module
touches (``page.evaluate`` / ``page.screenshot`` / ``page.locator`` and the
``client.messages.create(...)`` tool-use shape).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src import popup_claude as pc


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakePage:
    """Scripts ``evaluate`` return values (or raises) + a screenshot payload."""

    def __init__(
        self,
        *,
        eval_result: Any = "[]",
        eval_raises: bool = False,
        screenshot: bytes | None = b"PNGBYTES",
        screenshot_raises: bool = False,
    ) -> None:
        self._eval_result = eval_result
        self._eval_raises = eval_raises
        self._screenshot = screenshot
        self._screenshot_raises = screenshot_raises
        self.eval_calls: list[tuple[str, Any]] = []
        self.locators: list[str] = []

    def evaluate(self, js: str, arg: Any = None) -> Any:
        self.eval_calls.append((js, arg))
        if self._eval_raises:
            raise RuntimeError("evaluate blew up")
        # Container-stamp JS returns a bool; digest JS returns the JSON string.
        if js is pc._CONTAINER_JS:
            return self._eval_result
        return self._eval_result

    def screenshot(self, full_page: bool = False) -> bytes | None:  # noqa: ARG002
        if self._screenshot_raises:
            raise RuntimeError("screenshot blew up")
        return self._screenshot

    def locator(self, selector: str) -> Any:
        self.locators.append(selector)
        return SimpleNamespace(first=f"loc:{selector}")


class _FakeMessages:
    def __init__(self, response: Any, raises: bool = False) -> None:
        self._response = response
        self._raises = raises
        self.last_kwargs: dict | None = None

    def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        if self._raises:
            raise RuntimeError("api down")
        return self._response


class FakeAnthropicClient:
    """Stand-in for anthropic.Anthropic with the messages.create tool shape."""

    def __init__(self, tool_input: dict | None, *, usage: dict | None = None,
                 no_tool: bool = False, raises: bool = False) -> None:
        if no_tool:
            content = [SimpleNamespace(type="text", text="no tool here")]
        else:
            content = [SimpleNamespace(
                type="tool_use", name="locate_signup_form", input=tool_input,
            )]
        response = SimpleNamespace(
            content=content,
            usage=SimpleNamespace(**usage) if usage else None,
        )
        self.messages = _FakeMessages(response, raises=raises)


def _found(email=0, phone=1, submit=2, confidence="high", found=True):
    return {
        "found": found, "email_index": email, "phone_index": phone,
        "submit_index": submit, "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# _coerce_index
# ---------------------------------------------------------------------------

class TestCoerceIndex:
    def test_int(self):
        assert pc._coerce_index(3) == 3

    def test_zero_ok(self):
        assert pc._coerce_index(0) == 0

    def test_digit_string(self):
        assert pc._coerce_index("5") == 5

    def test_negative_is_none(self):
        assert pc._coerce_index(-1) is None

    def test_none(self):
        assert pc._coerce_index(None) is None

    def test_bool_rejected(self):
        # bool is an int subclass but never a valid element index.
        assert pc._coerce_index(True) is None
        assert pc._coerce_index(False) is None

    def test_garbage_string(self):
        assert pc._coerce_index("e") is None
        assert pc._coerce_index("1.5") is None
        assert pc._coerce_index("") is None


# ---------------------------------------------------------------------------
# parse_result
# ---------------------------------------------------------------------------

class TestParseResult:
    def test_happy_both_fields(self):
        form = pc.parse_result(_found(email=0, phone=1, submit=2))
        assert form == pc.ClaudeForm(
            submit_index=2, email_index=0, phone_index=1, confidence="high",
        )

    def test_email_only(self):
        form = pc.parse_result(_found(email=0, phone=None, submit=3))
        assert form.email_index == 0 and form.phone_index is None

    def test_phone_only(self):
        form = pc.parse_result(_found(email=None, phone=1, submit=2))
        assert form.email_index is None and form.phone_index == 1

    def test_medium_confidence_accepted(self):
        assert pc.parse_result(_found(confidence="medium")) is not None

    def test_low_confidence_rejected(self):
        assert pc.parse_result(_found(confidence="low")) is None

    def test_unknown_confidence_rejected(self):
        assert pc.parse_result(_found(confidence="maybe")) is None

    def test_not_found_rejected(self):
        assert pc.parse_result(_found(found=False)) is None

    def test_no_submit_rejected(self):
        assert pc.parse_result(_found(submit=None)) is None

    def test_no_fields_rejected(self):
        # Submit present but neither email nor phone → nothing to fill.
        assert pc.parse_result(_found(email=None, phone=None, submit=2)) is None

    def test_none_input(self):
        assert pc.parse_result(None) is None

    def test_empty_dict(self):
        assert pc.parse_result({}) is None

    def test_string_indices_coerced(self):
        form = pc.parse_result(_found(email="0", phone="1", submit="2"))
        assert form.submit_index == 2 and form.email_index == 0


# ---------------------------------------------------------------------------
# build_dom_digest
# ---------------------------------------------------------------------------

class TestBuildDomDigest:
    def test_returns_evaluate_output(self):
        page = FakePage(eval_result='[{"i":0,"tag":"input"}]')
        assert pc.build_dom_digest(page) == '[{"i":0,"tag":"input"}]'

    def test_passes_selector_and_cap(self):
        page = FakePage(eval_result="[]")
        pc.build_dom_digest(page)
        _, arg = page.eval_calls[0]
        assert arg["sel"] == pc._CANDIDATE_SELECTOR
        assert arg["maxN"] == pc._MAX_CANDIDATES
        assert arg["attr"] == pc._INDEX_ATTR

    def test_truncates_to_char_limit(self):
        page = FakePage(eval_result="x" * 100)
        assert pc.build_dom_digest(page, char_limit=10) == "x" * 10

    def test_failure_returns_empty(self):
        page = FakePage(eval_raises=True)
        assert pc.build_dom_digest(page) == ""


# ---------------------------------------------------------------------------
# capture_screenshot_b64
# ---------------------------------------------------------------------------

class TestScreenshot:
    def test_encodes_png(self):
        import base64
        page = FakePage(screenshot=b"PNGBYTES")
        assert pc.capture_screenshot_b64(page) == base64.b64encode(b"PNGBYTES").decode()

    def test_none_on_failure(self):
        page = FakePage(screenshot_raises=True)
        assert pc.capture_screenshot_b64(page) is None

    def test_none_on_empty_bytes(self):
        page = FakePage(screenshot=b"")
        assert pc.capture_screenshot_b64(page) is None


# ---------------------------------------------------------------------------
# index_locator / stamp_container
# ---------------------------------------------------------------------------

class TestLocators:
    def test_index_locator_none_index(self):
        assert pc.index_locator(FakePage(), None) is None

    def test_index_locator_resolves(self):
        page = FakePage()
        loc = pc.index_locator(page, 4)
        assert loc == "loc:[data-scc-idx='4']"

    def test_stamp_container_none_index(self):
        assert pc.stamp_container(FakePage(), None) is None

    def test_stamp_container_no_container_found(self):
        # container JS returns falsy → no container stamped.
        page = FakePage(eval_result=False)
        assert pc.stamp_container(page, 2) is None

    def test_stamp_container_resolves(self):
        page = FakePage(eval_result=True)
        loc = pc.stamp_container(page, 2)
        assert loc == f"loc:[{pc._CONTAINER_ATTR}='1']"

    def test_stamp_container_failure(self):
        page = FakePage(eval_raises=True)
        assert pc.stamp_container(page, 2) is None


# ---------------------------------------------------------------------------
# _call_locate
# ---------------------------------------------------------------------------

class TestCallLocate:
    def test_parses_tool_input(self):
        client = FakeAnthropicClient(_found(), usage={"input_tokens": 800})
        tool_input, usage = pc._call_locate(client, "m", "[]", None)
        assert tool_input["found"] is True
        assert usage.input_tokens == 800

    def test_forces_the_tool(self):
        client = FakeAnthropicClient(_found())
        pc._call_locate(client, "model-x", "[]", None)
        kw = client.messages.last_kwargs
        assert kw["tool_choice"] == {"type": "tool", "name": "locate_signup_form"}
        assert kw["model"] == "model-x"

    def test_includes_screenshot_block_when_present(self):
        client = FakeAnthropicClient(_found())
        pc._call_locate(client, "m", "[]", "BASE64DATA")
        content = client.messages.last_kwargs["messages"][0]["content"]
        kinds = [b["type"] for b in content]
        assert "image" in kinds
        img = [b for b in content if b["type"] == "image"][0]
        assert img["source"]["data"] == "BASE64DATA"

    def test_no_image_block_without_screenshot(self):
        client = FakeAnthropicClient(_found())
        pc._call_locate(client, "m", "[]", None)
        content = client.messages.last_kwargs["messages"][0]["content"]
        assert all(b["type"] != "image" for b in content)

    def test_raises_without_tool_use(self):
        client = FakeAnthropicClient(None, no_tool=True)
        with pytest.raises(RuntimeError):
            pc._call_locate(client, "m", "[]", None)


# ---------------------------------------------------------------------------
# locate_form — orchestration + isolation
# ---------------------------------------------------------------------------

class TestLocateForm:
    def test_happy_path(self):
        page = FakePage(eval_result='[{"i":0}]')
        client = FakeAnthropicClient(_found(email=0, phone=1, submit=2))
        form = pc.locate_form(page, client=client, want_screenshot=False)
        assert form.submit_index == 2

    def test_empty_digest_short_circuits(self, monkeypatch):
        page = FakePage(eval_result="[]")
        # A client whose create() would explode proves we never call it.
        client = FakeAnthropicClient(None, raises=True)
        assert pc.locate_form(page, client=client, want_screenshot=False) is None

    def test_api_error_isolated(self):
        page = FakePage(eval_result='[{"i":0}]')
        client = FakeAnthropicClient(_found(), raises=True)
        assert pc.locate_form(page, client=client, want_screenshot=False) is None

    def test_parse_none_returns_none(self):
        page = FakePage(eval_result='[{"i":0}]')
        client = FakeAnthropicClient(_found(found=False))
        assert pc.locate_form(page, client=client, want_screenshot=False) is None

    def test_screenshot_requested_when_enabled(self):
        page = FakePage(eval_result='[{"i":0}]')
        client = FakeAnthropicClient(_found())
        pc.locate_form(page, client=client, want_screenshot=True)
        content = client.messages.last_kwargs["messages"][0]["content"]
        assert any(b["type"] == "image" for b in content)
