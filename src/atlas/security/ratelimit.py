"""Rate limiting - the control that turns a stolen key into an annoyance.

An agent platform has an unusual property: each request can cost real money
(model tokens) and take a long time. So there are two independent limits,
and both matter:

    request rate   protects the service from load
    spend budget   protects the *bill* from a compromised or looping client

A token bucket is used rather than a fixed window because fixed windows
allow a 2x burst across the boundary, which for a spend limit means double
the budget. Buckets are per-principal, so one noisy client cannot consume
another's allowance.

Single-process, in-memory by design: distributing this needs Redis, and
pretending an in-memory limiter works behind multiple replicas is a
correctness lie. That limitation is documented in LIMITATIONS.md, and the
interface is the seam where a Redis implementation drops in.
"""

from __future__ import annotations

import threading
import time

from pydantic import BaseModel, ConfigDict


class RateLimitResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    retry_after_s: float = 0.0
    reason: str = ""
    remaining: float = 0.0


class TokenBucket:
    """Classic token bucket: `capacity` tokens, refilled at `refill_per_s`."""

    def __init__(self, capacity: float, refill_per_s: float) -> None:
        self.capacity = float(capacity)
        self.refill_per_s = float(refill_per_s)
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, amount: float = 1.0) -> RateLimitResult:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._updated
            self._updated = now
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_s)
            if self._tokens >= amount:
                self._tokens -= amount
                return RateLimitResult(allowed=True, remaining=round(self._tokens, 3))
            deficit = amount - self._tokens
            retry_after = deficit / self.refill_per_s if self.refill_per_s else 60.0
            return RateLimitResult(
                allowed=False,
                retry_after_s=round(retry_after, 3),
                reason="rate limit exceeded",
                remaining=round(self._tokens, 3),
            )


class RateLimiter:
    """Per-principal request-rate and spend limiting."""

    def __init__(
        self,
        *,
        requests_per_minute: float = 60,
        burst: float | None = None,
        daily_spend_usd: float = 25.0,
    ) -> None:
        self._rpm = requests_per_minute
        self._burst = burst if burst is not None else max(5.0, requests_per_minute / 4)
        self._daily_spend = daily_spend_usd
        self._buckets: dict[str, TokenBucket] = {}
        self._spend: dict[str, float] = {}
        self._reserved: dict[str, float] = {}
        # Per-principal windows: one global window would reset everyone's
        # daily budget simultaneously, 24h after process start, which is not
        # a "daily" limit for anyone.
        self._window_start: dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, principal: str) -> RateLimitResult:
        with self._lock:
            bucket = self._buckets.get(principal)
            if bucket is None:
                bucket = TokenBucket(self._burst, self._rpm / 60.0)
                self._buckets[principal] = bucket
        return bucket.consume()

    @property
    def daily_spend_usd(self) -> float:
        """The configured daily cap, so callers can validate their own
        budget invariants against it instead of reaching for a private."""
        return self._daily_spend

    def reserve_spend(self, principal: str, projected_usd: float = 0.0) -> RateLimitResult:
        """Atomically check *and reserve* budget before a run starts.

        Check-then-spend is a TOCTOU race: N concurrent requests all read the
        same balance, all pass, and the budget overshoots by N x projection.
        Reserving inside the same lock closes it - the reservation is
        released and replaced by the real figure in `record_spend`.
        """
        with self._lock:
            self._roll_window(principal)
            committed = self._spend.get(principal, 0.0) + self._reserved.get(principal, 0.0)
            if committed + projected_usd > self._daily_spend:
                return RateLimitResult(
                    allowed=False,
                    reason=(
                        f"daily spend limit reached (${committed:.4f} of ${self._daily_spend:.2f})"
                    ),
                    retry_after_s=3600.0,
                    remaining=max(0.0, self._daily_spend - committed),
                )
            self._reserved[principal] = self._reserved.get(principal, 0.0) + projected_usd
            return RateLimitResult(
                allowed=True,
                remaining=round(self._daily_spend - committed - projected_usd, 4),
            )

    def peek_spend(self, principal: str, projected_usd: float = 0.0) -> RateLimitResult:
        """Read-only budget view. **Never gate a run on this.**

        It is deliberately named `peek`, not `check`: an earlier `check_spend`
        looked like the right thing to call before a run and reintroduced the
        exact TOCTOU race `reserve_spend` exists to close. Use it for
        dashboards and headers only.
        """
        with self._lock:
            self._roll_window(principal)
            committed = self._spend.get(principal, 0.0) + self._reserved.get(principal, 0.0)
            if committed + projected_usd > self._daily_spend:
                return RateLimitResult(
                    allowed=False,
                    reason=(
                        f"daily spend limit reached (${committed:.4f} of ${self._daily_spend:.2f})"
                    ),
                    retry_after_s=3600.0,
                    remaining=max(0.0, self._daily_spend - committed),
                )
            return RateLimitResult(allowed=True, remaining=round(self._daily_spend - committed, 4))

    def record_spend(self, principal: str, usd: float, *, reserved: float = 0.0) -> None:
        """Commit actual spend and release any reservation held for it."""
        with self._lock:
            self._roll_window(principal)
            self._spend[principal] = self._spend.get(principal, 0.0) + max(0.0, usd)
            if reserved:
                self._reserved[principal] = max(0.0, self._reserved.get(principal, 0.0) - reserved)

    def release_reservation(self, principal: str, amount: float) -> None:
        """Return an unused reservation (e.g. the run failed before spending)."""
        with self._lock:
            self._reserved[principal] = max(0.0, self._reserved.get(principal, 0.0) - amount)

    def spend_for(self, principal: str) -> float:
        with self._lock:
            self._roll_window(principal)
            return round(self._spend.get(principal, 0.0), 6)

    def _roll_window(self, principal: str) -> None:
        now = time.monotonic()
        start = self._window_start.get(principal)
        if start is None:
            self._window_start[principal] = now
            return
        if now - start >= 86_400:
            self._spend.pop(principal, None)
            self._reserved.pop(principal, None)
            self._window_start[principal] = now
