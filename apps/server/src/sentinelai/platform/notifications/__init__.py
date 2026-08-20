"""Notification delivery port (security-architecture §25's "notify the uploading analyst").

A capability port only — *what* to send and *to whom* is a domain decision owned by the
``notification`` module; this package owns only the act of handing a message to a channel.
Keeps ``platform`` domain-agnostic (the import-linter contract): the message carries a
recipient id, a subject, and a body, and knows nothing about evidence or cases.
"""

from sentinelai.platform.notifications.sender import (
    LoggingNotificationSender,
    NotificationMessage,
    NotificationSender,
    SenderNotAvailable,
    build_notification_sender,
)

__all__ = [
    "LoggingNotificationSender",
    "NotificationMessage",
    "NotificationSender",
    "SenderNotAvailable",
    "build_notification_sender",
]
