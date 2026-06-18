"""Tests for src/order_extract.py.

Post-refactor: order_extract only handles items extraction. Shop, total,
tracking URL, dates — all deterministic via src/order_parse.py. Shipping
emails don't touch Claude at all.

Anthropic SDK is stubbed via FakeAnthropicClient (same pattern as
test_claude_fuzzy). No real API calls.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.order_extract import (
    SYSTEM_PROMPT,
    TOOL_SCHEMA,
    _empty_result,
    _usage_dict,
    extract_items,
)


class _FakeMessages:
    def __init__(self, response):
        self._response = response
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


class FakeAnthropicClient:
    def __init__(self, tool_input: dict, usage: dict | None = None):
        content = [SimpleNamespace(
            type="tool_use",
            name="submit_items",
            input=tool_input,
        )]
        response = SimpleNamespace(
            content=content,
            usage=SimpleNamespace(**usage) if usage else None,
        )
        self.messages = _FakeMessages(response)


def _tool_input(orders=None) -> dict:
    return {"orders": orders or []}


class TestEmptyShortCircuit:
    def test_no_emails_returns_empty(self):
        assert extract_items([]) == _empty_result()

    def test_no_emails_does_not_call_client(self):
        client = FakeAnthropicClient(_tool_input())
        extract_items([], client=client)
        assert client.messages.last_kwargs is None


class TestExtractItems:
    def test_round_trip(self):
        tool_input = _tool_input(orders=[{
            "task_id": "item_0_0",
            "email_id": "gm1",
            "items": [{"name": "Aros Chino", "size": "32", "color": "Black",
                       "qty": 1, "price": {"amount": 120.0, "currency": "USD"}}],
        }])
        client = FakeAnthropicClient(tool_input)
        result = extract_items(
            [{"email_id": "gm1", "from": "shop@np.com", "subject": "Order #123",
              "body_excerpt": "..."}],
            client=client,
        )
        assert len(result["orders"]) == 1
        order = result["orders"][0]
        assert "task_id" not in order  # stripped
        assert order["email_id"] == "gm1"
        assert order["items"][0]["name"] == "Aros Chino"
        assert order["items"][0]["size"] == "32"

    def test_multiple_items_per_email(self):
        tool_input = _tool_input(orders=[{
            "task_id": "item_0_0",
            "email_id": "gm1",
            "items": [
                {"name": "Item A", "size": "M", "qty": 1, "price": None},
                {"name": "Item B", "size": "L", "qty": 2, "price": None},
            ],
        }])
        client = FakeAnthropicClient(tool_input)
        result = extract_items(
            [{"email_id": "gm1", "from": "x@y.com", "subject": "Order",
              "body_excerpt": "..."}],
            client=client,
        )
        assert len(result["orders"][0]["items"]) == 2


class TestCallWiring:
    def test_uses_cached_system_prompt(self):
        client = FakeAnthropicClient(_tool_input())
        extract_items(
            [{"email_id": "a", "from": "x@y.com", "subject": "Order",
              "body_excerpt": ""}],
            client=client,
        )
        system = client.messages.last_kwargs["system"]
        assert isinstance(system, list)
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert system[0]["text"] == SYSTEM_PROMPT

    def test_forces_submit_items_tool(self):
        client = FakeAnthropicClient(_tool_input())
        extract_items(
            [{"email_id": "a", "from": "x@y.com", "subject": "Order",
              "body_excerpt": ""}],
            client=client,
        )
        kw = client.messages.last_kwargs
        assert kw["tool_choice"] == {"type": "tool", "name": "submit_items"}
        assert kw["tools"][0]["name"] == "submit_items"

    def test_default_model_is_haiku(self):
        client = FakeAnthropicClient(_tool_input())
        extract_items(
            [{"email_id": "a", "from": "x", "subject": "Order", "body_excerpt": ""}],
            client=client,
        )
        # Haiku 4.5 is the default — meaningfully cheaper + higher rate limit
        # than Sonnet for the items-only workload.
        assert client.messages.last_kwargs["model"].startswith("claude-haiku-4-5")

    def test_model_override(self):
        client = FakeAnthropicClient(_tool_input())
        extract_items(
            [{"email_id": "a", "from": "x", "subject": "Order", "body_excerpt": ""}],
            client=client,
            model="claude-sonnet-4-6",
        )
        assert client.messages.last_kwargs["model"] == "claude-sonnet-4-6"

    def test_batches_when_over_batch_size(self):
        client = FakeAnthropicClient(_tool_input())
        orders = [
            {"email_id": f"gm{i}", "from": "x@y.com", "subject": "Order",
             "body_excerpt": "body"}
            for i in range(50)
        ]
        original_create = client.messages.create
        client.messages.create_calls = 0
        def counted(**kw):
            client.messages.create_calls += 1
            return original_create(**kw)
        client.messages.create = counted

        extract_items(orders, client=client, batch_size=20,
                      inter_batch_sleep_s=0)
        # 50 / 20 → 3 calls (20, 20, 10).
        assert client.messages.create_calls == 3

    def test_missing_tool_call_raises(self):
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hello")],
            usage=None,
        )
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: response))
        with pytest.raises(RuntimeError, match="submit_items"):
            extract_items(
                [{"email_id": "a", "from": "x", "subject": "Order",
                  "body_excerpt": ""}],
                client=client,
            )

    def test_payload_contains_only_items_fields(self):
        # The payload sent to Claude must NOT include shop, total,
        # purchased_at, tracking_url — those are computed in code and would
        # waste prompt tokens.
        client = FakeAnthropicClient(_tool_input())
        extract_items(
            [{"email_id": "a", "from": "x@y.com", "subject": "Order",
              "body_excerpt": "body text"}],
            client=client,
        )
        payload = json.loads(client.messages.last_kwargs["messages"][0]["content"])
        task = payload["tasks"][0]
        assert set(task.keys()) == {
            "task_id", "email_id", "from", "subject", "body_excerpt",
        }


class TestUsageDict:
    def test_returns_none_on_none(self):
        assert _usage_dict(None) is None

    def test_extracts_known_fields(self):
        usage = SimpleNamespace(
            input_tokens=100, output_tokens=50,
            cache_creation_input_tokens=10, cache_read_input_tokens=200,
        )
        d = _usage_dict(usage)
        assert d == {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 10,
            "cache_read_input_tokens": 200,
        }
