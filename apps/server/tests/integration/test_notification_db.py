"""Keyset pagination of ``list_for_recipient`` against a real Postgres.

The unit tier proves the service's cursor arithmetic against an in-memory fake that *reproduces*
the repository contract; this proves Postgres itself honours it — specifically the
``tuple_(created_at, notification_id) < (cursor…)`` row-value comparison, whose semantics a
Python tuple sort can only imitate. Ties on ``created_at`` are the interesting case, so the seed
deliberately places several rows on one identical timestamp and pages across them.

Runs in a throwaway **database** created and dropped here (the ``test_migrations.py`` pattern):
the ORM models are pinned to the ``notification`` schema, the CI integration job does not apply
migrations, and a dev database must never be seeded with test rows — so the test builds exactly
the production table definitions (``Base.metadata.create_all`` limited to this module's tables)
in its own database and destroys it afterwards.

Skips cleanly when no Postgres is reachable. Never fakes a pass.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from sentinelai.modules.notification.models import Notification, NotificationRule
from sentinelai.modules.notification.repository import NotificationRepository
from sentinelai.platform.config import settings
from sentinelai.platform.db.base import Base

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_BASE_TIME = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


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
    name = f"sentinelai_notiftest_{uuid.uuid4().hex[:8]}"
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
    """Create the module's REAL table definitions (and only those) in the throwaway database.

    ``notification_rules`` rides along because ``notifications.rule_id`` declares an FK to it.
    """
    async with engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS notification"))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[NotificationRule.__table__, Notification.__table__],
        )


def _notification(recipient: UUID, *, offset_minutes: int) -> Notification:
    return Notification(
        rule_id=None,
        recipient_user_id=recipient,
        source_module="ingestion",
        source_reference_id=uuid.uuid4(),
        message=f"alert +{offset_minutes}",
        created_at=_BASE_TIME + timedelta(minutes=offset_minutes),
        read_at=None,
    )


async def test_keyset_pagination_pages_a_real_inbox_exactly_once_in_order() -> None:
    if not await _reachable(_URL):
        pytest.skip(
            f"no Postgres reachable at {_URL.split('@')[-1]} — set TEST_DATABASE_URL to run"
        )

    database, url = await _create_throwaway_database()
    engine = create_async_engine(url)
    try:
        await _create_tables(engine)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        recipient = uuid.uuid4()
        bystander = uuid.uuid4()
        async with session_factory() as session:
            repo = NotificationRepository(session)
            # 8 rows for the recipient: minutes 0..4 distinct, then THREE sharing minute 4 —
            # the tie the row-value comparison must break by id, across a page boundary.
            for minutes in (0, 1, 2, 3, 4, 4, 4, 4):
                await repo.add(_notification(recipient, offset_minutes=minutes))
            # Two rows for someone else: must never appear in the recipient's pages.
            for minutes in (2, 4):
                await repo.add(_notification(bystander, offset_minutes=minutes))
            await session.commit()

        # Page through with limit=3, reproducing the service's cursor arithmetic
        # (repo returns up to limit+1; the extra row is only the has_more probe).
        limit = 3
        pages: list[list[Notification]] = []
        cursor: tuple[datetime, UUID] | None = None
        async with session_factory() as session:
            repo = NotificationRepository(session)
            for _ in range(10):  # bounded — a non-advancing cursor must not loop forever
                rows = await repo.list_for_recipient(
                    recipient,
                    limit=limit,
                    cursor_created_at=cursor[0] if cursor else None,
                    cursor_notification_id=cursor[1] if cursor else None,
                )
                has_more = len(rows) > limit
                page = list(rows[:limit])
                pages.append(page)
                if not has_more:
                    break
                cursor = (page[-1].created_at, page[-1].notification_id)

        collected = [n for page in pages for n in page]

        # Every seeded row exactly once — no gaps, no repeats, even across the timestamp tie.
        assert len(collected) == 8
        assert len({n.notification_id for n in collected}) == 8
        # Nobody else's notifications leak in (scoping is in the SQL itself).
        assert all(n.recipient_user_id == recipient for n in collected)
        # Strict global order: (created_at, notification_id) both DESC, across page boundaries.
        keys = [(n.created_at, n.notification_id) for n in collected]
        assert keys == sorted(keys, reverse=True)
        # The page shape: 3 + 3 + 2, and the probe row never leaked into a page.
        assert [len(p) for p in pages] == [3, 3, 2]
    finally:
        await engine.dispose()
        await _drop_throwaway_database(database)


async def test_a_cursor_landing_inside_a_timestamp_tie_does_not_skip_the_remaining_ties() -> None:
    """Five rows on ONE timestamp, pages of 2: every boundary falls inside the tie."""
    if not await _reachable(_URL):
        pytest.skip(
            f"no Postgres reachable at {_URL.split('@')[-1]} — set TEST_DATABASE_URL to run"
        )

    database, url = await _create_throwaway_database()
    engine = create_async_engine(url)
    try:
        await _create_tables(engine)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        recipient = uuid.uuid4()
        async with session_factory() as session:
            repo = NotificationRepository(session)
            for _ in range(5):
                await repo.add(_notification(recipient, offset_minutes=0))  # identical timestamps
            await session.commit()

        seen: list[UUID] = []
        cursor: tuple[datetime, UUID] | None = None
        async with session_factory() as session:
            repo = NotificationRepository(session)
            for _ in range(10):
                rows = await repo.list_for_recipient(
                    recipient,
                    limit=2,
                    cursor_created_at=cursor[0] if cursor else None,
                    cursor_notification_id=cursor[1] if cursor else None,
                )
                page = list(rows[:2])
                seen.extend(n.notification_id for n in page)
                if len(rows) <= 2:
                    break
                cursor = (page[-1].created_at, page[-1].notification_id)

        assert len(seen) == 5
        assert len(set(seen)) == 5  # a created_at-only cursor would repeat or drop tie rows here
    finally:
        await engine.dispose()
        await _drop_throwaway_database(database)
