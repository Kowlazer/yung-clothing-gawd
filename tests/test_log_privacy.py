"""Tests for src.log_privacy — Actions-log redaction of watchlist content.

The threat model: the repo is public, so workflow logs are readable by any
logged-in GitHub user. Nothing shop-item-identifying (product URL paths,
promo-code values) may survive the filter; shop *domains* deliberately do
(that's what keeps remote 429/bot-block debugging possible). With the flag
unset the module must be inert so local full-URL logging is untouched.
"""

import io
import logging

import pytest

from src import log_privacy
from src.log_privacy import RedactFilter, redact_urls, redact_value


@pytest.fixture
def redact_on(monkeypatch):
    monkeypatch.setenv("SALE_CHECK_REDACT_LOGS", "1")


@pytest.fixture
def redact_off(monkeypatch):
    monkeypatch.delenv("SALE_CHECK_REDACT_LOGS", raising=False)


def _fresh_logger(name: str):
    """An isolated logger + StringIO handler carrying the RedactFilter,
    mimicking the production root-handler setup."""
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactFilter())
    logger.addHandler(handler)
    return logger, stream


# ---------------------------------------------------------------------------
# redact_urls
# ---------------------------------------------------------------------------

def test_product_url_reduced_to_domain():
    text = ("recovered blocked product via proxied HTML: "
            "https://shop.example.com/products/secret-tee-black?variant=123")
    out = redact_urls(text)
    assert "https://shop.example.com/…" in out
    assert "secret-tee" not in out
    assert "variant" not in out


def test_bare_homepage_keeps_no_ellipsis():
    assert redact_urls("fetch https://shop.example.com -> 403") == \
        "fetch https://shop.example.com -> 403"
    assert redact_urls("fetch https://shop.example.com/ -> 403") == \
        "fetch https://shop.example.com -> 403"


def test_credentials_and_port_stripped():
    out = redact_urls("push https://x-access-token:ghp_secret@github.com:443/o/r.git")
    assert "ghp_secret" not in out
    assert "x-access-token" not in out
    assert "github.com" in out


def test_multiple_urls_and_surrounding_prose_survive():
    out = redact_urls(
        "a https://a.example/p/1?q=2 b (https://b.example/x) c 'https://c.example/'"
    )
    assert out == "a https://a.example/… b (https://b.example/…) c 'https://c.example'"


def test_text_without_urls_unchanged():
    text = "gmail: 3 attributed codes, 1 unattributed, 2 sale signals"
    assert redact_urls(text) == text


def test_httpx_request_line_shape():
    out = redact_urls(
        'HTTP Request: GET https://shop.example.com/products/rare-hoodie.json '
        '"HTTP/1.1 200 OK"'
    )
    assert out == 'HTTP Request: GET https://shop.example.com/… "HTTP/1.1 200 OK"'


# ---------------------------------------------------------------------------
# RedactFilter on real log records
# ---------------------------------------------------------------------------

def test_filter_redacts_interpolated_args():
    logger, stream = _fresh_logger("t_lp_args")
    logger.info("http_util: %s -> %s, backing off %.1fs",
                "https://shop.example.com/products/secret?variant=9", 429, 5.0)
    out = stream.getvalue()
    assert "https://shop.example.com/…" in out
    assert "secret" not in out
    assert "429" in out


def test_filter_redacts_exception_traceback():
    logger, stream = _fresh_logger("t_lp_exc")
    try:
        raise RuntimeError(
            "Server error '503' for url 'https://shop.example.com/products/secret-cap'"
        )
    except RuntimeError:
        logger.exception("extract step failed")
    out = stream.getvalue()
    assert "extract step failed" in out
    assert "Traceback" in out
    assert "RuntimeError" in out
    assert "secret-cap" not in out
    assert "https://shop.example.com/…" in out


def test_filter_passes_malformed_records_through():
    # Mismatched %-args raise inside getMessage(); the filter must neither
    # blow up nor drop the record — it leaves it for logging's own error path.
    filt = RedactFilter()
    record = logging.LogRecord(
        "t_lp_bad", logging.INFO, __file__, 1, "bad %d %d", ("x",), None,
    )
    assert filt.filter(record) is True
    assert record.msg == "bad %d %d"
    assert record.args == ("x",)


# ---------------------------------------------------------------------------
# install() gate + redact_value
# ---------------------------------------------------------------------------

def test_install_noop_when_disabled(redact_off):
    assert log_privacy.install() is False


def test_install_attaches_filter_to_root_handlers(redact_on):
    root = logging.getLogger()
    handler = logging.StreamHandler(io.StringIO())
    root.addHandler(handler)
    try:
        assert log_privacy.install() is True
        assert any(isinstance(f, RedactFilter) for f in handler.filters)
    finally:
        root.removeHandler(handler)


def test_redact_value_masks_only_when_enabled(redact_on):
    assert redact_value("SAVE20") == "***"


def test_redact_value_passthrough_when_disabled(redact_off):
    assert redact_value("SAVE20") == "SAVE20"
