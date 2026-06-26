"""Tests for src/http_util.py — RateLimiter, Retry-After parsing, retry-with-backoff."""

from datetime import datetime, timedelta, timezone


class _FakeResp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class _FakeClient:
    """Hands back a queued sequence of responses for successive .get() calls."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# parse_retry_after
# ---------------------------------------------------------------------------

class TestParseRetryAfter:
    def test_none_and_empty(self):
        from src.http_util import parse_retry_after
        assert parse_retry_after(None) is None
        assert parse_retry_after("") is None
        assert parse_retry_after("   ") is None

    def test_delta_seconds(self):
        from src.http_util import parse_retry_after
        assert parse_retry_after("5") == 5.0
        assert parse_retry_after(" 30 ") == 30.0

    def test_http_date_in_future(self):
        from src.http_util import parse_retry_after
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        future = now + timedelta(seconds=42)
        header = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert abs(parse_retry_after(header, now=now) - 42.0) < 1.0

    def test_http_date_in_past_clamps_to_zero(self):
        from src.http_util import parse_retry_after
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        past = now - timedelta(seconds=10)
        header = past.strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert parse_retry_after(header, now=now) == 0.0

    def test_garbage_is_none(self):
        from src.http_util import parse_retry_after
        assert parse_retry_after("not-a-date") is None


# ---------------------------------------------------------------------------
# get_with_retry
# ---------------------------------------------------------------------------

class TestGetWithRetry:
    def test_returns_immediately_on_200(self):
        from src.http_util import get_with_retry
        client = _FakeClient([_FakeResp(200)])
        sleeps: list[float] = []
        resp = get_with_retry(client, "http://x", sleep=sleeps.append)
        assert resp.status_code == 200
        assert len(client.calls) == 1
        assert sleeps == []

    def test_retries_429_then_succeeds(self):
        from src.http_util import get_with_retry
        client = _FakeClient([
            _FakeResp(429, {"Retry-After": "3"}),
            _FakeResp(200),
        ])
        sleeps: list[float] = []
        resp = get_with_retry(client, "http://x", sleep=sleeps.append)
        assert resp.status_code == 200
        assert len(client.calls) == 2
        assert sleeps == [3.0]  # honored the Retry-After header

    def test_backoff_when_no_header(self):
        from src.http_util import get_with_retry, MAX_BACKOFF
        client = _FakeClient([
            _FakeResp(429), _FakeResp(429), _FakeResp(200),
        ])
        sleeps: list[float] = []
        get_with_retry(client, "http://x", sleep=sleeps.append)
        # exponential: 1, 2 (both under the cap)
        assert sleeps == [1.0, 2.0]
        assert all(s <= MAX_BACKOFF for s in sleeps)

    def test_caps_huge_retry_after(self):
        from src.http_util import get_with_retry, MAX_BACKOFF
        client = _FakeClient([
            _FakeResp(429, {"Retry-After": "9999"}),
            _FakeResp(200),
        ])
        sleeps: list[float] = []
        get_with_retry(client, "http://x", sleep=sleeps.append)
        assert sleeps == [MAX_BACKOFF]

    def test_returns_last_429_after_exhausting_retries(self):
        from src.http_util import get_with_retry
        client = _FakeClient([_FakeResp(429) for _ in range(10)])
        sleeps: list[float] = []
        resp = get_with_retry(client, "http://x", max_retries=3,
                              sleep=sleeps.append)
        assert resp.status_code == 429
        assert len(client.calls) == 4  # 1 initial + 3 retries
        assert len(sleeps) == 3

    def test_503_is_retried(self):
        from src.http_util import get_with_retry
        client = _FakeClient([
            _FakeResp(503, {"Retry-After": "2"}),
            _FakeResp(200),
        ])
        sleeps: list[float] = []
        resp = get_with_retry(client, "http://x", sleep=sleeps.append)
        assert resp.status_code == 200
        assert sleeps == [2.0]

    def test_forwards_get_kwargs(self):
        from src.http_util import get_with_retry
        client = _FakeClient([_FakeResp(200)])
        get_with_retry(client, "http://x", headers={"A": "b"},
                       follow_redirects=True, sleep=lambda s: None)
        url, kwargs = client.calls[0]
        assert kwargs == {"headers": {"A": "b"}, "follow_redirects": True}


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------

class TestRateLimiter:
    def test_first_acquire_does_not_sleep(self, monkeypatch):
        from src import http_util
        sleeps: list[float] = []
        monkeypatch.setattr(http_util.time, "sleep", sleeps.append)
        monkeypatch.setattr(http_util.time, "monotonic", lambda: 1000.0)
        lim = http_util.RateLimiter(5.0)
        lim.acquire()
        assert sleeps == []  # _last=0.0 -> huge elapsed -> no wait

    def test_second_acquire_waits_remaining_interval(self, monkeypatch):
        from src import http_util
        sleeps: list[float] = []
        clock = {"t": 1000.0}
        monkeypatch.setattr(http_util.time, "sleep", sleeps.append)
        monkeypatch.setattr(http_util.time, "monotonic", lambda: clock["t"])
        lim = http_util.RateLimiter(5.0)
        lim.acquire()                 # records _last = 1000.0
        clock["t"] = 1002.0           # only 2 s elapsed
        lim.acquire()
        assert sleeps == [3.0]        # waits the remaining 3 s of the 5 s gap

    def test_no_wait_when_interval_already_elapsed(self, monkeypatch):
        from src import http_util
        sleeps: list[float] = []
        clock = {"t": 1000.0}
        monkeypatch.setattr(http_util.time, "sleep", sleeps.append)
        monkeypatch.setattr(http_util.time, "monotonic", lambda: clock["t"])
        lim = http_util.RateLimiter(5.0)
        lim.acquire()
        clock["t"] = 1010.0           # 10 s elapsed > 5 s interval
        lim.acquire()
        assert sleeps == []
