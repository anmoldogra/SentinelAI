"""The social-media HTTP surface — api-design.md §4.6.

The contract a monitoring connector programs against: the `Idempotency-Key` behaviour on the two
"Yes (key)" POSTs, §4.6's per-endpoint RBAC, the documented shapes, and the `422`s a malformed
capture earns.

Idempotency is asserted on a **call counter**, not on matching response bodies: "a retried capture
does not record the post twice" means the service was not re-entered, and identical bodies alone
would pass against an implementation that ran twice and discarded the second result. Two rows for
one post would double-count it in every timeline and every content statistic built from this table.

The real service is exercised against Postgres in `test_social_media_db.py`; what this file observes
is what the *router* does.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from sentinelai.entrypoints.http.main import create_app
from sentinelai.modules.social_media.exceptions import ContentNotFoundError
from sentinelai.modules.social_media.models import CapturedContent, SocialAccountObserved
from sentinelai.modules.social_media.schemas import AccountCreate, ContentCreate
from sentinelai.modules.social_media.service import (
    STATUS_CAPTURED,
    STATUS_PUBLISHED,
    get_social_media_service,
    validate_captured_at,
    validate_content_kind,
)
from sentinelai.platform.auth.dependencies import CurrentUser, get_current_user
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.db.session import get_session
from sentinelai.platform.idempotency.guard import HEADER_NAME, REPLAY_HEADER
from tests.fixtures.idempotency import IdempotencySession
from tests.fixtures.kms import kms_for_tests

_KEY = "social-idem-key-000001"
_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
_PLATFORM = "X"
_HANDLE = "@suspect_01"

_ADMIN = CurrentUser(user_id=uuid4(), roles=("admin",))
_INVESTIGATOR = CurrentUser(user_id=uuid4(), roles=("investigator",))
_SYSTEM = CurrentUser(user_id=uuid4(), roles=("system",))

_ACCOUNT_BODY = {"platform": _PLATFORM, "handle": _HANDLE}
_CONTENT_BODY = {
    "platform": _PLATFORM,
    "account_handle": _HANDLE,
    "content_kind": "post",
    "captured_at": _NOW.isoformat(),
    "raw_attributes": {
        "schema_version": "1.0.0",
        "title": "Post by @suspect_01",
        "attributes": {"body": "meet at the usual spot"},
        "confidence": 0.9,
        "legal_authority_ref": "PRODUCTION-ORDER-2026-88",
    },
}


class _StubService:
    """Records calls and returns plausible rows, so re-entry is observable."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.accounts: list[SocialAccountObserved] = []
        self.content: list[CapturedContent] = []

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def _content_row(self, *, published: bool = False) -> CapturedContent:
        return CapturedContent(
            content_id=uuid4(),
            evidence_id=uuid4() if published else None,
            status=STATUS_PUBLISHED if published else STATUS_CAPTURED,
            collected_at=_NOW,
            platform=_PLATFORM,
            account_handle=_HANDLE,
            content_kind="post",
            raw_attributes={},
        )

    async def list_accounts(self, actor: CurrentUser) -> list[SocialAccountObserved]:
        self._count("list_accounts")
        return self.accounts

    async def register_account(
        self, data: AccountCreate, actor: CurrentUser, correlation_id: str
    ) -> SocialAccountObserved:
        self._count("register_account")
        account = SocialAccountObserved(
            account_id=uuid4(),
            platform=data.platform,
            handle=data.handle,
            first_observed_at=_NOW,
            last_observed_at=_NOW,
        )
        self.accounts.append(account)
        return account

    async def list_content(
        self, actor: CurrentUser, page: Any
    ) -> tuple[list[CapturedContent], str | None, bool]:
        self._count("list_content")
        return self.content, None, False

    async def create_content(
        self, data: ContentCreate, actor: CurrentUser, correlation_id: str
    ) -> CapturedContent:
        self._count("create_content")
        # The router hands the parsed body straight through, so validating here is what proves the
        # route did not quietly normalize it.
        validate_content_kind(data.content_kind)
        validate_captured_at(data.captured_at)
        content = self._content_row()
        self.content.append(content)
        return content

    async def get_content(self, content_id: UUID, actor: CurrentUser) -> CapturedContent:
        self._count("get_content")
        if self.content:
            return self.content[0]
        raise ContentNotFoundError()

    async def publish_content(
        self, content_id: UUID, actor: CurrentUser, correlation_id: str
    ) -> CapturedContent:
        self._count("publish_content")
        return self._content_row(published=True)


def _app(service: _StubService, actor: CurrentUser, store: IdempotencySession) -> Any:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_kms] = lambda: kms_for_tests()
    app.dependency_overrides[get_session] = lambda: store
    app.dependency_overrides[get_social_media_service] = lambda: service
    return app


@pytest.fixture
def service() -> _StubService:
    return _StubService()


@pytest.fixture
def store() -> IdempotencySession:
    return IdempotencySession()


async def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- accounts ---------------------------------------------------------------
async def test_registering_an_account_returns_the_documented_shape(
    service: _StubService, store: IdempotencySession
) -> None:
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        response = await client.post("/api/v1/social-media/accounts", json=_ACCOUNT_BODY)

    assert response.status_code == 201
    data = response.json()["data"]
    assert data["platform"] == _PLATFORM
    assert data["handle"] == _HANDLE
    assert data["first_observed_at"] is not None


async def test_a_retried_account_registration_replays(
    service: _StubService, store: IdempotencySession
) -> None:
    """§4.6 marks this "Yes (key)"."""
    headers = {HEADER_NAME: _KEY}
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        first = await client.post(
            "/api/v1/social-media/accounts", json=_ACCOUNT_BODY, headers=headers
        )
        second = await client.post(
            "/api/v1/social-media/accounts", json=_ACCOUNT_BODY, headers=headers
        )

    assert first.status_code == second.status_code == 201
    assert service.calls["register_account"] == 1, "the service was not re-entered"
    assert second.headers.get(REPLAY_HEADER) == "true"
    assert second.json()["data"] == first.json()["data"]


async def test_listing_accounts_is_unpaginated(
    service: _StubService, store: IdempotencySession
) -> None:
    """§4.6 gives `GET /accounts` no pagination — the monitoring list is analyst-curated."""
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        response = await client.get("/api/v1/social-media/accounts")

    assert response.status_code == 200
    assert response.json()["data"] == []


# --- content ----------------------------------------------------------------
async def test_capturing_content_returns_the_documented_shape(
    service: _StubService, store: IdempotencySession
) -> None:
    """§4.6: "Full content record, `evidence_id: null`", `201`."""
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        response = await client.post("/api/v1/social-media/content", json=_CONTENT_BODY)

    assert response.status_code == 201
    data = response.json()["data"]
    assert data["evidence_id"] is None
    assert data["status"] == STATUS_CAPTURED
    assert data["content_kind"] == "post"


async def test_a_retried_capture_records_the_post_once(
    service: _StubService, store: IdempotencySession
) -> None:
    """Two rows for one post would double-count it in every timeline built from this table."""
    headers = {HEADER_NAME: _KEY}
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        first = await client.post(
            "/api/v1/social-media/content", json=_CONTENT_BODY, headers=headers
        )
        second = await client.post(
            "/api/v1/social-media/content", json=_CONTENT_BODY, headers=headers
        )

    assert first.status_code == second.status_code == 201
    assert service.calls["create_content"] == 1
    assert second.headers.get(REPLAY_HEADER) == "true"
    assert len(service.content) == 1


async def test_a_different_key_records_a_second_capture(
    service: _StubService, store: IdempotencySession
) -> None:
    """Two genuine captures are two records — an account can post the same text twice."""
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        await client.post(
            "/api/v1/social-media/content", json=_CONTENT_BODY, headers={HEADER_NAME: f"{_KEY}-a"}
        )
        await client.post(
            "/api/v1/social-media/content", json=_CONTENT_BODY, headers={HEADER_NAME: f"{_KEY}-b"}
        )

    assert service.calls["create_content"] == 2


async def test_a_rejected_capture_leaves_its_key_reusable(
    service: _StubService, store: IdempotencySession
) -> None:
    """The claim shares the request's transaction, so a `422` rolls it back.

    A connector that sent a bad `content_kind` must be able to retry the same request with the same
    key once it is corrected — a key burned by a rejection would force it to invent a new one to
    recover, which is the confusion idempotency keys exist to remove.
    """
    headers = {HEADER_NAME: _KEY}
    bad = {**_CONTENT_BODY, "content_kind": "tweet"}

    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        rejected = await client.post("/api/v1/social-media/content", json=bad, headers=headers)
        retried = await client.post(
            "/api/v1/social-media/content", json=_CONTENT_BODY, headers=headers
        )

    assert rejected.status_code == 422
    assert retried.status_code == 201
    assert retried.headers.get(REPLAY_HEADER) is None


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"content_kind": "tweet"}, "content_kind"),
        ({"captured_at": (_NOW + timedelta(days=400)).isoformat()}, "captured_at"),
    ],
    ids=["platform-vocabulary", "future-capture"],
)
async def test_a_malformed_capture_is_a_422_naming_the_field(
    service: _StubService, store: IdempotencySession, override: dict[str, str], field: str
) -> None:
    """§4.6's two validation rules, surfaced as §2.4's envelope.

    `tweet` is the plausible wrong answer — a platform's own vocabulary rather than CEM §6's.
    """
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        response = await client.post(
            "/api/v1/social-media/content", json={**_CONTENT_BODY, **override}
        )

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "VALIDATION_FAILED"
    assert any(detail["field"] == field for detail in body["details"])


@pytest.mark.parametrize(
    "missing", ["platform", "account_handle", "content_kind", "captured_at", "raw_attributes"]
)
async def test_a_required_field_is_a_400_not_a_422(
    service: _StubService, store: IdempotencySession, missing: str
) -> None:
    """§2.4: `VALIDATION_FAILED` is "400 or 422 — request malformed (400) or fails domain/business
    rules (422)". A body missing a field never reached a domain rule."""
    body = {key: value for key, value in _CONTENT_BODY.items() if key != missing}

    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        response = await client.post("/api/v1/social-media/content", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_listing_content_returns_the_paginated_envelope(
    service: _StubService, store: IdempotencySession
) -> None:
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        response = await client.get("/api/v1/social-media/content")

    assert response.status_code == 200
    assert response.json()["pagination"] == {
        "next_cursor": None,
        "has_more": False,
        "limit": 50,
    }


async def test_publishing_returns_the_capture_carrying_its_evidence_id(
    service: _StubService, store: IdempotencySession
) -> None:
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        response = await client.post(f"/api/v1/social-media/content/{uuid4()}/publish")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["evidence_id"] is not None
    assert data["status"] == STATUS_PUBLISHED


# --- RBAC -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path", "actor", "expected"),
    [
        ("post", "/api/v1/social-media/accounts", _INVESTIGATOR, 201),
        ("post", "/api/v1/social-media/accounts", _ADMIN, 201),
        ("post", "/api/v1/social-media/accounts", _SYSTEM, 403),
        ("post", "/api/v1/social-media/content", _INVESTIGATOR, 201),
        ("post", "/api/v1/social-media/content", _SYSTEM, 201),
        ("post", "/api/v1/social-media/content", _ADMIN, 403),
        ("get", "/api/v1/social-media/accounts", _INVESTIGATOR, 200),
        ("get", "/api/v1/social-media/content", _SYSTEM, 403),
    ],
)
async def test_rbac_matches_the_documented_roles(
    service: _StubService,
    store: IdempotencySession,
    method: str,
    path: str,
    actor: CurrentUser,
    expected: int,
) -> None:
    """§4.6's Auth column, asserted per endpoint — and the two POSTs differ, deliberately.

    Registering an account is `investigator, admin`: deciding *who* to monitor is a supervisory act
    with civil-liberties weight, and §4.6 puts an administrator in that decision. Capturing content
    is `investigator, system`: an automated connector pushes content all day, and it is the one act
    an administrator has no business performing. A connector that could enrol new monitoring targets
    would be surveillance expanding itself without a person deciding.
    """
    body = _ACCOUNT_BODY if path.endswith("accounts") else _CONTENT_BODY
    async with await _client(_app(service, actor, store)) as client:
        response = await (client.post(path, json=body) if method == "post" else client.get(path))

    assert response.status_code == expected
