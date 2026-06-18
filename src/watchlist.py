"""Fetch the Google Doc watchlist and return raw text."""
from __future__ import annotations

import re

import httpx

_DOC_ID_RE = re.compile(r'/document/d/([^/?#]+)')
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; sale-check/1.0)"}


def fetch_watchlist(url: str) -> str:
    doc_id = _DOC_ID_RE.search(url)
    if not doc_id:
        raise ValueError(f"Could not extract Google Doc ID from URL: {url}")
    export_url = f"https://docs.google.com/document/d/{doc_id.group(1)}/export?format=txt"
    resp = httpx.get(export_url, headers=_HEADERS, follow_redirects=True, timeout=30)
    resp.raise_for_status()
    return resp.text
