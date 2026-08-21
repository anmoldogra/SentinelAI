"""Identity/session/audit ORM models — ``platform`` schema (database-design.md §3.1).

Identity is a platform concern, not a domain module, so these live in ``platform``
(schema ``platform``) rather than under ``modules/``. Foreign keys here are all
intra-schema (allowed); ``audit_log.actor_user_id`` is a deliberate app-ref (no FK).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from sentinelai.platform.db.base import Base
from sentinelai.platform.security.tokens import LOOKUP_PREFIX_LENGTH

_SCHEMA = "platform"


class User(Base):
    """A human analyst/administrator identity."""

    __tablename__ = "users"
    __table_args__ = ({"schema": _SCHEMA},)

    user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    external_idp_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    # argon2id digest of the password (ADR-0010 §3). NULL for SSO-only identities, which
    # authenticate through `identity_provider_links` and must never fall back to a password.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class Role(Base):
    """A named RBAC role."""

    __tablename__ = "roles"
    __table_args__ = ({"schema": _SCHEMA},)

    role_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)


class UserRole(Base):
    """User↔role grant (composite PK)."""

    __tablename__ = "user_roles"
    __table_args__ = ({"schema": _SCHEMA},)

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.users.user_id"), primary_key=True
    )
    role_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.roles.role_id"), primary_key=True
    )
    granted_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class Session(Base):
    """A server-side session record backing a bearer token (security §35).

    ADR-0010 §1: the bearer token is a high-entropy opaque secret that is **never** stored. The
    row keeps its argon2id digest (``token_hash``) plus a short, non-secret ``token_lookup``
    prefix. Because argon2id is salted, the digest is not itself indexable — the prefix is what
    turns token resolution into an index seek followed by a verify of the few candidates.
    """

    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_token_lookup", "token_lookup"),
        {"schema": _SCHEMA},
    )

    session_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.users.user_id"), nullable=False
    )
    # Deliberately NOT unique: a prefix collision between two live tokens is astronomically
    # unlikely but must degrade into an extra verify, never into a rejected login.
    token_lookup: Mapped[str] = mapped_column(String(LOOKUP_PREFIX_LENGTH), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class IdentityProviderLink(Base):
    """SSO/OIDC subject → user mapping."""

    __tablename__ = "identity_provider_links"
    __table_args__ = (
        UniqueConstraint("idp_name", "idp_subject", name="uq_idp_links_name_subject"),
        {"schema": _SCHEMA},
    )

    link_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.users.user_id"), nullable=False
    )
    idp_name: Mapped[str] = mapped_column(Text, nullable=False)
    idp_subject: Mapped[str] = mapped_column(Text, nullable=False)


class AuditLog(Base):
    """System-wide, hash-chained, insert-only audit trail (database-design.md §10)."""

    __tablename__ = "audit_log"
    __table_args__ = ({"schema": _SCHEMA},)

    audit_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    # app-ref (no FK) — null for system-initiated actions.
    actor_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    actor_role: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    module: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    prev_entry_hash: Mapped[str] = mapped_column(Text, nullable=False)
    entry_hash: Mapped[str] = mapped_column(Text, nullable=False)
