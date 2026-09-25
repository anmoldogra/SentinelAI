"""The HTTP transaction boundary — ADR-0005, modernization Wave 2.1.

ADR-0005 §1 requires the transaction to be **opened and committed at the entrypoint**, committed
once on success and rolled back on any exception. Before this module, services already never
committed (§2 was satisfied), but the commit itself was hand-written in every handler: some
twenty-four ``await uow.commit()`` calls, one per mutating route.

**That is a silent-data-loss shape, not a style problem.** A handler that forgets the call does not
fail, log, or raise — the session is closed at request end, the transaction is discarded, and the
write simply never happened. The endpoint returns ``201`` with a body describing a row that does not
exist. Nothing in the type system or the test suite notices unless some test happens to assert
persistence. Moving the commit to the boundary makes forgetting it impossible.

**Why a route class rather than the dependency ADR-0005 §1 suggests.** The obvious implementation is
a ``yield`` dependency that commits after the ``yield``. It does not work, and it fails in the worst
way: FastAPI runs dependency teardown *after* the response has been produced, so an exception from
``commit()`` there cannot become a ``500``. It escapes with the response already begun, bypassing
the registered exception handlers and the standard error envelope — a client would see success for a
transaction that never committed. Verified empirically before choosing this design, and recorded as
an amendment to the ADR rather than followed into a bug.

A custom ``APIRoute`` wraps the call *inside* the request/response cycle, so a failing commit raises
where the exception handlers can still see it.

**Why a router-level dependency binds the session, rather than ``get_session`` doing it.** Because
dependency overrides have to keep working. Tests routinely override ``get_session`` with a fake, and
a binding performed inside the real ``get_session`` would simply not happen — the boundary would
then quietly commit nothing, which is the exact failure mode this module exists to remove.
:func:`bind_session` depends on ``get_session``, so an override resolves through it and the fake is
what gets bound, and committed.

**Deliberate pre-commits still work, untouched.** Three endpoints must persist writes *through* an
error response, because the failure is itself an auditable fact: a rejected ingest keeps its intake
record and ``evidence.validation_failed`` event (event-driven §25.2), a failed integrity check keeps
its MISMATCH custody entry (ADR-0008 §3), and a failed login keeps its ``login_failed`` audit row
(security §5). Those handlers commit explicitly and re-raise; the rollback this boundary then
performs is a no-op on an already-committed transaction. The two compose without either knowing
about the other.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends, Request, Response
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.platform.db.session import get_session
from sentinelai.platform.logging import log

# Where the request-scoped session is published for the route wrapper to find.
_STATE_ATTR = "db_session"


async def bind_session(
    request: Request, session: AsyncSession = Depends(get_session)
) -> AsyncSession:
    """Publish the request-scoped session so :class:`TransactionalRoute` can commit it.

    Declared as a router-level dependency rather than called by handlers, so no handler can omit it.
    Returns the session too, so a caller that wants it can depend on this instead of reaching into
    ``request.state``.
    """
    setattr(request.state, _STATE_ATTR, session)
    return session


def bound_session(request: Request) -> AsyncSession | None:
    """The session bound to this request, or ``None`` if the route never touched the database."""
    session: AsyncSession | None = getattr(request.state, _STATE_ATTR, None)
    return session


class TransactionalRoute(APIRoute):
    """Commits once on success, rolls back on any exception — ADR-0005 §1.

    Attach with ``APIRouter(route_class=TransactionalRoute, dependencies=[Depends(bind_session)])``.
    Read-only routes cost nothing: committing a session with no pending work is a no-op, and
    SQLAlchemy has begun no transaction if nothing queried.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def transactional(request: Request) -> Response:
            try:
                response = await original(request)
            except BaseException:
                # Explicit, rather than relying on session close to discard the transaction. A
                # `DomainError` on its way to a 422 must not leave half a write behind, and saying
                # so here means the guarantee does not rest on SQLAlchemy's teardown semantics.
                #
                # `BaseException` on purpose: a cancelled request (`asyncio.CancelledError`, which a
                # client disconnect raises) must discard its transaction too, and that is not an
                # `Exception`.
                await _rollback(request)
                raise

            await _commit(request)
            return response

        return transactional


async def _commit(request: Request) -> None:
    session = bound_session(request)
    if session is None:
        return
    try:
        await session.commit()
    except Exception:
        # The response exists but has NOT been sent, so raising here still reaches the registered
        # exception handlers: the client gets the standard error envelope rather than a success for
        # a transaction that did not commit.
        await _rollback(request)
        log.error("request_transaction_commit_failed", path=request.url.path)
        raise


async def _rollback(request: Request) -> None:
    session = bound_session(request)
    if session is None:
        return
    try:
        await session.rollback()
    except Exception:  # pragma: no cover - a dead connection cannot be rolled back
        # Never mask the original failure with a rollback error. The session is closed at request
        # end regardless, which discards the transaction anyway.
        log.warning("request_transaction_rollback_failed", path=request.url.path)


__all__ = ["TransactionalRoute", "bind_session", "bound_session"]
