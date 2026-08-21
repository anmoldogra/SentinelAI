"""Session/user auth queries against a real Postgres.

The unit tier proves *what login decides* against fakes. This proves the part fakes cannot: that
ADR-0010 §1's token design actually resolves a bearer token in Postgres — the indexed
``token_lookup`` prefix narrows the rows, and the real argon2id ``token_hash`` picks the right one
out of them. Prefix collisions and the case-insensitive email lookup are the interesting cases,
so both are seeded deliberately.

Runs in a throwaway **database** created and dropped here (the ``test_migrations.py`` pattern):
the ORM models are pinned to the ``platform`` schema, the CI integration job does not apply
migrations, and a dev database must never be seeded with test rows.

Skips cleanly when no Postgres is reachable. Never fakes a pass.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.platform.auth.models import Role, Session, User, UserRole
from sentinelai.platform.auth.repository import SessionRepository, UserRepository
from sentinelai.platform.config import settings
from sentinelai.platform.db.base import Base
from sentinelai.platform.security.hashing import Argon2PasswordHasher
from sentinelai.platform.security.tokens import LOOKUP_PREFIX_LENGTH, generate_opaque_token

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


async def _reachable(url: str) -> bool:
    try:
        # Explicit short timeout: the probe decides skip-vs-run and must never stall the suite.
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
    """CREATE a uniquely-named database; returns ``(name, url)``."""
    name = f"sentinelai_authtest_{uuid.uuid4().hex[:8]}"
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
    """Create the REAL table definitions these queries touch, and only those."""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS platform"))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                User.__table__,
                Role.__table__,
                UserRole.__table__,
                Session.__table__,
            ],
        )


def _user(email: str) -> User:
    return User(
        external_idp_subject=None,
        email=email,
        display_name="Analyst",
        password_hash=None,
        status="active",
        created_at=_NOW,
        updated_at=_NOW,
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


async def test_token_resolves_to_its_own_session_despite_a_prefix_collision(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Two live tokens sharing a lookup prefix must each resolve to their own row.

    The prefix index is non-unique precisely so this degrades into an extra argon2 verify rather
    than a rejected login, and only the full-token digest decides the match.
    """
    async with db() as session:
        repo = SessionRepository(session, Argon2PasswordHasher())
        user = _user("collide@example.gov")
        session.add(user)
        await session.flush()

        # Two distinct tokens that genuinely share a lookup prefix, so both rows really do land
        # on one index key — the collision must be in the tokens, not patched into the rows.
        first = generate_opaque_token()
        second = first[:LOOKUP_PREFIX_LENGTH] + generate_opaque_token()[LOOKUP_PREFIX_LENGTH:]
        assert first != second

        row_a = await repo.create_session(
            user_id=user.user_id, token=first, issued_at=_NOW, expires_at=_NOW + timedelta(hours=8)
        )
        row_b = await repo.create_session(
            user_id=user.user_id, token=second, issued_at=_NOW, expires_at=_NOW + timedelta(hours=8)
        )
        await session.commit()
        assert row_a.token_lookup == row_b.token_lookup

        assert (await repo.get_active_by_token(first)).session_id == row_a.session_id  # type: ignore[union-attr]
        assert (await repo.get_active_by_token(second)).session_id == row_b.session_id  # type: ignore[union-attr]


async def test_stored_row_holds_a_digest_and_prefix_never_the_token(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        repo = SessionRepository(session, Argon2PasswordHasher())
        user = _user("stored@example.gov")
        session.add(user)
        await session.flush()

        token = generate_opaque_token()
        row = await repo.create_session(
            user_id=user.user_id, token=token, issued_at=_NOW, expires_at=_NOW + timedelta(hours=8)
        )
        await session.commit()

        assert row.token_hash.startswith("$argon2id$")
        assert token not in row.token_hash
        assert row.token_lookup == token[:LOOKUP_PREFIX_LENGTH]
        # The full token is not recoverable from anything the row persists.
        assert token not in (row.token_lookup + row.token_hash)


async def test_an_unknown_token_resolves_to_nothing(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        repo = SessionRepository(session, Argon2PasswordHasher())
        user = _user("unknown@example.gov")
        session.add(user)
        await session.flush()
        await repo.create_session(
            user_id=user.user_id,
            token=generate_opaque_token(),
            issued_at=_NOW,
            expires_at=_NOW + timedelta(hours=8),
        )
        await session.commit()

        assert await repo.get_active_by_token(generate_opaque_token()) is None


async def test_role_names_come_back_for_the_right_user(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        repo = SessionRepository(session, Argon2PasswordHasher())
        granted, ungranted = _user("roles@example.gov"), _user("noroles@example.gov")
        session.add_all([granted, ungranted])
        investigator = Role(name="investigator", description="")
        supervisor = Role(name="supervisor", description="")
        session.add_all([investigator, supervisor])
        await session.flush()
        session.add_all(
            [
                UserRole(user_id=granted.user_id, role_id=investigator.role_id, granted_at=_NOW),
                UserRole(user_id=granted.user_id, role_id=supervisor.role_id, granted_at=_NOW),
            ]
        )
        await session.commit()

        assert await repo.get_role_names(granted.user_id) == ["investigator", "supervisor"]
        assert await repo.get_role_names(ungranted.user_id) == []


async def test_email_lookup_is_case_insensitive_and_not_a_pattern(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """``%`` must be a literal character, not a wildcard — otherwise it matches any account."""
    async with db() as session:
        repo = UserRepository(session)
        session.add(_user("Analyst@Example.gov"))
        await session.commit()

        assert (await repo.get_by_email("analyst@example.gov")) is not None
        assert (await repo.get_by_email("ANALYST@EXAMPLE.GOV")) is not None
        assert (await repo.get_by_email("%@%")) is None
        assert (await repo.get_by_email("_nalyst@example.gov")) is None
