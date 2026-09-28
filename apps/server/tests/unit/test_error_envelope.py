"""The HTTP error envelope — api-design.md §2.4, guide Part 2 & 7.

Written for a defect: the ``RequestValidationError`` handler put ``exc.errors()`` straight into a
``JSONResponse``, and each entry of that list carries the **offending input** coerced toward the
parameter's declared type. A non-JSON-native type there — a ``Decimal`` query parameter failing a
bound — made ``json.dumps`` raise *inside the handler*, so the unhandled-exception handler caught it
and an out-of-range query parameter came back as a **500 instead of a 400**.

It went unnoticed because every route until ADR-0013's graph endpoint took only `str`/`int`/`UUID`
query parameters, all of which serialize. These tests pin the envelope for the awkward types so the
next one does not have to be found the same way.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import FastAPI, Query
from fastapi.testclient import TestClient

from sentinelai.entrypoints.http.exception_handlers import register_exception_handlers
from sentinelai.entrypoints.http.middleware import register_middleware


def _client() -> TestClient:
    app = FastAPI()
    register_middleware(app)
    register_exception_handlers(app)

    @app.get("/decimal")
    async def decimal_param(value: Decimal = Query(ge=0, le=1)) -> dict[str, str]:
        return {"value": str(value)}

    @app.get("/integer")
    async def integer_param(value: int = Query(ge=0, le=3)) -> dict[str, int]:
        return {"value": value}

    return TestClient(app, raise_server_exceptions=False)


def test_a_decimal_out_of_range_is_a_400_not_a_500() -> None:
    """The defect, directly. A 500 here tells a client the server broke when in fact the client
    sent a bad value — and it hides the field that was wrong."""
    response = _client().get("/decimal", params={"value": "1.5"})

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert body["error"]["details"], "the offending field must still be reported"


def test_the_offending_decimal_input_is_reported_as_json() -> None:
    """The input is echoed back so a client can see what was rejected. It has to survive encoding
    to be useful, which is exactly what broke."""
    response = _client().get("/decimal", params={"value": "-0.1"})

    detail = response.json()["error"]["details"][0]
    assert detail["loc"][-1] == "value"
    assert str(detail["input"]) in {"-0.1", "-0.100"}


def test_an_unparseable_decimal_is_also_a_400() -> None:
    """A value that never became a `Decimal` at all takes a different path through pydantic and
    must land in the same envelope."""
    response = _client().get("/decimal", params={"value": "not-a-number"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_an_integer_out_of_range_still_works() -> None:
    """The type that already worked, kept honest — the fix must not have changed the shape of the
    envelope for the parameters every other route uses."""
    response = _client().get("/integer", params={"value": "9"})

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert body["error"]["details"][0]["input"] == "9"


def test_the_envelope_carries_the_correlation_fields() -> None:
    """§2.4: every error body carries `request_id` and `correlation_id`, so a client's report of a
    rejection can be tied to the server's own log line."""
    response = _client().get("/decimal", params={"value": "2"})

    error = response.json()["error"]
    assert error["request_id"] and error["correlation_id"]
    assert error["timestamp"]


def test_a_valid_value_is_untouched() -> None:
    response = _client().get("/decimal", params={"value": "0.65"})

    assert response.status_code == 200
    assert response.json()["value"] == "0.65"
