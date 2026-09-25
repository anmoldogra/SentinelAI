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

from arq import cron
from arq.connections import RedisSettings

from sentinelai.modules.case_management.jobs import generate_case_report
from sentinelai.modules.forensics.jobs import process_artifact
from sentinelai.modules.ingestion.anchor_jobs import cut_anchor_batches
from sentinelai.modules.ingestion.integrity_jobs import reverify_evidentiary_ledgers
from sentinelai.modules.ingestion.jobs import scan_uploaded_evidence
from sentinelai.modules.investigation.jobs import run_correlation
from sentinelai.modules.threat_intel.jobs import sync_feed_subscription
from sentinelai.platform.config import settings
from sentinelai.platform.crypto import create_kms
from sentinelai.platform.db.session import async_session_factory, dispose_engine, engine
from sentinelai.platform.logging import configure_logging, log
from sentinelai.platform.security.scanner import build_malware_scanner
from sentinelai.platform.storage import build_object_storage
from sentinelai.platform.storage.worm import WormMisconfigured, verify_worm_bucket


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
    # Settings go on the context so job functions read the same object the process validated,
    # rather than re-importing the module-level singleton and diverging under test.
    ctx["settings"] = settings

    # ADR-0003 §3 / deployment Part 7: refuse to run the anchor cutter against a bucket that cannot
    # hold COMPLIANCE-mode objects. This matters more in the worker than in the API, because the
    # worker is the process that actually writes anchors — a non-WORM bucket here means every
    # anchor it publishes is deletable by the insider anchoring exists to defend against, and
    # nothing in the data would ever reveal it.
    #
    # Fails CLOSED in production, matching the KMS and bucket-bootstrap posture in the HTTP
    # lifespan. Outside production it logs and continues, so a developer without a WORM-capable
    # MinIO can still run the worker — the anchor job itself fails loudly if it tries to publish.
    try:
        await verify_worm_bucket(ctx["object_storage"], settings.storage_anchor_bucket)
    except WormMisconfigured as exc:
        log.error(
            "worm_bucket_misconfigured", bucket=settings.storage_anchor_bucket, detail=str(exc)
        )
        if settings.is_production:
            raise
    except Exception as exc:  # unreachable endpoint, denied credentials - not a misconfiguration
        log.error("worm_bucket_check_failed", error=type(exc).__name__)
        if settings.is_production:
            raise

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

    # ADR-0003 §6(b): scheduled re-verification of both evidentiary ledgers. Hourly on the half
    # hour, off the top of the hour where most other scheduled work clusters.
    #
    # `run_at_startup=False` deliberately: a deploy rollout restarts workers, and verifying both
    # ledgers on every pod start would turn a routine rollout into a stampede of full-ledger reads.
    # `max_tries=1` because a verification verdict does not change on retry — if the run itself
    # failed (KMS down, database unreachable) the next scheduled firing is the right retry, and
    # retrying a *completed* run that found tampering would re-alarm on the same finding.
    cron_jobs: ClassVar[list[Any]] = [
        cron(reverify_evidentiary_ledgers, minute=30, run_at_startup=False, max_tries=1),
        # ADR-0003 §3: cut anchor batches every 4 hours, on the hour. Six runs a day bounds how long
        # a newly-written entry stays uncommitted (and therefore how much history a truncation could
        # reach) without turning the KMS into a bottleneck: each run costs exactly two signatures,
        # one per ledger, regardless of how many entries the batch covers.
        #
        # Offset from the verification job's :30 so a re-verification never races the cutter it is
        # checking the output of — the two are individually safe to interleave, but a report saying
        # "N unanchored" is easier to reason about when it is not sampled mid-cut.
        cron(
            cut_anchor_batches,
            hour={0, 4, 8, 12, 16, 20},
            minute=0,
            run_at_startup=False,
            max_tries=3,
        ),
    ]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = on_startup
    on_shutdown = on_shutdown
    max_tries = 5  # mirrors event-driven-architecture.md §14's "Standard" policy
    job_timeout = 600
