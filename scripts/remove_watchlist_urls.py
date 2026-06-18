"""Generate a paste-ready replacement for the 'Shops and URLs' section of the
watchlist Google Doc, with a given list of URLs removed.

The Google Doc itself is NOT edited programmatically (would need Docs API OAuth,
deferred indefinitely — see CLAUDE.md). This script produces text the user
pastes into the Doc manually.

Usage:
    python scripts/remove_watchlist_urls.py URL [URL ...]
    python scripts/remove_watchlist_urls.py --urls-file removals.txt
    cat removals.txt | python scripts/remove_watchlist_urls.py -

Pipeline:
    1. Fetch the Doc as plain text via `/export?format=txt` (same endpoint
       `watchlist.py` uses for the daily cron).
    2. Normalize line endings + strip BOM.
    3. Remove each target URL (one per paragraph in the Doc).
    4. Collapse Google's blank-line paragraph separators -- the export emits
       a blank line after every paragraph, so what Docs renders as adjacent
       lines arrives here as `content\\n\\ncontent`. Collapse all multi-newline
       runs to single newlines for uniform paste-back.
    5. Slice to the 'Shops and URLs:' section onward.
    6. Insert a blank line after the section title and before every shop
       header (a header is a non-URL line ending in ':' or ': (parenthetical)';
       coupon/code lines like 'Discount code: ZWKZ62WS' stay inline).
    7. Write to `_shops_and_urls_section.txt`. User pastes that over the
       existing 'Shops and URLs:' section in the Doc.

Why not the Drive MCP read_file_content? It returns a markdown rendering, not
true plain text (wraps URLs in <...>, escapes `!` `-` `[` `]`, inserts blank
lines between paragraphs that are actually adjacent). The /export?format=txt
endpoint is closer to the underlying paragraph structure.

Why fully tight paragraphs (no blank between consecutive URLs)? Google's text
export emits inconsistent multi-newline runs even for paragraphs that visually
sit adjacent in Docs (default paragraph spacing collapses the visual gap). To
match the rendered look, collapse everything to single newlines and let Docs
apply uniform paragraph spacing on paste.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.request
from pathlib import Path

# Doc ID lives in WATCHLIST_URL in .env. Fall back to parsing it ourselves if
# config import isn't available (script can be run standalone).
_DOC_ID_RE = re.compile(r"/document/d/([^/?#]+)")
_HEADER_RE = re.compile(r"^[^:]+:\s*(\([^)]*\))?\s*$")

OUTPUT_FILE = Path("_shops_and_urls_section.txt")


def _doc_id_from_env() -> str:
    url = os.environ.get("WATCHLIST_URL")
    if not url:
        # try .env in CWD
        env_path = Path(".env")
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("WATCHLIST_URL="):
                    url = line.split("=", 1)[1].strip()
                    break
    if not url:
        raise SystemExit("WATCHLIST_URL not set in env or .env")
    m = _DOC_ID_RE.search(url)
    if not m:
        raise SystemExit(f"Could not extract doc ID from WATCHLIST_URL={url!r}")
    return m.group(1)


def _fetch_doc(doc_id: str) -> str:
    export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    with urllib.request.urlopen(export_url) as resp:
        return resp.read().decode("utf-8")


def _clean(text: str, targets: list[str]) -> str:
    # Strip BOM, normalize line endings
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")

    # Remove target URLs. Each lives on its own paragraph in the Doc.
    for url in targets:
        text = text.replace(f"\n{url}\n", "\n")
        text = text.replace(f"{url}\n", "")
        text = text.replace(url, "")

    # Collapse all multi-newline runs to a single newline. Google's text export
    # inserts a blank-line separator after every paragraph; collapsing makes
    # paste-back tight (uniform paragraph spacing applied by Docs renderer).
    text = re.sub(r"\n{2,}", "\n", text)

    # Trim trailing whitespace per line
    return "\n".join(line.rstrip() for line in text.split("\n"))


def _extract_shops_section(text: str) -> list[str]:
    """Slice from 'Shops and URLs:' to the end, with blank line after the title
    and before each shop header (any non-URL line ending in ':' or ': (...)').
    """
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "Shops and URLs:")
    except StopIteration:
        raise SystemExit("Could not find 'Shops and URLs:' header in doc")

    section = lines[start:]
    out: list[str] = []
    for i, line in enumerate(section):
        if i == 0:
            out.append(line)
            out.append("")
            continue
        if line.startswith(("http", "www.")) or not line.strip():
            out.append(line)
            continue
        is_header = bool(_HEADER_RE.match(line))
        if is_header and out and out[-1] != "":
            out.append("")
        out.append(line)
    return out


def _parse_targets(args: argparse.Namespace) -> list[str]:
    targets: list[str] = []
    if args.urls_file:
        targets.extend(
            line.strip()
            for line in Path(args.urls_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    if args.urls == ["-"]:
        targets.extend(
            line.strip()
            for line in sys.stdin
            if line.strip() and not line.strip().startswith("#")
        )
    else:
        targets.extend(args.urls)
    # De-dupe while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for u in targets:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("urls", nargs="*", help="URLs to remove (or '-' to read from stdin)")
    parser.add_argument("--urls-file", help="File containing URLs (one per line)")
    args = parser.parse_args()

    targets = _parse_targets(args)
    if not targets:
        parser.error("no URLs provided")

    doc_id = _doc_id_from_env()
    print(f"Fetching doc {doc_id} ...", file=sys.stderr)
    raw = _fetch_doc(doc_id)
    print(f"  got {len(raw)} chars", file=sys.stderr)

    cleaned = _clean(raw, targets)
    section_lines = _extract_shops_section(cleaned)

    OUTPUT_FILE.write_text("\n".join(section_lines), encoding="utf-8")
    print(f"Wrote {OUTPUT_FILE} ({len(section_lines)} lines)", file=sys.stderr)

    # Verify no targets remain in output
    section_text = "\n".join(section_lines)
    remaining = [u for u in targets if u in section_text]
    if remaining:
        print(f"WARNING: {len(remaining)} target(s) still present in output:", file=sys.stderr)
        for u in remaining:
            print(f"  - {u}", file=sys.stderr)
        return 1

    print(f"All {len(targets)} target URL(s) removed.", file=sys.stderr)
    print(
        f"Paste recipe: open {OUTPUT_FILE}, Ctrl+A copy. In the Doc, select from "
        "'Shops and URLs:' to end-of-doc, then paste.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
