"""Unit tests for the correlation run — the state machine, the service, and the trigger's guards.

Three layers, deliberately separated:

* `CorrelationRun`'s state machine is an ADR-0011 aggregate, so it is tested with no database, no
  queue and no worker — a declarative instance is an ordinary Python object until it meets a
  session.
* `CorrelationService.execute` runs against the in-memory UoW with injected readers, which is what
  makes the *decisions* testable — convergence, eligibility, cancellation, announcement shape —
  without a Postgres round trip per case.
* `trigger_correlation_run` guards api-design.md §6's documented pre-conditions.

The whole thing end-to-end, through the real dispatcher and into the case graph, is
`tests/integration/test_correlation_run_db.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from sentinelai.modules.case_management.public import CaseEvidenceScope
from sentinelai.modules.ingestion.public import EvidenceContent
from sentinelai.modules.investigation import service as service_module
from sentinelai.modules.investigation.correlation import (
    BATCH_SIZE,
    CorrelationService,
    eligible,
    publish_run_finished,
    to_record,
)
from sentinelai.modules.investigation.exceptions import (
    CaseNotFoundError,
    CorrelationRunInProgressError,
)
from sentinelai.modules.investigation.extraction import (
    CO_OCCURRENCE_REL_TYPE,
    EvidenceRecord,
    ExtractedEntity,
    ExtractedRelationship,
    Extraction,
    HeuristicIdentifierExtractor,
)
from sentinelai.modules.investigation.models import (
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_QUEUED,
    RUN_RUNNING,
    CorrelationRun,
)
from sentinelai.modules.investigation.schemas import CorrelationScope
from sentinelai.modules.investigation.service import InvestigationService
from sentinelai.shared.exceptions import ValidationFailedError
from tests.fixtures.kms import kms_for_tests

_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
_LATER = datetime(2026, 9, 29, 13, 0, tzinfo=UTC)
_OWNER = uuid4()
# The outbox column is a real `uuid`, so a workflow id is one too.
_CORR = str(uuid4())


class _Queue:
    """Records what was enqueued, so the job's arguments are assertable."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> None:
        self.jobs.append((function, args))


class _FixedExtractor:
    """An extractor that returns what a test tells it to, keyed by evidence id."""

    name = "fixed-extractor/test"

    def __init__(self, by_evidence: dict[UUID, Extraction] | None = None) -> None:
        self._by_evidence = by_evidence or {}
        self.seen: list[EvidenceRecord] = []

    async def extract(self, record: EvidenceRecord) -> Extraction:
        self.seen.append(record)
        return self._by_evidence.get(record.evidence_id, Extraction())


def _run(case_id: UUID, **overrides: Any) -> CorrelationRun:
    fields: dict[str, Any] = {
        "run_id": uuid4(),
        "case_id": case_id,
        "status": RUN_QUEUED,
        "started_at": None,
        "completed_at": None,
        "findings_generated_count": 0,
        "cancellation_requested": False,
    }
    fields.update(overrides)
    return CorrelationRun(**fields)


def _content(evidence_id: UUID, **overrides: Any) -> EvidenceContent:
    fields: dict[str, Any] = {
        "evidence_id": evidence_id,
        "category": "digital_forensics",
        "artifact_type": "chat_message",
        "title": "Chat export",
        "description": None,
        "attributes": {},
        "collected_at": _NOW,
        "status": "validated",
    }
    fields.update(overrides)
    return EvidenceContent(**fields)


def _pair(first: str, second: str) -> Extraction:
    return Extraction(
        entities=(
            ExtractedEntity("digital_asset", first, Decimal("1.000")),
            ExtractedEntity("digital_asset", second, Decimal("1.000")),
        ),
        relationships=(
            ExtractedRelationship(CO_OCCURRENCE_REL_TYPE, 0, 1, False, Decimal("0.500")),
        ),
    )


def _service(
    uow: Any,
    *,
    scope: CaseEvidenceScope | None,
    content: list[EvidenceContent],
    extractor: Any = None,
) -> CorrelationService:
    return CorrelationService(
        uow,
        extractor=extractor or _FixedExtractor(),
        read_scope=lambda _case_id: _resolved(scope),
        read_content=lambda ids: _resolved([c for c in content if c.evidence_id in set(ids)]),
    )


async def _resolved(value: Any) -> Any:
    return value


# --- the aggregate's state machine (ADR-0011 §1) ----------------------------
def test_a_queued_run_is_claimed_and_starts_its_clock() -> None:
    run = _run(uuid4())

    assert run.claim(_NOW) is True
    assert run.status == RUN_RUNNING
    assert run.started_at == _NOW


def test_a_completed_run_is_not_reclaimed() -> None:
    """What a **redelivered arq job** looks like. Re-walking it would re-announce every finding it
    made, and §25.9 keys `notification` on the finding rather than the run — so the analyst would be
    alerted twice for one fact."""
    run = _run(uuid4(), status=RUN_COMPLETED, completed_at=_NOW)

    assert run.claim(_LATER) is False
    assert run.status == RUN_COMPLETED


def test_a_failed_run_is_reclaimable() -> None:
    """A failure is usually transient, and arq's next attempt is the retry. A run that could
    never be
    retried would turn every database blip into a case an analyst re-triggers by hand."""
    run = _run(uuid4(), status=RUN_FAILED, started_at=_NOW, completed_at=_NOW)

    assert run.claim(_LATER) is True
    assert run.status == RUN_RUNNING


def test_started_at_is_the_first_attempts_clock() -> None:
    """Overwriting it on a retry would make a run that has been failing for an hour look fresh to
    whoever is reading `GET /correlation-runs/{run_id}`."""
    run = _run(uuid4(), status=RUN_FAILED, started_at=_NOW)

    run.claim(_LATER)

    assert run.started_at == _NOW


def test_progress_is_monotonic() -> None:
    """A count going backwards would read as findings having been withdrawn, which never happens: a
    finding is rejected by review, never deleted."""
    run = _run(uuid4())
    run.record_progress(5)

    with pytest.raises(ValidationFailedError):
        run.record_progress(4)

    assert run.findings_generated_count == 5


@pytest.mark.parametrize(
    ("failed", "expected"), [(False, RUN_COMPLETED), (True, RUN_FAILED)], ids=["ok", "failed"]
)
def test_finishing_stamps_the_status_and_the_clock(failed: bool, expected: str) -> None:
    """``completed_at`` on both paths: it is how a poller tells "still going" from "over", which
    matters most on the failure path where nothing else says so."""
    run = _run(uuid4(), status=RUN_RUNNING, started_at=_NOW)

    run.finish(_LATER, failed=failed)

    assert (run.status, run.completed_at, run.is_finished) == (expected, _LATER, True)


# --- eligibility ------------------------------------------------------------
@pytest.mark.parametrize(
    "status", ["quarantined", "superseded", "tombstoned", "pending_validation"]
)
def test_ineligible_evidence_is_not_correlated(status: str) -> None:
    """**`quarantined` is the one that matters most.** It is what a malware detection or a
    custody-chain gap sets, and extracting entities from it would put findings grounded in
    untrustworthy evidence in front of an analyst with nothing saying so."""
    rows = [_content(uuid4(), status=status), _content(uuid4())]

    keep = eligible(rows)

    assert [item.status for item in keep] == ["validated"]


def test_the_extractor_is_not_shown_the_evidence_status() -> None:
    """A deliberate narrowing: an adapter — including a future one that sends text to an inference
    endpoint — must not be able to re-decide the eligibility rule, or carry a field it has no
    business transmitting."""
    record = to_record(_content(uuid4(), status="validated"))

    assert not hasattr(record, "status")


# --- the run itself ---------------------------------------------------------
async def test_a_run_creates_proposed_findings_grounded_in_evidence(inv_uow) -> None:
    """CEM §10: every AI output is born `proposed`. CEM §13/§1.6: no finding without >= 1 supporting
    evidence reference, and CEM §11's MENTIONS edge is what grounds an entity."""
    case_id, evidence_id = uuid4(), uuid4()
    run = _run(case_id)
    await inv_uow.correlation_runs.add(run)
    scope = CaseEvidenceScope(case_id, _OWNER, (evidence_id,))
    service = _service(
        inv_uow,
        scope=scope,
        content=[_content(evidence_id)],
        extractor=_FixedExtractor({evidence_id: _pair("a.example", "b.example")}),
    )

    outcome = await service.execute(run.run_id, correlation_id="corr-1")

    assert outcome is not None
    assert outcome.findings_generated == 3  # two entities + one co-occurrence edge
    entities = list(inv_uow.entities.store.values())
    relationships = list(inv_uow.relationships.store.values())
    assert [entity.status for entity in entities] == ["proposed", "proposed"]
    assert [entity.created_by_type for entity in entities] == ["ai", "ai"]
    assert {entity.created_by_ref for entity in entities} == {run.run_id}
    assert [rel.status for rel in relationships] == ["proposed"]
    assert {mention.evidence_id for mention in inv_uow.entity_mentions.items} == {evidence_id}
    assert {link.evidence_id for link in inv_uow.relationship_evidence.items} == {evidence_id}


async def test_every_finding_is_announced_with_25_8s_payload(inv_uow) -> None:
    """§25.8: `case_id`, one of `relationship_id`/`entity_id`, `confidence`, `generated_by`, and the
    `recipient_user_id` `notification` alerts."""
    case_id, evidence_id = uuid4(), uuid4()
    run = _run(case_id)
    await inv_uow.correlation_runs.add(run)
    service = _service(
        inv_uow,
        scope=CaseEvidenceScope(case_id, _OWNER, (evidence_id,)),
        content=[_content(evidence_id)],
        extractor=_FixedExtractor({evidence_id: _pair("a.example", "b.example")}),
    )

    await service.execute(run.run_id, correlation_id="corr-1")

    published = [
        event
        for event in inv_uow.outbox.published
        if event["event_type"] == "investigation.correlation_generated"
    ]
    assert len(published) == 3
    for event in published:
        payload = event["payload"]
        assert payload["case_id"] == str(case_id)
        assert (payload["entity_id"] is None) != (payload["relationship_id"] is None)
        assert payload["recipient_user_id"] == str(_OWNER)
        assert payload["generated_by"] == f"fixed-extractor/test run:{run.run_id}"
        assert event["correlation_id"] == "corr-1"
        # No parent event: a run is started by a request, not by an event (§6 publishes none at
        # trigger time), so forging a causation would point an auditor at nothing.
        assert event["causation_id"] is None
        assert event["actor_type"] == "system"


async def test_a_second_run_over_unchanged_evidence_creates_nothing(inv_uow) -> None:
    """**The convergence test.** Entity resolution on `(entity_type, canonical_name)`, the MENTIONS
    pair, and the unordered relationship pair — together they make a re-run a no-op rather than a
    doubled graph."""
    case_id, evidence_id = uuid4(), uuid4()
    scope = CaseEvidenceScope(case_id, _OWNER, (evidence_id,))
    extraction = {evidence_id: _pair("a.example", "b.example")}

    first_run = _run(case_id)
    await inv_uow.correlation_runs.add(first_run)
    await _service(
        inv_uow,
        scope=scope,
        content=[_content(evidence_id)],
        extractor=_FixedExtractor(extraction),
    ).execute(first_run.run_id, correlation_id="corr-1")
    first_run.finish(_NOW, failed=False)
    announcements = len(inv_uow.outbox.published)

    second_run = _run(case_id)
    await inv_uow.correlation_runs.add(second_run)
    outcome = await _service(
        inv_uow,
        scope=scope,
        content=[_content(evidence_id)],
        extractor=_FixedExtractor(extraction),
    ).execute(second_run.run_id, correlation_id="corr-2")

    assert outcome is not None
    assert outcome.findings_generated == 0
    assert len(inv_uow.entities.store) == 2
    assert len(inv_uow.relationships.store) == 1
    assert len(inv_uow.entity_mentions.items) == 2
    assert len(inv_uow.outbox.published) == announcements


async def test_an_existing_entity_gains_a_mention_from_new_evidence(inv_uow) -> None:
    """A node resolved from an earlier run — or from an IOC match — that turns up in another
    evidence
    item is now *also* evidenced there. CEM §1.6 wants that grounding recorded; the entity is not
    re-announced, because it is not new."""
    case_id, first_evidence, second_evidence = uuid4(), uuid4(), uuid4()
    scope = CaseEvidenceScope(case_id, _OWNER, (first_evidence, second_evidence))
    one = Extraction(entities=(ExtractedEntity("digital_asset", "a.example", Decimal("1.0")),))

    run = _run(case_id)
    await inv_uow.correlation_runs.add(run)
    outcome = await _service(
        inv_uow,
        scope=scope,
        content=[_content(first_evidence), _content(second_evidence)],
        extractor=_FixedExtractor({first_evidence: one, second_evidence: one}),
    ).execute(run.run_id, correlation_id="corr-1")

    assert outcome is not None
    assert outcome.findings_generated == 1  # announced once
    assert len(inv_uow.entities.store) == 1
    assert {mention.evidence_id for mention in inv_uow.entity_mentions.items} == {
        first_evidence,
        second_evidence,
    }


async def test_a_missing_run_is_not_an_error(inv_uow) -> None:
    service = _service(inv_uow, scope=None, content=[])

    assert await service.execute(uuid4(), correlation_id="corr-1") is None


async def test_an_already_completed_run_does_nothing(inv_uow) -> None:
    run = _run(uuid4(), status=RUN_COMPLETED, completed_at=_NOW)
    await inv_uow.correlation_runs.add(run)
    service = _service(inv_uow, scope=None, content=[])

    assert await service.execute(run.run_id, correlation_id="corr-1") is None
    assert inv_uow.outbox.published == []


async def test_a_deleted_case_ends_the_run_without_failing_it(inv_uow) -> None:
    """Nothing to correlate, and nothing was wrong with the request that asked for it."""
    run = _run(uuid4())
    await inv_uow.correlation_runs.add(run)

    outcome = await _service(inv_uow, scope=None, content=[]).execute(
        run.run_id, correlation_id="corr-1"
    )

    assert outcome is not None
    assert (outcome.findings_generated, outcome.evidence_considered) == (0, 0)
    assert outcome.cancelled is False


async def test_a_malformed_extraction_fails_the_run(inv_uow) -> None:
    """A partially-persisted extraction would leave co-occurrence edges referencing whichever
    entities happened to survive — a worse record than none, and one an analyst cannot recognise as
    incomplete."""
    case_id, evidence_id = uuid4(), uuid4()
    run = _run(case_id)
    await inv_uow.correlation_runs.add(run)
    broken = Extraction(
        entities=(ExtractedEntity("digital_asset", "a.example", Decimal("1.0")),),
        relationships=(ExtractedRelationship(CO_OCCURRENCE_REL_TYPE, 0, 7, False, Decimal("0.5")),),
    )

    with pytest.raises(ValidationFailedError):
        await _service(
            inv_uow,
            scope=CaseEvidenceScope(case_id, _OWNER, (evidence_id,)),
            content=[_content(evidence_id)],
            extractor=_FixedExtractor({evidence_id: broken}),
        ).execute(run.run_id, correlation_id="corr-1")

    assert inv_uow.entities.store == {}


async def test_cancellation_stops_at_a_batch_boundary_and_keeps_what_was_found(inv_uow) -> None:
    """Guide Part 12's cooperative cancellation: between transactions, never mid-write. The findings
    already made are grounded and valid, so they stay."""
    case_id = uuid4()
    run = _run(case_id)
    await inv_uow.correlation_runs.add(run)
    ids = [uuid4() for _ in range(BATCH_SIZE + 5)]
    scope = CaseEvidenceScope(case_id, _OWNER, tuple(ids))
    extractions = {
        evidence_id: Extraction(
            entities=(ExtractedEntity("digital_asset", f"h{index}.example", Decimal("1.0")),)
        )
        for index, evidence_id in enumerate(ids)
    }
    service = _service(
        inv_uow,
        scope=scope,
        content=[_content(evidence_id) for evidence_id in ids],
        extractor=_FixedExtractor(extractions),
    )

    checkpoints = 0

    async def cancel_after_the_first_batch() -> None:
        # The first checkpoint is the claim, which fires before any extraction; the second is the
        # end of batch one. Setting the flag there is what an operator does mid-run.
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 2:
            run.cancellation_requested = True

    outcome = await service.execute(
        run.run_id, correlation_id="corr-1", checkpoint=cancel_after_the_first_batch
    )

    assert outcome is not None
    assert outcome.cancelled is True
    assert outcome.evidence_considered == BATCH_SIZE
    # Stopped at the boundary, and everything the first batch found is still there.
    assert len(inv_uow.entities.store) == BATCH_SIZE
    assert outcome.findings_generated == BATCH_SIZE


async def test_progress_and_checkpoints_land_per_batch(inv_uow) -> None:
    """A failure in batch nine must not discard batches one to eight, and a poller must see the
    count
    move — so the count is written with the findings it describes, in one transaction."""
    case_id = uuid4()
    run = _run(case_id)
    await inv_uow.correlation_runs.add(run)
    ids = [uuid4() for _ in range(BATCH_SIZE * 2)]
    extractions = {
        evidence_id: Extraction(
            entities=(ExtractedEntity("digital_asset", f"h{index}.example", Decimal("1.0")),)
        )
        for index, evidence_id in enumerate(ids)
    }
    counts: list[int] = []

    async def record() -> None:
        counts.append(run.findings_generated_count)

    outcome = await _service(
        inv_uow,
        scope=CaseEvidenceScope(case_id, _OWNER, tuple(ids)),
        content=[_content(evidence_id) for evidence_id in ids],
        extractor=_FixedExtractor(extractions),
    ).execute(run.run_id, correlation_id="corr-1", checkpoint=record)

    assert outcome is not None
    # One for the claim (before any work), then one per batch.
    assert counts == [0, BATCH_SIZE, BATCH_SIZE * 2]
    assert run.findings_generated_count == BATCH_SIZE * 2


async def test_a_retry_resumes_the_runs_count_rather_than_restarting_it(inv_uow) -> None:
    """**The regression this exists for.** `findings_generated_count` is the run's cumulative total
    and survives a failed attempt, while convergence means the retry re-derives those findings and
    creates nothing. A counter starting at zero would hand `record_progress` a number below the
    row's and the monotonic invariant would raise — failing every retry, permanently, on exactly the
    runs that got furthest before breaking.
    """
    case_id = uuid4()
    run = _run(case_id)
    await inv_uow.correlation_runs.add(run)
    ids = [uuid4() for _ in range(BATCH_SIZE + 1)]
    scope = CaseEvidenceScope(case_id, _OWNER, tuple(ids))
    content = [_content(evidence_id) for evidence_id in ids]
    extractions = {
        evidence_id: Extraction(
            entities=(ExtractedEntity("digital_asset", f"h{index}.example", Decimal("1.0")),)
        )
        for index, evidence_id in enumerate(ids)
    }

    class _FailsOnTheLastRecord(_FixedExtractor):
        async def extract(self, record: EvidenceRecord) -> Extraction:
            if record.evidence_id == ids[-1]:
                raise RuntimeError("inference endpoint unreachable")
            return await super().extract(record)

    with pytest.raises(RuntimeError):
        await _service(
            inv_uow,
            scope=scope,
            content=content,
            extractor=_FailsOnTheLastRecord(extractions),
        ).execute(run.run_id, correlation_id=_CORR)
    # The first batch committed its progress before the second one broke.
    assert run.findings_generated_count == BATCH_SIZE

    outcome = await _service(
        inv_uow, scope=scope, content=content, extractor=_FixedExtractor(extractions)
    ).execute(run.run_id, correlation_id=_CORR)

    assert outcome is not None
    assert run.findings_generated_count == BATCH_SIZE + 1
    assert outcome.findings_generated == BATCH_SIZE + 1


async def test_a_scoped_run_reads_only_what_it_was_given(inv_uow) -> None:
    case_id, wanted, other = uuid4(), uuid4(), uuid4()
    run = _run(case_id)
    await inv_uow.correlation_runs.add(run)
    extractor = _FixedExtractor()

    await _service(
        inv_uow,
        scope=CaseEvidenceScope(case_id, _OWNER, (wanted, other)),
        content=[_content(wanted), _content(other)],
        extractor=extractor,
    ).execute(run.run_id, correlation_id="corr-1", evidence_ids=[wanted])

    assert [record.evidence_id for record in extractor.seen] == [wanted]


async def test_a_scoped_run_drops_evidence_unlinked_since_the_trigger(inv_uow) -> None:
    """The case's links are re-read when the run starts. An item unlinked in the meantime is no
    longer this case's evidence, whatever the trigger asked for."""
    case_id, still_linked, unlinked = uuid4(), uuid4(), uuid4()
    run = _run(case_id)
    await inv_uow.correlation_runs.add(run)
    extractor = _FixedExtractor()

    await _service(
        inv_uow,
        scope=CaseEvidenceScope(case_id, _OWNER, (still_linked,)),
        content=[_content(still_linked), _content(unlinked)],
        extractor=extractor,
    ).execute(run.run_id, correlation_id="corr-1", evidence_ids=[still_linked, unlinked])

    assert [record.evidence_id for record in extractor.seen] == [still_linked]


async def test_the_default_run_re_reads_the_cases_links(inv_uow) -> None:
    """Unscoped means "the whole case at the time the run starts" — evidence linked while the job
    sat
    in the queue belongs in the pass the analyst asked for."""
    case_id, first, added_later = uuid4(), uuid4(), uuid4()
    run = _run(case_id)
    await inv_uow.correlation_runs.add(run)
    extractor = _FixedExtractor()

    await _service(
        inv_uow,
        scope=CaseEvidenceScope(case_id, _OWNER, (first, added_later)),
        content=[_content(first), _content(added_later)],
        extractor=extractor,
    ).execute(run.run_id, correlation_id="corr-1")

    assert {record.evidence_id for record in extractor.seen} == {first, added_later}


async def test_the_real_extractor_drives_a_run_end_to_end(inv_uow) -> None:
    """One test with the production adapter rather than a fixture, so the two halves are known to
    fit
    — the classification rules are proven in `test_correlation_extraction.py`."""
    case_id, evidence_id = uuid4(), uuid4()
    run = _run(case_id)
    await inv_uow.correlation_runs.add(run)

    outcome = await _service(
        inv_uow,
        scope=CaseEvidenceScope(case_id, _OWNER, (evidence_id,)),
        content=[
            _content(
                evidence_id,
                title="Beacon to evil-c2.example",
                attributes={"sender": "suspect@mail.example"},
            )
        ],
        extractor=HeuristicIdentifierExtractor(),
    ).execute(run.run_id, correlation_id="corr-1")

    assert outcome is not None
    assert outcome.findings_generated == 3
    assert {entity.entity_type for entity in inv_uow.entities.store.values()} == {
        "digital_asset",
        "account",
    }


# --- the run-lifecycle events -----------------------------------------------
@pytest.mark.parametrize(
    ("failed", "event_type"),
    [
        (False, "investigation.correlation_run_completed"),
        (True, "investigation.correlation_run_failed"),
    ],
    ids=["completed", "failed"],
)
async def test_the_run_finished_event_carries_25_8s_three_fields(
    inv_uow, failed: bool, event_type: str
) -> None:
    run = _run(uuid4(), status=RUN_RUNNING, findings_generated_count=7)

    await publish_run_finished(inv_uow, run, correlation_id="corr-1", failed=failed)

    [event] = inv_uow.outbox.published
    assert event["event_type"] == event_type
    assert event["aggregate_type"] == "correlation_run"
    assert event["payload"] == {
        "run_id": str(run.run_id),
        "case_id": str(run.case_id),
        "findings_generated_count": 7,
    }


# --- the trigger's documented guards (api-design.md §6) ---------------------
def _scope_reader(scope: CaseEvidenceScope | None) -> Any:
    async def _read(_session: Any, _case_id: UUID) -> CaseEvidenceScope | None:
        return scope

    return _read


async def test_a_queued_run_is_created_and_enqueued_with_its_workflow(
    inv_uow, actor, monkeypatch
) -> None:
    """§6's response is `{ run_id, status: "queued" }` — not `pending`, which is not in its enum.

    The `correlation_id` travels with the job so §11's causal chain survives the queue hop: the
    events the worker publishes minutes later belong to the workflow this request started.
    """
    case_id, evidence_id = uuid4(), uuid4()
    monkeypatch.setattr(
        service_module,
        "read_case_evidence_scope",
        _scope_reader(CaseEvidenceScope(case_id, _OWNER, (evidence_id,))),
    )
    queue = _Queue()

    run = await InvestigationService(inv_uow, kms=kms_for_tests()).trigger_correlation_run(
        case_id, actor, "corr-1", queue
    )

    assert run.status == RUN_QUEUED
    assert queue.jobs == [("run_correlation", (run.run_id, "corr-1", None))]


async def test_a_case_with_no_linked_evidence_is_refused(inv_uow, actor, monkeypatch) -> None:
    """§6's "Case must have >= 1 linked evidence item".

    Without this the run would claim itself, find nothing, publish `correlation_run_completed` with
    zero findings and tell the analyst the pass they asked for is done — when what happened is that
    there was nothing to correlate.
    """
    case_id = uuid4()
    monkeypatch.setattr(
        service_module,
        "read_case_evidence_scope",
        _scope_reader(CaseEvidenceScope(case_id, _OWNER, ())),
    )

    with pytest.raises(ValidationFailedError):
        await InvestigationService(inv_uow, kms=kms_for_tests()).trigger_correlation_run(
            case_id, actor, "corr-1", _Queue()
        )


async def test_a_missing_case_is_a_404(inv_uow, actor, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "read_case_evidence_scope", _scope_reader(None))

    with pytest.raises(CaseNotFoundError):
        await InvestigationService(inv_uow, kms=kms_for_tests()).trigger_correlation_run(
            uuid4(), actor, "corr-1", _Queue()
        )


@pytest.mark.parametrize("existing_status", [RUN_QUEUED, RUN_RUNNING])
async def test_a_second_run_for_the_same_case_is_a_conflict(
    inv_uow, actor, monkeypatch, existing_status: str
) -> None:
    """§6's 409. `queued` counts: the row exists and a worker will claim it, so a second trigger
    would put two runs over one case on the queue and announce every finding twice."""
    case_id, evidence_id = uuid4(), uuid4()
    monkeypatch.setattr(
        service_module,
        "read_case_evidence_scope",
        _scope_reader(CaseEvidenceScope(case_id, _OWNER, (evidence_id,))),
    )
    await inv_uow.correlation_runs.add(_run(case_id, status=existing_status))

    with pytest.raises(CorrelationRunInProgressError):
        await InvestigationService(inv_uow, kms=kms_for_tests()).trigger_correlation_run(
            case_id, actor, "corr-1", _Queue()
        )


@pytest.mark.parametrize("finished", [RUN_COMPLETED, RUN_FAILED])
async def test_a_finished_run_does_not_block_a_new_one(
    inv_uow, actor, monkeypatch, finished: str
) -> None:
    case_id, evidence_id = uuid4(), uuid4()
    monkeypatch.setattr(
        service_module,
        "read_case_evidence_scope",
        _scope_reader(CaseEvidenceScope(case_id, _OWNER, (evidence_id,))),
    )
    await inv_uow.correlation_runs.add(_run(case_id, status=finished, completed_at=_NOW))

    run = await InvestigationService(inv_uow, kms=kms_for_tests()).trigger_correlation_run(
        case_id, actor, "corr-1", _Queue()
    )

    assert run.status == RUN_QUEUED


async def test_a_scope_is_validated_against_the_cases_own_links(
    inv_uow, actor, monkeypatch
) -> None:
    """**Refused, not silently intersected.** A request naming ten items, two of them another
    case's,
    would otherwise run over eight and report success — and every finding would be announced against
    a `case_id` that does not hold that evidence."""
    case_id, linked, foreign = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(
        service_module,
        "read_case_evidence_scope",
        _scope_reader(CaseEvidenceScope(case_id, _OWNER, (linked,))),
    )

    with pytest.raises(ValidationFailedError) as caught:
        await InvestigationService(inv_uow, kms=kms_for_tests()).trigger_correlation_run(
            case_id,
            actor,
            "corr-1",
            _Queue(),
            scope=CorrelationScope(evidence_ids=[linked, foreign]),
        )

    assert str(foreign) in str(caught.value.details)


async def test_a_valid_scope_travels_with_the_job(inv_uow, actor, monkeypatch) -> None:
    """A job argument rather than a column: §3.5 gives `correlation_runs` nowhere to store it, and
    arq preserves a job's arguments across retries, so a re-attempt correlates the same set."""
    case_id, first, second = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(
        service_module,
        "read_case_evidence_scope",
        _scope_reader(CaseEvidenceScope(case_id, _OWNER, (first, second))),
    )
    queue = _Queue()

    run = await InvestigationService(inv_uow, kms=kms_for_tests()).trigger_correlation_run(
        case_id, actor, "corr-1", queue, scope=CorrelationScope(evidence_ids=[second])
    )

    assert queue.jobs == [("run_correlation", (run.run_id, "corr-1", [second]))]
