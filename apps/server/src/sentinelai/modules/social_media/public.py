"""social_media public interface — the ONLY symbols other modules may import."""

from __future__ import annotations

from sentinelai.modules.social_media.schemas import ContentRead
from sentinelai.modules.social_media.service import SocialMediaService

__all__ = ["ContentRead", "SocialMediaService"]
