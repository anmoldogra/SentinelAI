"""Structured logging (structlog) — guide Part 10.

``correlation_id`` / ``request_id`` / ``trace_id`` are bound once in the HTTP
correlation middleware (guide Part 2) and appear on every subsequent log line for
that request automatically via ``merge_contextvars`` — no call site threads them.
"""

from __future__ import annotations

import logging

import structlog


def configure_logging(level: str = "INFO", *, json_logs: bool = True) -> None:
    """Configure structlog process-wide. Call once at entrypoint startup.

    JSON renderer in every real environment (log aggregation via Loki); a console
    renderer is used only when ``json_logs`` is False for local readability.
    """
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


log: structlog.stdlib.BoundLogger = structlog.get_logger()
