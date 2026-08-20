"""ingestion business logic (guide Part 5) — real implementation.

Implements the Canonical Evidence Model's validation rules (CEM §13) and the
append-only, hash-chained custody ledger (CEM §4). Evidence core fields are
write-once (§13) — corrections go through supersession (§12), never mutation.

Presigned upload/download URLs go through ``platform.storage``'s ``ObjectStorage``
port (ADR-0008): this module addresses blobs by ``s3://bucket/key`` URI and never
imports an S3 client. Uploads are reserved into the quarantine bucket (ADR-0008 §2)
and ``verify_integrity`` recomputes a stored payload's digest server-side by
streaming it (ADR-0008 §3), recording the result on the custody ledger.

Malware scanning and promotion out of quarantine remain DEFERRED (ADR-0008 §2's
scan step); see ``jobs.py``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import Depends
from sqlalchemy.orm.attributes import set_committed_value

from sentinelai.modules.ingestion.events import (
    EVENT_EVIDENCE_INGESTED,
    EVENT_EVIDENCE_SCANNED,
    EVENT_EVIDENCE_SUPERSEDED,
    EVENT_EVIDENCE_VALIDATION_FAILED,
)
from sentinelai.modules.ingestion.exceptions import (
    ConnectorNotFoundError,
    EvidenceAlreadySupersededError,
    EvidenceNotFoundError,
    EvidencePayloadMissingError,
    IntegrityVerificationFailedError,
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
from sentinelai.platform.config import settings
from sentinelai.platform.security.digest import (
    compute_stream_digest,
    digests_match,
    is_valid_digest,
)
from sentinelai.platform.security.scanner import MalwareScanner
from sentinelai.platform.storage import (
    ObjectNotFound,
    ObjectStorage,
    build_object_uri,
    get_object_storage,
    parse_object_uri,
)
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
# Presigned URLs are short-lived bearer credentials (ADR-0008 §6, api-design §2.11).
_PRESIGN_TTL_SECONDS = 900


# security-architecture §25's deliberate domain carve-out: for these categories a malware
# detection is EVIDENCE, recorded as metadata, and must not block promotion — deleting or
# withholding evidence of malware defeats the investigation it is evidence for.
FORENSIC_CATEGORIES = frozenset({"digital_forensics", "mobile_forensics"})

# Background jobs act as the platform itself, not as a user. `actor_user_id` is the nil UUID and
# the role is `system` (the role api-design.md already grants to non-interactive callers).
SYSTEM_ACTOR = CurrentUser(user_id=UUID(int=0), roles=("system",))


def _payload_key(evidence_id: UUID, category: str, artifact_type: str) -> str:
    """Object key for an evidence payload — deterministic, so a reserved upload and its later
    ``payload_ref`` name the same object without the server storing the reservation."""
    return f"evidence/{category}/{artifact_type}/{evidence_id}"


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

    def __init__(self, uow: IngestionUnitOfWork, *, storage: ObjectStorage) -> None:
        self._uow = uow
        self._storage = storage

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
        """Reserve an ``evidence_id`` and return a short-lived presigned PUT URL (api-design §2.11).

        No evidence row is written here — the reservation is just an id plus a place to put bytes;
        ``POST /evidence`` creates the record with ``payload_ref`` once the upload has happened
        (api-design §13's sequence). The object key is deterministic, so the client can name the
        same object in that finalize call: ``evidence/{category}/{artifact_type}/{evidence_id}``.

        The upload lands in the **quarantine** bucket (ADR-0008 §2) — uploaded bytes are never
        placed directly into the evidence bucket. Promotion out of quarantine happens only after a
        clean malware scan, which is a later increment.
        """
        if category not in VALID_CATEGORIES:
            raise ValidationFailedError(
                [{"field": "category", "message": f"unknown evidence category '{category}'"}]
            )
        if not artifact_type:
            raise ValidationFailedError([{"field": "artifact_type", "message": "required"}])

        evidence_id = uuid4()
        bucket = settings.storage_quarantine_bucket
        key = _payload_key(evidence_id, category, artifact_type)
        url = await self._storage.presigned_upload_url(bucket, key, expires_in=_PRESIGN_TTL_SECONDS)
        # The URL is a bearer credential — audit that a reservation happened, never the URL itself.
        await self._audit(
            actor,
            "evidence.upload_reserved",
            evidence_id,
            {"category": category, "artifact_type": artifact_type},
        )
        return UploadReservationRead(evidence_id=evidence_id, upload_url=url)

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

    async def _overlay_derived_state(self, evidence: Evidence) -> Evidence:
        """Populate the three derived fields from append-only records (ADR-0015).

        ``set_committed_value`` writes the attribute *as if loaded from the database*, so the
        unit of work never marks it dirty and never emits an ``UPDATE`` — which the ADR-0004
        trigger on ``ingestion.evidence`` would reject. The columns hold genesis values only;
        this overlay is what makes every consumer (response schemas, legal-hold gates,
        already-superseded guards) see current truth.
        """
        # status (CEM §12): superseded iff a replacement row points at this item.
        if await self._uow.evidence.has_replacement(evidence.evidence_id):
            set_committed_value(evidence, "status", "superseded")
        # legal_hold (ADR-0004 §4): the latest hold event on the custody ledger wins.
        hold = await self._uow.custody.last_of_types(
            evidence.evidence_id, ("legal_hold_applied", "legal_hold_released")
        )
        if hold is not None:
            set_committed_value(evidence, "legal_hold", hold.event_type == "legal_hold_applied")
        # integrity_verification_status (ADR-0008 §3): latest server recomputation vs recorded
        # hash — a semantic digest comparison, never a parse of event notes.
        # payload location (ADR-0008 §2): a `transferred` event means the object was server-side
        # copied out of quarantine into the evidence bucket under the same key. The genesis
        # `payload_ref` names quarantine and cannot be UPDATEd (ADR-0015), so current location is
        # derived — keyed on the event TYPE, never on note text.
        if evidence.payload_ref:
            moved = await self._uow.custody.last_of_types(evidence.evidence_id, ("transferred",))
            if moved is not None:
                _, key = parse_object_uri(evidence.payload_ref)
                set_committed_value(
                    evidence, "payload_ref", build_object_uri(settings.storage_bucket, key)
                )
        if evidence.payload_ref and evidence.integrity_hash:
            check = await self._uow.custody.last_of_types(
                evidence.evidence_id, ("integrity_reverified",)
            )
            if check is not None:
                matched = digests_match(evidence.integrity_hash, check.integrity_hash_at_event)
                set_committed_value(
                    evidence,
                    "integrity_verification_status",
                    "verified" if matched else "failed",
                )
        return evidence

    async def get_evidence(self, evidence_id: UUID, actor: CurrentUser) -> Evidence:
        evidence = await self._uow.evidence.get_by_id(evidence_id)
        if evidence is None:
            raise EvidenceNotFoundError()
        return await self._overlay_derived_state(evidence)

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
        items = [await self._overlay_derived_state(row) for row in rows[: page.limit]]
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = encode_cursor(last.ingested_at.isoformat(), last.evidence_id)
        return items, next_cursor, has_more

    async def get_download_url(self, evidence_id: UUID, actor: CurrentUser) -> str:
        """Return a short-lived presigned GET URL and record the access (api-design §4.2).

        A download is a disclosure-significant act: it appends an ``accessed`` entry to the custody
        ledger (CEM §4) and writes a ``platform.audit_log`` row (api-design §8's rule for the same
        pattern on report downloads). Presigning happens first — if the ledger write fails, the
        transaction rolls back and the URL is never returned to the caller.
        """
        evidence = await self.get_evidence(evidence_id, actor)
        if not evidence.payload_ref:
            raise ValidationFailedError(
                [{"field": "payload_ref", "message": "evidence has no stored payload to download"}]
            )
        bucket, key = parse_object_uri(evidence.payload_ref)
        url = await self._storage.presigned_download_url(
            bucket, key, expires_in=_PRESIGN_TTL_SECONDS
        )
        await self._append_custody(
            evidence_id,
            "accessed",
            actor,
            integrity_hash_at_event=(evidence.integrity_hash or _GENESIS_HASH),
        )
        await self._audit(
            actor, "evidence.downloaded", evidence_id, {"category": evidence.category}
        )
        return url

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
        # Legal hold is ledger-derived (ADR-0015 / ADR-0004 §4): the custody event appended
        # below IS the state transition. Never an UPDATE — the ADR-0004 trigger forbids it;
        # the overlay keeps this in-memory instance consistent without dirtying it.
        if data.event_type == "legal_hold_applied":
            set_committed_value(evidence, "legal_hold", True)
        elif data.event_type == "legal_hold_released":
            set_committed_value(evidence, "legal_hold", False)
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
        return event

    async def verify_integrity(self, evidence_id: UUID, actor: CurrentUser) -> Evidence:
        """Recompute the stored payload's digest server-side and record the result (ADR-0008 §3).

        The server-computed digest is authoritative: the recorded ``integrity_hash`` is treated as a
        *claim* to be checked against the bytes actually in storage, never trusted. The object is
        streamed, so a multi-GB image is never held in memory.

        The outcome is recorded on the append-only custody ledger as an ``integrity_reverified``
        event (CEM §4) carrying the **recomputed** digest — on both success and mismatch, so a
        failure is auditable rather than silent. A mismatch then raises, and nothing is ever marked
        verified. Both the ledger write and the audit row commit in the caller's single UoW
        transaction; object storage stays outside it (blobs are not transactional).
        """
        evidence = await self.get_evidence(evidence_id, actor)
        if not evidence.payload_ref:
            raise ValidationFailedError(
                [{"field": "payload_ref", "message": "evidence has no stored payload to verify"}]
            )
        algorithm = evidence.integrity_algorithm or "SHA-256"
        if algorithm not in _ALLOWED_INTEGRITY_ALGORITHMS:
            raise ValidationFailedError(
                [
                    {
                        "field": "integrity_algorithm",
                        "message": f"unsupported algorithm '{algorithm}'",
                    }
                ]
            )
        expected = evidence.integrity_hash
        if not expected or not is_valid_digest(expected, algorithm):
            raise ValidationFailedError(
                [{"field": "integrity_hash", "message": f"not a valid {algorithm} digest"}]
            )

        bucket, key = parse_object_uri(evidence.payload_ref)
        if not await self._storage.exists(bucket, key):
            # Distinct from a mismatch: the bytes are gone, not different.
            raise EvidencePayloadMissingError()
        try:
            digest = await compute_stream_digest(self._storage.get_stream(bucket, key), algorithm)
        except ObjectNotFound as exc:  # deleted between the existence check and the read
            raise EvidencePayloadMissingError() from exc
        matched = digests_match(expected, digest.hex_digest)

        # Record the recomputed digest either way — the ledger is the auditable record of the
        # check AND the persistent state transition (ADR-0015): `integrity_verification_status`
        # is derived from this event at read time, never UPDATEd on the row.
        await self._append_custody(
            evidence_id,
            "integrity_reverified",
            actor,
            integrity_hash_at_event=digest.hex_digest,
            notes=("integrity verified" if matched else "integrity MISMATCH against recorded hash"),
        )
        set_committed_value(
            evidence, "integrity_verification_status", "verified" if matched else "failed"
        )
        await self._audit(
            actor,
            "evidence.integrity_verified",
            evidence_id,
            {"algorithm": algorithm, "matched": matched, "size_bytes": digest.size_bytes},
        )
        if not matched:
            raise IntegrityVerificationFailedError()
        return evidence

    async def scan_and_promote(
        self, evidence_id: UUID, scanner: MalwareScanner, correlation_id: str | None = None
    ) -> Evidence:
        """Scan the quarantined object and promote it if policy allows (ADR-0008 §2, §25).

        Called by the ``scan_uploaded_evidence`` background job — the scanner is passed in rather
        than held on the service because only this path needs one.

        **security-architecture §25 carve-out.** A detection blocks promotion for every category
        *except* `digital_forensics`/`mobile_forensics`, where a malware sample is legitimately the
        evidence itself; those are promoted with the detection recorded on the ledger. A scan that
        cannot run raises — it is never treated as clean.

        Promotion is a server-side copy followed by deletion from quarantine; no byte transits this
        process. Idempotent under arq retries: an already-promoted item returns unchanged, and an
        object found already in the evidence bucket (a crash between copy and commit) is recorded
        rather than reported missing.
        """
        evidence = await self._uow.evidence.get_by_id(evidence_id)
        if evidence is None:
            raise EvidenceNotFoundError()
        if not evidence.payload_ref:
            raise ValidationFailedError(
                [{"field": "payload_ref", "message": "evidence has no stored payload to scan"}]
            )
        evidence = await self._overlay_derived_state(evidence)
        # Re-read after the overlay: it may have repointed payload_ref at the evidence bucket.
        payload_ref = evidence.payload_ref or ""
        bucket, key = parse_object_uri(payload_ref)
        if bucket == settings.storage_bucket:
            return evidence  # already promoted — a retry of a completed job

        integrity_hash = evidence.integrity_hash or _GENESIS_HASH
        if not await self._storage.exists(bucket, key):
            if await self._storage.exists(settings.storage_bucket, key):
                # Copy+delete succeeded but the transaction did not commit; record the promotion.
                await self._append_custody(
                    evidence_id,
                    "transferred",
                    SYSTEM_ACTOR,
                    integrity_hash_at_event=integrity_hash,
                    notes="promoted from quarantine (recovered: copy completed before commit)",
                )
                return await self._overlay_derived_state(evidence)
            raise EvidencePayloadMissingError()

        result = await scanner.scan(self._storage.get_stream(bucket, key))
        forensic_exception = not result.is_clean and evidence.category in FORENSIC_CATEGORIES
        promote = result.is_clean or forensic_exception

        if promote:
            await self._storage.copy_object(bucket, key, settings.storage_bucket, key)
            await self._storage.delete(bucket, key)
            note = (
                f"promoted from quarantine; scan clean (engine={result.engine})"
                if result.is_clean
                else (
                    f"promoted from quarantine; malware detected "
                    f"({result.signature or 'unnamed'}, engine={result.engine}) — retained as "
                    f"evidence under security-architecture §25 forensic exception"
                )
            )
            await self._append_custody(
                evidence_id,
                "transferred",
                SYSTEM_ACTOR,
                integrity_hash_at_event=integrity_hash,
                notes=note,
            )
            set_committed_value(
                evidence, "payload_ref", build_object_uri(settings.storage_bucket, key)
            )
        else:
            # Blocked: the object stays in quarantine, which is never on any serving path (§24).
            await self._append_custody(
                evidence_id,
                "analyzed",
                SYSTEM_ACTOR,
                integrity_hash_at_event=integrity_hash,
                notes=(
                    f"promotion BLOCKED; malware detected "
                    f"({result.signature or 'unnamed'}, engine={result.engine}) — object retained "
                    f"in quarantine"
                ),
            )

        # Outbox insert in the SAME transaction as the custody write (guide Part 6) — published
        # on every outcome, clean or blocked (§25: every scan result is recorded, not just
        # failures). Thin payload: enough to decide whether to care, no evidence content.
        await self._uow.outbox.publish(
            event_type=EVENT_EVIDENCE_SCANNED,
            aggregate_type="evidence",
            aggregate_id=evidence_id,
            payload={
                "evidence_id": str(evidence_id),
                "category": evidence.category,
                "is_clean": result.is_clean,
                "detection_name": result.signature,
                "promoted": promote,
                "forensic_exception": forensic_exception,
                "engine": result.engine,
                # The uploading analyst — security §25 requires notifying them on a block, and
                # a consumer cannot name a recipient without it (§18's thin-event rule: carry
                # what the common-case consumer needs). Same field `evidence.ingested` carries.
                "collector_user_id": str(evidence.collector_user_id),
            },
            # A background job mints a correlation id when it isn't continuing one (§11).
            correlation_id=correlation_id or str(uuid4()),
            actor_type="system",
        )
        await self._audit(
            SYSTEM_ACTOR,
            EVENT_EVIDENCE_SCANNED,
            evidence_id,
            {
                "engine": result.engine,
                "is_clean": result.is_clean,
                "signature": result.signature,
                "promoted": promote,
                "forensic_exception": forensic_exception,
            },
        )
        return evidence

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
        # Derived check (ADR-0015): superseded means a replacement row exists — the genesis
        # `status` column never changes.
        if await self._uow.evidence.has_replacement(evidence_id):
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
        # The replacement row's `supersedes_evidence_id` IS the state transition (ADR-0015);
        # the original row is never written. Overlay keeps the in-memory instance truthful.
        set_committed_value(original, "status", "superseded")
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
        return connector

    async def list_attribute_schemas(self, actor: CurrentUser) -> Sequence[AttributeSchemaRegistry]:
        return await self._uow.attribute_schemas.list_()


def get_evidence_service(
    uow: IngestionUnitOfWork = Depends(get_ingestion_uow),
    storage: ObjectStorage = Depends(get_object_storage),
) -> EvidenceService:
    return EvidenceService(uow, storage=storage)


__all__ = ["CUSTODY_EVENT_TYPES", "VALID_CATEGORIES", "EvidenceService", "get_evidence_service"]
