"""Explicit review of generated memory; acceptance never upgrades provenance."""
from __future__ import annotations

import inspect
import json
import time

from .._mcp import mcp, read_tool, write_tool
from ..db import read_db, run_write
from ..memory_access import PROPOSABLE_TOOLS, current_access, reviewing_proposal, proposal_scope_allowed
from ..project_scope import recall_where


@read_tool()
def memory_proposals(k: int = 20) -> str:
    """List pending generated-memory candidates for explicit review."""
    if current_access().child:
        return 'ERR memory_review_requires_foreground'
    where, params = recall_where('memory_proposals')
    where = f"({where}) OR memory_proposals.memory_scope='unresolved'"
    with read_db() as conn:
        rows = conn.execute(
            f'SELECT id, tool_name, arguments FROM memory_proposals '
            f"WHERE status='pending' AND ({where}) ORDER BY id LIMIT ?",
            (*params, max(1, min(int(k), 100))),
        ).fetchall()
    return '\n'.join(f"#{r['id']} {r['tool_name']} {r['arguments']}" for r in rows) or 'no_proposals'


@write_tool(destructive=True)
async def memory_proposal_decide(id: int, accept: bool = False) -> str:
    """Accept or reject one proposal. Only a foreground reviewer may decide.

    Acceptance runs the original validated tool and keeps new artifacts
    at observed authority. Failed or interrupted applications are not retried
    automatically because filesystem-backed writes may already have landed.
    """
    if current_access().child:
        return 'ERR memory_review_requires_foreground'
    where, params = recall_where('memory_proposals')
    where = f"({where}) OR memory_proposals.memory_scope='unresolved'"

    def claim(conn):
        row = conn.execute(
            f"SELECT * FROM memory_proposals WHERE id=? AND status='pending' AND ({where})",
            (id, *params),
        ).fetchone()
        if row is not None:
            conn.execute(
                'UPDATE memory_proposals SET status=?, decided_at=? WHERE id=?',
                ('applying' if accept else 'rejected', int(time.time()), id),
            )
        return dict(row) if row else None

    proposal = run_write('memory_proposal_claim', claim)
    if proposal is None:
        return 'ERR pending_proposal_not_found'
    if not accept:
        return f'ok proposal={id} status=rejected'
    name = proposal['tool_name']
    result = 'ERR proposal_tool_not_supported'
    if name in PROPOSABLE_TOOLS:
        try:
            arguments = json.loads(proposal['arguments'])
            with read_db() as conn:
                if not proposal_scope_allowed(conn, proposal, arguments):
                    raise ValueError('proposal_namespace_denied')
            tool = mcp._tool_manager._tools[name]
            with reviewing_proposal(proposal['child_cid']):
                result = tool.fn(**arguments)
                if inspect.isawaitable(result):
                    result = await result
        except Exception as exc:
            result = f'ERR proposal_application_failed={type(exc).__name__}'
    result = str(result)
    status = 'failed' if result.startswith('ERR') else 'accepted'
    run_write('memory_proposal_finish', lambda conn: conn.execute(
        'UPDATE memory_proposals SET status=?,result=? WHERE id=?', (status, result, id),
    ))
    return f'ok proposal={id} status={status}\n{result}'
