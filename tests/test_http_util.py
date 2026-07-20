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
# HostCircuitBreaker (per-host 429 short-circuit)
# ---------------------------------------------------------------------------

class TestHostCircuitBreaker:
    def test_short_circuits_after_threshold(self):
        from src.http_util import get_with_retry, HostCircuitBreaker
        breaker = HostCircuitBreaker(threshold=2)
        # Two items on the same host, each persistently 429 -> trips the breaker.
        for _ in range(2):
            client = _FakeClient([_FakeResp(429) for _ in range(5)])
            resp = get_with_retry(client, "http://samehost/item", max_retries=2,
                                  sleep=lambda s: None, breaker=breaker)
            assert resp.status_code == 429
        assert breaker.is_tripped("samehost")

        # Third item: the host is tripped, so no network call and no backoff —
        # a synthetic 429 comes straight back even though the server would 200.
        client = _FakeClient([_FakeResp(200)])
        sleeps: list[float] = []
        resp = get_with_retry(client, "http://samehost/item3", max_retries=2,
                              sleep=sleeps.append, breaker=breaker)
        assert resp.status_code == 429
        assert client.calls == []   # never hit the network
        assert sleeps == []         # never backed off

    def test_is_per_host(self):
        from src.http_util import get_with_retry, HostCircuitBreaker
        breaker = HostCircuitBreaker(threshold=2)
        # One persistent-429 item on each of two hosts -> neither reaches 2.
        for host in ("a.com", "b.com"):
            client = _FakeClient([_FakeResp(429) for _ in range(5)])
            get_with_retry(client, f"http://{host}/x", max_retries=2,
                           sleep=lambda s: None, breaker=breaker)
        assert not breaker.is_tripped("a.com")
        assert not breaker.is_tripped("b.com")
        # A real fetch to a.com still happens (not short-circuited).
        client = _FakeClient([_FakeResp(200)])
        resp = get_with_retry(client, "http://a.com/y", breaker=breaker,
                              sleep=lambda s: None)
        assert resp.status_code == 200
        assert len(client.calls) == 1

    def test_success_does_not_trip(self):
        from src.http_util import get_with_retry, HostCircuitBreaker
        breaker = HostCircuitBreaker(threshold=2)
        for _ in range(5):
            client = _FakeClient([_FakeResp(200)])
            get_with_retry(client, "http://ok.com/x", breaker=breaker,
                           sleep=lambda s: None)
        assert not breaker.is_tripped("ok.com")

    def test_recovered_429_does_not_trip(self):
        # A 429 that recovers to 200 on retry is NOT a persistent throttle, so
        # even a threshold of 1 must not trip on it.
        from src.http_util import get_with_retry, HostCircuitBreaker
        breaker = HostCircuitBreaker(threshold=1)
        for _ in range(3):
            client = _FakeClient([_FakeResp(429, {"Retry-After": "0"}), _FakeResp(200)])
            resp = get_with_retry(client, "http://x.com/i", breaker=breaker,
                                  sleep=lambda s: None)
            assert resp.status_code == 200
        assert not breaker.is_tripped("x.com")

    def test_breaker_none_disables_short_circuit(self):
        # With breaker=None the pre-breaker behaviour is preserved: every call
        # hits the network and no host state is tracked.
        from src.http_util import get_with_retry
        for _ in range(4):
            client = _FakeClient([_FakeResp(429) for _ in range(5)])
            resp = get_with_retry(client, "http://h/i", max_retries=2,
                                  sleep=lambda s: None, breaker=None)
            assert resp.status_code == 429
            assert len(client.calls) == 3  # always 1 initial + 2 retries

    def test_record_throttled_reports_trip_once(self):
        from src.http_util import HostCircuitBreaker
        breaker = HostCircuitBreaker(threshold=2)
        assert breaker.record_throttled("h") is False   # count 1
        assert breaker.record_throttled("h") is True     # count 2 -> trips now
        assert breaker.record_throttled("h") is False    # already tripped
        assert breaker.is_tripped("h")

    def test_reset_clears_state(self):
        from src.http_util import HostCircuitBreaker
        breaker = HostCircuitBreaker(threshold=1)
        breaker.record_throttled("h")
        assert breaker.is_tripped("h")
        breaker.reset()
        assert not breaker.is_tripped("h")


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


# ---------------------------------------------------------------------------
# AdaptiveRateLimiter (AIMD platform gate)
# ---------------------------------------------------------------------------

def _adaptive(**kw):
    from src import http_util
    defaults = dict(start=1.0, min_interval=1.0, max_interval=5.0,
                    growth=2.0, decay_step=0.5, decay_after=3)
    defaults.update(kw)
    start = defaults.pop("start")
    return http_util.AdaptiveRateLimiter(start, **defaults)


class TestAdaptiveRateLimiter:
    def test_starts_narrow(self):
        assert _adaptive().interval == 1.0

    def test_throttle_widens_multiplicatively(self):
        lim = _adaptive()
        lim.record_throttled("shop.com")
        assert lim.interval == 2.0
        lim.record_throttled("shop.com")
        assert lim.interval == 4.0

    def test_widening_is_capped_at_the_old_fixed_gap(self):
        # The whole safety argument: the worst this can degrade to is the flat
        # 5 s gate it replaced — never slower.
        lim = _adaptive()
        for _ in range(10):
            lim.record_throttled("shop.com")
        assert lim.interval == 5.0

    def test_clean_stretch_decays_one_step(self, monkeypatch):
        from src import http_util
        monkeypatch.setattr(http_util.time, "sleep", lambda s: None)
        lim = _adaptive()                 # decay_after=3
        lim.record_throttled("shop.com")  # -> 2.0
        for _ in range(3):
            lim.acquire()
        assert lim.interval == 1.5

    def test_decay_stops_at_the_floor(self, monkeypatch):
        from src import http_util
        monkeypatch.setattr(http_util.time, "sleep", lambda s: None)
        lim = _adaptive()
        for _ in range(30):               # far more clean requests than needed
            lim.acquire()
        assert lim.interval == 1.0

    def test_throttle_resets_the_clean_streak(self, monkeypatch):
        # Two clean acquisitions then a throttle must not leave the counter
        # primed to decay immediately after — AIMD decays slowly on purpose.
        from src import http_util
        monkeypatch.setattr(http_util.time, "sleep", lambda s: None)
        lim = _adaptive()
        lim.record_throttled("a.com")     # -> 2.0
        lim.acquire()
        lim.acquire()
        lim.record_throttled("a.com")     # -> 4.0, streak cleared
        lim.acquire()
        assert lim.interval == 4.0        # only 1 clean since; decay_after is 3

    def test_zeroed_interval_short_circuits(self, monkeypatch):
        # The test/no-delay configuration (conftest zeroes the gate): acquire()
        # must not sleep, and must not run the feedback loop.
        from src import http_util
        sleeps: list[float] = []
        monkeypatch.setattr(http_util.time, "sleep", sleeps.append)
        lim = _adaptive()
        lim._interval = 0.0
        for _ in range(10):
            lim.acquire()
        assert sleeps == []
        assert lim.interval == 0.0

    def test_persistent_throttle_widens_the_global_gate(self, monkeypatch):
        # The wiring: get_with_retry feeds the gate the same persistent-throttle
        # signal the circuit breaker consumes.
        from src import http_util
        from src.http_util import get_with_retry, HostCircuitBreaker
        monkeypatch.setattr(http_util, "PLATFORM_LIMITER", _adaptive())
        client = _FakeClient([_FakeResp(429) for _ in range(5)])
        get_with_retry(client, "http://slow.com/i", max_retries=2,
                       sleep=lambda s: None, breaker=HostCircuitBreaker(threshold=99))
        assert http_util.PLATFORM_LIMITER.interval == 2.0

    def test_recovered_429_leaves_the_global_gate_alone(self, monkeypatch):
        # A blip that recovers on retry is not a storm — same standard the
        # circuit breaker holds.
        from src import http_util
        from src.http_util import get_with_retry
        monkeypatch.setattr(http_util, "PLATFORM_LIMITER", _adaptive())
        client = _FakeClient([_FakeResp(429, {"Retry-After": "0"}), _FakeResp(200)])
        resp = get_with_retry(client, "http://ok.com/i", max_retries=2,
                              sleep=lambda s: None, breaker=None)
        assert resp.status_code == 200
        assert http_util.PLATFORM_LIMITER.interval == 1.0

    def test_gate_widens_even_with_the_breaker_disabled(self, monkeypatch):
        # breaker=None turns off per-host short-circuiting only; the platform
        # gate is a separate consumer of the same signal.
        from src import http_util
        from src.http_util import get_with_retry
        monkeypatch.setattr(http_util, "PLATFORM_LIMITER", _adaptive())
        client = _FakeClient([_FakeResp(429) for _ in range(5)])
        get_with_retry(client, "http://slow.com/i", max_retries=2,
                       sleep=lambda s: None, breaker=None)
        assert http_util.PLATFORM_LIMITER.interval == 2.0

    def test_persistent_503_does_not_widen_the_global_gate(self, monkeypatch):
        # A permanently-broken shop 503s on every item of every run. Harmless to
        # a per-host breaker; fatal to a global gate, which it would pin at the
        # ceiling forever. Only 429 means "you are being rate limited".
        # (Observed live: blackrabbitco on the 2026-07-19 verification run.)
        from src import http_util
        from src.http_util import get_with_retry, HostCircuitBreaker
        monkeypatch.setattr(http_util, "PLATFORM_LIMITER", _adaptive())
        breaker = HostCircuitBreaker(threshold=2)
        for _ in range(4):
            client = _FakeClient([_FakeResp(503) for _ in range(5)])
            get_with_retry(client, "http://dead.com/i", max_retries=2,
                           sleep=lambda s: None, breaker=breaker)
        assert http_util.PLATFORM_LIMITER.interval == 1.0   # untouched
        assert breaker.is_tripped("dead.com")               # breaker still acts
