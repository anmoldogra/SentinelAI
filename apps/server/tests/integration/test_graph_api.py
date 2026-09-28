"""`GET /cases/{case_id}/graph` over the real router — api-design.md §6, ADR-0013.

The endpoint that raised ``NotImplementedError`` for eight phases. These tests cover the contract
§6 actually specifies — the envelope shape, the self-containment guarantee, the `depth` cap — and
the authorization requirement inherited from Wave 3.1: a case graph is case-scoped data, so the
same owner-or-member ABAC check that guards every other case route has to guard this one.

The projection is populated directly here rather than by running projectors: that path is proven
against Postgres in `test_graph_projection_db.py`, and what this file is for is the HTTP contract on
top of it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from sentinelai.entrypoints.http.main import create_app
from sentinelai.modules.investigation.models import STATUS_CONFIRMED, STATUS_PROPOSED
from sentinelai.modules.investigation.read.models import CaseGraphEdge, CaseGraphNode
from sentinelai.modules.investigation.service import (
    InvestigationService,
    get_investigation_service,
)
from sentinelai.platform.auth.dependencies import (
    CurrentUser,
    get_case_access_checker,
    get_current_user,
)
from sentinelai.platform.crypto import get_kms
from tests.fixtures.kms import kms_for_tests

_CASE = uuid4()
_ALICE, _WAREHOUSE, _BOB = uuid4(), uuid4(), uuid4()
_REL_A, _REL_B = uuid4(), uuid4()
_NOW = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)


def _node(
    entity_id: UUID,
    name: str,
    *,
    entity_type: str = "person",
    status: str = STATUS_PROPOSED,
    confidence: str = "0.700",
    is_seed: bool = True,
) -> CaseGraphNode:
    return CaseGraphNode(
        case_id=_CASE,
        entity_id=entity_id,
        entity_type=entity_type,
        canonical_name=name,
        status=status,
        confidence=Decimal(confidence),
        is_seed=is_seed,
        projected_at=_NOW,
    )


def _edge(
    relationship_id: UUID,
    frm: UUID,
    to: UUID,
    *,
    rel_type: str = "located_at",
    status: str = STATUS_PROPOSED,
    confidence: str = "0.650",
) -> CaseGraphEdge:
    return CaseGraphEdge(
        case_id=_CASE,
        relationship_id=relationship_id,
        rel_type=rel_type,
        from_entity_id=frm,
        to_entity_id=to,
        status=status,
        confidence=Decimal(confidence),
        projected_at=_NOW,
    )


class _FakeProjection:
    """Stands in for ``GraphProjectionRepository``, recording what the route asked for.

    A fake at this seam rather than a fake database: the SQL is proven against real Postgres in
    `test_graph_projection_db.py`, and what these tests need to see is the *arguments* the router
    derived from the query string — which a real database would hide behind its results.
    """

    def __init__(self, nodes: list[CaseGraphNode], edges: list[CaseGraphEdge]) -> None:
        self._nodes = nodes
        self._edges = edges
        self.calls: list[dict[str, Any]] = []

    async def read_subgraph(
        self,
        case_id: UUID,
        *,
        statuses: Any,
        entity_types: Any,
        min_confidence: Any,
        depth: int,
    ) -> tuple[list[CaseGraphNode], list[CaseGraphEdge]]:
        self.calls.append(
            {
                "case_id": case_id,
                "statuses": list(statuses),
                "entity_types": list(entity_types) if entity_types else None,
                "min_confidence": min_confidence,
                "depth": depth,
            }
        )
        return self._nodes, self._edges


class _AllowAll:
    async def user_has_access(self, case_id: object, user_id: object) -> bool:
        return True


class _DenyAll:
    async def user_has_access(self, case_id: object, user_id: object) -> bool:
        return False


def _app(projection: _FakeProjection, *, checker: object) -> Any:
    app = create_app()
    actor = CurrentUser(user_id=uuid4(), roles=("investigator",))

    def _service() -> InvestigationService:
        service = InvestigationService.__new__(InvestigationService)
        service._graph = projection  # type: ignore[attr-defined]
        return service

    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_kms] = lambda: kms_for_tests()
    app.dependency_overrides[get_case_access_checker] = lambda: checker
    app.dependency_overrides[get_investigation_service] = _service
    return app


@pytest.fixture
def projection() -> _FakeProjection:
    return _FakeProjection(
        nodes=[
            _node(_ALICE, "Alice"),
            _node(_WAREHOUSE, "5th Street Warehouse", entity_type="location"),
        ],
        edges=[_edge(_REL_A, _ALICE, _WAREHOUSE)],
    )


async def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- the contract -----------------------------------------------------------
async def test_the_graph_endpoint_returns_the_documented_envelope(
    projection: _FakeProjection,
) -> None:
    """§6's response body: `{ data: { entities: [...], relationships: [...] } }`."""
    async with await _client(_app(projection, checker=_AllowAll())) as client:
        response = await client.get(f"/api/v1/cases/{_CASE}/graph")

    assert response.status_code == 200
    data = response.json()["data"]
    assert {e["canonical_name"] for e in data["entities"]} == {"Alice", "5th Street Warehouse"}
    assert data["relationships"][0]["relationship_id"] == str(_REL_A)
    assert response.json()["meta"]["request_id"]


async def test_the_edge_exposes_type_not_rel_type(projection: _FakeProjection) -> None:
    """§6's example payload names the field `type`. The projection column is `rel_type` (because
    `type` shadows a builtin), so the schema aliases it — and a client reads the documented name."""
    async with await _client(_app(projection, checker=_AllowAll())) as client:
        response = await client.get(f"/api/v1/cases/{_CASE}/graph")

    edge = response.json()["data"]["relationships"][0]
    assert edge["type"] == "located_at"
    assert "rel_type" not in edge


async def test_the_response_carries_projection_metadata(projection: _FakeProjection) -> None:
    """`is_seed` and `projected_at` have no equivalent on the transactional row, which is why the
    graph has its own read schema rather than reusing `EntityRead`.

    `projected_at` is the honest answer to "how stale is this?" — ADR-0013 §2 accepts bounded
    staleness, and a client cannot reason about a bound it cannot see.
    """
    async with await _client(_app(projection, checker=_AllowAll())) as client:
        response = await client.get(f"/api/v1/cases/{_CASE}/graph")

    entity = response.json()["data"]["entities"][0]
    assert entity["is_seed"] is True
    assert entity["projected_at"]


# --- authorization ----------------------------------------------------------
async def test_a_non_member_is_refused_the_graph(projection: _FakeProjection) -> None:
    """The requirement this wave inherits from Wave 3.1.

    A case graph is case-scoped data, so `require_case_access` (owner or member, ADR-0017) gates it
    exactly as it gates the case itself. The caller here holds `investigator` — RBAC is not what
    stops them.
    """
    async with await _client(_app(projection, checker=_DenyAll())) as client:
        response = await client.get(f"/api/v1/cases/{_CASE}/graph")

    assert response.status_code == 403
    assert projection.calls == [], "the projection must not even be queried for a refused caller"


# --- query parameters -------------------------------------------------------
async def test_the_default_filters_match_the_documented_defaults(
    projection: _FakeProjection,
) -> None:
    """§6: `status` defaults to `proposed,confirmed` and `depth` to 1."""
    async with await _client(_app(projection, checker=_AllowAll())) as client:
        await client.get(f"/api/v1/cases/{_CASE}/graph")

    call = projection.calls[0]
    assert call["statuses"] == [STATUS_PROPOSED, STATUS_CONFIRMED]
    assert call["depth"] == 1
    assert call["entity_types"] is None
    assert call["min_confidence"] is None


async def test_csv_filters_are_split(projection: _FakeProjection) -> None:
    async with await _client(_app(projection, checker=_AllowAll())) as client:
        await client.get(
            f"/api/v1/cases/{_CASE}/graph",
            params={"status": "confirmed", "entity_types": "person, location"},
        )

    call = projection.calls[0]
    assert call["statuses"] == [STATUS_CONFIRMED]
    assert call["entity_types"] == ["person", "location"], "whitespace around a value is trimmed"


async def test_an_empty_csv_filter_means_no_filter(projection: _FakeProjection) -> None:
    """A client sending `?entity_types=` did not filter. Treating it as a filter that matches
    nothing would answer an empty graph, which is a confusing reply to "no preference"."""
    async with await _client(_app(projection, checker=_AllowAll())) as client:
        await client.get(f"/api/v1/cases/{_CASE}/graph", params={"entity_types": " , "})

    assert projection.calls[0]["entity_types"] is None


async def test_min_confidence_reaches_the_projection_as_a_decimal(
    projection: _FakeProjection,
) -> None:
    """Decimal, not float: the column is `Numeric` and this is a threshold comparison, so a float
    would answer the boundary case differently from the write side (ADR-0011 §2)."""
    async with await _client(_app(projection, checker=_AllowAll())) as client:
        await client.get(f"/api/v1/cases/{_CASE}/graph", params={"min_confidence": "0.65"})

    value = projection.calls[0]["min_confidence"]
    assert isinstance(value, Decimal)
    assert value == Decimal("0.65")


@pytest.mark.parametrize("depth", [0, 1, 2, 3])
async def test_depth_within_the_cap_is_accepted(projection: _FakeProjection, depth: int) -> None:
    async with await _client(_app(projection, checker=_AllowAll())) as client:
        response = await client.get(f"/api/v1/cases/{_CASE}/graph", params={"depth": depth})

    assert response.status_code == 200
    assert projection.calls[0]["depth"] == depth


@pytest.mark.parametrize("depth", [4, 99, -1])
async def test_depth_outside_the_cap_is_rejected(projection: _FakeProjection, depth: int) -> None:
    """§6: "`depth` capped at 3 to bound query cost". Rejected at the boundary with a 400 rather
    than silently clamped — a caller asking for depth 10 has misunderstood something, and quietly
    answering depth 3 would hide that."""
    async with await _client(_app(projection, checker=_AllowAll())) as client:
        response = await client.get(f"/api/v1/cases/{_CASE}/graph", params={"depth": depth})

    assert response.status_code == 400
    assert projection.calls == []


@pytest.mark.parametrize("value", ["1.5", "-0.1"])
async def test_min_confidence_outside_zero_to_one_is_rejected(
    projection: _FakeProjection, value: str
) -> None:
    """A confidence is a probability. A threshold outside [0, 1] is a client bug, and accepting it
    would return either everything or nothing with no indication why."""
    async with await _client(_app(projection, checker=_AllowAll())) as client:
        response = await client.get(
            f"/api/v1/cases/{_CASE}/graph", params={"min_confidence": value}
        )

    assert response.status_code == 400


async def test_an_empty_projection_is_an_empty_graph(projection: _FakeProjection) -> None:
    """A case with no findings yet — or one whose projection has not caught up — is an empty graph,
    not a 404. The case exists; its graph is simply empty, and a 404 would tell a client the case
    was gone."""
    empty = _FakeProjection(nodes=[], edges=[])
    async with await _client(_app(empty, checker=_AllowAll())) as client:
        response = await client.get(f"/api/v1/cases/{_CASE}/graph")

    assert response.status_code == 200
    assert response.json()["data"] == {"entities": [], "relationships": []}
