"""Response envelopes — api-design.md §2.3 / guide Part 19.

Every successful single-resource response serializes through ``Envelope[T]`` and
every list response through ``ListEnvelope[T]``. There is no third success shape;
error responses use the shape produced by the entrypoint's ``domain_error_handler``.
A route always declares ``response_model=Envelope[SomeReadModel]`` — never an ORM model.
"""

from __future__ import annotations

from pydantic import BaseModel


class Meta(BaseModel):
    """Correlation metadata attached to every response body."""

    request_id: str
    correlation_id: str


class Envelope[T](BaseModel):
    """Single-resource success envelope."""

    data: T
    meta: Meta


class Pagination(BaseModel):
    """Cursor-pagination descriptor for list responses (api-design.md §2.5)."""

    next_cursor: str | None
    has_more: bool
    limit: int


class ListEnvelope[T](BaseModel):
    """List success envelope."""

    data: list[T]
    pagination: Pagination
    meta: Meta
