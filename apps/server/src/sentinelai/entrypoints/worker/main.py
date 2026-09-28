"""Worker entrypoint — arq background-job runtime (guide Part 12).

The second process of the one deployable. Background jobs whose state IS a database
row (``correlation_runs``, ``case_reports``) run here; the queue is only the
execution mechanism. Job functions are contributed by domain modules as they land
(Phase 3+ of the roadmap); the ``functions`` list below is wired up per module.

The outbox relay (``EventDispatcher``) runs **here** as of Wave 2.2 (ADR-0006 §1). It used to run
in the HTTP process, where every API replica polled the same outbox tables independently. It now
runs in this process, claims rows with ``FOR UPDATE SKIP LOCKED``, and preserves per-aggregate
ordering — so several worker replicas can run safely side by side.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings

from sentinelai.entrypoints.consumers import register_all
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
from sentinelai.platform.events.dispatcher import EventDispatcher
from sentinelai.platform.events.signing import EventSigner
from sentinelai.platform.idempotency import purge_expired_idempotency_keys
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

    # ADR-0006 §1: the outbox relay lives here now. Started as a task rather than awaited, so arq
    # goes on to serve jobs; `on_shutdown` drains it.
    # ADR-0007 §2: the relay verifies every event before a handler sees it, and signs events that
    # handlers publish in turn. Both need the process KMS, which is why this is built here rather
    # than inside the dispatcher.
    dispatcher = register_all(
        EventDispatcher(
            async_session_factory,
            signer=EventSigner(ctx["kms"]),
            signature_mode=settings.events_signature_mode,
        )
    )
    ctx["dispatcher"] = dispatcher
    ctx["dispatcher_task"] = asyncio.create_task(dispatcher.run_forever())

    log.info("worker_startup", env=settings.app_env)


async def on_shutdown(ctx: dict[str, Any]) -> None:
    """Drain the relay, then dispose the pool and KMS resources on graceful shutdown."""
    # Drained BEFORE the engine is disposed: the relay holds sessions, and tearing the pool out from
    # under an in-flight handler would abort it mid-transaction — event-driven §2.2 requires the
    # current drain to finish instead.
    dispatcher = ctx.get("dispatcher")
    task = ctx.get("dispatcher_task")
    if dispatcher is not None:
        dispatcher.request_shutdown()
    if task is not None:
        await task

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
        # ADR-0012 §3: sweep expired idempotency records. Daily rather than hourly — the read path
        # already ignores an expired row (and deletes it when a client reuses that key), so this is
        # about table and index size, not correctness, and a missed run costs nothing but disk.
        #
        # 03:10, off every other schedule here: it is the one job that takes a table-wide DELETE,
        # and there is no reason for that to overlap the anchor cutter's reads.
        cron(
            purge_expired_idempotency_keys,
            hour={3},
            minute=10,
            run_at_startup=False,
            max_tries=2,
        ),
    ]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = on_startup
    on_shutdown = on_shutdown
    max_tries = 5  # mirrors event-driven-architecture.md §14's "Standard" policy
    job_timeout = 600
