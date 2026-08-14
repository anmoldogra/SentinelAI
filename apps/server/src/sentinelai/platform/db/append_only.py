"""Append-only trigger backstop for evidentiary tables (ADR-0004, part 3).

A ``BEFORE UPDATE OR DELETE`` trigger that ``RAISE``s, so an in-place mutation of an evidentiary
row is impossible at the database layer — even if an application bug or a compromised application
role attempts one. This is **defense-in-depth beneath** ADR-0004's role-level ``REVOKE`` and
ADR-0003's signed/anchored ledgers; a superuser can still drop the trigger, which is why ADR-0003's
external anchoring — not the database alone — is the ultimate integrity guarantee.

The SQL is generated **here**, not inlined in migrations, so the Alembic migrations that install it
on the real evidentiary tables and the integration test that proves it enforce the **identical**
statements. One reject function per schema (``<schema>.reject_append_only_mutation``) is shared by
every evidentiary trigger in that schema; each table gets its own trigger.

Evidentiary tables (ADR-0004): ``ingestion.evidence``, ``ingestion.evidence_custody_events``,
``platform.audit_log``.
"""

from __future__ import annotations

_FUNCTION = "reject_append_only_mutation"


def reject_function_name(schema: str) -> str:
    """Fully-qualified name of the per-schema reject function."""
    return f"{schema}.{_FUNCTION}"


def trigger_name(table: str) -> str:
    """Name of the per-table append-only trigger."""
    return f"{table}_append_only"


def create_reject_function_sql(schema: str) -> str:
    """DDL creating (idempotently) the schema's ``RAISE``-on-mutation trigger function."""
    return (
        f"CREATE OR REPLACE FUNCTION {schema}.{_FUNCTION}() RETURNS trigger\n"
        "LANGUAGE plpgsql AS $$\n"
        "BEGIN\n"
        "    RAISE EXCEPTION\n"
        "        'append-only violation: % on %.% is forbidden (ADR-0004)',\n"
        "        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME\n"
        "        USING ERRCODE = 'restrict_violation';\n"
        "END;\n"
        "$$;"
    )


def create_trigger_sql(schema: str, table: str) -> str:
    """DDL attaching the append-only trigger to ``<schema>.<table>``."""
    return (
        f"CREATE TRIGGER {trigger_name(table)}\n"
        f"BEFORE UPDATE OR DELETE ON {schema}.{table}\n"
        f"FOR EACH ROW EXECUTE FUNCTION {schema}.{_FUNCTION}();"
    )


def drop_trigger_sql(schema: str, table: str) -> str:
    """DDL removing the append-only trigger (migration downgrade)."""
    return f"DROP TRIGGER IF EXISTS {trigger_name(table)} ON {schema}.{table};"


def drop_reject_function_sql(schema: str) -> str:
    """DDL removing the schema's reject function (migration downgrade, after its triggers)."""
    return f"DROP FUNCTION IF EXISTS {schema}.{_FUNCTION}();"
