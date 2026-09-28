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


class GraphNodeRead(BaseModel):
    """One entity in a case subgraph, as the projection holds it (ADR-0013).

    Deliberately not ``EntityRead``. The projection is denormalized and case-scoped, so it carries
    exactly the fields the graph view needs plus two the transactional row has no concept of:
    ``is_seed`` (whether this entity is hop zero for ``depth``) and ``projected_at`` (how stale this
    view is). Reusing ``EntityRead`` would mean either dropping those or lying about where the row
    came from.
    """

    model_config = ConfigDict(from_attributes=True)

    entity_id: UUID
    entity_type: str
    canonical_name: str
    status: str
    confidence: Decimal
    is_seed: bool
    projected_at: datetime


class GraphEdgeRead(BaseModel):
    """One relationship in a case subgraph, as the projection holds it (ADR-0013)."""

    model_config = ConfigDict(from_attributes=True)

    relationship_id: UUID
    # `type` on the wire, matching api-design.md §6's example payload; `rel_type` in the projection,
    # because `type` shadows a builtin and SQLAlchemy models read worse for it.
    type: str = Field(validation_alias="rel_type")
    from_entity_id: UUID
    to_entity_id: UUID
    status: str
    confidence: Decimal


class GraphRead(BaseModel):
    """Entity/relationship subgraph for a case (api-design.md §6 graph endpoint).

    §6 guarantees the subgraph is self-contained: "every relationship's endpoints are guaranteed
    present in `entities`". The projection query enforces that by selecting edges last, restricted
    to
    node pairs that both survived the filters — so a client can lay the graph out without a second
    request, which is the point of returning it whole rather than paginated.
    """

    entities: list[GraphNodeRead]
    relationships: list[GraphEdgeRead]
