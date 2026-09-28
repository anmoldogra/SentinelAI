"""Authentication & authorization dependencies — guide Part 8, security §5-9.

RBAC (``require_role``) gates the action class; ABAC (``require_case_access``) gates
the specific resource.

**ABAC denials are audited** (ADR-0017 §4). ``security-architecture.md`` §6 requires it
by name -- "the denial itself is written to ``platform.audit_log`` with the caller's
identity, the resource requested, and the reason" -- because a compliance review has to
distinguish "this analyst never had access" from "this analyst had access and used it".
RBAC denials are **not** audited: no document requires it, and giving ``require_role``
a KMS dependency would put a signing identity in the path of every role-gated route to
record a fact the request log already carries. This docstring previously claimed both
were audited and neither was.

``require_case_access`` needs to know whether a user may see a specific case — a
fact owned by ``case_management``. ``platform`` may not import a module (import
DAG + import-linter ``platform is domain-agnostic``), so this defines a
``CaseAccessChecker`` **port**; ``case_management`` supplies the adapter and the
HTTP composition root registers it via ``app.dependency_overrides``. The default
provider raises, so a composition root that forgets to register one fails closed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.platform.auth.audit import record_audit_event
from sentinelai.platform.auth.repository import SessionRepository, get_session_repository
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.db.session import get_session
from sentinelai.shared.exceptions import ForbiddenError, UnauthenticatedError

_MODULE = "platform"


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


def require_role(*allowed: str) -> Callable[..., Awaitable[CurrentUser]]:
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
    """Default provider — overridden by the HTTP composition root, which registers
    ``case_management``'s adapter (``entrypoints/http/main.py``).

    Raising is the point: a route that reaches this has no access model at all, and the one
    thing it must not do is let the request through. Fail closed
    (``security-architecture.md`` §1, §6 checklist).
    """
    raise NotImplementedError(
        "CaseAccessChecker is provided by case_management via app.dependency_overrides"
    )


def require_case_access(param: str = "case_id") -> Callable[..., Awaitable[CurrentUser]]:
    """Dependency factory: allow only principals with access to the path's case.

    Access is owner-or-member (ADR-0017 §2), resolved by the ``case_management`` adapter behind
    the port. A denial is audited before it is raised — see :func:`_audit_case_access_denied` for
    why that write has to commit itself.
    """

    async def _dep(
        request: Request,
        current_user: CurrentUser = Depends(get_current_user),
        checker: CaseAccessChecker = Depends(get_case_access_checker),
        session: AsyncSession = Depends(get_session),
        kms: KeyManagementService = Depends(get_kms),
    ) -> CurrentUser:
        case_id = UUID(request.path_params[param])
        if not await checker.user_has_access(case_id, current_user.user_id):
            await _audit_case_access_denied(
                session,
                kms=kms,
                request=request,
                current_user=current_user,
                case_id=case_id,
            )
            raise ForbiddenError()
        return current_user

    return _dep


async def _audit_case_access_denied(
    session: AsyncSession,
    *,
    kms: KeyManagementService,
    request: Request,
    current_user: CurrentUser,
    case_id: UUID,
) -> None:
    """Record an ABAC denial, and commit it — ``security-architecture.md`` §6, ADR-0017 §4.

    **The commit is not optional here.** ADR-0005's ``TransactionalRoute`` rolls back on any
    exception, and this function exists only on a path that is about to raise ``ForbiddenError``,
    so without an explicit commit the boundary would erase the very record §6 requires. Committing
    first makes that rollback a no-op — the same composition the ``login_failed`` audit entry uses
    (``platform/db/transaction.py``, ``auth/router.py``).

    ``target_type`` is ``case`` rather than ``session``: the resource §6 wants named is the case
    the caller was refused, not the credential they held.
    """
    await record_audit_event(
        session,
        kms=kms,
        actor_user_id=current_user.user_id,
        actor_role=current_user.roles[0] if current_user.roles else "none",
        action="case_access_denied",
        module=_MODULE,
        target_type="case",
        target_id=case_id,
        ip_address=request.client.host if request.client is not None else None,
        user_agent=request.headers.get("user-agent"),
        # The caller's full role set, so a reviewer can tell "wrong role" from "right role, wrong
        # case" without joining against a user table whose grants may since have changed.
        details={"reason": "no_case_scope", "roles": list(current_user.roles)},
    )
    await session.commit()
