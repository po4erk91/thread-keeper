"""Auto-extraction MCP tools.

Extracted from server.py. Heuristic candidates for note/concept/distill/
verbatim from recent dialog_messages. Each candidate lands status='pending';
session reviews in batch via review_candidates() then accept/reject.
"""

import sqlite3
import time
import re as _re_extract

from .._mcp import mcp
from ..db import get_db
from ..config import SEMANTIC_AVAILABLE
from ..helpers import fmt_age, q, gen_concept_id, gen_distill_id
from .. import identity
from ..identity import _ensure_session, _detect_self_cid, _emit
from ..embeddings import _embed, embed_tag


# Locale-aware heuristic matchers — patterns live in i18n.py so this
# module stays English-only. Locale-independent patterns (header,
# bullet list) stay inline.
from ..i18n import (
    WANT_RE as _WANT_RE,
    INSIGHT_MARKERS_RE as _INSIGHT_MARKERS_RE,
    EXAMPLE_RE as _EXAMPLE_RE,
    FRAME_RE as _FRAME_RE,
)
_HEADER_RE = _re_extract.compile(r"^##+\s", _re_extract.MULTILINE)
_BULLET_RE = _re_extract.compile(
    r"^\s*(?:[-*•]|\d+[.)])\s", _re_extract.MULTILINE
)


# Message-level noise filter (complements session-level
# _INTERNAL_PROMPT_PREFIXES). These prefixes appear in INDIVIDUAL
# messages of otherwise-valid sessions — context-compaction summaries
# from Anthropic CLI, subagent task prompts, SKILL.md injections, and
# `[Request interrupted by user for tool use]` service markers. None
# of them are user-intent signals; all of them were polluting extract
# candidates in the first calibration pass (2026-05-16 audit, 13
# candidates → ~25% precision; ~half of misses were these patterns).
_NOISE_CONTENT_PREFIXES: tuple[str, ...] = (
    "This session is being continued from a previous conversation",
    "Base directory for this skill:",
    "In the repo at /",
    "[Request interrupted by user",
    # Generic subagent/spawn role prompts. Subagents and `claude -p`
    # children commonly open with one of these. They're never user-
    # intent signals — they're task framing injected by the parent.
    "You are the ",
    "You are a ",
    "You are an ",
    "Research task",
    "Design task",
    "Context:",
)


# Verbatim-specific minimum length. The global 30-char floor is too
# permissive for "I want X"-style user_want matches — short fragments
# like "[Request interrupted by user for tool use]" (39 chars) and
# CLI metadata strings sneak through. 50 chars is the empirical
# threshold below which user_want matches are almost always noise.
_VERBATIM_MIN_LEN = 50


def _is_log_content(text: str) -> bool:
    """Heuristic: high density of pass/fail/checkmark glyphs → this is
    a test runner / CI log output, not natural-language signal.

    Returns True if marker count ≥ 3 in the first 2KB of content. Logs
    from Detox / Maestro / mocha / pytest etc. trigger user_want false
    matches on lines like "✓ runFixture:registerUser → $ahmed (2.0s)"
    and entire blocks dump into a single dialog_message turn.
    """
    if len(text) < 100:
        return False
    sample = text[:2000]
    markers = ("✓", "✗", "[OK]", "[FAIL]", "PASS", "FAIL", "skipped",
               "runFixture:")
    return sum(sample.count(m) for m in markers) >= 3


def _candidate_exists(conn, source_uuid, content):
    """True if this source message or identical content was already
    enqueued — in ANY state, INCLUDING 'rejected'.

    Rejected MUST count. The extract daemon re-scans overlapping time
    windows, so a rejected candidate left out of the dedup gets
    re-harvested on the very next pass: the same heuristic trips the same
    noise and the reviewer re-rejects it, forever. (Confirmed in prod:
    #158 was a byte-identical re-harvest of #157 ~19m after rejection.)

    Content match uses a 500-char prefix on BOTH sides. `_enqueue` stores
    up to 2000-4000 chars, so the old `content = content[:500]` compared a
    full stored value against a 500-char key and never matched for any
    candidate longer than 500 chars — the content fallback was dead, and
    only source_uuid (which the rejected-status gap then bypassed) kept
    anything out.
    """
    if source_uuid:
        if conn.execute(
            "SELECT 1 FROM extract_candidates WHERE source_uuid=? LIMIT 1",
            (source_uuid,),
        ).fetchone():
            return True
    return bool(
        conn.execute(
            "SELECT 1 FROM extract_candidates "
            "WHERE substr(content, 1, 500) = ? LIMIT 1",
            (content[:500],),
        ).fetchone()
    )


def _enqueue(conn, kind, source_uuid, source_cid, content, rationale):
    if _candidate_exists(conn, source_uuid, content):
        return None
    cur = conn.execute(
        "INSERT INTO extract_candidates (kind, source_uuid, source_cid, "
        "content, rationale, status, created_at) VALUES (?,?,?,?,?,?,?)",
        (kind, source_uuid, source_cid, content, rationale,
         "pending", int(time.time())),
    )
    return cur.lastrowid


@mcp.tool()
def extract_recent(window_min: int = 60, max_messages: int = 500) -> str:
    """Scan recent dialog_messages and enqueue heuristic candidates.

    H1 user_want         → verbatim (normative phrasing)
    H2 long_insight      → distill (assistant ≥500ch + ## headers + conclusion marker)
    H3 example_regularity→ concept (bullets≥3 OR example-marker≥2 + abstract frame)
    H4 paraphrase_repeat → note (≥3 msgs cosine ≥0.80 within same session)"""
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    cutoff = now - max(1, int(window_min)) * 60
    # Exclude our own internal-prompted child sessions (shadow_review
    # observer + close_thread auto-reviewer + curator) — otherwise their
    # promp text becomes extract candidates, the same self-pollution
    # we fixed in shadow_review._collect_window.
    from ..shadow_review import (
        _INTERNAL_PROMPT_PREFIXES,
        _SPAWNED_SESSION_MARKERS,
    )
    # Session-level filter — drop entire sessions started by our own
    # spawn prompts.
    sess_prefix_clauses = " OR ".join(
        ["substr(content, 1, ?) = ?"] * len(_INTERNAL_PROMPT_PREFIXES)
    )
    sess_prefix_params: list = []
    for p in _INTERNAL_PROMPT_PREFIXES:
        sess_prefix_params.extend([len(p), p])
    spawned_marker_clauses = " OR ".join(
        ["instr(content, ?) > 0"] * len(_SPAWNED_SESSION_MARKERS)
    )
    spawned_marker_params = list(_SPAWNED_SESSION_MARKERS)
    # Message-level filter — drop individual noise messages (compaction
    # summaries, SKILL injections, subagent prompts) inside otherwise
    # valid sessions. SQL LIKE with '%' suffix on user-controlled prefix
    # is safe: each pattern is a literal in source code, not user input.
    msg_noise_clauses = " AND ".join(
        [f"content NOT LIKE ?"] * len(_NOISE_CONTENT_PREFIXES)
    )
    msg_noise_params = [p + "%" for p in _NOISE_CONTENT_PREFIXES]
    # Session-level filter #2 — drop sessions that ARE one of our spawned
    # children (their cid appears as tasks.spawned_cid). The prompt-prefix
    # list above only catches children whose opening line is a known
    # marker; spawned agents open with arbitrary task framing ("You are
    # auditing…", "You are analyzing whether…", "Use the Write tool to…"),
    # so 66/107 historical rejects were spawned-child noise that slipped
    # past the prefix list. The tasks.spawned_cid link identifies them
    # regardless of wording. Mirrors ingest._is_spawned_child_session.
    rows = conn.execute(
        "SELECT uuid, role, content, session_id, created_at, embedding "
        "FROM dialog_messages WHERE created_at >= ? "
        "AND coalesce(project, '') != 'subagents' "
        "AND role IN ('user','assistant') "
        "AND content NOT LIKE '[tool_result]%' AND content NOT LIKE '[Image%' "
        f"AND {msg_noise_clauses} "
        "AND length(content) >= 30 "
        "AND session_id NOT IN ("
        "  SELECT DISTINCT session_id FROM dialog_messages "
        f"  WHERE session_id IS NOT NULL AND role = 'user' AND ({sess_prefix_clauses})"
        ") "
        "AND session_id NOT IN ("
        "  SELECT DISTINCT session_id FROM dialog_messages "
        f"  WHERE session_id IS NOT NULL AND role = 'user' AND ({spawned_marker_clauses})"
        ") "
        "AND session_id NOT IN ("
        "  SELECT spawned_cid FROM tasks WHERE spawned_cid IS NOT NULL"
        ") "
        "ORDER BY created_at ASC LIMIT ?",
        (cutoff, *msg_noise_params, *sess_prefix_params,
         *spawned_marker_params,
         max(10, int(max_messages))),
    ).fetchall()
    if not rows:
        return f"no_dialog window={window_min}m"
    counts = {"verbatim": 0, "distill": 0, "concept": 0, "note": 0}
    skipped = 0
    for r in rows:
        uuid, cid, content, role = (
            r["uuid"], r["session_id"], r["content"], r["role"]
        )
        # Test-runner / CI log dumps trigger user_want false matches on
        # log labels like "Earnings Withdrawal → ✓ runFixture:…"
        if _is_log_content(content):
            skipped += 1
            continue
        if (role == "user" and _WANT_RE.search(content)
                and len(content) >= _VERBATIM_MIN_LEN):
            res = _enqueue(conn, "verbatim", uuid, cid, content[:2000],
                           "H1 user_want pattern")
            if res:
                counts["verbatim"] += 1
            else:
                skipped += 1
        if role == "assistant":
            if (len(content) >= 500 and _HEADER_RE.search(content)
                    and _INSIGHT_MARKERS_RE.search(content)):
                res = _enqueue(conn, "distill", uuid, cid, content[:4000],
                               "H2 long_insight (headers + conclusion marker)")
                if res:
                    counts["distill"] += 1
                else:
                    skipped += 1
            bullets = len(_BULLET_RE.findall(content))
            examples = len(_EXAMPLE_RE.findall(content))
            if (bullets >= 3 or examples >= 2) and _FRAME_RE.search(content):
                res = _enqueue(
                    conn, "concept", uuid, cid, content[:3000],
                    f"H3 example_regularity (bullets={bullets}, examples={examples})",
                )
                if res:
                    counts["concept"] += 1
                else:
                    skipped += 1
    if SEMANTIC_AVAILABLE:
        try:
            import numpy as _np  # type: ignore
        except ImportError:
            _np = None
        if _np is not None:
            with_emb = [r for r in rows if r["embedding"]]
            by_sess: dict = {}
            for r in with_emb:
                by_sess.setdefault(r["session_id"] or "", []).append(r)
            for sid, msgs in by_sess.items():
                if len(msgs) < 3:
                    continue
                embs = _np.stack([
                    _np.frombuffer(m["embedding"], dtype="float32") for m in msgs
                ])
                sim = embs @ embs.T
                clustered = [False] * len(msgs)
                for i in range(len(msgs)):
                    if clustered[i]:
                        continue
                    members = [i]
                    for j in range(i + 1, len(msgs)):
                        if not clustered[j] and sim[i, j] >= 0.80:
                            members.append(j)
                    if len(members) >= 3:
                        for k in members:
                            clustered[k] = True
                        sub = sim[_np.ix_(members, members)]
                        rep_idx = members[int(_np.argmax(sub.mean(axis=1)))]
                        rep = msgs[rep_idx]
                        member_uuids = sorted(msgs[k]["uuid"] for k in members)
                        cluster_key = "cluster:" + ",".join(
                            u[:8] for u in member_uuids[:6]
                        )
                        if conn.execute(
                            "SELECT 1 FROM extract_candidates WHERE source_uuid=? "
                            "AND status IN ('pending','accepted')",
                            (cluster_key,),
                        ).fetchone():
                            skipped += 1
                            continue
                        conn.execute(
                            "INSERT INTO extract_candidates (kind, source_uuid, "
                            "source_cid, content, rationale, status, created_at) "
                            "VALUES (?,?,?,?,?,?,?)",
                            ("note", cluster_key, sid, rep["content"][:2000],
                             f"H4 paraphrase_repeat n={len(members)} "
                             f"sess={sid[:8]} centroid={rep['uuid'][:8]}",
                             "pending", now),
                        )
                        counts["note"] += 1
    _emit(conn, "extract_recent",
          summary=" ".join(f"{k}={v}" for k, v in counts.items()))
    conn.commit()
    return (
        f"ok window={window_min}m scanned={len(rows)} "
        f"verbatim={counts['verbatim']} distill={counts['distill']} "
        f"concept={counts['concept']} note={counts['note']} "
        f"skipped_existing={skipped}"
    )


@mcp.tool()
def review_candidates(status: str = "pending", k: int = 20) -> str:
    """status ∈ {pending, accepted, rejected, all}. Newest first."""
    valid = ("pending", "accepted", "rejected", "all")
    if status not in valid:
        return f"ERR bad_status={status}"
    conn = get_db()
    sql = (
        "SELECT id, kind, source_uuid, source_cid, content, rationale, "
        "status, created_at FROM extract_candidates "
    )
    if status == "all":
        rows = conn.execute(
            sql + "ORDER BY created_at DESC LIMIT ?", (max(1, int(k)),)
        ).fetchall()
    else:
        rows = conn.execute(
            sql + "WHERE status=? ORDER BY created_at DESC LIMIT ?",
            (status, max(1, int(k))),
        ).fetchall()
    if not rows:
        return f"no_candidates status={status}"
    now = int(time.time())
    out = [f"candidates n={len(rows)} status={status}"]
    for r in rows:
        snip = r["content"][:240].replace("\n", " ")
        if len(r["content"]) > 240:
            snip += "…"
        out.append(
            f"  #{r['id']} {r['kind']} cid={(r['source_cid'] or '-')[:8]} "
            f"age={fmt_age(now - r['created_at'])}_ago"
        )
        out.append(f"    why={r['rationale'] or '?'}")
        out.append(f"    {q(snip)}")
    return "\n".join(out)


_VALID_TARGET_KINDS = ("note", "concept", "distill", "verbatim")


@mcp.tool()
def accept_candidate(id: int, target_kind: str = "",
                     thread_id: str = "") -> str:
    """Materialize candidate into its target table.
    target_kind overrides candidate's kind. thread_id optional."""
    conn = get_db()
    _ensure_session(conn)
    r = conn.execute(
        "SELECT * FROM extract_candidates WHERE id=?", (int(id),)
    ).fetchone()
    if not r:
        return f"ERR candidate_not_found={id}"
    if r["status"] != "pending":
        return f"ERR not_pending status={r['status']}"
    kind = (target_kind or r["kind"]).strip()
    if kind not in _VALID_TARGET_KINDS:
        return f"ERR bad_target_kind={kind}"
    tid = thread_id.strip() or None
    if tid and not conn.execute(
        "SELECT 1 FROM threads WHERE id=?", (tid,)
    ).fetchone():
        return f"ERR thread_not_found={tid}"
    now = int(time.time())
    content = r["content"]
    placed = ""
    if kind == "verbatim":
        cur = conn.execute(
            "INSERT INTO verbatim (speaker, content, thread_id, created_at, "
            "session_id) VALUES (?,?,?,?,?)",
            ("user", content, tid, now, identity._session_id),
        )
        placed = f"verbatim id={cur.lastrowid}"
    elif kind == "note":
        emb = _embed(content)
        cur = conn.execute(
            "INSERT INTO notes (thread_id, content, kind, created_at, "
            "session_id, embedding, embed_backend) VALUES (?,?,?,?,?,?,?)",
            (tid, content, "insight", now, identity._session_id, emb, embed_tag(emb)),
        )
        placed = f"note id={cur.lastrowid} thread={tid or '-'}"
    elif kind == "concept":
        pid = gen_concept_id(conn)
        cid = _detect_self_cid()
        conn.execute(
            "INSERT INTO concepts (id, description, triangulation_notes, "
            "confidence, source_thread, registered_by_cid, registered_at, "
            "last_evidence_at) VALUES (?,?,?,?,?,?,?,?)",
            (pid, content, r["rationale"], "low", tid, cid, now, now),
        )
        placed = f"concept id={pid}"
    elif kind == "distill":
        pid = gen_distill_id(conn)
        cid = _detect_self_cid()
        conn.execute(
            "INSERT INTO distill (id, content, kind, confidence, "
            "source_thread, source_cid, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (pid, content, "insight", "medium", tid, cid, now),
        )
        if cid:
            conn.execute(
                "INSERT INTO distill_votes (distill_id, voter_cid, weight, "
                "voted_at) VALUES (?,?,?,?)",
                (pid, cid, 1.0, now),
            )
            conn.execute(
                "UPDATE distill SET vote_sum=1.0, vote_count=1 WHERE id=?",
                (pid,),
            )
        placed = f"distill id={pid}"
    conn.execute(
        "UPDATE extract_candidates SET status='accepted', decided_at=? "
        "WHERE id=?",
        (now, int(id)),
    )
    _emit(conn, f"accept_candidate:{kind}", target=str(id), summary=placed)
    conn.commit()
    return f"ok accepted #{id} → {placed}"


@mcp.tool()
def reject_candidate(id: int, reason: str = "") -> str:
    """Mark rejected. Reason appended to rationale for heuristic tuning."""
    conn = get_db()
    _ensure_session(conn)
    r = conn.execute(
        "SELECT id, rationale, status FROM extract_candidates WHERE id=?",
        (int(id),),
    ).fetchone()
    if not r:
        return f"ERR candidate_not_found={id}"
    if r["status"] != "pending":
        return f"ERR not_pending status={r['status']}"
    now = int(time.time())
    new_r = r["rationale"] or ""
    if reason:
        new_r = (new_r + f" | rejected: {reason}").lstrip(" |")[:500]
    conn.execute(
        "UPDATE extract_candidates SET status='rejected', decided_at=?, "
        "rationale=? WHERE id=?",
        (now, new_r, int(id)),
    )
    _emit(conn, "reject_candidate", target=str(id), summary=reason)
    conn.commit()
    return f"ok rejected #{id}"
