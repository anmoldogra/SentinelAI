"""Worker entrypoint — arq background-job runtime (guide Part 12).

The second process of the one deployable. Background jobs whose state IS a database
row (``correlation_runs``, ``case_reports``) run here; the queue is only the
execution mechanism. Job functions are contributed by domain modules as they land
(Phase 3+ of the roadmap); the ``functions`` list below is wired up per module.

The in-process ``EventDispatcher`` runs in the HTTP process (see entrypoints/http),
not here, to avoid double-dispatch in Phase 1.
"""

from __future__ import annotations

from typing import Any, ClassVar

from arq.connections import RedisSettings

from sentinelai.modules.case_management.jobs import generate_case_report
from sentinelai.modules.forensics.jobs import process_artifact
from sentinelai.modules.ingestion.jobs import scan_uploaded_evidence
from sentinelai.modules.investigation.jobs import run_correlation
from sentinelai.modules.threat_intel.jobs import sync_feed_subscription
from sentinelai.platform.config import settings
from sentinelai.platform.crypto import create_kms
from sentinelai.platform.db.session import async_session_factory, dispose_engine, engine
from sentinelai.platform.logging import configure_logging, log
from sentinelai.platform.security.scanner import build_malware_scanner
from sentinelai.platform.storage import build_object_storage


async def on_startup(ctx: dict[str, Any]) -> None:
    """Configure logging and share the engine/session factory with job functions."""
    settings.validate_for_profile()  # fail closed on misconfig BEFORE opening any connection
    configure_logging(settings.log_level, json_logs=settings.app_env != "development")
    ctx["engine"] = engine
    ctx["session_factory"] = async_session_factory
    ctx["kms"] = create_kms(settings)  # ADR-0009: jobs sign/verify/encrypt via the KMS facade
    await ctx["kms"].start()
    # ADR-0008: object storage + the §25 malware scanner, built once per worker process.
    ctx["object_storage"] = build_object_storage(settings)
    ctx["malware_scanner"] = build_malware_scanner(settings)
    log.info("worker_startup", env=settings.app_env)


async def on_shutdown(ctx: dict[str, Any]) -> None:
    """Dispose the database connection pool + KMS resources on graceful shutdown."""
    kms = ctx.get("kms")
    if kms is not None:
        await kms.aclose()
    await dispose_engine()
    log.info("worker_shutdown")


class WorkerSettings:
    """arq worker configuration (guide Part 12 "Retries & Progress")."""

    functions: ClassVar[list[Any]] = [
        scan_uploaded_evidence,
        generate_case_report,
        sync_feed_subscription,
        process_artifact,
        run_correlation,
    ]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = on_startup
    on_shutdown = on_shutdown
    max_tries = 5  # mirrors event-driven-architecture.md §14's "Standard" policy
    job_timeout = 600
