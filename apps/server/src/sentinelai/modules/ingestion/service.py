"""ingestion business logic (guide Part 5) — real implementation.

Implements the Canonical Evidence Model's validation rules (CEM §13) and the
append-only, hash-chained custody ledger (CEM §4). Evidence core fields are
write-once (§13) — corrections go through supersession (§12), never mutation.

Storage-dependent operations (presigned upload/download URLs, integrity
re-verification against the stored object, malware scanning) are DEFERRED — they
require ``platform/storage.py`` (object storage client), which is not built yet.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import Depends

from sentinelai.modules.ingestion.events import (
    EVENT_EVIDENCE_INGESTED,
    EVENT_EVIDENCE_SUPERSEDED,
    EVENT_EVIDENCE_VALIDATION_FAILED,
)
from sentinelai.modules.ingestion.exceptions import (
    ConnectorNotFoundError,
    EvidenceAlreadySupersededError,
    EvidenceNotFoundError,
)
from sentinelai.modules.ingestion.models import (
    AttributeSchemaRegistry,
    ConnectorRegistry,
    Evidence,
    EvidenceCustodyEvent,
    IntakeRecord,
)
from sentinelai.modules.ingestion.repository import IngestionUnitOfWork, get_ingestion_uow
from sentinelai.modules.ingestion.schemas import (
    ConnectorCreate,
    ConnectorUpdate,
    CustodyEventCreate,
    EvidenceCreate,
    EvidenceSupersedeCreate,
    UploadReservationRead,
)
from sentinelai.platform.auth.audit import record_audit_event
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.shared.exceptions import LegalHoldViolationError, ValidationFailedError
from sentinelai.shared.pagination import PageParams, decode_cursor, encode_cursor

# CEM §5 evidence categories.
VALID_CATEGORIES = frozenset(
    {
        "digital_forensics",
        "mobile_forensics",
        "osint",
        "threat_intelligence",
        "social_media_intelligence",
        "blockchain_intelligence",
        "drone_iot",
        "cloud_evidence",
        "manual",
    }
)
# CEM §13: categories that require a legal authority reference (or the sentinel).
_LEGAL_AUTHORITY_REQUIRED = frozenset(
    {
        "digital_forensics",
        "mobile_forensics",
        "social_media_intelligence",
        "cloud_evidence",
    }
)
_PUBLIC_SOURCE_SENTINEL = "public_source_no_authority_required"
_ALLOWED_INTEGRITY_ALGORITHMS = frozenset({"SHA-256", "SHA-3-256", "SHA-512"})
# CEM §4 custody event-type enum.
CUSTODY_EVENT_TYPES = frozenset(
    {
        "collected",
        "ingested",
        "accessed",
        "exported",
        "transferred",
        "analyzed",
        "integrity_reverified",
        "linked_to_case",
        "unlinked_from_case",
        "legal_hold_applied",
        "legal_hold_released",
        "disposed",
    }
)
# §13 clock-skew tolerance value is unspecified in the docs — assumption.
_CLOCK_SKEW = timedelta(minutes=5)
_GENESIS_HASH = "0" * 64


def _actor_role(actor: CurrentUser) -> str:
    return actor.roles[0] if actor.roles else "unknown"


def _custody_entry_hash(
    prev_hash: str,
    evidence_id: UUID,
    sequence_number: int,
    event_type: str,
    integrity_hash_at_event: str,
    occurred_at: datetime,
) -> str:
    preimage = json.dumps(
        {
            "prev": prev_hash,
            "evidence_id": str(evidence_id),
            "seq": sequence_number,
            "event_type": event_type,
            "integrity_hash_at_event": integrity_hash_at_event,
            "occurred_at": occurred_at.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(preimage.encode()).hexdigest()


class EvidenceService:
    """Intake, canonicalization, custody, and integrity for evidence."""

    def __init__(self, uow: IngestionUnitOfWork) -> None:
        self._uow = uow

    async def _audit(
        self, actor: CurrentUser, action: str, target_id: UUID, details: dict[str, object]
    ) -> None:
        await record_audit_event(
            self._uow.session,
            actor_user_id=actor.user_id,
            actor_role=_actor_role(actor),
            action=action,
            module="ingestion",
            target_type="evidence",
            target_id=target_id,
            details=details,
        )

    async def _append_custody(
        self,
        evidence_id: UUID,
        event_type: str,
        actor: CurrentUser,
        *,
        integrity_hash_at_event: str,
        authority_ref: str | None = None,
        notes: str | None = None,
    ) -> EvidenceCustodyEvent:
        last = await self._uow.custody.last_entry(evidence_id)
        prev_hash = last.entry_hash if last is not None else _GENESIS_HASH
        sequence_number = (last.sequence_number + 1) if last is not None else 1
        occurred_at = datetime.now(UTC)
        entry_hash = _custody_entry_hash(
            prev_hash,
            evidence_id,
            sequence_number,
            event_type,
            integrity_hash_at_event,
            occurred_at,
        )
        event = EvidenceCustodyEvent(
            evidence_id=evidence_id,
            sequence_number=sequence_number,
            event_type=event_type,
            occurred_at=occurred_at,
            actor_user_id=actor.user_id,
            actor_role=_actor_role(actor),
            authority_ref=authority_ref,
            notes=notes,
            integrity_hash_at_event=integrity_hash_at_event,
            # CEM §4: genesis has null prev; the column is NOT NULL, so a sentinel is stored.
            prev_event_hash=prev_hash,
            entry_hash=entry_hash,
        )
        await self._uow.custody.add(event)
        return event

    @staticmethod
    def _validate(data: EvidenceCreate, ingested_at: datetime) -> list[dict[str, str]]:
        """CEM §13 rules checkable without object storage or a stored attribute schema."""
        errors: list[dict[str, str]] = []
        if data.category not in VALID_CATEGORIES:
            errors.append({"field": "category", "message": f"unknown category '{data.category}'"})
        if not data.source.get("system"):
            errors.append({"field": "source.system", "message": "required (provenance)"})
        if not data.source.get("collector_id"):
            errors.append({"field": "source.collector_id", "message": "required (provenance)"})
        if data.collected_at > ingested_at + _CLOCK_SKEW:
            errors.append(
                {"field": "collected_at", "message": "is after ingested_at beyond tolerance"}
            )
        if data.payload_ref is not None:
            if not data.integrity_hash or not data.integrity_algorithm:
                errors.append(
                    {
                        "field": "integrity",
                        "message": "hash+algorithm required for payload-bearing evidence",
                    }
                )
            elif data.integrity_algorithm not in _ALLOWED_INTEGRITY_ALGORITHMS:
                errors.append(
                    {
                        "field": "integrity_algorithm",
                        "message": f"must be one of {sorted(_ALLOWED_INTEGRITY_ALGORITHMS)}",
                    }
                )
        if data.category in _LEGAL_AUTHORITY_REQUIRED and not data.legal_authority_ref:
            errors.append(
                {
                    "field": "legal_authority_ref",
                    "message": f"required for {data.category} (or '{_PUBLIC_SOURCE_SENTINEL}')",
                }
            )
        return errors

    async def reserve_upload(
        self, category: str, artifact_type: str, actor: CurrentUser
    ) -> UploadReservationRead:
        raise NotImplementedError(
            "presigned upload URL requires platform/storage.py (object storage) — not built yet"
        )

    async def ingest_evidence(
        self, data: EvidenceCreate, actor: CurrentUser, correlation_id: str
    ) -> Evidence:
        ingested_at = datetime.now(UTC)
        errors = self._validate(data, ingested_at)
        if not errors and not await self._uow.attribute_schemas.is_registered(
            data.schema_version, data.category, data.artifact_type
        ):
            errors.append(
                {
                    "field": "schema_version",
                    "message": "(schema_version, category, artifact_type) is not registered",
                }
            )
        if errors:
            intake = IntakeRecord(
                connector_name=str(data.source.get("system", "unknown")),
                raw_payload_ref=data.payload_ref or "inline",
                validation_status="failed",
                validation_errors={"errors": errors},
                received_at=ingested_at,
                resulting_evidence_id=None,
            )
            await self._uow.intake.add(intake)
            await self._uow.outbox.publish(
                event_type=EVENT_EVIDENCE_VALIDATION_FAILED,
                aggregate_type="intake",
                aggregate_id=intake.intake_id,
                payload={"intake_id": str(intake.intake_id), "errors": errors},
                correlation_id=correlation_id,
                actor_type="user",
                actor_ref=actor.user_id,
            )
            await self._uow.commit()
            raise ValidationFailedError(errors)

        # Inline payload can be hashed deterministically now; a payload_ref object's
        # hash is the client-declared one (re-verification against storage is deferred).
        if data.integrity_hash:
            genesis_hash = data.integrity_hash
        elif data.inline_payload is not None:
            genesis_hash = hashlib.sha256(
                json.dumps(data.inline_payload, sort_keys=True).encode()
            ).hexdigest()
        else:
            genesis_hash = _GENESIS_HASH

        evidence = Evidence(
            schema_version=data.schema_version,
            category=data.category,
            artifact_type=data.artifact_type,
            title=data.title,
            description=data.description,
            source=data.source,
            collected_at=data.collected_at,
            ingested_at=ingested_at,
            integrity_algorithm=data.integrity_algorithm,
            integrity_hash=data.integrity_hash,
            integrity_verification_status=("pending" if data.payload_ref else "not_applicable"),
            payload_ref=data.payload_ref,
            inline_payload=data.inline_payload,
            attributes=data.attributes,
            confidence=data.confidence,
            reliability_rating=data.reliability_rating,
            legal_authority_ref=data.legal_authority_ref,
            status="validated",
            supersedes_evidence_id=None,
            collector_user_id=actor.user_id,
            retention_policy_ref=data.retention_policy_ref,
            legal_hold=False,
        )
        await self._uow.evidence.add(evidence)
        await self._append_custody(
            evidence.evidence_id, "ingested", actor, integrity_hash_at_event=genesis_hash
        )
        await self._uow.outbox.publish(
            event_type=EVENT_EVIDENCE_INGESTED,
            aggregate_type="evidence",
            aggregate_id=evidence.evidence_id,
            payload={
                "evidence_id": str(evidence.evidence_id),
                "category": evidence.category,
                "artifact_type": evidence.artifact_type,
                "collected_at": evidence.collected_at.isoformat(),
                "collector_user_id": str(evidence.collector_user_id),
            },
            correlation_id=correlation_id,
            actor_type="user",
            actor_ref=actor.user_id,
        )
        await self._audit(
            actor, EVENT_EVIDENCE_INGESTED, evidence.evidence_id, {"category": evidence.category}
        )
        await self._uow.commit()
        return evidence

    async def ingest_batch(
        self, items: Sequence[EvidenceCreate], actor: CurrentUser, correlation_id: str
    ) -> list[dict[str, object]]:
        """Per-item results (api-design §2.10) — one item's failure never fails the batch."""
        results: list[dict[str, object]] = []
        for index, item in enumerate(items):
            try:
                evidence = await self.ingest_evidence(item, actor, correlation_id)
                results.append(
                    {"index": index, "status": "created", "evidence_id": str(evidence.evidence_id)}
                )
            except ValidationFailedError as exc:
                results.append(
                    {
                        "index": index,
                        "status": "error",
                        "error": {"code": exc.code, "details": exc.details},
                    }
                )
        return results

    async def get_evidence(self, evidence_id: UUID, actor: CurrentUser) -> Evidence:
        evidence = await self._uow.evidence.get_by_id(evidence_id)
        if evidence is None:
            raise EvidenceNotFoundError()
        return evidence

    async def list_evidence(
        self,
        actor: CurrentUser,
        category: str | None,
        artifact_type: str | None,
        status_filter: str | None,
        text: str | None,
        page: PageParams,
    ) -> tuple[list[Evidence], str | None, bool]:
        cursor_ingested_at: datetime | None = None
        cursor_evidence_id: UUID | None = None
        if page.cursor:
            raw_value, cursor_evidence_id = decode_cursor(page.cursor)
            cursor_ingested_at = datetime.fromisoformat(raw_value)
        rows = await self._uow.evidence.list_(
            category=category,
            artifact_type=artifact_type,
            status=status_filter,
            text=text,
            limit=page.limit,
            cursor_ingested_at=cursor_ingested_at,
            cursor_evidence_id=cursor_evidence_id,
        )
        has_more = len(rows) > page.limit
        items = list(rows[: page.limit])
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = encode_cursor(last.ingested_at.isoformat(), last.evidence_id)
        return items, next_cursor, has_more

    async def get_download_url(self, evidence_id: UUID, actor: CurrentUser) -> str:
        raise NotImplementedError(
            "presigned download URL + 'accessed' custody event require "
            "platform/storage.py - not built yet"
        )

    async def list_custody_events(
        self, evidence_id: UUID, actor: CurrentUser
    ) -> Sequence[EvidenceCustodyEvent]:
        await self.get_evidence(evidence_id, actor)
        return await self._uow.custody.list_for_evidence(evidence_id)

    async def record_custody_event(
        self, evidence_id: UUID, data: CustodyEventCreate, actor: CurrentUser, correlation_id: str
    ) -> EvidenceCustodyEvent:
        evidence = await self.get_evidence(evidence_id, actor)
        if data.event_type not in CUSTODY_EVENT_TYPES:
            raise ValidationFailedError(
                [{"field": "event_type", "message": f"unknown custody event '{data.event_type}'"}]
            )
        # Legal-hold gate on any disposal/purge path (security §39).
        if data.event_type == "disposed" and evidence.legal_hold:
            raise LegalHoldViolationError("evidence is under legal hold and cannot be disposed")
        if data.event_type == "legal_hold_applied":
            evidence.legal_hold = True
        elif data.event_type == "legal_hold_released":
            evidence.legal_hold = False
        event = await self._append_custody(
            evidence_id,
            data.event_type,
            actor,
            integrity_hash_at_event=(evidence.integrity_hash or _GENESIS_HASH),
            authority_ref=data.authority_ref,
            notes=data.notes,
        )
        await self._audit(
            actor, "evidence.custody_event", evidence_id, {"event_type": data.event_type}
        )
        await self._uow.commit()
        return event

    async def verify_integrity(self, evidence_id: UUID, actor: CurrentUser) -> Evidence:
        raise NotImplementedError(
            "recomputing the payload hash requires reading the stored object via "
            "platform/storage.py — not built yet"
        )

    async def supersede_evidence(
        self,
        evidence_id: UUID,
        data: EvidenceSupersedeCreate,
        actor: CurrentUser,
        correlation_id: str,
    ) -> Evidence:
        original = await self._uow.evidence.get_by_id(evidence_id)
        if original is None:
            raise EvidenceNotFoundError()
        if original.status == "superseded":
            raise EvidenceAlreadySupersededError(f"evidence {evidence_id} is already superseded")

        ingested_at = datetime.now(UTC)
        errors = self._validate(data.replacement, ingested_at)
        if errors:
            raise ValidationFailedError(errors)
        replacement = Evidence(
            schema_version=data.replacement.schema_version,
            category=data.replacement.category,
            artifact_type=data.replacement.artifact_type,
            title=data.replacement.title,
            description=data.replacement.description,
            source=data.replacement.source,
            collected_at=data.replacement.collected_at,
            ingested_at=ingested_at,
            integrity_algorithm=data.replacement.integrity_algorithm,
            integrity_hash=data.replacement.integrity_hash,
            integrity_verification_status=(
                "pending" if data.replacement.payload_ref else "not_applicable"
            ),
            payload_ref=data.replacement.payload_ref,
            inline_payload=data.replacement.inline_payload,
            attributes=data.replacement.attributes,
            confidence=data.replacement.confidence,
            reliability_rating=data.replacement.reliability_rating,
            legal_authority_ref=data.replacement.legal_authority_ref,
            status="validated",
            supersedes_evidence_id=evidence_id,
            collector_user_id=actor.user_id,
            retention_policy_ref=data.replacement.retention_policy_ref,
            legal_hold=False,
        )
        await self._uow.evidence.add(replacement)
        original.status = "superseded"
        await self._append_custody(
            replacement.evidence_id,
            "ingested",
            actor,
            integrity_hash_at_event=(replacement.integrity_hash or _GENESIS_HASH),
            notes=f"supersedes {evidence_id}: {data.reason}",
        )
        await self._uow.outbox.publish(
            event_type=EVENT_EVIDENCE_SUPERSEDED,
            aggregate_type="evidence",
            aggregate_id=evidence_id,
            payload={
                "evidence_id": str(evidence_id),
                "supersedes_by_evidence_id": str(replacement.evidence_id),
            },
            correlation_id=correlation_id,
            actor_type="user",
            actor_ref=actor.user_id,
        )
        await self._audit(actor, EVENT_EVIDENCE_SUPERSEDED, evidence_id, {"reason": data.reason})
        await self._uow.commit()
        return replacement

    async def exists(self, evidence_id: UUID) -> bool:
        """Cross-module hook (ingestion.public) — used by case_management at link time."""
        return await self._uow.evidence.exists(evidence_id)

    async def list_connectors(self, actor: CurrentUser) -> Sequence[ConnectorRegistry]:
        return await self._uow.connectors.list_()

    async def register_connector(
        self, data: ConnectorCreate, actor: CurrentUser, correlation_id: str
    ) -> ConnectorRegistry:
        connector = ConnectorRegistry(
            name=data.name,
            owning_module=data.owning_module,
            mapping_profile_version=data.mapping_profile_version,
        )
        await self._uow.connectors.add(connector)
        await self._audit(
            actor, "connector.registered", connector.connector_id, {"name": data.name}
        )
        await self._uow.commit()
        return connector

    async def update_connector(
        self, connector_id: UUID, data: ConnectorUpdate, actor: CurrentUser, expected_etag: str
    ) -> ConnectorRegistry:
        connector = await self._uow.connectors.get_by_id(connector_id)
        if connector is None:
            raise ConnectorNotFoundError()
        changes = data.model_dump(exclude_unset=True)
        for field, value in changes.items():
            if value is not None:
                setattr(connector, field, value)
        await self._audit(actor, "connector.updated", connector_id, {"fields": sorted(changes)})
        await self._uow.commit()
        return connector

    async def list_attribute_schemas(self, actor: CurrentUser) -> Sequence[AttributeSchemaRegistry]:
        return await self._uow.attribute_schemas.list_()


def get_evidence_service(
    uow: IngestionUnitOfWork = Depends(get_ingestion_uow),
) -> EvidenceService:
    return EvidenceService(uow)


__all__ = ["CUSTODY_EVENT_TYPES", "VALID_CATEGORIES", "EvidenceService", "get_evidence_service"]
