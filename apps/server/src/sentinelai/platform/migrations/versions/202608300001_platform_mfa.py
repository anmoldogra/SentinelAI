"""platform schema — TOTP MFA storage (ADR-0010 A1, database-design.md §3.1).

`security-architecture.md` §8 makes MFA mandatory for every role that can reach evidence or case
data, and `api-design.md` §9 already documents the login → `{mfa_required, mfa_token}` →
`/auth/mfa/verify` exchange — but nothing in the schema could store a TOTP secret or that
`mfa_token`, so the documented endpoint was unbuildable. This adds exactly what
`database-design.md` §3.1 now specifies, and nothing beyond it.

**The secret is encrypted, not hashed.** Every other credential in this schema is a one-way
digest, because the server only verifies a value the client presents. A TOTP secret must be
recomputed over on every verification, so it has to be recoverable — argon2id here would produce a
column that can never authenticate anyone. The four columns hold a `Ciphertext` (value, nonce,
algorithm, key_id) produced under `KeyPurpose.SESSION_ROOT`, which ADR-0009 §7 already reserved
for "ADR-0010 token/secret keying". Splitting them, rather than storing one opaque blob, is what
keeps `key_id` queryable so a key rotation is a targeted scan instead of a full-table rewrite.

Every column is nullable: existing users are not enrolled, and this migration enrols nobody. The
CHECK constraint is what stops that nullability from permitting a *half-written* enrolment.

`platform.users` is not append-only (ADR-0004's trigger covers `audit_log` only), so enrolment may
UPDATE the row — no derived-state overlay is needed here, unlike `ingestion.evidence`.

Revision ID: 202608300001_platform_mfa
Revises: 202608210001_platform_authn
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "202608300001_platform_mfa"
down_revision = "202608210001_platform_authn"
branch_labels = None
depends_on = None

_SCHEMA = "platform"
# Mirrors platform.security.tokens.LOOKUP_PREFIX_LENGTH, duplicated as a literal for the same
# reason 202608210001 duplicates it: a migration is a historical record and must not shift if
# that constant is ever retuned.
_LOOKUP_PREFIX_LENGTH = 12

# The invariant database-design.md §3.1 states as "non-null iff `mfa_enrolled_at` is". Written as
# a constraint rather than trusted to the service layer: a partially-written enrolment would leave
# an account that believes it has a second factor but cannot verify one, which fails *open* at the
# exact moment MFA matters. `mfa_last_used_step` is deliberately outside it — it stays null until
# the first successful verification, which is a legitimately later event.
_MFA_SECRET_COMPLETE = """
    (
        mfa_enrolled_at IS NULL
        AND mfa_secret_ciphertext IS NULL
        AND mfa_secret_nonce IS NULL
        AND mfa_secret_algorithm IS NULL
        AND mfa_secret_key_id IS NULL
    ) OR (
        mfa_enrolled_at IS NOT NULL
        AND mfa_secret_ciphertext IS NOT NULL
        AND mfa_secret_nonce IS NOT NULL
        AND mfa_secret_algorithm IS NOT NULL
        AND mfa_secret_key_id IS NOT NULL
    )
"""


def upgrade() -> None:
    for column in (
        sa.Column("mfa_enrolled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("mfa_secret_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("mfa_secret_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("mfa_secret_algorithm", sa.Text(), nullable=True),
        sa.Column("mfa_secret_key_id", sa.Text(), nullable=True),
        # RFC 6238 §5.2: a verifier must reject a code it has already accepted. Without this, a
        # code observed inside its ~30s step is replayable by anyone who saw it.
        sa.Column("mfa_last_used_step", sa.BigInteger(), nullable=True),
    ):
        op.add_column("users", column, schema=_SCHEMA)

    # Named WITHOUT the `ck_users_` prefix: `platform.db.base.NAMING_CONVENTION` renders `ck` as
    # `ck_%(table_name)s_%(constraint_name)s`, so passing the prefixed form here would produce
    # `ck_users_ck_users_mfa_secret_complete` — and a `downgrade()` that then failed to find it.
    op.create_check_constraint("mfa_secret_complete", "users", _MFA_SECRET_COMPLETE, schema=_SCHEMA)

    # One row per code, retained after redemption rather than deleted, so "which code was used,
    # and when" survives for audit (security-architecture §8, §48).
    op.create_table(
        "mfa_recovery_codes",
        sa.Column("code_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Hashed, not encrypted: a recovery code is only ever verified, never read back.
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], [f"{_SCHEMA}.users.user_id"]),
        schema=_SCHEMA,
    )
    # Verification loads a user's whole (small) code set and verifies each candidate; argon2id is
    # salted, so there is nothing to index on the digest itself.
    op.create_index(
        "ix_mfa_recovery_codes_user_id", "mfa_recovery_codes", ["user_id"], schema=_SCHEMA
    )

    # The `mfa_token` from api-design.md §9. It is a credential for a half-authenticated principal
    # — the password has been accepted but the second factor has not — so it is revocable
    # server-side state shaped exactly like `sessions`, never a self-contained token the server
    # cannot withdraw.
    op.create_table(
        "mfa_challenges",
        sa.Column("challenge_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_lookup", sa.String(length=_LOOKUP_PREFIX_LENGTH), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        # Single-use: set on the first successful verify, so a replayed `mfa_token` cannot mint a
        # second session.
        sa.Column("consumed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], [f"{_SCHEMA}.users.user_id"]),
        schema=_SCHEMA,
    )
    # Non-unique, for the same reason `ix_sessions_token_lookup` is: a prefix collision between two
    # live challenges must cost an extra verify, never a rejected login.
    op.create_index(
        "ix_mfa_challenges_token_lookup",
        "mfa_challenges",
        ["token_lookup"],
        unique=False,
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_mfa_challenges_token_lookup", table_name="mfa_challenges", schema=_SCHEMA)
    op.drop_table("mfa_challenges", schema=_SCHEMA)
    op.drop_index("ix_mfa_recovery_codes_user_id", table_name="mfa_recovery_codes", schema=_SCHEMA)
    op.drop_table("mfa_recovery_codes", schema=_SCHEMA)
    op.drop_constraint("ck_users_mfa_secret_complete", "users", schema=_SCHEMA)
    for name in (
        "mfa_last_used_step",
        "mfa_secret_key_id",
        "mfa_secret_algorithm",
        "mfa_secret_nonce",
        "mfa_secret_ciphertext",
        "mfa_enrolled_at",
    ):
        op.drop_column("users", name, schema=_SCHEMA)
