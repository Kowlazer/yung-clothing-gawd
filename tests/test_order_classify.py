"""Tests for src/order_classify.py.

Mirrors test_order_extract: the Anthropic SDK is stubbed (no real calls).
``_MapClient`` is a smarter fake that reads the tasks out of the payload and
answers each by name, so round-trip mapping (task_id -> item_id -> category)
can be asserted across batches.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.order_classify import (
    DEFAULT_BATCH_SIZE,
    SYSTEM_PROMPT,
    TOOL_SCHEMA,
    classify_items,
)


class _FakeMessages:
    def __init__(self, response):
        self._response = response
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


class FakeAnthropicClient:
    """Returns one fixed ``submit_categories`` tool call for every request."""

    def __init__(self, results=None, usage: dict | None = None):
        content = [SimpleNamespace(
            type="tool_use",
            name="submit_categories",
            input={"results": results or []},
        )]
        response = SimpleNamespace(
            content=content,
            usage=SimpleNamespace(**usage) if usage else None,
        )
        self.messages = _FakeMessages(response)


class _MapClient:
    """Answers each task by looking its ``name`` up in ``name_to_cat``.

    Unknown names get whatever ``default`` is (use an invalid key to exercise
    the fallback path). Counts calls so batching can be asserted.
    """

    def __init__(self, name_to_cat: dict[str, str], default: str | None = None):
        self._map = name_to_cat
        self._default = default
        self.calls = 0
        self.messages = self  # so client.messages.create works

    def create(self, **kwargs):
        self.calls += 1
        payload = json.loads(kwargs["messages"][0]["content"])
        results = []
        for task in payload["tasks"]:
            cat = self._map.get(task["name"], self._default)
            entry = {"task_id": task["task_id"]}
            if cat is not None:
                entry["category"] = cat
            results.append(entry)
        return SimpleNamespace(
            content=[SimpleNamespace(
                type="tool_use", name="submit_categories",
                input={"results": results},
            )],
            usage=None,
        )


class TestEmptyShortCircuit:
    def test_no_items_returns_empty(self):
        assert classify_items([]) == {"results": [], "usage": None}

    def test_no_items_does_not_call_client(self):
        client = FakeAnthropicClient()
        classify_items([], client=client)
        assert client.messages.last_kwargs is None


class TestClassifyItems:
    def test_round_trip_maps_category_to_item(self):
        client = _MapClient({"Kitsune Tee": "tshirt", "Hyken Chair": "non_clothing"})
        result = classify_items(
            [
                {"item_id": "a", "name": "Kitsune Tee", "shop": "Sumie"},
                {"item_id": "b", "name": "Hyken Chair", "shop": "Staples"},
            ],
            client=client,
        )
        by_id = {r["item_id"]: r["category"] for r in result["results"]}
        assert by_id == {"a": "tshirt", "b": "non_clothing"}

    def test_results_in_input_order(self):
        client = _MapClient({"A": "tshirt", "B": "hoodie", "C": "pants"})
        result = classify_items(
            [{"item_id": str(i), "name": n} for i, n in enumerate("ABC")],
            client=client,
        )
        assert [r["item_id"] for r in result["results"]] == ["0", "1", "2"]

    def test_unknown_category_falls_back_to_other(self):
        client = _MapClient({}, default="totally-bogus")
        result = classify_items([{"item_id": "a", "name": "Mystery"}], client=client)
        assert result["results"][0]["category"] == "other"

    def test_missing_result_falls_back_to_other(self):
        # Model omits the only item — must still return one result, as "other".
        client = _MapClient({}, default=None)
        result = classify_items([{"item_id": "a", "name": "Ghost"}], client=client)
        assert result["results"] == [{"item_id": "a", "category": "other"}]

    def test_batches_when_over_batch_size(self):
        client = _MapClient({}, default="tshirt")
        items = [{"item_id": str(i), "name": f"Item {i}"} for i in range(95)]
        classify_items(items, client=client, batch_size=40, inter_batch_sleep_s=0)
        # 95 / 40 -> 3 calls (40, 40, 15).
        assert client.calls == 3

    def test_mapping_survives_batching(self):
        client = _MapClient({f"Item {i}": "tshirt" if i % 2 else "hoodie"
                             for i in range(95)})
        items = [{"item_id": str(i), "name": f"Item {i}"} for i in range(95)]
        result = classify_items(items, client=client, batch_size=40,
                                inter_batch_sleep_s=0)
        by_id = {r["item_id"]: r["category"] for r in result["results"]}
        assert by_id["1"] == "tshirt"
        assert by_id["2"] == "hoodie"
        assert len(by_id) == 95


class TestCallWiring:
    def test_uses_cached_system_prompt(self):
        client = FakeAnthropicClient()
        classify_items([{"item_id": "a", "name": "Tee"}], client=client)
        system = client.messages.last_kwargs["system"]
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert system[0]["text"] == SYSTEM_PROMPT

    def test_forces_submit_categories_tool(self):
        client = FakeAnthropicClient()
        classify_items([{"item_id": "a", "name": "Tee"}], client=client)
        kw = client.messages.last_kwargs
        assert kw["tool_choice"] == {"type": "tool", "name": "submit_categories"}
        assert kw["tools"][0]["name"] == "submit_categories"

    def test_default_model_is_haiku(self):
        client = FakeAnthropicClient()
        classify_items([{"item_id": "a", "name": "Tee"}], client=client)
        assert client.messages.last_kwargs["model"].startswith("claude-haiku-4-5")

    def test_payload_excludes_item_id(self):
        # The model only needs task_id + the descriptive fields; item_id is the
        # local join key and must not be sent.
        client = FakeAnthropicClient()
        classify_items(
            [{"item_id": "secret", "name": "Tee", "shop": "S", "size": "M",
              "color": "Black"}],
            client=client,
        )
        payload = json.loads(client.messages.last_kwargs["messages"][0]["content"])
        task = payload["tasks"][0]
        assert "item_id" not in task
        assert set(task.keys()) == {"task_id", "name", "shop", "size", "color"}

    def test_missing_tool_call_raises(self):
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hi")], usage=None,
        )
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: response))
        with pytest.raises(RuntimeError, match="submit_categories"):
            classify_items([{"item_id": "a", "name": "Tee"}], client=client)


class TestToolSchema:
    def test_enum_covers_taxonomy(self):
        from src.wardrobe_categories import CATEGORY_ORDER
        enum = TOOL_SCHEMA["input_schema"]["properties"]["results"]["items"][
            "properties"]["category"]["enum"]
        assert set(enum) == set(CATEGORY_ORDER)

    def test_default_batch_size_reasonable(self):
        assert DEFAULT_BATCH_SIZE >= 20
