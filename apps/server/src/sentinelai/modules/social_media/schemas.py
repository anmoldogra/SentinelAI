"""social_media Pydantic schemas — api-design.md §5 (Social Media)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AccountCreate(BaseModel):
    platform: str = Field(min_length=1)
    handle: str = Field(min_length=1)


class AccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: UUID
    platform: str
    handle: str
    first_observed_at: datetime
    last_observed_at: datetime | None


class ContentCreate(BaseModel):
    platform: str = Field(min_length=1)
    account_handle: str = Field(min_length=1)
    content_kind: str = Field(min_length=1)
    raw_attributes: dict[str, Any]


class ContentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    content_id: UUID
    evidence_id: UUID | None
    status: str
    platform: str
    account_handle: str
    content_kind: str
    collected_at: datetime
