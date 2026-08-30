"""Identity/session/audit ORM models — ``platform`` schema (database-design.md §3.1).

Identity is a platform concern, not a domain module, so these live in ``platform``
(schema ``platform``) rather than under ``modules/``. Foreign keys here are all
intra-schema (allowed); ``audit_log.actor_user_id`` is a deliberate app-ref (no FK).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from sentinelai.platform.db.base import Base
from sentinelai.platform.security.tokens import LOOKUP_PREFIX_LENGTH

_SCHEMA = "platform"

# Mirrors `202608300001_platform_mfa`'s CHECK verbatim. Declared here too so the ORM metadata
# describes the database as it actually is — the migration is the authority, this is the mirror.
# Named without the `ck_users_` prefix: `db.base.NAMING_CONVENTION` renders `ck` as
# `ck_%(table_name)s_%(constraint_name)s` and would otherwise double it.
_MFA_SECRET_COMPLETE = """
    (
        mfa_enrolled_at IS NULL
        AND mfa_secret_ciphertext IS NULL
        AND mfa_secret_nonce IS NULL
        AND mfa_secret_algorithm IS NULL
        AND mfa_secret_key_id IS NULL
    ) OR (
        mfa_enrolled_at IS NOT NULL
        AND mfa_secret_ciphertext IS NOT NULL
        AND mfa_secret_nonce IS NOT NULL
        AND mfa_secret_algorithm IS NOT NULL
        AND mfa_secret_key_id IS NOT NULL
    )
"""


class User(Base):
    """A human analyst/administrator identity."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(_MFA_SECRET_COMPLETE, name="mfa_secret_complete"),
        {"schema": _SCHEMA},
    )

    user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    external_idp_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    # argon2id digest of the password (ADR-0010 §3). NULL for SSO-only identities, which
    # authenticate through `identity_provider_links` and must never fall back to a password.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    # --- TOTP second factor (ADR-0010 A1, database-design.md §3.1) ---
    #
    # `mfa_enrolled_at` IS the enrolment flag — there is deliberately no `mfa_enabled` boolean
    # that could disagree with it. The four `mfa_secret_*` columns are one `crypto.Ciphertext`
    # decomposed; the CHECK above makes them all-or-nothing, so a half-written enrolment (an
    # account that believes it has a second factor but cannot verify one) is unrepresentable.
    #
    # The secret is ENCRYPTED, not hashed, unlike every other credential on this model: TOTP
    # verification recomputes an HMAC over the shared secret, so it must be recoverable. Argon2id
    # here would produce a column that can never authenticate anyone.
    mfa_enrolled_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    mfa_secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    mfa_secret_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    mfa_secret_algorithm: Mapped[str | None] = mapped_column(Text, nullable=True)
    # `provider:version:backend_ref` — see `_serialize_key_id` in repository.py for why the
    # version precedes the ref rather than following it.
    mfa_secret_key_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # RFC 6238 §5.2 replay guard: the last time-step accepted for this user. Outside the CHECK
    # because it is legitimately null until the first successful verification.
    mfa_last_used_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

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


class MfaRecoveryCode(Base):
    """One backup code (security-architecture §8) — account recovery only, single use.

    **Hashed, not encrypted**, unlike the TOTP secret on ``User``: a recovery code is only ever
    verified against what a user presents, never read back, so the one-way property is available
    and is therefore the one to take.

    Redeemed rows are retained rather than deleted, so "which code was used, and when" survives
    for audit — and so a redeemed code cannot silently become reissuable.
    """

    __tablename__ = "mfa_recovery_codes"
    __table_args__ = (
        Index("ix_mfa_recovery_codes_user_id", "user_id"),
        {"schema": _SCHEMA},
    )

    code_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.users.user_id"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class MfaChallenge(Base):
    """A pending MFA step — the ``mfa_token`` of ``api-design.md`` §9's login exchange.

    Server-side state, shaped exactly like ``Session``, because this token is a credential for a
    **half-authenticated** principal: the password has been accepted, the second factor has not.
    A self-contained token would be one the server could not withdraw between those two moments.

    ``consumed_at`` makes it single-use, so a replayed ``mfa_token`` cannot mint a second session.
    """

    __tablename__ = "mfa_challenges"
    __table_args__ = (
        Index("ix_mfa_challenges_token_lookup", "token_lookup"),
        {"schema": _SCHEMA},
    )

    challenge_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{_SCHEMA}.users.user_id"), nullable=False
    )
    # Non-unique, as on `Session`: a prefix collision must cost an extra verify, never a failure.
    token_lookup: Mapped[str] = mapped_column(String(LOOKUP_PREFIX_LENGTH), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


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
