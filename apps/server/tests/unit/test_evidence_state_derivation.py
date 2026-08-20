"""Unit tests for ADR-0015: evidence state is derived from append-only records, never UPDATEd.

The proof pattern used throughout: perform the transition, then wipe the in-memory attribute back
to its genesis value (simulating a fresh load of the physically-unchanged row), and assert that a
fresh read re-derives current truth from the ledger / supersession linkage alone. If any code path
relied on an in-place column write, these tests would see the genesis value and fail.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import make_transient_to_detached
from sqlalchemy.orm.attributes import set_committed_value

from sentinelai.modules.ingestion.exceptions import (
    EvidenceAlreadySupersededError,
    IntegrityVerificationFailedError,
)
from sentinelai.modules.ingestion.schemas import (
    CustodyEventCreate,
    EvidenceCreate,
    EvidenceSupersedeCreate,
)
from sentinelai.modules.ingestion.service import EvidenceService
from sentinelai.platform.config import settings
from sentinelai.platform.storage import build_object_uri
from sentinelai.shared.exceptions import LegalHoldViolationError
from tests.fixtures.fake_object_storage import FakeObjectStorage

_REGISTERED = ("1.0.0", "osint", "web_page")
_PAYLOAD = b"payload-bytes-under-test"
_PAYLOAD_SHA256 = hashlib.sha256(_PAYLOAD).hexdigest()
_BUCKET = settings.storage_quarantine_bucket
_KEY = "evidence/osint/web_page/object.bin"


async def _bytes(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def _create(**overrides: object) -> EvidenceCreate:
    base: dict[str, object] = {
        "schema_version": "1.0.0",
        "category": "osint",
        "artifact_type": "web_page",
        "title": "A post",
        "source": {"system": "connector-x", "collector_id": "c1"},
        "collected_at": datetime(2026, 1, 1, tzinfo=UTC),
        "attributes": {},
        "confidence": Decimal("0.8"),
        "inline_payload": {"k": "v"},
    }
    base.update(overrides)
    return EvidenceCreate(**base)  # type: ignore[arg-type]


def _svc(ing_uow, storage: FakeObjectStorage | None = None) -> EvidenceService:  # type: ignore[no-untyped-def]
    ing_uow.attribute_schemas.registered.add(_REGISTERED)
    return EvidenceService(ing_uow, storage=storage or FakeObjectStorage())


async def _payload_evidence(svc: EvidenceService, storage: FakeObjectStorage, actor):  # type: ignore[no-untyped-def]
    await storage.put_stream(_BUCKET, _KEY, _bytes(_PAYLOAD))
    return await svc.ingest_evidence(
        _create(
            payload_ref=build_object_uri(_BUCKET, _KEY),
            integrity_hash=_PAYLOAD_SHA256,
            integrity_algorithm="SHA-256",
            inline_payload=None,
        ),
        actor,
        "c",
    )


# --- legal_hold: derived from the custody ledger (ADR-0004 §4) ---------------


async def test_legal_hold_is_derived_from_the_ledger_not_the_column(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(ing_uow)
    evidence = await svc.ingest_evidence(_create(), actor, "c")
    await svc.record_custody_event(
        evidence.evidence_id, CustodyEventCreate(event_type="legal_hold_applied"), actor, "c"
    )
    # Simulate a fresh load of the physically-unchanged row: genesis value restored.
    set_committed_value(evidence, "legal_hold", False)

    fresh = await svc.get_evidence(evidence.evidence_id, actor)
    assert fresh.legal_hold is True  # re-derived from the legal_hold_applied ledger event


async def test_release_event_derives_hold_back_to_false(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(ing_uow)
    evidence = await svc.ingest_evidence(_create(), actor, "c")
    await svc.record_custody_event(
        evidence.evidence_id, CustodyEventCreate(event_type="legal_hold_applied"), actor, "c"
    )
    await svc.record_custody_event(
        evidence.evidence_id, CustodyEventCreate(event_type="legal_hold_released"), actor, "c"
    )
    set_committed_value(evidence, "legal_hold", True)  # stale in-memory value
    fresh = await svc.get_evidence(evidence.evidence_id, actor)
    assert fresh.legal_hold is False  # latest ledger event wins


async def test_disposal_gate_reads_the_derived_hold(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """Security §39's legal-hold gate must hold even if the column is stale-genesis."""
    svc = _svc(ing_uow)
    evidence = await svc.ingest_evidence(_create(), actor, "c")
    await svc.record_custody_event(
        evidence.evidence_id, CustodyEventCreate(event_type="legal_hold_applied"), actor, "c"
    )
    set_committed_value(evidence, "legal_hold", False)  # column genesis says no hold
    with pytest.raises(LegalHoldViolationError):
        await svc.record_custody_event(
            evidence.evidence_id, CustodyEventCreate(event_type="disposed"), actor, "c"
        )


# --- status: derived from supersession structure (CEM §12) -------------------


async def test_superseded_status_is_derived_from_the_replacement_row(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(ing_uow)
    original = await svc.ingest_evidence(_create(), actor, "c")
    await svc.supersede_evidence(
        original.evidence_id,
        EvidenceSupersedeCreate(reason="corrected", replacement=_create(title="Fixed")),
        actor,
        "c",
    )
    set_committed_value(original, "status", "validated")  # simulate fresh genesis load
    fresh = await svc.get_evidence(original.evidence_id, actor)
    assert fresh.status == "superseded"  # derived from supersedes_evidence_id linkage


async def test_double_supersede_is_rejected_via_derivation(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(ing_uow)
    original = await svc.ingest_evidence(_create(), actor, "c")
    await svc.supersede_evidence(
        original.evidence_id,
        EvidenceSupersedeCreate(reason="corrected", replacement=_create(title="Fixed")),
        actor,
        "c",
    )
    set_committed_value(original, "status", "validated")  # even with a stale-genesis column
    with pytest.raises(EvidenceAlreadySupersededError):
        await svc.supersede_evidence(
            original.evidence_id,
            EvidenceSupersedeCreate(reason="again", replacement=_create(title="Again")),
            actor,
            "c",
        )


# --- integrity_verification_status: derived from reverification events -------


async def test_verified_status_is_derived_from_the_reverification_event(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc = _svc(ing_uow, storage)
    evidence = await _payload_evidence(svc, storage, actor)
    await svc.verify_integrity(evidence.evidence_id, actor)

    set_committed_value(evidence, "integrity_verification_status", "pending")  # genesis
    fresh = await svc.get_evidence(evidence.evidence_id, actor)
    assert fresh.integrity_verification_status == "verified"


async def test_failed_status_is_derived_after_a_mismatch(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc = _svc(ing_uow, storage)
    evidence = await _payload_evidence(svc, storage, actor)
    # Corrupt the stored object after ingest: recorded hash no longer matches the bytes.
    await storage.put_stream(_BUCKET, _KEY, _bytes(b"tampered"))
    with pytest.raises(IntegrityVerificationFailedError):
        await svc.verify_integrity(evidence.evidence_id, actor)

    set_committed_value(evidence, "integrity_verification_status", "pending")
    fresh = await svc.get_evidence(evidence.evidence_id, actor)
    assert fresh.integrity_verification_status == "failed"


async def test_inline_evidence_keeps_not_applicable(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(ing_uow)
    evidence = await svc.ingest_evidence(_create(), actor, "c")
    fresh = await svc.get_evidence(evidence.evidence_id, actor)
    assert fresh.integrity_verification_status == "not_applicable"


# --- the core invariant: no transition dirties the ORM row -------------------


async def test_no_transition_marks_the_evidence_row_dirty(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """If any code path reintroduces an in-place assignment (which the ADR-0004 trigger would
    reject at the database), the instance becomes dirty and this test fails."""
    storage = FakeObjectStorage()
    svc = _svc(ing_uow, storage)
    evidence = await _payload_evidence(svc, storage, actor)
    # Simulate a persistent (DB-loaded) instance: commit all pending attribute state.
    make_transient_to_detached(evidence)
    assert inspect(evidence).modified is False

    await svc.record_custody_event(
        evidence.evidence_id, CustodyEventCreate(event_type="legal_hold_applied"), actor, "c"
    )
    await svc.record_custody_event(
        evidence.evidence_id, CustodyEventCreate(event_type="legal_hold_released"), actor, "c"
    )
    await svc.verify_integrity(evidence.evidence_id, actor)
    await svc.supersede_evidence(
        evidence.evidence_id,
        EvidenceSupersedeCreate(reason="corrected", replacement=_create(title="Fixed")),
        actor,
        "c",
    )

    assert inspect(evidence).modified is False  # every transition was append-only
    # ...while the in-memory view still reflects every transition:
    assert evidence.status == "superseded"
    assert evidence.legal_hold is False
    assert evidence.integrity_verification_status == "verified"
