"""notification Pydantic schemas — api-design.md §8."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class NotificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    notification_id: UUID
    rule_id: UUID | None
    recipient_user_id: UUID
    source_module: str | None
    source_reference_id: UUID | None
    message: str | None
    created_at: datetime
    read_at: datetime | None


class NotificationRuleCreate(BaseModel):
    name: str = Field(min_length=1)
    trigger_event_type: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    target_role_or_user: UUID


class NotificationRuleUpdate(BaseModel):
    name: str | None = None
    channel: str | None = None
    is_active: bool | None = None


class NotificationRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    rule_id: UUID
    name: str
    trigger_event_type: str
    channel: str
    target_role_or_user: UUID
    is_active: bool
