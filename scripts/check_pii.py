#!/usr/bin/env python3
"""Pre-commit PII / secret guard for sale-check.

Blocks a commit when the staged change (or a given file, e.g. the commit
message) would introduce something that must never enter this repo. Two layers:

  * **Live denylist read from ``.env``** — your real Gmail address, the Gist ID,
    the watchlist Google-Doc ID, the Google Voice number, the BodySpec login,
    and every API token / app password. The values are pulled from ``.env`` at
    run time and never written here, so this script itself holds **zero PII**
    and is safe to track / share.
  * **Secret-shaped patterns** — GitHub PAT, Anthropic / Resend key, AWS access
    key, a 16-char Gmail app password — caught by shape even if the value isn't
    in ``.env`` (e.g. someone else's token pasted into a fixture).

Only *added* lines in the staged diff are scanned (introducing the leak is what
matters; pre-existing tracked content was vetted in the 2026-06-18 cleanup).
Findings print as ``path:line  [what matched]`` with the offending value
**redacted**. A non-zero exit blocks the commit; ``git commit --no-verify`` is
the deliberate escape hatch.

Usage:
    python scripts/check_pii.py --staged          # scan the staged diff (pre-commit)
    python scripts/check_pii.py --file MSGFILE     # scan one file (commit-msg)
    python scripts/check_pii.py path1 path2 ...    # scan specific files
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- denylist construction (from .env, never hardcoded) ---------------------

# .env keys whose value is sensitive and should never appear in a commit. Any
# *other* key whose name matches SECRET_NAME_RE is auto-included too, so a new
# secret env var is covered without editing this list.
EXPLICIT_SECRET_KEYS = {
    "GMAIL_USERNAME", "TO_EMAIL", "BODYSPEC_USERNAME", "BODYSPEC_PASSWORD",
    "GIST_ID", "WATCHLIST_URL", "SIGNUP_PHONE", "GITHUB_TOKEN", "GIST_TOKEN",
    "ANTHROPIC_API_KEY", "RESEND_API_KEY", "GMAIL_APP_PASSWORD",
    "FIT_LINK_SECRET", "FIT_FORM_BASE_URL", "SHEET_ID", "WATCHLIST_DOC_ID",
}
SECRET_NAME_RE = re.compile(
    r"(TOKEN|KEY|SECRET|PASSWORD|PASSWD|_PHONE|USERNAME|GIST_ID|DOC_ID"
    r"|SHEET_ID|WATCHLIST_URL|FORM_BASE_URL)",
    re.I,
)
# Values that are deliberately public / placeholder — never treat as PII.
PUBLIC_OR_PLACEHOLDER_RE = re.compile(
    r"(example|YOUR_|your-|xxxx|changeme|placeholder|onboarding@resend\.dev"
    r"|you@|re_xxx|ghp_xxx|sk-ant-xxx)",
    re.I,
)
MIN_DENYLIST_LEN = 6  # ignore short config scalars (M,L,XL / 7 / fri ...)


def _strip(val: str) -> str:
    return val.strip().strip('"').strip("'").replace("\r", "").strip()


def load_env_denylist(env_path: Path) -> list[tuple[str, str]]:
    """Return [(label, needle), ...] of real sensitive values from ``.env``.

    ``label`` names the source (for the report); ``needle`` is the lowercased
    substring to search for. Placeholders/public values are skipped.
    """
    out: list[tuple[str, str]] = []
    if not env_path.exists():
        return out
    seen: set[str] = set()

    def add(label: str, value: str) -> None:
        value = _strip(value)
        if (
            len(value) < MIN_DENYLIST_LEN
            or PUBLIC_OR_PLACEHOLDER_RE.search(value)
            or value.lower() in seen
        ):
            return
        seen.add(value.lower())
        out.append((label, value.lower()))

    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = _strip(value)
        if not value:
            continue
        if not (key in EXPLICIT_SECRET_KEYS or SECRET_NAME_RE.search(key)):
            continue

        add(f"{key} value (.env)", value)

        # WATCHLIST_URL / *_DOC_ID URLs: also flag the bare Google-Doc id.
        m = re.search(r"/d(?:ocument/d)?/([A-Za-z0-9_-]{20,})", value)
        if m:
            add(f"{key} Google-Doc id (.env)", m.group(1))

        # Phone: also flag the bare-digit and 11-digit forms.
        if "PHONE" in key.upper():
            digits = re.sub(r"\D", "", value)
            if len(digits) >= 10:
                add(f"{key} digits (.env)", digits)
                add(f"{key} last-10 (.env)", digits[-10:])

        # Gmail app password is shown space-grouped; also flag the joined form.
        if "APP_PASSWORD" in key.upper() and " " in value:
            add(f"{key} joined (.env)", value.replace(" ", ""))

    return out


# --- secret-shape patterns (catch tokens even when not in .env) -------------

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("GitHub classic PAT", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("GitHub fine-grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("GitHub OAuth/refresh token", re.compile(r"\bgh[ousr]_[A-Za-z0-9]{36}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("Resend API key", re.compile(r"\bre_[A-Za-z0-9]{20,}\b")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    # 16-char Gmail app password rendered as four lowercase quads.
    ("Gmail app password", re.compile(r"\b[a-z]{4} [a-z]{4} [a-z]{4} [a-z]{4}\b")),
]


def redact(value: str) -> str:
    if len(value) <= 6:
        return value[0] + "***"
    return f"{value[:2]}***{value[-2:]}"


def scan_line(text: str, denylist: list[tuple[str, str]]) -> list[str]:
    """Return human-readable hit labels for one line of added content."""
    hits: list[str] = []
    low = text.lower()
    for label, needle in denylist:
        if needle in low:
            hits.append(f"{label}  (matched ~{redact(needle)})")
    for label, pat in SECRET_PATTERNS:
        m = pat.search(text)
        if m and not PUBLIC_OR_PLACEHOLDER_RE.search(m.group(0)):
            hits.append(f"{label}  (matched ~{redact(m.group(0))})")
    return hits


# --- input sources ----------------------------------------------------------

def staged_added_lines() -> list[tuple[str, int, str]]:
    """Yield (path, new_lineno, text) for every added line in the staged diff."""
    # Decode git's output as UTF-8 (its default for diffs) with errors="replace",
    # NOT the locale codec. text=True uses the platform default — cp1252 on
    # Windows, which raises on bytes it has no mapping for (e.g. the smart quote
    # U+201D = 0x9D), crashing the scanner instead of running it. Matches the
    # encoding handling in file_lines / load_env_denylist.
    diff = subprocess.run(
        ["git", "diff", "--cached", "--unified=0", "--no-color", "--diff-filter=ACMR"],
        cwd=REPO_ROOT, capture_output=True, encoding="utf-8", errors="replace",
    ).stdout or ""
    out: list[tuple[str, int, str]] = []
    path = "?"
    lineno = 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            lineno = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((path, lineno, line[1:]))
            lineno += 1
    return out


def file_lines(paths: list[str]) -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for p in paths:
        fp = Path(p)
        if not fp.exists():
            continue
        for i, line in enumerate(
            fp.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            out.append((p, i, line))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="PII / secret pre-commit guard")
    ap.add_argument("--staged", action="store_true", help="scan the staged diff")
    ap.add_argument("--file", action="append", default=[], help="scan a file (repeatable)")
    ap.add_argument("paths", nargs="*", help="explicit files to scan")
    args = ap.parse_args()

    denylist = load_env_denylist(REPO_ROOT / ".env")

    lines: list[tuple[str, int, str]] = []
    if args.staged:
        lines += staged_added_lines()
    if args.file:
        lines += file_lines(args.file)
    if args.paths:
        lines += file_lines(args.paths)
    if not (args.staged or args.file or args.paths):
        lines += staged_added_lines()  # default

    findings: list[str] = []
    for path, lineno, text in lines:
        for hit in scan_line(text, denylist):
            findings.append(f"  {path}:{lineno}  {hit}")

    if findings:
        sys.stderr.write(
            "\n\033[31mcheck_pii: BLOCKED -- possible personal info / secret in this commit:\033[0m\n"
        )
        sys.stderr.write("\n".join(findings) + "\n")
        sys.stderr.write(
            "\nReview the lines above. If a hit is a genuine false positive, "
            "commit with --no-verify.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
