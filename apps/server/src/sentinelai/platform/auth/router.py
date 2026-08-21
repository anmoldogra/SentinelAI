"""Authentication HTTP routes — api-design.md §4.1, §9.

Routers parse and delegate only (guide Part 5). ``POST /api/v1/auth/login`` is one of the four
endpoints api-design.md §4 lists as unauthenticated, so it takes no ``CurrentUser`` dependency —
that absence is the design, not an omission.

The entrypoint owns the transaction (ADR-0005). Login is unusual in that a *rejected* attempt
still has to persist something: security-architecture.md §5 requires every attempt to be audited,
so the failure path commits the audit entry the service wrote before re-raising. The
``AsyncSession`` injected here is the same instance the service's repositories hold — FastAPI
caches the ``get_session`` sub-dependency per request.

The remaining §4.1 auth endpoints (``/auth/mfa/verify``, ``/auth/refresh``, ``/auth/logout``, the
SSO pair) are not implemented here; MFA and SSO are explicitly out of this increment's scope.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.platform.auth.schemas import LoginRequest, LoginResponse
from sentinelai.platform.auth.service import AuthService, get_auth_service
from sentinelai.platform.db.session import get_session
from sentinelai.shared.envelope import Envelope, Meta
from sentinelai.shared.exceptions import UnauthenticatedError

router = APIRouter(prefix="/api/v1", tags=["auth"])


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
        token, session_row = await service.login(
            payload.email,
            payload.password.get_secret_value(),
            ip_address=client_host,
            user_agent=request.headers.get("user-agent"),
        )
    except UnauthenticatedError:
        # Commit the `login_failed` audit entry, then let the handler turn this into a 401.
        # Without this the rollback would erase the very record §5 requires.
        await session.commit()
        raise
    await session.commit()  # ADR-0005: the entrypoint owns the transaction

    return Envelope(
        data=LoginResponse(access_token=token, expires_at=session_row.expires_at),
        meta=_meta(request),
    )
