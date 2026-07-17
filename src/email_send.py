"""Send the digest via the Resend API.

Ported from files/send_email.py (the battle-tested local sender). Differences:
- Drops CLI / stdin handling — call send_email() from main.py directly.
- Drops .env loading — config.py handles env in the GitHub Actions runtime.
- Raises EmailSendError on failure instead of sys.exit().
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

# Mirrors files/send_email.py — Cloudflare on api.resend.com blocks the default
# Python-urllib User-Agent, so we send a Mozilla one.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
# Content/URL exclude their own closing char so non-link brackets in the line
# (e.g. '$30 USD [CAD $45]' from the FX dual format) don't accidentally chain
# into a real [text](url) later on the same line.
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

# --- Visual-emphasis layer ("make drops / restocks pop") -------------------
# A change-section line arrives from digest.py prefixed with a leading state
# marker emoji; we turn it into a colored badge pill + a tinted callout card so
# the good news reads at a glance, and we recolor the high-signal section
# headers. Marker constants are imported from digest so the two can't drift.
# EVERYTHING is inlined on the elements (not just the <style> block) so it still
# renders if a client strips <style>, and the colours are tuned for the white
# background Gmail draws the message on.
from src.digest import _MARK_DROP, _MARK_STOCK, _MARK_LOW, _MARK_OOS, _MARK_FLAT

# marker → (kind, badge label)
_BADGE_BY_MARKER = {
    _MARK_DROP: ("drop", "Price drop"),
    _MARK_STOCK: ("stock", "Back in stock"),
    _MARK_LOW: ("low", "Low stock"),
    _MARK_OOS: ("oos", "Sold out"),
    _MARK_FLAT: ("flat", "Marked down"),
}

# kind → (badge text colour, badge tint background)
_PILL_COLORS = {
    "drop": ("#b23b30", "#fbeae7"),
    "stock": ("#1c7a4f", "#e4f3ea"),
    "low": ("#9a6a12", "#f8efd8"),
    "oos": ("#7a7269", "#efece8"),
    "flat": ("#6c655d", "#efece8"),
    "watch": ("#8a6d0f", "#fcf6e2"),
}

# kind → (card left-border colour, card row tint) — a lighter wash than the pill
# so a whole row of it stays readable.
_CARD_COLORS = {
    "drop": ("#d9695c", "#fdf3f1"),
    "stock": ("#5aa982", "#eff8f2"),
    "low": ("#cfa24e", "#fbf5e6"),
    "oos": ("#b7afa4", "#f5f3f0"),
    "flat": ("#b7afa4", "#f5f3f0"),
}

# Known section headers → semantic colour. Prefix match, so the "(specific
# URLs)" / "(non-clothing)" suffixes and the ⭐ prefix all still resolve. Covers
# the item-level change sections plus the shop-level sale-announcement headers
# ("Shops on sale" / "Sales announced by email" — sale = rose, like a price
# drop). Everything else (roster, no-sale, codes, …) stays neutral.
_SECTION_KIND = (
    ("⭐ watching now", "watch"),
    ("items on sale", "drop"),
    ("back in stock", "stock"),
    ("newly out of stock", "oos"),
    ("now low stock", "low"),
    ("standing discounts", "flat"),
    ("shops on sale", "drop"),
    ("non-clothing shops on sale", "drop"),
    ("sales announced by email", "drop"),
)

_PILL_BASE = (
    "display:inline-block;font-size:11px;font-weight:800;letter-spacing:.04em;"
    "text-transform:uppercase;padding:2px 7px;border-radius:4px;margin-right:6px;"
    "white-space:nowrap;"
)


class EmailSendError(Exception):
    """Raised when Resend rejects the message or the request fails."""


def _pill(kind: str, label: str) -> str:
    color, bg = _PILL_COLORS[kind]
    return f'<span style="{_PILL_BASE}color:{color};background:{bg};">{label}</span>'


def _card_style(kind: str) -> str:
    edge, bg = _CARD_COLORS[kind]
    return (
        f"list-style:none;border-left:3px solid {edge};background:{bg};"
        f"padding:8px 12px;margin:6px 0;border-radius:0 6px 6px 0;"
    )


def _section_kind(header: str) -> str | None:
    h = header.strip().lower()
    for prefix, kind in _SECTION_KIND:
        if h.startswith(prefix):
            return kind
    return None


def _h2_style(kind: str) -> str:
    color = _PILL_COLORS[kind][0]
    edge = _CARD_COLORS.get(kind, ("#e0d9cf",))[0]
    return f"color:{color};border-bottom-color:{edge};"


def markdown_to_html(md: str) -> str:
    """Tiny markdown-to-HTML converter for the digest format we control.

    Handles headers (##), bold (**x**), links ([text](url)), and bullet lists.
    A change-section bullet that starts with a state marker emoji (from
    digest.py) becomes a colored badge pill + a tinted callout card, and the
    high-signal section headers get a semantic colour.
    """
    lines = md.splitlines()
    html_lines: list[str] = []
    in_list = False

    def _inline(s: str) -> str:
        s = _BOLD_RE.sub(r"<strong>\1</strong>", s)
        s = _LINK_RE.sub(r'<a href="\2">\1</a>', s)
        return s

    for line in lines:
        stripped = line.strip()

        if in_list and not stripped.startswith("- "):
            html_lines.append("</ul>")
            in_list = False

        if not stripped:
            html_lines.append("")
            continue

        if stripped.startswith("### "):
            html_lines.append(f"<h3>{_inline(stripped[4:])}</h3>")
            continue
        if stripped.startswith("## "):
            text = stripped[3:]
            kind = _section_kind(text)
            attr = f' style="{_h2_style(kind)}"' if kind else ""
            html_lines.append(f"<h2{attr}>{_inline(text)}</h2>")
            continue
        if stripped.startswith("# "):
            html_lines.append(f"<h1>{_inline(stripped[2:])}</h1>")
            continue

        if stripped.startswith("- "):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            content = stripped[2:]
            attr = ""
            prefix = ""
            for marker, (kind, label) in _BADGE_BY_MARKER.items():
                if content.startswith(marker):
                    content = content[len(marker):].lstrip()
                    attr = f' style="{_card_style(kind)}"'
                    prefix = _pill(kind, label)
                    break
            html_lines.append(f"  <li{attr}>{prefix}{_inline(content)}</li>")
            continue

        html_lines.append(f"<p>{_inline(stripped)}</p>")

    if in_list:
        html_lines.append("</ul>")

    body = "\n".join(html_lines)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 640px; margin: 1em auto; padding: 0 1em; color: #222; }}
  h1 {{ font-size: 1.4em; }}
  h2 {{ font-size: 1.1em; margin-top: 1.6em; border-bottom: 1px solid #eee; padding-bottom: 0.3em; }}
  h3 {{ font-size: 1em; margin-top: 1.2em; color: #555; }}
  ul {{ padding-left: 1.2em; }}
  li {{ margin: 0.3em 0; }}
  a {{ color: #2563eb; }}
</style></head>
<body>
{body}
</body></html>"""


def send_email(
    api_key: str,
    from_addr: str,
    to_addr: str,
    subject: str,
    body_md: str,
) -> str:
    """POST the digest to Resend. Returns the Resend message id on success.

    Raises EmailSendError on any HTTP error or transport failure.
    """
    if not api_key or not from_addr or not to_addr:
        raise EmailSendError("api_key, from_addr, and to_addr are all required")
    if not body_md.strip():
        raise EmailSendError("body_md is empty")

    payload = json.dumps(
        {
            "from": from_addr,
            "to": [to_addr],
            "subject": subject,
            "html": markdown_to_html(body_md),
            "text": body_md,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise EmailSendError(f"Resend API HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise EmailSendError(f"Resend request failed: {e}") from e

    return result.get("id", "")
