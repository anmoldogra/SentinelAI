"""Authentication business logic (guide Part 5) — ADR-0010, security-architecture.md §5.

Everything the login decision involves lives here: credential verification, account-status
enforcement, session issuance, and the audit trail. The router only parses, delegates, and owns
the transaction (ADR-0005); the repositories only read and write rows.

Two properties of this module are security requirements rather than style choices:

* **Failure is indistinguishable.** An unknown email, an SSO-only account with no password, a
  wrong password, and a disabled account all produce the same ``UnauthenticatedError`` with the
  same message — and all four pay the same argon2id verification cost, so response time is not an
  account-enumeration oracle either.
* **Every attempt is audited**, success or failure (security-architecture.md §5). The failure
  entry is written on the same transaction the router commits before re-raising, so a rejected
  login still leaves a trail.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.platform.auth.audit import record_audit_event
from sentinelai.platform.auth.models import Session, User
from sentinelai.platform.auth.repository import (
    SessionRepository,
    UserRepository,
    get_session_repository,
    get_user_repository,
)
from sentinelai.platform.config import settings
from sentinelai.platform.db.session import get_session
from sentinelai.platform.security.hashing import Argon2PasswordHasher, PasswordHasher
from sentinelai.platform.security.tokens import generate_opaque_token
from sentinelai.shared.exceptions import UnauthenticatedError

_MODULE = "platform"
_ACTIVE_STATUS = "active"
# One message for every rejection reason — see the module docstring.
_REJECTION = "Invalid email or password."


# Keyed by hasher implementation, since the digest format and cost come from that type. Not a
# secret — it protects nothing, so caching it process-wide is safe; the point is only that a
# failed login should not have to pay for a fresh hash on top of the verify it already does.
_dummy_hashes: dict[type[PasswordHasher], str] = {}


def _dummy_hash(hasher: PasswordHasher) -> str:
    """A throwaway digest to verify against when there is no real one, to level timing."""
    cached = _dummy_hashes.get(type(hasher))
    if cached is None:
        cached = hasher.hash(generate_opaque_token())
        _dummy_hashes[type(hasher)] = cached
    return cached


class AuthService:
    """Issues and audits sessions for password logins."""

    def __init__(
        self,
        session: AsyncSession,
        users: UserRepository,
        sessions: SessionRepository,
        hasher: PasswordHasher | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self._session = session
        self._users = users
        self._sessions = sessions
        self._hasher = hasher if hasher is not None else Argon2PasswordHasher()
        self._ttl_seconds = ttl_seconds if ttl_seconds is not None else settings.session_ttl_seconds

    async def login(
        self,
        email: str,
        password: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[str, Session]:
        """Verify credentials and issue a session, returning ``(plaintext_token, session)``.

        The plaintext token is returned to the caller exactly once and never persisted — only
        its digest and lookup prefix reach the database (ADR-0010 §1).
        """
        user = await self._users.get_by_email(email)
        # Evaluated before the branch, never short-circuited past: if an unknown email skipped
        # the verification the way `user is None or not ...` would, its faster response would be
        # exactly the account-enumeration oracle the dummy digest exists to close.
        password_ok = self._password_matches(
            user.password_hash if user is not None else None, password
        )
        if user is None or not password_ok:
            await self._audit_failure(
                user, "invalid_credentials", ip_address=ip_address, user_agent=user_agent
            )
            raise UnauthenticatedError(_REJECTION)

        if user.status != _ACTIVE_STATUS:
            await self._audit_failure(
                user, "account_not_active", ip_address=ip_address, user_agent=user_agent
            )
            raise UnauthenticatedError(_REJECTION)

        token = generate_opaque_token()
        issued_at = datetime.now(UTC)
        session_row = await self._sessions.create_session(
            user_id=user.user_id,
            token=token,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=self._ttl_seconds),
        )

        # Transparently upgrade a digest produced under weaker argon2 parameters, now that the
        # password is in hand and known-correct.
        if user.password_hash is not None and self._hasher.needs_rehash(user.password_hash):
            user.password_hash = self._hasher.hash(password)

        roles = await self._sessions.get_role_names(user.user_id)
        await record_audit_event(
            self._session,
            actor_user_id=user.user_id,
            actor_role=roles[0] if roles else "none",
            action="login_success",
            module=_MODULE,
            target_type="session",
            target_id=session_row.session_id,
            ip_address=ip_address,
            user_agent=user_agent,
            details={"roles": roles},
        )
        return token, session_row

    def _password_matches(self, stored_hash: str | None, password: str) -> bool:
        """Verify ``password``, spending the same time whether or not a digest exists."""
        if stored_hash is None:
            # Nothing to check — an unknown account, or an SSO-only one. Verify against a
            # throwaway digest anyway so this costs the same as a wrong password does.
            self._hasher.verify(_dummy_hash(self._hasher), password)
            return False
        return self._hasher.verify(stored_hash, password)

    async def _audit_failure(
        self,
        user: User | None,
        reason: str,
        *,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        """Record a rejected attempt. ``actor_user_id`` is null when no account resolved."""
        await record_audit_event(
            self._session,
            actor_user_id=user.user_id if user is not None else None,
            actor_role="anonymous",
            action="login_failed",
            module=_MODULE,
            target_type="user",
            target_id=user.user_id if user is not None else None,
            ip_address=ip_address,
            user_agent=user_agent,
            # The reason is recorded for the audit trail but never returned to the client.
            details={"reason": reason},
        )


async def get_auth_service(
    session: AsyncSession = Depends(get_session),
    users: UserRepository = Depends(get_user_repository),
    sessions: SessionRepository = Depends(get_session_repository),
) -> AuthService:
    """FastAPI dependency providing a request-scoped ``AuthService``."""
    return AuthService(session, users, sessions)
