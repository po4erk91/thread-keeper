"""Inspectable project-memory namespace state."""
from __future__ import annotations

from .._mcp import read_tool
from ..db import read_db, project_scope_migration_report
from ..project_scope import resolve_project_namespace


@read_tool()
def memory_scope_status() -> str:
    """Show the current path-free namespace and legacy migration accounting.

    Unresolved rows are excluded from automatic recall until an operator
    classifies them, so this gives a visible audit surface after upgrading.
    """
    namespace = resolve_project_namespace()
    lines = [
        "memory_scope "
        f"project={namespace.project_id or 'unresolved'} source={namespace.source} "
        "default=project+global+shared cross_project=explicit",
    ]
    with read_db() as conn:
        report = project_scope_migration_report(conn)
    if not report:
        return "\n".join(lines + ["migration_report=not_run"])
    lines.append("migration_report")
    for row in report:
        lines.append(
            f"  {row['table_name']} project={row['project_rows']} "
            f"global={row['global_rows']} shared={row['shared_rows']} "
            f"unresolved={row['unresolved_rows']}"
        )
    return "\n".join(lines)
