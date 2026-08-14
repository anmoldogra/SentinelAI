"""Repository base tests (W1-05a).

The base is a thin persist-only wrapper (ADR-0005: it never commits), so its contract is *which
session calls it makes*, not database behaviour (that is SQLAlchemy's). A recording session double
verifies the delegation deterministically without a live Postgres.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from sentinelai.platform.db.base import Base
from sentinelai.platform.db.repository import Repository


class _Widget(Base):
    """Throwaway ORM model in an isolated schema — never created against a real database here."""

    __tablename__ = "widget"
    __table_args__ = ({"schema": "test_repository"},)

    id: Mapped[str] = mapped_column(primary_key=True)


class _RecordingSession:
    """Captures the session operations the repository performs."""

    def __init__(self, *, get_result: Any = None) -> None:
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.flushed = 0
        self.get_calls: list[tuple[type, Any]] = []
        self._get_result = get_result

    def add(self, entity: Any) -> None:
        self.added.append(entity)

    async def flush(self) -> None:
        self.flushed += 1

    async def get(self, model: type, pk: Any) -> Any:
        self.get_calls.append((model, pk))
        return self._get_result

    async def delete(self, entity: Any) -> None:
        self.deleted.append(entity)


def _repo(session: _RecordingSession) -> Repository[_Widget]:
    return Repository(cast(AsyncSession, session), _Widget)


async def test_add_stages_flushes_and_returns_entity() -> None:
    session = _RecordingSession()
    entity = _Widget(id="w1")
    result = await _repo(session).add(entity)
    assert session.added == [entity]  # staged on the session
    assert session.flushed == 1  # flushed so integrity errors surface in-transaction
    assert result is entity  # returned for call-site convenience


async def test_get_delegates_to_identity_lookup() -> None:
    target = _Widget(id="w2")
    session = _RecordingSession(get_result=target)
    got = await _repo(session).get("w2")
    assert got is target
    assert session.get_calls == [(_Widget, "w2")]


async def test_get_returns_none_when_absent() -> None:
    session = _RecordingSession(get_result=None)
    assert await _repo(session).get("missing") is None


async def test_delete_stages_entity_for_removal() -> None:
    session = _RecordingSession()
    entity = _Widget(id="w3")
    await _repo(session).delete(entity)
    assert session.deleted == [entity]


async def test_repository_never_commits() -> None:
    # ADR-0005: only the entrypoint UoW commits. The recording session exposes no `commit`, so if
    # the base ever called one these operations would raise — they don't.
    session = _RecordingSession()
    assert not hasattr(session, "commit")
    await _repo(session).add(_Widget(id="w4"))
    await _repo(session).delete(_Widget(id="w4"))
    await _repo(session).get("w4")
