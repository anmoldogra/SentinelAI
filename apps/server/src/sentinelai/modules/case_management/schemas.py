"""case_management Pydantic schemas — request/response contracts (api-design.md §7).

Read models set ``from_attributes=True`` so a service may return an ORM object and
the router maps it through the schema — never returning the ORM model directly.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CaseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None


class CaseUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class CaseStatusUpdate(BaseModel):
    new_status: str = Field(min_length=1, max_length=50)
    notes: str | None = None


class CaseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    case_id: UUID
    title: str
    description: str | None
    status: str
    owning_user_id: UUID
    created_at: datetime
    closed_at: datetime | None


class CaseStatusHistoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    history_id: UUID
    case_id: UUID
    previous_status: str
    new_status: str
    actor_user_id: UUID
    changed_at: datetime
    notes: str | None


class EvidenceLinkCreate(BaseModel):
    evidence_id: UUID


class CaseEvidenceLinkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    link_id: UUID
    case_id: UUID
    evidence_id: UUID
    linked_by_user_id: UUID
    linked_at: datetime


class CaseReportCreate(BaseModel):
    report_type: str = Field(min_length=1, max_length=50)


class CaseReportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    report_id: UUID
    case_id: UUID
    report_type: str
    storage_ref: str
    generated_by_user_id: UUID
    generated_at: datetime
