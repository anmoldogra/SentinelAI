"""Health & readiness endpoints — api-design.md §11.

Deliberate, fixed, unversioned well-known paths (infra tooling expects them and
does not participate in API versioning). They are NOT wrapped in the API envelope.

- ``/healthz`` (liveness): process is up; checks NO dependencies — a dependency
  outage must fail readiness and reroute traffic, not restart a healthy container.
- ``/readyz`` (readiness): every dependency on the request path is reachable, else
  503 with the specific failing check identified.
"""

from __future__ import annotations

from typing import Any

import redis.asyncio as redis_asyncio
from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from sentinelai.platform.config import settings
from sentinelai.platform.crypto import HealthState
from sentinelai.platform.db.session import engine
from sentinelai.platform.logging import log

router = APIRouter(tags=["health"])


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


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, Any]:
    """Readiness probe — 200 only if every checked dependency is reachable, else 503."""
    checks = {
        "postgres": await _check_postgres(),
        "redis": await _check_redis(),
        "kms": await _check_kms(request),
    }
    if all(state == "ok" for state in checks.values()):
        return {"status": "ok", "checks": checks}
    response.status_code = 503
    return {"status": "degraded", "checks": checks}
