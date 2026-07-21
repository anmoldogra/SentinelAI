"""forensics Pydantic schemas — api-design.md §5 (Forensics)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ArtifactCreate(BaseModel):
    artifact_kind: str = Field(min_length=1)
    device_info: dict[str, Any] | None = None
    acquisition_tool: str | None = None
    acquisition_hash: str | None = None


class ArtifactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    artifact_id: UUID
    evidence_id: UUID | None
    status: str
    artifact_kind: str
    acquisition_tool: str | None
    acquisition_hash: str | None
    collected_at: datetime
