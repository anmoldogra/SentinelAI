"""threat_intel Pydantic schemas — api-design.md §5 (Threat Intel)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class IocCreate(BaseModel):
    indicator_type: str = Field(min_length=1)
    value: str = Field(min_length=1)
    threat_actor_id: UUID | None = None


class IocRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ioc_id: UUID
    evidence_id: UUID | None
    status: str
    indicator_type: str
    value: str
    threat_actor_id: UUID | None
    collected_at: datetime
    first_seen: datetime | None
    last_seen: datetime | None


class ThreatActorCreate(BaseModel):
    name: str = Field(min_length=1)
    aliases: list[str] | None = None
    description: str | None = None


class ThreatActorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    threat_actor_id: UUID
    name: str
    aliases: list[str] | None
    description: str | None


class FeedCreate(BaseModel):
    feed_name: str = Field(min_length=1)
    protocol: str


class FeedRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    subscription_id: UUID
    feed_name: str
    protocol: str
    is_active: bool
    last_synced_at: datetime | None


class MatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    match_id: UUID
    ioc_id: UUID
    matched_evidence_id: UUID
    matched_at: datetime
    confidence: Decimal
