"""LLM governance — rate limits, retry, cache, budget, usage tracking."""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Any

from logging_config import get_logger
from fetch.politeness import jittered_backoff

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Rate Limiter
# ---------------------------------------------------------------------------
@dataclass
class RateLimiter:
    """Token-bucket rate limiter per key (user_id or API key).

    `time_fn`/`sleep_fn` are injectable seams (default to the real clock) so
    tests can drive `wait_and_allow` with a fake clock instead of a real sleep.
    """
    max_requests: int = 60
    window_seconds: int = 60
    time_fn: Any = time.monotonic
    sleep_fn: Any = time.sleep

    def __post_init__(self):
        self._requests: dict[str, list[float]] = defaultdict(list)

    def _prune(self, key: str) -> float:
        now = self.time_fn()
        cutoff = now - self.window_seconds
        self._requests[key] = [t for t in self._requests[key] if t > cutoff]
        return now

    def is_allowed(self, key: str) -> bool:
        now = self._prune(key)
        if len(self._requests[key]) >= self.max_requests:
            return False
        self._requests[key].append(now)
        return True

    def remaining(self, key: str) -> int:
        self._prune(key)
        return max(0, self.max_requests - len(self._requests[key]))

    def wait_time(self, key: str) -> float:
        """Seconds until the next call would be allowed (0 if allowed now)."""
        self._prune(key)
        if len(self._requests[key]) < self.max_requests:
            return 0.0
        oldest = min(self._requests[key])
        return max(0.0, (oldest + self.window_seconds) - self.time_fn())

    def wait_and_allow(self, key: str) -> float:
        """Block (via sleep_fn) until a slot is free, then record the call.

        Proactive counterpart to is_allowed(): instead of rejecting a call
        that would exceed the limit, it waits exactly long enough not to.
        Returns the number of seconds waited (0 if none were needed).
        """
        waited = self.wait_time(key)
        if waited > 0:
            self.sleep_fn(waited)
        now = self._prune(key)
        self._requests[key].append(now)
        return waited


# ---------------------------------------------------------------------------
# Retry with exponential backoff
# ---------------------------------------------------------------------------
def _shared_retry_backoff() -> tuple[float, Any]:
    """The fetch layer's jitter setting + RNG, shared with LLM retries.

    Read at call time (not import time) so the policy always tracks the live
    process-wide limiter rather than a snapshot of it. Defensive by design: if
    politeness cannot be reached the policy gets ``jitter=0.0`` (no RNG), i.e.
    exactly the deterministic backoff this module always had.
    """
    try:
        from fetch.politeness import get_limiter
        limiter = get_limiter()
        return limiter.jitter, limiter.rng
    except Exception:  # pragma: no cover - only hit if fetch package is absent
        return 0.0, None


@dataclass
class RetryPolicy:
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    retryable_exceptions: tuple = (TimeoutError, ConnectionError, OSError)
    # Jitter shared with fetch.politeness (see _shared_retry_backoff). Both
    # default to 0.0/None so a bare RetryPolicy() stays byte-for-byte the
    # legacy deterministic delay and never draws from an RNG.
    jitter: float = 0.0
    rng: Any = None

    def delay_for_attempt(self, attempt: int) -> float:
        delay = min(self.base_delay * (2 ** attempt), self.max_delay)
        if self.jitter <= 0 or self.rng is None:
            return delay
        return jittered_backoff(delay, self.jitter, self.rng)


# ---------------------------------------------------------------------------
# Response Cache
# ---------------------------------------------------------------------------
class ResponseCache:
    """LRU-ish cache keyed by prompt hash. Evicts entries older than max_age."""

    def __init__(self, max_size: int = 1000, max_age_seconds: int = 3600):
        self.max_size = max_size
        self.max_age = max_age_seconds
        self._cache: dict[str, tuple[Any, float]] = {}

    @staticmethod
    def _key(model: str, messages: list[dict],
             prompt_version: str = "", schema_version: str = "",
             temperature: float = 0.0, provider: str = "") -> str:
        raw = json.dumps({
            "provider": provider,
            "model": model,
            "messages": messages,
            "prompt_version": prompt_version,
            "schema_version": schema_version,
            "temperature": temperature,
        }, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def get(self, model: str, messages: list[dict],
            prompt_version: str = "", schema_version: str = "",
            temperature: float = 0.0, provider: str = "") -> Any | None:
        key = self._key(model, messages, prompt_version, schema_version, temperature, provider)
        if key in self._cache:
            value, ts = self._cache[key]
            if time.monotonic() - ts < self.max_age:
                return value
            del self._cache[key]
        return None

    def set(self, model: str, messages: list[dict], value: Any,
            prompt_version: str = "", schema_version: str = "",
            temperature: float = 0.0, provider: str = "") -> None:
        key = self._key(model, messages, prompt_version, schema_version, temperature, provider)
        if len(self._cache) >= self.max_size:
            oldest = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest]
        self._cache[key] = (value, time.monotonic())

    def clear(self) -> None:
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)


# ---------------------------------------------------------------------------
# Budget Tracker
# ---------------------------------------------------------------------------
@dataclass
class BudgetEntry:
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    requests: int = 0


class BudgetTracker:
    """Tracks usage per key with daily and monthly limits."""

    def __init__(self, daily_limit_usd: float = 10.0,
                 monthly_limit_usd: float = 200.0):
        self.daily_limit = daily_limit_usd
        self.monthly_limit = monthly_limit_usd
        self._daily: dict[str, dict[str, BudgetEntry]] = defaultdict(lambda: defaultdict(BudgetEntry))
        self._monthly: dict[str, dict[str, BudgetEntry]] = defaultdict(lambda: defaultdict(BudgetEntry))

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _month() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def record(self, key: str, tokens_in: int, tokens_out: int,
               cost_usd: float) -> None:
        day = self._today()
        month = self._month()
        entry_d = self._daily[key][day]
        entry_d.tokens_in += tokens_in
        entry_d.tokens_out += tokens_out
        entry_d.cost_usd += cost_usd
        entry_d.requests += 1

        entry_m = self._monthly[key][month]
        entry_m.tokens_in += tokens_in
        entry_m.tokens_out += tokens_out
        entry_m.cost_usd += cost_usd
        entry_m.requests += 1

    def daily_spend(self, key: str) -> float:
        day = self._today()
        return self._daily[key][day].cost_usd

    def monthly_spend(self, key: str) -> float:
        month = self._month()
        return self._monthly[key][month].cost_usd

    def is_over_daily_limit(self, key: str) -> bool:
        return self.daily_spend(key) >= self.daily_limit

    def is_over_monthly_limit(self, key: str) -> bool:
        return self.monthly_spend(key) >= self.monthly_limit

    def is_over_limit(self, key: str) -> bool:
        return self.is_over_daily_limit(key) or self.is_over_monthly_limit(key)

    def get_usage(self, key: str) -> dict:
        day = self._today()
        month = self._month()
        d = self._daily[key][day]
        m = self._monthly[key][month]
        return {
            "daily": {
                "cost_usd": round(d.cost_usd, 6),
                "tokens_in": d.tokens_in,
                "tokens_out": d.tokens_out,
                "requests": d.requests,
                "limit_usd": self.daily_limit,
                "remaining_usd": round(max(0, self.daily_limit - d.cost_usd), 6),
            },
            "monthly": {
                "cost_usd": round(m.cost_usd, 6),
                "tokens_in": m.tokens_in,
                "tokens_out": m.tokens_out,
                "requests": m.requests,
                "limit_usd": self.monthly_limit,
                "remaining_usd": round(max(0, self.monthly_limit - m.cost_usd), 6),
            },
        }


# ---------------------------------------------------------------------------
# Usage Metrics Logger
# ---------------------------------------------------------------------------
@dataclass
class UsageMetrics:
    """Collects per-request metrics for observability."""
    request_id: str = ""
    model: str = ""
    provider: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    cached: bool = False
    retries: int = 0
    error: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class UsageLog:
    """In-memory log of usage metrics. Replace with DB-backed store in production."""

    def __init__(self, max_entries: int = 10000):
        self.max_entries = max_entries
        self._entries: list[UsageMetrics] = []

    def record(self, entry: UsageMetrics) -> None:
        self._entries.append(entry)
        if len(self._entries) > self.max_entries:
            self._entries = self._entries[-self.max_entries:]

    def recent(self, n: int = 50) -> list[UsageMetrics]:
        return self._entries[-n:]

    def totals(self) -> dict:
        if not self._entries:
            return {"requests": 0, "tokens_in": 0, "tokens_out": 0,
                    "cost_usd": 0.0, "cached": 0, "errors": 0}
        return {
            "requests": len(self._entries),
            "tokens_in": sum(e.tokens_in for e in self._entries),
            "tokens_out": sum(e.tokens_out for e in self._entries),
            "cost_usd": round(sum(e.cost_usd for e in self._entries), 6),
            "cached": sum(1 for e in self._entries if e.cached),
            "errors": sum(1 for e in self._entries if e.error),
        }

    def clear(self) -> None:
        self._entries.clear()


# ---------------------------------------------------------------------------
# Price config (USD per 1K tokens) — override via env or config
# ---------------------------------------------------------------------------
DEFAULT_PRICES = {
    # Cohere Command A pricing, USD per 1K tokens.
    "command-a-03-2025": {"input": 0.0025, "output": 0.01},
    "gpt-4o": {"input": 0.0025, "output": 0.01},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "claude-sonnet-4-20250514": {"input": 0.003, "output": 0.015},
    "gemini-flash-lite-latest": {"input": 0.0001, "output": 0.0004},
}

# Google no longer publishes a stable per-tier RPM number for
# gemini-flash-lite-latest in its own docs - ai.google.dev/gemini-api/docs/rate-limits
# and /pricing both redirect to a live, per-project dashboard at
# aistudio.google.com/rate-limit that requires the caller's own login to view.
# Third-party trackers in 2026 report this model's free tier anywhere from 15
# to 30 RPM, and the limit itself has already been cut at least once (a
# published change effective ~April 2026). 15 is the conservative end of that
# range: waiting a little when it wasn't strictly necessary is cheap; a 429
# that aborts a chunk's extraction is not. Override via GEMINI_FREE_TIER_RPM
# if your project's actual dashboard number differs.
DEFAULT_GEMINI_FREE_TIER_RPM = 15
# Paid tier 1 is reported far higher (low thousands of RPM) and is not the
# limiting factor in practice - this default just needs to be well above
# anything this pipeline could realistically generate. Override via
# GEMINI_PAID_TIER_RPM to match your project's actual paid-tier dashboard number.
DEFAULT_GEMINI_PAID_TIER_RPM = 1000

GEMINI_TIER_FREE = "free"
GEMINI_TIER_PAID = "paid"
_VALID_GEMINI_TIERS = (GEMINI_TIER_FREE, GEMINI_TIER_PAID)


def get_gemini_tier() -> str:
    """Which Gemini quota tier this environment is on.

    Controlled by GEMINI_TIER=free|paid, defaulting to the conservative
    free-tier assumption - a deployment must opt into "paid" explicitly, it
    is never inferred. Drives both get_shared_governance()'s RateLimiter
    (via get_gemini_rpm) and GeminiLLMClient's retry ceiling (llm_client.py).
    """
    tier = os.getenv("GEMINI_TIER", GEMINI_TIER_FREE).strip().lower()
    if tier not in _VALID_GEMINI_TIERS:
        raise ValueError(
            f"Unsupported GEMINI_TIER={tier!r}; expected one of {_VALID_GEMINI_TIERS}."
        )
    return tier


def get_gemini_rpm() -> int:
    """The RPM to proactively throttle Gemini calls to, for the active tier."""
    if get_gemini_tier() == GEMINI_TIER_PAID:
        return int(os.getenv("GEMINI_PAID_TIER_RPM", str(DEFAULT_GEMINI_PAID_TIER_RPM)))
    return int(os.getenv("GEMINI_FREE_TIER_RPM", str(DEFAULT_GEMINI_FREE_TIER_RPM)))


def estimate_cost(model: str, tokens_in: int, tokens_out: int,
                  prices: dict | None = None) -> float:
    """Estimate USD cost for a request."""
    p = (prices or DEFAULT_PRICES).get(model, {"input": 0.005, "output": 0.015})
    return (tokens_in * p["input"] + tokens_out * p["output"]) / 1000


# ---------------------------------------------------------------------------
# Shared governance instance — for production call sites only
# ---------------------------------------------------------------------------
# get_client() previously built a brand-new, empty-state LLMGovernance() on
# every single call. For a one-off extraction that's harmless, but a sync
# run that extracts many documents/sources in the same process never let the
# response cache or rate limiter see calls from anything but the one
# document in front of it - the cache couldn't dedupe across a sync batch
# and the rate limiter couldn't see load from other sources sharing the same
# Gemini quota. Production call sites opt in explicitly by passing
# get_shared_governance() into get_client(governance=...); tests and one-off
# scripts that want isolated state keep constructing their own
# LLMGovernance() (the default when governance=None), so this never leaks
# state across tests.
_shared_governance: "LLMGovernance | None" = None


def get_shared_governance() -> "LLMGovernance":
    """Lazily build and return ONE process-wide LLMGovernance instance.

    The cache's max_age is set much longer than the per-call default (a full
    day, not an hour) since a resync of the same unchanged content can
    legitimately happen the next day, not just within the same hour.

    The rate limiter defaults to get_gemini_rpm() - the active GEMINI_TIER's
    RPM (free by default, conservative; paid is opt-in via GEMINI_TIER=paid)
    - since requirement extraction is this governance instance's one real
    use, and Gemini's free tier is the tightest cap among the supported
    providers. GeminiLLMClient calls pre_request(..., wait=True) so this
    throttles proactively instead of reacting to 429s; OpenAILLMClient keeps
    the reactive default (wait=False), so this same number only ever makes
    OpenAI calls wait if a deployment somehow mixes both providers' traffic
    through one process - the common case is one EXTRACTION_MODEL (and
    therefore one provider) per deployment.
    """
    global _shared_governance
    if _shared_governance is None:
        _shared_governance = LLMGovernance(
            cache=ResponseCache(
                max_size=int(os.getenv("LLM_CACHE_MAX_SIZE", "5000")),
                max_age_seconds=int(os.getenv("LLM_CACHE_MAX_AGE_SECONDS", str(24 * 3600))),
            ),
            rate_limiter=RateLimiter(
                max_requests=get_gemini_rpm(),
                window_seconds=60,
            ),
        )
    return _shared_governance


def reset_shared_governance() -> None:
    """Test-only: drop the process-wide instance so the next get_shared_governance()
    call builds a fresh one. Production code never calls this."""
    global _shared_governance
    _shared_governance = None


# ---------------------------------------------------------------------------
# Governance Manager — ties everything together
# ---------------------------------------------------------------------------
class LLMGovernance:
    """Facade that applies rate limits, caching, retry, budget checks."""

    def __init__(self, *, rate_limiter: RateLimiter | None = None,
                 retry_policy: RetryPolicy | None = None,
                 cache: ResponseCache | None = None,
                 budget: BudgetTracker | None = None,
                 usage_log: UsageLog | None = None):
        self.rate_limiter = rate_limiter or RateLimiter()
        if retry_policy is not None:
            self.retry_policy = retry_policy
        else:
            # Share the fetch layer's jitter + RNG so retries across the whole
            # process are spaced by the same configuration (FETCH_JITTER_SECONDS).
            # A caller passing their own RetryPolicy() still gets the exact
            # deterministic legacy delay, because that is what it defaults to.
            _jitter, _rng = _shared_retry_backoff()
            self.retry_policy = RetryPolicy(jitter=_jitter, rng=_rng)
        self.cache = cache or ResponseCache()
        self.budget = budget or BudgetTracker()
        self.usage_log = usage_log or UsageLog()

    def pre_request(self, key: str, model: str,
                    messages: list[dict],
                    prompt_version: str = "",
                    schema_version: str = "",
                    temperature: float = 0.0,
                    provider: str = "",
                    wait: bool = False) -> dict[str, Any]:
        """Check cache, rate limits, budget before making an LLM call.
        Returns metadata dict with {cached, cached_response, blocked, reason}.

        `wait=False` (the default) is the original reactive behavior: a call
        that would exceed the rate limit is rejected immediately
        (blocked=True, reason="rate_limit_exceeded") and it is up to the
        caller to decide what to do. `wait=True` instead blocks proactively
        (via the rate limiter's sleep_fn) until a slot is free, so the call
        almost never needs to be rejected or hit a real 429 in the first
        place - used for providers with a known, tight free-tier RPM cap
        (see llm_client.py's GeminiLLMClient).
        """
        # 1. Cache lookup
        cached = self.cache.get(model, messages, prompt_version, schema_version, temperature, provider=provider)
        if cached is not None:
            return {"cached": True, "cached_response": cached,
                    "blocked": False, "reason": None, "waited_seconds": 0.0}

        # 2. Rate limit
        if wait:
            waited = self.rate_limiter.wait_and_allow(key)
            if waited > 0:
                logger.info("Rate limiter proactively waited before LLM call",
                            extra={"key": key, "waited_seconds": round(waited, 2)})
        else:
            waited = 0.0
            if not self.rate_limiter.is_allowed(key):
                return {"cached": False, "cached_response": None,
                        "blocked": True, "reason": "rate_limit_exceeded",
                        "waited_seconds": 0.0}

        # 3. Budget
        if self.budget.is_over_limit(key):
            return {"cached": False, "cached_response": None,
                    "blocked": True, "reason": "budget_exceeded",
                    "waited_seconds": waited}

        return {"cached": False, "cached_response": None,
                "blocked": False, "reason": None, "waited_seconds": waited}

    def post_request(self, key: str, model: str,
                     messages: list[dict], response: Any,
                     tokens_in: int, tokens_out: int,
                     latency_ms: float, retries: int = 0,
                     error: str | None = None,
                     prompt_version: str = "",
                     schema_version: str = "",
                     temperature: float = 0.0,
                     provider: str = "") -> UsageMetrics:
        """Record usage, update cache and budget after an LLM call."""
        cost = estimate_cost(model, tokens_in, tokens_out)
        self.budget.record(key, tokens_in, tokens_out, cost)

        if error is None:
            self.cache.set(model, messages, response, prompt_version, schema_version, temperature, provider=provider)

        entry = UsageMetrics(
            model=model, tokens_in=tokens_in, tokens_out=tokens_out,
            cost_usd=cost, latency_ms=latency_ms, retries=retries,
            error=error,
        )
        self.usage_log.record(entry)

        logger.info("LLM request completed",
                     extra={"model": model, "tokens_in": tokens_in,
                            "tokens_out": tokens_out, "cost_usd": cost,
                            "latency_ms": latency_ms, "cached": False,
                            "retries": retries, "error": error})
        return entry
