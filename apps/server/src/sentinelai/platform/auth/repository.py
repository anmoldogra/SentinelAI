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

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy import CursorResult, Executable, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.platform.auth.models import (
    MfaChallenge,
    MfaRecoveryCode,
    Role,
    Session,
    User,
    UserRole,
)
from sentinelai.platform.crypto import (
    Algorithm,
    Ciphertext,
    KeyId,
    KeyManagementService,
    KeyPurpose,
    KeyRef,
    ProviderKind,
    get_kms,
)
from sentinelai.platform.db.session import get_session
from sentinelai.platform.security.hashing import Argon2PasswordHasher, PasswordHasher
from sentinelai.platform.security.tokens import token_lookup_prefix
from sentinelai.shared.exceptions import NotFoundError

# A *named* key under SESSION_ROOT (ADR-0009 §7 reserves that purpose for "ADR-0010 token/secret
# keying"), so MFA secrets and session material rotate independently — re-keying one need not
# invalidate the other.
MFA_SECRET_KEY = KeyRef(purpose=KeyPurpose.SESSION_ROOT, name="mfa_secret")


def _mfa_aad(user_id: UUID) -> bytes:
    """Additional authenticated data binding a secret's ciphertext to its owner.

    Without it, anyone able to write to `platform.users` could copy a known-plaintext MFA
    ciphertext onto another account and pass that account's second factor. With it, the AEAD tag
    fails and the decrypt raises. The cost is that ciphertext is deliberately non-portable between
    users: moving an enrolment would require a re-encrypt, not a column copy.
    """
    return f"mfa_secret:{user_id}".encode()


def _serialize_key_id(key_id: KeyId) -> str:
    """Render a ``KeyId`` for the ``mfa_secret_key_id`` column.

    **`provider:version:backend_ref`, with the ref last and parsed by `split(":", 2)`.** A
    backend ref is provider-shaped and may itself contain colons — an AWS KMS ARN
    (`arn:aws:kms:region:account:key/id`) is the obvious case — so putting it anywhere but last
    would make the format ambiguous the first time SentinelAI runs on AWS KMS.
    """
    return f"{key_id.provider.value}:{key_id.version}:{key_id.backend_ref}"


def _parse_key_id(raw: str) -> KeyId:
    provider, version, backend_ref = raw.split(":", 2)
    return KeyId(provider=ProviderKind(provider), backend_ref=backend_ref, version=int(version))


async def _affected_rows(session: AsyncSession, statement: Executable) -> int:
    """Execute a DML statement and return how many rows it changed.

    ``AsyncSession.execute`` is typed as returning ``Result``, which exposes no ``rowcount``; for
    DML the runtime object is always a ``CursorResult``, which does. Narrowing that once here
    keeps the cast — and the explanation for it — out of the five conditional-update call sites
    whose whole correctness rests on reading the count.
    """
    result = await session.execute(statement)
    return cast("CursorResult[Any]", result).rowcount


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


class MfaRepository:
    """Persists the TOTP second factor: the encrypted secret, the replay guard, recovery codes,
    and pending MFA challenges.

    Two transformations happen here rather than in the service, following ``SessionRepository``'s
    precedent of hashing a token at the persistence boundary: **encryption** of the shared secret,
    and **hashing** of recovery codes and challenge tokens. The point is containment — ciphertext
    columns, lookup prefixes, and digests never appear above this layer, so no caller can
    accidentally persist a plaintext secret or compare a digest by equality.

    What is deliberately *not* here: deciding whether a code is correct (that is
    ``security.totp``, a pure function) and deciding whether a login proceeds (that is the
    service). This layer answers "store this", "give me that", and "did I win the race".

    Every write flushes rather than commits — the transaction belongs to the entrypoint (ADR-0005).
    """

    def __init__(
        self,
        session: AsyncSession,
        kms: KeyManagementService,
        hasher: PasswordHasher | None = None,
    ) -> None:
        self._session = session
        self._kms = kms
        self._hasher = hasher if hasher is not None else Argon2PasswordHasher()

    # --- the shared secret (KMS envelope encryption) ------------------------

    async def store_secret(self, *, user_id: UUID, secret: str, enrolled_at: datetime) -> None:
        """Encrypt ``secret`` and write the enrolment in one statement.

        Raises :class:`NotFoundError` if no such user. KMS failures (provider unavailable, key
        disabled) propagate unchanged — an enrolment that cannot be encrypted must fail loudly,
        never fall back to plaintext.
        """
        ciphertext = await self._kms.encrypt(
            MFA_SECRET_KEY, secret.encode("utf-8"), _mfa_aad(user_id)
        )
        # All five columns together: the CHECK constraint rejects any partial write, which is what
        # makes "enrolled" and "has a usable secret" the same fact rather than two hopeful ones.
        affected = await _affected_rows(
            self._session,
            update(User)
            .where(User.user_id == user_id)
            .values(
                mfa_enrolled_at=enrolled_at,
                mfa_secret_ciphertext=ciphertext.value,
                mfa_secret_nonce=ciphertext.nonce,
                mfa_secret_algorithm=ciphertext.algorithm.value,
                mfa_secret_key_id=_serialize_key_id(ciphertext.key_id),
            )
            .execution_options(synchronize_session=False),
        )
        if affected != 1:
            raise NotFoundError(f"no user {user_id}")
        await self._session.flush()

    async def load_secret(self, user_id: UUID) -> str | None:
        """Return the decrypted TOTP secret, or ``None`` when the user is not enrolled.

        ``None`` is a normal state the caller branches on, not an error. A *decrypt* failure is
        the opposite — it means tampering or a lost key, never "no secret", because the CHECK
        constraint makes a partially-written row unrepresentable — so it propagates.
        """
        row = (
            await self._session.execute(
                select(
                    User.mfa_secret_ciphertext,
                    User.mfa_secret_nonce,
                    User.mfa_secret_algorithm,
                    User.mfa_secret_key_id,
                ).where(User.user_id == user_id)
            )
        ).one_or_none()
        if row is None:
            return None
        value, nonce, algorithm, key_id = row
        if value is None or nonce is None or algorithm is None or key_id is None:
            return None  # not enrolled; the CHECK guarantees these are null together
        plaintext = await self._kms.decrypt(
            Ciphertext(
                value=value,
                nonce=nonce,
                algorithm=Algorithm(algorithm),
                key_id=_parse_key_id(key_id),
            ),
            _mfa_aad(user_id),
        )
        return plaintext.decode("utf-8")

    async def clear_secret(self, user_id: UUID) -> None:
        """Un-enrol: null all five secret columns together, as the CHECK requires.

        ``mfa_last_used_step`` is cleared too — leaving a high watermark behind would silently
        reject the first codes of a *new* enrolment, whose steps start from the current time and
        could fall below it. Raises :class:`NotFoundError` if no such user.
        """
        affected = await _affected_rows(
            self._session,
            update(User)
            .where(User.user_id == user_id)
            .values(
                mfa_enrolled_at=None,
                mfa_secret_ciphertext=None,
                mfa_secret_nonce=None,
                mfa_secret_algorithm=None,
                mfa_secret_key_id=None,
                mfa_last_used_step=None,
            )
            .execution_options(synchronize_session=False),
        )
        if affected != 1:
            raise NotFoundError(f"no user {user_id}")
        await self._session.flush()

    # --- replay guard -------------------------------------------------------

    async def claim_totp_step(self, *, user_id: UUID, step: int) -> bool:
        """Atomically claim ``step``, returning whether this caller won it.

        RFC 6238 §5.2 requires a verifier to reject a code it has already accepted. **A single
        conditional UPDATE, never read-then-write**: two requests presenting the same code arrive
        concurrently in exactly the situation this guards against, and a Python-side comparison
        would let both through.

        ``<`` rather than ``!=`` so a code from an earlier step still inside the drift window is
        also refused — going backwards is as much a replay as repeating.

        ``False`` means replayed or lost the race; it is an answer, not an error.
        """
        return (
            await _affected_rows(
                self._session,
                update(User)
                .where(User.user_id == user_id)
                .where(or_(User.mfa_last_used_step.is_(None), User.mfa_last_used_step < step))
                .values(mfa_last_used_step=step)
                .execution_options(synchronize_session=False),
            )
            == 1
        )

    # --- recovery codes -----------------------------------------------------

    async def replace_recovery_codes(
        self, *, user_id: UUID, codes: Sequence[str], created_at: datetime
    ) -> None:
        """Hash and store ``codes`` as the user's complete set, discarding any previous one.

        Replace rather than append: §8 generates codes as a set at enrolment, and appending would
        silently keep older codes alive after a user regenerated them precisely because they
        believed the old ones compromised.
        """
        await self._session.execute(
            delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user_id)
        )
        for code in codes:
            self._session.add(
                MfaRecoveryCode(
                    user_id=user_id, code_hash=self._hasher.hash(code), created_at=created_at
                )
            )
        await self._session.flush()

    async def redeem_recovery_code(self, *, user_id: UUID, code: str, used_at: datetime) -> bool:
        """Verify ``code`` against the user's unused codes and spend it atomically.

        The digests are salted argon2id, so there is nothing to look up by equality — each unused
        candidate is verified in turn, exactly as ``get_active_by_token`` does. A code set is
        single-digit in size, so the cost is bounded and this path is rare by construction.

        The spend is a conditional UPDATE on ``used_at IS NULL``, so two concurrent redemptions of
        the same code cannot both succeed. ``False`` means no match *or* a lost race; a wrong code
        never raises.
        """
        candidates = (
            (
                await self._session.execute(
                    select(MfaRecoveryCode).where(
                        MfaRecoveryCode.user_id == user_id, MfaRecoveryCode.used_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        for candidate in candidates:
            if not self._hasher.verify(candidate.code_hash, code):
                continue
            affected = await _affected_rows(
                self._session,
                update(MfaRecoveryCode)
                .where(
                    MfaRecoveryCode.code_id == candidate.code_id,
                    MfaRecoveryCode.used_at.is_(None),
                )
                .values(used_at=used_at)
                .execution_options(synchronize_session=False),
            )
            return affected == 1
        return False

    async def count_unused_recovery_codes(self, user_id: UUID) -> int:
        """How many codes remain, so the caller can warn a user before they run out."""
        return (
            await self._session.execute(
                select(func.count())
                .select_from(MfaRecoveryCode)
                .where(MfaRecoveryCode.user_id == user_id, MfaRecoveryCode.used_at.is_(None))
            )
        ).scalar_one()

    # --- pending challenges (the `mfa_token` exchange) ----------------------

    async def create_challenge(
        self, *, user_id: UUID, token: str, issued_at: datetime, expires_at: datetime
    ) -> MfaChallenge:
        """Persist a pending MFA step for ``token``, storing only its digest and lookup prefix."""
        challenge = MfaChallenge(
            user_id=user_id,
            token_lookup=token_lookup_prefix(token),
            token_hash=self._hasher.hash(token),
            issued_at=issued_at,
            expires_at=expires_at,
            consumed_at=None,
        )
        self._session.add(challenge)
        await self._session.flush()
        return challenge

    async def get_challenge_by_token(self, token: str) -> MfaChallenge | None:
        """Resolve a challenge from an ``mfa_token`` — prefix seek, then verify each candidate.

        **Does not filter on expiry or consumption**, mirroring ``get_active_by_token``: the
        caller decides validity, as ``get_current_user`` does for sessions. That split is what
        lets the service distinguish "no such challenge" from "expired" and "already used", which
        are three different things to an operator reading an audit log.
        """
        candidates = (
            (
                await self._session.execute(
                    select(MfaChallenge).where(
                        MfaChallenge.token_lookup == token_lookup_prefix(token)
                    )
                )
            )
            .scalars()
            .all()
        )
        for candidate in candidates:
            if self._hasher.verify(candidate.token_hash, token):
                return candidate
        return None

    async def consume_challenge(self, *, challenge_id: UUID, consumed_at: datetime) -> bool:
        """Atomically mark a challenge used. ``False`` means it already was — a replayed token."""
        return (
            await _affected_rows(
                self._session,
                update(MfaChallenge)
                .where(
                    MfaChallenge.challenge_id == challenge_id,
                    MfaChallenge.consumed_at.is_(None),
                )
                .values(consumed_at=consumed_at)
                .execution_options(synchronize_session=False),
            )
            == 1
        )


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


async def get_mfa_repository(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> MfaRepository:
    """FastAPI dependency providing a request-scoped ``MfaRepository``.

    The KMS comes from application state rather than being constructed per request — it owns
    connection pools and a circuit breaker whose whole value is being shared across requests.
    """
    return MfaRepository(session, get_kms(request))
