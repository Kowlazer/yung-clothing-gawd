"""Claude vision/DOM fallback for newsletter-popup detection — Phase 4.

When the deterministic ``popup_detect`` heuristics miss (no vendor selector or
generic dialog matched, or a matched popup exposed no fillable email/phone +
submit), the caller hands the live page to Claude: a screenshot plus a compact
JSON digest of every candidate form control, and Claude returns which elements
are the newsletter **email input**, **phone input**, and **submit button**.

Robust element addressing without brittle model-authored selectors
------------------------------------------------------------------
Rather than ask Claude to invent CSS selectors (which frequently don't resolve
against the real DOM), ``build_dom_digest`` **stamps** each candidate element
with a ``data-scc-idx="N"`` attribute and describes it by that integer index.
Claude returns indices; the caller resolves them with the exact-match locator
``[data-scc-idx="N"]`` on the *same* page instance. The attribute is a
throwaway on a headless page we're about to close, so the mutation is harmless.

Cost + isolation
----------------
One batched tool call per shop that *falls back* (the heuristic path handles
the vast majority). ``locate_form`` is fully failure-isolated — any error
(screenshot failure, API error, model declining the tool, malformed indices)
logs a warning and returns ``None`` so the signup batch proceeds on the
heuristic result. The client pattern mirrors :mod:`src.claude_fuzzy`.

The module imports ``playwright`` nowhere and only calls ``page.evaluate`` /
``page.screenshot`` on the passed object, so its pure logic (``parse_result``,
``_coerce_index``) is unit-tested without Chromium, and the impure helpers are
exercised with fakes.
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any

from src.claude_fuzzy import DEFAULT_MODEL

log = logging.getLogger(__name__)

# Small output — the tool returns at most four integers + a confidence label.
MAX_TOKENS = 512

# CSS candidates fed to the stamper: form controls, buttons, and container
# shapes (so Claude can see the popup boundary). Deliberately narrow — links,
# images, and layout divs are noise that only dilutes the digest.
_CANDIDATE_SELECTOR = (
    "input, textarea, button, [role='button'], "
    "form, dialog, [role='dialog'], [aria-modal='true']"
)
# Cap the number of stamped candidates (keeps the digest small + bounds cost).
_MAX_CANDIDATES = 60
# Hard char cap on the DOM digest handed to Claude (~10 KB of JSON).
_DOM_CHAR_LIMIT = 12_000

# Confidences we act on. ``low`` is dropped — a wrong guess would fill the
# wrong field / click the wrong button, worse than falling through to the
# recorded heuristic miss.
_ACCEPTED_CONFIDENCE = frozenset({"high", "medium"})

_INDEX_ATTR = "data-scc-idx"
_CONTAINER_ATTR = "data-scc-container"


# ---------------------------------------------------------------------------
# In-page JS — stamp candidates + collect descriptors; stamp a container
# ---------------------------------------------------------------------------

# Returns a JSON string: a list of ``{i, tag, type, name, id, placeholder,
# aria, cls, text, container, visible}`` descriptors, one per stamped element.
# Invisible non-container controls are skipped entirely (never stamped) so
# Claude can't point us at something the user can't interact with.
_DIGEST_JS = """
(args) => {
  const els = Array.from(document.querySelectorAll(args.sel));
  const out = [];
  let idx = 0;
  for (const el of els) {
    if (idx >= args.maxN) break;
    const rect = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || '';
    const isContainer = tag === 'form' || tag === 'dialog' ||
                        role === 'dialog' ||
                        el.getAttribute('aria-modal') === 'true';
    const visible = rect.width > 1 && rect.height > 1 &&
                    st.visibility !== 'hidden' && st.display !== 'none' &&
                    st.opacity !== '0';
    if (!visible && !isContainer) continue;
    el.setAttribute(args.attr, String(idx));
    let text = '';
    try { text = (el.innerText || el.value || '').trim().slice(0, 60); } catch (e) {}
    out.push({
      i: idx,
      tag: tag,
      type: el.getAttribute('type') || '',
      name: el.getAttribute('name') || '',
      id: el.id || '',
      placeholder: el.getAttribute('placeholder') || '',
      aria: el.getAttribute('aria-label') || '',
      cls: (el.getAttribute('class') || '').slice(0, 80),
      text: text,
      container: isContainer,
      visible: visible,
    });
    idx++;
  }
  return JSON.stringify(out);
}
"""

# Stamp the nearest form/dialog ancestor of element ``idx`` as the container
# (used for post-submit success detection). Returns true iff one was stamped.
_CONTAINER_JS = """
(args) => {
  const el = document.querySelector('[' + args.attr + '="' + args.idx + '"]');
  if (!el) return false;
  const c = el.closest('form, dialog, [role="dialog"], [aria-modal="true"]');
  document.querySelectorAll('[' + args.cattr + ']').forEach(
    (e) => e.removeAttribute(args.cattr));
  if (!c) return false;
  c.setAttribute(args.cattr, '1');
  return true;
}
"""


# ---------------------------------------------------------------------------
# Prompt + tool schema
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You locate a shop's NEWSLETTER / EMAIL-or-SMS SIGNUP form on a web page.\n"
    "\n"
    "You are given a screenshot of the page and a JSON list of candidate "
    "elements. Each candidate has an integer `i` (its index) plus attributes "
    "(tag, type, name, id, placeholder, aria, class, text, container, "
    "visible).\n"
    "\n"
    "Return, via the locate_signup_form tool:\n"
    "  - email_index: the index of the text/email input where the shopper "
    "types their EMAIL to subscribe, or null if there is none.\n"
    "  - phone_index: the index of the input for a PHONE / SMS number to "
    "subscribe, or null if there is none.\n"
    "  - submit_index: the index of the button that SUBMITS the signup "
    "(Subscribe / Sign up / Join / Get code / Unlock ...), or null.\n"
    "  - found: true only if this is a genuine newsletter/SMS signup with a "
    "submit button AND at least one of email/phone. Otherwise false.\n"
    "  - confidence: high / medium / low.\n"
    "\n"
    "Rules:\n"
    "  - Pick the MARKETING signup, never a login, account-creation, search, "
    "checkout, address, or coupon-apply field.\n"
    "  - The submit must be the button that subscribes — never a 'No thanks', "
    "'Close', 'Decline', or 'Continue shopping' control.\n"
    "  - Prefer visible candidates. If nothing on the page is a newsletter "
    "signup, set found=false and all indices null.\n"
    "  - Only use indices that appear in the candidate list."
)

TOOL_SCHEMA: dict[str, Any] = {
    "name": "locate_signup_form",
    "description": "Report which candidate elements form the newsletter signup.",
    "input_schema": {
        "type": "object",
        "properties": {
            "found": {"type": "boolean"},
            "email_index": {"type": ["integer", "null"]},
            "phone_index": {"type": ["integer", "null"]},
            "submit_index": {"type": ["integer", "null"]},
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
            },
        },
        "required": ["found", "confidence"],
    },
}


# ---------------------------------------------------------------------------
# Result model + pure parsing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClaudeForm:
    """A newsletter form Claude located, addressed by stamped element index."""
    submit_index: int
    email_index: int | None
    phone_index: int | None
    confidence: str


def _coerce_index(value: Any) -> int | None:
    """Coerce a model-returned index to a non-negative int, else None.

    Tolerates ints and digit strings; rejects negatives, floats-as-str, and
    anything unparseable (a hallucinated ``"e"`` → None, not a crash)."""
    if isinstance(value, bool):  # bool is an int subclass — never an index
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return int(s)
    return None


def parse_result(tool_input: dict | None) -> ClaudeForm | None:
    """Turn the tool payload into a :class:`ClaudeForm`, or None.

    None (fall through to the heuristic miss) when: the model reported no form,
    confidence is below :data:`_ACCEPTED_CONFIDENCE`, there is no submit index,
    or neither an email nor a phone index is present (nothing to fill).
    """
    if not tool_input or not tool_input.get("found"):
        return None
    confidence = str(tool_input.get("confidence") or "").lower()
    if confidence not in _ACCEPTED_CONFIDENCE:
        return None
    submit = _coerce_index(tool_input.get("submit_index"))
    if submit is None:
        return None
    email = _coerce_index(tool_input.get("email_index"))
    phone = _coerce_index(tool_input.get("phone_index"))
    if email is None and phone is None:
        return None
    return ClaudeForm(
        submit_index=submit, email_index=email, phone_index=phone,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Impure browser helpers
# ---------------------------------------------------------------------------

def build_dom_digest(page: Any, *, char_limit: int = _DOM_CHAR_LIMIT) -> str:
    """Stamp candidate elements on ``page`` and return their JSON digest.

    Best-effort: any evaluate error returns ``""`` (the caller then skips the
    fallback for this shop). Truncated to ``char_limit`` chars.
    """
    try:
        digest = page.evaluate(
            _DIGEST_JS,
            {"sel": _CANDIDATE_SELECTOR, "maxN": _MAX_CANDIDATES,
             "attr": _INDEX_ATTR},
        ) or ""
    except Exception as exc:  # noqa: BLE001 — never let digest capture abort
        log.info("popup_claude: dom digest failed: %s", exc)
        return ""
    return digest[:char_limit]


def capture_screenshot_b64(page: Any) -> str | None:
    """Base64-encoded PNG screenshot of ``page``, or None on failure."""
    try:
        png = page.screenshot(full_page=False)
    except Exception as exc:  # noqa: BLE001 — screenshot is optional context
        log.info("popup_claude: screenshot failed: %s", exc)
        return None
    if not png:
        return None
    return base64.b64encode(png).decode("ascii")


def stamp_container(page: Any, index: int | None) -> Any | None:
    """Stamp + return a locator for the form/dialog wrapping element ``index``.

    Used as the container for post-submit success detection. None when there's
    no index or no wrapping container (the caller then falls back to a smaller
    surface, e.g. the submit button, so ``detect_success`` degrades to page
    body text instead of a bogus "popup closed" signal).
    """
    if index is None:
        return None
    try:
        ok = page.evaluate(
            _CONTAINER_JS,
            {"attr": _INDEX_ATTR, "idx": str(index), "cattr": _CONTAINER_ATTR},
        )
    except Exception as exc:  # noqa: BLE001
        log.info("popup_claude: container stamp failed: %s", exc)
        return None
    if not ok:
        return None
    try:
        return page.locator(f"[{_CONTAINER_ATTR}='1']").first
    except Exception:  # noqa: BLE001
        return None


def index_locator(page: Any, index: int | None) -> Any | None:
    """Locator for a stamped candidate by index, or None for a null index."""
    if index is None:
        return None
    try:
        return page.locator(f"[{_INDEX_ATTR}='{index}']").first
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Claude call
# ---------------------------------------------------------------------------

def _user_content(digest: str, screenshot_b64: str | None) -> list[dict]:
    """Message content blocks: optional screenshot image + the DOM digest."""
    blocks: list[dict] = []
    if screenshot_b64:
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": screenshot_b64,
            },
        })
    blocks.append({
        "type": "text",
        "text": "Candidate elements (JSON):\n" + digest,
    })
    return blocks


def _call_locate(
    client: Any,
    model: str,
    digest: str,
    screenshot_b64: str | None,
) -> tuple[dict, Any]:
    """Send the batched locate call; return (parsed tool input, usage)."""
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "locate_signup_form"},
        messages=[{"role": "user", "content": _user_content(digest, screenshot_b64)}],
    )
    tool_input: dict | None = None
    for block in response.content:
        if (getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == "locate_signup_form"):
            tool_input = getattr(block, "input", None)
            break
    if tool_input is None:
        raise RuntimeError("popup_claude: model did not call locate_signup_form")
    return tool_input, getattr(response, "usage", None)


def locate_form(
    page: Any,
    *,
    client: Any,
    model: str = DEFAULT_MODEL,
    want_screenshot: bool = True,
    char_limit: int = _DOM_CHAR_LIMIT,
) -> ClaudeForm | None:
    """Locate a newsletter form on ``page`` via Claude, or None.

    Failure-isolated end to end: a digest/screenshot/API/parse failure logs a
    warning and returns None so the signup batch keeps going on the heuristic
    result. Stamps ``data-scc-idx`` on the page's candidates as a side effect
    (the caller resolves the returned indices against the same page).
    """
    digest = build_dom_digest(page, char_limit=char_limit)
    if not digest or digest == "[]":
        log.info("popup_claude: no candidate elements to send")
        return None
    screenshot = capture_screenshot_b64(page) if want_screenshot else None
    try:
        tool_input, usage = _call_locate(client, model, digest, screenshot)
    except Exception as exc:  # noqa: BLE001 — API/refusal must not abort the batch
        log.warning("popup_claude: locate call failed: %s", exc)
        return None
    form = parse_result(tool_input)
    if form is None:
        log.info("popup_claude: no usable form (input=%s)", tool_input)
    else:
        in_tok = getattr(usage, "input_tokens", None)
        out_tok = getattr(usage, "output_tokens", None)
        log.info(
            "popup_claude: located form (email=%s phone=%s submit=%s conf=%s"
            " tokens=%s/%s)",
            form.email_index, form.phone_index, form.submit_index,
            form.confidence, in_tok, out_tok,
        )
    return form
