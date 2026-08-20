"""Unit tests for EvidenceService: CEM §13 validation + §4 custody hash chain."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from sentinelai.modules.ingestion.schemas import CustodyEventCreate, EvidenceCreate
from sentinelai.modules.ingestion.service import EvidenceService
from sentinelai.shared.exceptions import LegalHoldViolationError, ValidationFailedError
from tests.fixtures.fake_object_storage import FakeObjectStorage

_REGISTERED = ("1.0.0", "osint", "web_page")


def _valid(category: str = "osint", **overrides) -> EvidenceCreate:  # type: ignore[no-untyped-def]
    base = {
        "schema_version": "1.0.0",
        "category": category,
        "artifact_type": "web_page",
        "title": "A post",
        "source": {"system": "connector-x", "collector_id": "c1"},
        "collected_at": datetime(2026, 1, 1, tzinfo=UTC),
        "attributes": {},
        "confidence": Decimal("0.8"),
        "inline_payload": {"k": "v"},
    }
    base.update(overrides)
    return EvidenceCreate(**base)


def _seed(ing_uow) -> None:  # type: ignore[no-untyped-def]
    ing_uow.attribute_schemas.registered.add(_REGISTERED)


async def test_ingest_valid_evidence_writes_genesis_custody_and_event(ing_uow, actor) -> None:
    _seed(ing_uow)
    svc = EvidenceService(ing_uow, storage=FakeObjectStorage())
    evidence = await svc.ingest_evidence(_valid(), actor, "c")
    assert evidence.status == "validated"
    assert ing_uow.commits == 0  # ADR-0005: the entrypoint, not the service, commits
    # genesis custody event
    assert len(ing_uow.custody.items) == 1
    genesis = ing_uow.custody.items[0]
    assert genesis.event_type == "ingested"
    assert genesis.sequence_number == 1
    assert any(e["event_type"] == "evidence.ingested" for e in ing_uow.outbox.published)


async def test_ingest_unregistered_schema_is_rejected(ing_uow, actor) -> None:
    svc = EvidenceService(ing_uow, storage=FakeObjectStorage())  # registry NOT seeded
    with pytest.raises(ValidationFailedError):
        await svc.ingest_evidence(_valid(), actor, "c")
    assert len(ing_uow.intake.items) == 1  # failure recorded
    assert any(e["event_type"] == "evidence.validation_failed" for e in ing_uow.outbox.published)


async def test_ingest_missing_provenance_rejected(ing_uow, actor) -> None:
    _seed(ing_uow)
    svc = EvidenceService(ing_uow, storage=FakeObjectStorage())
    with pytest.raises(ValidationFailedError):
        await svc.ingest_evidence(_valid(source={"system": "x"}), actor, "c")  # no collector_id


async def test_legal_authority_required_for_forensics(ing_uow, actor) -> None:
    svc = EvidenceService(ing_uow, storage=FakeObjectStorage())
    with pytest.raises(ValidationFailedError):
        await svc.ingest_evidence(_valid(category="digital_forensics"), actor, "c")


async def test_custody_chain_links_forward(ing_uow, actor) -> None:
    _seed(ing_uow)
    svc = EvidenceService(ing_uow, storage=FakeObjectStorage())
    evidence = await svc.ingest_evidence(_valid(), actor, "c")
    genesis = ing_uow.custody.items[0]
    event = await svc.record_custody_event(
        evidence.evidence_id, CustodyEventCreate(event_type="accessed"), actor, "c"
    )
    assert event.sequence_number == 2
    assert event.prev_event_hash == genesis.entry_hash  # chain links


async def test_disposed_under_legal_hold_is_blocked(ing_uow, actor) -> None:
    _seed(ing_uow)
    svc = EvidenceService(ing_uow, storage=FakeObjectStorage())
    evidence = await svc.ingest_evidence(_valid(), actor, "c")
    await svc.record_custody_event(
        evidence.evidence_id, CustodyEventCreate(event_type="legal_hold_applied"), actor, "c"
    )
    with pytest.raises(LegalHoldViolationError):
        await svc.record_custody_event(
            evidence.evidence_id, CustodyEventCreate(event_type="disposed"), actor, "c"
        )


async def test_exists(ing_uow, actor) -> None:
    _seed(ing_uow)
    svc = EvidenceService(ing_uow, storage=FakeObjectStorage())
    evidence = await svc.ingest_evidence(_valid(), actor, "c")
    assert await svc.exists(evidence.evidence_id) is True
    assert await svc.exists(uuid4()) is False


async def test_batch_returns_per_item_results(ing_uow, actor) -> None:
    _seed(ing_uow)
    svc = EvidenceService(ing_uow, storage=FakeObjectStorage())
    results = await svc.ingest_batch([_valid(), _valid(source={"system": "x"})], actor, "c")
    assert results[0]["status"] == "created"
    assert results[1]["status"] == "error"


async def test_supersede_creates_linked_new_version(ing_uow, actor) -> None:
    from sentinelai.modules.ingestion.schemas import EvidenceSupersedeCreate

    _seed(ing_uow)
    svc = EvidenceService(ing_uow, storage=FakeObjectStorage())
    original = await svc.ingest_evidence(_valid(), actor, "c")
    replacement = await svc.supersede_evidence(
        original.evidence_id,
        EvidenceSupersedeCreate(reason="corrected title", replacement=_valid(title="Fixed")),
        actor,
        "c",
    )
    assert replacement.supersedes_evidence_id == original.evidence_id
    assert original.status == "superseded"
    assert any(e["event_type"] == "evidence.superseded" for e in ing_uow.outbox.published)
