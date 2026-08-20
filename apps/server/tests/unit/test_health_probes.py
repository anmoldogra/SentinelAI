"""Unit tests for the liveness/readiness/startup probes (implementation-wave-1.md §9, W1-11).

Drives the real router through the real ASGI app with the dependency singletons replaced on
``app.state`` — no database, Redis, MinIO, KMS, or clamd required. Postgres and Redis are the only
checks that reach real clients, so they are stubbed at the module boundary.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient

from sentinelai.entrypoints.http import health
from sentinelai.platform.crypto import HealthState


class _Health:
    """Stand-in for a subsystem exposing the ``health()`` contract."""

    def __init__(self, state: HealthState, *, raises: bool = False) -> None:
        self._state = state
        self._raises = raises
        self.calls = 0

    async def health(self) -> Any:
        self.calls += 1
        if self._raises:
            raise RuntimeError("health call failed")
        return SimpleNamespace(state=self._state, detail=None)


class _Storage:
    """Stand-in for ``ObjectStorage`` exposing only what the probe uses."""

    def __init__(self, *, reachable: bool = True) -> None:
        self._reachable = reachable
        self.calls = 0

    async def exists(self, bucket: str, key: str) -> bool:
        self.calls += 1
        if not self._reachable:
            raise ConnectionError("storage endpoint unreachable")
        return False


@pytest.fixture(autouse=True)
def _clear_probe_cache() -> None:
    """Probe results are cached in a module-level TTL cache; isolate every test."""
    health._cache.clear()


@pytest.fixture(autouse=True)
def _stub_infra(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Default postgres/redis to healthy; individual tests override."""
    state: dict[str, Any] = {"postgres": "ok", "redis": "ok", "postgres_calls": 0}

    async def _pg() -> str:
        state["postgres_calls"] += 1
        return str(state["postgres"])

    async def _redis() -> str:
        return str(state["redis"])

    monkeypatch.setattr(health, "_check_postgres", _pg)
    monkeypatch.setattr(health, "_check_redis", _redis)
    return state


def _app(
    *,
    kms: object | None = None,
    storage: object | None = None,
    scanner: object | None = None,
    startup: dict[str, Any] | None = None,
) -> FastAPI:
    """A minimal app carrying just the health router and the state the probes read."""
    app = FastAPI()
    app.include_router(health.router)
    app.state.kms = kms if kms is not None else _Health(HealthState.READY)
    app.state.object_storage = storage if storage is not None else _Storage()
    app.state.malware_scanner = scanner if scanner is not None else _Health(HealthState.READY)
    app.state.startup = startup if startup is not None else {"complete": True}
    return app


async def _get(app: FastAPI, path: str) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# --- liveness ---------------------------------------------------------------


async def test_healthz_checks_no_dependencies() -> None:
    """A dependency outage must fail readiness, never restart a healthy container."""
    app = _app(kms=_Health(HealthState.UNAVAILABLE), storage=_Storage(reachable=False))
    response = await _get(app, "/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- readiness: healthy -----------------------------------------------------


async def test_readyz_returns_200_when_every_dependency_is_healthy() -> None:
    response = await _get(_app(), "/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"] == {
        "postgres": "ok",
        "redis": "ok",
        "kms": "ok",
        "object_storage": "ok",
        "object_scanner": "ok",
    }


# --- readiness: failures ----------------------------------------------------


async def test_unreachable_object_store_fails_readiness() -> None:
    response = await _get(_app(storage=_Storage(reachable=False)), "/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["object_storage"] == "unreachable"


async def test_unavailable_scanner_fails_readiness() -> None:
    response = await _get(_app(scanner=_Health(HealthState.UNAVAILABLE)), "/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["object_scanner"] == "unavailable"


async def test_a_raising_scanner_health_call_fails_readiness_without_erroring() -> None:
    response = await _get(_app(scanner=_Health(HealthState.READY, raises=True)), "/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["object_scanner"] == "unavailable"


async def test_degraded_scanner_is_reported_but_does_not_fail_readiness() -> None:
    """§25: stale signatures are an operational warning; the scanner is off the HTTP path."""
    response = await _get(_app(scanner=_Health(HealthState.DEGRADED)), "/readyz")
    assert response.status_code == 200
    assert response.json()["checks"]["object_scanner"] == "degraded"


async def test_degraded_kms_still_fails_readiness() -> None:
    """Unlike the scanner, the KMS is on the request path — only READY passes."""
    response = await _get(_app(kms=_Health(HealthState.DEGRADED)), "/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["kms"] == "degraded"


async def test_uninitialised_singletons_are_reported_not_crashed() -> None:
    app = FastAPI()
    app.include_router(health.router)
    response = await _get(app, "/readyz")
    assert response.status_code == 503
    checks = response.json()["checks"]
    assert checks["kms"] == checks["object_storage"] == checks["object_scanner"] == "uninitialized"


async def test_readyz_lists_every_failing_dependency_not_just_the_first(
    _stub_infra: dict[str, Any],
) -> None:
    _stub_infra["postgres"] = "unreachable"
    app = _app(storage=_Storage(reachable=False), scanner=_Health(HealthState.UNAVAILABLE))
    response = await _get(app, "/readyz")
    assert response.status_code == 503
    checks = response.json()["checks"]
    assert checks["postgres"] == "unreachable"
    assert checks["object_storage"] == "unreachable"
    assert checks["object_scanner"] == "unavailable"
    assert checks["redis"] == "ok"  # healthy ones still reported


# --- TTL cache --------------------------------------------------------------


async def test_repeated_probes_do_not_re_hit_dependencies_within_the_ttl() -> None:
    storage, scanner = _Storage(), _Health(HealthState.READY)
    app = _app(storage=storage, scanner=scanner)
    for _ in range(5):
        assert (await _get(app, "/readyz")).status_code == 200
    assert storage.calls == 1
    assert scanner.calls == 1


async def test_the_cache_expires_and_re_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = _Storage()
    app = _app(storage=storage)
    await _get(app, "/readyz")
    assert storage.calls == 1

    clock = [1_000_000.0]
    monkeypatch.setattr(health.time, "monotonic", lambda: clock[0])
    health._cache.clear()
    await _get(app, "/readyz")
    assert storage.calls == 2
    clock[0] += health._CACHE_TTL_SECONDS + 0.1  # past the TTL
    await _get(app, "/readyz")
    assert storage.calls == 3


async def test_a_failure_is_not_cached_longer_than_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recovered dependency must be visible within one TTL — failures never stick."""
    clock = [2_000_000.0]
    monkeypatch.setattr(health.time, "monotonic", lambda: clock[0])
    storage = _Storage(reachable=False)
    app = _app(storage=storage)

    assert (await _get(app, "/readyz")).status_code == 503
    storage._reachable = True  # dependency recovers
    assert (await _get(app, "/readyz")).status_code == 503  # still within the TTL

    clock[0] += health._CACHE_TTL_SECONDS + 0.1
    assert (await _get(app, "/readyz")).status_code == 200


async def test_concurrent_probes_collapse_into_one_downstream_call() -> None:
    """The per-key lock is what prevents a probe stampede."""
    import asyncio

    storage = _Storage()
    app = _app(storage=storage)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(*(client.get("/readyz") for _ in range(10)))
    assert all(r.status_code == 200 for r in responses)
    assert storage.calls == 1


# --- startup gate -----------------------------------------------------------


async def test_startupz_returns_200_once_initialization_completed() -> None:
    app = _app(startup={"complete": True, "buckets_ready": True, "kms_started": True})
    response = await _get(app, "/startupz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_startupz_holds_traffic_while_initialization_is_incomplete() -> None:
    app = _app(startup={"complete": False, "buckets_ready": False, "kms_started": True})
    response = await _get(app, "/startupz")
    assert response.status_code == 503
    assert response.json()["status"] == "starting"
    assert response.json()["startup"]["buckets_ready"] is False


async def test_startupz_holds_traffic_when_the_database_schema_is_stale() -> None:
    """W1-11: a pod booted against a database missing a revision must not be admitted."""
    app = _app(
        startup={
            "complete": False,
            "kms_started": True,
            "buckets_ready": True,
            "migrations_current": False,
        }
    )
    response = await _get(app, "/startupz")
    assert response.status_code == 503
    assert response.json()["startup"]["migrations_current"] is False


async def test_startupz_fails_when_the_lifespan_never_recorded_a_result() -> None:
    app = FastAPI()
    app.include_router(health.router)
    response = await _get(app, "/startupz")
    assert response.status_code == 503
    assert response.json() == {"status": "starting", "startup": {}}


async def test_startupz_is_independent_of_current_dependency_reachability() -> None:
    """Startup reports whether init ever succeeded, not whether deps are up right now."""
    app = _app(storage=_Storage(reachable=False), startup={"complete": True})
    assert (await _get(app, "/startupz")).status_code == 200
    assert (await _get(app, "/readyz")).status_code == 503


# --- router surface ---------------------------------------------------------


def test_the_probes_are_unversioned_and_outside_the_api_envelope() -> None:
    paths = {route.path for route in health.router.routes if isinstance(route, APIRouter | object)}
    assert {"/healthz", "/readyz", "/startupz"} <= {p for p in paths if isinstance(p, str)}
    assert not any(isinstance(p, str) and p.startswith("/api/") for p in paths)
