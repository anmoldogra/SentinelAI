"""osint business logic (guide Part 5) — source config, finding capture, and publishing a finding
into the canonical evidence model.

**The publish path is CEM §9's pipeline, and only the middle of it lives here.** Extract is the
connector's (the raw output is what `raw_attributes` holds); Map is this module's; Enrich, Validate
and Commit are `ingestion`'s, reached through its `public.py` interface. That split is deliberate:
§13's validation rules and the custody genesis entry belong to the module that owns the evidence
table, and re-implementing either here would give the platform two places to disagree about whether
an evidence object is admissible.

**Why this calls `ingestion` rather than only publishing an event.** `api-design.md` §4.3 specifies
a
`200` whose body carries `evidence_id`, and an `ingestion.evidence_custody_events` genesis entry, as
publish's outcome — a contract an outbox hand-off cannot satisfy, because the evidence would not
exist
yet when the response was written. The call goes through `ingestion.public`, which `CLAUDE.md` names
as
a sanctioned cross-module path ("only through that module's `public.py`"); what is forbidden is
importing another module's `models.py`/`repository.py` or touching its tables, and neither happens
here. `osint.finding_captured` still goes to this schema's own outbox, and `evidence.ingested` is
published by `ingestion` on its own — §4.3's "indirectly".
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from fastapi import Depends

from sentinelai.modules.ingestion.public import (
    EvidenceCreate,
    EvidenceService,
    get_evidence_service,
)
from sentinelai.modules.osint.events import (
    EVENT_FINDING_CAPTURED,
    EVENT_SOURCE_ACTIVATED,
    EVENT_SOURCE_DEACTIVATED,
)
from sentinelai.modules.osint.exceptions import (
    FindingAlreadyPublishedError,
    FindingNotFoundError,
    SourceNotFoundError,
)
from sentinelai.modules.osint.models import OsintFinding, OsintSource
from sentinelai.modules.osint.repository import OsintUnitOfWork, get_osint_uow
from sentinelai.modules.osint.schemas import FindingCreate, SourceCreate, SourceUpdate
from sentinelai.platform.auth.audit import record_audit_event
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.shared.cem import PUBLIC_SOURCE_AUTHORITY
from sentinelai.shared.exceptions import PreconditionFailedError, ValidationFailedError
from sentinelai.shared.pagination import PageParams, decode_cursor

_MODULE = "osint"
# CEM §5's category for open-source intelligence. Fixed, not client-supplied: a finding that reached
# this module through the OSINT connector surface is OSINT evidence, and letting a payload field say
# otherwise would let a connector mislabel its own provenance.
CATEGORY_OSINT = "osint"

# The finding lifecycle. `database-design.md` §3.3 requires a `status` column and api-design.md §4.3
# fixes the post-publish value (`published`); the pre-publish value is not documented anywhere, so
# `captured` is an assumption recorded here rather than in code alone — it matches the event name
# (`osint.finding_captured`) so the two cannot drift into describing different things.
STATUS_CAPTURED = "captured"
STATUS_PUBLISHED = "published"

# What `raw_attributes` must carry before a finding can be mapped onto the CEM. See `_map_to_cem`.
_REQUIRED_FOR_PUBLISH = ("schema_version", "artifact_type", "title", "attributes", "confidence")


def source_etag(source: OsintSource) -> str:
    """A weak ETag over the source's *mutable* fields (api-design.md §2.6).

    Only `reliability_baseline` and `is_active` can change (§4.3: "Update reliability baseline /
    activate-deactivate"), so only those are in the digest. Including immutable fields would make
    the
    ETag churn on nothing; including the id alone would make it never change and the `If-Match`
    guard
    decorative.
    """
    material = f"{source.source_id}|{source.reliability_baseline}|{source.is_active}"
    return f'W/"{hashlib.sha256(material.encode()).hexdigest()[:32]}"'


def _normalize_etag(value: str) -> str:
    """Compare ETags by their opaque value, ignoring the `W/` marker and quoting a client adds."""
    return value.strip().removeprefix("W/").strip('"')


class OsintService:
    def __init__(
        self,
        uow: OsintUnitOfWork,
        *,
        evidence: EvidenceService,
        kms: KeyManagementService,
    ) -> None:
        self._uow = uow
        # `ingestion`'s service, via its public interface. Injected rather than constructed so this
        # module never learns how to build one — that is `ingestion`'s own composition concern, and
        # a
        # constructor call here would couple osint to ingestion's storage and KMS wiring.
        self._evidence = evidence
        # Required, not optional: publishing writes a `platform.audit_log` entry (§4.3) and every
        # audit entry is signed (ADR-0003 §1), so an optional KMS would make an unsigned one
        # reachable.
        self._kms = kms

    # -- sources ------------------------------------------------------------
    async def list_sources(self, actor: CurrentUser) -> Sequence[OsintSource]:
        return await self._uow.sources.list_()

    async def register_source(
        self, data: SourceCreate, actor: CurrentUser, correlation_id: str
    ) -> OsintSource:
        """Register a connector source. New sources start active.

        Publishes `osint.source_activated` (§25.3) because a newly-registered source *is* newly
        active — a consumer tracking which feeds are live would otherwise miss every source that was
        never toggled after creation.
        """
        source = OsintSource(
            name=data.name,
            connector_type=data.connector_type,
            reliability_baseline=data.reliability_baseline,
            is_active=True,
        )
        await self._uow.sources.add(source)
        await self._publish_source_state(source, activated=True, correlation_id=correlation_id)
        await self._audit(actor, "osint_source_registered", source.source_id, {"name": data.name})
        return source

    async def update_source(
        self,
        source_id: UUID,
        data: SourceUpdate,
        actor: CurrentUser,
        expected_etag: str,
        correlation_id: str,
    ) -> OsintSource:
        """Update the reliability baseline and/or the active flag (§4.3, ETag-guarded)."""
        source = await self._uow.sources.get_by_id(source_id)
        if source is None:
            raise SourceNotFoundError()
        if _normalize_etag(expected_etag) != _normalize_etag(source_etag(source)):
            raise PreconditionFailedError("source was modified concurrently (ETag mismatch)")

        if data.reliability_baseline is not None:
            source.reliability_baseline = data.reliability_baseline

        was_active = source.is_active
        if data.is_active is not None and data.is_active != was_active:
            source.is_active = data.is_active
            # Only on an actual transition. Publishing on every PATCH would tell consumers a source
            # was activated when the operator only edited its reliability baseline.
            await self._publish_source_state(
                source, activated=data.is_active, correlation_id=correlation_id
            )
        await self._audit(
            actor,
            "osint_source_updated",
            source_id,
            {"is_active": source.is_active, "reliability_baseline": source.reliability_baseline},
        )
        return source

    # -- findings -----------------------------------------------------------
    async def list_findings(self, actor: CurrentUser, page: PageParams) -> Sequence[OsintFinding]:
        after: tuple[datetime, UUID] | None = None
        if page.cursor is not None:
            # The cursor carries the sort value as an ISO string; the column is `timestamptz`, so it
            # has to be parsed back before it can be compared. Passing the string through produced a
            # Postgres type error rather than a wrong answer, which is the good failure mode — but
            # only because the row-value comparison is typed at all.
            sort_value, last_id = decode_cursor(page.cursor)
            after = (datetime.fromisoformat(sort_value), last_id)
        return await self._uow.findings.list_(limit=page.limit, after=after)

    async def get_finding(self, finding_id: UUID, actor: CurrentUser) -> OsintFinding:
        finding = await self._uow.findings.get_by_id(finding_id)
        if finding is None:
            raise FindingNotFoundError()
        return finding

    async def create_finding(
        self, data: FindingCreate, actor: CurrentUser, correlation_id: str
    ) -> OsintFinding:
        """Capture a raw finding. It is **not** evidence yet (§3.3: `evidence_id` is nullable).

        The source must exist and be active: a finding attributed to a deactivated feed would claim
        a
        provenance the operator has explicitly withdrawn, and the whole point of `is_active` is that
        it stops new data arriving under that source's name.

        `raw_attributes` is stored **unmodified** — CEM §9 step 1 keeps the raw connector output for
        audit, and normalization happens at publish. Validating its CEM shape here would reject
        findings a future mapping profile could handle, and the raw record is the thing an examiner
        goes back to when a mapping is later found wrong.
        """
        source = await self._uow.sources.get_by_id(data.source_id)
        if source is None:
            raise SourceNotFoundError()
        if not source.is_active:
            raise ValidationFailedError(
                [{"field": "source_id", "message": "source is deactivated and accepts no findings"}]
            )

        finding = OsintFinding(
            source_id=data.source_id,
            evidence_id=None,
            status=STATUS_CAPTURED,
            collected_at=datetime.now(UTC),
            raw_attributes=data.raw_attributes,
            # The source's baseline is the fallback: a connector that rates individual records
            # overrides it, one that does not inherits the operator's judgment about the feed.
            reliability_rating=data.reliability_rating or source.reliability_baseline,
        )
        await self._uow.findings.add(finding)

        # §25.3's trigger is "a connector or manual entry creates a finding" — so this fires on
        # capture, not publish. api-design.md §4.3 also lists the event under publish's "Events
        # Published"; §25.3 is authoritative for the event catalog and is the one followed here.
        await self._uow.outbox.publish(
            event_type=EVENT_FINDING_CAPTURED,
            aggregate_type="osint_finding",
            aggregate_id=finding.finding_id,
            payload={
                "finding_id": str(finding.finding_id),
                "source_id": str(finding.source_id),
                "reliability_rating": finding.reliability_rating,
            },
            correlation_id=correlation_id,
            actor_type="user",
            actor_ref=actor.user_id,
        )
        await self._audit(
            actor, "osint_finding_captured", finding.finding_id, {"source_id": str(data.source_id)}
        )
        return finding

    async def publish_finding(
        self, finding_id: UUID, actor: CurrentUser, correlation_id: str
    ) -> OsintFinding:
        """Normalize the finding into `ingestion.evidence` (CEM §9, api-design.md §4.3).

        Idempotency is **natural** (§4.3): republishing returns `409`, not a duplicate, because the
        check is on the finding's own `evidence_id`. That is why this endpoint needs no idempotency
        key — its state, not a client-supplied token, is what makes a retry safe.

        Ordering is the safety property. The evidence row is committed by `ingestion` *before* this
        finding records the `evidence_id`, both inside the request's single transaction (ADR-0005),
        so
        the two cannot disagree: if mapping or §13 validation fails, `ValidationFailedError`
        propagates,
        the transaction rolls back, and the finding stays unpublished — which is §4.3's "the finding
        remains unpublished" and FR-1.3's "never silent partial ingestion".
        """
        finding = await self.get_finding(finding_id, actor)
        if finding.evidence_id is not None:
            raise FindingAlreadyPublishedError(
                f"finding {finding_id} was already published as evidence {finding.evidence_id}"
            )
        source = await self._uow.sources.get_by_id(finding.source_id)
        if source is None:  # pragma: no cover - FK-enforced; a finding cannot outlive its source
            raise SourceNotFoundError()

        evidence = await self._evidence.ingest_evidence(
            self._map_to_cem(finding, source), actor, correlation_id
        )
        finding.evidence_id = evidence.evidence_id
        finding.status = STATUS_PUBLISHED

        await self._audit(
            actor,
            "evidence_published_from_osint",
            finding.finding_id,
            {"evidence_id": str(evidence.evidence_id), "source_id": str(source.source_id)},
        )
        return finding

    # -- CEM mapping (§9 step 2) --------------------------------------------
    def _map_to_cem(self, finding: OsintFinding, source: OsintSource) -> EvidenceCreate:
        """Map a raw finding onto a canonical evidence object.

        **What the connector must supply, and why this is not a guess.** CEM §9 step 2 specifies a
        "connector mapping profile — a versioned, declarative field-mapping definition, **not**
        per-connector business logic embedded in the ingestion path". `database-design.md` §3.2
        records
        a `mapping_profile_version` on `connector_registry` but models no table holding the profiles
        themselves, so there is nowhere to read a declarative mapping from. Rather than write the
        per-connector logic §9 forbids, this maps a **fixed envelope**: the connector states the CEM
        fields in `raw_attributes`, and anything missing is a `422` listing exactly what was absent.

        That keeps the mapping declarative (the connector declares it), keeps §9's Validate step
        loud
        rather than silent (FR-1.3), and leaves the profile store as a recorded gap instead of an
        invented table. A finding whose fields are wrong stays capturable and re-publishable once a
        profile exists.

        **Provenance is derived server-side.** `source` is built from the registered `OsintSource`,
        never from the payload — a connector must not be able to attribute its output to a different
        system. Same reason `category` is fixed to `osint`.
        """
        raw = finding.raw_attributes if isinstance(finding.raw_attributes, dict) else {}
        missing = [key for key in _REQUIRED_FOR_PUBLISH if raw.get(key) is None]
        if missing:
            raise ValidationFailedError(
                [
                    {
                        "field": f"raw_attributes.{key}",
                        "message": "required to map this finding onto the canonical evidence model",
                    }
                    for key in missing
                ]
            )

        attributes = raw["attributes"]
        if not isinstance(attributes, dict):
            raise ValidationFailedError(
                [{"field": "raw_attributes.attributes", "message": "must be an object"}]
            )

        return EvidenceCreate(
            schema_version=str(raw["schema_version"]),
            category=CATEGORY_OSINT,
            artifact_type=str(raw["artifact_type"]),
            title=str(raw["title"]),
            description=str(raw["description"]) if raw.get("description") is not None else None,
            # `system` + `collector_id` are both required by ingestion for provenance, and
            # `collector_id` is the registered source's own id: the source row *is* the collector,
            # so this ties every published evidence object back to the exact feed configuration it
            # came from — which survives the source being renamed. CEM §5's OSINT example shows
            # `system` and `collection_method`; `collector_id` is what makes the provenance
            # resolvable rather than merely descriptive.
            source={
                "system": source.name,
                "collector_id": str(source.source_id),
                "collection_method": source.connector_type,
            },
            collected_at=finding.collected_at,
            attributes=attributes,
            confidence=_decimal_confidence(raw["confidence"]),
            reliability_rating=finding.reliability_rating,
            payload_ref=str(raw["payload_ref"]) if raw.get("payload_ref") is not None else None,
            integrity_algorithm=(
                str(raw["integrity_algorithm"]) if raw.get("integrity_algorithm") else None
            ),
            integrity_hash=str(raw["integrity_hash"]) if raw.get("integrity_hash") else None,
            # OSINT is, by definition, lawfully collected from a public source. CEM §13's sentinel
            # says exactly that, and its own §5 OSINT example uses it — so it is asserted here
            # rather
            # than demanded of every connector. `osint` is not in ingestion's
            # legal-authority-required set, so this is a truthful default, not a bypass.
            legal_authority_ref=PUBLIC_SOURCE_AUTHORITY,
        )

    # -- internals ----------------------------------------------------------
    async def _publish_source_state(
        self, source: OsintSource, *, activated: bool, correlation_id: str
    ) -> None:
        await self._uow.outbox.publish(
            event_type=EVENT_SOURCE_ACTIVATED if activated else EVENT_SOURCE_DEACTIVATED,
            aggregate_type="osint_source",
            aggregate_id=source.source_id,
            payload={"source_id": str(source.source_id)},
            correlation_id=correlation_id,
            actor_type="user",
        )

    async def _audit(
        self, actor: CurrentUser, action: str, target_id: UUID, details: dict[str, object]
    ) -> None:
        roles = actor.roles
        await record_audit_event(
            self._uow.session,
            kms=self._kms,
            actor_user_id=actor.user_id,
            actor_role=roles[0] if roles else "none",
            action=action,
            module=_MODULE,
            target_type="osint_finding",
            target_id=target_id,
            details=details,
        )


def _decimal_confidence(value: Any) -> Decimal:
    """Parse a confidence as ``Decimal``, never ``float``.

    `Decimal(str(value))` rather than `Decimal(value)`: a JSON body parses `0.9` into a Python
    float,
    and `Decimal(0.9)` is 0.9000000000000000222…, which fails `EvidenceCreate`'s `le=1` bound only
    by
    luck and stores a value that does not round-trip. Going through `str` keeps the number the
    connector actually wrote (ADR-0011 §2).
    """
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationFailedError(
            [{"field": "raw_attributes.confidence", "message": "must be a number in [0, 1]"}]
        ) from None
    if not (Decimal(0) <= parsed <= Decimal(1)):
        raise ValidationFailedError(
            [{"field": "raw_attributes.confidence", "message": "must be in [0, 1]"}]
        )
    return parsed


def get_osint_service(
    uow: OsintUnitOfWork = Depends(get_osint_uow),
    evidence: EvidenceService = Depends(get_evidence_service),
    kms: KeyManagementService = Depends(get_kms),
) -> OsintService:
    """Compose the service. `evidence` arrives through `ingestion.public` — see the module docstring
    for why publishing calls `ingestion` synchronously rather than handing off to the outbox."""
    return OsintService(uow, evidence=evidence, kms=kms)
