"""osint Pydantic schemas — api-design.md §5 (OSINT sub-section)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SourceCreate(BaseModel):
    name: str = Field(min_length=1)
    connector_type: str
    reliability_baseline: str | None = None


class SourceUpdate(BaseModel):
    reliability_baseline: str | None = None
    is_active: bool | None = None


class SourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_id: UUID
    name: str
    connector_type: str
    reliability_baseline: str | None
    is_active: bool


class FindingCreate(BaseModel):
    source_id: UUID
    raw_attributes: dict[str, Any]
    reliability_rating: str | None = None


class FindingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    finding_id: UUID
    source_id: UUID
    evidence_id: UUID | None
    status: str
    collected_at: datetime
    reliability_rating: str | None
