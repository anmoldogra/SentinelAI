"""Unit tests for the storage-backed ingestion paths (ADR-0008 + api-design §2.11/§4.2).

Covers only what the object-storage foundation wired up: ``reserve_upload`` (presigned PUT, no
evidence row) and ``get_download_url`` (presigned GET + ``accessed`` custody entry). Evidence
validation and the custody hash chain itself are covered by ``test_ingestion_service.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from sentinelai.modules.ingestion.exceptions import EvidenceNotFoundError
from sentinelai.modules.ingestion.schemas import EvidenceCreate
from sentinelai.modules.ingestion.service import EvidenceService
from sentinelai.platform.config import settings
from sentinelai.platform.storage import InvalidObjectUri, build_object_uri
from sentinelai.shared.exceptions import ValidationFailedError
from tests.fixtures.fake_object_storage import FakeObjectStorage

_REGISTERED = ("1.0.0", "osint", "web_page")


def _payload_evidence(payload_ref: str | None) -> EvidenceCreate:
    return EvidenceCreate(
        schema_version="1.0.0",
        category="osint",
        artifact_type="web_page",
        title="A post",
        source={"system": "connector-x", "collector_id": "c1"},
        collected_at=datetime(2026, 1, 1, tzinfo=UTC),
        attributes={},
        confidence=Decimal("0.8"),
        payload_ref=payload_ref,
        integrity_hash=("a" * 64) if payload_ref else None,
        integrity_algorithm="SHA-256" if payload_ref else None,
        inline_payload=None if payload_ref else {"k": "v"},
    )


def _svc(ing_uow, storage: FakeObjectStorage) -> EvidenceService:  # type: ignore[no-untyped-def]
    ing_uow.attribute_schemas.registered.add(_REGISTERED)
    return EvidenceService(ing_uow, storage=storage)


# --- reserve_upload ---------------------------------------------------------


async def test_reserve_upload_returns_an_id_and_a_presigned_put_url(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(ing_uow, FakeObjectStorage())
    reservation = await svc.reserve_upload("osint", "web_page", actor)
    assert reservation.upload_url.startswith("http")
    assert "method=PUT" in reservation.upload_url
    assert str(reservation.evidence_id) in reservation.upload_url


async def test_reserve_upload_does_not_create_an_evidence_row(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """The reservation is an id + a place to put bytes; POST /evidence creates the record."""
    svc = _svc(ing_uow, FakeObjectStorage())
    reservation = await svc.reserve_upload("osint", "web_page", actor)
    assert await ing_uow.evidence.get_by_id(reservation.evidence_id) is None
    assert ing_uow.custody.items == []


async def test_reserve_upload_keys_are_unique_per_reservation(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(ing_uow, FakeObjectStorage())
    first = await svc.reserve_upload("osint", "web_page", actor)
    second = await svc.reserve_upload("osint", "web_page", actor)
    assert first.evidence_id != second.evidence_id
    assert first.upload_url != second.upload_url


async def test_reserve_upload_rejects_an_unknown_category(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(ing_uow, FakeObjectStorage())
    with pytest.raises(ValidationFailedError):
        await svc.reserve_upload("not_a_category", "web_page", actor)


async def test_reserve_upload_rejects_a_blank_artifact_type(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(ing_uow, FakeObjectStorage())
    with pytest.raises(ValidationFailedError):
        await svc.reserve_upload("osint", "", actor)


# --- get_download_url -------------------------------------------------------


async def test_download_url_records_an_accessed_custody_event(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc = _svc(ing_uow, storage)
    uri = build_object_uri(settings.storage_bucket, "evidence/osint/web_page/x.bin")
    evidence = await svc.ingest_evidence(_payload_evidence(uri), actor, "c")
    before = len(ing_uow.custody.items)

    url = await svc.get_download_url(evidence.evidence_id, actor)

    assert "method=GET" in url
    assert len(ing_uow.custody.items) == before + 1
    accessed = ing_uow.custody.items[-1]
    assert accessed.event_type == "accessed"
    # The ledger stays a chain: the new entry continues the sequence.
    assert accessed.sequence_number == before + 1


async def test_download_url_targets_the_bucket_and_key_from_payload_ref(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc = _svc(ing_uow, storage)
    uri = build_object_uri("other-bucket", "deep/path/object.bin")
    evidence = await svc.ingest_evidence(_payload_evidence(uri), actor, "c")
    url = await svc.get_download_url(evidence.evidence_id, actor)
    assert "other-bucket/deep/path/object.bin" in url


async def test_download_url_rejects_evidence_without_a_payload(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(ing_uow, FakeObjectStorage())
    evidence = await svc.ingest_evidence(_payload_evidence(None), actor, "c")
    with pytest.raises(ValidationFailedError):
        await svc.get_download_url(evidence.evidence_id, actor)


async def test_download_url_rejects_a_malformed_payload_ref(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(ing_uow, FakeObjectStorage())
    evidence = await svc.ingest_evidence(_payload_evidence("not-a-uri"), actor, "c")
    with pytest.raises(InvalidObjectUri):
        await svc.get_download_url(evidence.evidence_id, actor)


async def test_download_url_for_unknown_evidence_raises_not_found(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(ing_uow, FakeObjectStorage())
    with pytest.raises(EvidenceNotFoundError):
        await svc.get_download_url(uuid4(), actor)


async def test_a_failed_ledger_write_does_not_return_a_url(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """If the access cannot be recorded, the caller gets an error — never an unlogged URL."""
    storage = FakeObjectStorage()
    svc = _svc(ing_uow, storage)
    uri = build_object_uri(settings.storage_bucket, "evidence/osint/web_page/x.bin")
    evidence = await svc.ingest_evidence(_payload_evidence(uri), actor, "c")

    async def _fail(_event) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("ledger unavailable")

    ing_uow.custody.add = _fail
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await svc.get_download_url(evidence.evidence_id, actor)
