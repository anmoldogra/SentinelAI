"""HTTP entrypoint — FastAPI application factory & lifespan (guide Part 2).

One deployable process: builds the app, wires cross-cutting middleware/handlers,
registers module routers, exposes ``/metrics``, and runs the Phase 1 in-process
``EventDispatcher`` for the lifetime of the process (started/stopped in the
lifespan, honoring the graceful-shutdown requirement of event-driven §2.2).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from sentinelai import __version__
from sentinelai.entrypoints.http import health
from sentinelai.entrypoints.http.exception_handlers import register_exception_handlers
from sentinelai.entrypoints.http.middleware import register_middleware

# Domain module wiring — the composition root is allowed to import modules
# (entrypoints is the top layer of the import DAG).
from sentinelai.modules.case_management import events as cm_events
from sentinelai.modules.case_management.router import router as cm_router
from sentinelai.modules.case_management.service import provide_case_access_checker
from sentinelai.modules.forensics import events as forensics_events
from sentinelai.modules.forensics.router import router as forensics_router
from sentinelai.modules.ingestion import events as ingestion_events
from sentinelai.modules.ingestion.router import router as ingestion_router
from sentinelai.modules.investigation import events as investigation_events
from sentinelai.modules.investigation.router import router as investigation_router
from sentinelai.modules.notification import events as notification_events
from sentinelai.modules.notification.router import router as notification_router
from sentinelai.modules.osint import events as osint_events
from sentinelai.modules.osint.router import router as osint_router
from sentinelai.modules.social_media import events as social_media_events
from sentinelai.modules.social_media.router import router as social_media_router
from sentinelai.modules.threat_intel import events as threat_intel_events
from sentinelai.modules.threat_intel.router import router as threat_intel_router
from sentinelai.platform.auth.dependencies import get_case_access_checker
from sentinelai.platform.config import settings
from sentinelai.platform.crypto import HealthState, KmsUnavailable, create_kms
from sentinelai.platform.db.session import async_session_factory, dispose_engine, engine
from sentinelai.platform.events.dispatcher import EventDispatcher
from sentinelai.platform.logging import configure_logging, log
from sentinelai.platform.migrations.currency import check_migrations_current
from sentinelai.platform.security.scanner import build_malware_scanner
from sentinelai.platform.storage import build_object_storage

# Registered in database-design.md §5 DAG order.
_MODULE_ROUTERS = (
    ingestion_router,
    osint_router,
    threat_intel_router,
    forensics_router,
    social_media_router,
    cm_router,
    investigation_router,
    notification_router,
)
_CONSUMER_REGISTRARS = (
    ingestion_events.register_consumers,
    osint_events.register_consumers,
    threat_intel_events.register_consumers,
    forensics_events.register_consumers,
    social_media_events.register_consumers,
    cm_events.register_consumers,
    investigation_events.register_consumers,
    notification_events.register_consumers,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the event dispatcher on startup; drain it and dispose the pool on shutdown."""
    settings.validate_for_profile()  # fail closed on misconfig BEFORE opening any connection
    configure_logging(settings.log_level, json_logs=settings.app_env != "development")
    log.info("http_startup", version=__version__, env=settings.app_env)

    dispatcher = EventDispatcher(async_session_factory)
    for register in _CONSUMER_REGISTRARS:
        register(dispatcher)
    dispatcher_task = asyncio.create_task(dispatcher.run_forever())
    app.state.dispatcher = dispatcher

    # arq pool for enqueuing background jobs (report generation, etc.). Best-effort:
    # if Redis is unavailable at startup the app still serves; endpoints that enqueue
    # will surface the error when actually called.
    try:
        app.state.task_queue = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    except Exception:  # pragma: no cover - environment-dependent
        app.state.task_queue = None
        log.warning("task_queue_unavailable_at_startup")

    # Object storage (ADR-0008) + the §25 malware scanner: built once for the process.
    # Construction is local (a session + credentials) and opens no connection, so it cannot fail
    # on an unreachable endpoint; errors surface on the paths that actually use them.
    app.state.object_storage = build_object_storage(settings)
    app.state.malware_scanner = build_malware_scanner(settings)

    # Bucket bootstrap (ADR-0008 §2, wave-1 W1-07): both buckets must exist before an upload is
    # reserved into quarantine or an object is promoted. `ensure_bucket` is idempotent. Fails
    # CLOSED in production — same posture as the KMS check below; outside production the process
    # still serves, but `/startupz` keeps reporting 503 so the degraded start is never silent.
    buckets_ready = False
    try:
        for bucket in (settings.storage_quarantine_bucket, settings.storage_bucket):
            await app.state.object_storage.ensure_bucket(bucket)
        buckets_ready = True
    except Exception as exc:
        log.error("object_storage_bucket_bootstrap_failed", error=type(exc).__name__)
        if settings.is_production:
            raise

    # KMS (ADR-0009): construct + verify reachability. Fail CLOSED in production — a signing
    # subsystem that cannot reach its keys must not serve requests that would need to sign.
    app.state.kms = create_kms(settings)
    await app.state.kms.start()  # begins provider background work (e.g. Vault lease renewal)
    kms_health = await app.state.kms.health()
    if kms_health.state == HealthState.UNAVAILABLE:
        log.error("kms_unavailable_at_startup", detail=kms_health.detail)
        if settings.is_production:
            raise KmsUnavailable(f"KMS unavailable at startup: {kms_health.detail}")

    # Migration currency (wave-1 §9, W1-11): serving against a schema the code does not match is
    # a correctness hazard, so it gates `/startupz`. Never fatal — migrations are applied by the
    # ArgoCD PreSync job, and during a rolling deploy a new pod can legitimately start moments
    # before that job finishes; failing the startup probe holds traffic until it catches up.
    migrations_current = False
    try:
        migration_status = await check_migrations_current(engine)
        migrations_current = migration_status.is_current
        if not migrations_current:
            log.error("database_schema_stale", schemas=migration_status.summary())
    except Exception as exc:
        log.error("migration_currency_check_failed", error=type(exc).__name__)

    # Startup gate (wave-1 W1-11): `/startupz` reports 503 until every critical step actually
    # succeeded. Reaching this line means config validated and the process is running; the gate
    # additionally requires KMS reachability, the buckets, and a current schema, so a degraded
    # start is visible to Kubernetes rather than silently admitted.
    kms_started = kms_health.state != HealthState.UNAVAILABLE
    app.state.startup = {
        "config_validated": True,
        "kms_started": kms_started,
        "buckets_ready": buckets_ready,
        "migrations_current": migrations_current,
        "complete": kms_started and buckets_ready and migrations_current,
    }
    log.info("http_startup_complete", **app.state.startup)

    try:
        yield
    finally:
        dispatcher.request_shutdown()
        await dispatcher_task  # drains in-flight handler invocations before exit
        if getattr(app.state, "task_queue", None) is not None:
            await app.state.task_queue.aclose()
        if getattr(app.state, "kms", None) is not None:
            await app.state.kms.aclose()
        await dispose_engine()
        log.info("http_shutdown")


def create_app() -> FastAPI:
    """Build and fully wire the FastAPI application."""
    app = FastAPI(
        title="SentinelAI API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    register_middleware(app)
    register_exception_handlers(app)

    # Well-known infra endpoints (unversioned).
    app.include_router(health.router)

    # Domain module routers (each carries its own /api/v1 prefix).
    for module_router in _MODULE_ROUTERS:
        app.include_router(module_router)

    # case_management supplies the CaseAccessChecker adapter for platform's ABAC port.
    app.dependency_overrides[get_case_access_checker] = provide_case_access_checker

    # Prometheus metrics at /metrics (network-restricted — api-design.md §12).
    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

    return app


app = create_app()
