"""Claude classification of stored wardrobe items into the garment taxonomy.

Companion to ``src/order_extract.py``. Where extraction parses the *items*
array out of an order email, this assigns each **already-stored** item a
durable ``category`` from :mod:`src.wardrobe_categories`, so the wardrobe
browser can read a stored field instead of guessing from the name. Used by
``order_scan --classify`` to backfill the existing catalogue (see issue #18).

The model sees only what the catalogue stored — ``name`` (required), plus
``shop``, ``size`` and ``color`` as hints. That's weaker context than the
order email itself, but the shop + size signals are enough to fix the two
failure modes a name-only regex can't: design-only graphic tees ("Kitsune"
from a streetwear brand -> tshirt) and oddly-named non-clothing ("Hyken
Task Chair" -> non_clothing).

``is_clothing`` is **not** asked of the model — it's derived in the caller
(``category == "non_clothing"`` -> False) so the flag can never disagree
with the stored category.

Batched (default 40/call — each item is a short line, far smaller than a
full order-email body). Uses Haiku 4.5, same as extraction.

Public API::

    classify_items(
        items: [{"item_id", "name", "shop"?, "size"?, "color"?}],
        *, client=None, model="claude-haiku-4-5-20251001",
    ) -> {
        "results": [{"item_id", "category"}],   # one per input, input order
        "usage":   {...} | None,
    }

If ``items`` is empty the function short-circuits without calling the API.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from src.wardrobe_categories import (
    CATEGORY_ORDER,
    normalise_category,
    prompt_category_list,
)

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 8192

DEFAULT_BATCH_SIZE = 40
DEFAULT_INTER_BATCH_SLEEP_S = 2.0
_RATE_LIMIT_BACKOFF_S = 70.0

# Fallback when the model omits an item or returns an unknown key — a real
# garment we couldn't type is far more likely than junk, and "other" stays
# visible in the browser (whereas a wrong non_clothing would hide it).
_FALLBACK_CATEGORY = "other"


SYSTEM_PROMPT = """You classify already-purchased wardrobe items into a fixed
garment taxonomy.

Each item was parsed from a clothing-shop order email. You are given its
product name and, when known, the shop it came from and its size and color.
Assign each item the single best-fitting category KEY from this list:

""" + prompt_category_list() + """

# How to choose
  * Use the SHOP as a strong hint. A streetwear / anime / graphic apparel
    brand selling a design-named item ("Kitsune", "Raijin", "Ten Shadows")
    is almost always a t-shirt unless size or other cues say otherwise.
  * Use the SIZE as a cue. A numeric waist/inseam like "32" or "32x34" means
    pants or shorts; a shoe size like "10.5" means shoes; "S"/"M"/"L"/"XL"
    is generic — decide from the name.
  * For SHORTS, split by use: activewear brands (Fabletics, Gymshark,
    Kinetickings) and mesh / performance / running / training / lined /
    compression names are "shorts_athletic"; sweatshorts, chino, cargo,
    denim, lounge, fleece, board/swim, and graphic-print shorts are
    "shorts_casual". Use the generic "shorts" ONLY when you truly cannot tell.
  * Worn fabric accessories (belts, scarves, gloves, ties, bandanas, robes)
    are "accessory". But jewelry (necklaces, rings, chains, pendants),
    watches, sunglasses, and bags/totes/backpacks are "non_clothing".
  * "non_clothing" is anything NOT worn as a garment: homeware, decor,
    electronics, supplements, grooming, furniture, software, games, jewelry,
    sunglasses, bags.
  * If it is clearly clothing but you cannot tell the type, use "other"
    (NOT "non_clothing").

# Identifiers
Every input task carries a ``task_id``. Echo it back in each result so the
script can match responses to requests. Return exactly one result per input
task; do not invent extras or drop any.

# Output
Call ``submit_categories`` exactly once, with one array ``results``. Each
entry has ``task_id`` and ``category`` (one of the keys above). Do not write
any text outside the tool call."""


TOOL_SCHEMA: dict[str, Any] = {
    "name": "submit_categories",
    "description": "Submit the chosen category for each wardrobe item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "category": {"type": "string", "enum": list(CATEGORY_ORDER)},
                    },
                    "required": ["task_id", "category"],
                },
            },
        },
        "required": ["results"],
    },
}


def _build_payload(tasks: list[dict]) -> str:
    return json.dumps({"tasks": tasks}, indent=2, ensure_ascii=False)


def _call_claude(client: Any, model: str, payload: str) -> tuple[dict, Any]:
    """One batched call with explicit 70-second backoff on 429.

    Mirrors ``order_extract._call_claude`` — the SDK's sub-second 429 retries
    aren't enough on the 30K-tokens/min tier when a prior batch just spent the
    minute's budget, so we sleep one full window and retry once.
    """
    def _do_call():
        return client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                # No-op below Haiku's 4096-token minimum cacheable prefix;
                # left in place so caching activates if the prefix ever grows.
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "submit_categories"},
            messages=[{"role": "user", "content": payload}],
        )

    try:
        response = _do_call()
    except Exception as exc:
        if exc.__class__.__name__ == "RateLimitError":
            log.warning(
                "order_classify: 429 from Anthropic — sleeping %.0fs to let "
                "the per-minute window roll over", _RATE_LIMIT_BACKOFF_S,
            )
            time.sleep(_RATE_LIMIT_BACKOFF_S)
            response = _do_call()
        else:
            raise

    tool_input: dict | None = None
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "submit_categories":
            tool_input = getattr(block, "input", None)
            break
    if tool_input is None:
        raise RuntimeError("order_classify: model did not call submit_categories")
    return tool_input, getattr(response, "usage", None)


def _usage_dict(usage: Any) -> dict | None:
    if usage is None:
        return None
    out = {}
    for field in ("input_tokens", "output_tokens",
                  "cache_creation_input_tokens", "cache_read_input_tokens"):
        val = getattr(usage, field, None)
        if val is not None:
            out[field] = val
    return out or None


def _accumulate_usage(running: dict | None, new: dict | None) -> dict | None:
    if running is None:
        return dict(new) if new else None
    if new is None:
        return running
    out = dict(running)
    for k, v in new.items():
        out[k] = (out.get(k) or 0) + (v or 0)
    return out


def _get_client(client: Any | None) -> Any:
    if client is not None:
        return client
    import anthropic
    return anthropic.Anthropic()


def classify_items(
    items: list[dict],
    *,
    client: Any | None = None,
    model: str = DEFAULT_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    inter_batch_sleep_s: float = DEFAULT_INTER_BATCH_SLEEP_S,
) -> dict:
    """Classify stored wardrobe items into the garment taxonomy. See docstring.

    Each input::

        {"item_id": str, "name": str, "shop"?: str, "size"?: str, "color"?: str}

    Returns ``{"results": [{"item_id", "category"}], "usage": {...} | None}``,
    one result per input item, in input order. Items the model omits or labels
    with an unknown key fall back to ``"other"`` so nothing is silently lost.
    """
    items = items or []
    if not items:
        return {"results": [], "usage": None}

    api_client = _get_client(client)
    total_usage: dict | None = None
    # task_id -> raw category from the model, accumulated across batches.
    by_task: dict[str, str] = {}

    def _chunks(seq: list, n: int):
        for i in range(0, len(seq), n):
            yield seq[i:i + n]

    call_count = 0
    # Stable task ids so a result maps back to its item regardless of batching.
    tasked = [
        {
            "task_id": f"cls_{i}",
            "item_id": it.get("item_id", ""),
            "name": it.get("name", ""),
            "shop": it.get("shop") or "",
            "size": it.get("size") or "",
            "color": it.get("color") or "",
        }
        for i, it in enumerate(items)
    ]

    for chunk in _chunks(tasked, batch_size):
        if call_count > 0 and inter_batch_sleep_s > 0:
            time.sleep(inter_batch_sleep_s)
        # Drop item_id from the payload — the model only needs task_id + fields.
        payload = _build_payload([
            {k: v for k, v in t.items() if k != "item_id"} for t in chunk
        ])
        log.info("order_classify: batch %d (%d items)", call_count, len(chunk))
        tool_input, usage = _call_claude(api_client, model, payload)
        for r in tool_input.get("results") or []:
            tid = r.get("task_id")
            if tid is not None:
                by_task[tid] = r.get("category")
        total_usage = _accumulate_usage(total_usage, _usage_dict(usage))
        call_count += 1

    results = [
        {
            "item_id": t["item_id"],
            "category": normalise_category(by_task.get(t["task_id"])) or _FALLBACK_CATEGORY,
        }
        for t in tasked
    ]
    return {"results": results, "usage": total_usage}
