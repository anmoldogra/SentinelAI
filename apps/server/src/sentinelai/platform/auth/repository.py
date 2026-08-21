"""Auth persistence — the ``platform`` schema's identity/session queries.

Persistence only (no business rules — guide Part 5): this layer resolves and writes rows. The
decision of *whether* a login succeeds, what a session's lifetime is, and what gets audited lives
in ``service.py``.

Token resolution (ADR-0010 §1) is the one query here with a non-obvious shape. The stored
``token_hash`` is argon2id and therefore salted, so it cannot be looked up by equality. The
indexed ``token_lookup`` prefix narrows the table to the handful of rows that could match, and
each candidate's full digest is then verified against the presented token. Security rests
entirely on that verify — the prefix is a non-secret index key, not a credential.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.platform.auth.models import Role, Session, User, UserRole
from sentinelai.platform.db.session import get_session
from sentinelai.platform.security.hashing import Argon2PasswordHasher, PasswordHasher
from sentinelai.platform.security.tokens import token_lookup_prefix


class SessionRepository:
    """Reads active sessions and their owning user's roles; writes new sessions."""

    def __init__(self, session: AsyncSession, hasher: PasswordHasher | None = None) -> None:
        self._session = session
        self._hasher = hasher if hasher is not None else Argon2PasswordHasher()

    async def get_active_by_token(self, token: str) -> Session | None:
        """Resolve a non-expired, non-revoked session from a bearer token."""
        stmt = select(Session).where(Session.token_lookup == token_lookup_prefix(token))
        candidates = (await self._session.execute(stmt)).scalars().all()
        for candidate in candidates:
            if self._hasher.verify(candidate.token_hash, token):
                return candidate
        return None

    async def get_role_names(self, user_id: UUID) -> list[str]:
        """Return the role names granted to a user (user_roles ⋈ roles)."""
        stmt = (
            select(Role.name)
            .join(UserRole, UserRole.role_id == Role.role_id)
            .where(UserRole.user_id == user_id)
            .order_by(Role.name)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def create_session(
        self,
        *,
        user_id: UUID,
        token: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> Session:
        """Persist a new session for ``token``, storing only its digest and lookup prefix.

        Flushed, not committed: the transaction belongs to the entrypoint (ADR-0005).
        """
        session_row = Session(
            user_id=user_id,
            token_lookup=token_lookup_prefix(token),
            token_hash=self._hasher.hash(token),
            issued_at=issued_at,
            expires_at=expires_at,
            revoked_at=None,
        )
        self._session.add(session_row)
        await self._session.flush()
        return session_row


class UserRepository:
    """Reads identities for authentication."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_email(self, email: str) -> User | None:
        """Resolve a user by email address (case-insensitive).

        Lowercased equality rather than ``ILIKE``: the argument is attacker-controlled, and
        ``ILIKE`` would interpret ``%``/``_`` in it as wildcards — letting a submitted ``%@%``
        match an arbitrary account.
        """
        stmt = select(User).where(func.lower(User.email) == email.lower())
        return (await self._session.execute(stmt)).scalars().first()


async def get_session_repository(
    session: AsyncSession = Depends(get_session),
) -> SessionRepository:
    """FastAPI dependency providing a request-scoped ``SessionRepository``."""
    return SessionRepository(session)


async def get_user_repository(
    session: AsyncSession = Depends(get_session),
) -> UserRepository:
    """FastAPI dependency providing a request-scoped ``UserRepository``."""
    return UserRepository(session)
