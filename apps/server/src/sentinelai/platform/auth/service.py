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
class IssuedSession:
    """A newly-issued session and **both** of its plaintext credentials — ADR-0010 A3.

    The two travel differently and must not be confused: ``access_token`` goes in the response body
    for the client to hold in memory and send as a bearer, ``refresh_token`` goes in an HttpOnly
    cookie scoped to the refresh endpoint and is never in a body. Returning them as one object
    rather than a tuple is what makes a caller name which is which at the point of use.

    Both plaintexts exist here and nowhere else — only their argon2id digests are persisted, so this
    object is the single opportunity to hand them out.
    """

    access_token: str
    refresh_token: str
    session: Session


@dataclass(frozen=True, slots=True)
class LoginOutcome:
    """What a password login produced: an issued session, or a pending second factor.

    Exactly one of ``issued`` / ``mfa_token`` is set, and which one is the discriminator the router
    branches on. Modelled as two explicit fields rather than one overloaded ``token`` because A3
    made a login's success case carry *two* credentials — a single field could no longer describe
    both outcomes without lying about one of them.
    """

    issued: IssuedSession | None = None
    mfa_token: str | None = None

    @property
    def mfa_required(self) -> bool:
        return self.issued is None


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
            return LoginOutcome(mfa_token=challenge_token)

        issued = await self._issue(user.user_id, issued_at)

        roles = await self._sessions.get_role_names(user.user_id)
        await record_audit_event(
            self._session,
            kms=self._kms,
            actor_user_id=user.user_id,
            actor_role=roles[0] if roles else "none",
            action="login_success",
            module=_MODULE,
            target_type="session",
            target_id=issued.session.session_id,
            ip_address=ip_address,
            user_agent=user_agent,
            details={"roles": roles},
        )
        return LoginOutcome(issued=issued)

    async def _issue(self, user_id: UUID, now: datetime) -> IssuedSession:
        """Mint an access/refresh pair and persist their digests — ADR-0010 §1, A3.

        One place, called by login, MFA completion and rotation alike, so the three paths cannot
        drift on how long a credential lives or on whether a refresh token is issued at all.

        The two tokens are independently generated 256-bit values, not derived from one another: a
        refresh token computable from an access token would make the access token — the one exposed
        to JavaScript — sufficient to mint new sessions, which is the exact property A3's split
        exists to remove.
        """
        access_token = generate_opaque_token()
        refresh_token = generate_opaque_token()
        session_row = await self._sessions.create_session(
            user_id=user_id,
            token=access_token,
            issued_at=now,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
            refresh_token=refresh_token,
            refresh_expires_at=now + timedelta(seconds=settings.refresh_token_ttl_seconds),
        )
        return IssuedSession(
            access_token=access_token, refresh_token=refresh_token, session=session_row
        )

    async def verify_mfa(
        self,
        mfa_token: str,
        code: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> IssuedSession:
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

        issued = await self._issue(user.user_id, now)
        roles = await self._sessions.get_role_names(user.user_id)
        await self._audit_event(
            user,
            "login_success",
            target_id=issued.session.session_id,
            ip_address=ip_address,
            user_agent=user_agent,
            details={"roles": roles, "mfa": True},
        )
        return issued

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
        refresh_token: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> IssuedSession:
        """Rotate a session on its **refresh** token — ADR-0010 §2 and A3.

        Implemented as rotation rather than an ``UPDATE`` to ``expires_at`` because A3 requires that
        "each successful refresh issues a new token and revokes its predecessor", which is what
        makes a stolen refresh token single-use and its reuse detectable.

        **Keyed on the refresh credential, and checked against ``refresh_expires_at``.** The whole
        point of A3's split is that the access token expires while the session stays refreshable, so
        a refresh path that resolved the access token (or checked ``expires_at``) would refuse
        exactly the case it exists to serve — and would require the client to hold a live access
        token in order to replace one, which is circular.

        A revoked or refresh-expired session is refused rather than forgiven: that is what
        revocation means, and extending one would make logout advisory. A pre-A3 session with no
        refresh credential simply does not resolve, which is the honest answer — there is no token
        to present, because none was ever issued.
        """
        now = datetime.now(UTC)
        current = await self._sessions.get_active_by_refresh_token(refresh_token)
        if (
            current is None
            or current.revoked_at is not None
            or current.refresh_expires_at is None
            or current.refresh_expires_at < now
        ):
            raise UnauthenticatedError(_SESSION_REJECTION)

        successor = await self._issue(current.user_id, now)
        # Revoked, not deleted: the row is the evidence that these credentials existed and when
        # they stopped being valid, which is what makes a later replay attempt legible.
        current.revoked_at = now

        user = await self._users.get_by_id(current.user_id)
        await self._audit_event(
            user,
            "session_refreshed",
            target_id=successor.session.session_id,
            ip_address=ip_address,
            user_agent=user_agent,
            details={"revoked_session_id": str(current.session_id)},
        )
        return successor

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
