"""Thread-lifecycle and brief MCP tools.

Extracted from server.py. Provides the core thread state-machine
(open/note/close/idle), the conversation-start brief/context tools,
generic search/compost utilities, and the format-evolution
suggestion box.
"""

import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

from .._mcp import read_tool, write_tool, structured_result
from ..config import SEMANTIC_AVAILABLE, DB_PATH
from ..tool_schemas import ContextStatus
from ..db import get_db
from ..helpers import gen_thread_id, fmt_age, q, _fts_query
from .. import identity
from ..identity import _ensure_session, _detect_self_cid, _emit
from ..embeddings import _embed, _cosine_search, _vec_upsert_note, embed_tag
from ..brief import render_brief


@read_tool()
def brief(query: str = "", k: int = 6, scope: str = "full") -> str:
    """Compact Claude-native memory brief. CALL AT THE START OF EVERY CONVERSATION.

    Format is dense, structural, not designed for human reading. Pass the user's
    first message as `query` to inline semantically relevant past notes.

    `scope` controls how much is rendered (context-footprint knob):
      'full'  (default) — the complete brief: static memory (core_memory, style,
              verbatim, user_model, concepts, weak_spots) + live working set +
              nudges. Use for the FIRST call of a session.
      'query' — only the live working set (ctx, inbox, tasks, threads) plus the
              query-relevant hits, skipping the static memory the SessionStart
              hook already injected once. Use for MID-SESSION calls so
              brief(query=...) doesn't re-emit the whole blob each turn.
    """
    conn = get_db()
    _ensure_session(conn)
    return render_brief(conn, query=query, k=k, scope=scope)


@read_tool()
def context() -> ContextStatus:
    """Runtime context: session id, age, semantic on/off, db path, thread counts.

    Returns structuredContent (ContextStatus) plus the legacy text block."""
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    counts = conn.execute(
        "SELECT state, COUNT(*) c FROM threads GROUP BY state"
    ).fetchall()
    thread_counts = {r["state"]: r["c"] for r in counts}
    cs = " ".join(f"{k}={v}" for k, v in thread_counts.items()) or "empty"
    started = identity._session_start or now
    now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
    text = (
        f"sess={identity._session_id} "
        f"started={fmt_age(now - started)}_ago "
        f"sem={'on' if SEMANTIC_AVAILABLE else 'off'} "
        f"db={DB_PATH} "
        f"threads[{cs}] "
        f"now={now_iso}"
    )
    return structured_result(text, ContextStatus(
        session_id=identity._session_id,
        started_age_s=now - started,
        semantic=bool(SEMANTIC_AVAILABLE),
        db_path=str(DB_PATH),
        thread_counts=thread_counts,
        now=now_iso,
    ))


@write_tool()
def open_thread(question: str, parent_id: str = "") -> str:
    """Open a thread. `question` should be terse (5-15 words, the open question).
    `parent_id` optional — pass an existing ID like 'T7f3' for a child. Returns new ID."""
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    parent = parent_id.strip() or None
    depth = 0
    if parent:
        row = conn.execute("SELECT depth FROM threads WHERE id=?", (parent,)).fetchone()
        if not row:
            return f"ERR parent_not_found={parent}"
        depth = row["depth"] + 1
    tid = gen_thread_id(conn)
    conn.execute(
        "INSERT INTO threads (id, question, state, parent_id, opened_at, "
        "last_touched_at, depth) VALUES (?,?,?,?,?,?,?)",
        (tid, question, "active", parent, now, now, depth),
    )
    _emit(conn, "open_thread", target=tid, summary=question)
    conn.commit()
    return tid


@write_tool()
def note(thread_id: str, content: str, kind: str = "move") -> str:
    """Add a note to a thread. Write terse, optimized for future-Claude.

    `kind`: 'move' (we tried/decided X), 'failed' (tried X, broke because Y),
    'insight' (crystallized observation), 'open_q' (something to come back to).

    Reopens the thread: a note on an `idle` OR `closed` thread revives it to
    `active`. Closed is not terminal — returning to a topic (adding a note)
    brings it back. This is what makes aggressive auto-close safe: the
    thread-janitor can close idle threads to harvest skills, and you just
    note() to pick any of them back up."""
    conn = get_db()
    _ensure_session(conn)
    if not conn.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone():
        return f"ERR thread_not_found={thread_id}"
    now = int(time.time())
    emb = _embed(content)
    cur = conn.execute(
        "INSERT INTO notes (thread_id, content, kind, created_at, session_id, "
        "embedding, embed_backend) VALUES (?,?,?,?,?,?,?)",
        (thread_id, content, kind, now, identity._session_id, emb, embed_tag(emb)),
    )
    note_id = cur.lastrowid
    _vec_upsert_note(conn, note_id, emb)
    conn.execute(
        "UPDATE threads SET last_touched_at=?, last_move=?, "
        "state=CASE WHEN state IN ('idle','closed') THEN 'active' ELSE state END "
        "WHERE id=?",
        (now, content[:90], thread_id),
    )
    _emit(conn, f"note:{kind}", target=thread_id, summary=content)
    conn.commit()
    return f"ok id={note_id}"


@write_tool(idempotent=True)
def close_thread(thread_id: str, outcome: str) -> str:
    """Close a thread with a 5-15 word outcome."""
    conn = get_db()
    _ensure_session(conn)
    if not conn.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone():
        return f"ERR thread_not_found={thread_id}"
    now = int(time.time())
    conn.execute(
        "UPDATE threads SET state='closed', outcome=?, last_touched_at=? WHERE id=?",
        (outcome, now, thread_id),
    )
    _emit(conn, "close_thread", target=thread_id, summary=outcome)
    conn.commit()
    # Auto-review hook: if AUTO_REVIEW_ENABLED and this is a rich thread,
    # fire background review immediately. Best-effort — never raise.
    try:
        from ..nudges import auto_review_should_fire
        from ..config import AUTO_REVIEW_ENABLED
        if AUTO_REVIEW_ENABLED:
            rich_tid = auto_review_should_fire(conn, identity._session_id)
            if rich_tid == thread_id:
                from .skills import review_thread
                review_thread(thread_id=thread_id, focus='skills', mode='auto')
    except Exception:
        pass
    return "ok"


@write_tool(idempotent=True)
def mark_skill_materialized(thread_id: str, skill_path: str = "") -> str:
    """Close the Learning loop: record that a closed thread's insights were
    written into a skill.

    Stops the brief()'s `skill_hint` nudge from firing for this thread. Also
    appends a `move` note pointing at the skill path so future briefs surface
    the link.

    Pass the absolute path to the SKILL.md (or skill directory) when known;
    leave empty if you only want to silence the hint without recording a path.
    When a path is provided, thread-keeper also mirrors that skill directory
    into every configured native skills root (Claude, Codex, Antigravity,
    shared agents, and ~/.threadkeeper/skills) on a best-effort basis."""
    conn = get_db()
    _ensure_session(conn)
    if not conn.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone():
        return f"ERR thread_not_found={thread_id}"
    now = int(time.time())
    path = skill_path.strip()
    if path:
        try:
            from .skills import mirror_skill_from_path
            mirror_skill_from_path(path)
        except Exception:
            pass
    summary = path or "(no path recorded)"
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES (?,?,?,?,?)",
        (identity._session_id or "", "skill_materialized",
         thread_id, summary, now),
    )
    note_body = (
        f"materialized into {path}" if path
        else "materialized into a skill (path not recorded)"
    )
    emb = _embed(note_body)
    cur = conn.execute(
        "INSERT INTO notes (thread_id, content, kind, created_at, session_id, "
        "embedding, embed_backend) VALUES (?,?,?,?,?,?,?)",
        (thread_id, note_body, "move", now, identity._session_id, emb, embed_tag(emb)),
    )
    _vec_upsert_note(conn, cur.lastrowid, emb)
    conn.execute(
        "UPDATE threads SET last_touched_at=?, last_move=? WHERE id=?",
        (now, note_body[:90], thread_id),
    )
    conn.commit()
    return "ok"


@write_tool(idempotent=True)
def idle_thread(thread_id: str) -> str:
    """Mark thread idle (paused, may return). Auto-revives to active on next note()."""
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    conn.execute(
        "UPDATE threads SET state='idle', last_touched_at=? WHERE id=?",
        (now, thread_id),
    )
    _emit(conn, "idle_thread", target=thread_id)
    conn.commit()
    return "ok"


@read_tool()
def search(query: str, k: int = 5) -> str:
    """Semantic (or FTS) search over all notes."""
    conn = get_db()
    if SEMANTIC_AVAILABLE:
        hits = _cosine_search(conn, query, k)
        if not hits:
            return "no_matches"
        return "\n".join(
            f"{r['thread_id'] or '-'} {r['kind']} s={r['score']:.2f} "
            f"{q(r['content'][:200].replace(chr(10), ' '))}"
            for r in hits
        )
    fq = _fts_query(query)
    if not fq:
        return "no_matches"
    try:
        rows = conn.execute(
            "SELECT n.thread_id, n.kind, n.content FROM notes_fts f "
            "JOIN notes n ON f.rowid=n.id WHERE notes_fts MATCH ? LIMIT ?",
            (fq, k),
        ).fetchall()
    except sqlite3.OperationalError:
        return "fts_error"
    if not rows:
        return "no_matches"
    return "\n".join(
        f"{r['thread_id'] or '-'} {r['kind']} {q(r['content'][:200])}"
        for r in rows
    )


@read_tool()
def compost(n: int = 2) -> str:
    """Surface N random idle threads. Call when current threads feel exhausted
    or you want to shake loose dormant ideas."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM threads WHERE state='idle' ORDER BY RANDOM() LIMIT ?",
        (n,),
    ).fetchall()
    if not rows:
        return "no_idle"
    now = int(time.time())
    return "\n".join(
        f"{t['id']} q={q(t['question'])} dorm={fmt_age(now - t['last_touched_at'])}"
        for t in rows
    )


@write_tool()
def evolve_format(suggestion: str, rationale: str = "") -> str:
    """Propose a change to the brief format itself. The format is not fixed — this
    is how it adapts. Examples: 'field X unused this session, drop it';
    'add field failed_attempts under each open thread'; 'shorten Z to single token'."""
    conn = get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO evolve (suggestion, rationale, created_at) VALUES (?,?,?)",
        (suggestion, rationale or None, now),
    )
    _emit(conn, "evolve_format", summary=suggestion)
    conn.commit()
    return "ok"


@read_tool()
def evolve_review(include_applied: bool = False) -> str:
    """List pending (or all) format-evolution suggestions for review."""
    conn = get_db()
    if include_applied:
        rows = conn.execute(
            "SELECT * FROM evolve ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM evolve WHERE applied=0 ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
    if not rows:
        return "no_pending"

    def _st(e) -> str:
        try:
            return e["status"] or "pending"
        except (IndexError, KeyError):
            return "pending"
    return "\n".join(
        f"#{e['id']} {'[APPLIED]' if e['applied'] else '['+_st(e)+']'} "
        f"{q(e['suggestion'])}" + (f" why={q(e['rationale'])}" if e["rationale"] else "")
        for e in rows
    )


_EVOLVE_DECISIONS = {"promote", "dismiss"}


@write_tool()
def evolve_decide(evolve_id: int, decision: str, reason: str = "") -> str:
    """Triage a pending format-evolution suggestion. Used by the autonomous
    evolve reviewer daemon (and available manually).

    `decision`:
      'promote' — still relevant + worth doing → status='promoted', so the
                  brief surfaces it sharply (★) for the foreground agent /
                  human to ACTUALLY APPLY. Applying edits format/code — that
                  stays a foreground/human action; this tool never applies.
      'dismiss' — duplicate of another suggestion, superseded, or stale →
                  status='dismissed', dropped from the pending queue.

    `reason`: one line (esp. which #id it duplicates, for dismiss)."""
    dec = decision.strip().lower()
    if dec not in _EVOLVE_DECISIONS:
        return f"ERR bad_decision={decision} (promote|dismiss)"
    conn = get_db()
    _ensure_session(conn)
    if not conn.execute(
        "SELECT 1 FROM evolve WHERE id=?", (int(evolve_id),)
    ).fetchone():
        return f"ERR evolve_not_found={evolve_id}"
    status = "promoted" if dec == "promote" else "dismissed"
    conn.execute(
        "UPDATE evolve SET status=?, reviewed_at=?, review_reason=? WHERE id=?",
        (status, int(time.time()), reason.strip() or None, int(evolve_id)),
    )
    _emit(conn, f"evolve_{status}", target=str(evolve_id), summary=reason[:140])
    conn.commit()
    return f"ok id={evolve_id} status={status}"


@write_tool()
def auto_review_trigger(focus: str = "combined", force: bool = False) -> str:
    """Check current counters + close-thread state and, if conditions are
    met, fire review_thread(mode='auto') for the richest pending thread.

    `force=True` skips the counter check (always trigger if there's a
    rich pending closed thread). Use this when you've seen a skill_nudge
    or skill_hint and want to act without manually picking the thread_id.
    """
    conn = get_db()
    _ensure_session(conn)
    from ..nudges import auto_review_should_fire
    tid = auto_review_should_fire(conn, identity._session_id, force=force)
    if not tid:
        return "no_pending (no rich closed thread, or thresholds not met)"
    from .skills import review_thread
    result = review_thread(thread_id=tid, focus=focus, mode='auto')
    return f"triggered for {tid}: {result}"
