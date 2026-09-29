"""A session double for the ADR-0012 idempotency guard, shared by the connector API suites.

Every "Yes (key)" endpoint in `api-design.md` §4.3 to §4.6 needs the same thing from a test: a
stand-in for the slice of ``AsyncSession`` the idempotency repository touches, honouring the claim's
uniqueness *and* its rollback. Three suites need it (`osint`, `threat_intel`, `forensics`); two had
already grown their own copy before this became one.

**Rollback is the part that must not be simplified away.** The claim row shares the request's
transaction, so "a failed submission leaves its key reusable" is a property of ROLLBACK. A double
that kept the row on rollback would assert the exact opposite of production, and the test would pass
while the behaviour it describes was broken.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError

from sentinelai.platform.idempotency.models import IdempotencyKey

__all__ = ["IdempotencySession"]

_Claim = tuple[Any, Any, Any]


class IdempotencySession:
    """The slice of ``AsyncSession`` the idempotency repository uses."""

    def __init__(self) -> None:
        self.rows: dict[_Claim, IdempotencyKey] = {}
        self._pending: list[_Claim] = []
        self.commits = 0

    def add(self, row: Any) -> None:
        if isinstance(row, IdempotencyKey):
            claim = (row.principal_id, row.idempotency_key, row.path)
            if claim in self.rows:
                # What `uq_idempotency_claim` does in production — the unique index *is* the
                # concurrency control (ADR-0012), so a double that merely overwrote would hide the
                # one race the design depends on catching.
                raise IntegrityError("duplicate claim", None, Exception("uq_idempotency_claim"))
            self.rows[claim] = row
            self._pending.append(claim)

    async def flush(self) -> None:
        return None

    async def delete(self, row: Any) -> None:
        claim = (row.principal_id, row.idempotency_key, row.path)
        self.rows.pop(claim, None)
        if claim in self._pending:
            self._pending.remove(claim)

    async def execute(self, statement: Any) -> _Result:
        return _Result(self, statement)

    def begin_nested(self) -> _Savepoint:
        return _Savepoint()

    async def commit(self) -> None:
        self.commits += 1
        self._pending.clear()

    async def rollback(self) -> None:
        for claim in self._pending:
            self.rows.pop(claim, None)
        self._pending.clear()


class _Savepoint:
    async def __aenter__(self) -> _Savepoint:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Result:
    """Reads the claim out of the guard's own ``SELECT`` rather than assuming its shape.

    Pulling the three bound values off the WHERE clause means a change to the lookup's columns shows
    up as a test that stops finding rows, instead of a double that quietly answers a different
    question than the code asks.
    """

    def __init__(self, session: IdempotencySession, statement: Any) -> None:
        self._session = session
        self._statement = statement

    def scalar_one_or_none(self) -> IdempotencyKey | None:
        found: dict[str, Any] = {}
        clauses = getattr(self._statement, "whereclause", None)
        for clause in getattr(clauses, "clauses", []):
            left, right = getattr(clause, "left", None), getattr(clause, "right", None)
            if left is not None and right is not None and hasattr(right, "value"):
                found[left.name] = right.value
        claim = (found.get("principal_id"), found.get("idempotency_key"), found.get("path"))
        if None in claim:
            return None
        return self._session.rows.get(claim)
