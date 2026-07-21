"""Worker entrypoint — arq background-job runtime (guide Part 12).

The second process of the one deployable. Background jobs whose state IS a database
row (``correlation_runs``, ``case_reports``) run here; the queue is only the
execution mechanism. Job functions are contributed by domain modules as they land
(Phase 3+ of the roadmap); the ``functions`` list below is wired up per module.

The in-process ``EventDispatcher`` runs in the HTTP process (see entrypoints/http),
not here, to avoid double-dispatch in Phase 1.
"""

from __future__ import annotations

from typing import Any

from arq.connections import RedisSettings

from sentinelai.modules.case_management.jobs import generate_case_report
from sentinelai.modules.forensics.jobs import process_artifact
from sentinelai.modules.ingestion.jobs import scan_uploaded_evidence
from sentinelai.modules.investigation.jobs import run_correlation
from sentinelai.modules.threat_intel.jobs import sync_feed_subscription
from sentinelai.platform.config import settings
from sentinelai.platform.db.session import async_session_factory, dispose_engine, engine
from sentinelai.platform.logging import configure_logging, log


async def on_startup(ctx: dict[str, Any]) -> None:
    """Configure logging and share the engine/session factory with job functions."""
    configure_logging(settings.log_level, json_logs=settings.app_env != "development")
    ctx["engine"] = engine
    ctx["session_factory"] = async_session_factory
    log.info("worker_startup", env=settings.app_env)


async def on_shutdown(ctx: dict[str, Any]) -> None:
    """Dispose the database connection pool on graceful shutdown."""
    await dispose_engine()
    log.info("worker_shutdown")


class WorkerSettings:
    """arq worker configuration (guide Part 12 "Retries & Progress")."""

    functions: list[Any] = [
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
