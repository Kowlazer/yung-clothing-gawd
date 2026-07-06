"""Redact watchlist content from logs on public GitHub Actions runs.

The repo is public, and Actions logs are readable by any logged-in GitHub
user for ~90 days — anything the crons print is effectively published. The
run log carries exactly the leak classes the privacy guardrails forbid in
repo content: httpx>=0.25 logs EVERY request at INFO ("HTTP Request: GET
<url> ..."), so the whole watchlist enumerates itself daily; the
extraction/recovery/backoff lines log full product URLs; and the code-prune
line logs promo-code values. Locally, those full URLs at INFO are exactly
what you want for debugging.

Resolution: the workflows set SALE_CHECK_REDACT_LOGS=1 and the two Actions
entry points (src.main, src.order_scan) call install() right after
logging.basicConfig(). When the flag is set, a filter attached to the root
handlers rewrites every http(s) URL in every record — message, args, and
exception traceback — down to scheme + host ("https://shop.example/…"), so
the public log still shows WHICH domain 429'd or recovered but never the
product path/query that would reconstruct the watchlist. Values that are
sensitive in their entirety (promo codes) go through redact_value() at the
call site. With the flag unset (every local run) this module is inert.

The filter must sit on the *handlers*, not on our loggers: httpx's records
are born on the "httpx" logger, and logger-level filters don't apply to
records propagated from child loggers.
"""

from __future__ import annotations

import logging
import os
import re
import traceback
from urllib.parse import urlsplit

# Stops before whitespace/quotes/brackets so surrounding prose (tracebacks,
# %r reprs, "fetch %s -> 403" phrasing) survives the rewrite readably.
_URL_RE = re.compile(r"https?://[^\s'\"<>()\[\]]+")


def enabled() -> bool:
    return os.environ.get("SALE_CHECK_REDACT_LOGS", "").lower() in ("1", "true", "yes")


def redact_value(value):
    """For values that are sensitive in full (promo codes): *** when redacting."""
    return "***" if enabled() else value


def _domain_only(match: re.Match) -> str:
    url = match.group(0)
    try:
        parts = urlsplit(url)
        host = parts.hostname  # drops userinfo credentials and the port
    except ValueError:
        return "https://***"
    if not host:
        return "https://***"
    had_more = parts.path not in ("", "/") or parts.query or parts.fragment
    return f"{parts.scheme}://{host}" + ("/…" if had_more else "")


def redact_urls(text: str) -> str:
    return _URL_RE.sub(_domain_only, text)


class RedactFilter(logging.Filter):
    """Rewrite URLs in the interpolated message and the exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed %-args: pass the record through as-is
            return True
        record.msg = redact_urls(message)
        record.args = None
        # Pre-format the traceback ourselves so the Formatter never sees the
        # raw exc_info (httpx errors embed the full request URL in their str).
        if record.exc_info and not record.exc_text:
            record.exc_text = "".join(
                traceback.format_exception(*record.exc_info)
            ).rstrip("\n")
        record.exc_info = None
        if record.exc_text:
            record.exc_text = redact_urls(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_urls(record.stack_info)
        return True


def install() -> bool:
    """Attach the redaction filter to every root handler.

    Call immediately after logging.basicConfig() in any entry point that can
    run on GitHub Actions. No-op (returns False) when the flag is unset.
    """
    if not enabled():
        return False
    filt = RedactFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(filt)
    logging.getLogger(__name__).info(
        "log redaction enabled — URLs reduced to scheme://host"
    )
    return True
