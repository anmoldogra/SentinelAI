"""Cursor pagination primitives — guide Part 19, api-design.md §2.5.

Opaque, base64-encoded cursors carrying the sort value + tie-break id, aligned
with database-design.md §6–7's BRIN-indexed, append-ordered tables (never offset
pagination on a large append-heavy resource — guide anti-pattern #48). Implemented
once here and reused by every list endpoint.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from uuid import UUID

from fastapi import Query

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200


def encode_cursor(sort_value: str, id_value: UUID) -> str:
    """Encode an opaque forward cursor from the last row's sort value + id."""
    raw = json.dumps({"v": sort_value, "id": str(id_value)}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[str, UUID]:
    """Decode an opaque cursor back into (sort_value, id). Raises on malformed input."""
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    data = json.loads(raw)
    return data["v"], UUID(data["id"])


@dataclass(frozen=True, slots=True)
class PageParams:
    """Parsed pagination query parameters for a list endpoint."""

    limit: int
    cursor: str | None


def page_params(
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    cursor: str | None = Query(None),
) -> PageParams:
    """FastAPI dependency yielding validated ``PageParams`` for any list route."""
    return PageParams(limit=limit, cursor=cursor)
