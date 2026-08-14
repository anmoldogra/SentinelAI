"""Background-task enqueue seam (arq) — the produce side of guide Part 12.

The worker (``entrypoints/worker``) runs the jobs; this is how an HTTP request hands
one off. The arq pool is created once in the HTTP lifespan and stored on
``app.state.task_queue``; ``get_task_queue`` exposes it as a dependency. A ``Protocol``
keeps callers (services) decoupled from arq's concrete type and easy to fake in tests.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from fastapi import Request


@runtime_checkable
class TaskQueue(Protocol):
    """Minimal enqueue surface (satisfied by ``arq.ArqRedis``)."""

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> Any: ...


def get_task_queue(request: Request) -> TaskQueue:
    """FastAPI dependency returning the process-wide task queue."""
    queue: TaskQueue | None = getattr(request.app.state, "task_queue", None)
    if queue is None:  # pragma: no cover - only when Redis was unavailable at startup
        raise RuntimeError("task queue is unavailable (Redis not connected at startup)")
    return queue
