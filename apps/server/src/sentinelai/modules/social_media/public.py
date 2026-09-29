"""social_media public interface — the ONLY symbols other modules may import.

Nothing consumes this today: §25.6 makes `social_media` a pure publisher, and a downstream module
learns about captured content through the canonical `evidence.ingested` event that publication
produces — which is what §25.6 means by "reaches `investigation` via `evidence.ingested`". The
interface exists so that when something does need to ask, the answer is a schema and a service
rather than a table.
"""

from __future__ import annotations

from sentinelai.modules.social_media.schemas import AccountRead, ContentRead
from sentinelai.modules.social_media.service import SocialMediaService

__all__ = ["AccountRead", "ContentRead", "SocialMediaService"]
