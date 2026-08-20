"""Unit tests for the notification read/update path (api-design.md §8).

Covers the caller's inbox listing (keyset pagination, recipient scoping) and mark-read
(idempotency, ownership). The repository's SQL is exercised against a real Postgres by the
integration tier; what is proved here is the service's pagination arithmetic and access rules.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from sentinelai.modules.notification.exceptions import NotificationNotFoundError
from sentinelai.modules.notification.models import Notification
from sentinelai.modules.notification.service import NotificationService
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.shared.exceptions import ForbiddenError
from sentinelai.shared.pagination import PageParams, decode_cursor

_BASE = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class _FakeNotificationRepo:
    """In-memory stand-in reproducing the repository's keyset contract."""

    def __init__(self) -> None:
        self.items: list[Notification] = []

    async def get_by_id(self, notification_id: UUID) -> Notification | None:
        return next(
            (n for n in self.items if n.notification_id == notification_id),
            None,
        )

    async def list_for_recipient(
        self,
        recipient_user_id: UUID,
        *,
        limit: int,
        cursor_created_at: datetime | None,
        cursor_notification_id: UUID | None,
    ) -> list[Notification]:
        rows = [n for n in self.items if n.recipient_user_id == recipient_user_id]
        if cursor_created_at is not None and cursor_notification_id is not None:
            rows = [
                n
                for n in rows
                if (n.created_at, n.notification_id) < (cursor_created_at, cursor_notification_id)
            ]
        rows.sort(key=lambda n: (n.created_at, n.notification_id), reverse=True)
        return rows[: limit + 1]  # the +1 the service uses to compute has_more


class _FakeUow:
    def __init__(self) -> None:
        self.session = object()
        self.notifications = _FakeNotificationRepo()
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _actor(user_id: UUID | None = None) -> CurrentUser:
    return CurrentUser(user_id=user_id or uuid4(), roles=("investigator",))


def _notification(recipient: UUID, *, minutes: int = 0, read: bool = False) -> Notification:
    return Notification(
        notification_id=uuid4(),
        rule_id=None,
        recipient_user_id=recipient,
        source_module="ingestion",
        source_reference_id=uuid4(),
        message=f"alert +{minutes}",
        created_at=_BASE + timedelta(minutes=minutes),
        read_at=_BASE if read else None,
    )


def _service(uow: Any) -> NotificationService:
    # A sender is irrelevant to the read path; supply a trivial one so no config is read.
    class _NullSender:
        channel = "log"

        async def send(self, message: Any) -> None:
            return None

    return NotificationService(uow, sender=_NullSender())


# --- listing ----------------------------------------------------------------


async def test_the_inbox_returns_only_the_callers_own_notifications() -> None:
    """The recipient is the authenticated actor, never a parameter."""
    uow = _FakeUow()
    me, someone_else = uuid4(), uuid4()
    uow.notifications.items = [
        _notification(me, minutes=1),
        _notification(someone_else, minutes=2),
        _notification(me, minutes=3),
    ]
    items, _, _ = await _service(uow).list_notifications(
        _actor(me), PageParams(limit=50, cursor=None)
    )
    assert {n.recipient_user_id for n in items} == {me}
    assert len(items) == 2


async def test_notifications_are_returned_newest_first() -> None:
    uow = _FakeUow()
    me = uuid4()
    uow.notifications.items = [_notification(me, minutes=m) for m in (1, 5, 3)]
    items, _, _ = await _service(uow).list_notifications(
        _actor(me), PageParams(limit=50, cursor=None)
    )
    assert [n.message for n in items] == ["alert +5", "alert +3", "alert +1"]


async def test_a_full_first_page_reports_more_and_issues_a_cursor() -> None:
    uow = _FakeUow()
    me = uuid4()
    uow.notifications.items = [_notification(me, minutes=m) for m in range(5)]
    items, next_cursor, has_more = await _service(uow).list_notifications(
        _actor(me), PageParams(limit=2, cursor=None)
    )
    assert len(items) == 2  # the limit+1 probe row is trimmed off
    assert has_more is True
    assert next_cursor is not None


async def test_a_short_page_reports_no_more_and_no_cursor() -> None:
    uow = _FakeUow()
    me = uuid4()
    uow.notifications.items = [_notification(me, minutes=1)]
    items, next_cursor, has_more = await _service(uow).list_notifications(
        _actor(me), PageParams(limit=50, cursor=None)
    )
    assert (len(items), has_more, next_cursor) == (1, False, None)


async def test_an_empty_inbox_is_not_an_error() -> None:
    uow = _FakeUow()
    items, next_cursor, has_more = await _service(uow).list_notifications(
        _actor(), PageParams(limit=50, cursor=None)
    )
    assert (items, next_cursor, has_more) == ([], None, False)


async def test_the_cursor_encodes_the_last_returned_row() -> None:
    uow = _FakeUow()
    me = uuid4()
    uow.notifications.items = [_notification(me, minutes=m) for m in range(4)]
    items, next_cursor, _ = await _service(uow).list_notifications(
        _actor(me), PageParams(limit=2, cursor=None)
    )
    assert next_cursor is not None
    sort_value, id_value = decode_cursor(next_cursor)
    assert id_value == items[-1].notification_id
    assert datetime.fromisoformat(sort_value) == items[-1].created_at


async def test_paging_walks_the_whole_inbox_without_gaps_or_repeats() -> None:
    uow = _FakeUow()
    me = uuid4()
    uow.notifications.items = [_notification(me, minutes=m) for m in range(7)]
    service, seen, cursor = _service(uow), [], None

    for _ in range(10):  # bounded: guards against a non-advancing cursor looping forever
        items, cursor, has_more = await service.list_notifications(
            _actor(me), PageParams(limit=3, cursor=cursor)
        )
        seen.extend(n.notification_id for n in items)
        if not has_more:
            break

    assert len(seen) == 7
    assert len(set(seen)) == 7  # every notification exactly once


async def test_a_tie_on_created_at_is_broken_by_id_so_paging_still_terminates() -> None:
    """Notifications raised in one transaction share a timestamp — the id makes the sort total."""
    uow = _FakeUow()
    me = uuid4()
    uow.notifications.items = [_notification(me, minutes=0) for _ in range(5)]
    service, seen, cursor = _service(uow), [], None

    for _ in range(10):
        items, cursor, has_more = await service.list_notifications(
            _actor(me), PageParams(limit=2, cursor=cursor)
        )
        seen.extend(n.notification_id for n in items)
        if not has_more:
            break

    assert len(set(seen)) == 5


# --- mark read --------------------------------------------------------------


async def test_marking_read_stamps_read_at() -> None:
    uow = _FakeUow()
    me = uuid4()
    notification = _notification(me)
    uow.notifications.items = [notification]
    result = await _service(uow).mark_read(notification.notification_id, _actor(me))
    assert result.read_at is not None


async def test_marking_read_is_idempotent_and_preserves_the_first_timestamp() -> None:
    """A second call must not rewrite when the analyst first saw the alert."""
    uow = _FakeUow()
    me = uuid4()
    notification = _notification(me)
    uow.notifications.items = [notification]
    service = _service(uow)

    first = await service.mark_read(notification.notification_id, _actor(me))
    stamped = first.read_at
    second = await service.mark_read(notification.notification_id, _actor(me))
    assert second.read_at == stamped


async def test_an_already_read_notification_keeps_its_original_timestamp() -> None:
    uow = _FakeUow()
    me = uuid4()
    notification = _notification(me, read=True)
    uow.notifications.items = [notification]
    result = await _service(uow).mark_read(notification.notification_id, _actor(me))
    assert result.read_at == _BASE


async def test_marking_someone_elses_notification_is_forbidden() -> None:
    """api-design.md §8: 403, deliberately NOT 404."""
    uow = _FakeUow()
    owner = uuid4()
    notification = _notification(owner)
    uow.notifications.items = [notification]
    with pytest.raises(ForbiddenError):
        await _service(uow).mark_read(notification.notification_id, _actor(uuid4()))
    assert notification.read_at is None  # refused, not half-applied


async def test_marking_an_unknown_notification_raises_not_found() -> None:
    with pytest.raises(NotificationNotFoundError):
        await _service(_FakeUow()).mark_read(uuid4(), _actor())


async def test_mark_read_never_commits() -> None:
    """ADR-0005: the router owns the transaction."""
    uow = _FakeUow()
    me = uuid4()
    notification = _notification(me)
    uow.notifications.items = [notification]
    await _service(uow).mark_read(notification.notification_id, _actor(me))
    assert uow.commits == 0
