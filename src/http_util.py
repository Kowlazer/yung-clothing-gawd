"""Shared HTTP throttling + retry helpers.

A thread-safe ``RateLimiter`` (minimum inter-request gap across all threads) and
a ``Retry-After``-aware retry wrapper, used by both the product-price extractor
(``src/extract.py``) and the homepage sale-check fetcher (``src/claude_fuzzy.py``)
so neither hammers shared platform infrastructure — Shopify rate-limits by source
IP across every store it hosts, so concurrent / bursty requests from one runner
trip a platform-level 429 even though each store is a different domain.

``AdaptiveRateLimiter`` is the same gate that learns *across runs*: it starts each
run proactively at the safe ceiling (or a small speedup earned by a streak of
clean runs), snaps back to the ceiling the instant a host persistently throttles
us, and shaves the gap down only after a fully clean run — so we neither expose
the opening burst that trips a per-IP throttle nor pay storm-day pacing on the
~360 calm days a year. The learned gap is persisted in the Gist by ``src/main.py``.
"""

from __future__ import annotations

import email.utils
import logging
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

# Retry policy for rate-limit / transient-unavailable responses. We honor the
# server's Retry-After header when present (adaptive — no guessing at the
# threshold), falling back to exponential backoff otherwise.
#
# Tuned down 3->2 retries / 20s->15s cap after 2026-07-15, when a wide 429 storm
# (dozens of shops rate-limiting at once) made the per-shop backoff cost — up to
# 3 x 20s = 60s each, serialised per-domain — blow the workflow's 45-min cap, so
# the run was cancelled before it could send the email. The worst case is now
# 2 x 15s = 30s/shop. Trade-off: on a heavy-storm day a few shops may end up
# `rate_limited` (stale price, recovered next run) rather than getting a third
# attempt — an acceptable price for the run actually finishing and the digest
# going out. Retry-After is still honored (up to the 15s cap), so a well-behaved
# limiter that asks for a short wait is unaffected.
_RETRY_STATUSES = frozenset({429, 503})
DEFAULT_MAX_RETRIES = 2   # extra attempts after the first GET (up to 3 total)
_BACKOFF_BASE = 1.0       # seconds; doubles each attempt: 1, 2, 4 ...
MAX_BACKOFF = 15.0        # cap on any single wait (also caps a huge Retry-After)


# ---------------------------------------------------------------------------
# Per-host rate-limit circuit breaker
# ---------------------------------------------------------------------------
# On a wide 429 storm (observed 2026-07-15 and again 2026-07-17, when the runs
# were cancelled at the workflow timeout before they could send the digest) the
# runner IP gets throttled across dozens of shops at once. With items serialised
# per domain, every item on a throttling host used to pay its own
# initial-plus-backoff ladder — up to 2 x 15s = 30s, and ~60s for a Shopify
# product that probes `.json` then the page — before giving up and being
# recorded `rate_limited`. So a shop with N watched items cost ~N x 30s of pure
# waiting; enough such shops blew the 70-min cap (07-17: 255 min of summed
# backoff and 1530 429s in the item scan, with the worst single host pinning one
# worker for ~27 min all by itself).
#
# The breaker stops the *waiting*, never the recording: once a host has
# persistently throttled us `_BREAKER_THRESHOLD` times in a run, every further
# request to it short-circuits to a synthetic 429 — no network call, no backoff
# — which the caller classifies as `rate_limited` exactly as it would a real
# one, so those items simply recover next run as they already did. State is
# process-global (a fresh process per cron run resets it) and shared by every
# `get_with_retry` caller, so it spans both the product scan and the homepage
# scan: a host tripped while pricing items also short-circuits its homepage
# check.
#
# Threshold tuned 2 -> 4 on 2026-07-18. The first prod run (07-18, still inside
# the same severe storm) delivered but short-circuited 217 items to
# `rate_limited` at a threshold of 2 — the run finished, but with a lot of stale
# prices. Raising it to 4 gives a shop more price attempts before it trips, so a
# host that is only *softly* throttling (bursty 429s that recover on retry) keeps
# getting priced instead of being written off after two unlucky items; a
# genuinely hard-blocked host still trips well before its whole item list is
# spent. The cost is a few extra ladders on the big multi-item hosts — measured
# ~5-9 min added to a storm-day item scan, still far under the 90-min cap. On a
# calm day almost nothing trips, so the knob barely matters. Raise further if
# stale-price counts stay high on moderate days; lower it back toward 2 if a
# future storm ever threatens the cap again.
_BREAKER_THRESHOLD = 4


class HostCircuitBreaker:
    """Thread-safe per-host throttle tracker (see the module note above).

    Counts persistent 429/503s per host; once a host reaches ``threshold`` it is
    "tripped" and `is_tripped` returns True so the caller can skip the fetch.
    """

    def __init__(self, threshold: int = _BREAKER_THRESHOLD) -> None:
        self._lock = threading.Lock()
        self._threshold = threshold
        self._counts: dict[str, int] = {}
        self._tripped: set[str] = set()

    def is_tripped(self, host: str) -> bool:
        if not host:
            return False
        with self._lock:
            return host in self._tripped

    def record_throttled(self, host: str) -> bool:
        """Register one persistent throttle for ``host``.

        Returns True the moment the host crosses the threshold (so the caller
        logs the trip exactly once), False otherwise.
        """
        if not host:
            return False
        with self._lock:
            n = self._counts.get(host, 0) + 1
            self._counts[host] = n
            if n >= self._threshold and host not in self._tripped:
                self._tripped.add(host)
                return True
            return False

    def reset(self) -> None:
        """Clear all state — used between tests; prod gets a fresh process."""
        with self._lock:
            self._counts.clear()
            self._tripped.clear()


# Process-global breaker shared by every get_with_retry caller.
_BREAKER = HostCircuitBreaker()


class RateLimiter:
    """Thread-safe minimum inter-request delay across all threads.

    Ensures at least ``interval`` seconds between successive acquisitions so
    concurrent (or rapid sequential) callers don't hammer shared platform
    infrastructure. The lock is held across the sleep on purpose: that
    serialises acquirers, giving a strict ``interval`` gap between request
    *starts* rather than a thundering-herd wake-up.
    """

    def __init__(self, interval: float) -> None:
        self._lock = threading.Lock()
        self._last = 0.0
        self._interval = interval

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._interval - (now - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


# ---------------------------------------------------------------------------
# Adaptive platform gate — AIMD across *runs*, not within one
# ---------------------------------------------------------------------------
# The fixed 5 s Shopify gate was set during the June/July 2026 429 storm, when a
# platform-level per-IP throttle was blanking the digest. It held Shopify at zero
# failures for two weeks, but it is priced for the worst day of the year and
# charged on every day: the gate applies to every `/products/` URL, and at ~300 of
# them a run it *is* the runtime — 25m46s of the 2026-07-19 run's 37m09s was gate
# sleep, on a day with 14 total 429s and zero circuit-breaker trips.
#
# A first attempt (2026-07-20) paced *reactively within* a run: start at 1 s and
# widen on the first persistent throttle. It failed loudly — the 2026-07-21/22
# prod runs stormed (51 hosts, ~314 items rate-limited, gate pinned at 5 s). The
# reason is structural: Shopify's per-IP throttle is a **penalty box** — once
# tripped it lasts the whole run (`Retry-After >= 15s` on every 429) — and the
# very first request of the run was already refused, so by the time the gate
# observed a throttle and widened (~30 s in) the damage was long done. Reactive
# slowing cannot un-throttle an already-tripped IP; the only lever is not tripping
# it, which means pacing proactively from request #1.
#
# So the feedback loop moved to the **across-run** timescale, the right one for a
# once-daily batch. Each run STARTS at the ceiling (or a gap earned by prior clean
# runs, persisted in the Gist); a persistent throttle SNAPS it back to the ceiling
# and flags the run "stormed"; at end of run ``next_interval`` shaves one step off
# ONLY if the run was fully clean. Over a calm stretch the start ratchets 5 -> 4.5
# -> 4 -> ... toward the floor (recovering the speed win); one storm snaps it back
# to 5 s and holds there until calm returns. Multiplicative-increase / additive-
# decrease, exactly — just clocked in days, not seconds.
#
# The floor is deliberately kept ABOVE the 1 s that stormed: even the one calm day
# at 1 s (07-20) only caught a cool IP. The circuit breaker and per-host
# Retry-After ladder are untouched and still do all per-host work; this knob
# governs only platform-wide pacing. Only a persistent **429** feeds it — see the
# note at the call site in ``get_with_retry`` for why 503 is deliberately excluded.
_ADAPT_START_INTERVAL = 5.0    # proactive default when nothing is persisted yet
_ADAPT_MIN_INTERVAL = 3.0      # fastest a clean streak may earn (stays above the 1s that stormed)
_ADAPT_MAX_INTERVAL = 5.0      # the proven-safe gap; a storm snaps us back here
_ADAPT_DECAY_STEP = 0.5        # seconds shaved off next run's start after a fully clean run


class AdaptiveRateLimiter(RateLimiter):
    """A ``RateLimiter`` that learns its gap across runs (see the note above).

    Started each run via ``seed()`` at the ceiling or a gap earned by prior clean
    runs; ``record_throttled()`` snaps the gap to ``max_interval`` and flags the
    run ``stormed``; ``next_interval`` is what to persist for next run — the
    ceiling after a storm, else one ``decay_step`` below where we ended (floored
    at ``min_interval``). The gap and the storm flag live under the base class's
    lock, so the whole thing stays safe to share across the extractor's pool.
    """

    def __init__(
        self,
        start: float = _ADAPT_START_INTERVAL,
        *,
        min_interval: float = _ADAPT_MIN_INTERVAL,
        max_interval: float = _ADAPT_MAX_INTERVAL,
        decay_step: float = _ADAPT_DECAY_STEP,
    ) -> None:
        super().__init__(start)
        self._min = min_interval
        self._max = max_interval
        self._decay_step = decay_step
        self._stormed = False

    @property
    def interval(self) -> float:
        """Current gap in seconds — read by tests and the end-of-run log line."""
        with self._lock:
            return self._interval

    @property
    def stormed(self) -> bool:
        """True once any host has persistently throttled us this run."""
        with self._lock:
            return self._stormed

    @property
    def next_interval(self) -> float:
        """The gap to *start next run* with: the ceiling after a storm, else one
        decay step below where this run ended (floored) — AIMD across runs."""
        with self._lock:
            if self._stormed:
                return self._max
            return max(self._min, self._interval - self._decay_step)

    def seed(self, interval: float) -> None:
        """Set the starting gap from persisted cross-run state (clamped to range).

        A gate explicitly disabled (``interval <= 0`` — the test/no-delay sentinel
        that ``acquire`` also honors) is left disabled: seeding never re-enables a
        gate a caller has zeroed.
        """
        with self._lock:
            if self._interval <= 0:
                return
            self._interval = max(self._min, min(self._max, interval))

    def record_throttled(self, host: str = "") -> None:
        """A host throttled us past its whole retry ladder.

        Reactive *widening* can't rescue the current run — the per-IP throttle is
        a penalty box that persists once tripped — so this just (a) guarantees the
        safe ceiling even on a run started below it on an earned speedup, and
        (b) flags the run ``stormed`` so ``next_interval`` persists the ceiling for
        tomorrow instead of decaying. Logged once, on the first throttle of a run.
        """
        with self._lock:
            first = not self._stormed
            prev = self._interval
            self._stormed = True
            self._interval = self._max
        if first:
            log.info(
                "http_util: %s persistently throttled — holding the platform gate "
                "at the %.2fs ceiling and flagging the run stormed (was %.2fs)",
                host or "a host", self._max, prev,
            )

    def acquire(self) -> None:
        # A zeroed interval is the test/no-delay configuration; skip the sleep so
        # a suite that zeroes the gate pays nothing.
        if self._interval <= 0:
            return
        super().acquire()


# Process-global adaptive gate for shared-platform (Shopify) traffic. Owned here
# because the throttle signal that feeds it is observed here, in
# ``get_with_retry``; ``src/main.py`` decides *which* requests have to pass it.
PLATFORM_LIMITER = AdaptiveRateLimiter()


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Translate a ``Retry-After`` header into seconds to wait.

    Accepts both RFC 7231 forms — delta-seconds (a bare integer) or an
    HTTP-date — and returns the seconds to wait (never negative), or ``None``
    when the header is absent or unparseable (caller then uses backoff).
    """
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (dt - now).total_seconds())


def get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    sleep=time.sleep,
    breaker: HostCircuitBreaker | None = _BREAKER,
    **get_kwargs,
) -> httpx.Response:
    """GET ``url``, retrying on 429/503 while honoring ``Retry-After``.

    On a retry status, waits the server-instructed ``Retry-After`` (or an
    exponential backoff when the header is absent/invalid, capped at
    ``MAX_BACKOFF``), up to ``max_retries`` extra attempts, then returns the
    final response — which the caller still classifies, so a persistent 429
    becomes ``rate_limited``. Network-level exceptions propagate unchanged (the
    caller owns the try/except), so this only adapts to a server that
    explicitly told us to slow down. ``get_kwargs`` are forwarded to every
    ``client.get`` (e.g. per-call ``headers`` / ``timeout`` / ``follow_redirects``).

    A process-global per-host circuit ``breaker`` (see ``HostCircuitBreaker``)
    short-circuits requests to a host that has already persistently throttled us
    this run: instead of paying the ladder above again, it hands back a
    synthetic 429 the caller records as ``rate_limited``. Pass ``breaker=None``
    to disable (behaviour is then identical to before the breaker existed).
    """
    host = urlparse(url).netloc
    if breaker is not None and breaker.is_tripped(host):
        # Host already known-throttled this run: skip the network + backoff and
        # return a synthetic 429 the caller records as `rate_limited` (recovers
        # next run), the same verdict it would have reached after 30s of waiting.
        log.info("http_util: %s rate-limited earlier this run, short-circuiting", host)
        return httpx.Response(429, request=httpx.Request("GET", url))

    resp = client.get(url, **get_kwargs)
    for attempt in range(max_retries):
        if resp.status_code not in _RETRY_STATUSES:
            return resp
        wait = parse_retry_after(resp.headers.get("Retry-After"))
        if wait is None:
            wait = _BACKOFF_BASE * (2 ** attempt)
        wait = min(wait, MAX_BACKOFF)
        log.info("http_util: %s -> %s, backing off %.1fs (retry %d/%d)",
                 url, resp.status_code, wait, attempt + 1, max_retries)
        sleep(wait)
        resp = client.get(url, **get_kwargs)

    # Exhausted retries. If the host is still throttling us, register it with the
    # breaker — enough persistent throttles and it trips, so the rest of the
    # host's items skip the ladder above instead of each paying it in full.
    if resp.status_code == 429:
        # Same signal, second consumer: snap the platform-wide gate to its ceiling
        # and flag the run stormed (so tomorrow starts pessimistic). Recorded for
        # *any* throttling host, not just Shopify ones — a per-IP throttle shows up
        # wherever we happen to be pointed when it starts.
        #
        # 429 ONLY, deliberately, even though the breaker below acts on 503 too.
        # 503 is ambiguous — most often a shop that is simply broken, and a
        # permanently-down shop 503s on every item of every run. That's harmless
        # to a *per-host* breaker (it just stops waiting on that host) but it is
        # exactly the wrong input to a *global* gate: two dead shops would pin
        # the whole run at the ceiling forever and we'd have gained nothing.
        # Caught live on the 2026-07-19 verification run, where blackrabbitco
        # 503d persistently on an otherwise calm day. 429 says "you are being
        # rate limited" and nothing else; that is the signal this gate wants.
        PLATFORM_LIMITER.record_throttled(host)
    if breaker is not None and resp.status_code in _RETRY_STATUSES:
        if breaker.record_throttled(host):
            log.info("http_util: %s tripped the rate-limit circuit breaker after "
                     "%d persistent throttles; further requests to it this run "
                     "will short-circuit to rate_limited", host, breaker._threshold)
    return resp
