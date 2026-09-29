"""HTTP middleware — correlation IDs + request logging (guide Part 2, api-design.md §2.8).

Binds ``request_id`` (always server-minted), ``correlation_id`` (client-supplied via
``X-Correlation-Id`` or minted) and ``trace_id`` (the active OTel span's, when tracing is
configured — ADR-0018) into the structlog context, so every log line for the request carries all
three automatically. The first two are echoed back as response headers; ``trace_id`` is not, because
it is an internal execution detail with no business meaning (event-driven §11) and handing clients
a handle on the platform's own traces tells them about its topology for no benefit to them.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request, Response

from sentinelai.platform.logging import log
from sentinelai.platform.tracing import current_trace_id

_Handler = Callable[[Request], Awaitable[Response]]


def register_middleware(app: FastAPI) -> None:
    """Attach the correlation + request-logging middleware to the app."""

    @app.middleware("http")
    async def correlation_and_logging(request: Request, call_next: _Handler) -> Response:
        request_id = str(uuid4())
        correlation_id = request.headers.get("X-Correlation-Id", str(uuid4()))
        request.state.request_id = request_id
        request.state.correlation_id = correlation_id

        # ADR-0018: the OTel server span is already open here — `instrument_fastapi` is attached
        # after this middleware, and Starlette builds the stack so the last-added runs outermost.
        # Binding its trace id is what lets Grafana pivot from a Loki log line to the Tempo trace
        # (Part 20), and it is what `platform/logging.py` has always said this middleware does.
        with structlog.contextvars.bound_contextvars(
            request_id=request_id,
            correlation_id=correlation_id,
            **({"trace_id": trace_id} if (trace_id := current_trace_id()) else {}),
        ):
            start = time.perf_counter()
            log.info("request_started", method=request.method, path=request.url.path)
            try:
                response = await call_next(request)
            except Exception:
                log.exception(
                    "request_unhandled_exception", method=request.method, path=request.url.path
                )
                raise
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            log.info(
                "request_completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=duration_ms,
            )

        response.headers["X-Request-Id"] = request_id
        response.headers["X-Correlation-Id"] = correlation_id
        return response
