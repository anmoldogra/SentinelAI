"""Unit tests for the KMS resilience primitives (ADR-0009 H2).

Retry, backoff, and the circuit breaker are pure logic, so these are deterministic: jitter and the
clock are pinned, and no sleeping actually happens. The property that matters throughout is
**fail-closed** — every exhausted path raises ``KmsUnavailable`` rather than returning.
"""

from __future__ import annotations

import asyncio

import pytest

from sentinelai.platform.crypto.exceptions import KeyNotFound, KmsUnavailable
from sentinelai.platform.crypto.resilience import (
    CircuitBreaker,
    RetryPolicy,
    call_resilient,
)

_FAST = RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0, timeout=1.0)


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff is exercised for its control flow, not its wall-clock delay."""
    real_sleep = asyncio.sleep

    async def _instant(_seconds: float) -> None:
        await real_sleep(0)  # zero delay, but still yields to the loop

    monkeypatch.setattr(asyncio, "sleep", _instant)


def _breaker(**overrides: object) -> CircuitBreaker:
    kwargs: dict[str, object] = {
        "provider": "test",
        "failure_threshold": 5,
        "reset_seconds": 30.0,
    }
    kwargs.update(overrides)
    return CircuitBreaker(**kwargs)  # type: ignore[arg-type]


async def _run(fn, breaker: CircuitBreaker, policy: RetryPolicy = _FAST):  # type: ignore[no-untyped-def]
    return await call_resilient(fn, provider="test", operation="op", policy=policy, breaker=breaker)


# --- circuit breaker state machine ------------------------------------------


def test_a_new_breaker_is_closed_and_allows_calls() -> None:
    breaker = _breaker()
    assert breaker.state == "closed"
    assert breaker.allow() is True


def test_the_breaker_opens_at_the_failure_threshold() -> None:
    breaker = _breaker(failure_threshold=3)
    for _ in range(2):
        breaker.record_failure()
    assert breaker.state == "closed"  # below threshold
    breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.allow() is False


def test_a_success_resets_the_failure_count() -> None:
    breaker = _breaker(failure_threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.state == "closed"  # the count restarted, so one failure is not enough


def test_the_breaker_half_opens_after_the_reset_window(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr("sentinelai.platform.crypto.resilience.time.monotonic", lambda: clock[0])
    breaker = _breaker(failure_threshold=1, reset_seconds=30.0)
    breaker.record_failure()
    assert breaker.allow() is False

    clock[0] += 30.0
    assert breaker.allow() is True
    assert breaker.state == "half_open"


def test_a_success_in_half_open_closes_the_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr("sentinelai.platform.crypto.resilience.time.monotonic", lambda: clock[0])
    breaker = _breaker(failure_threshold=1, reset_seconds=5.0)
    breaker.record_failure()
    clock[0] += 5.0
    breaker.allow()  # -> half_open
    breaker.record_success()
    assert breaker.state == "closed"


# --- call_resilient ---------------------------------------------------------


async def test_a_successful_call_returns_its_value_and_closes_the_breaker() -> None:
    breaker = _breaker()
    breaker.record_failure()

    async def _ok() -> str:
        return "value"

    assert await _run(_ok, breaker) == "value"
    assert breaker.state == "closed"


async def test_a_transient_failure_is_retried_until_it_succeeds() -> None:
    attempts = 0

    async def _flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise KmsUnavailable("transient")
        return "recovered"

    assert await _run(_flaky, _breaker()) == "recovered"
    assert attempts == 3


async def test_retries_are_bounded_by_max_attempts() -> None:
    attempts = 0

    async def _always_down() -> str:
        nonlocal attempts
        attempts += 1
        raise KmsUnavailable("down")

    with pytest.raises(KmsUnavailable, match="failed after 3 attempt"):
        await _run(_always_down, _breaker())
    assert attempts == 3  # policy.max_attempts, not unbounded


async def test_a_deterministic_error_is_raised_immediately_and_never_retried() -> None:
    """A missing key will still be missing on the next attempt — retrying only adds latency."""
    attempts = 0

    async def _missing() -> str:
        nonlocal attempts
        attempts += 1
        raise KeyNotFound("no such key")

    with pytest.raises(KeyNotFound):
        await _run(_missing, _breaker())
    assert attempts == 1


async def test_a_timeout_is_treated_as_transient_and_retried() -> None:
    attempts = 0

    async def _slow() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError
        return "ok"

    assert await _run(_slow, _breaker()) == "ok"
    assert attempts == 2


async def test_an_open_breaker_short_circuits_before_calling() -> None:
    """Fail closed: an open breaker must not even attempt the call."""
    breaker = _breaker(failure_threshold=1)
    breaker.record_failure()
    called = False

    async def _never() -> str:
        nonlocal called
        called = True
        return "should not happen"

    with pytest.raises(KmsUnavailable, match="circuit breaker is open"):
        await _run(_never, breaker)
    assert called is False


async def test_the_breaker_tripping_mid_retry_stops_further_attempts() -> None:
    attempts = 0

    async def _down() -> str:
        nonlocal attempts
        attempts += 1
        raise KmsUnavailable("down")

    breaker = _breaker(failure_threshold=2)
    with pytest.raises(KmsUnavailable):
        await _run(_down, breaker, RetryPolicy(max_attempts=10, base_delay=0.0, max_delay=0.0))
    assert attempts == 2  # stopped by the breaker, well before max_attempts
    assert breaker.state == "open"


async def test_the_original_failure_is_chained_onto_the_raised_error() -> None:
    async def _down() -> str:
        raise KmsUnavailable("root cause detail")

    with pytest.raises(KmsUnavailable) as caught:
        await _run(_down, _breaker())
    assert isinstance(caught.value.__cause__, KmsUnavailable)


async def test_an_actual_timeout_of_the_wrapped_call_is_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """asyncio.sleep is stubbed for backoff, so drive the timeout through wait_for directly."""
    monkeypatch.undo()  # restore the real asyncio.sleep for this test

    async def _too_slow() -> str:
        await asyncio.sleep(0.5)
        return "never"

    policy = RetryPolicy(max_attempts=1, base_delay=0.0, max_delay=0.0, timeout=0.01)
    with pytest.raises(KmsUnavailable):
        await _run(_too_slow, _breaker(), policy)
