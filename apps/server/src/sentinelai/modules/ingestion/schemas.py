"""ingestion Pydantic schemas — request/response contracts (api-design.md §5)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EvidenceCreate(BaseModel):
    schema_version: str
    category: str = Field(min_length=1, max_length=50)
    artifact_type: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    source: dict[str, Any]
    collected_at: datetime
    attributes: dict[str, Any]
    confidence: Decimal = Field(ge=0, le=1)
    reliability_rating: str | None = None
    payload_ref: str | None = None
    inline_payload: dict[str, Any] | None = None
    # Required for payload-bearing evidence (CEM §13); validated in the service.
    integrity_algorithm: str | None = None
    integrity_hash: str | None = None
    # CEM §13: required for certain categories (or the public-source sentinel).
    legal_authority_ref: str | None = None
    retention_policy_ref: str = "default"


class EvidenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    evidence_id: UUID
    schema_version: str
    category: str
    artifact_type: str
    title: str
    description: str | None
    status: str
    confidence: Decimal
    collected_at: datetime
    ingested_at: datetime
    integrity_verification_status: str | None
    legal_hold: bool


class UploadReservationRead(BaseModel):
    evidence_id: UUID
    upload_url: str


class CustodyEventCreate(BaseModel):
    event_type: str = Field(min_length=1, max_length=50)
    authority_ref: str | None = None
    notes: str | None = None


class CustodyEventRead(BaseModel):
    """One custody ledger entry (CEM §4, api-design.md §5).

    Exposes every field of the entry-hash preimage, because `api-design.md` §5 commits this
    endpoint to returning each event so "the chain is independently verifiable by the caller" —
    and a caller can only recompute `entry_hash` if it receives every input that went into it.
    `_custody_entry_hash` hashes `{prev, evidence_id, seq, event_type, integrity_hash_at_event,
    occurred_at}`, so omitting `prev_event_hash` or `integrity_hash_at_event` (as this schema
    previously did) left the ledger displayable but unverifiable.

    Additive only — no field is renamed or removed, so per `api-design.md` §2.2 this does not bump
    the API version.
    """

    model_config = ConfigDict(from_attributes=True)

    custody_event_id: UUID
    evidence_id: UUID
    sequence_number: int
    event_type: str
    occurred_at: datetime
    actor_user_id: UUID | None
    # The actor's role *at the time of the action*, retained even if the role later changes.
    actor_role: str | None
    authority_ref: str | None
    # Payload hash recomputed at this event — proves what was accessed/exported/analyzed matched
    # the original (CEM §4).
    integrity_hash_at_event: str
    # Returned as stored, including the all-zero genesis sentinel, rather than mapped to null.
    # The sentinel is what `_custody_entry_hash` actually hashed for the first entry, so a client
    # that received `null` here could not reproduce the genesis preimage without knowing the
    # convention out of band — which would defeat the point of exposing the chain at all.
    prev_event_hash: str
    entry_hash: str
    notes: str | None


class EvidenceSupersedeCreate(BaseModel):
    reason: str = Field(min_length=1)
    replacement: EvidenceCreate


class ConnectorCreate(BaseModel):
    name: str
    owning_module: str
    mapping_profile_version: str


class ConnectorUpdate(BaseModel):
    name: str | None = None
    mapping_profile_version: str | None = None


class ConnectorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    connector_id: UUID
    name: str
    owning_module: str
    mapping_profile_version: str


class AttributeSchemaRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    registry_id: UUID
    schema_version: str
    category: str
    artifact_type: str
