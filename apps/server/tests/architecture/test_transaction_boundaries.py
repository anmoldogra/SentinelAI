"""ADR-0005 enforcement: services never own the transaction.

The UnitOfWork is opened and committed at the entrypoint boundary — an HTTP router or a worker
job wrapper — never inside a service. This test AST-scans every module's ``service.py`` and fails
on any ``.commit(...)`` or ``.rollback(...)`` call, so the convention this repository just
migrated to (IC-012) cannot silently regress: a reintroduced service-level commit fails CI with
the exact file, line, and call spelled out.

Static and dependency-free: no import of the scanned modules, no database, no fixtures.
"""

from __future__ import annotations

import ast
from pathlib import Path

_MODULES = Path(__file__).resolve().parents[2] / "src" / "sentinelai" / "modules"
_FORBIDDEN = frozenset({"commit", "rollback"})


def _service_files() -> list[Path]:
    return sorted(_MODULES.glob("*/service.py"))


def _forbidden_calls(path: Path) -> list[str]:
    """Every ``<anything>.commit()`` / ``<anything>.rollback()`` call site in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    violations: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _FORBIDDEN
        ):
            violations.append(f"{path.as_posix()}:{node.lineno} calls .{node.func.attr}()")
    return violations


def test_every_module_has_a_service_to_scan() -> None:
    """Guards the scan below from silently passing on an empty file list."""
    assert len(_service_files()) >= 8  # one per domain module today


def test_no_service_commits_or_rolls_back_the_unit_of_work() -> None:
    """ADR-0005 §2: services mutate through the injected UoW and raise on failure — the
    entrypoint commits once on success. A service that commits re-takes ownership of the
    transaction and breaks the single-commit-per-request guarantee."""
    violations = [v for path in _service_files() for v in _forbidden_calls(path)]
    assert violations == [], (
        "ADR-0005 violation — services must never commit/rollback:\n" + "\n".join(violations)
    )
