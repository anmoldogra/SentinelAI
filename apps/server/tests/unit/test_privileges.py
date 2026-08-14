"""Unit tests for the evidentiary-table privilege DDL (ADR-0004 part 2).

Deterministic string assertions — the SQL is verified for *shape* here and for *behaviour* against a
real Postgres (when the roles are provisioned) in ``tests/integration/test_privileges_db.py``.
"""

from __future__ import annotations

from sentinelai.platform.db.privileges import (
    APP_ROLE,
    APPEND_ROLE,
    grant_evidentiary_privileges_sql,
    revoke_evidentiary_privileges_sql,
)


def test_grant_gives_append_role_insert_select_only() -> None:
    sql = grant_evidentiary_privileges_sql("ingestion", "evidence")
    assert f"GRANT INSERT, SELECT ON ingestion.evidence TO {APPEND_ROLE};" in sql
    # UPDATE, DELETE, and TRUNCATE are all revoked (TRUNCATE bypasses the row-level trigger).
    assert f"REVOKE UPDATE, DELETE, TRUNCATE ON ingestion.evidence FROM {APPEND_ROLE};" in sql
    # Minimum privilege: the mutable-DML role gets NO positive grant on evidentiary tables.
    assert f"GRANT INSERT, SELECT ON ingestion.evidence TO {APP_ROLE};" not in sql
    assert f"REVOKE UPDATE, DELETE, TRUNCATE ON ingestion.evidence FROM {APP_ROLE};" in sql


def test_grant_is_role_existence_guarded() -> None:
    sql = grant_evidentiary_privileges_sql("platform", "audit_log")
    assert "DO $$" in sql and "END $$;" in sql
    assert f"pg_roles WHERE rolname = '{APPEND_ROLE}'" in sql
    assert f"pg_roles WHERE rolname = '{APP_ROLE}'" in sql


def test_downgrade_revokes_the_grant_and_never_restores_mutation() -> None:
    sql = revoke_evidentiary_privileges_sql("platform", "audit_log")
    assert f"REVOKE INSERT, SELECT ON platform.audit_log FROM {APPEND_ROLE};" in sql
    # A downgrade must never re-introduce UPDATE/DELETE on an evidentiary table.
    assert "GRANT UPDATE" not in sql
    assert "GRANT DELETE" not in sql
    assert "DO $$" in sql  # still guarded


def test_no_mutation_grant_appears_anywhere() -> None:
    for schema, table in (
        ("platform", "audit_log"),
        ("ingestion", "evidence"),
        ("ingestion", "evidence_custody_events"),
    ):
        sql = grant_evidentiary_privileges_sql(schema, table)
        assert "GRANT UPDATE" not in sql
        assert "GRANT DELETE" not in sql
        assert "GRANT TRUNCATE" not in sql  # TRUNCATE is only ever revoked, never granted
        assert f"REVOKE UPDATE, DELETE, TRUNCATE ON {schema}.{table} FROM {APPEND_ROLE};" in sql
