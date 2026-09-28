"""The refresh-token cookie — ADR-0010 A3.

One module owns every attribute of this cookie, because A3's security argument is a property of the
*set* of them and not of any one. Setting it from three handlers with three literal attribute lists
would be three chances for one of them to drift, and the one that drifts is the one that matters.

**A3's CSRF argument, and the invariant it rests on.** The cookie authorizes *only* token refresh,
never an API call. Every protected endpoint requires the ``Authorization`` header, which a
cross-site request cannot set, so the worst a forged cross-site POST to the refresh endpoint is a
rotation whose response the attacker cannot read — and ``SameSite=Strict`` blocks even that. This
holds **only for as long as no endpoint accepts the cookie as authentication**. If one ever does, a
CSRF token becomes mandatory in the same change. ``get_current_user`` reads the header and nothing
else, and `test_the_cookie_is_not_accepted_as_authentication` is what keeps that true.
"""

from __future__ import annotations

from typing import Final

from fastapi import Response

from sentinelai.platform.config import settings

# Named for the product rather than something generic like `refresh_token`: a cookie jar may hold
# several apps' cookies on a shared host, and a collision would log one of them out.
COOKIE_NAME: Final = "sentinelai_refresh"

# A3: "`Path` scoped to the refresh endpoint". This is the attribute that makes the cookie
# unavailable to every other route — the browser will not attach it to `/api/v1/evidence`, so even a
# future handler that *tried* to read it would find nothing on any path but this one. Defence that
# does not depend on server code staying correct.
COOKIE_PATH: Final = "/api/v1/auth/refresh"

# A3: "`Secure` is set in every profile except `development`." Keyed on the profile name rather
# than on `is_production`, because `testing` must also refuse to hand out a cookie that could
# travel in clear — `is_production` excludes it, and a test profile is not a reason to weaken a
# transport rule.
_INSECURE_PROFILE: Final = "development"


def _secure() -> bool:
    return settings.app_env != _INSECURE_PROFILE


def set_refresh_cookie(response: Response, refresh_token: str) -> None:
    """Attach the refresh credential as an ``HttpOnly; Secure; SameSite=Strict`` cookie.

    ``max_age`` matches the token's own server-side expiry, so the browser stops sending a cookie at
    about the moment the server would stop honouring it. The two are independent — the server's
    ``refresh_expires_at`` is authoritative and a client that keeps sending an expired cookie is
    refused — but a browser that discards it on time saves a pointless round trip.
    """
    response.set_cookie(
        COOKIE_NAME,
        refresh_token,
        max_age=settings.refresh_token_ttl_seconds,
        path=COOKIE_PATH,
        httponly=True,
        secure=_secure(),
        samesite="strict",
    )


def clear_refresh_cookie(response: Response) -> None:
    """Expire the cookie in the client's jar (logout).

    The attributes have to match the ones it was set with — a browser treats
    ``(name, domain, path)`` as the identity of a cookie, so clearing with a different ``path``
    leaves the original in place and the client keeps presenting a credential the server has already
    revoked.

    Note that ``/auth/logout`` never *receives* this cookie: its ``Path`` scopes it to the refresh
    endpoint alone. Clearing it works regardless, because ``Set-Cookie`` is applied by the browser
    on the response and does not require the cookie to have been sent on the request.
    """
    response.delete_cookie(
        COOKIE_NAME,
        path=COOKIE_PATH,
        httponly=True,
        secure=_secure(),
        samesite="strict",
    )


__all__ = ["COOKIE_NAME", "COOKIE_PATH", "clear_refresh_cookie", "set_refresh_cookie"]
