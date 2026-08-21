"""Unit tests for password login — AuthService's decisions and the /auth/login route.

The security properties here are the point of the tests, not incidental to them: every rejection
must be indistinguishable to the caller, every attempt must be audited (security-architecture.md
§5), and the plaintext bearer token must never reach the database (ADR-0010 §1). The repository's
SQL is exercised against real Postgres in ``tests/integration/test_auth_db.py`` — these tests use
in-memory fakes so they can assert on behaviour the SQL layer would hide.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sentinelai.entrypoints.http.exception_handlers import register_exception_handlers
from sentinelai.entrypoints.http.middleware import register_middleware
from sentinelai.platform.auth.models import Session, User
from sentinelai.platform.auth.router import router as auth_router
from sentinelai.platform.auth.schemas import LoginRequest
from sentinelai.platform.auth.service import AuthService, get_auth_service
from sentinelai.platform.db.session import get_session
from sentinelai.platform.security.tokens import LOOKUP_PREFIX_LENGTH, token_lookup_prefix
from sentinelai.shared.exceptions import UnauthenticatedError

_PASSWORD = "correct-horse-battery-staple"


class FakeHasher:
    """A cheap stand-in for argon2 — same contract, no key-derivation cost."""

    def __init__(self) -> None:
        self.hash_calls = 0
        self.verify_calls = 0

    def hash(self, password: str) -> str:
        self.hash_calls += 1
        return f"new::{password}"

    def verify(self, stored_hash: str, candidate: str) -> bool:
        self.verify_calls += 1
        return stored_hash.split("::", 1)[-1] == candidate

    def needs_rehash(self, stored_hash: str) -> bool:
        return stored_hash.startswith("old::")


class FakeUsers:
    def __init__(self, user: User | None) -> None:
        self._user = user

    async def get_by_email(self, email: str) -> User | None:
        if self._user is None or self._user.email.lower() != email.lower():
            return None
        return self._user


class FakeSessions:
    """Records what would be persisted, so tests can assert on the stored shape."""

    def __init__(self, roles: list[str] | None = None) -> None:
        self.created: list[Session] = []
        self.roles = roles if roles is not None else ["investigator"]

    async def create_session(
        self, *, user_id: UUID, token: str, issued_at: datetime, expires_at: datetime
    ) -> Session:
        row = Session(
            session_id=uuid4(),
            user_id=user_id,
            token_lookup=token_lookup_prefix(token),
            # Mirrors the real repository: the digest is stored, never the token.
            token_hash=f"new::{token}",
            issued_at=issued_at,
            expires_at=expires_at,
            revoked_at=None,
        )
        self.created.append(row)
        return row

    async def get_role_names(self, user_id: UUID) -> list[str]:
        return self.roles


class FakeDbSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture audit writes instead of hitting the hash-chained ledger's real SQL."""
    entries: list[dict[str, Any]] = []

    async def _capture(_session: Any, **kwargs: Any) -> None:
        entries.append(kwargs)

    monkeypatch.setattr("sentinelai.platform.auth.service.record_audit_event", _capture)
    return entries


def make_user(*, password_hash: str | None = f"new::{_PASSWORD}", status: str = "active") -> User:
    now = datetime.now(UTC)
    return User(
        user_id=uuid4(),
        external_idp_subject=None,
        email="Analyst@example.gov",
        display_name="Analyst",
        password_hash=password_hash,
        status=status,
        created_at=now,
        updated_at=now,
    )


def make_service(
    user: User | None,
    *,
    roles: list[str] | None = None,
    ttl_seconds: int = 900,
) -> tuple[AuthService, FakeSessions, FakeHasher]:
    sessions, hasher = FakeSessions(roles), FakeHasher()
    service = AuthService(
        FakeDbSession(),  # type: ignore[arg-type]
        FakeUsers(user),  # type: ignore[arg-type]
        sessions,  # type: ignore[arg-type]
        hasher=hasher,
        ttl_seconds=ttl_seconds,
    )
    return service, sessions, hasher


# --- the issued session ------------------------------------------------------
async def test_login_issues_token_and_never_stores_it(audit: list[dict[str, Any]]) -> None:
    user = make_user()
    service, sessions, _ = make_service(user)

    token, session_row = await service.login(user.email, _PASSWORD)

    assert token  # returned to the caller exactly once
    stored = sessions.created[0]
    assert stored is session_row
    assert stored.token_hash != token, "the plaintext token must never be persisted"
    assert token not in stored.token_lookup or len(stored.token_lookup) == LOOKUP_PREFIX_LENGTH
    assert stored.token_lookup == token[:LOOKUP_PREFIX_LENGTH]
    assert stored.user_id == user.user_id
    assert stored.revoked_at is None


async def test_login_honours_configured_session_ttl(audit: list[dict[str, Any]]) -> None:
    user = make_user()
    service, _, _ = make_service(user, ttl_seconds=3600)

    _, session_row = await service.login(user.email, _PASSWORD)

    assert (session_row.expires_at - session_row.issued_at).total_seconds() == 3600


async def test_email_match_is_case_insensitive(audit: list[dict[str, Any]]) -> None:
    user = make_user()
    service, _, _ = make_service(user)

    token, _ = await service.login(user.email.upper(), _PASSWORD)

    assert token


# --- rejection is uniform ----------------------------------------------------
@pytest.mark.parametrize(
    ("user", "password"),
    [
        pytest.param(None, _PASSWORD, id="unknown-email"),
        pytest.param(make_user(), "wrong-password", id="wrong-password"),
        pytest.param(make_user(password_hash=None), _PASSWORD, id="sso-only-account"),
        pytest.param(make_user(status="disabled"), _PASSWORD, id="disabled-account"),
    ],
)
async def test_every_rejection_looks_identical(
    user: User | None, password: str, audit: list[dict[str, Any]]
) -> None:
    """A caller must not be able to tell these four cases apart — no account enumeration."""
    service, sessions, _ = make_service(user)
    email = user.email if user is not None else "nobody@example.gov"

    with pytest.raises(UnauthenticatedError) as raised:
        await service.login(email, password)

    assert str(raised.value) == "Invalid email or password."
    assert sessions.created == [], "no session may be issued for a rejected login"


async def test_missing_password_hash_still_pays_a_verification(
    audit: list[dict[str, Any]],
) -> None:
    """The dummy verify is what keeps response time from leaking account existence."""
    service, _, hasher = make_service(None)

    with pytest.raises(UnauthenticatedError):
        await service.login("nobody@example.gov", _PASSWORD)

    assert hasher.verify_calls == 1


async def test_disabled_account_is_rejected_after_a_correct_password(
    audit: list[dict[str, Any]],
) -> None:
    user = make_user(status="disabled")
    service, _, _ = make_service(user)

    with pytest.raises(UnauthenticatedError):
        await service.login(user.email, _PASSWORD)

    assert [e["details"]["reason"] for e in audit] == ["account_not_active"]


# --- the audit trail ---------------------------------------------------------
async def test_success_is_audited_with_roles(audit: list[dict[str, Any]]) -> None:
    user = make_user()
    service, _, _ = make_service(user, roles=["supervisor", "investigator"])

    _, session_row = await service.login(user.email, _PASSWORD, ip_address="10.0.0.9")

    (entry,) = audit
    assert entry["action"] == "login_success"
    assert entry["actor_user_id"] == user.user_id
    assert entry["actor_role"] == "supervisor"
    assert entry["details"] == {"roles": ["supervisor", "investigator"]}
    assert entry["target_id"] == session_row.session_id
    assert entry["ip_address"] == "10.0.0.9"


async def test_success_without_roles_records_none(audit: list[dict[str, Any]]) -> None:
    user = make_user()
    service, _, _ = make_service(user, roles=[])

    await service.login(user.email, _PASSWORD)

    assert audit[0]["actor_role"] == "none"


async def test_failure_for_unknown_email_audits_without_an_actor(
    audit: list[dict[str, Any]],
) -> None:
    service, _, _ = make_service(None)

    with pytest.raises(UnauthenticatedError):
        await service.login("nobody@example.gov", _PASSWORD, user_agent="curl/8")

    (entry,) = audit
    assert entry["action"] == "login_failed"
    assert entry["actor_user_id"] is None
    assert entry["actor_role"] == "anonymous"
    assert entry["details"] == {"reason": "invalid_credentials"}
    assert entry["user_agent"] == "curl/8"


async def test_failure_for_known_email_names_the_account(audit: list[dict[str, Any]]) -> None:
    user = make_user()
    service, _, _ = make_service(user)

    with pytest.raises(UnauthenticatedError):
        await service.login(user.email, "wrong-password")

    assert audit[0]["actor_user_id"] == user.user_id


# --- password digest maintenance --------------------------------------------
async def test_weak_digest_is_upgraded_on_successful_login(audit: list[dict[str, Any]]) -> None:
    user = make_user(password_hash=f"old::{_PASSWORD}")
    service, _, _ = make_service(user)

    await service.login(user.email, _PASSWORD)

    assert user.password_hash == f"new::{_PASSWORD}"


async def test_current_digest_is_left_alone(audit: list[dict[str, Any]]) -> None:
    user = make_user()
    service, _, hasher = make_service(user)

    await service.login(user.email, _PASSWORD)

    assert user.password_hash == f"new::{_PASSWORD}"
    assert hasher.hash_calls == 0, "an up-to-date digest must not be rewritten"


# --- the schema --------------------------------------------------------------
def test_password_is_not_exposed_by_repr() -> None:
    payload = LoginRequest(email="a@example.gov", password="hunter2")

    assert "hunter2" not in repr(payload)
    assert payload.password.get_secret_value() == "hunter2"


# --- the route ---------------------------------------------------------------
class StubService:
    """Stands in for AuthService so the route's own behaviour is what is under test."""

    def __init__(self, result: tuple[str, Session] | Exception) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def login(
        self,
        email: str,
        password: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[str, Session]:
        self.calls.append({"email": email, "password": password, "ip_address": ip_address})
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def build_client(service: StubService) -> tuple[TestClient, FakeDbSession]:
    app = FastAPI()
    register_middleware(app)
    register_exception_handlers(app)
    app.include_router(auth_router)
    db = FakeDbSession()
    app.dependency_overrides[get_auth_service] = lambda: service
    app.dependency_overrides[get_session] = lambda: db
    return TestClient(app, raise_server_exceptions=False), db


def make_session_row() -> Session:
    now = datetime.now(UTC)
    return Session(
        session_id=uuid4(),
        user_id=uuid4(),
        token_lookup="abcdefghijkl",
        token_hash="digest",
        issued_at=now,
        expires_at=now,
        revoked_at=None,
    )


def test_route_returns_the_token_in_the_standard_envelope() -> None:
    session_row = make_session_row()
    service = StubService(("plaintext-token", session_row))
    client, db = build_client(service)

    response = client.post(
        "/api/v1/auth/login", json={"email": "a@example.gov", "password": _PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["access_token"] == "plaintext-token"
    assert body["data"]["token_type"] == "bearer"
    assert body["data"]["expires_at"] is not None
    assert body["meta"]["request_id"] and body["meta"]["correlation_id"]
    assert db.commits == 1, "ADR-0005: the entrypoint commits exactly once"
    assert service.calls[0]["password"] == _PASSWORD


def test_route_commits_the_audit_entry_before_returning_401() -> None:
    """A rejected login must still leave its `login_failed` trail (security §5)."""
    client, db = build_client(StubService(UnauthenticatedError("Invalid email or password.")))

    response = client.post(
        "/api/v1/auth/login", json={"email": "a@example.gov", "password": "nope"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert db.commits == 1, "without this commit the audit entry would be rolled back"


def test_route_rejects_a_malformed_body() -> None:
    client, _ = build_client(StubService(("t", make_session_row())))

    response = client.post("/api/v1/auth/login", json={"email": "a@example.gov"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_route_never_echoes_the_password() -> None:
    client, _ = build_client(StubService(("plaintext-token", make_session_row())))

    response = client.post(
        "/api/v1/auth/login", json={"email": "a@example.gov", "password": _PASSWORD}
    )

    assert _PASSWORD not in response.text
