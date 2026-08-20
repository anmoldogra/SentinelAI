"""Health, readiness & startup endpoints — api-design.md §11, implementation-wave-1.md §9.

Deliberate, fixed, unversioned well-known paths (infra tooling expects them and
does not participate in API versioning). They are NOT wrapped in the API envelope.

- ``/healthz`` (liveness): process is up; checks NO dependencies — a dependency
  outage must fail readiness and reroute traffic, not restart a healthy container.
- ``/readyz`` (readiness): every dependency on the request path is reachable, else
  503 with **every** failing check identified (all checks run; the response never
  stops at the first failure).
- ``/startupz`` (startup): 503 until the lifespan's critical sequence actually
  succeeded, so a Kubernetes startup probe holds traffic until the process is
  truly ready — not merely running.

Readiness **reports**, never raises. Results are cached for a few seconds so
frequent probe scraping cannot stampede the dependencies; failures are cached for
exactly the same short TTL, never longer.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as redis_asyncio
from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from sentinelai.platform.config import settings
from sentinelai.platform.crypto import HealthState
from sentinelai.platform.db.session import engine
from sentinelai.platform.logging import log

router = APIRouter(tags=["health"])

# Probe-result cache lifetime. Short enough that an outage surfaces within one or two
# poll intervals, long enough that a 1s-interval probe does not hammer the dependencies.
_CACHE_TTL_SECONDS = 3.0

# Key used only to prove the object store answers an authenticated request. It is never
# written and is not expected to exist — a reachable store returns "absent", an unreachable
# or misconfigured one raises. Nothing is created, so the probe has no side effects.
_STORAGE_PROBE_KEY = ".sentinelai-healthcheck"

# A check passes when it reports "ok"...
_PASSING = frozenset({"ok"})
# ...except the malware scanner: it runs in the worker, off the HTTP request path, so stale
# signatures (§25) are an operational warning worth surfacing, not a reason to drain HTTP
# traffic. An unreachable scanner still fails readiness.
_SCANNER_PASSING = frozenset({"ok", "degraded"})
_SCANNER = "object_scanner"


class _TtlCache:
    """Tiny async-safe TTL cache for probe results.

    One lock per key collapses a burst of concurrent probes into a single downstream call
    (the others await the in-flight result), which is the stampede this exists to prevent.
    Entries — successes and failures alike — expire after the same TTL.
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[float, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def _fresh(self, key: str, now: float) -> str | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        return value if expires_at > now else None

    async def get_or_run(self, key: str, produce: Callable[[], Awaitable[str]]) -> str:
        cached = self._fresh(key, time.monotonic())
        if cached is not None:
            return cached
        async with self._lock(key):
            # Re-check: another probe may have refreshed the entry while we waited.
            cached = self._fresh(key, time.monotonic())
            if cached is not None:
                return cached
            value = await produce()
            self._entries[key] = (time.monotonic() + self._ttl, value)
            return value

    def clear(self) -> None:
        """Drop all cached results (used by tests; not exposed over HTTP)."""
        self._entries.clear()


_cache = _TtlCache(_CACHE_TTL_SECONDS)


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe — no dependency checks by design."""
    return {"status": "ok"}


async def _check_postgres() -> str:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return "ok"
    except Exception:  # readiness must report, not raise
        log.warning("readyz_postgres_unreachable")
        return "unreachable"


async def _check_redis() -> str:
    client = redis_asyncio.Redis.from_url(settings.redis_url)
    try:
        await client.ping()
        return "ok"
    except Exception:
        log.warning("readyz_redis_unreachable")
        return "unreachable"
    finally:
        await client.aclose()


async def _check_kms(request: Request) -> str:
    kms = getattr(request.app.state, "kms", None)
    if kms is None:
        return "uninitialized"
    try:
        status = await kms.health()
    except Exception:  # readiness reports, never raises
        log.warning("readyz_kms_health_failed")
        return "unavailable"
    return "ok" if status.state == HealthState.READY else status.state.value


async def _check_object_storage(request: Request) -> str:
    """Prove the object store answers an authenticated request for the evidence bucket.

    ``exists`` on a key that is not expected to be there is a HEAD: it returns ``False``
    when the store is reachable and raises when it is not (including a missing bucket,
    which surfaces as ``BucketNotFound``). No object is read, written, or created.
    """
    storage = getattr(request.app.state, "object_storage", None)
    if storage is None:
        return "uninitialized"
    try:
        await storage.exists(settings.storage_bucket, _STORAGE_PROBE_KEY)
    except Exception:
        log.warning("readyz_object_storage_unreachable", bucket=settings.storage_bucket)
        return "unreachable"
    return "ok"


async def _check_scanner(request: Request) -> str:
    """Malware-scanner reachability + signature freshness (security-architecture §25)."""
    scanner = getattr(request.app.state, "malware_scanner", None)
    if scanner is None:
        return "uninitialized"
    try:
        status = await scanner.health()
    except Exception:  # health() should never raise, but readiness still must not
        log.warning("readyz_scanner_health_failed")
        return "unavailable"
    if status.state == HealthState.DEGRADED:
        # Surfaced in the response body (and logged by the adapter) without failing readiness.
        log.warning("readyz_scanner_degraded", detail=status.detail)
    return "ok" if status.state == HealthState.READY else status.state.value


def _passing(name: str, state: str) -> bool:
    return state in (_SCANNER_PASSING if name == _SCANNER else _PASSING)


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, Any]:
    """Readiness probe — 200 only if every checked dependency is reachable, else 503.

    Every check runs (concurrently) so the response lists **all** failing dependencies.
    """
    names = ("postgres", "redis", "kms", "object_storage", _SCANNER)
    results = await asyncio.gather(
        _cache.get_or_run("postgres", _check_postgres),
        _cache.get_or_run("redis", _check_redis),
        _cache.get_or_run("kms", lambda: _check_kms(request)),
        _cache.get_or_run("object_storage", lambda: _check_object_storage(request)),
        _cache.get_or_run(_SCANNER, lambda: _check_scanner(request)),
    )
    checks = dict(zip(names, results, strict=True))
    if all(_passing(name, state) for name, state in checks.items()):
        return {"status": "ok", "checks": checks}
    response.status_code = 503
    return {"status": "degraded", "checks": checks}


@router.get("/startupz")
async def startupz(request: Request, response: Response) -> dict[str, Any]:
    """Startup probe — 503 until the lifespan's critical sequence completed successfully.

    Distinct from ``/readyz``: this reports whether initialization (config validation, KMS
    start, bucket bootstrap) ever *succeeded*, not whether dependencies are reachable right
    now. A process that started degraded — e.g. buckets could not be created outside a
    production profile — keeps failing this probe, so a Kubernetes startup probe holds
    traffic instead of admitting a half-initialized pod.
    """
    state = getattr(request.app.state, "startup", None)
    if state is not None and state.get("complete") is True:
        return {"status": "ok", "startup": state}
    response.status_code = 503
    return {"status": "starting", "startup": state or {}}
