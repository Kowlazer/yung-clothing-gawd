"""Tests for scripts/check_pii.py (the pre-commit PII / secret guard).

``scripts/`` isn't an importable package, so the module is loaded by path. The
headline case is the Windows decode regression: a staged diff containing a byte
undefined in cp1252 (e.g. the smart quote U+201D = 0x9D) must NOT crash the
scanner — git output is decoded as UTF-8 with errors="replace", not the locale
codec.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_pii.py"
_spec = importlib.util.spec_from_file_location("check_pii", _PATH)
check_pii = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_pii)


# --------------------------------------------------------------------------
# staged_added_lines — UTF-8 decode regression
# --------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_staged_added_lines_handles_non_cp1252_bytes(tmp_path, monkeypatch):
    # A staged line carrying U+201D (”) — byte 0x9D, undefined in cp1252 — used
    # to crash the scanner on Windows (text=True → locale strict decode). It must
    # now decode cleanly and the added line is returned intact.
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "snippet.js").write_text('emptyHtml(`No brands match “X”.`)\n',
                                     encoding="utf-8")
    _git(repo, "add", "snippet.js")

    monkeypatch.setattr(check_pii, "REPO_ROOT", repo)
    added = check_pii.staged_added_lines()

    texts = [text for _, _, text in added]
    assert any("No brands match" in t for t in texts)
    # Decoded as UTF-8 (the smart quote survives), not mangled / not crashed.
    assert any("”" in t for t in texts)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_staged_added_lines_empty_when_nothing_staged(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    monkeypatch.setattr(check_pii, "REPO_ROOT", repo)
    assert check_pii.staged_added_lines() == []


# --------------------------------------------------------------------------
# scan_line — secret-shape + denylist matching (pure, no git)
# --------------------------------------------------------------------------

def test_scan_line_flags_secret_shaped_token_redacted():
    hits = check_pii.scan_line("token = ghp_" + "a" * 36, [])
    assert len(hits) == 1
    assert "GitHub classic PAT" in hits[0]
    # The matched value is redacted, never echoed in full.
    assert "a" * 36 not in hits[0]


def test_scan_line_ignores_placeholder_token():
    # Placeholder/public values are explicitly exempt.
    assert check_pii.scan_line("ANTHROPIC_API_KEY=sk-ant-xxx", []) == []


def test_scan_line_matches_env_denylist_value():
    deny = [("GIST_ID value (.env)", "deadbeefcafe1234")]
    hits = check_pii.scan_line("url = gist/DeadBeefCafe1234/raw", deny)
    assert hits and "GIST_ID value (.env)" in hits[0]


def test_scan_line_clean_line_has_no_hits():
    assert check_pii.scan_line("const brandFilter = '';", []) == []


def test_redact_short_and_long():
    assert check_pii.redact("abcdef") == "a***"
    assert check_pii.redact("abcdefghij").startswith("ab***")
    assert check_pii.redact("abcdefghij").endswith("***ij")
