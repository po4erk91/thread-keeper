"""Live cross-session dialog log tail window.

Opens a Terminal window that follows the shared dialog log so the user can
watch broadcast/whisper/question/answer traffic between concurrent claude
sessions in real time.
"""

import shlex
import sqlite3
import subprocess
import time
from pathlib import Path

from .._mcp import read_tool, write_tool
from ..config import TASK_LOG_DIR, DIALOG_LOG, SEMANTIC_AVAILABLE
from ..db import get_db, read_db
from ..helpers import fmt_age, q
from .. import identity
from ..identity import _ensure_session
from ..task_spool import (
    TASK_SPOOL_EXEC_MODE,
    ensure_task_spool_dir,
    touch_spool_file,
    write_spool_text,
)
from ..retrieval import retrieve_dialogs
from ..ingest import _ingest_all


@write_tool()
def open_dialog_window() -> str:
    """Open a Terminal window that tails the live cross-session signal log.

    Every broadcast/whisper/question/answer is appended to the log in real
    time; this lets the user see the dialog between concurrent claude
    sessions as it happens. The window stays open until you close it (it's
    a `tail -F`, no exit). Title: 'thread-keeper-dialog'."""
    try:
        ensure_task_spool_dir(TASK_LOG_DIR)
        touch_spool_file(DIALOG_LOG)
    except OSError as e:
        return f"ERR task_spool_unavailable={e}"
    script_path = TASK_LOG_DIR / "dialog-tail.command"
    tag = "thread-keeper-dialog"
    script = (
        "#!/bin/bash\n"
        f"printf '\\033]0;{tag}\\007'\n"
        "echo '── thread-keeper: live cross-session dialog ──'\n"
        f"echo '  log: {DIALOG_LOG}'\n"
        "echo '  ctrl+c or close window to stop'\n"
        "echo\n"
        f"exec tail -n 50 -F {shlex.quote(str(DIALOG_LOG))}\n"
    )
    try:
        write_spool_text(
            script_path,
            script,
            file_mode=TASK_SPOOL_EXEC_MODE,
        )
    except OSError as e:
        return f"ERR write_failed={e}"
    try:
        subprocess.Popen(["open", "-a", "Terminal", str(script_path)])
    except (FileNotFoundError, OSError) as e:
        return f"ERR open_failed={e}"
    return f"opened tailing {DIALOG_LOG}"


@read_tool()
def dialog_search(query: str, k: int = 5, role: str = "",
                  mode: str = "hybrid") -> str:
    """Search ingested Claude Code transcripts.

    mode='hybrid' (default) combines semantic and FTS5 keyword via RRF.
    mode='semantic' is pure cosine. mode='fts' is pure FTS5 keyword."""
    identity.ensure_session_started()
    with read_db() as conn:
        return _dialog_search_conn(conn, query=query, k=k, role=role, mode=mode)


def _dialog_search_conn(conn: sqlite3.Connection, *, query: str, k: int,
                        role: str, mode: str) -> str:
    """Connection-scoped implementation used by the read-only MCP wrapper."""
    role = role.strip().lower()
    mode = mode.strip().lower()
    if mode not in ("hybrid", "semantic", "fts"):
        return f"ERR bad_mode={mode} (use hybrid|semantic|fts)"
    hits = retrieve_dialogs(conn, query, k=k, role=role, mode=mode)
    if not hits:
        if not SEMANTIC_AVAILABLE and mode != "semantic":
            return _legacy_like_fallback(conn, query, k, role)
        return f"no_matches (mode={mode})"
    now = int(time.time())
    lines = []
    for hit in hits:
        snip = hit.content[:240].replace("\n", " ⏎ ")
        ago = fmt_age(now - hit.created_at)
        sess = (hit.session_id or "-")[:8]
        score = hit.display_score
        score_part = f"s={score:.2f} " if score is not None else ""
        lines.append(f"{hit.role}@{sess} {score_part}{ago}_ago {q(snip)}")
    return "\n".join(lines)


def _legacy_like_fallback(conn: sqlite3.Connection, query: str,
                          k: int, role: str) -> str:
    pattern = f"%{query}%"
    where = "content LIKE ?"
    params: list = [pattern]
    if role:
        where += " AND role = ?"
        params.append(role)
    params.append(k)
    rows = conn.execute(
        f"SELECT * FROM dialog_messages WHERE {where} "
        f"ORDER BY created_at DESC LIMIT ?", params,
    ).fetchall()
    if not rows:
        return "no_matches (no_embeddings, used LIKE)"
    now = int(time.time())
    return "\n".join(
        f"{r['role']}@{(r['session_id'] or '-')[:8]} "
        f"{fmt_age(now - r['created_at'])}_ago "
        f"{q(r['content'][:240].replace(chr(10), ' '))}"
        for r in rows
    )


@write_tool()
def ingest(max_msgs: int = 5000) -> str:
    """Ingest new transcripts. Initial and periodic passes run asynchronously
    in the daemon host; call manually for backfill or after a long absence."""
    conn = get_db()
    _ensure_session(conn)
    new_msgs, files = _ingest_all(conn, max_msgs=max_msgs)
    total = conn.execute(
        "SELECT COUNT(*) c FROM dialog_messages"
    ).fetchone()["c"]
    return f"ingested new={new_msgs} files_seen={files} total_indexed={total}"
