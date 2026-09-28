"""ADR-0010 A3's cookie transport, over the real auth router.

A3's security argument is a property of the *whole set* of cookie attributes plus one invariant
about the rest of the API, so these tests check the set and the invariant, not the happy path:

* the refresh token is in the cookie and **not** in any response body — if it were in both, the
  ``HttpOnly`` flag would be decoration;
* the cookie is ``HttpOnly; Secure; SameSite=Strict`` and ``Path``-scoped to the refresh endpoint;
* refresh works **without** a request body and rotates both credentials;
* logout clears it;
* **no endpoint accepts the cookie as authentication** — the precondition A3 names as load-bearing
  for its CSRF argument.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from sentinelai.entrypoints.http.exception_handlers import register_exception_handlers
from sentinelai.entrypoints.http.middleware import register_middleware
from sentinelai.platform.auth.cookies import COOKIE_NAME, COOKIE_PATH
from sentinelai.platform.auth.models import Session
from sentinelai.platform.auth.router import router as auth_router
from sentinelai.platform.auth.service import (
    IssuedSession,
    LoginOutcome,
    get_auth_service,
)
from sentinelai.platform.config import settings
from sentinelai.platform.db.session import get_session

_PASSWORD = "correct-horse-battery-staple"
_ACCESS = "access-token-plaintext"
_REFRESH = "refresh-token-plaintext"
_ROTATED_ACCESS = "rotated-access-plaintext"
_ROTATED_REFRESH = "rotated-refresh-plaintext"


def _session_row() -> Session:
    now = datetime.now(UTC)
    return Session(
        session_id=uuid4(),
        user_id=uuid4(),
        token_lookup="abcdefghijkl",
        token_hash="digest",
        issued_at=now,
        expires_at=now + timedelta(hours=8),
        revoked_at=None,
        refresh_token_lookup="mnopqrstuvwx",
        refresh_token_hash="refresh-digest",
        refresh_expires_at=now + timedelta(days=30),
    )


class _FakeDbSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


class _StubService:
    """Records what the router asked for and hands back fixed credentials."""

    def __init__(self) -> None:
        self.refresh_calls: list[str] = []
        self.logout_calls: list[str] = []

    async def login(self, email: str, password: str, **_: Any) -> LoginOutcome:
        return LoginOutcome(
            issued=IssuedSession(
                access_token=_ACCESS, refresh_token=_REFRESH, session=_session_row()
            )
        )

    async def verify_mfa(self, mfa_token: str, code: str, **_: Any) -> IssuedSession:
        return IssuedSession(access_token=_ACCESS, refresh_token=_REFRESH, session=_session_row())

    async def refresh(self, refresh_token: str, **_: Any) -> IssuedSession:
        self.refresh_calls.append(refresh_token)
        return IssuedSession(
            access_token=_ROTATED_ACCESS,
            refresh_token=_ROTATED_REFRESH,
            session=_session_row(),
        )

    async def logout(self, token: str, **_: Any) -> None:
        self.logout_calls.append(token)


@pytest.fixture
def service() -> _StubService:
    return _StubService()


@pytest.fixture
def app(service: _StubService) -> FastAPI:
    application = FastAPI()
    register_middleware(application)
    register_exception_handlers(application)
    application.include_router(auth_router)
    application.dependency_overrides[get_auth_service] = lambda: service
    application.dependency_overrides[get_session] = _FakeDbSession
    return application


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _set_cookie_header(response: Any) -> str:
    """The raw ``Set-Cookie`` line, because the attributes are the thing under test.

    httpx's cookie jar exposes the *value* but flattens the attributes, and A3 is a statement about
    the attributes — so this reads the header as the browser would.
    """
    headers = [v for k, v in response.headers.multi_items() if k.lower() == "set-cookie"]
    matching = [h for h in headers if h.startswith(f"{COOKIE_NAME}=")]
    assert matching, f"no {COOKIE_NAME} cookie in {headers}"
    return matching[0]


# --- issuing -----------------------------------------------------------------
async def test_login_sets_the_refresh_cookie(app: FastAPI) -> None:
    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/auth/login", json={"email": "a@example.gov", "password": _PASSWORD}
        )

    assert response.status_code == 200
    assert f"{COOKIE_NAME}={_REFRESH}" in _set_cookie_header(response)


async def test_the_refresh_token_is_never_in_the_body(app: FastAPI) -> None:
    """If it were in both places, ``HttpOnly`` would be decoration: script could read the body.

    Asserts against the raw text, not just the parsed fields, so a stray extra key cannot slip the
    plaintext through under a different name.
    """
    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/auth/login", json={"email": "a@example.gov", "password": _PASSWORD}
        )

    body = response.json()
    assert body["data"]["access_token"] == _ACCESS
    assert "refresh_token" not in body["data"]
    assert _REFRESH not in response.text


async def test_the_cookie_carries_every_attribute_a3_requires(app: FastAPI) -> None:
    """A3's table, read back off the wire. Each attribute removes a distinct attack:

    ``HttpOnly`` stops an XSS from reading it, ``SameSite=Strict`` stops a cross-site request from
    sending it, and ``Path`` means the browser will not attach it to any other endpoint even if some
    future handler tried to read one.
    """
    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/auth/login", json={"email": "a@example.gov", "password": _PASSWORD}
        )

    header = _set_cookie_header(response)
    assert "HttpOnly" in header
    assert "SameSite=strict" in header.replace("SameSite=Strict", "SameSite=strict")
    assert f"Path={COOKIE_PATH}" in header
    assert f"Max-Age={settings.refresh_token_ttl_seconds}" in header


async def test_secure_is_set_outside_development(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A3: "`Secure` is set in every profile except `development`."

    Keyed on the profile name rather than `is_production`, so `testing` is covered too — a test
    profile is not a reason to hand out a cookie that may travel in clear.
    """
    monkeypatch.setattr(settings, "app_env", "production")
    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/auth/login", json={"email": "a@example.gov", "password": _PASSWORD}
        )
    assert "Secure" in _set_cookie_header(response)


async def test_development_does_not_require_secure(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one documented exception. A LAN-served dev stack has no TLS, and a `Secure` cookie there
    would simply never be stored — breaking local work rather than protecting anything."""
    monkeypatch.setattr(settings, "app_env", "development")
    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/auth/login", json={"email": "a@example.gov", "password": _PASSWORD}
        )
    assert "Secure" not in _set_cookie_header(response)


async def test_mfa_completion_also_sets_the_cookie(app: FastAPI) -> None:
    """The second-factor path issues a session too, so it must issue both credentials — a client
    that completed MFA and got no cookie would be unable to refresh."""
    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/auth/mfa/verify",
            json={"mfa_token": "challenge-token-value", "code": "123456"},
        )

    assert response.status_code == 200
    assert f"{COOKIE_NAME}={_REFRESH}" in _set_cookie_header(response)
    assert _REFRESH not in response.text


# --- refreshing --------------------------------------------------------------
async def test_refresh_reads_the_cookie_and_needs_no_body(
    app: FastAPI, service: _StubService
) -> None:
    """No body at all: the credential is the cookie the browser attaches on its own. A body field
    would mean the client had to hold the refresh token in JavaScript to send it."""
    async with await _client(app) as client:
        client.cookies.set(COOKIE_NAME, _REFRESH, path=COOKIE_PATH)
        response = await client.post("/api/v1/auth/refresh")

    assert response.status_code == 200
    assert service.refresh_calls == [_REFRESH], "the cookie's value must be what is rotated"
    assert response.json()["data"]["access_token"] == _ROTATED_ACCESS


async def test_refresh_rotates_the_cookie_too(app: FastAPI) -> None:
    """A3: rotation is what makes a stolen cookie single-use. A response that returned a new access
    token while leaving the old cookie in place would leave the long-lived credential immortal."""
    async with await _client(app) as client:
        client.cookies.set(COOKIE_NAME, _REFRESH, path=COOKIE_PATH)
        response = await client.post("/api/v1/auth/refresh")

    header = _set_cookie_header(response)
    assert f"{COOKIE_NAME}={_ROTATED_REFRESH}" in header
    assert _ROTATED_REFRESH not in response.text, "the successor stays out of the body as well"


async def test_refresh_without_a_cookie_is_a_401(app: FastAPI, service: _StubService) -> None:
    async with await _client(app) as client:
        response = await client.post("/api/v1/auth/refresh")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert service.refresh_calls == [], "nothing should be looked up when nothing was presented"


# --- logout ------------------------------------------------------------------
async def test_logout_clears_the_cookie(app: FastAPI, service: _StubService) -> None:
    """Revocation alone would be enough for security, but leaving a dead cookie in the jar means
    the browser keeps presenting it and every resulting 401 is indistinguishable, to the client,
    from a session that expired on its own."""
    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/auth/logout", headers={"Authorization": f"Bearer {_ACCESS}"}
        )

    assert response.status_code == 204
    assert service.logout_calls == [_ACCESS], "logout revokes server-side as well as clearing"
    header = _set_cookie_header(response)
    assert f"Path={COOKIE_PATH}" in header, (
        "a browser identifies a cookie by (name, domain, path) — clearing on a different path "
        "leaves the original in place"
    )
    cleared = (
        'sentinelai_refresh=""' in header or "Max-Age=0" in header or "expires=" in header.lower()
    )
    assert cleared, f"the cookie must be expired, not merely re-sent: {header}"


# --- the load-bearing invariant ---------------------------------------------
async def test_the_cookie_is_not_accepted_as_authentication(app: FastAPI) -> None:
    """A3 names this precondition explicitly: its CSRF argument "holds only for as long as no
    endpoint accepts the cookie as authentication".

    ``/auth/logout`` is the sharpest probe available on this router — it is the one authenticated
    endpoint here, and it takes its credential from the ``Authorization`` header. Presenting the
    cookie and no header must fail, or the cookie has become an API credential and a CSRF token
    becomes mandatory in the same change that made it one.
    """
    async with await _client(app) as client:
        client.cookies.set(COOKIE_NAME, _REFRESH, path="/")
        response = await client.post("/api/v1/auth/logout")

    assert response.status_code in (400, 401), (
        "the refresh cookie must never stand in for a bearer token"
    )
