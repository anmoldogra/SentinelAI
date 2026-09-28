"""The idempotency decision — ADR-0012 §2, api-design.md §2.9.

Three outcomes for a mutating request carrying an ``Idempotency-Key``:

* **replay** — a completed record with the same fingerprint exists. The stored response is returned
  verbatim and *the handler never runs*, which is the whole point: "no double effects" means the
  business logic is not re-executed, not merely that its writes are deduplicated afterwards.
* **conflict** — same key, different fingerprint. ``409 IDEMPOTENCY_KEY_CONFLICT``.
* **proceed** — no usable record. Claim the key and let the request run.

**Why a dependency plus a pre-commit hook, and not middleware.** ADR-0012 §2(c) requires the
response be persisted *in the same transaction as the business write*. ASGI middleware runs outside
the route's session entirely, so a middleware implementation would have to open its own transaction
and could commit a response record for a business write that then rolled back — or the reverse. The
decision therefore runs as a router-level dependency (same request-scoped session as the handler,
and able to stop the handler by raising), and the response is recorded by a pre-commit hook on
ADR-0005's transaction boundary.

**Why claiming inside the request's transaction is the concurrency control.** Two simultaneous
requests with one key both reach the claim; the ``uq_idempotency_claim`` index makes the second wait
until the first commits or rolls back. If the first committed, the second's insert fails and it
replays the now-stored response. If the first rolled back, the second's insert succeeds and it
proceeds. Neither outcome needs an in-flight state machine, a cleanup job for abandoned claims, or
§2(d)'s alternative of answering ``409`` to a client whose only mistake was retrying.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from fastapi import Depends, Header, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.platform.auth.dependencies import CurrentUser, get_current_user
from sentinelai.platform.config import settings
from sentinelai.platform.db.session import get_session
from sentinelai.platform.db.transaction import register_pre_commit
from sentinelai.platform.idempotency.fingerprint import request_fingerprint
from sentinelai.platform.idempotency.models import STATE_COMPLETED, IdempotencyKey
from sentinelai.platform.idempotency.repository import IdempotencyRepository
from sentinelai.platform.logging import log
from sentinelai.shared.exceptions import IdempotencyKeyConflictError, ValidationFailedError

HEADER_NAME: Final = "Idempotency-Key"

# api-design.md §2.9 scopes keys to resource-creating writes. `DELETE` is excluded deliberately:
# every keyed `DELETE` in §4 is marked naturally idempotent — deleting an absent thing already
# returns the same answer — so storing a response for it would add a write to buy nothing.
IDEMPOTENT_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH"})

# A key is opaque to the server; it only has to be long enough to be unguessable by a peer (keys
# are per-principal, so a collision is a self-inflicted bug, not a cross-tenant leak) and short
# enough not to be a payload. Stripe's guidance and every client library land inside this range.
MIN_KEY_LENGTH: Final = 8
MAX_KEY_LENGTH: Final = 255

# Headers a replay must reproduce, lower-cased. Deliberately an allowlist rather than "everything
# except a denylist": `Date`, `Content-Length` and any correlation/request id describe *this*
# exchange, and serving a stored copy of them would be actively wrong — a client correlating logs
# by request id would be handed someone else's.
REPLAYABLE_HEADERS: Final[frozenset[str]] = frozenset({"etag", "location", "content-type"})

# Marks a response as served from the store, so a client (and an operator reading a trace) can tell
# a replay from a fresh execution. Not in api-design.md §2.9 — added because a replay that is
# indistinguishable from the original makes "did my retry take effect?" unanswerable from the wire.
REPLAY_HEADER: Final = "Idempotent-Replay"


class IdempotentReplay(Exception):
    """Raised to short-circuit a request whose response is already stored.

    Not a ``DomainError``: nothing failed. It exists because a dependency cannot return a response,
    only refuse to let the handler run — and refusing is exactly what a replay requires. The
    registered handler in ``entrypoints/http/exception_handlers`` turns it back into the stored
    response.
    """

    def __init__(self, *, status_code: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self.body = body
        super().__init__(f"idempotent replay of a stored {status_code} response")


def _validate_key(key: str) -> None:
    if not (MIN_KEY_LENGTH <= len(key) <= MAX_KEY_LENGTH):
        raise ValidationFailedError(
            [
                {
                    "field": HEADER_NAME,
                    "message": (
                        f"must be between {MIN_KEY_LENGTH} and {MAX_KEY_LENGTH} characters"
                    ),
                }
            ]
        )


def _stored_headers(row: IdempotencyKey) -> dict[str, str]:
    headers = {str(k): str(v) for k, v in (row.response_headers or {}).items()}
    headers[REPLAY_HEADER] = "true"
    return headers


async def enforce_idempotency(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias=HEADER_NAME),
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Router-level dependency implementing ADR-0012 §2.

    A no-op for a safe method or a request with no key, so attaching it to a router changes nothing
    for the endpoints §2.9 does not cover.

    ``session`` is the same instance the handler's repositories hold — FastAPI caches
    ``get_session`` per request — which is what puts the claim, the business write and the stored
    response in one transaction.
    """
    if idempotency_key is None or request.method.upper() not in IDEMPOTENT_METHODS:
        return

    _validate_key(idempotency_key)

    repo = IdempotencyRepository(session)
    path = request.url.path
    # Reading the body here is safe: Starlette caches it on the request, so the handler's own
    # parsing reuses these bytes rather than trying to read a consumed stream.
    body = await request.body()
    fingerprint = request_fingerprint(
        method=request.method,
        path=path,
        principal_id=str(current_user.user_id),
        body=body,
    )
    now = datetime.now(UTC)

    existing = await repo.get(principal_id=current_user.user_id, key=idempotency_key, path=path)
    if existing is not None:
        await _decide_on_existing(
            existing,
            repo=repo,
            fingerprint=fingerprint,
            now=now,
            key=idempotency_key,
            principal_id=current_user.user_id,
        )

    await _claim_and_arm(
        request,
        repo=repo,
        session=session,
        key=idempotency_key,
        principal_id=current_user.user_id,
        method=request.method.upper(),
        path=path,
        fingerprint=fingerprint,
        now=now,
    )


async def _decide_on_existing(
    existing: IdempotencyKey,
    *,
    repo: IdempotencyRepository,
    fingerprint: str,
    now: datetime,
    key: str,
    principal_id: UUID,
) -> None:
    """Replay, conflict, or fall through to a fresh claim for an expired row."""
    if existing.expires_at <= now:
        # Past the window there is nothing to replay and nothing to conflict with, so the key is
        # reusable. Deleting here rather than waiting for the purge job is what makes that true
        # immediately — the unique constraint would otherwise refuse the new claim.
        await repo.drop(existing)
        return

    if existing.request_fingerprint != fingerprint:
        # api-design.md §2.9: `409 IDEMPOTENCY_KEY_CONFLICT`. Logged at warning because it is
        # nearly always a client bug (a key reused across two different payloads), and one worth
        # noticing before it becomes "the API randomly rejects my requests".
        log.warning(
            "idempotency_key_conflict",
            principal_id=str(principal_id),
            path=existing.path,
            method=existing.method,
        )
        raise IdempotencyKeyConflictError(
            f"the {HEADER_NAME} {key!r} was already used for a different request"
        )

    if existing.state != STATE_COMPLETED or existing.response_status is None:
        # Unreachable through the HTTP path: a `claimed` row is only ever visible inside the
        # transaction that created it, and a concurrent duplicate blocks on the index rather than
        # reading it. Treated as a conflict rather than asserted, because the alternative on a row
        # written by some future non-HTTP caller would be to replay a response that does not exist.
        raise IdempotencyKeyConflictError(
            f"the {HEADER_NAME} {key!r} is in use by a request that has not completed"
        )

    await repo.note_replay(existing)
    log.info("idempotent_replay", path=existing.path, status=existing.response_status)
    raise IdempotentReplay(
        status_code=existing.response_status,
        headers=_stored_headers(existing),
        body=existing.response_body or b"",
    )


async def _claim_and_arm(
    request: Request,
    *,
    repo: IdempotencyRepository,
    session: AsyncSession,
    key: str,
    principal_id: UUID,
    method: str,
    path: str,
    fingerprint: str,
    now: datetime,
) -> None:
    """Claim the key, then arm the pre-commit hook that will record the response."""
    try:
        # SAVEPOINT: a unique violation poisons the transaction it occurs in, and the outer
        # transaction still has work to do — namely reading and replaying the row that beat us.
        async with session.begin_nested():
            row = await repo.claim(
                principal_id=principal_id,
                key=key,
                method=method,
                path=path,
                fingerprint=fingerprint,
                now=now,
                ttl_seconds=settings.idempotency_ttl_seconds,
            )
    except IntegrityError:
        # A concurrent request holding the same key committed while we waited on its index entry.
        # Its response is now stored, which is exactly what this client wants.
        winner = await repo.get(principal_id=principal_id, key=key, path=path)
        if winner is None:  # pragma: no cover - the row is committed by construction
            raise IdempotencyKeyConflictError(
                f"the {HEADER_NAME} {key!r} is in use by a concurrent request"
            ) from None
        await _decide_on_existing(
            winner,
            repo=repo,
            fingerprint=fingerprint,
            now=now,
            key=key,
            principal_id=principal_id,
        )
        return

    async def _record(_: Request, response: Response) -> None:
        await _persist_response(repo, row, response)

    register_pre_commit(request, _record)


async def _persist_response(
    repo: IdempotencyRepository, row: IdempotencyKey, response: Response
) -> None:
    """Store the response on the claimed row, or drop the claim if there is nothing worth storing.

    A failure response is **not** cached. Most arrive as an exception, which rolls the transaction
    back and takes the claim with it; a handler that *returns* a 4xx/5xx instead reaches here, and
    keeping its claim would cache nothing while blocking every retry of that key until the TTL
    expired — turning a transient failure into a permanent one.

    A streaming response has no materialized ``body``. Rather than consume its iterator (which
    would break the response being sent), the claim is dropped: this endpoint is simply not
    idempotency-cacheable, and saying so by not caching is better than storing an empty body and
    replaying it as if it were the answer.
    """
    body = getattr(response, "body", None)
    if response.status_code >= 400 or not isinstance(body, bytes):
        await repo.drop(row)
        return

    headers = {
        name.lower(): value
        for name, value in response.headers.items()
        if name.lower() in REPLAYABLE_HEADERS
    }
    await repo.complete(row, status_code=response.status_code, headers=headers, body=body)


__all__ = [
    "HEADER_NAME",
    "IDEMPOTENT_METHODS",
    "REPLAYABLE_HEADERS",
    "REPLAY_HEADER",
    "IdempotentReplay",
    "enforce_idempotency",
]
