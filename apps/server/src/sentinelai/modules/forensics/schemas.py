"""forensics Pydantic schemas — api-design.md §4.5."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ArtifactCreate(BaseModel):
    """§4.5's request body: ``{ artifact_kind, device_info, acquisition_tool, acquisition_hash,
    collected_at }``.

    ``acquisition_hash`` is **required** and self-describing (``ALGORITHM:hexdigest``, the form
    `shared.cem.IntegrityHash` renders) — §4.5 requires it and requires that it "match the declared
    algorithm's format", and `database-design.md` §3.3 gives the table no algorithm column, so the
    value has to carry its own label. See `ForensicsService.register_artifact`.

    ``acquisition_tool`` is required too, for a different reason: it becomes the evidence object's
    ``source.system`` on publish, and CEM §13 rejects evidence without provenance. An artifact
    registered with no tool named could never be published, so the field that makes it publishable
    is demanded when the record is created rather than discovered to be missing later.
    """

    artifact_kind: str = Field(min_length=1, max_length=50)
    acquisition_tool: str = Field(min_length=1, max_length=200)
    acquisition_hash: str = Field(min_length=1, max_length=200)
    collected_at: datetime
    device_info: dict[str, Any] | None = None


class ArtifactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    artifact_id: UUID
    evidence_id: UUID | None
    status: str
    artifact_kind: str
    acquisition_tool: str | None
    acquisition_hash: str | None
    collected_at: datetime
