"""investigation background jobs — arq (guide Part 12).

The correlation run's ``correlation_runs`` row IS the job's state: `POST
/cases/{case_id}/correlation-runs` creates it ``queued`` and enqueues this job, and the client polls
`GET /correlation-runs/{run_id}` rather than the queue (api-design.md §2.12/§6). A long run updates
its count incrementally and checks ``cancellation_requested`` at batch boundaries (cooperative
cancellation), so a poller sees real interim state instead of a stale ``running``.

A thin composition root: it resolves the session and the extractor from the worker context, owns the
**transaction boundaries** (ADR-0005 — the entrypoint commits, never the service), and delegates
every decision to `CorrelationService`. Registered in the worker's ``WorkerSettings.functions``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sentinelai.modules.investigation.correlation import CorrelationService, publish_run_finished
from sentinelai.modules.investigation.extraction import (
    EvidenceExtractor,
    HeuristicIdentifierExtractor,
)
from sentinelai.modules.investigation.repository import InvestigationUnitOfWork
from sentinelai.platform.logging import log


def build_extractor(ctx: dict[str, Any]) -> EvidenceExtractor:
    """The extraction adapter for this run.

    Read off the worker context when one is provided, so a deployment that wires a model-backed
    adapter in ``on_startup`` needs no change here, and so an integration test can substitute one.
    Defaults to the deterministic `HeuristicIdentifierExtractor`, which is what exists today.
    """
    extractor: EvidenceExtractor | None = ctx.get("evidence_extractor")
    return extractor if extractor is not None else HeuristicIdentifierExtractor()


async def run_correlation(
    ctx: dict[str, Any],
    run_id: UUID,
    correlation_id: str | None = None,
    evidence_ids: Sequence[UUID] | None = None,
) -> None:
    """Execute a cross-domain correlation run for a case — CEM §10, event-driven §25.8.

    **Transaction shape, and why it is not one transaction.** The claim (``queued`` -> ``running``)
    commits on its own so a poller can see the run start; each batch of evidence commits with the
    findings it produced *and* the progress count that describes them (§16 — the outbox row for
    every finding is in that same transaction); the terminal status commits with
    ``correlation_run_completed``/``_failed``. One long transaction would make every intermediate
    state invisible and would discard an hour of correct findings over a failure in the last batch.

    **Retry-safe.** `CorrelationService.execute` returns ``None`` for an already-``completed`` run,
    so an arq retry or a re-enqueue after a worker restart neither re-walks the case nor
    re-publishes a single event. A ``failed`` run *is* re-claimed, because that is what a retry is
    for.

    **On failure** the transaction is rolled back and the row is marked ``failed`` in a **separate**
    transaction, so the outcome survives for a client polling it; the exception is then re-raised so
    arq can retry, and a later attempt flips the row to ``completed``. That publishes
    ``correlation_run_failed`` once per failed attempt, which is truthful — each attempt did fail —
    and harmless today, since §25.9 registers no `notification` consumer for it.

    ``correlation_id`` is threaded from the triggering request so §11's causal chain holds across
    the queue hop; ``None`` (an operator's manual enqueue) falls back to the run id, which is still
    a stable identifier for the workflow rather than a fresh one per event.

    ``evidence_ids`` carries §6's optional ``scope``. It is a job argument rather than a column
    because `database-design.md` §3.5 gives `correlation_runs` nowhere to store it and §6's poll
    response does not echo it — and because arq preserves a job's arguments across retries, so a
    re-attempt correlates the same narrowed set rather than quietly widening to the whole case.
    """
    session_factory = ctx["session_factory"]
    # One KMS per worker process (entrypoints/worker/main.py). The run signs nothing itself, but
    # every event it publishes is signed by the outbox writer on the UoW (ADR-0007 §1).
    kms = ctx["kms"]
    workflow_id = correlation_id or str(run_id)
    extractor = build_extractor(ctx)

    async with session_factory() as session:
        uow = InvestigationUnitOfWork(session, kms=kms)
        service = CorrelationService(uow, extractor=extractor)
        try:
            outcome = await service.execute(
                run_id,
                correlation_id=workflow_id,
                checkpoint=uow.commit,
                evidence_ids=evidence_ids,
            )
            if outcome is None:
                # Nothing to do: the run is gone or already finished. Committing is still right —
                # `execute` wrote nothing, and a rollback would be equally correct but noisier.
                await uow.commit()
                return
            run = await uow.correlation_runs.get_by_id(run_id)
            if run is not None:
                # A cancelled run is `failed`, not `completed`. api-design.md §6's status enum has
                # no `cancelled` value, and of the two available answers `completed` is the wrong
                # one: a client reading it would believe the case had been correlated in full. The
                # findings it did make are kept — they are grounded and valid — and the distinction
                # is in the `correlation_run_cancelled` log line.
                run.finish(datetime.now(UTC), failed=outcome.cancelled)
                await publish_run_finished(
                    uow, run, correlation_id=workflow_id, failed=outcome.cancelled
                )
            await uow.commit()
            log.info(
                "correlation_run_finished",
                run_id=str(run_id),
                case_id=str(outcome.case_id),
                findings=outcome.findings_generated,
                evidence_considered=outcome.evidence_considered,
                cancelled=outcome.cancelled,
            )
            return
        except Exception as exc:
            await uow.rollback()
            reason = type(exc).__name__
            log.warning("correlation_run_failed", run_id=str(run_id), error=reason)

    # Fresh transaction: the one above is dead, and the failure must be visible to a poller.
    async with session_factory() as session:
        failure_uow = InvestigationUnitOfWork(session, kms=kms)
        run = await failure_uow.correlation_runs.get_by_id(run_id)
        if run is not None:
            run.finish(datetime.now(UTC), failed=True)
            await publish_run_finished(failure_uow, run, correlation_id=workflow_id, failed=True)
        await failure_uow.commit()
    raise RuntimeError(f"correlation run {run_id} failed: {reason}")


__all__ = ["build_extractor", "run_correlation"]
