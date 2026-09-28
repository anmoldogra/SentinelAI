"""API-level tests for the case-membership endpoints — ADR-0017 §3, api-design.md §4.2.

Deliberately does **not** override ``get_case_access_checker`` with an allow-all, the way
``test_case_api.py`` does: these tests exist to prove the ABAC gate actually refuses, so the
checker is backed by the same fake member store the service writes through. An allow-all here
would make every assertion below vacuous.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient

from sentinelai.entrypoints.http.main import create_app
from sentinelai.modules.case_management.service import CaseService, get_case_service
from sentinelai.platform.auth.dependencies import (
    CurrentUser,
    get_case_access_checker,
    get_current_user,
)
from sentinelai.platform.crypto import get_kms
from tests.fixtures.fake_object_storage import FakeObjectStorage
from tests.fixtures.kms import kms_for_tests


class _UowBackedChecker:
    """The ABAC port, answered by the same store the service mutates.

    This is what makes a grant observable through the gate: the router's check and the service's
    writes have to agree, and a checker with its own state would hide exactly the bug where they
    do not.
    """

    def __init__(self, uow: object) -> None:
        self._uow = uow

    async def user_has_access(self, case_id: UUID, user_id: UUID) -> bool:
        return await self._uow.members.user_has_access(case_id, user_id)  # type: ignore[attr-defined]


def _app(uow: object, actor: CurrentUser) -> object:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_kms] = lambda: kms_for_tests()
    app.dependency_overrides[get_case_access_checker] = lambda: _UowBackedChecker(uow)
    app.dependency_overrides[get_case_service] = lambda: CaseService(
        uow, storage=FakeObjectStorage(), kms=kms_for_tests()
    )
    return app


async def _client(app: object) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _create_case(uow: object, owner: CurrentUser) -> str:
    async with await _client(_app(uow, owner)) as client:
        created = await client.post("/api/v1/cases", json={"title": "Operation Nightfall"})
        assert created.status_code == 201
        return str(created.json()["data"]["case_id"])


async def test_a_non_member_is_refused_the_case(uow) -> None:
    """security-architecture.md §6's worked example at the HTTP boundary.

    The stranger holds ``investigator`` — RBAC passes. Only the case-scope attribute stops them.
    """
    owner = CurrentUser(user_id=uuid4(), roles=("investigator",))
    stranger = CurrentUser(user_id=uuid4(), roles=("investigator",))
    case_id = await _create_case(uow, owner)

    async with await _client(_app(uow, stranger)) as client:
        assert (await client.get(f"/api/v1/cases/{case_id}")).status_code == 403
        assert (await client.get(f"/api/v1/cases/{case_id}/members")).status_code == 403
        assert (await client.get(f"/api/v1/cases/{case_id}/evidence")).status_code == 403, (
            "every case-scoped route must refuse, not just the case itself"
        )


async def test_a_granted_member_reaches_the_case_and_a_revoked_one_stops(uow) -> None:
    owner = CurrentUser(user_id=uuid4(), roles=("investigator",))
    analyst = CurrentUser(user_id=uuid4(), roles=("investigator",))
    case_id = await _create_case(uow, owner)

    async with await _client(_app(uow, analyst)) as client:
        assert (await client.get(f"/api/v1/cases/{case_id}")).status_code == 403

    async with await _client(_app(uow, owner)) as client:
        granted = await client.put(
            f"/api/v1/cases/{case_id}/members/{analyst.user_id}", json={"role": "analyst"}
        )
        assert granted.status_code == 200
        assert granted.json()["data"]["role"] == "analyst"
        assert granted.json()["data"]["granted_by_user_id"] == str(owner.user_id)

    async with await _client(_app(uow, analyst)) as client:
        assert (await client.get(f"/api/v1/cases/{case_id}")).status_code == 200

    async with await _client(_app(uow, owner)) as client:
        revoked = await client.delete(f"/api/v1/cases/{case_id}/members/{analyst.user_id}")
        assert revoked.status_code == 204

    async with await _client(_app(uow, analyst)) as client:
        assert (await client.get(f"/api/v1/cases/{case_id}")).status_code == 403, (
            "revocation must take effect immediately"
        )


async def test_a_stranger_cannot_grant_themselves_access(uow) -> None:
    """The escalation path this endpoint group would otherwise be.

    Granting is itself case-scoped, so the gate that refuses the read refuses the grant.
    """
    owner = CurrentUser(user_id=uuid4(), roles=("investigator",))
    stranger = CurrentUser(user_id=uuid4(), roles=("investigator",))
    case_id = await _create_case(uow, owner)

    async with await _client(_app(uow, stranger)) as client:
        attempt = await client.put(
            f"/api/v1/cases/{case_id}/members/{stranger.user_id}", json={"role": "lead"}
        )
        assert attempt.status_code == 403

    async with await _client(_app(uow, stranger)) as client:
        assert (await client.get(f"/api/v1/cases/{case_id}")).status_code == 403


async def test_a_member_may_extend_the_team(uow) -> None:
    """Membership confers the grant right, not just read access — that is what makes a case team
    self-sustaining without an admin in the loop for every addition."""
    owner = CurrentUser(user_id=uuid4(), roles=("investigator",))
    lead = CurrentUser(user_id=uuid4(), roles=("investigator",))
    third = CurrentUser(user_id=uuid4(), roles=("investigator",))
    case_id = await _create_case(uow, owner)

    async with await _client(_app(uow, owner)) as client:
        assert (
            await client.put(
                f"/api/v1/cases/{case_id}/members/{lead.user_id}", json={"role": "lead"}
            )
        ).status_code == 200

    async with await _client(_app(uow, lead)) as client:
        assert (
            await client.put(
                f"/api/v1/cases/{case_id}/members/{third.user_id}", json={"role": "observer"}
            )
        ).status_code == 200

    async with await _client(_app(uow, third)) as client:
        assert (await client.get(f"/api/v1/cases/{case_id}")).status_code == 200


async def test_re_granting_updates_the_role_in_place(uow) -> None:
    """``PUT`` is naturally idempotent (api-design.md §4.2): the membership is named by the URL,
    so a repeat call is an update, never a duplicate or a conflict."""
    owner = CurrentUser(user_id=uuid4(), roles=("investigator",))
    analyst = CurrentUser(user_id=uuid4(), roles=("investigator",))
    case_id = await _create_case(uow, owner)

    async with await _client(_app(uow, owner)) as client:
        first = await client.put(
            f"/api/v1/cases/{case_id}/members/{analyst.user_id}", json={"role": "observer"}
        )
        second = await client.put(
            f"/api/v1/cases/{case_id}/members/{analyst.user_id}", json={"role": "lead"}
        )
        listing = await client.get(f"/api/v1/cases/{case_id}/members")

    assert first.status_code == second.status_code == 200
    assert second.json()["data"]["role"] == "lead"
    assert len(listing.json()["data"]) == 1, "a re-grant must not create a second membership"


async def test_revoking_a_non_member_is_a_no_op(uow) -> None:
    owner = CurrentUser(user_id=uuid4(), roles=("investigator",))
    case_id = await _create_case(uow, owner)

    async with await _client(_app(uow, owner)) as client:
        response = await client.delete(f"/api/v1/cases/{case_id}/members/{uuid4()}")
    assert response.status_code == 204


async def test_the_owners_access_cannot_be_revoked(uow) -> None:
    """A case whose owner cannot open it would be unreachable by anyone."""
    owner = CurrentUser(user_id=uuid4(), roles=("investigator",))
    case_id = await _create_case(uow, owner)

    async with await _client(_app(uow, owner)) as client:
        response = await client.delete(f"/api/v1/cases/{case_id}/members/{owner.user_id}")
    assert response.status_code == 422

    async with await _client(_app(uow, owner)) as client:
        assert (await client.get(f"/api/v1/cases/{case_id}")).status_code == 200


async def test_the_owner_cannot_be_added_as_a_member(uow) -> None:
    """Two records of the same access is the duplicate ADR-0017 §2 exists to avoid — and
    revoking the membership later would produce a row that lies about who can reach the case."""
    owner = CurrentUser(user_id=uuid4(), roles=("investigator",))
    case_id = await _create_case(uow, owner)

    async with await _client(_app(uow, owner)) as client:
        response = await client.put(
            f"/api/v1/cases/{case_id}/members/{owner.user_id}", json={"role": "lead"}
        )
    assert response.status_code == 422


async def test_an_unknown_membership_role_is_refused(uow) -> None:
    owner = CurrentUser(user_id=uuid4(), roles=("investigator",))
    case_id = await _create_case(uow, owner)

    async with await _client(_app(uow, owner)) as client:
        response = await client.put(
            f"/api/v1/cases/{case_id}/members/{uuid4()}", json={"role": "superuser"}
        )
    assert response.status_code == 422


async def test_the_member_list_excludes_the_owner(uow) -> None:
    """The owner is not a membership row by design; the case resource carries them."""
    owner = CurrentUser(user_id=uuid4(), roles=("investigator",))
    analyst = CurrentUser(user_id=uuid4(), roles=("investigator",))
    case_id = await _create_case(uow, owner)

    async with await _client(_app(uow, owner)) as client:
        await client.put(
            f"/api/v1/cases/{case_id}/members/{analyst.user_id}", json={"role": "analyst"}
        )
        listing = await client.get(f"/api/v1/cases/{case_id}/members")

    members = listing.json()["data"]
    assert [m["user_id"] for m in members] == [str(analyst.user_id)]
