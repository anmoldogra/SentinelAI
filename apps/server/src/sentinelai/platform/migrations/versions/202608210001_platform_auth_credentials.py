"""platform schema — password and bearer-token credential columns (ADR-0010 §1, §3).

Resolves ADR-0010's open decision D1: ``platform.sessions`` had no way to map a presented bearer
token back to a row. Adds the ADR's two-part design — the argon2id ``token_hash`` plus the short,
indexed, non-secret ``token_lookup`` prefix that makes an otherwise unindexable salted digest
resolvable in one index seek — and the ``users.password_hash`` that password login (ADR-0010 §3)
verifies against. ``database-design.md`` §3.1 is updated in this same change, as ADR-0010 §1
requires.

Revision ID: 202608210001_platform_authn
Revises: 202607300001_platform_privs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "202608210001_platform_authn"
down_revision = "202607300001_platform_privs"
branch_labels = None
depends_on = None

_SCHEMA = "platform"
# Mirrors platform.security.tokens.LOOKUP_PREFIX_LENGTH. Duplicated as a literal on purpose:
# a migration is a historical record and must not shift if that constant is ever retuned.
_LOOKUP_PREFIX_LENGTH = 12


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("password_hash", sa.Text(), nullable=True),
        schema=_SCHEMA,
    )

    # Any pre-existing session predates token storage and can never be resolved by the new
    # lookup path, so it is already dead weight — clearing the table is what makes the NOT NULL
    # adds safe on a populated database. Nothing could authenticate before this migration
    # (`SessionRepository.get_active_by_token` raised `NotImplementedError`), so no live session
    # is being destroyed here.
    op.execute(sa.text(f"DELETE FROM {_SCHEMA}.sessions"))

    op.add_column(
        "sessions",
        sa.Column("token_lookup", sa.String(length=_LOOKUP_PREFIX_LENGTH), nullable=False),
        schema=_SCHEMA,
    )
    op.add_column(
        "sessions",
        sa.Column("token_hash", sa.Text(), nullable=False),
        schema=_SCHEMA,
    )
    # Non-unique by design: a prefix collision must cost an extra verify, not a failed login.
    op.create_index(
        "ix_sessions_token_lookup",
        "sessions",
        ["token_lookup"],
        unique=False,
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_token_lookup", table_name="sessions", schema=_SCHEMA)
    op.drop_column("sessions", "token_hash", schema=_SCHEMA)
    op.drop_column("sessions", "token_lookup", schema=_SCHEMA)
    op.drop_column("users", "password_hash", schema=_SCHEMA)
