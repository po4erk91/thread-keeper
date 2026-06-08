"""Dialectic miner — mechanical capture of user replies into the
dialectic_observations buffer. No LLM, no spawn: deterministic and lossless.

For each user-role dialog_message since the last pass it stores the verbatim
quote plus the most-recent preceding assistant turn as context. The
dialectic_validator child later turns this buffer into claims. Session
filtering mirrors extract_recent so only the REAL user's turns are captured
(internal-prompt sessions + spawned-child sessions are excluded)."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time

from .config import DIALECTIC_MINE_INTERVAL_S
from .db import get_db
from . import identity
from .identity import _ensure_session, _emit

logger = logging.getLogger(__name__)

_started = False
_CONTEXT_MAX = 600


def _last_mine_ts(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT target FROM events WHERE kind='dialectic_mine_pass' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row or not row["target"]:
        return 0
    try:
        return int(row["target"])
    except (ValueError, TypeError):
        return 0


def _record_pass(conn: sqlite3.Connection, ts: int, outcome: str) -> None:
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, 'dialectic_mine_pass', ?, ?, ?)",
            (identity._session_id or "", str(ts), outcome[:300], int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("dialectic_miner: record_pass failed", exc_info=True)


def _preceding_context(conn: sqlite3.Connection, session_id: str,
                       before_ts: int) -> str:
    """Most recent assistant turn in this session before before_ts."""
    row = conn.execute(
        "SELECT content FROM dialog_messages WHERE session_id=? "
        "AND role='assistant' AND created_at <= ? "
        "ORDER BY created_at DESC LIMIT 1",
        (session_id, before_ts),
    ).fetchone()
    if not row or not row["content"]:
        return ""
    return row["content"][:_CONTEXT_MAX]


def run_mine_pass(force: bool = False) -> str:
    """Capture new user replies since the cursor. Returns
    'ok captured=N skipped=M' / 'no_user_dialog' / 'disabled'."""
    if DIALECTIC_MINE_INTERVAL_S <= 0 and not force:
        return "disabled"
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    cursor = _last_mine_ts(conn)

    from .shadow_review import _INTERNAL_PROMPT_PREFIXES
    sess_prefix_clauses = " OR ".join(
        ["substr(content, 1, ?) = ?"] * len(_INTERNAL_PROMPT_PREFIXES)
    )
    sess_prefix_params: list = []
    for p in _INTERNAL_PROMPT_PREFIXES:
        sess_prefix_params.extend([len(p), p])

    rows = conn.execute(
        "SELECT uuid, session_id, content, created_at FROM dialog_messages "
        "WHERE role='user' AND created_at >= ? "
        "AND content NOT LIKE '[tool_result]%' AND content NOT LIKE '[Image%' "
        "AND length(content) >= 1 "
        "AND session_id NOT IN ("
        "  SELECT DISTINCT session_id FROM dialog_messages "
        f"  WHERE role='user' AND ({sess_prefix_clauses})"
        ") "
        "AND session_id NOT IN ("
        "  SELECT spawned_cid FROM tasks WHERE spawned_cid IS NOT NULL"
        ") "
        "ORDER BY created_at ASC",
        (cursor, *sess_prefix_params),
    ).fetchall()

    if not rows:
        _record_pass(conn, now, "no_user_dialog")
        return "no_user_dialog"

    captured = skipped = 0
    max_ts = cursor
    for r in rows:
        max_ts = max(max_ts, r["created_at"])
        ctx = _preceding_context(conn, r["session_id"] or "", r["created_at"])
        cur = conn.execute(
            "INSERT OR IGNORE INTO dialectic_observations "
            "(dialog_uuid, user_quote, context, source_cid, status, created_at) "
            "VALUES (?,?,?,?, 'pending', ?)",
            (r["uuid"], r["content"], ctx, r["session_id"], now),
        )
        if cur.rowcount:
            captured += 1
        else:
            skipped += 1
    _emit(conn, "dialectic_mine_capture", summary=f"captured={captured}")
    conn.commit()
    _record_pass(conn, max_ts, f"ok captured={captured} skipped={skipped}")
    return f"ok captured={captured} skipped={skipped}"


def _serve_loop() -> None:
    while True:
        try:
            run_mine_pass()
        except Exception:
            logger.debug("dialectic_miner tick failed", exc_info=True)
        time.sleep(DIALECTIC_MINE_INTERVAL_S)


def start_dialectic_miner_daemon() -> None:
    """Idempotent. Mechanical capture needs no embeddings, so it is gated only
    by BACKGROUND_DAEMONS_ALLOWED (not SEMANTIC_AVAILABLE)."""
    global _started
    if _started:
        return
    if DIALECTIC_MINE_INTERVAL_S <= 0:
        return
    from .config import BACKGROUND_DAEMONS_ALLOWED
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    t = threading.Thread(target=_serve_loop, name="dialectic_miner", daemon=True)
    t.start()
    _started = True
