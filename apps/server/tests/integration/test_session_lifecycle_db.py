"""Session lifecycle and MFA enforcement against a real Postgres — ADR-0010 §2/§3, Wave 3.1.

What this proves that the unit tier cannot:

* an **enrolled** account cannot obtain a session with a password alone (security §8 makes the
  second factor mandatory, and until Wave 3.1 the enrolment columns existed and nothing read them);
* refresh **rotates** — the successor is a different token and the predecessor is revoked, so a
  stolen token is single-use and its reuse is detectable (ADR-0010 A3);
* logout revokes **immediately**, which is the hard requirement that ruled out stateless JWTs;
* the plaintext of every credential this module mints is absent from the database.

Runs in a throwaway database created and dropped here. Skips cleanly when no Postgres is
reachable; never fakes a pass.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.platform.auth.models import (
    MfaChallenge,
    MfaRecoveryCode,
    Role,
    Session,
    User,
    UserRole,
)
from sentinelai.platform.auth.repository import (
    MfaRepository,
    SessionRepository,
    UserRepository,
)
from sentinelai.platform.auth.service import AuthService
from sentinelai.platform.config import settings
from sentinelai.platform.db.base import Base
from sentinelai.platform.security import totp
from sentinelai.platform.security.hashing import Argon2PasswordHasher
from sentinelai.shared.exceptions import UnauthenticatedError
from tests.fixtures.kms import kms_for_tests

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_NOW = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
_PASSWORD = "correct-horse-battery-staple"


async def _reachable(url: str) -> bool:
    try:
        engine = create_async_engine(url, connect_args={"timeout": 3})
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()
        return True
    except Exception:
        return False


async def _create_throwaway_database() -> tuple[str, str]:
    name = f"sentinelai_sesstest_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()
    return name, _URL.rsplit("/", 1)[0] + f"/{name}"


async def _drop_throwaway_database(name: str) -> None:
    admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await admin.dispose()


async def _create_tables(engine: AsyncEngine) -> None:
    """Only the tables these flows touch — plus ``audit_log``, because every one of them writes
    to it and a missing table would fail the test for the wrong reason."""
    from sentinelai.platform.auth.models import AuditLog

    async with engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS platform"))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                User.__table__,
                Role.__table__,
                UserRole.__table__,
                Session.__table__,
                MfaChallenge.__table__,
                MfaRecoveryCode.__table__,
                AuditLog.__table__,
            ],
        )


@pytest.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if not await _reachable(_URL):
        pytest.skip(
            f"no Postgres reachable at {_URL.split('@')[-1]} — set TEST_DATABASE_URL to run"
        )
    database, url = await _create_throwaway_database()
    engine = create_async_engine(url)
    try:
        await _create_tables(engine)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        await _drop_throwaway_database(database)


def _service(session: AsyncSession) -> AuthService:
    kms = kms_for_tests()
    return AuthService(
        session,
        UserRepository(session),
        SessionRepository(session),
        kms=kms,
        mfa=MfaRepository(session, kms),
    )


async def _seed_user(session: AsyncSession, email: str) -> User:
    user = User(
        external_idp_subject=None,
        email=email,
        display_name="Analyst",
        password_hash=Argon2PasswordHasher().hash(_PASSWORD),
        status="active",
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(user)
    await session.flush()
    return user


# --- MFA enforcement --------------------------------------------------------
async def test_an_enrolled_user_gets_a_challenge_not_a_session(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The gap Wave 3.1 closed. Before it, enrolment was storage with nothing reading it, so a
    correct password alone opened a session for an account that had elected a second factor."""
    async with db() as session:
        user = await _seed_user(session, "enrolled@example.gov")
        kms = kms_for_tests()
        await MfaRepository(session, kms).store_secret(
            user_id=user.user_id, secret=totp.generate_secret(), enrolled_at=_NOW
        )

        outcome = await _service(session).login(user.email, _PASSWORD)

        assert outcome.session is None, (
            "a password alone must not open a session for an enrolled account"
        )
        assert outcome.token
        rows = (await session.execute(select(Session))).scalars().all()
        assert rows == [], "no session row may exist until the second factor is verified"


async def test_the_second_factor_completes_the_login(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        user = await _seed_user(session, "totp@example.gov")
        secret = totp.generate_secret()
        kms = kms_for_tests()
        await MfaRepository(session, kms).store_secret(
            user_id=user.user_id, secret=secret, enrolled_at=_NOW
        )
        service = _service(session)
        outcome = await service.login(user.email, _PASSWORD)

        now = datetime.now(UTC)
        code = totp.compute_code(secret, totp.current_step(now))
        token, session_row = await service.verify_mfa(outcome.token, code)

        assert token and session_row.user_id == user.user_id
        assert session_row.revoked_at is None


async def test_a_wrong_code_is_refused_and_burns_the_challenge(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The challenge is consumed before the code is checked, so a stolen ``mfa_token`` cannot be
    used to brute-force six digits — each attempt costs a fresh password login."""
    async with db() as session:
        user = await _seed_user(session, "wrongcode@example.gov")
        secret = totp.generate_secret()
        kms = kms_for_tests()
        await MfaRepository(session, kms).store_secret(
            user_id=user.user_id, secret=secret, enrolled_at=_NOW
        )
        service = _service(session)
        outcome = await service.login(user.email, _PASSWORD)

        with pytest.raises(UnauthenticatedError):
            await service.verify_mfa(outcome.token, "000000")

        # Even the *correct* code cannot rescue that challenge now.
        correct = totp.compute_code(secret, totp.current_step(datetime.now(UTC)))
        with pytest.raises(UnauthenticatedError):
            await service.verify_mfa(outcome.token, correct)


async def test_an_mfa_token_cannot_be_replayed(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """``consumed_at`` is what stops one password login from minting two sessions."""
    async with db() as session:
        user = await _seed_user(session, "replay@example.gov")
        secret = totp.generate_secret()
        kms = kms_for_tests()
        await MfaRepository(session, kms).store_secret(
            user_id=user.user_id, secret=secret, enrolled_at=_NOW
        )
        service = _service(session)
        outcome = await service.login(user.email, _PASSWORD)
        code = totp.compute_code(secret, totp.current_step(datetime.now(UTC)))
        await service.verify_mfa(outcome.token, code)

        with pytest.raises(UnauthenticatedError):
            await service.verify_mfa(outcome.token, code)


async def test_an_expired_challenge_is_refused(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        user = await _seed_user(session, "expired@example.gov")
        secret = totp.generate_secret()
        kms = kms_for_tests()
        mfa = MfaRepository(session, kms)
        await mfa.store_secret(user_id=user.user_id, secret=secret, enrolled_at=_NOW)

        stale = "expired-challenge-token-" + uuid.uuid4().hex
        past = datetime.now(UTC) - timedelta(hours=1)
        await mfa.create_challenge(
            user_id=user.user_id,
            token=stale,
            issued_at=past,
            expires_at=past + timedelta(minutes=5),
        )
        code = totp.compute_code(secret, totp.current_step(datetime.now(UTC)))

        with pytest.raises(UnauthenticatedError):
            await _service(session).verify_mfa(stale, code)


async def test_an_unenrolled_user_still_logs_in_with_a_password_alone(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The MFA branch must not become an unconditional gate: accounts with no second factor
    enrolled still authenticate, or Wave 3.1 would have locked every existing user out."""
    async with db() as session:
        user = await _seed_user(session, "plain@example.gov")
        outcome = await _service(session).login(user.email, _PASSWORD)
        assert outcome.session is not None


# --- session lifecycle ------------------------------------------------------
async def test_refresh_rotates_and_revokes_its_predecessor(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """ADR-0010 A3: rotation is what makes a stolen refresh token single-use."""
    async with db() as session:
        user = await _seed_user(session, "rotate@example.gov")
        service = _service(session)
        first = await service.login(user.email, _PASSWORD)
        assert first.session is not None

        second_token, second = await service.refresh(first.token)

        assert second_token != first.token
        assert second.session_id != first.session.session_id
        assert first.session.revoked_at is not None, (
            "the predecessor must be revoked, not left live"
        )
        assert second.revoked_at is None


async def test_a_rotated_token_cannot_be_used_again(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Reuse detection is the point of rotation; a predecessor that still refreshed would make
    the whole scheme decorative."""
    async with db() as session:
        user = await _seed_user(session, "reuse@example.gov")
        service = _service(session)
        first = await service.login(user.email, _PASSWORD)
        await service.refresh(first.token)

        with pytest.raises(UnauthenticatedError):
            await service.refresh(first.token)


async def test_logout_revokes_immediately(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The hard requirement that ruled out stateless JWTs (ADR-0010 §2): a compromised session
    must be killable now, not at expiry."""
    async with db() as session:
        user = await _seed_user(session, "logout@example.gov")
        service = _service(session)
        issued = await service.login(user.email, _PASSWORD)
        assert issued.session is not None

        await service.logout(issued.token)

        assert issued.session.revoked_at is not None
        assert await SessionRepository(session).get_active_by_token(issued.token) is not None, (
            "the row still resolves — revocation is a state on it, not a deletion, so a later "
            "replay attempt stays legible"
        )
        with pytest.raises(UnauthenticatedError):
            await service.refresh(issued.token)


async def test_logout_is_idempotent_and_silent(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """A second logout, and a logout of something that was never a token, both return normally:
    a failure here would tell a holder of a stale token whether it was ever real."""
    async with db() as session:
        user = await _seed_user(session, "twice@example.gov")
        service = _service(session)
        issued = await service.login(user.email, _PASSWORD)

        await service.logout(issued.token)
        await service.logout(issued.token)
        await service.logout("not-a-token-that-was-ever-issued")


async def test_a_revoked_session_cannot_be_refreshed_back_to_life(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        user = await _seed_user(session, "revoked@example.gov")
        service = _service(session)
        issued = await service.login(user.email, _PASSWORD)
        assert issued.session is not None
        issued.session.revoked_at = datetime.now(UTC)
        await session.flush()

        with pytest.raises(UnauthenticatedError):
            await service.refresh(issued.token)


async def test_an_expired_session_cannot_be_refreshed(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Sliding expiry slides only while the session is alive — otherwise `expires_at` would
    never actually expire anything."""
    async with db() as session:
        user = await _seed_user(session, "stale@example.gov")
        service = _service(session)
        issued = await service.login(user.email, _PASSWORD)
        assert issued.session is not None
        issued.session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.flush()

        with pytest.raises(UnauthenticatedError):
            await service.refresh(issued.token)


# --- token-hash security ----------------------------------------------------
async def test_no_credential_plaintext_reaches_the_database(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """ADR-0010 §1: the bearer token and the ``mfa_token`` are returned once and never stored.

    Scans the actual column values rather than trusting the write path, because "we hash it" is
    the kind of claim that survives a refactor that stops being true.
    """
    async with db() as session:
        enrolled = await _seed_user(session, "scan-mfa@example.gov")
        kms = kms_for_tests()
        await MfaRepository(session, kms).store_secret(
            user_id=enrolled.user_id, secret=totp.generate_secret(), enrolled_at=_NOW
        )
        plain = await _seed_user(session, "scan-session@example.gov")
        service = _service(session)

        challenge = await service.login(enrolled.email, _PASSWORD)
        issued = await service.login(plain.email, _PASSWORD)
        await session.flush()

        session_rows = (await session.execute(select(Session))).scalars().all()
        challenge_rows = (await session.execute(select(MfaChallenge))).scalars().all()

        for row in session_rows:
            assert row.token_hash != issued.token
            assert issued.token not in row.token_hash
        for row in challenge_rows:
            assert row.token_hash != challenge.token
            assert challenge.token not in row.token_hash

        # The lookup prefix IS derived from the token and is meant to be — it is a non-secret
        # index key, not a credential. What matters is that it is too short to be the token.
        for row in session_rows:
            assert len(row.token_lookup) < len(issued.token)


async def test_a_recovery_code_completes_a_login_and_is_single_use(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The fallback factor (security §8). A user whose authenticator is lost must have a way in
    that is not "an administrator turns your second factor off"."""
    from sentinelai.platform.security.tokens import generate_recovery_code

    async with db() as session:
        user = await _seed_user(session, "recovery@example.gov")
        kms = kms_for_tests()
        mfa = MfaRepository(session, kms)
        await mfa.store_secret(
            user_id=user.user_id, secret=totp.generate_secret(), enrolled_at=_NOW
        )
        codes = [generate_recovery_code() for _ in range(3)]
        await mfa.replace_recovery_codes(user_id=user.user_id, codes=codes, created_at=_NOW)
        service = _service(session)

        first = await service.login(user.email, _PASSWORD)
        token, session_row = await service.verify_mfa(first.token, codes[0])
        assert token and session_row.user_id == user.user_id

        # The same code cannot be redeemed twice, even on a fresh challenge.
        second = await service.login(user.email, _PASSWORD)
        with pytest.raises(UnauthenticatedError):
            await service.verify_mfa(second.token, codes[0])

        # A different, unused code still works.
        third = await service.login(user.email, _PASSWORD)
        again, _ = await service.verify_mfa(third.token, codes[1])
        assert again


async def test_recovery_codes_are_stored_hashed_not_in_the_clear(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Unlike the TOTP secret, a recovery code is only ever *compared* — so it is hashed, and the
    column must not contain the value handed to the user."""
    from sentinelai.platform.security.tokens import generate_recovery_code

    async with db() as session:
        user = await _seed_user(session, "codehash@example.gov")
        codes = [generate_recovery_code() for _ in range(2)]
        await MfaRepository(session, kms_for_tests()).replace_recovery_codes(
            user_id=user.user_id, codes=codes, created_at=_NOW
        )
        await session.flush()

        rows = (await session.execute(select(MfaRecoveryCode))).scalars().all()

    assert len(rows) == 2
    for row in rows:
        assert row.code_hash not in codes
        for code in codes:
            assert code not in row.code_hash


def test_recovery_codes_avoid_transcription_confusable_characters() -> None:
    """A code is read off a screen and typed back. An alphabet containing both ``0`` and ``O``
    guarantees support tickets, so the generator excludes the confusable letters outright."""
    from sentinelai.platform.security.tokens import generate_recovery_code

    codes = [generate_recovery_code() for _ in range(200)]
    assert all(len(c) == 11 and c[5] == "-" for c in codes), "5-5 grouping with a hyphen"
    body = "".join(c.replace("-", "") for c in codes)
    assert not (set(body) & set("ILOU01")), "no confusable characters may appear"
    assert len(set(codes)) == len(codes), "codes must not repeat"
