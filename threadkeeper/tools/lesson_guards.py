"""Review, verify and operate bounded server-side lesson guards."""
from __future__ import annotations

from .._mcp import read_tool, write_tool


@write_tool()
def lesson_guard_manage(action: str, slug: str, expected_revision: int = 0,
                        trigger: dict | None = None, adapter: str = "mcp",
                        event: str = "before_tool", allow_payload: dict | None = None,
                        deny_payload: dict | None = None, owner_confirm: bool = False) -> str:
    """Manage an owner-reviewed guard for a repeatedly violated lesson.

    propose: trigger={tool,field,operator,value}; operators are equals,
    command_prefix (literal shell-token prefix), path_glob (relative pattern).
    verify: provide complete allow_payload and deny_payload for the target tool.
    activate: requires the verified revision and explicit owner_confirm.
    escape: owner-confirm one exact deny_payload once, for 60 seconds.
    retire/rollback: owner-confirm; rollback restores a proposed prior contract.
    Only adapter=mcp,event=before_tool is supported. Native CLI hooks and shell
    tools outside this server are not covered. Payload samples are not retained.
    """
    from ..lesson_guards import manage
    return manage(action=action, slug=slug, expected_revision=expected_revision,
                  trigger=trigger, adapter=adapter, event=event,
                  allow_payload=allow_payload, deny_payload=deny_payload,
                  owner_confirm=owner_confirm)


@read_tool()
def lesson_guard_status(slug: str = "") -> str:
    """List guard lifecycle, verification and safe failure codes without samples."""
    from ..db import read_db
    from ..lesson_guards import states
    with read_db() as conn:
        records = states(conn)
    lines = ["adapter=mcp event=before_tool native_cli_hooks=unsupported"]
    for key, row in records.items():
        if slug and key != slug:
            continue
        lines.append(
            f"{key} revision={row['revision']} state={row['effective_state']} "
            f"verified_at={row['verified_at'] or 0} failure={row['effective_failure'] or '-'} "
            f"contract={row['trigger_json']}"
        )
    return "\n".join(lines)
