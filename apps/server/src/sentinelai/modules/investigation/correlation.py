"""The correlation engine — the write half of Phase 3's AI investigation layer.

**What a correlation run is.** api-design.md §6 defines it as "an AI correlation pass over a case's
linked evidence"; `database-design.md` §3.5 gives it a `correlation_runs` row that *is* its state
(guide Part 12); CEM §10 says what it should find and — the rule everything here is arranged
around — that every output is born ``status: proposed``, never a confirmed fact (PRD FR-7.3).

**Three boundaries this module does not cross.**

* It reads a case's evidence set through `case_management.public` and that evidence's content
  through `ingestion.public`, never by querying either schema. `database-design.md` §5 forbids the
  cross-schema join, and `investigation` is the one module allowed to read across domains — through
  their public interfaces (event-driven §25.8's "a consumer that needs more fetches it").
* It writes only `investigation`'s own tables, and announces every finding through the module's
  outbox in the **same transaction** as the write (§16). The graph an analyst actually reads is
  built by the projector from those announcements (ADR-0013), not by this module writing
  `investigation_read` directly.
* It decides nothing about *what* an identifier means. That is `extraction.py`'s adapter behind the
  ``EvidenceExtractor`` port, so the day a model-backed adapter arrives this file does not change.

**Why the findings converge instead of multiplying.** Running the same case twice must not double
its graph. Three resolutions make a re-run a no-op: an entity is resolved by
``(entity_type, canonical_name)`` before it is inserted, a MENTIONS edge by its
``(entity_id, evidence_id)`` pair (``uq_entity_mention_pair``), and an ``associated_with`` edge by
its unordered endpoint pair. So a second run over unchanged evidence creates nothing, publishes
nothing, and reports ``findings_generated_count = 0`` — the correct answer, not a failure.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from sentinelai.modules.case_management.public import CaseEvidenceScope, read_case_evidence_scope
from sentinelai.modules.ingestion.public import EvidenceContent, read_evidence_content
from sentinelai.modules.investigation.events import (
    CREATED_BY_MACHINE,
    EVENT_CORRELATION_RUN_COMPLETED,
    EVENT_CORRELATION_RUN_FAILED,
    FindingKind,
    publish_finding,
)
from sentinelai.modules.investigation.extraction import (
    MAX_ENTITIES_PER_EVIDENCE,
    EvidenceExtractor,
    EvidenceRecord,
    validate_extraction,
)
from sentinelai.modules.investigation.models import (
    STATUS_PROPOSED,
    CorrelationRun,
    Entity,
    EntityEvidenceMention,
    Relationship,
    RelationshipEvidence,
)
from sentinelai.modules.investigation.repository import InvestigationUnitOfWork
from sentinelai.platform.logging import log
from sentinelai.shared.exceptions import ValidationFailedError

# Which evidence lifecycle states a correlation run may read — **this module's policy, not
# `ingestion`'s.** `validated` alone, and each exclusion is deliberate:
#
# * `quarantined` is what a malware detection or a custody-chain gap sets (CEM §13,
#   security-architecture §25). Extracting entities from it would put findings grounded in
#   untrustworthy evidence in front of an analyst with nothing saying so.
# * `superseded` has a replacement (CEM §12); correlating the stale copy would ground a finding in a
#   record the investigation has already corrected.
# * `tombstoned` is gone.
# * `pending_validation` has not passed CEM §13 yet. Nothing today leaves evidence in that state —
#   `ingest_evidence` writes `validated` — so this is a guard against a future intake path rather
#   than a live filter, and it is cheap to hold.
ELIGIBLE_EVIDENCE_STATUSES: Final[frozenset[str]] = frozenset({"validated"})

# Evidence items per transaction. A run commits per batch so `GET /correlation-runs/{run_id}` shows
# real interim progress (guide Part 12) and so a failure at item 400 does not discard the findings
# from items 1-399 — those are correct, grounded, and an analyst can review them while the rest is
# retried. Small enough that a batch is a short transaction, large enough that a 5,000-item case is
# not 5,000 commits.
BATCH_SIZE: Final = 25

# §9's `aggregate_type` for the run lifecycle events. The run, not the case: §25.8's payload is
# keyed on `run_id`, and per-aggregate ordering in the dispatcher (ADR-0006) should serialize one
# run's events rather than every run a busy case ever had.
RUN_AGGREGATE: Final = "correlation_run"


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What a finished run did, for the caller that has to stamp the row and log it."""

    run_id: UUID
    case_id: UUID
    findings_generated: int
    evidence_considered: int
    cancelled: bool


CaseScopeReader = Callable[[UUID], Awaitable[CaseEvidenceScope | None]]
EvidenceContentReader = Callable[[Sequence[UUID]], Awaitable[Sequence[EvidenceContent]]]
# What the caller does at a batch boundary — in production, commit. Passed **in** rather than called
# on the UoW directly because ADR-0005 gives transaction ownership to the entrypoint: the service
# decides *where* a checkpoint is meaningful, the job decides *what one costs*. A unit test passes
# nothing and the whole run stays in one uncommitted transaction.
Checkpoint = Callable[[], Awaitable[None]]


def eligible(content: Sequence[EvidenceContent]) -> list[EvidenceContent]:
    """Drop evidence a correlation run must not read, and say so in the log.

    Logged rather than silent: "the run found nothing" and "the run was not allowed to look" are
    very different answers to an analyst who triggered a pass over a case they know holds evidence.
    """
    keep = [item for item in content if item.status in ELIGIBLE_EVIDENCE_STATUSES]
    skipped = len(content) - len(keep)
    if skipped:
        log.info(
            "correlation_evidence_skipped",
            skipped=skipped,
            considered=len(keep),
            reason="status outside ELIGIBLE_EVIDENCE_STATUSES",
        )
    return keep


def to_record(content: EvidenceContent) -> EvidenceRecord:
    """Narrow an evidence row to the content an extractor is allowed to see.

    A deliberate narrowing, not a copy for its own sake: `EvidenceRecord` carries no ``status``, so
    an adapter — including a future one that sends text to an inference endpoint — cannot re-decide
    the eligibility rule above, and holds no field it has no business transmitting.
    """
    return EvidenceRecord(
        evidence_id=content.evidence_id,
        category=content.category,
        artifact_type=content.artifact_type,
        title=content.title,
        description=content.description,
        attributes=content.attributes,
    )


async def publish_run_finished(
    uow: InvestigationUnitOfWork,
    run: CorrelationRun,
    *,
    correlation_id: str,
    failed: bool,
) -> None:
    """Announce that a run finished — §25.8's ``correlation_run_completed`` / ``_failed``.

    The payload is §25.8's three fields exactly. ``findings_generated_count`` is read off the row
    rather than passed in, so the number in the event and the number a client polling
    `GET /correlation-runs/{run_id}` sees are the same number by construction.

    ``causation_id`` is **omitted, honestly**: a run is started by an HTTP request (api-design.md §6
    publishes no event at trigger time), so there is no parent event to point one hop back at, and
    inventing one would give an auditor a causal link that leads nowhere. The ``correlation_id``
    still threads the whole workflow, which is what §11 actually requires of it.

    Published in the same transaction as the terminal status write (§16) — a run whose row says
    ``completed`` while no event says so, or the reverse, is the split-brain the outbox exists to
    prevent.
    """
    await uow.outbox.publish(
        event_type=EVENT_CORRELATION_RUN_FAILED if failed else EVENT_CORRELATION_RUN_COMPLETED,
        aggregate_type=RUN_AGGREGATE,
        aggregate_id=run.run_id,
        payload={
            "run_id": str(run.run_id),
            "case_id": str(run.case_id),
            "findings_generated_count": run.findings_generated_count,
        },
        correlation_id=correlation_id,
        # No `actor_ref`: a worker finished a job, no principal did it.
        actor_type="system",
    )


class CorrelationService:
    """Executes one correlation run: extract, persist as ``proposed``, announce, record progress.

    Constructed per run by `jobs.run_correlation`. It takes **no KMS and no object storage**: the
    run signs nothing itself (the outbox writer on the UoW holds the signer, ADR-0007) and reads no
    payload bytes — only the metadata `ingestion.public` hands over. A service demanding either
    would have to be built with ``None`` for a dependency it never touches.

    The two readers are injectable for the same reason `events.on_ioc_matched`'s are: the
    cross-module hooks are module-level functions over a session, so a unit test can drive a whole
    run with no database while the default path is the real public interface.
    """

    def __init__(
        self,
        uow: InvestigationUnitOfWork,
        *,
        extractor: EvidenceExtractor,
        read_scope: CaseScopeReader | None = None,
        read_content: EvidenceContentReader | None = None,
    ) -> None:
        self._uow = uow
        self._extractor = extractor
        self._read_scope = read_scope or (
            lambda case_id: read_case_evidence_scope(uow.session, case_id)
        )
        self._read_content = read_content or (lambda ids: read_evidence_content(uow.session, ids))

    async def execute(
        self,
        run_id: UUID,
        *,
        correlation_id: str,
        checkpoint: Checkpoint | None = None,
        evidence_ids: Sequence[UUID] | None = None,
    ) -> RunOutcome | None:
        """Run the correlation pass for ``run_id``. ``None`` if there is nothing to do.

        ``None`` for a run that no longer exists or has already completed — both are what a
        **redelivered arq job** looks like, and neither is an error. Re-walking a completed run
        would re-announce every finding it made, and §25.9 keys `notification`'s idempotency on the
        finding rather than on the run, so an analyst would be alerted twice about one fact.

        Commits are the **caller's**, per ADR-0005: ``checkpoint`` is invoked once per batch, after
        the batch's findings and the progress count that describes them are both on the session — so
        the transaction that makes a batch's findings visible makes its count visible with them, and
        a failure in batch nine does not discard batches one to eight.

        Raises rather than swallowing. The job wrapper marks the row ``failed``, publishes
        ``correlation_run_failed`` and lets arq retry; a service that caught its own failures would
        leave a run stuck ``running`` forever, with nothing to distinguish a crash from a slow pass.
        """
        run = await self._uow.correlation_runs.get_by_id(run_id)
        if run is None:
            log.warning("correlation_run_missing", run_id=str(run_id))
            return None
        if not run.claim(datetime.now(UTC)):
            log.info("correlation_run_already_finished", run_id=str(run_id), status=run.status)
            return None
        if checkpoint is not None:
            # The claim becomes durable before any extraction starts, so a client polling
            # `GET /correlation-runs/{run_id}` sees `running` while the pass is running rather than
            # `queued` until it ends. It is also what makes the claim a real claim: a second worker
            # reading the row after this commit finds `running`, not `queued`.
            await checkpoint()

        scope = await self._read_scope(run.case_id)
        if scope is None:
            # The case was deleted after the run was queued. Not a failure *of the run*: there is
            # nothing to correlate, and nothing was wrong with the request that asked for it.
            log.warning(
                "correlation_run_case_missing", run_id=str(run_id), case_id=str(run.case_id)
            )
            # The row's own count, not zero: on a retry of a run whose case was deleted after it
            # had already produced findings, reporting zero would contradict the row a poller reads.
            return RunOutcome(
                run_id, run.case_id, run.findings_generated_count, 0, cancelled=False
            )

        # The case's links are re-read here, not taken from the trigger: a run queued behind a
        # backlog may start minutes later, and evidence linked in the meantime belongs in the pass.
        # A scoped run intersects, so an item unlinked since the trigger is dropped rather than
        # read.
        wanted = scope.evidence_ids
        if evidence_ids is not None:
            permitted = set(scope.evidence_ids)
            wanted = tuple(evidence_id for evidence_id in evidence_ids if evidence_id in permitted)
        content = eligible(list(await self._read_content(wanted)))
        # **Seeded from the row, not from zero.** `findings_generated_count` is the run's cumulative
        # total, and a retry re-walks evidence whose findings a previous attempt already committed —
        # convergence means it creates nothing for those, so a counter starting at zero would hand
        # `record_progress` a number lower than the row's and the monotonic invariant would raise,
        # failing the run permanently on its second attempt.
        findings = run.findings_generated_count
        considered = 0
        for start in range(0, len(content), BATCH_SIZE):
            # Cooperative cancellation at a batch boundary (guide Part 12): between transactions,
            # never mid-write, so the database stays consistent and every finding already made
            # survives. Read from the row each time rather than off the instance — the flag is set
            # by somebody else while this run is in flight, which is the only way it can ever be
            # true.
            if await self._uow.correlation_runs.is_cancellation_requested(run_id):
                log.info(
                    "correlation_run_cancelled",
                    run_id=str(run_id),
                    considered=considered,
                    findings=findings,
                )
                return RunOutcome(run_id, run.case_id, findings, considered, cancelled=True)
            for item in content[start : start + BATCH_SIZE]:
                findings += await self._correlate_one(
                    item, scope=scope, run=run, correlation_id=correlation_id
                )
                considered += 1
            run.record_progress(findings)
            if checkpoint is not None:
                await checkpoint()

        return RunOutcome(run_id, run.case_id, findings, considered, cancelled=False)

    async def _correlate_one(
        self,
        content: EvidenceContent,
        *,
        scope: CaseEvidenceScope,
        run: CorrelationRun,
        correlation_id: str,
    ) -> int:
        """Extract from one evidence item and persist what is new. Returns findings created."""
        extraction = await self._extractor.extract(to_record(content))
        problems = validate_extraction(extraction)
        if problems:
            # A malformed extraction is the *extractor's* fault, and it fails the run loudly rather
            # than writing the well-formed half. A partially-persisted extraction would leave
            # co-occurrence edges referencing whichever entities happened to survive — a worse
            # record than no record, and one an analyst has no way to recognise as incomplete.
            raise ValidationFailedError(
                [
                    {"field": f"extraction.{content.evidence_id}", "message": problem}
                    for problem in problems
                ]
            )
        if len(extraction.entities) >= MAX_ENTITIES_PER_EVIDENCE:
            # At the cap. `_at_cap` rather than `_capped` because an item with exactly twelve
            # identifiers is indistinguishable from one that was truncated — the extractor returns a
            # tuple either way. Logged rather than silent because the run will still report
            # `completed`, and this is the only place that says the item may not have been read
            # exhaustively.
            log.info(
                "correlation_extraction_at_cap",
                evidence_id=str(content.evidence_id),
                cap=MAX_ENTITIES_PER_EVIDENCE,
            )
        if extraction.is_empty:
            return 0

        created = 0
        entities: list[Entity] = []
        for candidate in extraction.entities:
            entity, is_new = await self._resolve_entity(
                candidate.entity_type,
                candidate.canonical_name,
                confidence=candidate.confidence,
                run_id=run.run_id,
            )
            entities.append(entity)
            if is_new:
                created += 1
                await self._announce(
                    scope=scope,
                    correlation_id=correlation_id,
                    kind="entity",
                    finding_id=entity.entity_id,
                    confidence=candidate.confidence,
                    run_id=run.run_id,
                )
            # CEM §11's MENTIONS edge grounds the entity in *this* evidence item, and it is written
            # for an already-existing entity too: a node resolved from an earlier run or from an IOC
            # match is now also evidenced here, and that is the fact §1.6 requires. Guarded by the
            # pair check because `uq_entity_mention_pair` would otherwise abort the transaction.
            if not await self._uow.entity_mentions.exists_for_pair(
                entity_id=entity.entity_id, evidence_id=content.evidence_id
            ):
                await self._uow.entity_mentions.add(
                    EntityEvidenceMention(
                        entity_id=entity.entity_id, evidence_id=content.evidence_id
                    )
                )

        for edge in extraction.relationships:
            relationship = await self._resolve_relationship(
                rel_type=edge.rel_type,
                from_entity=entities[edge.from_index],
                to_entity=entities[edge.to_index],
                directional=edge.directional,
                confidence=edge.confidence,
                evidence_id=content.evidence_id,
                run_id=run.run_id,
            )
            if relationship is None:
                continue
            created += 1
            await self._announce(
                scope=scope,
                correlation_id=correlation_id,
                kind="relationship",
                finding_id=relationship.relationship_id,
                confidence=edge.confidence,
                run_id=run.run_id,
            )
        return created

    async def _resolve_entity(
        self, entity_type: str, canonical_name: str, *, confidence: Decimal, run_id: UUID
    ) -> tuple[Entity, bool]:
        """The existing entity for this ``(type, name)``, or a new ``proposed`` one.

        Entity resolution is what keeps a re-run from doubling the graph, and it is deliberately
        narrow: an exact match on the normalized canonical name, nothing fuzzy. CEM §10 lists
        "entity-resolution candidates (the same real-world entity referenced differently across
        sources)" as a *separate* extraction target for a reason — deciding that two differently
        spelled names are one person is an analyst's judgement, and a run that merged them silently
        would be asserting an identity nobody reviewed.

        ``created_by_ref`` is the run id: the one reference that answers "why is this node here"
        without a join, and an app-ref like every other cross-schema id (§5).
        """
        existing = await self._uow.entities.find_by_type_and_name(entity_type, canonical_name)
        if existing is not None:
            return existing, False
        entity = Entity(
            entity_type=entity_type,
            canonical_name=canonical_name,
            aliases=None,
            # Machine output is never born confirmed (PRD FR-7.3, CEM §10, ADR-0011 §1).
            status=STATUS_PROPOSED,
            confidence=confidence,
            created_by_type=CREATED_BY_MACHINE,
            created_by_ref=run_id,
        )
        await self._uow.entities.add(entity)
        return entity, True

    async def _resolve_relationship(
        self,
        *,
        rel_type: str,
        from_entity: Entity,
        to_entity: Entity,
        directional: bool,
        confidence: Decimal,
        evidence_id: UUID,
        run_id: UUID,
    ) -> Relationship | None:
        """A new ``proposed`` edge, or ``None`` if this pair is already related this way.

        ``None`` rather than a second edge, and rather than adding a supporting-evidence row to the
        existing one. The second would be defensible — more grounding for the same claim — but
        `relationship_evidence`'s composite primary key makes it an upsert this layer would have to
        reason about per pair, and §25.8 gives no event for "an existing finding gained evidence",
        so the fact would be unannounced and invisible in the projection. Recorded as a limitation
        rather than solved by inventing an event.
        """
        existing = await self._uow.relationships.find_between(
            rel_type=rel_type,
            first_entity_id=from_entity.entity_id,
            second_entity_id=to_entity.entity_id,
        )
        if existing is not None:
            return None
        supporting = (evidence_id,)
        # ADR-0011 §1: the aggregate owns CEM §13's ">= 1 supporting evidence" rule, checked where
        # the row is built rather than where it is saved.
        Relationship.assert_supporting_evidence(len(supporting))
        relationship = Relationship(
            type=rel_type,
            from_entity_id=from_entity.entity_id,
            to_entity_id=to_entity.entity_id,
            directional=directional,
            confidence=confidence,
            status=STATUS_PROPOSED,
            created_by_type=CREATED_BY_MACHINE,
            created_by_ref=run_id,
        )
        await self._uow.relationships.add(relationship)
        for supporting_evidence_id in supporting:
            await self._uow.relationship_evidence.add(
                RelationshipEvidence(
                    relationship_id=relationship.relationship_id,
                    evidence_id=supporting_evidence_id,
                )
            )
        return relationship

    async def _announce(
        self,
        *,
        scope: CaseEvidenceScope,
        correlation_id: str,
        kind: FindingKind,
        finding_id: UUID,
        confidence: Decimal,
        run_id: UUID,
    ) -> None:
        """Announce one finding for the case this run belongs to (§25.8).

        One case, unlike `events.on_ioc_matched`'s fan-out over every case holding the matched
        evidence: a run is *scoped to a case* by `correlation_runs.case_id`, so the case it
        announces for is the one that asked. The same entity turning up in another case's evidence
        is that case's own run to make, and announcing it here would put a finding in a graph nobody
        requested — with a different case's owner as `recipient_user_id`.

        ``generated_by`` names the extractor **and** the run: §25.8 calls for a "model/run
        reference", and an analyst reading a finding months later needs to know both which extractor
        proposed it and which pass produced it.
        """
        await publish_finding(
            self._uow,
            correlation_id=correlation_id,
            # No parent event: a run is triggered by a request, not by an event (api-design.md §6
            # publishes none at trigger time).
            causation_id=None,
            case_id=scope.case_id,
            recipient_user_id=scope.owning_user_id,
            confidence=confidence,
            generated_by=f"{self._extractor.name} run:{run_id}",
            kind=kind,
            finding_id=finding_id,
        )


__all__ = [
    "BATCH_SIZE",
    "ELIGIBLE_EVIDENCE_STATUSES",
    "RUN_AGGREGATE",
    "Checkpoint",
    "CorrelationService",
    "RunOutcome",
    "eligible",
    "publish_run_finished",
    "to_record",
]
