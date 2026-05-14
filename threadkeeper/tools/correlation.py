"""Task↔signals correlation MCP tools.

Extracted from server.py. Lets a session manually attach a signal to a
spawned task (when auto-tagging at emit-time missed it) and replay a
spawned task as a chronological thread of signals plus relevant notes.
"""

import sqlite3
import time

from .._mcp import mcp
from ..db import get_db
from ..helpers import fmt_age, q
from ..identity import _ensure_session


@mcp.tool()
def tag_signal(signal_id: int, task_id: str) -> str:
    """Manually attach a signal to a task. Useful when retroactively building
    a task-thread (auto-tagging happens at signal-emit time when the cid
    matches a known spawned_cid)."""
    conn = get_db()
    _ensure_session(conn)
    if not conn.execute(
        "SELECT 1 FROM signals WHERE id=?", (int(signal_id),)
    ).fetchone():
        return f"ERR signal_not_found={signal_id}"
    if not conn.execute(
        "SELECT 1 FROM tasks WHERE id=?", (task_id.strip(),)
    ).fetchone():
        return f"ERR task_not_found={task_id}"
    conn.execute(
        "UPDATE signals SET task_id=? WHERE id=?",
        (task_id.strip(), int(signal_id)),
    )
    conn.commit()
    return f"ok signal={signal_id} → task={task_id}"


@mcp.tool()
def task_thread(task_id: str, include_notes: bool = True,
                k: int = 50) -> str:
    """Replay a spawned task as a chronological thread: every signal tagged
    with the task_id (or to/from the task's spawned_cid), plus optionally
    notes added during the task window."""
    conn = get_db()
    t = conn.execute(
        "SELECT pid, parent_cid, spawned_cid, prompt, started_at, ended_at "
        "FROM tasks WHERE id=?", (task_id.strip(),)
    ).fetchone()
    if not t:
        return f"ERR task_not_found={task_id}"
    end_t = t["ended_at"] or int(time.time())
    start_t = t["started_at"]
    spawned = t["spawned_cid"]
    parent = t["parent_cid"]
    # signals: explicitly tagged OR (to/from spawned_cid within window)
    query = """
        SELECT id, from_cid, to_cid, kind, content, created_at
        FROM signals
        WHERE (task_id = ?)
           OR (created_at BETWEEN ? AND ?
               AND ((from_cid = ? OR to_cid = ?)
                    OR (from_cid = ? AND to_cid = ?)
                    OR (from_cid = ? AND to_cid = ?)))
        ORDER BY created_at ASC LIMIT ?
    """
    sigs = conn.execute(
        query,
        (task_id.strip(), start_t - 5, end_t + 60,
         spawned or "_none_", spawned or "_none_",
         spawned or "_none_", parent or "_none_",
         parent or "_none_", spawned or "_none_",
         max(1, int(k))),
    ).fetchall()
    notes_rows = []
    if include_notes and spawned:
        notes_rows = conn.execute(
            "SELECT id, thread_id, kind, content, created_at "
            "FROM notes WHERE session_id LIKE ? AND created_at BETWEEN ? AND ? "
            "ORDER BY created_at ASC",
            (f"%", start_t - 5, end_t + 60),
        ).fetchall()
        # narrow notes to ones actually authored by spawned (cid not session_id)
        # session_id in notes is mcp-internal; we don't have direct cid match.
        # Best heuristic: include notes whose content references the task or
        # whose thread last_touched within window.
        notes_rows = [
            n for n in notes_rows
            if (task_id.strip() in (n["content"] or "")
                or n["thread_id"] in {None, "Tcd1"})
        ][:10]
    lines = [
        f"task={task_id} parent={(parent or '-')[:8]} child={(spawned or '-')[:8]} "
        f"started={fmt_age(int(time.time()) - start_t)}_ago "
        f"{'ended' if t['ended_at'] else 'open'}"
    ]
    if not sigs and not notes_rows:
        lines.append("  (no signals or notes in window)")
        return "\n".join(lines)
    for s in sigs:
        ago = fmt_age(int(time.time()) - s["created_at"])
        snip = (s["content"] or "")[:140].replace("\n", " ")
        scope = "*" if s["to_cid"] is None else "→" + (s["to_cid"][:8])
        lines.append(
            f"  sig#{s['id']} {scope} from={s['from_cid'][:8]} "
            f"+{s['kind']} {ago}_ago {q(snip)}"
        )
    for n in notes_rows:
        ago = fmt_age(int(time.time()) - n["created_at"])
        snip = (n["content"] or "")[:140].replace("\n", " ")
        lines.append(
            f"  note#{n['id']} thread={n['thread_id'] or '-'} "
            f"+{n['kind']} {ago}_ago {q(snip)}"
        )
    return "\n".join(lines)
