"""case_management background jobs — arq (guide Part 12).

Report generation is async: ``POST /cases/{id}/reports`` creates the ``case_reports`` row in
``queued`` state and enqueues this job; that row IS the job's state (api-design.md §2.12/§7), so
the client polls ``GET /reports/{report_id}`` rather than the queue. On completion the job
publishes ``case.report_generated`` via the module outbox, which ``notification`` consumes to
alert the requester (§25.7/§25.9).

A thin composition root: it resolves the session/storage from the worker context and delegates —
all business logic lives in ``CaseService`` (guide Part 5). Registered in the worker's
``WorkerSettings.functions``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sentinelai.modules.case_management.repository import CaseManagementUnitOfWork
from sentinelai.modules.case_management.service import CaseService
from sentinelai.platform.logging import log
from sentinelai.platform.storage import build_object_storage


async def generate_case_report(ctx: dict[str, Any], case_id: UUID, report_id: UUID) -> None:
    """Render the case report, store it, and mark the row completed.

    Retry-safe: arq retries this job and ``CaseService.complete_report`` is idempotent — an
    already-completed report returns untouched, so a redelivery neither re-uploads the object nor
    re-publishes the event.

    On failure the transaction is rolled back and the row is marked ``failed`` in a **separate**
    transaction, so the reason survives for a client polling the row; the exception is then
    re-raised so arq can retry (a later attempt flips it back to ``completed``).
    """
    session_factory = ctx["session_factory"]
    storage = ctx.get("object_storage") or build_object_storage()

    async with session_factory() as session:
        uow = CaseManagementUnitOfWork(session)
        service = CaseService(uow, storage=storage)
        try:
            await service.complete_report(
                report_id, storage, correlation_id=str(ctx.get("job_id") or case_id)
            )
            await uow.commit()  # ADR-0005: the entrypoint (this job wrapper) owns the transaction
            return
        except Exception as exc:
            await uow.rollback()
            reason = type(exc).__name__
            log.warning("case_report_generation_failed", report_id=str(report_id), error=reason)

    # Fresh transaction: the one above is dead, and the failure must be visible to a poller.
    async with session_factory() as session:
        failure_uow = CaseManagementUnitOfWork(session)
        await CaseService(failure_uow, storage=storage).fail_report(report_id, reason)
        await failure_uow.commit()
    raise RuntimeError(f"case report {report_id} failed to generate: {reason}")
