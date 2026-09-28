"""Administrative HTTP surface — api-design.md §4.1, §10.

Exports only the router, which the HTTP composition root registers. The repository and schemas are
internals of this package.
"""

from __future__ import annotations

from sentinelai.platform.admin.router import router as admin_router

__all__ = ["admin_router"]
