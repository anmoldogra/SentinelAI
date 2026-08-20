"""Unit tests for quarantine placement + server-side integrity verification (ADR-0008 §2-3).

Uses the existing ``FakeObjectStorage`` (no second fake) and the existing ``ing_uow`` fixture, so
these exercise the real service against the real ObjectStorage contract.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from sentinelai.modules.ingestion.exceptions import (
    EvidenceNotFoundError,
    EvidencePayloadMissingError,
    IntegrityVerificationFailedError,
)
from sentinelai.modules.ingestion.schemas import EvidenceCreate
from sentinelai.modules.ingestion.service import EvidenceService
from sentinelai.platform.config import settings
from sentinelai.platform.storage import StorageUnavailable, build_object_uri
from sentinelai.shared.exceptions import ValidationFailedError
from tests.fixtures.fake_object_storage import FakeObjectStorage

_REGISTERED = ("1.0.0", "osint", "web_page")
_PAYLOAD = b"forensic-image-bytes"
_PAYLOAD_SHA256 = hashlib.sha256(_PAYLOAD).hexdigest()
_BUCKET = settings.storage_quarantine_bucket
_KEY = "evidence/osint/web_page/object.bin"


async def _bytes(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def _evidence_create(
    *, integrity_hash: str | None, algorithm: str | None = "SHA-256", payload_ref: str | None
) -> EvidenceCreate:
    return EvidenceCreate(
        schema_version="1.0.0",
        category="osint",
        artifact_type="web_page",
        title="A captured page",
        source={"system": "connector-x", "collector_id": "c1"},
        collected_at=datetime(2026, 1, 1, tzinfo=UTC),
        attributes={},
        confidence=Decimal("0.8"),
        payload_ref=payload_ref,
        integrity_hash=integrity_hash,
        integrity_algorithm=algorithm,
        inline_payload=None if payload_ref else {"k": "v"},
    )


def _svc(ing_uow, storage: FakeObjectStorage) -> EvidenceService:  # type: ignore[no-untyped-def]
    ing_uow.attribute_schemas.registered.add(_REGISTERED)
    return EvidenceService(ing_uow, storage=storage)


async def _stored_evidence(ing_uow, actor, storage, *, declared_hash, algorithm="SHA-256"):  # type: ignore[no-untyped-def]
    """Ingest payload-bearing evidence whose object really exists in the fake store."""
    await storage.put_stream(_BUCKET, _KEY, _bytes(_PAYLOAD))
    svc = _svc(ing_uow, storage)
    evidence = await svc.ingest_evidence(
        _evidence_create(
            integrity_hash=declared_hash,
            algorithm=algorithm,
            payload_ref=build_object_uri(_BUCKET, _KEY),
        ),
        actor,
        "c",
    )
    return svc, evidence


# --- quarantine placement (ADR-0008 §2) -------------------------------------


async def test_reserved_uploads_target_the_quarantine_bucket(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(ing_uow, FakeObjectStorage())
    reservation = await svc.reserve_upload("osint", "web_page", actor)
    assert settings.storage_quarantine_bucket in reservation.upload_url


async def test_quarantine_bucket_is_not_the_evidence_bucket(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """Uploaded bytes must never land directly in the served evidence bucket."""
    svc = _svc(ing_uow, FakeObjectStorage())
    reservation = await svc.reserve_upload("osint", "web_page", actor)
    assert settings.storage_quarantine_bucket != settings.storage_bucket
    assert f"/{settings.storage_bucket}/" not in reservation.upload_url


# --- successful verification ------------------------------------------------


async def test_matching_digest_verifies_and_records_the_recomputed_hash(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence = await _stored_evidence(ing_uow, actor, storage, declared_hash=_PAYLOAD_SHA256)
    before = len(ing_uow.custody.items)

    result = await svc.verify_integrity(evidence.evidence_id, actor)

    assert result.evidence_id == evidence.evidence_id
    entry = ing_uow.custody.items[-1]
    assert entry.event_type == "integrity_reverified"
    assert entry.integrity_hash_at_event == _PAYLOAD_SHA256
    assert entry.sequence_number == before + 1  # the chain continues, nothing is rewritten


async def test_verification_never_commits_the_uow_itself(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """ADR-0005: the ledger write rides the entrypoint's transaction — the service must not
    commit, so a failed HTTP request can still roll the whole unit back."""
    storage = FakeObjectStorage()
    svc, evidence = await _stored_evidence(ing_uow, actor, storage, declared_hash=_PAYLOAD_SHA256)
    commits_before = ing_uow.commits
    await svc.verify_integrity(evidence.evidence_id, actor)
    assert ing_uow.commits == commits_before


async def test_empty_object_verifies_against_the_empty_digest(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    empty_digest = hashlib.sha256(b"").hexdigest()
    await storage.put_stream(_BUCKET, _KEY, _bytes())
    svc = _svc(ing_uow, storage)
    evidence = await svc.ingest_evidence(
        _evidence_create(integrity_hash=empty_digest, payload_ref=build_object_uri(_BUCKET, _KEY)),
        actor,
        "c",
    )
    await svc.verify_integrity(evidence.evidence_id, actor)
    assert ing_uow.custody.items[-1].integrity_hash_at_event == empty_digest


async def test_large_object_is_verified_by_streaming(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    blob = b"q" * (4 * 1024 * 1024)
    digest = hashlib.sha256(blob).hexdigest()
    await storage.put_stream(_BUCKET, _KEY, _bytes(blob[:1000], blob[1000:]))
    svc = _svc(ing_uow, storage)
    evidence = await svc.ingest_evidence(
        _evidence_create(integrity_hash=digest, payload_ref=build_object_uri(_BUCKET, _KEY)),
        actor,
        "c",
    )
    await svc.verify_integrity(evidence.evidence_id, actor)
    assert ing_uow.custody.items[-1].integrity_hash_at_event == digest


# --- mismatch ---------------------------------------------------------------


async def test_hash_mismatch_raises_and_never_reports_success(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence = await _stored_evidence(ing_uow, actor, storage, declared_hash="b" * 64)
    with pytest.raises(IntegrityVerificationFailedError):
        await svc.verify_integrity(evidence.evidence_id, actor)


async def test_hash_mismatch_is_recorded_on_the_custody_ledger(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """A failed verification must be auditable, not silent."""
    storage = FakeObjectStorage()
    svc, evidence = await _stored_evidence(ing_uow, actor, storage, declared_hash="b" * 64)
    with pytest.raises(IntegrityVerificationFailedError):
        await svc.verify_integrity(evidence.evidence_id, actor)

    entry = ing_uow.custody.items[-1]
    assert entry.event_type == "integrity_reverified"
    assert entry.integrity_hash_at_event == _PAYLOAD_SHA256  # the real bytes' digest
    assert "MISMATCH" in (entry.notes or "")


async def test_mismatch_error_does_not_leak_digest_values(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence = await _stored_evidence(ing_uow, actor, storage, declared_hash="b" * 64)
    with pytest.raises(IntegrityVerificationFailedError) as caught:
        await svc.verify_integrity(evidence.evidence_id, actor)
    assert _PAYLOAD_SHA256 not in str(caught.value)


# --- failure modes ----------------------------------------------------------


async def test_missing_object_is_distinguished_from_a_mismatch(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()  # nothing ever stored
    svc = _svc(ing_uow, storage)
    evidence = await svc.ingest_evidence(
        _evidence_create(
            integrity_hash=_PAYLOAD_SHA256, payload_ref=build_object_uri(_BUCKET, _KEY)
        ),
        actor,
        "c",
    )
    with pytest.raises(EvidencePayloadMissingError):
        await svc.verify_integrity(evidence.evidence_id, actor)


async def test_storage_failure_propagates_as_a_storage_error(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence = await _stored_evidence(ing_uow, actor, storage, declared_hash=_PAYLOAD_SHA256)

    async def _unavailable(bucket: str, key: str) -> bool:
        raise StorageUnavailable("endpoint down")

    storage.exists = _unavailable  # type: ignore[method-assign]
    with pytest.raises(StorageUnavailable):
        await svc.verify_integrity(evidence.evidence_id, actor)


async def test_evidence_without_a_payload_cannot_be_verified(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(ing_uow, FakeObjectStorage())
    evidence = await svc.ingest_evidence(
        _evidence_create(integrity_hash=None, algorithm=None, payload_ref=None), actor, "c"
    )
    with pytest.raises(ValidationFailedError):
        await svc.verify_integrity(evidence.evidence_id, actor)


async def test_malformed_recorded_hash_is_rejected_before_streaming(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """An unusable expectation is a validation error, never a silent pass."""
    storage = FakeObjectStorage()
    svc, evidence = await _stored_evidence(ing_uow, actor, storage, declared_hash="a" * 64)
    evidence.integrity_hash = "not-a-digest"
    with pytest.raises(ValidationFailedError):
        await svc.verify_integrity(evidence.evidence_id, actor)


async def test_unsupported_recorded_algorithm_is_rejected(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence = await _stored_evidence(ing_uow, actor, storage, declared_hash=_PAYLOAD_SHA256)
    evidence.integrity_algorithm = "MD5"
    with pytest.raises(ValidationFailedError):
        await svc.verify_integrity(evidence.evidence_id, actor)


async def test_verifying_unknown_evidence_raises_not_found(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(ing_uow, FakeObjectStorage())
    with pytest.raises(EvidenceNotFoundError):
        await svc.verify_integrity(uuid4(), actor)
