"""investigation Pydantic schemas — api-design.md §6."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EntityCreate(BaseModel):
    entity_type: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    aliases: list[str] | None = None
    confidence: Decimal = Field(default=Decimal("1.0"), ge=0, le=1)


class EntityStatusUpdate(BaseModel):
    status: str = Field(min_length=1)


class EntityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    entity_id: UUID
    entity_type: str
    canonical_name: str
    aliases: list[str] | None
    status: str
    confidence: Decimal
    created_by_type: str
    created_by_ref: UUID


class RelationshipStatusUpdate(BaseModel):
    status: str = Field(min_length=1)
    note: str | None = None


class RelationshipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    relationship_id: UUID
    type: str
    from_entity_id: UUID
    to_entity_id: UUID
    directional: bool
    confidence: Decimal
    status: str
    valid_from: datetime | None
    valid_to: datetime | None
    created_by_type: str
    created_by_ref: UUID


class EntityMentionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    mention_id: UUID
    entity_id: UUID
    evidence_id: UUID


class RelationshipEvidenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    relationship_id: UUID
    evidence_id: UUID


class CorrelationRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: UUID
    case_id: UUID
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    findings_generated_count: int


class GraphRead(BaseModel):
    """Entity/relationship subgraph for a case (api-design.md §6 graph endpoint)."""

    entities: list[EntityRead]
    relationships: list[RelationshipRead]
