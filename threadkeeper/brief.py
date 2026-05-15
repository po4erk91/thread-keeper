"""Brief rendering and dialog log helper.

`render_brief` builds the multi-section ctx string returned by the
`brief()` MCP tool: ctx header, core_memory, inbox, tasks_running,
live_peers, open/idle/closed threads, style, verbatim, query-relevant
hits, weak_spots, concepts, distill_pending, extract_pending,
pickup_top, evolve_pending, and the trailing user-facing reminder.

`_append_dialog_log` writes one line per cross-session signal to the
shared dialog log tailed by open_dialog_window().
"""

import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

from .config import SEMANTIC_AVAILABLE, DIALOG_LOG, TASK_LOG_DIR
from .helpers import fmt_age, q
from . import identity
from .identity import _detect_self_cid, _ensure_cursor
from .embeddings import _cosine_search


# Parallelism cue regex: matches user-language signals that work
# decomposes into multiple independent units. The actual locale bundles
# (English + Russian + count-plural-noun families) live in i18n.py.
from .i18n import SPAWN_CUE_RE as _SPAWN_CUE_RE  # noqa: E402


def render_brief(conn: sqlite3.Connection, query: str = "", k: int = 6) -> str:
    now = int(time.time())
    out: list[str] = []

    # ── ctx ───────────────────────────────────────────────────────────────
    last = conn.execute(
        "SELECT started_at, ended_at FROM sessions "
        "WHERE id IS NOT ? ORDER BY started_at DESC LIMIT 1",
        (identity._session_id,),
    ).fetchone()
    last_str = "first" if last is None else fmt_age(now - (last["ended_at"] or last["started_at"]))
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    sem = "on" if SEMANTIC_AVAILABLE else "off"

    # live=N: events from OTHER sessions since this session's cursor
    _ensure_cursor(conn)
    cur_row = conn.execute(
        "SELECT last_event_id FROM cursors WHERE session_id=?", (identity._session_id,)
    ).fetchone()
    cur_id = cur_row["last_event_id"] if cur_row else 0
    fresh = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE id > ? AND session_id != ?",
        (cur_id, identity._session_id),
    ).fetchone()["c"]

    self_cid = _detect_self_cid()
    out.append(
        f"ctx sess={identity._session_id or '-'} last={last_str} sem={sem} "
        f"live={fresh} cid={self_cid[:8] if self_cid else '-'} now={now_iso}"
    )

    # ── core_memory ───────────────────────────────────────────────────────
    # Letta-style RAM tier: always-shown, ordered by priority DESC. Use
    # sparingly — these are "what new-claude must know" lines.
    try:
        core_rows = conn.execute(
            "SELECT key, content, priority FROM core_memory "
            "ORDER BY priority DESC, key ASC"
        ).fetchall()
    except sqlite3.OperationalError:
        core_rows = []
    if core_rows:
        out.append("")
        out.append("core_memory")
        for r in core_rows:
            snip = r["content"][:120].replace("\n", " ")
            if len(r["content"]) > 120:
                snip += "…"
            out.append(f"  [P{r['priority']}] {r['key']}: {q(snip)}")

    # ── inbox (unread signals to me) ──────────────────────────────────────
    if self_cid:
        unread = conn.execute(
            "SELECT id, from_cid, to_cid, kind, content, created_at FROM signals "
            "WHERE (to_cid = ? OR to_cid IS NULL) AND from_cid != ? "
            "AND read_at IS NULL ORDER BY created_at DESC LIMIT 10",
            (self_cid, self_cid),
        ).fetchall()
        if unread:
            out.append("")
            out.append(f"inbox unread={len(unread)} (call inbox() to read+mark)")
            for s in unread[:5]:
                ago = fmt_age(now - s["created_at"])
                scope = "*" if s["to_cid"] is None else "→me"
                snip = s["content"][:90].replace("\n", " ")
                if len(s["content"]) > 90:
                    snip += "…"
                out.append(
                    f"  #{s['id']} {scope} from={s['from_cid'][:8]} "
                    f"+{s['kind']} {ago}_ago {q(snip)}"
                )

    # ── tasks_running ─────────────────────────────────────────────────────
    # Only my own spawned children that are still alive. Refresh first so
    # zombies (parent died, child orphaned and reaped) get marked ended
    # instead of lingering as "running" forever.
    if self_cid:
        try:
            from .tools.spawn import _refresh_tasks
            _refresh_tasks(conn)
        except (sqlite3.OperationalError, NameError, ImportError):
            pass  # tolerate at startup before tools.spawn is imported
        running = conn.execute(
            "SELECT id, pid, prompt, started_at, spawned_cid FROM tasks "
            "WHERE parent_cid=? AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 5",
            (self_cid,),
        ).fetchall()
        if running:
            out.append("")
            out.append("tasks_running")
            for t in running:
                cid = (t["spawned_cid"] or "?")[:8]
                snip = t["prompt"][:60].replace("\n", " ")
                if len(t["prompt"]) > 60:
                    snip += "…"
                out.append(
                    f"  {t['id']} pid={t['pid']} cid={cid} "
                    f"age={fmt_age(now - t['started_at'])} {q(snip)}"
                )

    # ── spawn_hint ────────────────────────────────────────────────────────
    # Behavioral nudge: agents systematically under-use spawn(). They read
    # "tool exists" but never reach for it as a parallelism primitive.
    # Surface a one-line trigger when conditions suggest decomposition would
    # help (work piling up, never-spawned this conversation, or explicit
    # parallel cue in user message).
    if self_cid:
        try:
            active_n = conn.execute(
                "SELECT COUNT(*) c FROM threads WHERE state='active'"
            ).fetchone()["c"]
            idle_n = conn.execute(
                "SELECT COUNT(*) c FROM threads WHERE state='idle'"
            ).fetchone()["c"]
            running_kids = conn.execute(
                "SELECT COUNT(*) c FROM tasks "
                "WHERE parent_cid=? AND ended_at IS NULL",
                (self_cid,),
            ).fetchone()["c"]
            ever_kids = conn.execute(
                "SELECT COUNT(*) c FROM tasks WHERE parent_cid=?",
                (self_cid,),
            ).fetchone()["c"]
        except sqlite3.OperationalError:
            active_n = idle_n = running_kids = ever_kids = 0

        cue_hit = None
        try:
            row = conn.execute(
                "SELECT content FROM dialog_messages "
                "WHERE session_id=? AND role='user' AND created_at > ? "
                "AND content NOT LIKE '[tool_result]%' "
                "ORDER BY created_at DESC LIMIT 1",
                (self_cid, now - 600),
            ).fetchone()
            if row:
                m = _SPAWN_CUE_RE.search(row["content"])
                if m:
                    cue_hit = m.group(0)
        except sqlite3.OperationalError:
            pass

        # Show hint only when not already parallelizing AND there's a real signal.
        show = running_kids == 0 and (active_n >= 3 or idle_n >= 3 or cue_hit)
        if show:
            # Count consecutive hint-shows since the last actual spawn() for
            # this cid. Escalates phrasing when the agent keeps ignoring.
            consecutive_ignored = 0
            try:
                last_spawn_at = conn.execute(
                    "SELECT MAX(started_at) m FROM tasks WHERE parent_cid=?",
                    (self_cid,),
                ).fetchone()["m"] or 0
                consecutive_ignored = conn.execute(
                    "SELECT COUNT(*) c FROM events "
                    "WHERE kind='spawn_hint_shown' AND target=? "
                    "AND created_at > ?",
                    (self_cid, last_spawn_at),
                ).fetchone()["c"]
            except sqlite3.OperationalError:
                pass

            parts = [
                f"active={active_n}",
                f"idle={idle_n}",
                f"children={running_kids}",
            ]
            if ever_kids == 0:
                parts.append("never_spawned=1")
            if cue_hit:
                parts.append(f"user_cue={q(cue_hit)}")
            if consecutive_ignored >= 3:
                parts.append(f"ignored={consecutive_ignored}x")
            out.append("")
            out.append("spawn_hint " + " ".join(parts))

            warn = "⚠️ " if consecutive_ignored >= 3 else ""
            if cue_hit:
                out.append(
                    f"  → {warn}user signaled decomposable work. DO NOT "
                    "answer linearly — spawn(prompt, role=...) each unit "
                    "NOW, sync via inbox/wait"
                )
            elif ever_kids == 0:
                out.append(
                    f"  → {warn}never spawned this convo. BEFORE answering: "
                    "does request split into ≥2 independent units? if yes → "
                    "spawn(prompt, role=...) each, don't go serial"
                )
            else:
                out.append(
                    f"  → {warn}work piling up. DECOMPOSE: "
                    "spawn(prompt, role=...) for each independent unit "
                    "before answering"
                )
            if consecutive_ignored >= 3:
                out.append(
                    f"  ⚠️ hint shown {consecutive_ignored}× without spawn — "
                    "reflex is FAILING. next response must spawn() or "
                    "explain why decomp doesn't apply"
                )

            # Log this show so the next render_brief can detect repeated ignore.
            try:
                conn.execute(
                    "INSERT INTO events (session_id, kind, target, summary, "
                    "created_at) VALUES (?,?,?,?,?)",
                    (identity._session_id or "", "spawn_hint_shown",
                     self_cid, "", now),
                )
                conn.commit()
            except sqlite3.OperationalError:
                pass

    # ── live_peers ────────────────────────────────────────────────────────
    # Recent activity (last 5 min) from concurrent claude conversations.
    # Identity is jsonl conversation_id. Self is marked with `*`.
    peer_rows = conn.execute(
        "SELECT session_id, role, content, created_at FROM dialog_messages "
        "WHERE created_at > ? AND session_id IS NOT NULL AND session_id != '' "
        "AND content NOT LIKE '[tool_result]%' AND content NOT LIKE '[Image%' "
        "ORDER BY created_at DESC LIMIT 200",
        (now - 300,),
    ).fetchall()
    by_sess: dict[str, dict] = {}
    for r in peer_rows:
        sid = r["session_id"]
        d = by_sess.setdefault(sid, {"last_user": None, "last_at": 0, "msgs": 0})
        d["msgs"] += 1
        if r["created_at"] > d["last_at"]:
            d["last_at"] = r["created_at"]
        if r["role"] == "user" and d["last_user"] is None:
            d["last_user"] = dict(r)
    if by_sess:
        ordered = sorted(by_sess.items(), key=lambda x: x[1]["last_at"], reverse=True)
        out.append("")
        out.append("live_peers (* = you)")
        for sid, d in ordered[:6]:
            marker = "*" if sid == self_cid else " "
            u = d["last_user"]
            if u:
                snip = u["content"][:80].replace("\n", " ")
                if len(u["content"]) > 80:
                    snip += "…"
                out.append(
                    f" {marker}{sid[:8]} u={q(snip)} "
                    f"u_age={fmt_age(now - u['created_at'])} msgs={d['msgs']}"
                )
            else:
                out.append(
                    f" {marker}{sid[:8]} (no user msg) msgs={d['msgs']} "
                    f"last={fmt_age(now - d['last_at'])}_ago"
                )

    # ── open ──────────────────────────────────────────────────────────────
    open_t = conn.execute(
        "SELECT * FROM threads WHERE state='active' ORDER BY last_touched_at DESC"
    ).fetchall()
    if open_t:
        out.append("")
        out.append("open")
        for t in open_t:
            parts = [f"  {t['id']}", f"q={q(t['question'])}"]
            if t["parent_id"]:
                parts.append(f"p={t['parent_id']}")
            if t["last_move"]:
                parts.append(f"last={q(t['last_move'][:90])}")
            parts.append(f"age={fmt_age(now - t['opened_at'])}")
            out.append(" ".join(parts))

    # ── idle ──────────────────────────────────────────────────────────────
    idle_t = conn.execute(
        "SELECT * FROM threads WHERE state='idle' "
        "ORDER BY last_touched_at DESC LIMIT 5"
    ).fetchall()
    if idle_t:
        out.append("")
        out.append("idle")
        for t in idle_t:
            out.append(
                f"  {t['id']} q={q(t['question'])} "
                f"dorm={fmt_age(now - t['last_touched_at'])}"
            )

    # ── closed (recent) ───────────────────────────────────────────────────
    closed_t = conn.execute(
        "SELECT * FROM threads WHERE state='closed' "
        "ORDER BY last_touched_at DESC LIMIT 3"
    ).fetchall()
    if closed_t:
        out.append("")
        out.append("closed_recent")
        for t in closed_t:
            out.append(f"  {t['id']} out={q((t['outcome'] or '-')[:120])}")

    # ── memory_nudge ──────────────────────────────────────────────────────
    # Counter-driven (active push, not just passive surface): when N mutating
    # events have passed in this session without a memory save, escalate from
    # soft hint to demanding ⚠️. See threadkeeper/nudges.py for thresholds.
    try:
        from .nudges import compute_memory_nudge
        mem_nudge = compute_memory_nudge(conn, identity._session_id or "")
    except (sqlite3.OperationalError, ImportError):
        mem_nudge = None
    if mem_nudge:
        out.append("")
        out.append(mem_nudge)

    # ── skill_hint ────────────────────────────────────────────────────────
    # Behavioral nudge inspired by hermes-agent's Learning loop: after a rich
    # thread closes, the lessons inside it (insights + repeated moves) should
    # be materialized as a reusable Claude skill under ~/.claude/skills/,
    # not just sit in notes. Trigger only on threads recently closed AND
    # rich enough to be worth a class-level skill — never on one-off chatter.
    #
    # Rich = ≥5 notes total AND ≥2 of those tagged 'insight' or 'move'.
    # Recently_closed = closed within last 24h.
    # Suppress if a "skill_materialized" event already logged for the thread.
    if self_cid:
        try:
            rich_closed = conn.execute(
                "SELECT t.id, t.question, t.outcome, "
                "  (SELECT COUNT(*) FROM notes n WHERE n.thread_id=t.id) AS n_total, "
                "  (SELECT COUNT(*) FROM notes n WHERE n.thread_id=t.id "
                "   AND n.kind IN ('insight','move')) AS n_rich "
                "FROM threads t "
                "WHERE t.state='closed' AND t.last_touched_at > ? "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM events e "
                "    WHERE e.kind='skill_materialized' AND e.target=t.id"
                "  ) "
                "ORDER BY t.last_touched_at DESC LIMIT 5",
                (now - 86400,),
            ).fetchall()
        except sqlite3.OperationalError:
            rich_closed = []

        candidates = [r for r in rich_closed
                      if r["n_total"] >= 5 and r["n_rich"] >= 2]
        if candidates:
            # Count consecutive shows since the last skill_materialized event
            # for this cid — escalates if the agent keeps ignoring.
            try:
                last_mat_at = conn.execute(
                    "SELECT MAX(created_at) m FROM events "
                    "WHERE kind='skill_materialized' AND session_id=?",
                    (identity._session_id or "",),
                ).fetchone()["m"] or 0
                consecutive_ignored = conn.execute(
                    "SELECT COUNT(*) c FROM events "
                    "WHERE kind='skill_hint_shown' AND target=? "
                    "AND created_at > ?",
                    (self_cid, last_mat_at),
                ).fetchone()["c"]
            except sqlite3.OperationalError:
                consecutive_ignored = 0

            top = candidates[0]
            out.append("")
            parts = [
                f"n={top['n_total']}",
                f"rich={top['n_rich']}",
            ]
            if consecutive_ignored >= 3:
                parts.append(f"ignored={consecutive_ignored}x")
            out.append("skill_hint " + " ".join(parts))
            warn = "⚠️ " if consecutive_ignored >= 3 else ""
            out.append(
                f"  → {warn}closed thread is rich (≥5 notes, ≥2 insight/move). "
                "MATERIALIZE: invoke skill-creator to write ~/.claude/skills/"
                "<class-level-name>/SKILL.md from the insights — don't let "
                "learnings sit only in notes"
            )
            if consecutive_ignored >= 3:
                out.append(
                    f"  ⚠️ hint shown {consecutive_ignored}× without "
                    "materialization — next reply must invoke skill-creator "
                    "or explain why the thread isn't class-level"
                )

            # Log this show so the next render_brief can detect repeated ignore.
            try:
                conn.execute(
                    "INSERT INTO events (session_id, kind, target, summary, "
                    "created_at) VALUES (?,?,?,?,?)",
                    (identity._session_id or "", "skill_hint_shown",
                     self_cid, top["id"], now),
                )
                conn.commit()
            except sqlite3.OperationalError:
                pass

    # ── skill_nudge ───────────────────────────────────────────────────────
    # Counter-driven companion to skill_hint: skill_hint reads thread state,
    # this one reads session event-counter. Together they catch "we have a
    # rich thread, materialize it" (hint) AND "we've been working hard
    # without saving any skill — likely missing something" (nudge).
    try:
        from .nudges import compute_skill_nudge
        sk_nudge = compute_skill_nudge(conn, identity._session_id or "")
    except (sqlite3.OperationalError, ImportError):
        sk_nudge = None
    if sk_nudge:
        out.append("")
        out.append(sk_nudge)

    # ── consulted_skills (this session) ───────────────────────────────────
    # Surface which skills the agent actually invoked / viewed in the
    # current session, plus any user-judgment outcomes ('helped' /
    # 'partial' / 'wrong'). Drives the patch-loop: if a recently-
    # consulted skill turned out 'wrong', the agent should PATCH it
    # before forgetting context, not next session.
    try:
        sess = identity._session_id or ""
        consulted = conn.execute(
            "SELECT target, kind, summary FROM events "
            "WHERE session_id = ? "
            "  AND kind IN ('skill_view', 'skill_use', 'skill_patch', "
            "               'skill_create', 'skill_outcome') "
            "ORDER BY created_at ASC",
            (sess,),
        ).fetchall()
    except sqlite3.OperationalError:
        consulted = []
    if consulted:
        # Group by skill name, collect kinds + outcomes.
        per_skill: dict[str, dict] = {}
        for r in consulted:
            tgt = r["target"] or "?"
            slot = per_skill.setdefault(
                tgt, {"used": 0, "viewed": 0, "patched": 0, "created": 0,
                       "outcomes": []},
            )
            kind = r["kind"]
            if kind == "skill_use":
                slot["used"] += 1
            elif kind == "skill_view":
                slot["viewed"] += 1
            elif kind == "skill_patch":
                slot["patched"] += 1
            elif kind == "skill_create":
                slot["created"] += 1
            elif kind == "skill_outcome" and r["summary"]:
                slot["outcomes"].append(r["summary"])
        out.append("")
        out.append("consulted_skills")
        for tgt in sorted(per_skill.keys()):
            s = per_skill[tgt]
            parts: list[str] = []
            if s["created"]:
                parts.append(f"created×{s['created']}")
            if s["viewed"]:
                parts.append(f"viewed×{s['viewed']}")
            if s["used"]:
                parts.append(f"used×{s['used']}")
            if s["patched"]:
                parts.append(f"patched×{s['patched']}")
            if s["outcomes"]:
                # Compact outcome tally
                tally: dict[str, int] = {}
                for o in s["outcomes"]:
                    tally[o] = tally.get(o, 0) + 1
                for o, n in tally.items():
                    parts.append(f"{o}×{n}")
            out.append(f"  {tgt}: {' '.join(parts)}")

    # ── style ─────────────────────────────────────────────────────────────
    style_rows = conn.execute("SELECT key, value FROM style").fetchall()
    if style_rows:
        out.append("")
        out.append("style " + " ".join(f"{r['key']}={r['value']}" for r in style_rows))

    # ── verbatim (last 5, chronological) ──────────────────────────────────
    qt = conn.execute(
        "SELECT speaker, content FROM verbatim ORDER BY created_at DESC LIMIT 5"
    ).fetchall()
    if qt:
        out.append("")
        out.append("verbatim")
        for r in reversed(qt):
            out.append(f"  {r['speaker']}> {q(r['content'][:200])}")

    # ── relevant_to_query (only if query passed) ──────────────────────────
    if query:
        if SEMANTIC_AVAILABLE:
            hits = _cosine_search(conn, query, k)
            if hits:
                out.append("")
                out.append(f"relevant q={q(query[:80])}")
                for r in hits:
                    snip = r["content"][:160].replace("\n", " ")
                    if len(r["content"]) > 160:
                        snip += "…"
                    out.append(
                        f"  {r['thread_id'] or '-'} {r['kind']} "
                        f"s={r['score']:.2f} {q(snip)}"
                    )
        else:
            try:
                rows = conn.execute(
                    "SELECT n.thread_id, n.kind, n.content FROM notes_fts f "
                    "JOIN notes n ON f.rowid=n.id WHERE notes_fts MATCH ? LIMIT ?",
                    (query, k),
                ).fetchall()
                if rows:
                    out.append("")
                    out.append(f"fts q={q(query[:80])}")
                    for r in rows:
                        snip = r["content"][:160].replace("\n", " ")
                        out.append(f"  {r['thread_id'] or '-'} {r['kind']} {q(snip)}")
            except sqlite3.OperationalError:
                pass

    # ── weak_spots ────────────────────────────────────────────────────────
    # Top categories with high recent failure rate, plus categories that have
    # registered probes but never been tested in this DB. Skip if both empty.
    try:
        weak = conn.execute(
            "SELECT category, fail_rate_7d, attempts, last_at FROM reliability "
            "WHERE fail_rate_7d IS NOT NULL AND attempts >= 3 "
            "ORDER BY fail_rate_7d DESC LIMIT 3"
        ).fetchall()
        unknown = conn.execute(
            "SELECT DISTINCT p.category FROM probes p "
            "LEFT JOIN reliability r ON r.category = p.category "
            "WHERE p.enabled = 1 AND r.category IS NULL LIMIT 3"
        ).fetchall()
    except sqlite3.OperationalError:
        weak, unknown = [], []
    if weak or unknown:
        out.append("")
        out.append("weak_spots")
        for r in weak:
            age = fmt_age(now - r["last_at"]) if r["last_at"] else "?"
            out.append(
                f"  {r['category']} fail7d={r['fail_rate_7d']:.2f} "
                f"n={r['attempts']} last={age}_ago"
            )
        for r in unknown:
            out.append(f"  {r['category']} (never_tested)")

    # ── concepts (high-confidence, recent) ─────────────────────────────────
    try:
        cs = conn.execute(
            "SELECT id, description FROM concepts "
            "WHERE confidence='high' "
            "ORDER BY registered_at DESC LIMIT 3"
        ).fetchall()
    except sqlite3.OperationalError:
        cs = []
    if cs:
        out.append("")
        out.append("concepts (high-conf)")
        for c in cs:
            snip = c["description"][:140].replace("\n", " ")
            out.append(f"  {c['id']} {q(snip)}")

    # ── user_model (dialectic) ────────────────────────────────────────────
    # Honcho-inspired dialectic snapshot: medium+high confidence claims about
    # the user, grouped by domain. Excludes low/disputed; never inferred from
    # one signal — claims earn confidence through accumulating evidence.
    try:
        syn_rows = conn.execute(
            "SELECT id, claim, domain, confidence, "
            "  support_count, contradict_count "
            "FROM user_dialectic "
            "WHERE state='active' AND confidence IN ('medium','high') "
            "ORDER BY "
            "  CASE confidence WHEN 'high' THEN 0 ELSE 1 END, "
            "  (support_count - contradict_count) DESC, "
            "  domain ASC "
            "LIMIT 12"
        ).fetchall()
    except sqlite3.OperationalError:
        syn_rows = []
    if syn_rows:
        # Group by domain inline (keep total ≤ 10 lines incl. headers).
        out.append("")
        out.append("user_model (dialectic)")
        grouped: dict[str, list] = {}
        order: list[str] = []
        for r in syn_rows:
            key = r["domain"] or "other"
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(r)
        line_budget = 8
        for dom in order:
            if line_budget <= 0:
                break
            out.append(f"  [{dom}]")
            line_budget -= 1
            for r in grouped[dom]:
                if line_budget <= 0:
                    break
                tag = "★" if r["confidence"] == "high" else "·"
                snip = r["claim"][:140].replace("\n", " ")
                if len(r["claim"]) > 140:
                    snip += "…"
                out.append(f"    {tag} {snip}")
                line_budget -= 1

    # ── distill_pending (votes >= 2) ───────────────────────────────────────
    try:
        ds = conn.execute(
            "SELECT id, kind, vote_sum FROM distill "
            "WHERE vote_sum >= 2 AND exported_at IS NULL "
            "ORDER BY vote_sum DESC LIMIT 3"
        ).fetchall()
    except sqlite3.OperationalError:
        ds = []
    if ds:
        out.append("")
        out.append("distill_pending (vote≥2)")
        for d in ds:
            out.append(
                f"  {d['id']} {d['kind']} votes={d['vote_sum']:.1f}"
            )

    # ── extract_pending ────────────────────────────────────────────────────
    try:
        ex_pending = conn.execute(
            "SELECT COUNT(*) c FROM extract_candidates WHERE status='pending'"
        ).fetchone()["c"]
    except sqlite3.OperationalError:
        ex_pending = 0
    if ex_pending > 0:
        out.append("")
        out.append(
            f"extract_pending n={ex_pending} (review_candidates / "
            f"accept_candidate to materialize)"
        )

    # ── pickup_top ────────────────────────────────────────────────────────
    # Surface the single oldest unclaimed unresolved thread as a hint that
    # a free-context session could pick it up. Only show when no high-value
    # current work (i.e. fewer than 3 active threads).
    try:
        active_count = conn.execute(
            "SELECT COUNT(*) c FROM threads WHERE state='active'"
        ).fetchone()["c"]
        if active_count < 3:
            top = conn.execute(
                "SELECT id, question, last_touched_at FROM threads "
                "WHERE state IN ('active','idle') AND claimed_at IS NULL "
                "AND last_touched_at <= ? "
                "ORDER BY last_touched_at ASC LIMIT 1",
                (now - 3 * 86400,),
            ).fetchone()
            if top:
                out.append("")
                out.append("pickup_top")
                out.append(
                    f"  {top['id']} idle={fmt_age(now - top['last_touched_at'])} "
                    f"q={q(top['question'][:120])}"
                )
    except sqlite3.OperationalError:
        pass

    # ── evolve hints ──────────────────────────────────────────────────────
    pend = conn.execute(
        "SELECT suggestion FROM evolve WHERE applied=0 "
        "ORDER BY created_at DESC LIMIT 3"
    ).fetchall()
    if pend:
        out.append("")
        out.append("evolve_pending")
        for e in pend:
            out.append(f"  {q(e['suggestion'][:200])}")

    # ── footer reminder: IDs are tool-call internals only ─────────────────
    # Evolve_pending #1 noted that brief's T-codes/cids leak into user-facing
    # replies when claude paraphrases. Loud trailing reminder beats quiet
    # style line buried mid-brief.
    out.append("")
    out.append(
        "⚠️ user-facing: paraphrase plain. Do NOT cite internal IDs above "
        "(thread T-codes, cids, signal #ids, session s_codes, task tk_codes, "
        "probe P-codes) when replying to the user — those are tool-call only."
    )

    return "\n".join(out)


def _append_dialog_log(from_cid: Optional[str], to_cid: Optional[str],
                      kind: str, content: str) -> None:
    """Single-line log of every cross-session signal. Tailed by
    open_dialog_window() so the user sees the live conversation."""
    try:
        TASK_LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H:%M:%S")
        f = (from_cid or "?")[:8]
        t = (to_cid or "*")[:8]
        # collapse newlines so each signal stays on one log line
        body = content.replace("\n", " ⏎ ")
        if len(body) > 280:
            body = body[:280] + "…"
        line = f"[{ts}] {f} → {t:<8} [{kind:<9}] {body}\n"
        with DIALOG_LOG.open("a", encoding="utf-8") as fp:
            fp.write(line)
    except OSError:
        pass
