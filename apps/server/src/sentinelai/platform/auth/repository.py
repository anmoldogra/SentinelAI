"""Auth persistence — the ``platform`` schema's identity/session queries.

Persistence only (no business rules — guide Part 5). Query bodies are left as
``NotImplementedError`` skeletons until the authentication slice is built; see the
open question flagged in the Phase 3 summary about how a bearer token maps to a
``platform.sessions`` row (the documented table carries no token/token-hash column).
"""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.platform.auth.models import Session
from sentinelai.platform.db.session import get_session


class SessionRepository:
    """Reads active sessions and their owning user's roles."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active_by_token(self, token: str) -> Session | None:
        """Resolve a non-expired, non-revoked session from a bearer token."""
        raise NotImplementedError

    async def get_role_names(self, user_id: object) -> list[str]:
        """Return the role names granted to a user (user_roles ⋈ roles)."""
        raise NotImplementedError


async def get_session_repository(
    session: AsyncSession = Depends(get_session),
) -> SessionRepository:
    """FastAPI dependency providing a request-scoped ``SessionRepository``."""
    return SessionRepository(session)
