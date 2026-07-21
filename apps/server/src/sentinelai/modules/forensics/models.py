"""forensics ORM models — schema ``forensics`` (database-design.md §3.3).

The rich record table ``artifacts`` covers all forensic artifact kinds (disk,
mobile, cloud, blockchain, drone/IoT are ``artifact_kind`` values, not separate
tables). ``evidence_id`` is a nullable app-ref, set on publish.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from sentinelai.platform.db.base import Base

_SCHEMA = "forensics"


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = ({"schema": _SCHEMA},)

    artifact_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    evidence_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)  # app-ref
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    artifact_kind: Mapped[str] = mapped_column(Text, nullable=False)
    device_info: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    acquisition_tool: Mapped[str | None] = mapped_column(Text, nullable=True)
    acquisition_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
