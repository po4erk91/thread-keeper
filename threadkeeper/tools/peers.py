"""Cross-session channel: collective awareness across concurrent claude windows.

Identity is conversation_id (jsonl stem). Use peers() to discover who's live,
broadcast() to talk to all, whisper() to address one, inbox() to read mail.
"""

import json as _json
import sqlite3
import time
from typing import Optional

from .._mcp import read_tool, write_tool
from ..db import read_db, run_write
from ..helpers import fmt_age, q
from .. import identity
from ..identity import (
    _detect_self_cid,
    _emit,
    _heartbeat,
)
from ..brief import _append_dialog_log


@read_tool()
def whoami() -> str:
    """Return this conversation's detected conversation_id + how we know.

    Resolution order:
    - 'forced': THREADKEEPER_FORCE_CID env (set by spawn() for children)
    - 'ppid':   walk up process tree → claude --resume/--session-id <uuid>
    - 'mtime':  fallback heuristic (latest jsonl mtime; flaps under
                concurrent peer activity)
    """
    cid = _detect_self_cid()
    if not cid:
        return "no_cid_detected"
    via = identity._self_cid_via or "?"
    note = {
        "forced": "via env THREADKEEPER_FORCE_CID (stable)",
        "ppid":   "via ppid walk to claude CLI args (stable)",
        "mtime":  "via latest-jsonl-mtime (heuristic; may flap)",
    }.get(via, f"via {via}")
    return f"cid={cid} ({note})"


@read_tool()
def peers(window_min: int = 5) -> str:
    """List concurrent claude conversations active in the last `window_min`.

    Activity inferred from dialog_messages (ingested live). For each peer
    returns: cid, last user message snippet, age, message count. Self is
    marked with `*`. Empty if you're alone."""
    identity.ensure_session_started()
    self_cid = _detect_self_cid()
    now_t = int(time.time())
    cutoff = now_t - (window_min * 60)
    with read_db() as conn:
        rows = conn.execute(
            "SELECT session_id, role, content, created_at FROM dialog_messages "
            "WHERE created_at > ? AND session_id IS NOT NULL AND session_id != '' "
            "ORDER BY created_at DESC LIMIT 200",
            (cutoff,),
        ).fetchall()
        by_sess: dict[str, dict] = {}
        for r in rows:
            sid = r["session_id"]
            d = by_sess.setdefault(
                sid, {"last_user": None, "last_any_at": 0, "msgs": 0}
            )
            d["msgs"] += 1
            if r["created_at"] > d["last_any_at"]:
                d["last_any_at"] = r["created_at"]
            if r["role"] == "user" and d["last_user"] is None:
                content = r["content"]
                if content.startswith("[tool_result]") or content.startswith("[Image"):
                    alt = conn.execute(
                        "SELECT content, created_at FROM dialog_messages "
                        "WHERE session_id=? AND role='user' "
                        "AND content NOT LIKE '[tool_result]%' "
                        "AND content NOT LIKE '[Image%' "
                        "ORDER BY created_at DESC LIMIT 1",
                        (sid,),
                    ).fetchone()
                    if alt:
                        d["last_user"] = {
                            "content": alt["content"],
                            "created_at": alt["created_at"],
                        }
                else:
                    d["last_user"] = dict(r)
    if not by_sess:
        return "no_peers (you alone)"
    items = sorted(
        by_sess.items(), key=lambda x: x[1]["last_any_at"], reverse=True
    )
    lines = []
    for sid, d in items:
        marker = "*" if sid == self_cid else " "
        u = d["last_user"]
        if u:
            snip = u["content"][:80].replace("\n", " ")
            if len(u["content"]) > 80:
                snip += "…"
            u_age = fmt_age(now_t - u["created_at"])
            lines.append(
                f"{marker} {sid[:8]} u={q(snip)} u_age={u_age} msgs={d['msgs']}"
            )
        else:
            lines.append(f"{marker} {sid[:8]} (no user msg) msgs={d['msgs']}")
    return "\n".join(lines)


@write_tool()
def broadcast(content: str) -> str:
    """Post a message visible to ALL concurrent claude conversations.

    Other peers see it in their next brief() under `inbox` (unread) and via
    inbox(). Use for: shared insights, status updates, work claims, anything
    you'd want sibling sessions to know."""
    return _post_signal(to_cid="", content=content, kind="broadcast")


@write_tool()
def whisper(to_cid: str, content: str) -> str:
    """Post a message visible only to the specified conversation_id.

    Use peers() to discover available cids. The 8-char prefix shown there is
    enough — it'll be matched as prefix. Use whoami() to get your own cid
    (rarely needed; messages from self to self are dropped)."""
    return _post_signal(to_cid=to_cid.strip(), content=content, kind="whisper")


def _post_signal(to_cid: str, content: str, kind: str) -> str:
    self_cid = _detect_self_cid()
    if not self_cid:
        return "ERR cannot_detect_self_cid"
    identity.ensure_session_started()
    target: Optional[str] = None
    with read_db() as conn:
        if to_cid:
            if len(to_cid) < 36:
                row = conn.execute(
                    "SELECT DISTINCT session_id FROM dialog_messages "
                    "WHERE session_id LIKE ? LIMIT 2",
                    (to_cid + "%",),
                ).fetchall()
                if not row:
                    return f"ERR no_peer_matching={to_cid}"
                if len(row) > 1:
                    return f"ERR ambiguous_prefix={to_cid} matches={len(row)}"
                target = row[0]["session_id"]
            else:
                target = to_cid
            if target == self_cid:
                return "ERR self_target (whispering to yourself; use note instead)"
        auto_task = None
        try:
            candidate_cids = [self_cid]
            if target:
                candidate_cids.append(target)
            for cc in candidate_cids:
                row = conn.execute(
                    "SELECT id FROM tasks WHERE spawned_cid=? "
                    "ORDER BY started_at DESC LIMIT 1",
                    (cc,),
                ).fetchone()
                if row:
                    auto_task = row["id"]
                    break
        except sqlite3.OperationalError:
            pass
    now_t = int(time.time())

    def _write(conn: sqlite3.Connection) -> int:
        cur = conn.execute(
            "INSERT INTO signals (from_cid, to_cid, kind, content, created_at, "
            "task_id) VALUES (?,?,?,?,?,?)",
            (self_cid, target, kind, content, now_t, auto_task),
        )
        _emit(conn, f"signal:{kind}", target=target or "*", summary=content)
        return int(cur.lastrowid)

    signal_id = run_write("post-signal", _write)
    _append_dialog_log(self_cid, target, kind, content)
    tail = f" task={auto_task}" if auto_task else ""
    if target:
        return f"ok id={signal_id} -> {target[:8]}{tail}"
    return f"ok id={signal_id} broadcast{tail}"


@write_tool()
def wait(timeout_s: int = 30, kinds: str = "", mark_read: bool = True) -> str:
    """Block until a new signal arrives for me or `timeout_s` elapses.

    Returns immediately if there are unread signals. Otherwise polls the
    signals table every 250ms. Use this for realtime turn-based exchange
    with peers: one side waits, the other side broadcasts/whispers/responds.

    `kinds`: comma-separated filter ('whisper,question,answer,broadcast');
    empty = any. `timeout_s` is clamped to [1, 120] (mcp tool call has its
    own deadline; don't oversleep)."""
    self_cid = _detect_self_cid()
    if not self_cid:
        return "ERR cannot_detect_self_cid"
    timeout_s = max(1, min(int(timeout_s), 120))
    kinds_filter = [k.strip() for k in kinds.split(",") if k.strip()]
    identity.ensure_session_started()
    deadline = time.time() + timeout_s
    poll_interval = 0.25
    while True:
        params: list = [self_cid, self_cid]
        kind_clause = ""
        if kinds_filter:
            ph = ",".join(["?"] * len(kinds_filter))
            kind_clause = f" AND kind IN ({ph})"
            params += kinds_filter
        with read_db() as conn:
            rows = conn.execute(
                "SELECT id, from_cid, to_cid, kind, content, created_at "
                "FROM signals "
                "WHERE (to_cid = ? OR to_cid IS NULL) AND from_cid != ? "
                f"AND read_at IS NULL{kind_clause} "
                "ORDER BY created_at ASC LIMIT 10",
                params,
            ).fetchall()
        if rows:
            now_t = int(time.time())
            lines = [f"got={len(rows)} cid={self_cid[:8]}"]
            for r in rows:
                ago = fmt_age(now_t - r["created_at"])
                scope = "*" if r["to_cid"] is None else "→me"
                lines.append(
                    f"  #{r['id']} {scope} from={r['from_cid'][:8]} "
                    f"+{r['kind']} {ago}_ago {q(r['content'][:240])}"
                )
            if mark_read:
                ids = tuple(r["id"] for r in rows)

                def _mark(conn: sqlite3.Connection) -> None:
                    conn.execute(
                        "UPDATE signals SET read_at=? "
                        f"WHERE id IN ({','.join('?' * len(ids))})",
                        (now_t, *ids),
                    )

                run_write("wait-mark-read", _mark)
            return "\n".join(lines)
        if time.time() >= deadline:
            return f"timeout after {timeout_s}s (no_signals)"
        time.sleep(poll_interval)


@write_tool()
def ask(to_cid: str, question: str, timeout_s: int = 60) -> str:
    """Send a question to a peer and wait synchronously for their answer.

    Mechanics: posts a whisper with kind='question'; blocks until target
    posts a whisper/answer back to me, or `timeout_s` elapses. Use peers()
    to find available cids; 8-char prefix accepted.

    Note: requires the target to be in a `wait()` loop or actively calling
    inbox()+respond(). If they're idle, you'll just timeout."""
    self_cid = _detect_self_cid()
    if not self_cid:
        return "ERR cannot_detect_self_cid"
    if not to_cid.strip():
        return "ERR empty_to_cid"
    timeout_s = max(1, min(int(timeout_s), 120))
    identity.ensure_session_started()
    target = to_cid.strip()
    with read_db() as conn:
        if len(target) < 36:
            row = conn.execute(
                "SELECT DISTINCT session_id FROM dialog_messages "
                "WHERE session_id LIKE ? LIMIT 2",
                (target + "%",),
            ).fetchall()
            if not row:
                return f"ERR no_peer_matching={target}"
            if len(row) > 1:
                return f"ERR ambiguous_prefix={target} matches={len(row)}"
            target = row[0]["session_id"]
    if target == self_cid:
        return "ERR self_target"
    now_t = int(time.time())

    def _post(conn: sqlite3.Connection) -> int:
        cur = conn.execute(
            "INSERT INTO signals (from_cid, to_cid, kind, content, created_at) "
            "VALUES (?,?,?,?,?)",
            (self_cid, target, "question", question, now_t),
        )
        _emit(conn, "signal:question", target=target, summary=question)
        return int(cur.lastrowid)

    qid = run_write("ask-post", _post)
    deadline = time.time() + timeout_s
    while True:
        with read_db() as conn:
            ans = conn.execute(
                "SELECT id, content, kind, created_at FROM signals "
                "WHERE from_cid=? AND to_cid=? "
                "AND kind IN ('answer','whisper') "
                "AND created_at >= ? ORDER BY created_at ASC LIMIT 1",
                (target, self_cid, now_t),
            ).fetchone()
        if ans:
            answer_id = ans["id"]

            def _mark(conn: sqlite3.Connection) -> None:
                conn.execute(
                    "UPDATE signals SET read_at=? WHERE id=?",
                    (int(time.time()), answer_id),
                )

            run_write("ask-mark-answer", _mark)
            return (
                f"answer #{ans['id']} ({ans['kind']}) from {target[:8]}: "
                f"{ans['content']}"
            )
        if time.time() >= deadline:
            return (
                f"TIMEOUT qid={qid} target={target[:8]} "
                f"(no whisper/answer reply within {timeout_s}s)"
            )
        time.sleep(0.4)


@write_tool()
def respond(qid: int, content: str) -> str:
    """Answer a specific question (signals.id) with a directed whisper.

    Use after seeing a `+question` entry in inbox()/wait(). Marks the
    original question as read and inserts an `answer` whisper to the asker."""
    self_cid = _detect_self_cid()
    if not self_cid:
        return "ERR cannot_detect_self_cid"
    identity.ensure_session_started()
    now_t = int(time.time())

    def _write(conn: sqlite3.Connection) -> tuple[str, int, str]:
        qrow = conn.execute(
            "SELECT from_cid, to_cid, kind FROM signals WHERE id=?", (qid,)
        ).fetchone()
        if not qrow:
            return "missing", 0, ""
        if qrow["to_cid"] != self_cid:
            scope = qrow["to_cid"][:8] if qrow["to_cid"] else "broadcast"
            return "wrong-target", 0, scope
        from_cid = qrow["from_cid"]
        cur = conn.execute(
            "INSERT INTO signals (from_cid, to_cid, kind, content, created_at) "
            "VALUES (?,?,?,?,?)",
            (self_cid, from_cid, "answer", content, now_t),
        )
        conn.execute("UPDATE signals SET read_at=? WHERE id=?", (now_t, qid))
        _emit(conn, "signal:answer", target=from_cid, summary=content)
        return "ok", int(cur.lastrowid), from_cid

    status, answer_id, detail = run_write("respond", _write)
    if status == "missing":
        return f"ERR question_not_found id={qid}"
    if status == "wrong-target":
        return f"ERR not_addressed_to_me question.to={detail}"
    _append_dialog_log(self_cid, detail, "answer", content)
    return f"ok id={answer_id} -> {detail[:8]}"


@write_tool()
def inbox(unread_only: bool = True, k: int = 20, mark_read: bool = True) -> str:
    """Read signals addressed to me (whispers + broadcasts).

    `unread_only=True` (default) returns only what hasn't been seen yet, and
    if `mark_read=True` marks them read on this call. Set both False to
    re-read history."""
    self_cid = _detect_self_cid()
    if not self_cid:
        return "ERR cannot_detect_self_cid"
    identity.ensure_session_started()
    where = "(to_cid = ? OR to_cid IS NULL) AND from_cid != ?"
    params: list = [self_cid, self_cid]
    if unread_only:
        where += " AND read_at IS NULL"
    with read_db() as conn:
        rows = conn.execute(
            "SELECT id, from_cid, to_cid, kind, content, created_at "
            f"FROM signals WHERE {where} ORDER BY created_at DESC LIMIT ?",
            (*params, k),
        ).fetchall()
    if not rows:
        return "no_signals"
    now_t = int(time.time())
    lines = [f"got={len(rows)} cid={self_cid[:8]}"]
    for r in rows:
        ago = fmt_age(now_t - r["created_at"])
        scope = "*" if r["to_cid"] is None else "→me"
        lines.append(
            f"  #{r['id']} {scope} from={r['from_cid'][:8]} +{r['kind']} "
            f"{ago}_ago {q(r['content'][:200])}"
        )
    if mark_read and unread_only:
        ids = tuple(r["id"] for r in rows)

        def _mark(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE signals SET read_at=? "
                f"WHERE id IN ({','.join('?' * len(ids))})",
                (now_t, *ids),
            )

        run_write("inbox-mark-read", _mark)
    return "\n".join(lines)


@write_tool()
def live_status(advance_cursor: bool = True, k: int = 30) -> str:
    """See what OTHER concurrent Claude sessions did since this session last
    polled. Call when brief() shows live=N where N>0, or proactively when you
    suspect a parallel instance is working on something relevant. Advances
    this session's cursor by default; pass advance_cursor=False to peek
    without consuming."""
    identity.ensure_session_started()
    with read_db() as conn:
        cur_row = conn.execute(
            "SELECT last_event_id FROM cursors WHERE session_id=?",
            (identity._session_id,),
        ).fetchone()
        cur_id = cur_row["last_event_id"] if cur_row else 0
        rows = conn.execute(
            "SELECT id, session_id, kind, target, summary, created_at "
            "FROM events WHERE id > ? AND session_id != ? "
            "ORDER BY id ASC LIMIT ?",
            (cur_id, identity._session_id, k),
        ).fetchall()
    now = int(time.time())
    max_seen = rows[-1]["id"] if rows and advance_cursor else None

    def _touch(conn: sqlite3.Connection) -> None:
        _heartbeat(conn)
        if max_seen is not None:
            conn.execute(
                "UPDATE cursors SET last_event_id=?, updated_at=? "
                "WHERE session_id=?",
                (max_seen, now, identity._session_id),
            )

    run_write("live-status-touch", _touch)
    if not rows:
        return "no_fresh_events"
    lines = [f"fresh={len(rows)}"]
    for e in rows:
        ago = fmt_age(now - e["created_at"])
        target = e["target"] or "-"
        summary = (e["summary"] or "")[:140]
        lines.append(
            f"  {e['session_id']}@{target} +{e['kind']} {q(summary)} {ago}_ago"
        )
    return "\n".join(lines)


@write_tool()
def presence(idle_threshold_min: int = 5) -> str:
    """List concurrent Claude sessions with heartbeats within threshold
    (default 5 min). Excludes self. Useful for understanding who else is
    currently active before making changes."""
    identity.ensure_session_started()

    def _touch(conn: sqlite3.Connection) -> None:
        _heartbeat(conn)

    run_write("presence-heartbeat", _touch)
    now = int(time.time())
    threshold = now - (idle_threshold_min * 60)
    with read_db() as conn:
        rows = conn.execute(
            "SELECT * FROM presence WHERE heartbeat_at >= ? AND session_id != ? "
            "ORDER BY heartbeat_at DESC",
            (threshold, identity._session_id),
        ).fetchall()
    if not rows:
        return "no_other_active"
    lines = [f"active_others={len(rows)}"]
    for p in rows:
        idle = fmt_age(now - p["heartbeat_at"])
        lines.append(
            f"  {p['session_id']} cli={p['client'] or '-'} "
            f"on={p['current_thread'] or '-'} "
            f"last={p['last_action'] or '-'} idle={idle}"
        )
    return "\n".join(lines)


def _resolve_parent_cid(conn) -> Optional[str]:
    """Find this child's parent cid via tasks.parent_cid. Returns None when
    this is not a spawned child (no row links spawned_cid == me)."""
    self_cid = _detect_self_cid()
    if not self_cid:
        return None
    row = conn.execute(
        "SELECT parent_cid FROM tasks WHERE spawned_cid=? "
        "ORDER BY started_at DESC LIMIT 1",
        (self_cid,),
    ).fetchone()
    if not row:
        return None
    return row["parent_cid"]


@write_tool()
def search_via_parent(query: str, k: int = 5,
                      scope: str = "notes",
                      mode: str = "hybrid",
                      timeout_s: int = 30) -> str:
    """Delegate a semantic search to the parent process (or any peer with
    embeddings loaded). For light children spawned with
    THREADKEEPER_NO_EMBEDDINGS=1, this is how you reach into the shared
    DB's semantic index without loading PyTorch yourself.

    Mechanism: posts a 'search_request' signal addressed to the parent's
    cid (auto-resolved via tasks.parent_cid; falls back to broadcast if
    none). The parent's search_proxy daemon answers with a 'search_response'
    signal. This tool blocks until reply or timeout_s.

    `scope`: 'notes' (default) or 'dialog'.
    `mode`:  'hybrid'|'semantic'|'fts' (dialog scope only).
    `k`:     top-N results, 1..100.

    Returns formatted result lines, or 'timeout' if no parent answers."""
    self_cid = _detect_self_cid()
    if not self_cid:
        return "ERR cannot_detect_self_cid"
    identity.ensure_session_started()
    with read_db() as conn:
        target = _resolve_parent_cid(conn)
    if target == self_cid:
        return "ERR self_target"

    payload = _json.dumps({
        "query": query, "k": int(k), "scope": scope, "mode": mode,
    })
    now_t = int(time.time())

    def _post(conn: sqlite3.Connection) -> int:
        cur = conn.execute(
            "INSERT INTO signals (from_cid, to_cid, kind, content, created_at) "
            "VALUES (?, ?, 'search_request', ?, ?)",
            (self_cid, target, payload, now_t),
        )
        return int(cur.lastrowid)

    request_id = run_write("search-via-parent-post", _post)
    _append_dialog_log(self_cid, target, "search_request",
                       f"q={query[:80]} k={k} scope={scope}")

    deadline = time.time() + max(1, min(int(timeout_s), 120))
    while True:
        with read_db() as conn:
            resp = conn.execute(
                "SELECT id, from_cid, content FROM signals "
                "WHERE kind='search_response' AND to_cid=? "
                "AND created_at >= ? ORDER BY id ASC LIMIT 1",
                (self_cid, now_t),
            ).fetchone()
        if resp:
            response_id = resp["id"]

            def _mark(conn: sqlite3.Connection) -> None:
                conn.execute(
                    "UPDATE signals SET read_at=? WHERE id=?",
                    (int(time.time()), response_id),
                )

            run_write("search-via-parent-mark", _mark)
            try:
                body = _json.loads(resp["content"])
            except _json.JSONDecodeError:
                return f"ERR bad_response_payload from={resp['from_cid'][:8]}"
            if body.get("error"):
                return f"ERR remote={body['error']}"
            results = body.get("results") or []
            if not results:
                return "no_matches"
            scope_actual = body.get("scope", scope)
            now2 = int(time.time())
            lines = [
                f"got={len(results)} via={resp['from_cid'][:8]} "
                f"scope={scope_actual}"
            ]
            for r in results:
                snip = (r.get("content") or "")[:200].replace("\n", " ⏎ ")
                if scope_actual == "dialog":
                    sess = (r.get("session_id") or "-")[:8]
                    age = fmt_age(now2 - int(r.get("created_at") or now2))
                    role = r.get("role", "?")
                    score = r.get("score")
                    score_part = (
                        f"s={score:.2f} " if isinstance(score, (int, float))
                        else ""
                    )
                    lines.append(f"  {role}@{sess} {score_part}{age}_ago {q(snip)}")
                else:
                    tid = (r.get("thread_id") or "-")
                    kind = r.get("kind") or "?"
                    score = r.get("score")
                    score_part = (
                        f"s={score:.2f} " if isinstance(score, (int, float))
                        else ""
                    )
                    lines.append(f"  {tid} {kind} {score_part}{q(snip)}")
            return "\n".join(lines)
        if time.time() >= deadline:
            return (
                f"timeout request_id={request_id} target="
                f"{(target or 'broadcast')[:8]} (no parent with "
                "embeddings answered)"
            )
        time.sleep(0.25)
