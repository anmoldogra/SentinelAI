"""ingestion ORM models — schema ``ingestion`` (database-design.md §3.2).

The canonical evidence table (CEM implemented relationally), its per-evidence
hash-chained custody ledger (append-only), intake staging, and the connector /
attribute-schema registries. Intra-schema FKs only; ``collector_user_id`` and
custody ``actor_user_id`` are app-refs to ``platform.users`` (no FK, §5).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from sentinelai.modules.ingestion.exceptions import EvidenceAlreadySupersededError
from sentinelai.platform.db.base import Base
from sentinelai.shared.cem import CustodyEventType, IntegrityHash
from sentinelai.shared.exceptions import LegalHoldViolationError, ValidationFailedError

_SCHEMA = "ingestion"


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = ({"schema": _SCHEMA},)

    evidence_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    integrity_algorithm: Mapped[str | None] = mapped_column(Text, nullable=True)
    integrity_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    integrity_verification_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    inline_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    encoding: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    reliability_rating: Mapped[str | None] = mapped_column(Text, nullable=True)
    sensitivity: Mapped[str | None] = mapped_column(Text, nullable=True)
    legal_authority_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_restriction_tags: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    geo: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending_validation")
    supersedes_evidence_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.evidence.evidence_id"), nullable=True
    )
    collector_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    retention_policy_ref: Mapped[str] = mapped_column(Text, nullable=False)
    legal_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # -- aggregate behaviour (ADR-0011 §1): the custody and supersession invariants live HERE ----
    #
    # These were service-level `if` statements. The difference matters because they are legal
    # guarantees, not conveniences: a second code path that appended a custody event, or a raw
    # `UPDATE`, silently bypassed the legal-hold gate and the already-superseded check. Asking the
    # aggregate is now the only way to get a valid answer, and the aggregate cannot be asked to
    # produce an invalid state.
    #
    # They stay on the ORM class rather than a separate pure-domain object because that is the
    # pattern `Case` already established here for ADR-0011 §1, and because a declarative instance is
    # an ordinary Python object until it meets a session — so these are fully unit-testable with no
    # database, which is the property the ADR actually wants.

    def assert_can_record_custody(self, event_type: CustodyEventType) -> None:
        """Refuse a custody event this evidence item must not accept.

        The one prohibition is disposal under legal hold (security-architecture.md §39, which
        requires every deletion/purge path to check the hold first). It is expressed as a guard
        rather than folded into :meth:`apply_custody_event` so a caller can check before doing the
        expensive work of hashing and signing a ledger entry it is about to be refused.
        """
        if event_type.is_disposal and self.legal_hold:
            raise LegalHoldViolationError("evidence is under legal hold and cannot be disposed")

    def apply_custody_event(self, event_type: CustodyEventType) -> bool | None:
        """Fold a custody event's effect into this instance; returns the new hold state or ``None``.

        Legal hold is **ledger-derived** (ADR-0015, ADR-0004 §4): the custody event *is* the state
        transition, and the `legal_hold` column holds only the genesis value because the append-only
        trigger rejects an `UPDATE` on this table outright. This keeps the in-memory instance
        consistent with the ledger without dirtying it — see the caller's `set_committed_value`.

        Returns ``None`` for the events that say nothing about holds, which is why
        :attr:`CustodyEventType.legal_hold_state` is three-valued: collapsing it to a boolean would
        make every `accessed` event release a legal hold.
        """
        self.assert_can_record_custody(event_type)
        return event_type.legal_hold_state

    def assert_supersedable(self, *, already_superseded: bool) -> None:
        """Refuse to supersede an item that has already been replaced.

        ``already_superseded`` is passed in rather than read from a relationship, because the fact
        lives in another row (`supersedes_evidence_id` on the replacement) and an aggregate that
        lazily loaded it would need a session — which would make this untestable without a database
        and would hide a query inside an invariant check.

        The status column is deliberately not consulted: CEM §12 makes `superseded` a *derived*
        state (ADR-0015 — the genesis row's `status` never changes), so trusting it here would let a
        second supersession through on any row written before the overlay existed.
        """
        if already_superseded:
            raise EvidenceAlreadySupersededError(
                f"evidence {self.evidence_id} is already superseded"
            )

    @property
    def integrity(self) -> IntegrityHash | None:
        """The recorded payload digest as a value object, or ``None`` for payload-less evidence.

        Returns ``None`` rather than raising when the stored pair is incomplete or malformed: this
        is a read of history, and history can contain a row written before the value object existed.
        A caller that needs to *act* on the digest gets ``None`` and must decide; one that merely
        displays it is unaffected. Constructing an invalid `IntegrityHash` to represent bad stored
        data would defeat the type's only purpose.
        """
        if not self.integrity_algorithm or not self.integrity_hash:
            return None
        try:
            return IntegrityHash(self.integrity_algorithm, self.integrity_hash)
        except ValidationFailedError:
            return None


class EvidenceCustodyEvent(Base):
    __tablename__ = "evidence_custody_events"
    # Mirrors `202609080004_ingestion_chain`. Scoped to `evidence_id` because every chain starts
    # from the same all-zero genesis sentinel, so a global unique index on the link would permit
    # exactly one evidence item to exist. Declared here so `create_all` in tests reproduces them.
    __table_args__ = (
        Index(
            "uq_custody_events_evidence_prev_hash",
            "evidence_id",
            "prev_event_hash",
            unique=True,
        ),
        Index("uq_custody_events_evidence_sequence", "evidence_id", "sequence_number", unique=True),
        {"schema": _SCHEMA},
    )

    custody_event_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    evidence_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.evidence.evidence_id"), nullable=False
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    actor_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    actor_role: Mapped[str | None] = mapped_column(Text, nullable=True)
    authority_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    integrity_hash_at_event: Mapped[str] = mapped_column(Text, nullable=False)
    prev_event_hash: Mapped[str] = mapped_column(Text, nullable=False)
    entry_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # --- Cryptographic agility (ADR-0003 §5, modernization Wave 1.1) -------------------
    # All nullable: Wave 1.1 adds the columns, Wave 1.2 populates hash_algo/sig_alg/key_id/
    # signature at write time, Wave 1.3 populates anchor_ref asynchronously. A null therefore
    # means "written before that wave", which the Verification Engine (1.4) dispatches on —
    # it is a real state, not a missing value.
    hash_algo: Mapped[str | None] = mapped_column(Text, nullable=True)
    sig_alg: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    preimage_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signature: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    anchor_ref: Mapped[str | None] = mapped_column(Text, nullable=True)


class IntakeRecord(Base):
    __tablename__ = "intake_records"
    __table_args__ = ({"schema": _SCHEMA},)

    intake_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    connector_name: Mapped[str] = mapped_column(Text, nullable=False)
    raw_payload_ref: Mapped[str] = mapped_column(Text, nullable=False)
    validation_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_errors: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    received_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    resulting_evidence_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.evidence.evidence_id"), nullable=True
    )


class ConnectorRegistry(Base):
    __tablename__ = "connector_registry"
    __table_args__ = ({"schema": _SCHEMA},)

    connector_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    owning_module: Mapped[str] = mapped_column(Text, nullable=False)
    mapping_profile_version: Mapped[str] = mapped_column(Text, nullable=False)


class AttributeSchemaRegistry(Base):
    __tablename__ = "attribute_schema_registry"
    __table_args__ = (
        UniqueConstraint(
            "schema_version", "category", "artifact_type", name="uq_attr_schema_ver_cat_type"
        ),
        {"schema": _SCHEMA},
    )

    registry_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_type: Mapped[str] = mapped_column(Text, nullable=False)
