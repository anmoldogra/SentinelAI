"""Unit tests for the scan + promotion workflow (ADR-0008 §2, security-architecture §25).

Uses the existing ``FakeObjectStorage`` (no second fake) and the existing ``DummyMalwareScanner``.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from sentinelai.modules.ingestion.events import EVENT_EVIDENCE_SCANNED
from sentinelai.modules.ingestion.exceptions import (
    EvidenceNotFoundError,
    EvidencePayloadMissingError,
)
from sentinelai.modules.ingestion.schemas import EvidenceCreate
from sentinelai.modules.ingestion.service import EvidenceService
from sentinelai.platform.config import settings
from sentinelai.platform.security.scanner import DummyMalwareScanner, ScanResult
from sentinelai.platform.storage import build_object_uri, parse_object_uri
from sentinelai.shared.exceptions import ValidationFailedError
from tests.fixtures.fake_object_storage import FakeObjectStorage

_PAYLOAD = b"uploaded-object-bytes"
_PAYLOAD_SHA256 = hashlib.sha256(_PAYLOAD).hexdigest()
_QUARANTINE = settings.storage_quarantine_bucket
_EVIDENCE = settings.storage_bucket


async def _bytes(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


class _ExplodingScanner:
    """A scanner whose engine fails — must never be treated as a clean verdict."""

    engine = "exploding"

    async def scan(self, stream: AsyncIterator[bytes]) -> ScanResult:
        raise RuntimeError("scan engine unavailable")


async def _quarantined(ing_uow, actor, storage, *, category="osint", artifact_type="web_page"):  # type: ignore[no-untyped-def]
    """Ingest payload-bearing evidence whose object sits in the quarantine bucket."""
    ing_uow.attribute_schemas.registered.add(("1.0.0", category, artifact_type))
    key = f"evidence/{category}/{artifact_type}/{uuid4()}"
    await storage.put_stream(_QUARANTINE, key, _bytes(_PAYLOAD))
    svc = EvidenceService(ing_uow, storage=storage)
    evidence = await svc.ingest_evidence(
        EvidenceCreate(
            schema_version="1.0.0",
            category=category,
            artifact_type=artifact_type,
            title="An upload",
            source={"system": "connector-x", "collector_id": "c1"},
            collected_at=datetime(2026, 1, 1, tzinfo=UTC),
            attributes={},
            confidence=Decimal("0.8"),
            payload_ref=build_object_uri(_QUARANTINE, key),
            integrity_hash=_PAYLOAD_SHA256,
            integrity_algorithm="SHA-256",
            legal_authority_ref="warrant-1" if category != "osint" else None,
        ),
        actor,
        "c",
    )
    return svc, evidence, key


# --- clean file: promoted ---------------------------------------------------


async def test_clean_object_is_copied_to_the_evidence_bucket(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence, key = await _quarantined(ing_uow, actor, storage)
    await svc.scan_and_promote(evidence.evidence_id, DummyMalwareScanner(is_clean=True))
    assert await storage.exists(_EVIDENCE, key) is True


async def test_clean_promotion_deletes_the_quarantine_copy(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence, key = await _quarantined(ing_uow, actor, storage)
    await svc.scan_and_promote(evidence.evidence_id, DummyMalwareScanner(is_clean=True))
    assert await storage.exists(_QUARANTINE, key) is False


async def test_clean_promotion_appends_a_transferred_custody_event(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence, _ = await _quarantined(ing_uow, actor, storage)
    before = len(ing_uow.custody.items)
    await svc.scan_and_promote(evidence.evidence_id, DummyMalwareScanner(is_clean=True))
    entry = ing_uow.custody.items[-1]
    assert entry.event_type == "transferred"
    assert entry.sequence_number == before + 1
    assert "clean" in (entry.notes or "")


async def test_promotion_repoints_the_derived_payload_ref(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """The genesis payload_ref names quarantine and is never UPDATEd (ADR-0015); the current
    location is derived from the `transferred` event."""
    storage = FakeObjectStorage()
    svc, evidence, key = await _quarantined(ing_uow, actor, storage)
    await svc.scan_and_promote(evidence.evidence_id, DummyMalwareScanner(is_clean=True))
    fresh = await svc.get_evidence(evidence.evidence_id, actor)
    assert parse_object_uri(fresh.payload_ref) == (_EVIDENCE, key)


async def test_download_serves_the_promoted_object(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence, key = await _quarantined(ing_uow, actor, storage)
    await svc.scan_and_promote(evidence.evidence_id, DummyMalwareScanner(is_clean=True))
    url = await svc.get_download_url(evidence.evidence_id, actor)
    assert f"{_EVIDENCE}/{key}" in url
    assert _QUARANTINE not in url


async def test_the_scanner_streams_the_whole_object(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence, _ = await _quarantined(ing_uow, actor, storage)
    scanner = DummyMalwareScanner(is_clean=True)
    await svc.scan_and_promote(evidence.evidence_id, scanner)
    assert scanner.bytes_scanned == len(_PAYLOAD)


# --- dirty file, non-forensic: blocked --------------------------------------


async def test_infected_non_forensic_object_stays_in_quarantine(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence, key = await _quarantined(ing_uow, actor, storage, category="osint")
    await svc.scan_and_promote(
        evidence.evidence_id, DummyMalwareScanner(is_clean=False, signature="Eicar-Test")
    )
    assert await storage.exists(_QUARANTINE, key) is True
    assert await storage.exists(_EVIDENCE, key) is False


async def test_blocked_promotion_appends_an_analyzed_event_not_transferred(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence, _ = await _quarantined(ing_uow, actor, storage, category="osint")
    await svc.scan_and_promote(
        evidence.evidence_id, DummyMalwareScanner(is_clean=False, signature="Eicar-Test")
    )
    entry = ing_uow.custody.items[-1]
    assert entry.event_type == "analyzed"
    assert "BLOCKED" in (entry.notes or "")
    assert "Eicar-Test" in (entry.notes or "")
    assert not any(e.event_type == "transferred" for e in ing_uow.custody.items)


async def test_blocked_object_payload_ref_still_names_quarantine(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence, key = await _quarantined(ing_uow, actor, storage, category="osint")
    await svc.scan_and_promote(
        evidence.evidence_id, DummyMalwareScanner(is_clean=False, signature="Eicar-Test")
    )
    fresh = await svc.get_evidence(evidence.evidence_id, actor)
    assert parse_object_uri(fresh.payload_ref) == (_QUARANTINE, key)


# --- security §25 forensic carve-out ----------------------------------------


@pytest.mark.parametrize("category", ["digital_forensics", "mobile_forensics"])
async def test_infected_forensic_image_is_promoted_not_blocked(ing_uow, actor, category) -> None:  # type: ignore[no-untyped-def]
    """§25: a forensic disk image containing malware is evidence, not a scanning failure."""
    storage = FakeObjectStorage()
    svc, evidence, key = await _quarantined(
        ing_uow, actor, storage, category=category, artifact_type="disk_image"
    )
    await svc.scan_and_promote(
        evidence.evidence_id, DummyMalwareScanner(is_clean=False, signature="Trojan.Win32")
    )
    assert await storage.exists(_EVIDENCE, key) is True
    assert await storage.exists(_QUARANTINE, key) is False


async def test_forensic_exception_records_the_detection_on_the_ledger(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """The detection must be recorded as metadata, not silently dropped (§25)."""
    storage = FakeObjectStorage()
    svc, evidence, _ = await _quarantined(
        ing_uow, actor, storage, category="digital_forensics", artifact_type="disk_image"
    )
    await svc.scan_and_promote(
        evidence.evidence_id, DummyMalwareScanner(is_clean=False, signature="Trojan.Win32")
    )
    entry = ing_uow.custody.items[-1]
    assert entry.event_type == "transferred"
    assert "Trojan.Win32" in (entry.notes or "")
    assert "§25" in (entry.notes or "")


async def test_clean_forensic_image_is_promoted_normally(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence, key = await _quarantined(
        ing_uow, actor, storage, category="digital_forensics", artifact_type="disk_image"
    )
    await svc.scan_and_promote(evidence.evidence_id, DummyMalwareScanner(is_clean=True))
    assert await storage.exists(_EVIDENCE, key) is True


# --- failure handling / idempotency -----------------------------------------


async def test_a_failing_scan_engine_never_promotes(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence, key = await _quarantined(ing_uow, actor, storage)
    with pytest.raises(RuntimeError, match="scan engine unavailable"):
        await svc.scan_and_promote(evidence.evidence_id, _ExplodingScanner())
    assert await storage.exists(_EVIDENCE, key) is False
    assert await storage.exists(_QUARANTINE, key) is True
    assert not any(e.event_type == "transferred" for e in ing_uow.custody.items)


async def test_rerunning_after_promotion_is_a_no_op(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """arq retries the job; a completed promotion must not be repeated or double-recorded."""
    storage = FakeObjectStorage()
    svc, evidence, _ = await _quarantined(ing_uow, actor, storage)
    await svc.scan_and_promote(evidence.evidence_id, DummyMalwareScanner(is_clean=True))
    transfers = [e for e in ing_uow.custody.items if e.event_type == "transferred"]
    await svc.scan_and_promote(evidence.evidence_id, DummyMalwareScanner(is_clean=True))
    assert [e for e in ing_uow.custody.items if e.event_type == "transferred"] == transfers


async def test_object_already_in_evidence_bucket_is_recovered_not_reported_missing(  # type: ignore[no-untyped-def]
    ing_uow, actor
) -> None:
    """Crash between the server-side copy and the commit: the next run records the promotion."""
    storage = FakeObjectStorage()
    svc, evidence, key = await _quarantined(ing_uow, actor, storage)
    await storage.copy_object(_QUARANTINE, key, _EVIDENCE, key)
    await storage.delete(_QUARANTINE, key)

    await svc.scan_and_promote(evidence.evidence_id, DummyMalwareScanner(is_clean=True))
    entry = ing_uow.custody.items[-1]
    assert entry.event_type == "transferred"
    assert "recovered" in (entry.notes or "")


async def test_missing_object_raises_rather_than_promoting(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence, key = await _quarantined(ing_uow, actor, storage)
    await storage.delete(_QUARANTINE, key)
    with pytest.raises(EvidencePayloadMissingError):
        await svc.scan_and_promote(evidence.evidence_id, DummyMalwareScanner(is_clean=True))


async def test_evidence_without_a_payload_cannot_be_scanned(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    ing_uow.attribute_schemas.registered.add(("1.0.0", "osint", "web_page"))
    svc = EvidenceService(ing_uow, storage=FakeObjectStorage())
    evidence = await svc.ingest_evidence(
        EvidenceCreate(
            schema_version="1.0.0",
            category="osint",
            artifact_type="web_page",
            title="Inline only",
            source={"system": "connector-x", "collector_id": "c1"},
            collected_at=datetime(2026, 1, 1, tzinfo=UTC),
            attributes={},
            confidence=Decimal("0.8"),
            inline_payload={"k": "v"},
        ),
        actor,
        "c",
    )
    with pytest.raises(ValidationFailedError):
        await svc.scan_and_promote(evidence.evidence_id, DummyMalwareScanner())


async def test_scanning_unknown_evidence_raises_not_found(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    svc = EvidenceService(ing_uow, storage=FakeObjectStorage())
    with pytest.raises(EvidenceNotFoundError):
        await svc.scan_and_promote(uuid4(), DummyMalwareScanner())


# --- evidence.scanned event (event-driven-architecture §25.2) ---------------


def _scanned_events(ing_uow) -> list[dict]:  # type: ignore[no-untyped-def]
    return [e for e in ing_uow.outbox.published if e["event_type"] == EVENT_EVIDENCE_SCANNED]


async def test_clean_scan_publishes_evidence_scanned(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence, _ = await _quarantined(ing_uow, actor, storage)
    await svc.scan_and_promote(
        evidence.evidence_id, DummyMalwareScanner(is_clean=True), correlation_id="corr-1"
    )
    published = _scanned_events(ing_uow)
    assert len(published) == 1
    event = published[0]
    assert event["aggregate_id"] == evidence.evidence_id
    assert event["correlation_id"] == "corr-1"
    assert event["payload"]["is_clean"] is True
    assert event["payload"]["detection_name"] is None
    assert event["payload"]["promoted"] is True


async def test_blocked_scan_also_publishes_evidence_scanned(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    """§25: every scan result is recorded — not only the failures."""
    storage = FakeObjectStorage()
    svc, evidence, _ = await _quarantined(ing_uow, actor, storage, category="osint")
    await svc.scan_and_promote(
        evidence.evidence_id, DummyMalwareScanner(is_clean=False, signature="Eicar-Test")
    )
    event = _scanned_events(ing_uow)[0]
    assert event["payload"]["is_clean"] is False
    assert event["payload"]["detection_name"] == "Eicar-Test"
    assert event["payload"]["promoted"] is False
    assert event["payload"]["forensic_exception"] is False


async def test_forensic_exception_is_visible_in_the_event(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence, _ = await _quarantined(
        ing_uow, actor, storage, category="digital_forensics", artifact_type="disk_image"
    )
    await svc.scan_and_promote(
        evidence.evidence_id, DummyMalwareScanner(is_clean=False, signature="Trojan.Win32")
    )
    payload = _scanned_events(ing_uow)[0]["payload"]
    assert (payload["promoted"], payload["forensic_exception"], payload["is_clean"]) == (
        True,
        True,
        False,
    )


async def test_scanned_event_carries_no_evidence_content(ing_uow, actor) -> None:
    """Thin event + reference (§18): identifiers and verdict only, never payload content."""
    storage = FakeObjectStorage()
    svc, evidence, _ = await _quarantined(ing_uow, actor, storage)
    await svc.scan_and_promote(evidence.evidence_id, DummyMalwareScanner(is_clean=True))
    payload = _scanned_events(ing_uow)[0]["payload"]
    assert set(payload) == {
        "evidence_id",
        "category",
        "is_clean",
        "detection_name",
        "promoted",
        "forensic_exception",
        "engine",
        # The uploader — a recipient identifier, not evidence content; the notification
        # consumer needs it to satisfy security §25 (§25.2 catalog).
        "collector_user_id",
    }


async def test_a_failed_scan_publishes_nothing(ing_uow, actor) -> None:  # type: ignore[no-untyped-def]
    storage = FakeObjectStorage()
    svc, evidence, _ = await _quarantined(ing_uow, actor, storage)
    with pytest.raises(RuntimeError):
        await svc.scan_and_promote(evidence.evidence_id, _ExplodingScanner())
    assert _scanned_events(ing_uow) == []
