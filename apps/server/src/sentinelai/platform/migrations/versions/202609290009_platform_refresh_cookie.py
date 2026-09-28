"""platform schema — the refresh credential on ``sessions`` (ADR-0010 A3).

A3 splits the session credential in two: a short-lived access token in the ``Authorization`` header,
held in JavaScript memory only, and a long-lived refresh token in an ``HttpOnly; Secure;
SameSite=Strict`` cookie scoped to the refresh endpoint. Wave 3.1 built A3's *rotation* — a
successful refresh issues a successor and revokes its predecessor — but with one credential class
carried in the request body. This adds the second.

**Additive and nullable.** Sessions written before this migration have no refresh token and cannot
be given one: the plaintext was never stored, so there is no digest to backfill. Those sessions stay
usable until their access token expires and are then simply not refreshable. Making the columns
``NOT NULL`` with a placeholder would mean writing a digest that no token matches — a row that
claims a credential exists when none does.

**Why `refresh_expires_at` is separate from `expires_at`.** The access token must be able to expire
while the session is still refreshable; that is the whole purpose of the split. One expiry column
cannot express it, and a refresh path checking ``expires_at`` would refuse precisely the case it
exists to serve.

Revision ID: 202609290009_platform_refresh
Revises: 202609290008_platform_idem
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "202609290009_platform_refresh"
down_revision = "202609290008_platform_idem"
branch_labels = None
depends_on = None

_SCHEMA = "platform"
_TABLE = "sessions"
# Mirrors `security.tokens.LOOKUP_PREFIX_LENGTH`. Duplicated as a literal on purpose: a migration
# records the schema at its own revision, and importing the constant would let a later change to it
# silently rewrite history.
_LOOKUP_PREFIX_LENGTH = 12


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("refresh_token_lookup", sa.String(length=_LOOKUP_PREFIX_LENGTH), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(_TABLE, sa.Column("refresh_token_hash", sa.Text(), nullable=True), schema=_SCHEMA)
    op.add_column(
        _TABLE,
        sa.Column("refresh_expires_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        schema=_SCHEMA,
    )
    # Non-unique, like the access-token prefix index: a prefix collision between two live refresh
    # tokens must cost an extra argon2 verify, never a rejected refresh.
    op.create_index(
        "ix_sessions_refresh_token_lookup", _TABLE, ["refresh_token_lookup"], schema=_SCHEMA
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_refresh_token_lookup", table_name=_TABLE, schema=_SCHEMA)
    op.drop_column(_TABLE, "refresh_expires_at", schema=_SCHEMA)
    op.drop_column(_TABLE, "refresh_token_hash", schema=_SCHEMA)
    op.drop_column(_TABLE, "refresh_token_lookup", schema=_SCHEMA)
