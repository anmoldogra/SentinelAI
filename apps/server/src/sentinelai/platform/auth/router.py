"""Authentication HTTP routes — api-design.md §4.1, §9.

Routers parse and delegate only (guide Part 5). ``POST /api/v1/auth/login`` is one of the four
endpoints api-design.md §4 lists as unauthenticated, so it takes no ``CurrentUser`` dependency —
that absence is the design, not an omission.

The entrypoint owns the transaction (ADR-0005). Login is unusual in that a *rejected* attempt
still has to persist something: security-architecture.md §5 requires every attempt to be audited,
so the failure path commits the audit entry the service wrote before re-raising. The
``AsyncSession`` injected here is the same instance the service's repositories hold — FastAPI
caches the ``get_session`` sub-dependency per request.

``/auth/mfa/verify``, ``/auth/refresh`` and ``/auth/logout`` complete §9's session lifecycle
(Wave 3.1). The SSO pair (``/auth/sso/{provider}/redirect|callback``) is still unbuilt — ADR-0010
A1 defers it by sequencing, not by profile, and the schema it needs already exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.platform.auth.schemas import (
    LoginRequest,
    LoginResponse,
    MfaRequiredResponse,
    MfaVerifyRequest,
    RefreshRequest,
)
from sentinelai.platform.auth.service import AuthService, get_auth_service
from sentinelai.platform.config import settings
from sentinelai.platform.db.session import get_session
from sentinelai.platform.db.transaction import TransactionalRoute, bind_session
from sentinelai.shared.envelope import Envelope, Meta
from sentinelai.shared.exceptions import UnauthenticatedError

router = APIRouter(
    prefix="/api/v1",
    tags=["auth"],
    # ADR-0005 §1: the entrypoint owns the transaction. The route class commits once on
    # success and rolls back on any exception; `bind_session` publishes the request-scoped
    # session for it. Declared here rather than per-handler so no handler can omit it.
    route_class=TransactionalRoute,
    dependencies=[Depends(bind_session)],
)


def _meta(request: Request) -> Meta:
    return Meta(request_id=request.state.request_id, correlation_id=request.state.correlation_id)


@router.post(
    "/auth/login",
    response_model=Envelope[LoginResponse],
    status_code=status.HTTP_200_OK,
    summary="Password/credential login",
)
async def login(
    payload: LoginRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
    session: AsyncSession = Depends(get_session),
) -> Envelope[LoginResponse]:
    """Exchange credentials for an opaque bearer token (api-design.md §9)."""
    client_host = request.client.host if request.client is not None else None
    try:
        outcome = await service.login(
            payload.email,
            payload.password.get_secret_value(),
            ip_address=client_host,
            user_agent=request.headers.get("user-agent"),
        )
    except UnauthenticatedError:
        # Commit the `login_failed` audit entry, then let the handler turn this into a 401. Without
        # this, ADR-0005's boundary rollback would erase the very record security §5 requires; with
        # it, that rollback has nothing left to undo.
        await session.commit()
        raise

    if outcome.session is None:
        # No session means the account is MFA-enrolled and `token` is the challenge credential.
        # Still a 200 (api-design.md §9): the password was correct. `security-architecture.md` §8
        # makes the second factor mandatory, so a password alone never reaches case data.
        return Envelope(
            data=MfaRequiredResponse(
                mfa_token=outcome.token,
                expires_at=datetime.now(UTC)
                + timedelta(seconds=settings.mfa_challenge_ttl_seconds),
            ),
            meta=_meta(request),
        )

    return Envelope(
        data=LoginResponse(access_token=outcome.token, expires_at=outcome.session.expires_at),
        meta=_meta(request),
    )


@router.post(
    "/auth/mfa/verify",
    response_model=Envelope[LoginResponse],
    status_code=status.HTTP_200_OK,
    summary="Complete a login with the second factor",
)
async def verify_mfa(
    payload: MfaVerifyRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
    session: AsyncSession = Depends(get_session),
) -> Envelope[LoginResponse]:
    """Exchange an ``mfa_token`` + code for a bearer token (api-design.md §9)."""
    client_host = request.client.host if request.client is not None else None
    try:
        token, session_row = await service.verify_mfa(
            payload.mfa_token,
            payload.code,
            ip_address=client_host,
            user_agent=request.headers.get("user-agent"),
        )
    except UnauthenticatedError:
        # Same composition as login: the rejection's audit entry must survive the boundary
        # rollback that the 401 is about to trigger. A failed second factor is the more
        # interesting of the two signals — it means a correct password was already presented.
        await session.commit()
        raise

    return Envelope(
        data=LoginResponse(access_token=token, expires_at=session_row.expires_at),
        meta=_meta(request),
    )


@router.post(
    "/auth/refresh",
    response_model=Envelope[LoginResponse],
    status_code=status.HTTP_200_OK,
    summary="Rotate a session before it expires",
)
async def refresh(
    payload: RefreshRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> Envelope[LoginResponse]:
    """Issue a successor session and revoke the presented one (api-design.md §9, ADR-0010 §2).

    No explicit commit here, unlike login: a refused refresh writes no audit entry to preserve —
    an unresolvable token names no actor, so there is nothing to attribute the attempt to.
    """
    client_host = request.client.host if request.client is not None else None
    token, session_row = await service.refresh(
        payload.refresh_token,
        ip_address=client_host,
        user_agent=request.headers.get("user-agent"),
    )
    return Envelope(
        data=LoginResponse(access_token=token, expires_at=session_row.expires_at),
        meta=_meta(request),
    )


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the current session",
)
async def logout(
    request: Request,
    authorization: str = Header(...),
    service: AuthService = Depends(get_auth_service),
) -> Response:
    """Revoke the bearer token immediately (api-design.md §9, ADR-0010 §2).

    Takes the token from the ``Authorization`` header rather than depending on
    ``get_current_user``: that dependency rejects an expired or already-revoked session with a
    ``401``, and logout must succeed for exactly those — a caller who is told their logout failed
    will reasonably believe the session is still live.

    ``204`` whether or not anything was revoked, for the same reason: the response must not tell
    a holder of a stale token whether it was ever real.
    """
    if not authorization.startswith("Bearer "):
        raise UnauthenticatedError()
    await service.logout(
        authorization.removeprefix("Bearer "),
        ip_address=request.client.host if request.client is not None else None,
        user_agent=request.headers.get("user-agent"),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
