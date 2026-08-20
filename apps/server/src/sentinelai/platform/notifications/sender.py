"""Notification delivery port + the Phase-1 logging adapter.

Mirrors the shape of ``platform.security.scanner`` and ``platform.storage``: a Protocol callers
depend on, a concrete adapter selected by configuration, and a ``build_*`` factory used by the
composition root. Real channels (SMTP, Slack, webhook) are later increments — only the logging
adapter exists today.

The port reports an outcome; it never decides *whether* to notify. That is a domain rule and
lives in the ``notification`` module's service (security-architecture §25).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sentinelai.platform.config import Settings
from sentinelai.platform.config import settings as default_settings
from sentinelai.platform.logging import log


class SenderNotAvailable(Exception):
    """The configured delivery channel has no adapter — never silently drop a message."""


@dataclass(frozen=True, slots=True)
class NotificationMessage:
    """One message bound for one recipient on one channel.

    Deliberately free of domain types: ``recipient_user_id`` is an opaque app-ref and the body is
    already-rendered text, so ``platform`` stays domain-agnostic.
    """

    recipient_user_id: UUID
    subject: str
    body: str
    channel: str = "log"


class NotificationSender(Protocol):
    """Port: hand a message to a delivery channel. Raises on a channel failure — a message that
    could not be delivered must never be reported as delivered."""

    channel: str

    async def send(self, message: NotificationMessage) -> None: ...


class LoggingNotificationSender:
    """Phase-1 adapter: records the delivery as a structured log line instead of sending it.

    Not a no-op stand-in for a missing feature — the in-app notification row IS the durable
    delivery in Phase 1 (``GET /api/v1/notifications``); this makes the dispatch observable until
    a real channel adapter lands. The body is logged, so a channel carrying sensitive evidence
    detail would need that reviewed before switching a real adapter on.
    """

    channel = "log"

    def __init__(self) -> None:
        self.sent: list[NotificationMessage] = []

    async def send(self, message: NotificationMessage) -> None:
        self.sent.append(message)
        log.info(
            "notification_dispatched",
            channel=self.channel,
            recipient_user_id=str(message.recipient_user_id),
            subject=message.subject,
        )


def build_notification_sender(cfg: Settings | None = None) -> NotificationSender:
    """Construct the configured sender.

    Unlike the malware scanner, the logging adapter is **not** refused in production: an
    undelivered email degrades the experience, it does not silently weaken a security control,
    and the notification row still reaches the analyst's in-app inbox. Selecting a channel with
    no adapter fails closed instead of dropping the message.
    """
    cfg = cfg or default_settings
    if cfg.notification_sender_provider == "log":
        return LoggingNotificationSender()
    raise SenderNotAvailable(
        f"no adapter for NOTIFICATION_SENDER_PROVIDER='{cfg.notification_sender_provider}'"
    )


__all__ = [
    "LoggingNotificationSender",
    "NotificationMessage",
    "NotificationSender",
    "SenderNotAvailable",
    "build_notification_sender",
]
