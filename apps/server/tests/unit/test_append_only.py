"""Unit tests for the append-only DDL generators (ADR-0004 part 3).

Deterministic string assertions — the SQL is verified for *shape* here and for *behaviour* against a
real Postgres in ``tests/integration/test_append_only_db.py``.
"""

from __future__ import annotations

from sentinelai.platform.db.append_only import (
    create_reject_function_sql,
    create_trigger_sql,
    drop_reject_function_sql,
    drop_trigger_sql,
    reject_function_name,
    trigger_name,
)


def test_reject_function_ddl_raises_with_restrict_violation() -> None:
    sql = create_reject_function_sql("platform")
    assert "CREATE OR REPLACE FUNCTION platform.reject_append_only_mutation()" in sql
    assert "RAISE EXCEPTION" in sql
    assert "append-only violation" in sql
    assert "restrict_violation" in sql  # SQLSTATE class 23 → surfaced as an integrity error


def test_trigger_ddl_fires_before_update_or_delete_per_row() -> None:
    sql = create_trigger_sql("ingestion", "evidence")
    assert f"CREATE TRIGGER {trigger_name('evidence')}" in sql
    assert "BEFORE UPDATE OR DELETE ON ingestion.evidence" in sql
    assert "FOR EACH ROW" in sql
    assert f"EXECUTE FUNCTION {reject_function_name('ingestion')}()" in sql


def test_trigger_does_not_fire_on_insert() -> None:
    # Append-only means INSERT/SELECT remain allowed; the trigger must not name INSERT.
    assert "INSERT" not in create_trigger_sql("ingestion", "evidence_custody_events")


def test_drop_ddl_is_idempotent() -> None:
    assert drop_trigger_sql("platform", "audit_log") == (
        "DROP TRIGGER IF EXISTS audit_log_append_only ON platform.audit_log;"
    )
    assert drop_reject_function_sql("platform") == (
        "DROP FUNCTION IF EXISTS platform.reject_append_only_mutation();"
    )


def test_names_are_schema_and_table_scoped() -> None:
    assert reject_function_name("ingestion") == "ingestion.reject_append_only_mutation"
    assert trigger_name("audit_log") == "audit_log_append_only"
