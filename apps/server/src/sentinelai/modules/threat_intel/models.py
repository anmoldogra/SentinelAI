"""threat_intel ORM models — schema ``threat_intel`` (database-design.md §3.3)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import ARRAY, Boolean, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from sentinelai.platform.db.base import Base

_SCHEMA = "threat_intel"


class ThreatActorProfile(Base):
    __tablename__ = "threat_actor_profiles"
    __table_args__ = ({"schema": _SCHEMA},)

    threat_actor_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class Ioc(Base):
    __tablename__ = "iocs"
    __table_args__ = ({"schema": _SCHEMA},)

    ioc_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    evidence_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)  # app-ref
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    indicator_type: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    threat_actor_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{_SCHEMA}.threat_actor_profiles.threat_actor_id"),
        nullable=True,
    )
    first_seen: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class FeedSubscription(Base):
    __tablename__ = "feed_subscriptions"
    __table_args__ = ({"schema": _SCHEMA},)

    subscription_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    feed_name: Mapped[str] = mapped_column(Text, nullable=False)
    protocol: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class IocEvidenceMatch(Base):
    __tablename__ = "ioc_evidence_matches"
    __table_args__ = ({"schema": _SCHEMA},)

    match_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    ioc_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.iocs.ioc_id"), nullable=False
    )
    matched_evidence_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )  # app-ref
    matched_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
