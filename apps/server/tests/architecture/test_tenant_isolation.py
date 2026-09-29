"""ADR-0014 enforcement: the tenant context stays reserved, and stays inert.

ADR-0014 decides that SentinelAI is isolated by *deployment* — one agency, one database cluster,
one KMS root key, one network zone — and that no shared-infrastructure tier exists. The correct
amount of application code for that decision is **zero**, which makes it the rare decision that
can only regress by someone starting to write the code it declined.

That is exactly what this file catches. It fails if any source file outside ``platform/config.py``
starts reading or setting the reserved tenant context, and if any migration introduces row-level
security or a ``current_tenant`` connection setting — the two shapes a shared tier would take.

**This is not a prohibition on ever having multi-tenancy.** It is the mechanism that makes a shared
tier arrive as a superseding ADR (``engineering-governance.md`` §2) instead of as a merged pull
request, which is what ``security-architecture.md`` §40 exists to prevent. A future ADR that
introduces one deletes this file as part of the same change.

Static and dependency-free apart from one import of ``platform.config``, which exists to assert the
ContextVar's declared default rather than to exercise any behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path

from sentinelai.platform.config import tenant_id

_SRC = Path(__file__).resolve().parents[2] / "src" / "sentinelai"

# The one file ADR-0014 §3 permits to name the reserved context — it is where the context is
# declared, and its comment is where the decision is explained to the next reader.
_DECLARATION = _SRC / "platform" / "config.py"

# ``\btenant_id\b`` matches the identifier, not prose: a comment saying "cross-tenant" is a
# discussion of the decision, which is welcome. A reference to the symbol is the regression.
_CONTEXT_REF = re.compile(r"\btenant_id\b")

# The two shapes a shared tier takes at the database layer. ADR-0014 "Alternatives considered" A.2
# and A.3 explain why both are weaker here than they look.
_SHARED_TIER_DDL = re.compile(r"row\s+level\s+security|current_tenant", re.IGNORECASE)

_SUPERSEDE = (
    "ADR-0014 is Accepted: isolation is delivered by the deployment boundary and the tenant "
    "context is permanently None. A shared-infrastructure tier requires a SUPERSEDING ADR "
    "(engineering-governance.md §2) that first amends database-design.md, api-design.md and "
    "event-driven-architecture.md — none of which defines a tenant column, header or envelope "
    "field today. Do not wire it here first."
)


def _source_files() -> list[Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _migration_files() -> list[Path]:
    return sorted(p for p in _SRC.rglob("migrations/versions/*.py") if p.name != "__init__.py")


def test_the_scans_below_have_something_to_scan() -> None:
    """Guards both scans from silently passing on an empty file list."""
    assert len(_source_files()) >= 200  # 247 today
    assert len(_migration_files()) >= 40  # 47 today, across nine module histories
    assert _DECLARATION.is_file()


def test_the_reserved_tenant_context_defaults_to_none() -> None:
    """ADR-0014 §3: the extension point exists; it is never populated."""
    assert tenant_id.get() is None


def test_no_source_file_outside_config_references_the_tenant_context() -> None:
    """ADR-0014 §3: nothing sets it, nothing reads it.

    A reference appearing anywhere else means tenant scoping is being threaded through the
    application — a session hook, a repository predicate, a middleware binding — which is the
    decision this ADR declined.
    """
    violations = [
        f"{path.relative_to(_SRC).as_posix()}:{lineno}: {line.strip()}"
        for path in _source_files()
        if path != _DECLARATION
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _CONTEXT_REF.search(line)
    ]
    assert violations == [], (
        "ADR-0014 §3 violation — the tenant context must stay inert:\n"
        + "\n".join(violations)
        + f"\n\n{_SUPERSEDE}"
    )


def test_no_migration_introduces_row_level_security_or_a_tenant_setting() -> None:
    """ADR-0014 §2: the shared tier has no migration path that does not start with an ADR.

    RLS is not forbidden forever — ADR-0014 records it as defence-in-depth *if* a shared tier is
    ever introduced. It is forbidden as the first artifact of one, because a policy added ahead of
    the decision is isolation nobody reviewed.
    """
    violations = [
        f"{path.relative_to(_SRC).as_posix()}:{lineno}: {line.strip()}"
        for path in _migration_files()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _SHARED_TIER_DDL.search(line)
    ]
    assert violations == [], (
        "ADR-0014 §2 violation — shared-tier DDL in a migration:\n"
        + "\n".join(violations)
        + f"\n\n{_SUPERSEDE}"
    )
