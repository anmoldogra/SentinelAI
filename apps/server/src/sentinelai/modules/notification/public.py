"""notification public interface — the ONLY symbols other modules may import."""

from __future__ import annotations

from sentinelai.modules.notification.schemas import NotificationRead
from sentinelai.modules.notification.service import NotificationService

__all__ = ["NotificationService", "NotificationRead"]
