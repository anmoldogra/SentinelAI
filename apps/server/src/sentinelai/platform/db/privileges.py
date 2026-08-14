"""Evidentiary-table privilege model (ADR-0004, part 2).

Minimum privileges for the three evidentiary tables (``platform.audit_log``,
``ingestion.evidence``, ``ingestion.evidence_custody_events``): the append role
(``sentinel_append``) may ``INSERT`` + ``SELECT`` only; **no** application role may ``UPDATE``,
``DELETE``, or ``TRUNCATE``. This is the privilege half of ADR-0004; the append-only trigger
(``platform.db.append_only``) blocks mutation unconditionally, and this layer additionally ensures
the application role cannot even *attempt* it.

Each statement is wrapped in a **role-existence guard** (a ``DO`` block checking ``pg_roles``), so
the migration is idempotent AND safe to run whether or not the roles have been provisioned yet — the
three roles are a deployment prerequisite (cluster-level ``CREATE ROLE``; ADR-0004 part 1 / infra),
so in production they exist before migrations run and the grants apply. If a misconfigured
environment runs this without the roles, the grants no-op — and the append-only trigger still
prevents mutation (defense-in-depth).

The SQL is generated here (not inlined in migrations) so the migrations and the integration test
exercise the identical statements.
"""

from __future__ import annotations

APP_ROLE = "sentinel_app"
APPEND_ROLE = "sentinel_append"


def grant_evidentiary_privileges_sql(schema: str, table: str) -> str:
    """Guarded ``GRANT INSERT, SELECT`` to the append role + ``REVOKE UPDATE, DELETE, TRUNCATE``.

    The append role gets the only positive grant (minimum privilege); the mutable-DML role
    (``sentinel_app``) gets no grant here, only an explicit revoke of mutation rights. ``TRUNCATE``
    is revoked as defense-in-depth: it is not granted by default, but it bypasses the row-level
    append-only trigger, so an evidentiary table must not expose it to any application role.
    """
    fq = f"{schema}.{table}"
    return (
        "DO $$\n"
        "BEGIN\n"
        f"    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APPEND_ROLE}') THEN\n"
        f"        GRANT INSERT, SELECT ON {fq} TO {APPEND_ROLE};\n"
        f"        REVOKE UPDATE, DELETE, TRUNCATE ON {fq} FROM {APPEND_ROLE};\n"
        "    END IF;\n"
        f"    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN\n"
        f"        REVOKE UPDATE, DELETE, TRUNCATE ON {fq} FROM {APP_ROLE};\n"
        "    END IF;\n"
        "END $$;"
    )


def revoke_evidentiary_privileges_sql(schema: str, table: str) -> str:
    """Downgrade: revoke the ``INSERT, SELECT`` grant from the append role (guarded, idempotent).

    Deliberately does **not** re-grant ``UPDATE``/``DELETE`` — a downgrade must never re-introduce
    mutation privileges on an evidentiary table.
    """
    fq = f"{schema}.{table}"
    return (
        "DO $$\n"
        "BEGIN\n"
        f"    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APPEND_ROLE}') THEN\n"
        f"        REVOKE INSERT, SELECT ON {fq} FROM {APPEND_ROLE};\n"
        "    END IF;\n"
        "END $$;"
    )
