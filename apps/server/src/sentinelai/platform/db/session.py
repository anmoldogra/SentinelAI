"""Async engine + session factory + request-scoped session dependency — guide Part 2.

The async engine forbids implicit lazy loading (``expire_on_commit=False`` keeps
loaded attributes usable after commit; unloaded relationships must be eager-loaded
explicitly with ``selectinload`` — guide Part 3 "Eager vs. Lazy Loading").
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.platform.config import settings

engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
    future=True,
)

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding one request-scoped session."""
    async with async_session_factory() as session:
        yield session


async def dispose_engine() -> None:
    """Dispose the connection pool. Called from entrypoint lifespan shutdown."""
    await engine.dispose()
