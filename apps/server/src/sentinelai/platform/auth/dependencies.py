"""Authentication & authorization dependencies — guide Part 8, security §5–9.

RBAC (``require_role``) gates the action class; ABAC (``require_case_access``) gates
the specific resource. Both are audited (Part 10) regardless of outcome.

``require_case_access`` needs to know whether a user may see a specific case — a
fact owned by ``case_management``. ``platform`` may not import a module (import
DAG + import-linter ``platform is domain-agnostic``), so this defines a
``CaseAccessChecker`` **port**; ``case_management`` supplies the adapter and the
HTTP composition root registers it via ``app.dependency_overrides`` (Phase 4).
Until then the default provider raises ``NotImplementedError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from fastapi import Depends, Header, Request

from sentinelai.platform.auth.repository import SessionRepository, get_session_repository
from sentinelai.shared.exceptions import ForbiddenError, UnauthenticatedError


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The authenticated principal for one request."""

    user_id: UUID
    roles: tuple[str, ...]


async def get_current_user(
    authorization: str = Header(...),
    session_repo: SessionRepository = Depends(get_session_repository),
) -> CurrentUser:
    """Resolve the current user from a ``Bearer`` token, or raise ``UnauthenticatedError``."""
    if not authorization.startswith("Bearer "):
        raise UnauthenticatedError()
    session = await session_repo.get_active_by_token(authorization.removeprefix("Bearer "))
    if session is None or session.expires_at < datetime.now(UTC) or session.revoked_at is not None:
        raise UnauthenticatedError()
    roles = await session_repo.get_role_names(session.user_id)
    return CurrentUser(user_id=session.user_id, roles=tuple(roles))


def require_role(*allowed: str):
    """Dependency factory: allow only principals holding at least one ``allowed`` role."""

    async def _dep(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not set(current_user.roles) & set(allowed):
            raise ForbiddenError()
        return current_user

    return _dep


class CaseAccessChecker(Protocol):
    """Port: does ``user_id`` have access to ``case_id``? Implemented by case_management."""

    async def user_has_access(self, case_id: UUID, user_id: UUID) -> bool: ...


async def get_case_access_checker() -> CaseAccessChecker:
    """Default provider — overridden by the HTTP composition root once
    ``case_management`` registers its adapter (Phase 4)."""
    raise NotImplementedError(
        "CaseAccessChecker is provided by case_management via app.dependency_overrides"
    )


def require_case_access(param: str = "case_id"):
    """Dependency factory: allow only principals with access to the path's case."""

    async def _dep(
        request: Request,
        current_user: CurrentUser = Depends(get_current_user),
        checker: CaseAccessChecker = Depends(get_case_access_checker),
    ) -> CurrentUser:
        case_id = UUID(request.path_params[param])
        if not await checker.user_has_access(case_id, current_user.user_id):
            raise ForbiddenError()
        return current_user

    return _dep
