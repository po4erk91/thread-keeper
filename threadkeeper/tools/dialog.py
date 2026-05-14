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

from .._mcp import mcp
from ..config import TASK_LOG_DIR, DIALOG_LOG, SEMANTIC_AVAILABLE
from ..db import get_db
from ..helpers import fmt_age, q
from ..identity import _ensure_session
from ..embeddings import _dialog_cosine_search, _fts_search, _rrf_combine
from ..ingest import _ingest_all


@mcp.tool()
def open_dialog_window() -> str:
    """Open a Terminal window that tails the live cross-session signal log.

    Every broadcast/whisper/question/answer is appended to the log in real
    time; this lets the user see the dialog between concurrent claude
    sessions as it happens. The window stays open until you close it (it's
    a `tail -F`, no exit). Title: 'thread-keeper-dialog'."""
    TASK_LOG_DIR.mkdir(parents=True, exist_ok=True)
    DIALOG_LOG.touch(exist_ok=True)
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
    script_path.write_text(script)
    script_path.chmod(0o755)
    try:
        subprocess.Popen(["open", "-a", "Terminal", str(script_path)])
    except (FileNotFoundError, OSError) as e:
        return f"ERR open_failed={e}"
    return f"opened tailing {DIALOG_LOG}"


@mcp.tool()
def dialog_search(query: str, k: int = 5, role: str = "",
                  mode: str = "hybrid") -> str:
    """Search ingested Claude Code transcripts.

    mode='hybrid' (default) combines semantic and FTS5 keyword via RRF.
    mode='semantic' is pure cosine. mode='fts' is pure FTS5 keyword."""
    conn = get_db()
    _ensure_session(conn)
    role = role.strip().lower()
    mode = mode.strip().lower()
    if mode not in ("hybrid", "semantic", "fts"):
        return f"ERR bad_mode={mode} (use hybrid|semantic|fts)"
    over_fetch = max(k * 5, 20)
    sem_hits: list[dict] = []
    fts_hits: list[dict] = []
    if mode in ("hybrid", "semantic") and SEMANTIC_AVAILABLE:
        sem_hits = _dialog_cosine_search(conn, query, over_fetch)
    if mode in ("hybrid", "fts"):
        fts_hits = _fts_search(conn, query, over_fetch)
    if role:
        sem_hits = [h for h in sem_hits if h.get("role") == role]
        fts_hits = [h for h in fts_hits if h.get("role") == role]
    if mode == "hybrid":
        hits = _rrf_combine([sem_hits, fts_hits], top_n=k)
    elif mode == "semantic":
        hits = sem_hits[:k]
    else:
        hits = fts_hits[:k]
    if not hits:
        if not SEMANTIC_AVAILABLE and not fts_hits:
            return _legacy_like_fallback(conn, query, k, role)
        return f"no_matches (mode={mode})"
    now = int(time.time())
    lines = []
    for h in hits:
        snip = h["content"][:240].replace("\n", " ⏎ ")
        ago = fmt_age(now - h["created_at"])
        sess = (h["session_id"] or "-")[:8]
        score_part = f"s={h['score']:.2f} " if h.get("score") is not None else ""
        lines.append(f"{h['role']}@{sess} {score_part}{ago}_ago {q(snip)}")
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


@mcp.tool()
def ingest(max_msgs: int = 5000) -> str:
    """Ingest new Claude Code transcripts. Auto-runs on session start; call
    manually for backfill or after long absence."""
    conn = get_db()
    _ensure_session(conn)
    new_msgs, files = _ingest_all(conn, max_msgs=max_msgs)
    total = conn.execute(
        "SELECT COUNT(*) c FROM dialog_messages"
    ).fetchone()["c"]
    return f"ingested new={new_msgs} files_seen={files} total_indexed={total}"
