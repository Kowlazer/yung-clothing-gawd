"""Claude-based extraction of the *items* array from order-confirmation
emails.

This is the only part of the wardrobe pipeline that genuinely needs a
language model. Shop name, order total, currency, tracking URL,
purchased_at, shipped_at — all deterministic via ``src/order_parse.py``
and the Gmail Date header. Shipping emails don't touch this module at
all anymore.

What's left for the model: parsing the **items list** out of templated
HTML / text bodies that look completely different across shops (Shopify,
Amazon, WooCommerce, Squarespace, custom). For each item we want
``{name, size, color, qty, price}``.

Batched calls (15 emails per call) keep individual requests well under
the 30K input-tokens-per-minute tier. Uses Haiku 4.5 — accurate enough
for templated item extraction, ~10x higher rate limits than Sonnet,
~5x cheaper.

Note: prompt caching does NOT engage here. The cached prefix (system
prompt + tool schema) is ~1.4K tokens, below Haiku 4.5's 4096-token
minimum cacheable prefix, so the ``cache_control`` marker on the system
block is a silent no-op (``cache_read_input_tokens`` stays 0). It is
left in place so caching activates automatically if the prefix ever
grows past 4096 tokens. Padding the prefix solely to cross that
threshold isn't worth it: this is a rare manual command and the prefix
is only ~10% of a typical batch's input.

Public API::

    extract_items(
        order_emails:  [{"email_id", "from", "subject", "body_excerpt",
                         "date_hint"}],
        *,
        client=None, model="claude-haiku-4-5-20251001",
    ) -> {
        "orders": [{email_id, items: [{name, size, color, qty, price}]}],
        "usage":  {...} | None,
    }

If ``order_emails`` is empty the function short-circuits and returns
the empty skeleton without calling the API.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from src.wardrobe_categories import CATEGORY_ORDER, prompt_category_list

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 8192

DEFAULT_BATCH_SIZE = 15
DEFAULT_INTER_BATCH_SLEEP_S = 2.0
_RATE_LIMIT_BACKOFF_S = 70.0


SYSTEM_PROMPT = """You are the items-extraction step of a personal
wardrobe-tracking tool.

A Python script has already done everything except parsing the items
list out of each email body. It will fill in shop, total, dates, and
tracking URLs deterministically. Your single job is to return the
``items`` array.

You MUST respond by calling the ``submit_items`` tool exactly once.
Never reply with free-form text.

# Per email you receive:
  - email_id     opaque string; echo back as `email_id` in your result
  - from         the sender header (use only as a tiebreaker for ambiguity)
  - subject      the subject line
  - body_excerpt up to ~2KB of the body text — this contains the items list

# Extract per item:
  - name      Product title, cleaned of SKU codes and color/size duplication
              when those live in their own fields. Remove trailing
              " - OUTLET", " - FINAL SALE", "(Pre-order)" style modifiers
              when the rest of the title is intact; keep them if removing
              would leave the name ambiguous.
  - size      The size string as printed (e.g. "M", "L / 32", "10.5",
              "Medium"). Null if no size is shown for that item.
  - color     The color name as printed. Null if not shown. DO NOT
              put a product variant name here that isn't really a color
              — e.g. headbands named "Arcadia" or "Vibrant Thrush" are
              pattern names, not colors. Use null when uncertain.
  - qty       Integer quantity. Default to 1 if not stated.
  - price     {"amount": float, "currency": "USD"|"EUR"|...} for the
              per-item price, or null if no per-item price is shown.
  - category  The garment type, chosen from the fixed list below. You see the
              full email body, so use it: a design-named item from a t-shirt
              shop is "tshirt"; a clearly non-garment line is "non_clothing".
              Use "other" for a garment whose type is unclear; omit / null
              only if you truly cannot tell.

# Categories (pick one key for `category`)
""" + prompt_category_list() + """

# Rules
  * Skip line items that are clearly shipping, tax, or discount lines
    (do not emit "Shipping $5.00" as an item).
  * Skip non-clothing items if the sender is a multi-category store
    (e.g. an Amazon order containing a book — exclude the book; if the
    order is ALL non-clothing, return an empty items list).
  * If you're unsure whether an item is clothing, INCLUDE it. The user
    will prune false positives during the fit-review pass; missing
    purchases are worse than including extras.
  * Leave fields null when uncertain rather than guessing.

# Identifiers
Every input task carries a ``task_id``. Echo the same ``task_id`` back
in each result so the script can match responses to requests. Return one
result per input task; do not invent extras.

# Output
Call ``submit_items`` with one array, ``orders``. Each entry has
``task_id``, ``email_id``, and ``items``. ``items`` may be empty if the
email had no extractable items. Do not write any text outside the tool
call."""


TOOL_SCHEMA: dict[str, Any] = {
    "name": "submit_items",
    "description": "Submit the parsed items array for each order email.",
    "input_schema": {
        "type": "object",
        "properties": {
            "orders": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "email_id": {"type": "string"},
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "size": {"type": ["string", "null"]},
                                    "color": {"type": ["string", "null"]},
                                    "qty": {"type": ["integer", "null"]},
                                    "price": {
                                        "type": ["object", "null"],
                                        "properties": {
                                            "amount": {"type": "number"},
                                            "currency": {"type": "string"},
                                        },
                                    },
                                    "category": {
                                        "type": ["string", "null"],
                                        "enum": list(CATEGORY_ORDER) + [None],
                                    },
                                },
                                "required": ["name"],
                            },
                        },
                    },
                    "required": ["task_id", "email_id", "items"],
                },
            },
        },
        "required": ["orders"],
    },
}


def _build_payload(tasks: list[dict]) -> str:
    return json.dumps({"tasks": tasks}, indent=2, ensure_ascii=False)


def _call_claude(client: Any, model: str, payload: str) -> tuple[dict, Any]:
    """One batched call with explicit 70-second backoff on 429.

    The Anthropic SDK retries on 429 with sub-second waits, which isn't
    enough on the 30K/min tier when a prior batch has just consumed the
    minute's budget. We catch and sleep one full window before retrying.
    """
    def _do_call():
        return client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                # No-op at the current ~1.4K-token prefix (< Haiku's 4096-token
                # minimum); activates automatically if the prefix grows. See
                # module docstring.
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "submit_items"},
            messages=[{"role": "user", "content": payload}],
        )

    try:
        response = _do_call()
    except Exception as exc:
        if exc.__class__.__name__ == "RateLimitError":
            log.warning(
                "order_extract: 429 from Anthropic — sleeping %.0fs to let "
                "the per-minute window roll over", _RATE_LIMIT_BACKOFF_S,
            )
            time.sleep(_RATE_LIMIT_BACKOFF_S)
            response = _do_call()
        else:
            raise

    tool_input: dict | None = None
    for block in response.content:
        block_type = getattr(block, "type", None)
        if block_type == "tool_use" and getattr(block, "name", None) == "submit_items":
            tool_input = getattr(block, "input", None)
            break
    if tool_input is None:
        raise RuntimeError("order_extract: model did not call submit_items")
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


def _get_client(client: Any | None) -> Any:
    if client is not None:
        return client
    import anthropic
    return anthropic.Anthropic()


def _empty_result() -> dict:
    return {"orders": [], "usage": None}


def _accumulate_usage(running: dict | None, new: dict | None) -> dict | None:
    if running is None:
        return dict(new) if new else None
    if new is None:
        return running
    out = dict(running)
    for k, v in new.items():
        out[k] = (out.get(k) or 0) + (v or 0)
    return out


def extract_items(
    order_emails: list[dict],
    *,
    client: Any | None = None,
    model: str = DEFAULT_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    inter_batch_sleep_s: float = DEFAULT_INTER_BATCH_SLEEP_S,
) -> dict:
    """Send the batched items-extraction call(s). See module docstring.

    Each input::

        {"email_id": str, "from": str, "subject": str,
         "body_excerpt": str, "date_hint": str}

    Output orders are returned in input order across batches.
    """
    order_emails = order_emails or []
    if not order_emails:
        return _empty_result()

    api_client = _get_client(client)
    all_orders: list[dict] = []
    total_usage: dict | None = None

    def _chunks(seq: list, n: int):
        for i in range(0, len(seq), n):
            yield seq[i:i + n]

    call_count = 0
    for batch_idx, chunk in enumerate(_chunks(order_emails, batch_size)):
        if call_count > 0 and inter_batch_sleep_s > 0:
            time.sleep(inter_batch_sleep_s)
        tasks = [{
            "task_id": f"item_{batch_idx}_{i}",
            "email_id": em.get("email_id", ""),
            "from": em.get("from", ""),
            "subject": em.get("subject", ""),
            "body_excerpt": em.get("body_excerpt", ""),
        } for i, em in enumerate(chunk)]
        payload = _build_payload(tasks)
        log.info("order_extract: items batch %d (%d emails)", batch_idx, len(chunk))
        tool_input, usage = _call_claude(api_client, model, payload)
        all_orders.extend(tool_input.get("orders", []))
        total_usage = _accumulate_usage(total_usage, _usage_dict(usage))
        call_count += 1

    # Strip task_id from each order before returning.
    out_orders = [{k: v for k, v in o.items() if k != "task_id"} for o in all_orders]

    return {"orders": out_orders, "usage": total_usage}
