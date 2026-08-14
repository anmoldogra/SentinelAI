"""Generic persist-only repository base (guide Part 5, ADR-0005).

A repository **persists and reloads** aggregates for one module. It holds **no business logic**
(that lives in the service) and **never commits** — the entrypoint's Unit of Work owns the
transaction boundary (ADR-0005), so a repository only stages work on the shared session.

This base captures the operations every repository shares — ``add`` (stage + flush), ``get`` (load
by primary key), ``delete`` (stage removal). A concrete repository subclasses it, binds its model,
and adds its own typed queries::

    class CaseRepository(Repository[Case]):
        def __init__(self, session: AsyncSession) -> None:
            super().__init__(session, Case)

        async def list_open(self, owner_id: UUID) -> Sequence[Case]:
            ...  # module-specific, keyset-paginated query lives here

The base deliberately exposes **no generic ``list``**: listing is query-shaped per aggregate
(filters, keyset pagination — guide Part 19) and belongs in the subclass. ``delete`` must never be
used on an evidentiary/append-only table (ADR-0004 revokes it at the DB-role level as well).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.platform.db.base import Base


class Repository[ModelT: Base]:
    """Async, persist-only base for a single ORM model.

    Args:
        session: the request-scoped session opened by the Unit of Work.
        model: the ORM model class this repository persists.
    """

    def __init__(self, session: AsyncSession, model: type[ModelT]) -> None:
        self._session = session
        self._model = model

    async def add(self, entity: ModelT) -> ModelT:
        """Stage ``entity`` and flush so integrity errors surface inside the transaction.

        Flush is not a commit: the entrypoint ``UnitOfWork`` commits exactly once at the request
        boundary (ADR-0005). Returns the same instance for call-site convenience.
        """
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def get(self, pk: Any) -> ModelT | None:
        """Return the row by primary key (identity-map lookup), or ``None`` if it does not exist."""
        return await self._session.get(self._model, pk)

    async def delete(self, entity: ModelT) -> None:
        """Stage ``entity`` for deletion.

        Never call this on an evidentiary/append-only table — deletion there is revoked at the
        database-role level (ADR-0004) and would raise regardless.
        """
        await self._session.delete(entity)
