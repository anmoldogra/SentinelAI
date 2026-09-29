"""forensics business logic (guide Part 5) — artifact intake and publication into the CEM.

The same domain-producer shape `osint` established: a **rich record** table holds the examiner's
tool-specific view, and publishing maps it onto the canonical evidence model through
`ingestion.public`. CEM §9's pipeline splits the same way — Extract is the acquisition tool's, Map
is this module's, and Enrich/Validate/Commit are `ingestion`'s, because §13's rules and the custody
genesis entry belong to the module that owns the evidence table.

**Two things make forensics stricter than `osint`, and both are legal rather than technical.**

*Legal authority is mandatory.* CEM §13 requires `classification.legal_authority_ref` for
`digital_forensics` and `mobile_forensics`, and `osint`'s answer — the
`public_source_no_authority_required` sentinel — is unavailable here by definition: a device
extraction is not a public source. There is no lawful default, so publication demands the reference
and refuses without it.

*The acquisition hash is the examiner's claim, and ADR-0008 tests it.* `acquisition_hash` is what
the imaging tool reported. On publish it becomes the evidence object's `integrity.hash`, which
`ingestion` **recomputes from the stored bytes and rejects on mismatch** (ADR-0008 §3) — so an image
whose stored object does not match the tool's manifest cannot become evidence. That is the whole
point of carrying the hash through rather than trusting it: the two hashes are checked against each
other by the module that can see the bytes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from fastapi import Depends

from sentinelai.modules.forensics.events import (
    EVENT_ARTIFACT_PROCESSED,
    EVENT_ARTIFACT_REGISTERED,
)
from sentinelai.modules.forensics.exceptions import (
    ArtifactAlreadyPublishedError,
    ArtifactNotFoundError,
)
from sentinelai.modules.forensics.models import Artifact
from sentinelai.modules.forensics.repository import ForensicsUnitOfWork, get_forensics_uow
from sentinelai.modules.forensics.schemas import ArtifactCreate
from sentinelai.modules.ingestion.public import (
    EvidenceCreate,
    EvidenceService,
    get_evidence_service,
)
from sentinelai.platform.auth.audit import record_audit_event
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.shared.cem import IntegrityHash
from sentinelai.shared.exceptions import ValidationFailedError
from sentinelai.shared.pagination import PageParams, decode_cursor, encode_cursor

_MODULE = "forensics"

# CEM §6's artifact types for the two forensic categories, verbatim. The **kind decides the
# category**, which is why this is a mapping and not a fixed constant the way `osint`'s
# `CATEGORY_OSINT` is: this one module produces evidence in two of CEM §5's categories, and
# `mobile_forensics` exists separately from `digital_forensics` precisely because the tools and
# attribute shapes differ (§5's own note). Deriving the category server-side also means a caller
# cannot mislabel a phone extraction as a disk image to dodge a different attributes schema.
_DIGITAL_FORENSICS_KINDS: Final[frozenset[str]] = frozenset(
    {
        "disk_image",
        "memory_dump",
        "file_artifact",
        "registry_hive",
        "event_log",
        "network_capture",
        "email_archive",
        "forensic_image",
    }
)
_MOBILE_FORENSICS_KINDS: Final[frozenset[str]] = frozenset(
    {
        "full_extraction",
        "file_system_extraction",
        "call_log",
        "sms_mms_message",
        "app_data_artifact",
        "device_metadata",
        "location_history",
        "oxygen_extraction",
    }
)
CATEGORY_BY_KIND: Final[dict[str, str]] = {
    **dict.fromkeys(_DIGITAL_FORENSICS_KINDS, "digital_forensics"),
    **dict.fromkeys(_MOBILE_FORENSICS_KINDS, "mobile_forensics"),
}

# The artifact lifecycle. `database-design.md` §3.3 requires a `status` column and does not fix its
# vocabulary; these match the two events §25.5 defines, so the row state and the announced fact
# cannot drift into describing different things. Same reasoning `osint` records for `captured`.
STATUS_REGISTERED = "registered"
STATUS_PUBLISHED = "published"

# A forensic acquisition is a mechanical read of a device or image, not an inference: the tool
# either recovered the artifact or it did not. CEM §14's mobile-forensics example carries
# `confidence: 1.0` for exactly that reason. Analytical doubt about what an artifact *means* belongs
# on the entities and relationships an analyst derives from it (CEM §10), not on its own fidelity.
ACQUISITION_CONFIDENCE: Final = Decimal("1.000")

# CEM §14's mobile-forensics example uses this `collection_method` verbatim.
COLLECTION_METHOD: Final = "forensic_extraction"

# What `device_info` must carry before an artifact can be mapped onto the CEM. See `_map_to_cem`.
_REQUIRED_FOR_PUBLISH: Final[tuple[str, ...]] = (
    "schema_version",
    "title",
    "attributes",
    "legal_authority_ref",
)


# -- CEM mapping (§9 step 2) -------------------------------------------------------------
#
# Module-level functions rather than `ForensicsService` methods, because neither touches the
# service's state: the mapping needs an artifact and the examiner, and the category lookup needs
# a string. `osint` keeps its equivalent as a method; this is the shape `threat_intel`'s matcher
# settled on, and it is better — the validation an examiner hits most often is reachable without
# constructing a service around an object storage client and a KMS it never calls.
def map_artifact_to_cem(artifact: Artifact, actor: CurrentUser) -> EvidenceCreate:
    """Map a registered artifact onto a canonical evidence object.

    **``device_info`` is the record's envelope, and the column's name is narrower than the job
    it has to do.** CEM §9 step 2 asks for a "connector mapping profile — a versioned,
    declarative field-mapping definition, **not** per-connector business logic embedded in the
    ingestion path", and `database-design.md` §3.2 records a `mapping_profile_version` on
    `connector_registry` while modelling no table to hold the profiles. So there is nowhere to
    read a declarative mapping from, and §3.3 gives this table four artifact columns plus the
    common three. The CEM fields they do not cover — `schema_version`, `title`, `attributes`,
    `legal_authority_ref` — are stated by the examiner's tool inside `device_info`, and anything
    missing is a `422` naming it.

    That is the same fixed-envelope decision `osint` made for `raw_attributes`, for the same
    recorded gap, and it is preferable to the two alternatives: writing the per-connector logic
    §9 forbids, or inventing the columns §3.3 does not model. It is called out rather than
    smoothed over, because reading a warrant reference out of a field called `device_info` is
    not self-evidently right — it is the least wrong option until the profile store exists.

    **Provenance and category are derived server-side, never taken from the envelope.** The tool
    that made the acquisition is `source.system`; the examiner is `collector_id`, matching CEM
    §14's `"collector_id": "examiner:priya.n"`; and `category` comes from the artifact kind. A
    caller able to set any of those could attribute an extraction to a different tool, another
    examiner, or a different attributes schema.

    The examiner recorded is the one **publishing**, because §3.3 models no `registered_by`
    column and this module must not invent one. Normally they are the same person — whoever
    registers an acquisition publishes it — and where they are not, the audit log holds both
    acts with both principals, which is the record that has to be right.
    """
    info = artifact.device_info if isinstance(artifact.device_info, dict) else {}
    missing = [key for key in _REQUIRED_FOR_PUBLISH if info.get(key) is None]
    if missing:
        raise ValidationFailedError(
            [
                {
                    "field": f"device_info.{key}",
                    "message": "required to map this artifact onto the canonical evidence model",
                }
                for key in missing
            ]
        )

    attributes = info["attributes"]
    if not isinstance(attributes, dict):
        raise ValidationFailedError(
            [{"field": "device_info.attributes", "message": "must be an object"}]
        )

    # Re-parsed rather than split: the stored value came through `IntegrityHash` at
    # registration, and going back through it means the two halves handed to `ingestion`
    # cannot be assembled from a string this platform would no longer accept.
    acquisition = IntegrityHash.parse(artifact.acquisition_hash or "", field="acquisition_hash")

    return EvidenceCreate(
        schema_version=str(info["schema_version"]),
        category=category_for_kind(artifact.artifact_kind),
        artifact_type=artifact.artifact_kind,
        title=str(info["title"]),
        description=str(info["description"]) if info.get("description") is not None else None,
        source={
            "system": artifact.acquisition_tool or "unknown",
            # The examiner is the collector for a forensic acquisition — a person performed it,
            # and CEM §14's own example says so. The prefix keeps it readable beside the
            # connector ids other modules put here.
            "collector_id": f"examiner:{actor.user_id}",
            "collection_method": COLLECTION_METHOD,
        },
        collected_at=artifact.collected_at,
        attributes=attributes,
        confidence=ACQUISITION_CONFIDENCE,
        # The acquisition hash becomes the evidence object's integrity claim, which `ingestion`
        # recomputes from the stored object and rejects on mismatch (ADR-0008 §3). Passing
        # `payload_ref` through from the envelope is what gives it something to recompute; an
        # artifact registered as metadata only has none, and CEM §13 asks for integrity fields
        # on *payload-bearing* evidence, so both shapes are legitimate.
        payload_ref=str(info["payload_ref"]) if info.get("payload_ref") is not None else None,
        integrity_algorithm=acquisition.algorithm,
        integrity_hash=acquisition.digest,
        # CEM §13 requires this for both forensic categories, and there is no lawful default:
        # `osint`'s public-source sentinel cannot apply to a device extraction. `_REQUIRED_FOR_
        # PUBLISH` is what guarantees it is present by the time this runs.
        legal_authority_ref=str(info["legal_authority_ref"]),
    )


# -- internals ----------------------------------------------------------
def category_for_kind(artifact_kind: str) -> str:
    """CEM §5's category for an artifact kind, or a `422` listing what is accepted.

    §4.5's validation rule, enforced in one place so registration and publication cannot
    disagree about which kinds exist.
    """
    category = CATEGORY_BY_KIND.get(artifact_kind)
    if category is None:
        raise ValidationFailedError(
            [
                {
                    "field": "artifact_kind",
                    "message": (
                        "must be a CEM §6 artifact type for digital_forensics or "
                        f"mobile_forensics; permitted: {sorted(CATEGORY_BY_KIND)}"
                    ),
                }
            ]
        )
    return category


class ForensicsService:
    def __init__(
        self,
        uow: ForensicsUnitOfWork,
        *,
        evidence: EvidenceService,
        kms: KeyManagementService,
    ) -> None:
        self._uow = uow
        # `ingestion`'s service, via its public interface. Injected rather than constructed so this
        # module never learns how to build one — that is `ingestion`'s composition concern, and a
        # constructor call here would couple forensics to its storage and KMS wiring.
        self._evidence = evidence
        # Required, not optional. §4.5 makes registration itself auditable ("artifact registration
        # is itself a chain-of-custody-relevant act even before canonical publication") and every
        # audit entry is signed (ADR-0003 §1), so an optional KMS would make an unsigned one
        # reachable on the one path that must never have one.
        self._kms = kms

    # -- reads --------------------------------------------------------------
    async def list_artifacts(
        self, actor: CurrentUser, page: PageParams
    ) -> tuple[Sequence[Artifact], str | None, bool]:
        """One page of artifacts, with the cursor for the next (api-design.md §2.5).

        Returns the cursor rather than leaving the route to invent one: a list endpoint that always
        answers ``next_cursor: null`` is a list endpoint a client cannot page, whatever the
        repository underneath it supports.
        """
        after: tuple[datetime, UUID] | None = None
        if page.cursor is not None:
            # The cursor carries the sort value as an ISO string and the column is `timestamptz`, so
            # it has to be parsed back before the row-value comparison can be typed at all.
            sort_value, last_id = decode_cursor(page.cursor)
            after = (datetime.fromisoformat(sort_value), last_id)
        # One extra row, so "is there a next page" is answered by the query rather than guessed.
        rows = list(await self._uow.artifacts.list_(limit=page.limit + 1, after=after))
        has_more = len(rows) > page.limit
        items = rows[: page.limit]
        next_cursor = (
            encode_cursor(items[-1].collected_at.isoformat(), items[-1].artifact_id)
            if has_more and items
            else None
        )
        return items, next_cursor, has_more

    async def get_artifact(self, artifact_id: UUID, actor: CurrentUser) -> Artifact:
        artifact = await self._uow.artifacts.get_by_id(artifact_id)
        if artifact is None:
            raise ArtifactNotFoundError()
        return artifact

    # -- intake -------------------------------------------------------------
    async def register_artifact(
        self, data: ArtifactCreate, actor: CurrentUser, correlation_id: str
    ) -> Artifact:
        """Register the examiner's rich record. It is **not** evidence yet (§3.3's nullable
        ``evidence_id``).

        Two validations happen here rather than at publish, because §4.5 puts them here and because
        both are cheap to fix at registration and expensive to discover later:

        * ``artifact_kind`` must be one of CEM §6's forensic artifact types. An unknown kind has no
          category, so it could never be published — refusing it now names the problem while the
          examiner is still looking at the acquisition.
        * ``acquisition_hash`` must parse as ``ALGORITHM:hexdigest`` with a digest length matching
          its label (`shared.cem.IntegrityHash`). §4.5 requires the format check; the
          length-vs-label part is what catches a SHA-256 digest labelled SHA-512, which would verify
          against nothing forever because the label is what a verifier trusts.

        Publishes ``forensics.artifact_registered`` (§25.5) and writes an audit entry (§4.5).
        """
        category = category_for_kind(data.artifact_kind)
        # Parsed for validation; stored in the canonical `ALGORITHM:digest` spelling so the column
        # holds one format rather than whatever casing a tool emitted.
        acquisition = IntegrityHash.parse(data.acquisition_hash, field="acquisition_hash")

        artifact = Artifact(
            status=STATUS_REGISTERED,
            collected_at=data.collected_at,
            artifact_kind=data.artifact_kind,
            device_info=data.device_info,
            acquisition_tool=data.acquisition_tool,
            acquisition_hash=str(acquisition),
        )
        await self._uow.artifacts.add(artifact)

        await self._uow.outbox.publish(
            event_type=EVENT_ARTIFACT_REGISTERED,
            aggregate_type="forensic_artifact",
            aggregate_id=artifact.artifact_id,
            payload={
                "artifact_id": str(artifact.artifact_id),
                "artifact_kind": artifact.artifact_kind,
            },
            correlation_id=correlation_id,
            actor_type="user",
            actor_ref=actor.user_id,
        )
        await self._audit(
            actor,
            "forensic_artifact_registered",
            artifact.artifact_id,
            {"artifact_kind": data.artifact_kind, "category": category},
        )
        return artifact

    async def publish_artifact(
        self, artifact_id: UUID, actor: CurrentUser, correlation_id: str
    ) -> Artifact:
        """Normalize the artifact into ``ingestion.evidence`` (CEM §9, api-design.md §4.5).

        Idempotency is **natural**, as it is for `osint`'s publish: the check is on the artifact's
        own ``evidence_id``, so republishing returns `409` rather than creating a second evidence
        object for one acquisition. That is why §4.5 marks this endpoint idempotent without an
        ``Idempotency-Key`` — its state, not a client token, is what makes a retry safe.

        Ordering is the safety property. `ingestion` commits the evidence row *before* this artifact
        records the ``evidence_id``, both inside the request's single transaction (ADR-0005), so the
        two cannot disagree: if the mapping fails §13's rules — or if ADR-0008's recompute finds the
        stored bytes do not match the acquisition hash — the error propagates, the transaction rolls
        back, and the artifact stays unpublished. FR-1.3's "never a silent partial ingestion".

        Publishes ``forensics.artifact_processed`` (§25.5), whose trigger is normalization
        completing and whose payload carries the ``evidence_id`` it produced.
        """
        artifact = await self.get_artifact(artifact_id, actor)
        if artifact.evidence_id is not None:
            raise ArtifactAlreadyPublishedError(
                f"artifact {artifact_id} was already published as evidence {artifact.evidence_id}"
            )

        evidence = await self._evidence.ingest_evidence(
            map_artifact_to_cem(artifact, actor), actor, correlation_id
        )
        artifact.evidence_id = evidence.evidence_id
        artifact.status = STATUS_PUBLISHED

        await self._uow.outbox.publish(
            event_type=EVENT_ARTIFACT_PROCESSED,
            aggregate_type="forensic_artifact",
            aggregate_id=artifact.artifact_id,
            payload={
                "artifact_id": str(artifact.artifact_id),
                "evidence_id": str(evidence.evidence_id),
            },
            correlation_id=correlation_id,
            actor_type="user",
            actor_ref=actor.user_id,
        )
        await self._audit(
            actor,
            "evidence_published_from_forensics",
            artifact.artifact_id,
            {"evidence_id": str(evidence.evidence_id), "artifact_kind": artifact.artifact_kind},
        )
        return artifact

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
            target_type="forensic_artifact",
            target_id=target_id,
            details=details,
        )


def get_forensics_service(
    uow: ForensicsUnitOfWork = Depends(get_forensics_uow),
    evidence: EvidenceService = Depends(get_evidence_service),
    kms: KeyManagementService = Depends(get_kms),
) -> ForensicsService:
    """Compose the service. `evidence` arrives through `ingestion.public` — see the module docstring
    for why publishing calls `ingestion` synchronously rather than handing off to the outbox."""
    return ForensicsService(uow, evidence=evidence, kms=kms)
