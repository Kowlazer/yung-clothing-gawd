"""Tests for src/config.py — env loading and validation."""

import pytest

from src.config import Config, ConfigError, load_config

_FULL_ENV = {
    "WATCHLIST_URL": "https://docs.google.com/document/d/abc/edit",
    "RESEND_API_KEY": "re_xxx",
    "FROM_EMAIL": "from@example.com",
    "TO_EMAIL": "to@example.com",
    "GITHUB_TOKEN": "ghp_xxx",
    "GIST_ID": "gist123",
    "ANTHROPIC_API_KEY": "sk-ant-xxx",
    "GMAIL_USERNAME": "user@gmail.com",
    "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop",
}


def test_load_config_happy_path():
    cfg = load_config(_FULL_ENV)
    assert isinstance(cfg, Config)
    assert cfg.watchlist_url == _FULL_ENV["WATCHLIST_URL"]
    assert cfg.anthropic_api_key == _FULL_ENV["ANTHROPIC_API_KEY"]


def test_load_config_missing_one_var():
    env = {k: v for k, v in _FULL_ENV.items() if k != "GIST_ID"}
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "GIST_ID" in str(exc.value)


def test_load_config_missing_multiple_vars():
    env = {k: v for k, v in _FULL_ENV.items()
           if k not in {"FROM_EMAIL", "TO_EMAIL"}}
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    msg = str(exc.value)
    assert "FROM_EMAIL" in msg
    assert "TO_EMAIL" in msg


def test_load_config_blank_value_counts_as_missing():
    env = {**_FULL_ENV, "RESEND_API_KEY": "   "}
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "RESEND_API_KEY" in str(exc.value)


def test_load_config_defaults_to_os_environ(monkeypatch):
    for k, v in _FULL_ENV.items():
        monkeypatch.setenv(k, v)
    cfg = load_config()
    assert cfg.gist_id == _FULL_ENV["GIST_ID"]


def test_load_config_includes_gmail_vars():
    cfg = load_config(_FULL_ENV)
    assert cfg.gmail_username == _FULL_ENV["GMAIL_USERNAME"]
    assert cfg.gmail_app_password == _FULL_ENV["GMAIL_APP_PASSWORD"]


def test_load_config_missing_gmail_app_password():
    env = {k: v for k, v in _FULL_ENV.items() if k != "GMAIL_APP_PASSWORD"}
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert "GMAIL_APP_PASSWORD" in str(exc.value)


# ---------------------------------------------------------------------------
# Newsletter signup config — optional, off by default
# ---------------------------------------------------------------------------

class TestSignupConfig:
    def test_signup_defaults_to_disabled(self):
        """SIGNUP_ENABLED unset means the feature is off."""
        cfg = load_config(_FULL_ENV)
        assert cfg.signup_enabled is False

    def test_signup_phone_optional_when_disabled(self):
        """With the toggle off, SIGNUP_PHONE doesn't need to be set."""
        cfg = load_config(_FULL_ENV)
        assert cfg.signup_phone == ""

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_signup_enabled_truthy_values(self, value):
        cfg = load_config({**_FULL_ENV, "SIGNUP_ENABLED": value})
        assert cfg.signup_enabled is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "  "])
    def test_signup_enabled_falsy_values(self, value):
        cfg = load_config({**_FULL_ENV, "SIGNUP_ENABLED": value})
        assert cfg.signup_enabled is False

    def test_signup_phone_loaded_when_provided(self):
        cfg = load_config({**_FULL_ENV, "SIGNUP_PHONE": "+15555550100"})
        assert cfg.signup_phone == "+15555550100"

    def test_signup_phone_whitespace_stripped(self):
        cfg = load_config({**_FULL_ENV, "SIGNUP_PHONE": "  +15555550100  "})
        assert cfg.signup_phone == "+15555550100"


# ---------------------------------------------------------------------------
# PREFERRED_SIZES — optional, drives size-aware OOS in extract.parse
# ---------------------------------------------------------------------------

class TestPreferredSizes:
    def test_defaults_to_empty_tuple(self):
        cfg = load_config(_FULL_ENV)
        assert cfg.preferred_sizes == ()

    def test_blank_value_is_empty_tuple(self):
        cfg = load_config({**_FULL_ENV, "PREFERRED_SIZES": "   "})
        assert cfg.preferred_sizes == ()

    def test_comma_separated(self):
        cfg = load_config({**_FULL_ENV, "PREFERRED_SIZES": "M,L,XL"})
        assert cfg.preferred_sizes == ("M", "L", "XL")

    def test_uppercased_and_stripped(self):
        cfg = load_config({**_FULL_ENV, "PREFERRED_SIZES": " m , l , xL "})
        assert cfg.preferred_sizes == ("M", "L", "XL")

    def test_dedupes_preserving_order(self):
        cfg = load_config({**_FULL_ENV, "PREFERRED_SIZES": "M,L,m,XL,l"})
        assert cfg.preferred_sizes == ("M", "L", "XL")

    def test_empty_tokens_skipped(self):
        cfg = load_config({**_FULL_ENV, "PREFERRED_SIZES": "M,,L,"})
        assert cfg.preferred_sizes == ("M", "L")

    def test_supports_numeric_sizes(self):
        """Pants waist sizes etc. should pass through unchanged after upper()."""
        cfg = load_config({**_FULL_ENV, "PREFERRED_SIZES": "32,33,M"})
        assert cfg.preferred_sizes == ("32", "33", "M")


# ---------------------------------------------------------------------------
# PREFERRED_SIZES_PANTS — per-category override for bottoms
# ---------------------------------------------------------------------------

class TestPreferredSizesPants:
    def test_defaults_to_empty_tuple(self):
        cfg = load_config(_FULL_ENV)
        assert cfg.preferred_sizes_pants == ()

    def test_blank_value_is_empty_tuple(self):
        cfg = load_config({**_FULL_ENV, "PREFERRED_SIZES_PANTS": "  "})
        assert cfg.preferred_sizes_pants == ()

    def test_parses_like_preferred_sizes(self):
        cfg = load_config({**_FULL_ENV, "PREFERRED_SIZES_PANTS": " s , m , L "})
        assert cfg.preferred_sizes_pants == ("S", "M", "L")

    def test_independent_of_preferred_sizes(self):
        """Tops and bottoms can carry different size shortlists."""
        cfg = load_config({
            **_FULL_ENV,
            "PREFERRED_SIZES": "L,XL",
            "PREFERRED_SIZES_PANTS": "S,M",
        })
        assert cfg.preferred_sizes == ("L", "XL")
        assert cfg.preferred_sizes_pants == ("S", "M")


# ---------------------------------------------------------------------------
# Fit-feedback web form — optional URL/secret + toggles (default on)
# ---------------------------------------------------------------------------

class TestFitFeedbackConfig:
    def test_url_and_secret_default_blank(self):
        cfg = load_config(_FULL_ENV)
        assert cfg.fit_form_base_url == ""
        assert cfg.fit_link_secret == ""

    def test_url_and_secret_loaded_and_stripped(self):
        cfg = load_config({
            **_FULL_ENV,
            "FIT_FORM_BASE_URL": "  https://script.google.com/x/exec  ",
            "FIT_LINK_SECRET": "  s3cret  ",
        })
        assert cfg.fit_form_base_url == "https://script.google.com/x/exec"
        assert cfg.fit_link_secret == "s3cret"

    def test_toggles_default_on(self):
        cfg = load_config(_FULL_ENV)
        assert cfg.fit_feedback_daily is True
        assert cfg.fit_feedback_weekly is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE"])
    def test_toggles_can_be_disabled(self, value):
        cfg = load_config({
            **_FULL_ENV,
            "FIT_FEEDBACK_DAILY": value,
            "FIT_FEEDBACK_WEEKLY": value,
        })
        assert cfg.fit_feedback_daily is False
        assert cfg.fit_feedback_weekly is False

    def test_blank_toggle_keeps_default_on(self):
        cfg = load_config({**_FULL_ENV, "FIT_FEEDBACK_DAILY": "   "})
        assert cfg.fit_feedback_daily is True

    def test_weekly_day_defaults_to_friday(self):
        cfg = load_config(_FULL_ENV)
        assert cfg.fit_feedback_weekly_day == "fri"

    @pytest.mark.parametrize("value,expected", [
        ("mon", "mon"), ("Sunday", "sun"), ("WED", "wed"), ("thursday", "thu"),
    ])
    def test_weekly_day_parsed(self, value, expected):
        cfg = load_config({**_FULL_ENV, "FIT_FEEDBACK_WEEKLY_DAY": value})
        assert cfg.fit_feedback_weekly_day == expected

    @pytest.mark.parametrize("value", ["", "   ", "funday", "xyz"])
    def test_weekly_day_invalid_falls_back_to_friday(self, value):
        cfg = load_config({**_FULL_ENV, "FIT_FEEDBACK_WEEKLY_DAY": value})
        assert cfg.fit_feedback_weekly_day == "fri"


# ---------------------------------------------------------------------------
# Review-request aggregation — optional toggle (on) + recent window (30d)
# ---------------------------------------------------------------------------

class TestReviewRequestsConfig:
    def test_daily_defaults_on(self):
        assert load_config(_FULL_ENV).review_requests_daily is True

    def test_days_defaults_to_30(self):
        assert load_config(_FULL_ENV).review_requests_days == 30

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE"])
    def test_daily_can_be_disabled(self, value):
        cfg = load_config({**_FULL_ENV, "REVIEW_REQUESTS_DAILY": value})
        assert cfg.review_requests_daily is False

    def test_blank_daily_keeps_default_on(self):
        cfg = load_config({**_FULL_ENV, "REVIEW_REQUESTS_DAILY": "   "})
        assert cfg.review_requests_daily is True

    def test_days_override(self):
        cfg = load_config({**_FULL_ENV, "REVIEW_REQUESTS_DAYS": "14"})
        assert cfg.review_requests_days == 14

    @pytest.mark.parametrize("value", ["", "abc", "-5", "0"])
    def test_days_invalid_falls_back_to_30(self, value):
        cfg = load_config({**_FULL_ENV, "REVIEW_REQUESTS_DAYS": value})
        assert cfg.review_requests_days == 30


class TestPriceHistoryConfig:
    def test_defaults(self):
        cfg = load_config(_FULL_ENV)
        assert cfg.price_history_retention_days == 365
        assert cfg.price_baseline_days == 90
        assert cfg.price_history_min_days == 7
        assert cfg.price_drop_margin_pct == 2.0

    def test_overrides(self):
        cfg = load_config({
            **_FULL_ENV,
            "PRICE_HISTORY_RETENTION_DAYS": "180",
            "PRICE_BASELINE_DAYS": "60",
            "PRICE_HISTORY_MIN_DAYS": "14",
            "PRICE_DROP_MARGIN_PCT": "5",
        })
        assert cfg.price_history_retention_days == 180
        assert cfg.price_baseline_days == 60
        assert cfg.price_history_min_days == 14
        assert cfg.price_drop_margin_pct == 5.0

    @pytest.mark.parametrize("value", ["", "abc", "-1", "0"])
    def test_baseline_days_invalid_falls_back(self, value):
        cfg = load_config({**_FULL_ENV, "PRICE_BASELINE_DAYS": value})
        assert cfg.price_baseline_days == 90

    @pytest.mark.parametrize("value", ["", "abc", "-2"])
    def test_margin_invalid_falls_back(self, value):
        cfg = load_config({**_FULL_ENV, "PRICE_DROP_MARGIN_PCT": value})
        assert cfg.price_drop_margin_pct == 2.0

    def test_margin_zero_is_allowed(self):
        """A 0% margin is a legitimate choice (any sub-baseline price is a drop)."""
        cfg = load_config({**_FULL_ENV, "PRICE_DROP_MARGIN_PCT": "0"})
        assert cfg.price_drop_margin_pct == 0.0


class TestExcludedShops:
    def test_default_empty(self):
        cfg = load_config(_FULL_ENV)
        assert cfg.excluded_shops == ()

    def test_parses_csv_lowercased(self):
        cfg = load_config({**_FULL_ENV, "EXCLUDED_SHOPS": "Nocturne Goods, ACME"})
        assert cfg.excluded_shops == ("nocturne goods", "acme")

    def test_dedupes_and_strips(self):
        cfg = load_config({**_FULL_ENV, "EXCLUDED_SHOPS": " Nocturne Goods , nocturne goods ,, "})
        assert cfg.excluded_shops == ("nocturne goods",)


class TestSmsSaleShops:
    def test_default_empty(self):
        cfg = load_config(_FULL_ENV)
        assert cfg.sms_sale_shops == ()

    def test_parses_csv_preserving_display_case(self):
        # Case preserved (these become shop names shown in the digest), unlike
        # EXCLUDED_SHOPS which lowercases for substring matching.
        cfg = load_config({**_FULL_ENV, "SMS_SALE_SHOPS": "Harborlight, Greyfox"})
        assert cfg.sms_sale_shops == ("Harborlight", "Greyfox")

    def test_dedupes_case_insensitively_and_strips(self):
        cfg = load_config({**_FULL_ENV, "SMS_SALE_SHOPS": " Harborlight , harborlight ,, Junewave "})
        assert cfg.sms_sale_shops == ("Harborlight", "Junewave")


class TestRestockConfig:
    def test_defaults(self):
        cfg = load_config(_FULL_ENV)
        assert cfg.restock_signup_enabled is False
        assert cfg.restock_emails_daily is True
        assert cfg.restock_email_days == 7

    def test_signup_enabled_truthy(self):
        cfg = load_config({**_FULL_ENV, "RESTOCK_SIGNUP_ENABLED": "1"})
        assert cfg.restock_signup_enabled is True

    def test_emails_daily_can_be_disabled(self):
        cfg = load_config({**_FULL_ENV, "RESTOCK_EMAILS_DAILY": "no"})
        assert cfg.restock_emails_daily is False

    @pytest.mark.parametrize("value,expected", [("14", 14), ("", 7), ("abc", 7), ("0", 7)])
    def test_email_days_parsing(self, value, expected):
        cfg = load_config({**_FULL_ENV, "RESTOCK_EMAIL_DAYS": value})
        assert cfg.restock_email_days == expected
