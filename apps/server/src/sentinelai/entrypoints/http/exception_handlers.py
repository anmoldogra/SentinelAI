"""Global exception handlers — the API error envelope (guide Part 2 & 7, api-design.md §2.4).

These handlers *are* the error-response mapping — there is no separate Problem
Details layer. ``DomainError`` maps to its ``http_status``/``code``;
``RequestValidationError`` is overridden to 400 (malformed shape) so it is distinct
from a 422 (a domain rule failing); anything unhandled becomes a 500 INTERNAL_ERROR
without leaking internals.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from sentinelai.platform.idempotency import IdempotentReplay
from sentinelai.platform.logging import log
from sentinelai.shared.exceptions import DomainError


def _error_body(request: Request, *, code: str, message: str, details: Any) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "request_id": getattr(request.state, "request_id", None),
            "correlation_id": getattr(request.state, "correlation_id", None),
            "timestamp": datetime.now(UTC).isoformat(),
        }
    }


def register_exception_handlers(app: FastAPI) -> None:
    """Register the four response-shaping exception handlers on the app."""

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=_error_body(request, code=exc.code, message=str(exc), details=exc.details),
        )

    @app.exception_handler(IdempotentReplay)
    async def idempotent_replay_handler(request: Request, exc: IdempotentReplay) -> Response:
        """Serve a stored response verbatim — api-design.md §2.9, ADR-0012 §2(a).

        Not an error path, despite arriving as an exception: a dependency can only *refuse* to let
        the handler run, and refusing is precisely what a replay needs. Raising is what guarantees
        "without re-executing business logic" rather than merely deduplicating its writes.

        ``Response``, not ``JSONResponse``: the body is replayed as the bytes that were sent, so
        it must not be re-serialized. §2.9 says "same status code, same body", and a client that
        signed or hashed the original would notice a re-encoding that changed key order.
        """
        return Response(
            content=exc.body,
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def shape_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Override FastAPI's default 422-for-everything to match api-design.md §2.4's
        # 400 (malformed shape) vs 422 (domain rule) split.
        return JSONResponse(
            status_code=400,
            content=_error_body(
                request, code="VALIDATION_FAILED", message="Malformed request", details=exc.errors()
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_exception", path=request.url.path)
        return JSONResponse(
            status_code=500,
            content=_error_body(
                request, code="INTERNAL_ERROR", message="An internal error occurred", details=[]
            ),
        )
