"""social_media Pydantic schemas — api-design.md §4.6."""

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
    """§4.6's body: ``{ platform, account_handle, content_kind, raw_attributes, captured_at }``.

    ``raw_attributes`` is the capture's CEM envelope, the same role `osint`'s column of that name
    plays and `forensics` had to press `device_info` into — see
    `SocialMediaService`'s module docstring.
    """

    platform: str = Field(min_length=1, max_length=100)
    account_handle: str = Field(min_length=1, max_length=200)
    content_kind: str = Field(min_length=1, max_length=50)
    captured_at: datetime
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
