"""Resilience primitives for KMS provider calls — ADR-0009 (H2).

Bounded retry with exponential backoff + full jitter, a per-provider circuit breaker, and a
per-call timeout. Fails **closed**: when the breaker is open or retries are exhausted, the call
raises ``KmsUnavailable`` (never a silent success, never an unsigned proceed). Thresholds are
configurable.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sentinelai.platform.crypto.exceptions import KmsUnavailable
from sentinelai.platform.crypto.metrics import KMS_CIRCUIT_STATE, KMS_RETRIES


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 0.1
    max_delay: float = 5.0
    timeout: float = 10.0


class CircuitBreaker:
    """Closed → (failures ≥ threshold) → Open → (after reset window) → Half-open → Closed."""

    def __init__(
        self, *, provider: str, failure_threshold: int = 5, reset_seconds: float = 30.0
    ) -> None:
        self._provider = provider
        self._threshold = failure_threshold
        self._reset = reset_seconds
        self._failures = 0
        self._opened_at: float | None = None
        self._state = "closed"
        self._publish()

    def _publish(self) -> None:
        KMS_CIRCUIT_STATE.labels(self._provider).set(
            {"closed": 0, "half_open": 1, "open": 2}[self._state]
        )

    def allow(self) -> bool:
        if self._state == "open":
            assert self._opened_at is not None
            if time.monotonic() - self._opened_at >= self._reset:
                self._state = "half_open"
                self._publish()
                return True
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._state = "closed"
        self._publish()

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._state = "open"
            self._opened_at = time.monotonic()
        self._publish()

    @property
    def state(self) -> str:
        return self._state


async def call_resilient[T](
    fn: Callable[[], Awaitable[T]],
    *,
    provider: str,
    operation: str,
    policy: RetryPolicy,
    breaker: CircuitBreaker,
) -> T:
    """Run ``fn`` with timeout + bounded jittered-backoff retry behind the breaker.

    Retries only transient failures (``KmsUnavailable``/timeout). Deterministic errors
    (``KeyNotFound``, bad request) are raised immediately and never retried.
    """
    if not breaker.allow():
        raise KmsUnavailable(f"{provider} circuit breaker is open")
    attempt = 0
    delay = policy.base_delay
    while True:
        attempt += 1
        try:
            result = await asyncio.wait_for(fn(), timeout=policy.timeout)
        except (KmsUnavailable, TimeoutError) as exc:
            breaker.record_failure()
            if attempt >= policy.max_attempts or not breaker.allow():
                raise KmsUnavailable(
                    f"{provider} {operation} failed after {attempt} attempt(s): {exc}"
                ) from exc
            KMS_RETRIES.labels(provider, operation).inc()
            await asyncio.sleep(min(policy.max_delay, delay) * random.random())  # full jitter
            delay *= 2
        else:
            breaker.record_success()
            return result
