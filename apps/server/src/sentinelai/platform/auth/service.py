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

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.platform.auth.audit import record_audit_event
from sentinelai.platform.auth.models import Session, User
from sentinelai.platform.auth.repository import (
    MfaRepository,
    SessionRepository,
    UserRepository,
    get_mfa_repository,
    get_session_repository,
    get_user_repository,
)
from sentinelai.platform.config import settings
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.db.session import get_session
from sentinelai.platform.security import totp
from sentinelai.platform.security.hashing import Argon2PasswordHasher, PasswordHasher
from sentinelai.platform.security.tokens import generate_opaque_token
from sentinelai.shared.exceptions import UnauthenticatedError

_MODULE = "platform"
_ACTIVE_STATUS = "active"
# One message for every rejection reason — see the module docstring.
_REJECTION = "Invalid email or password."
# Same principle at the second factor and at the session surface: a caller learns that it
# failed, never why. "Expired" and "already used" and "wrong code" are one answer here and
# three distinct entries in the audit log.
_MFA_REJECTION = "Invalid or expired verification."
_SESSION_REJECTION = "Invalid or expired session."


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


@dataclass(frozen=True, slots=True)
class LoginOutcome:
    """What a password login produced: a session, or a pending second factor.

    Both shapes carry exactly one freshly-minted opaque credential, so there is one ``token``
    field and ``session`` is what distinguishes them: present, and ``token`` is a bearer token;
    absent, and ``token`` is the ``mfa_token`` for the second-factor exchange. A second nullable
    field for the MFA case would hold the same string and give the router two things to keep
    consistent instead of one to branch on.

    The plaintext is present here and nowhere else — only its argon2id digest is persisted, so
    this object is the single opportunity to return it.
    """

    token: str
    session: Session | None = None


class AuthService:
    """Issues and audits sessions for password logins."""

    def __init__(
        self,
        session: AsyncSession,
        users: UserRepository,
        sessions: SessionRepository,
        hasher: PasswordHasher | None = None,
        ttl_seconds: int | None = None,
        *,
        kms: KeyManagementService,
        mfa: MfaRepository | None = None,
    ) -> None:
        self._session = session
        # Keyword-only and required: login success *and* failure are both audited
        # (security-architecture §5), and both entries must be signed (ADR-0003 §1).
        self._kms = kms
        self._users = users
        self._sessions = sessions
        self._hasher = hasher if hasher is not None else Argon2PasswordHasher()
        self._ttl_seconds = ttl_seconds if ttl_seconds is not None else settings.session_ttl_seconds
        # Optional only so existing construction sites keep working; built from the KMS this
        # service already requires, never skipped. An enrolled user must not be able to log in
        # without their second factor because a caller forgot to pass a repository.
        self._mfa = mfa if mfa is not None else MfaRepository(session, kms)

    async def login(
        self,
        email: str,
        password: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> LoginOutcome:
        """Verify credentials and either issue a session or demand a second factor.

        The plaintext token is returned to the caller exactly once and never persisted — only
        its digest and lookup prefix reach the database (ADR-0010 §1).

        **An enrolled user never receives a session here.** ``security-architecture.md`` §8 makes
        MFA mandatory for every role that can reach evidence or case data, so a password alone
        buys an ``mfa_token``: a short-lived, single-use credential for a half-authenticated
        principal, which ``verify_mfa`` exchanges for the real thing (api-design.md §9). Until
        Wave 3.1 the enrolment columns existed and nothing consulted them — an account could
        enrol a second factor and still log in with one.
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

        # Transparently upgrade a digest produced under weaker argon2 parameters, now that the
        # password is in hand and known-correct. Done before the MFA branch so an enrolled user
        # gets the rehash too — it depends on the password, not on the session.
        if user.password_hash is not None and self._hasher.needs_rehash(user.password_hash):
            user.password_hash = self._hasher.hash(password)

        issued_at = datetime.now(UTC)
        if user.mfa_enrolled_at is not None:
            challenge_token = generate_opaque_token()
            await self._mfa.create_challenge(
                user_id=user.user_id,
                token=challenge_token,
                issued_at=issued_at,
                expires_at=issued_at + timedelta(seconds=settings.mfa_challenge_ttl_seconds),
            )
            # Audited as its own action: "the password was right and we asked for the factor" is
            # a different fact from a completed login, and a burst of them with no matching
            # `login_success` is exactly the signal SR-11's misuse detection wants.
            await self._audit_event(
                user,
                "login_mfa_required",
                target_id=user.user_id,
                ip_address=ip_address,
                user_agent=user_agent,
                details={},
            )
            return LoginOutcome(token=challenge_token)

        token = generate_opaque_token()
        session_row = await self._sessions.create_session(
            user_id=user.user_id,
            token=token,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=self._ttl_seconds),
        )

        roles = await self._sessions.get_role_names(user.user_id)
        await record_audit_event(
            self._session,
            kms=self._kms,
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
        return LoginOutcome(token=token, session=session_row)

    async def verify_mfa(
        self,
        mfa_token: str,
        code: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[str, Session]:
        """Complete a login by presenting the second factor (api-design.md §9).

        Accepts either a TOTP code or an unused recovery code — a user who has lost their
        authenticator must still be able to get in, and refusing that produces account lockouts,
        not security.

        Every rejection is the same ``UnauthenticatedError`` with the same message, for the same
        reason login's is: an attacker holding a stolen ``mfa_token`` must not learn whether it
        expired, was already used, or simply had the wrong code.

        Four things must all hold, and each is checked separately so the audit trail can say
        which failed even though the client cannot:

        1. the ``mfa_token`` resolves to a challenge,
        2. that challenge is unexpired,
        3. it is claimed exactly once (``consume_challenge`` is an atomic conditional UPDATE, so
           two concurrent verifications cannot both win),
        4. the code verifies against the enrolled secret at this instant, and its time step has
           not been used before (RFC 6238 §5.2 — otherwise a code observed over the user's
           shoulder is replayable for the rest of its 30-second window).
        """
        now = datetime.now(UTC)
        challenge = await self._mfa.get_challenge_by_token(mfa_token)
        if challenge is None:
            await self._audit_failure(
                None, "mfa_token_unknown", ip_address=ip_address, user_agent=user_agent
            )
            raise UnauthenticatedError(_MFA_REJECTION)

        user = await self._users.get_by_id(challenge.user_id)
        if challenge.expires_at < now or challenge.consumed_at is not None:
            await self._audit_failure(
                user, "mfa_token_expired_or_used", ip_address=ip_address, user_agent=user_agent
            )
            raise UnauthenticatedError(_MFA_REJECTION)

        # Claim it before verifying the code, not after. A challenge consumed only on success
        # would let an attacker holding the token brute-force six digits against it until it
        # expired; consuming first makes every attempt cost a fresh password login.
        if not await self._mfa.consume_challenge(
            challenge_id=challenge.challenge_id, consumed_at=now
        ):
            await self._audit_failure(
                user, "mfa_token_replayed", ip_address=ip_address, user_agent=user_agent
            )
            raise UnauthenticatedError(_MFA_REJECTION)

        if user is None or user.status != _ACTIVE_STATUS:
            # The account was disabled between password and factor. Rare, but the window exists
            # and a disabled account must not be able to walk through it.
            await self._audit_failure(
                user, "account_not_active", ip_address=ip_address, user_agent=user_agent
            )
            raise UnauthenticatedError(_MFA_REJECTION)

        if not await self._factor_accepted(user, code, now):
            await self._audit_failure(
                user, "mfa_code_invalid", ip_address=ip_address, user_agent=user_agent
            )
            raise UnauthenticatedError(_MFA_REJECTION)

        token = generate_opaque_token()
        session_row = await self._sessions.create_session(
            user_id=user.user_id,
            token=token,
            issued_at=now,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
        )
        roles = await self._sessions.get_role_names(user.user_id)
        await self._audit_event(
            user,
            "login_success",
            target_id=session_row.session_id,
            ip_address=ip_address,
            user_agent=user_agent,
            details={"roles": roles, "mfa": True},
        )
        return token, session_row

    async def _factor_accepted(self, user: User, code: str, now: datetime) -> bool:
        """Verify ``code`` as a TOTP code, then as a recovery code. Neither leaks which was used.

        Order matters only for cost: TOTP is an HMAC and a recovery-code check is an argon2id
        verify per stored code, so trying the cheap one first keeps the common path cheap.
        """
        secret = await self._mfa.load_secret(user.user_id)
        if secret is not None:
            step = totp.verify_code(secret, code, now)
            # `claim_totp_step` is the RFC 6238 §5.2 replay guard: it records the accepted step
            # and returns False if that step was already used, so a code cannot be presented
            # twice inside its own validity window.
            if step is not None and await self._mfa.claim_totp_step(
                user_id=user.user_id, step=step
            ):
                return True
        return await self._mfa.redeem_recovery_code(user_id=user.user_id, code=code, used_at=now)

    async def refresh(
        self,
        token: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[str, Session]:
        """Rotate a live session: issue a successor and revoke the presented one (ADR-0010 §2).

        api-design.md §9 describes this as extending a session before ``expires_at``. It is
        implemented as **rotation** rather than as an ``UPDATE`` to ``expires_at``, because
        ADR-0010's A3 requires that "each successful refresh issues a new token and revokes its
        predecessor", which is what makes a stolen token single-use and its reuse detectable.

        Refreshing an expired or revoked session is refused rather than forgiven: a session that
        has ended is exactly what revocation means, and extending one would make logout advisory.
        """
        now = datetime.now(UTC)
        current = await self._sessions.get_active_by_token(token)
        if current is None or current.expires_at < now or current.revoked_at is not None:
            raise UnauthenticatedError(_SESSION_REJECTION)

        successor_token = generate_opaque_token()
        successor = await self._sessions.create_session(
            user_id=current.user_id,
            token=successor_token,
            issued_at=now,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
        )
        # Revoked, not deleted: the row is the evidence that this token existed and when it
        # stopped being valid, which is what makes a later replay attempt legible.
        current.revoked_at = now

        user = await self._users.get_by_id(current.user_id)
        await self._audit_event(
            user,
            "session_refreshed",
            target_id=successor.session_id,
            ip_address=ip_address,
            user_agent=user_agent,
            details={"revoked_session_id": str(current.session_id)},
        )
        return successor_token, successor

    async def logout(
        self,
        token: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Revoke a session immediately (api-design.md §9, ADR-0010 §2).

        Idempotent and silent about what it found: an unknown, expired or already-revoked token
        all return normally. Logout is the one operation that must never fail in a way that
        leaves a caller believing their session is still live, and a ``401`` here would tell an
        attacker holding a stale token whether it was ever real.
        """
        now = datetime.now(UTC)
        current = await self._sessions.get_active_by_token(token)
        if current is None or current.revoked_at is not None:
            return
        current.revoked_at = now
        user = await self._users.get_by_id(current.user_id)
        await self._audit_event(
            user,
            "logout",
            target_id=current.session_id,
            ip_address=ip_address,
            user_agent=user_agent,
            details={},
        )

    async def _audit_event(
        self,
        user: User | None,
        action: str,
        *,
        target_id: UUID | None,
        ip_address: str | None,
        user_agent: str | None,
        details: dict[str, object],
    ) -> None:
        """Record an authenticated auth-lifecycle event, signed like every other ledger entry."""
        roles = await self._sessions.get_role_names(user.user_id) if user is not None else []
        await record_audit_event(
            self._session,
            kms=self._kms,
            actor_user_id=user.user_id if user is not None else None,
            actor_role=roles[0] if roles else "none",
            action=action,
            module=_MODULE,
            target_type="session" if target_id is not None else None,
            target_id=target_id,
            ip_address=ip_address,
            user_agent=user_agent,
            details=details,
        )

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
            kms=self._kms,
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
    mfa: MfaRepository = Depends(get_mfa_repository),
    kms: KeyManagementService = Depends(get_kms),
) -> AuthService:
    """FastAPI dependency providing a request-scoped ``AuthService``."""
    return AuthService(session, users, sessions, kms=kms, mfa=mfa)
