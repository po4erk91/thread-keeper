"""Core-memory MCP tools.

Small key/value store always surfaced in `brief()`, sorted by priority
DESC. Designed as the 'what new-claude must know' surface — not a note
store. Entries are capped at 1KB and a soft hint at 20 entries total.
"""

import sqlite3
import time

from .._mcp import read_tool, write_tool
from ..db import get_db
from ..helpers import fmt_age, q
from ..identity import _ensure_session, _emit
from ..project_scope import (
    format_provenance,
    recall_where,
    resolve_write_scope,
)


CORE_MAX_BYTES = 1024
CORE_MAX_ENTRIES_HINT = 20
CORE_PRIORITY_MIN = 0
CORE_PRIORITY_MAX = 100


@write_tool(idempotent=True)
def core_set(key: str, content: str, priority: int = 50,
             memory_scope: str = "project") -> str:
    """Upsert a core-memory entry. ALWAYS shown in brief, sorted by priority DESC.

    Use sparingly — this is the 'what new-claude must know' surface, not a
    note store. Good: 'project_root=/Users/.../ai-memory'. Bad: 'today we
    tried X'. `priority` 0-100 (higher = shown first). `content` capped 1KB."""
    conn = get_db()
    _ensure_session(conn)
    key = key.strip()
    if not key:
        return "ERR empty_key"
    if len(key) > 64:
        return "ERR key_too_long max=64"
    if not content.strip():
        return "ERR empty_content"
    if len(content.encode("utf-8")) > CORE_MAX_BYTES:
        return f"ERR content_too_large max={CORE_MAX_BYTES}B"
    if not (CORE_PRIORITY_MIN <= priority <= CORE_PRIORITY_MAX):
        return f"ERR priority_out_of_range min={CORE_PRIORITY_MIN} max={CORE_PRIORITY_MAX}"
    try:
        scope, project_id = resolve_write_scope(memory_scope)
    except ValueError as exc:
        return f"ERR {exc}"
    storage_key = f"{scope}:{project_id or '-'}:{key}"
    now = int(time.time())
    conn.execute(
        "INSERT INTO core_memory (key, key_name, content, priority, updated_at, "
        "project_id, memory_scope) VALUES (?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
        "key_name=excluded.key_name, "
        "content=excluded.content, priority=excluded.priority, "
        "updated_at=excluded.updated_at, project_id=excluded.project_id, "
        "memory_scope=excluded.memory_scope",
        (storage_key, key, content, priority, now, project_id, scope),
    )
    _emit(conn, "core_set", target=key, summary=f"P{priority} scope={scope} {content[:80]}")
    conn.commit()
    n = conn.execute("SELECT COUNT(*) c FROM core_memory").fetchone()["c"]
    warn = f" warn=over_hint({n}/{CORE_MAX_ENTRIES_HINT})" if n > CORE_MAX_ENTRIES_HINT else ""
    return f"ok n={n}{warn}"


@write_tool(destructive=True, idempotent=True)
def core_remove(key: str, memory_scope: str = "project") -> str:
    """Delete a core-memory entry by key."""
    conn = get_db()
    _ensure_session(conn)
    try:
        scope, project_id = resolve_write_scope(memory_scope)
    except ValueError as exc:
        return f"ERR {exc}"
    storage_key = f"{scope}:{project_id or '-'}:{key.strip()}"
    cur = conn.execute("DELETE FROM core_memory WHERE key=?", (storage_key,))
    if cur.rowcount == 0:
        return f"ERR not_found={key}"
    _emit(conn, "core_remove", target=key)
    conn.commit()
    return "ok"


@read_tool()
def core_list(cross_project: bool = False) -> str:
    """List all core-memory entries, ordered by priority DESC then key."""
    conn = get_db()
    where, params = recall_where("core_memory", cross_project=cross_project)
    rows = conn.execute(
        "SELECT key, key_name, content, priority, updated_at, project_id, memory_scope "
        f"FROM core_memory WHERE {where} ORDER BY priority DESC, key_name ASC", params
    ).fetchall()
    if not rows:
        return "empty"
    now = int(time.time())
    lines = []
    for r in rows:
        snip = r["content"][:120].replace("\n", " ")
        if len(r["content"]) > 120:
            snip += "…"
        lines.append(
            f"[P{r['priority']}] {r['key_name'] or r['key']}: {q(snip)} "
            f"{format_provenance(r['memory_scope'], r['project_id'])} "
            f"upd={fmt_age(now - r['updated_at'])}_ago"
        )
    return "\n".join(lines)


@read_tool()
def core_get(key: str, memory_scope: str = "project") -> str:
    """Return the full content of a single core-memory entry."""
    conn = get_db()
    try:
        scope, project_id = resolve_write_scope(memory_scope)
    except ValueError as exc:
        return f"ERR {exc}"
    storage_key = f"{scope}:{project_id or '-'}:{key.strip()}"
    row = conn.execute(
        "SELECT key, key_name, content, priority, updated_at, project_id, memory_scope "
        "FROM core_memory WHERE key=?",
        (storage_key,),
    ).fetchone()
    if not row:
        return f"ERR not_found={key}"
    now = int(time.time())
    return (
        f"key={row['key_name'] or row['key']} P{row['priority']} "
        f"{format_provenance(row['memory_scope'], row['project_id'])} "
        f"upd={fmt_age(now - row['updated_at'])}_ago\n"
        f"{row['content']}"
    )
