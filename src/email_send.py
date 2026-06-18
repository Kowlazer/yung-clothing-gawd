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


class EmailSendError(Exception):
    """Raised when Resend rejects the message or the request fails."""


def markdown_to_html(md: str) -> str:
    """Tiny markdown-to-HTML converter for the digest format we control.

    Handles headers (##), bold (**x**), links ([text](url)), and bullet lists.
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
            html_lines.append(f"<h2>{_inline(stripped[3:])}</h2>")
            continue
        if stripped.startswith("# "):
            html_lines.append(f"<h1>{_inline(stripped[2:])}</h1>")
            continue

        if stripped.startswith("- "):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            html_lines.append(f"  <li>{_inline(stripped[2:])}</li>")
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
