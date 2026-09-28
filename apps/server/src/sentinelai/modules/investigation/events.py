"""investigation event wiring — event-driven-architecture.md §25.8.

Published: correlation run/finding lifecycle facts (emitted from ``service.py`` /
the correlation job). Consumed: four upstream events. Every handler performs the
Inbox claim BEFORE any side effect (§17) — that dedup is fully implemented here.

**ADR-0013 (Wave 4.1) added two real consumers**: ``investigation.correlation_generated`` and
``investigation.finding_reviewed`` are consumed by this module's own graph projectors. A module
consuming its own published events is deliberate — routing the projection through the outbox is what
makes it rebuildable by replay instead of a side effect welded to the write path.

Phase-1 note: the correlation *side-effects* these handlers describe (index evidence
for correlation candidacy, mark case-evidence eligibility, consider an IOC match) are
deferred no-ops — cross-domain correlation is AI-driven and not yet built, and §3.5
defines no correlation-candidacy or case↔evidence-eligibility table to record into.
The (future) correlation job reads eligible evidence live. Handlers still claim +
mark the inbox so redelivery is absorbed and real events are not dead-lettered.
"""

from __future__ import annotations

from sentinelai.modules.investigation.read.projector import (
    project_correlation_generated,
    project_finding_reviewed,
)
from sentinelai.modules.investigation.repository import InvestigationUnitOfWork
from sentinelai.platform.events.dispatcher import EventDispatcher
from sentinelai.platform.events.envelope import EventEnvelope
from sentinelai.platform.events.inbox import InboxGuard

SCHEMA = "investigation"

# Published (§25.8).
EVENT_CORRELATION_RUN_COMPLETED = "investigation.correlation_run_completed"
EVENT_CORRELATION_RUN_FAILED = "investigation.correlation_run_failed"
EVENT_CORRELATION_GENERATED = "investigation.correlation_generated"
EVENT_FINDING_REVIEWED = "investigation.finding_reviewed"

# Consumed (§25.8).
EVENT_EVIDENCE_INGESTED = "evidence.ingested"
EVENT_EVIDENCE_LINKED_TO_CASE = "evidence.linked_to_case"
EVENT_EVIDENCE_UNLINKED_FROM_CASE = "evidence.unlinked_from_case"
EVENT_IOC_MATCHED = "threat_intel.ioc_matched"

# ADR-0013's projection handlers. Investigation consuming its own published events is deliberate
# and not a loop: the publisher writes the transactional row, the projector builds the read model
# from the committed fact, and routing that through the outbox is what makes the projection
# rebuildable by replay (§2) rather than a side effect of the write path.
_H_PROJECT_CORRELATION = "investigation.project_case_graph"
_H_PROJECT_REVIEW = "investigation.project_finding_review"

_H_INDEX = "investigation.index_evidence"
_H_ELIGIBLE = "investigation.mark_eligible"
_H_INELIGIBLE = "investigation.mark_ineligible"
_H_IOC = "investigation.consider_ioc_match"


async def _claim_and_ack(event: EventEnvelope, uow: InvestigationUnitOfWork, handler: str) -> bool:
    """Inbox claim + mark-processed. Returns False on redelivery (already handled)."""
    guard = InboxGuard(uow.session, schema=SCHEMA)
    if not await guard.try_claim(event.event_id, handler_name=handler):
        return False
    # (Phase-1 correlation side-effect deferred — see module docstring.)
    await guard.mark_processed(event.event_id, handler_name=handler)
    return True


async def on_evidence_ingested(event: EventEnvelope, uow: InvestigationUnitOfWork) -> None:
    """Index new evidence for correlation candidacy (deferred no-op; dedup real)."""
    await _claim_and_ack(event, uow, _H_INDEX)


async def on_evidence_linked_to_case(event: EventEnvelope, uow: InvestigationUnitOfWork) -> None:
    """Mark evidence eligible for this case's correlation runs (deferred no-op; dedup real)."""
    await _claim_and_ack(event, uow, _H_ELIGIBLE)


async def on_evidence_unlinked_from_case(
    event: EventEnvelope, uow: InvestigationUnitOfWork
) -> None:
    """Mark evidence ineligible (deferred no-op; dedup real)."""
    await _claim_and_ack(event, uow, _H_INELIGIBLE)


async def on_ioc_matched(event: EventEnvelope, uow: InvestigationUnitOfWork) -> None:
    """Consider the IOC match as correlation input (deferred no-op; dedup real)."""
    await _claim_and_ack(event, uow, _H_IOC)


async def on_correlation_generated(event: EventEnvelope, uow: InvestigationUnitOfWork) -> None:
    """Project a generated relationship into its case's graph (ADR-0013).

    Unlike the deferred no-op handlers above, this one has a **real** side effect, so the inbox
    claim is load-bearing rather than merely tidy: without it a redelivery would re-run the
    projection writes. Those writes are themselves upserts and would converge anyway — that
    redundancy is deliberate (see ``read/projector.py``), not a reason to drop the claim.
    """
    guard = InboxGuard(uow.session, schema=SCHEMA)
    if not await guard.try_claim(event.event_id, handler_name=_H_PROJECT_CORRELATION):
        return
    await project_correlation_generated(event, uow)
    await guard.mark_processed(event.event_id, handler_name=_H_PROJECT_CORRELATION)


async def on_finding_reviewed(event: EventEnvelope, uow: InvestigationUnitOfWork) -> None:
    """Fold a disposition into every case graph showing the relationship (ADR-0013)."""
    guard = InboxGuard(uow.session, schema=SCHEMA)
    if not await guard.try_claim(event.event_id, handler_name=_H_PROJECT_REVIEW):
        return
    await project_finding_reviewed(event, uow)
    await guard.mark_processed(event.event_id, handler_name=_H_PROJECT_REVIEW)


def register_consumers(dispatcher: EventDispatcher) -> None:
    # ADR-0013's projectors first: they are the only handlers here with a real side effect, and
    # registering them beside the deferred no-ops makes the difference visible in one place.
    dispatcher.register(
        EVENT_CORRELATION_GENERATED,
        on_correlation_generated,
        inbox_schema=SCHEMA,
        uow_factory=InvestigationUnitOfWork,
    )
    dispatcher.register(
        EVENT_FINDING_REVIEWED,
        on_finding_reviewed,
        inbox_schema=SCHEMA,
        uow_factory=InvestigationUnitOfWork,
    )
    dispatcher.register(
        EVENT_EVIDENCE_INGESTED,
        on_evidence_ingested,
        inbox_schema=SCHEMA,
        uow_factory=InvestigationUnitOfWork,
    )
    dispatcher.register(
        EVENT_EVIDENCE_LINKED_TO_CASE,
        on_evidence_linked_to_case,
        inbox_schema=SCHEMA,
        uow_factory=InvestigationUnitOfWork,
    )
    dispatcher.register(
        EVENT_EVIDENCE_UNLINKED_FROM_CASE,
        on_evidence_unlinked_from_case,
        inbox_schema=SCHEMA,
        uow_factory=InvestigationUnitOfWork,
    )
    dispatcher.register(
        EVENT_IOC_MATCHED,
        on_ioc_matched,
        inbox_schema=SCHEMA,
        uow_factory=InvestigationUnitOfWork,
    )
