"""Unit of Work — the per-request transaction boundary (guide Part 3).

One transaction per request/use-case: committed once at the end of a successful
service call, rolled back entirely on any exception — never a partial commit.

``platform`` stays domain-agnostic (it is the lowest layer in the import DAG and
may never import a module — enforced by the import-linter ``platform is
domain-agnostic`` contract in pyproject.toml). So this is the GENERIC base:
session + commit/rollback only. Each module defines a concrete UoW that
subclasses this and attaches its own repositories and ``OutboxWriter(schema=...)``
(see ``modules/<name>/uow.py``), which is what gives the outbox write and the
business write a single shared transaction (event-driven-architecture.md §16).
"""

from __future__ import annotations

from types import TracebackType

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.platform.db.session import get_session


class UnitOfWork:
    """Generic transaction boundary. Subclassed per module to add repositories."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def __aenter__(self) -> UnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            await self.rollback()

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()


async def get_unit_of_work(session: AsyncSession = Depends(get_session)) -> UnitOfWork:
    """FastAPI dependency for the generic UoW. Modules provide their own concrete
    ``get_<module>_uow`` returning their subclass."""
    return UnitOfWork(session)
