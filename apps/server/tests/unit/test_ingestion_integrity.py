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
from tests.fixtures.kms import kms_for_tests

_REGISTERED = ("1.0.0", "osint", "web_page")
_PAYLOAD = b"forensic-image-bytes"
_PAYLOAD_SHA256 = hashlib.sha256(_PAYLOAD).hexdigest()
# Different bytes under the same key — what tampering after ingest actually looks like.
_TAMPERED = b"forensic-image-bytes-ALTERED"
_TAMPERED_SHA256 = hashlib.sha256(_TAMPERED).hexdigest()
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
    return EvidenceService(ing_uow, storage=storage, kms=kms_for_tests())


async def _stored_evidence(  # type: ignore[no-untyped-def]
    ing_uow, actor, storage, *, declared_hash=_PAYLOAD_SHA256, algorithm="SHA-256"
):
    """Ingest payload-bearing evidence whose object really exists in the fake store.

    ``declared_hash`` defaults to the payload's true digest because, since ADR-0008 §3's
    ingest-time verification, a wrong one no longer produces a stored row to test against —
    it is rejected at the door. Post-hoc verification failures are now set up by mutating
    *storage* after ingest (``_tamper_stored_object``), which is the scenario
    ``verify_integrity`` exists to catch.
    """
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


async def _tamper_stored_object(storage) -> None:  # type: ignore[no-untyped-def]
    """Replace the stored bytes under the same key, leaving the evidence row untouched."""
    await storage.put_stream(_BUCKET, _KEY, _bytes(_TAMPERED))


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


# --- mismatch (post-ingest tampering) ---------------------------------------
#
# These now stage the failure the way it really happens: evidence is admitted with bytes that
# verified, and the *stored object* is altered afterwards. A lying client can no longer create
# this state — ingest rejects it (see the ingest-time section below).


async def test_hash_mismatch_raises_and_never_reports_success(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence = await _stored_evidence(ing_uow, actor, storage)
    await _tamper_stored_object(storage)
    with pytest.raises(IntegrityVerificationFailedError):
        await svc.verify_integrity(evidence.evidence_id, actor)


async def test_hash_mismatch_is_recorded_on_the_custody_ledger(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """A failed verification must be auditable, not silent."""
    storage = FakeObjectStorage()
    svc, evidence = await _stored_evidence(ing_uow, actor, storage)
    await _tamper_stored_object(storage)
    with pytest.raises(IntegrityVerificationFailedError):
        await svc.verify_integrity(evidence.evidence_id, actor)

    entry = ing_uow.custody.items[-1]
    assert entry.event_type == "integrity_reverified"
    assert entry.integrity_hash_at_event == _TAMPERED_SHA256  # what is on disk NOW
    assert "MISMATCH" in (entry.notes or "")


async def test_mismatch_error_does_not_leak_digest_values(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence = await _stored_evidence(ing_uow, actor, storage)
    await _tamper_stored_object(storage)
    with pytest.raises(IntegrityVerificationFailedError) as caught:
        await svc.verify_integrity(evidence.evidence_id, actor)
    assert _TAMPERED_SHA256 not in str(caught.value)
    assert _PAYLOAD_SHA256 not in str(caught.value)


# --- failure modes ----------------------------------------------------------


async def test_missing_object_is_distinguished_from_a_mismatch(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """404, not 422: the record exists and it is its payload that has gone.

    Staged by deleting the object after ingest — the row cannot be created without one now.
    """
    storage = FakeObjectStorage()
    svc, evidence = await _stored_evidence(ing_uow, actor, storage)
    await storage.delete(_BUCKET, _KEY)
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
    svc, evidence = await _stored_evidence(ing_uow, actor, storage)
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


# --- ingest-time verification (ADR-0008 §3) ---------------------------------
#
# The client's declared hash is a claim, checked against the stored bytes before any row is
# written. These are the tests that make "never trusted" true rather than aspirational.


async def test_ingest_records_the_server_digest_not_the_client_claim(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """The genesis ledger entry must attest to bytes the server hashed itself."""
    storage = FakeObjectStorage()
    await _stored_evidence(ing_uow, actor, storage)
    genesis = ing_uow.custody.items[0]
    assert genesis.event_type == "ingested"
    assert genesis.sequence_number == 1
    assert genesis.integrity_hash_at_event == _PAYLOAD_SHA256


async def test_ingest_marks_a_verified_payload_verified_not_pending(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    _, evidence = await _stored_evidence(ing_uow, actor, storage)
    assert evidence.integrity_verification_status == "verified"


async def test_ingest_rejects_a_hash_that_does_not_match_the_stored_bytes(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    await storage.put_stream(_BUCKET, _KEY, _bytes(_PAYLOAD))
    svc = _svc(ing_uow, storage)
    with pytest.raises(ValidationFailedError) as caught:
        await svc.ingest_evidence(
            _evidence_create(integrity_hash="b" * 64, payload_ref=build_object_uri(_BUCKET, _KEY)),
            actor,
            "c",
        )
    assert any(error["field"] == "integrity_hash" for error in caught.value.details)


async def test_a_rejected_hash_writes_no_evidence_and_no_custody_entry(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """Rejection means the store is untouched — not a row quarantined in some failed state."""
    storage = FakeObjectStorage()
    await storage.put_stream(_BUCKET, _KEY, _bytes(_PAYLOAD))
    svc = _svc(ing_uow, storage)
    with pytest.raises(ValidationFailedError):
        await svc.ingest_evidence(
            _evidence_create(integrity_hash="b" * 64, payload_ref=build_object_uri(_BUCKET, _KEY)),
            actor,
            "c",
        )
    assert ing_uow.evidence.store == {}
    assert ing_uow.custody.items == []


async def test_a_rejected_hash_is_still_recorded_as_a_failed_intake(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """§25.2: the rejection is itself a business fact — intake record + validation_failed event."""
    storage = FakeObjectStorage()
    await storage.put_stream(_BUCKET, _KEY, _bytes(_PAYLOAD))
    svc = _svc(ing_uow, storage)
    with pytest.raises(ValidationFailedError):
        await svc.ingest_evidence(
            _evidence_create(integrity_hash="b" * 64, payload_ref=build_object_uri(_BUCKET, _KEY)),
            actor,
            "c",
        )
    assert len(ing_uow.intake.items) == 1
    assert ing_uow.intake.items[0].validation_status == "failed"
    assert [event["event_type"] for event in ing_uow.outbox.published] == [
        "evidence.validation_failed"
    ]


async def test_ingest_rejects_a_payload_ref_with_no_stored_object(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """422 on the submission, not 404: no record exists yet, so the request is what is wrong."""
    svc = _svc(ing_uow, FakeObjectStorage())  # nothing ever stored
    with pytest.raises(ValidationFailedError) as caught:
        await svc.ingest_evidence(
            _evidence_create(
                integrity_hash=_PAYLOAD_SHA256, payload_ref=build_object_uri(_BUCKET, _KEY)
            ),
            actor,
            "c",
        )
    assert any(error["field"] == "payload_ref" for error in caught.value.details)


async def test_ingest_rejects_a_malformed_payload_ref(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """Caught at the door now, rather than surfacing much later at download time."""
    svc = _svc(ing_uow, FakeObjectStorage())
    with pytest.raises(ValidationFailedError) as caught:
        await svc.ingest_evidence(
            _evidence_create(integrity_hash=_PAYLOAD_SHA256, payload_ref="not-a-uri"), actor, "c"
        )
    assert any(error["field"] == "payload_ref" for error in caught.value.details)


async def test_ingest_verification_streams_a_large_object(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    blob = b"q" * (4 * 1024 * 1024)
    await storage.put_stream(_BUCKET, _KEY, _bytes(blob[:1000], blob[1000:]))
    svc = _svc(ing_uow, storage)
    evidence = await svc.ingest_evidence(
        _evidence_create(
            integrity_hash=hashlib.sha256(blob).hexdigest(),
            payload_ref=build_object_uri(_BUCKET, _KEY),
        ),
        actor,
        "c",
    )
    assert evidence.integrity_verification_status == "verified"
    assert ing_uow.custody.items[0].integrity_hash_at_event == hashlib.sha256(blob).hexdigest()


async def test_ingest_verification_never_commits_the_uow_itself(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """ADR-0005 again, on the ingest path: the entrypoint owns the transaction."""
    storage = FakeObjectStorage()
    commits_before = ing_uow.commits
    await _stored_evidence(ing_uow, actor, storage)
    assert ing_uow.commits == commits_before


async def test_payload_free_evidence_is_unaffected_by_ingest_verification(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """No stored bytes, nothing to verify — inline evidence keeps its previous behaviour."""
    svc = _svc(ing_uow, FakeObjectStorage())
    evidence = await svc.ingest_evidence(
        _evidence_create(integrity_hash=None, algorithm=None, payload_ref=None), actor, "c"
    )
    assert evidence.integrity_verification_status == "not_applicable"
