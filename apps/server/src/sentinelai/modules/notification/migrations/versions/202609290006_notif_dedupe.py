"""notification schema — the §25.9 business-idempotency key, enforced.

`event-driven-architecture.md` §25.9 calls
``(recipient_user_id, source_module, source_reference_id)`` **"the tightest idempotency key in the
catalog, since a replayed event must never re-send an email the analyst already received"**. Until
now that key was a read-then-write check in the service (`exists_for_source`), which is correct
under sequential delivery and **not** under concurrency: two dispatcher workers handling two
different events that describe the same fact — a replayed upstream publication, one finding
announced for a case twice — can both find nothing and both insert. The analyst then gets the
message twice, which is precisely the outcome §25.9 forbids.

The check stays (it keeps the common case cheap and lets the service return a clear "already
delivered"); the index makes the race impossible. Same division of labour as
`uq_entity_mention_pair` in `investigation` and `uq_social_account_platform_handle` in
`social_media`.

**Why ``message`` is in the key, via ``md5``.** §25.9's keys are not uniform:
`case.status_changed`'s key is ``(recipient_user_id, source_reference_id, new_status)`` — a case
legitimately notifies its investigator once per transition, so ``(recipient, case_management,
case_id)`` alone would collapse "opened" and "closed" into one notification and silence every
transition after the first. The service composes each message as a pure function of exactly its key
fields, so including the message carries `new_status` for that handler and is a no-op for the three
whose key has no extra discriminator.

``md5`` here is a **length-bounding discriminator, not a security control** — a btree entry is
capped around 2704 bytes and a message has no documented length limit, so indexing the raw text
would turn a long message into a failed insert (a lost notification and a dead-lettered handler)
instead of a deduplicated one. Nothing security-relevant depends on it: a collision would suppress
one duplicate notification, and the signed, hash-chained audit surfaces (security-architecture §22)
are elsewhere and unaffected.

Nullable columns behave correctly here without extra work: Postgres treats each NULL as distinct in
a unique index, so a notification with no ``source_module``/``source_reference_id`` has no business
key and is never deduplicated — which is the right answer for a row that describes no upstream fact.

Revision ID: 202609290006_notif_dedupe
Revises: 202609280008_notif_evtsig
"""

from __future__ import annotations

from alembic import op

revision = "202609290006_notif_dedupe"
down_revision = "202609280008_notif_evtsig"
branch_labels = None
depends_on = None

_SCHEMA = "notification"
_INDEX = "uq_notification_dedupe"


def upgrade() -> None:
    # Raw DDL rather than `op.create_index`: the last key element is an expression, and Alembic's
    # helper takes column names. Written out so the index Postgres holds is exactly what is read
    # here, with no translation layer to reason about.
    op.execute(
        f"CREATE UNIQUE INDEX {_INDEX} ON {_SCHEMA}.notifications "
        "(recipient_user_id, source_module, source_reference_id, md5(message))"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX {_SCHEMA}.{_INDEX}")
