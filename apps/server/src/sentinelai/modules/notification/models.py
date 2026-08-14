"""notification ORM models — schema ``notification`` (database-design.md §3.6).

``notifications.source_reference_id`` is intentionally polymorphic (paired with
``source_module``) rather than typed FK-like columns, keeping ``notification`` a
generic, low-coupling terminal consumer (database-design §5). ``recipient_user_id``
and ``target_role_or_user`` are app-refs (no FK, §5).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from sentinelai.platform.db.base import Base

_SCHEMA = "notification"


class NotificationRule(Base):
    __tablename__ = "notification_rules"
    __table_args__ = ({"schema": _SCHEMA},)

    rule_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_event_type: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    target_role_or_user: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )  # app-ref
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = ({"schema": _SCHEMA},)

    notification_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    rule_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.notification_rules.rule_id"), nullable=True
    )
    recipient_user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)  # app-ref
    source_module: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_reference_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = ({"schema": _SCHEMA},)

    delivery_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    notification_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.notifications.notification_id"), nullable=False
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_status: Mapped[str] = mapped_column(String(30), nullable=False)
    attempted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
