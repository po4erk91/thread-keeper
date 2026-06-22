"""Extract daemon — periodic auto-harvest of decision-shaped utterances
into the extract_candidates queue.

The daemon thread is a thin wrapper around extract_recent() with a
cursor in events.kind='extract_pass'. We test the wrapper and the
internal-session filter we added to extract_recent for self-pollution
guarding.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest


_FAKE_CID = "aaaa1111-2222-3333-4444-555566667777"


def _bootstrap(tmp_path, monkeypatch, interval="0", window_min="30"):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_INGEST_CAP": "0",
        "THREADKEEPER_SKILL_WATCH_INTERVAL_S": "0",
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_SEARCH_PROXY_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_CURATOR_INTERVAL_S": "0",
        "THREADKEEPER_EXTRACT_INTERVAL_S": interval,
        "THREADKEEPER_EXTRACT_WINDOW_MIN": window_min,
        "THREADKEEPER_LESSONS": str(tmp_path / "lessons.md"),
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
        "THREADKEEPER_CLIENT": "pytest",
        "THREADKEEPER_FORCE_CID": _FAKE_CID,
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import db, extract_daemon, identity
    return {
        "db": db,
        "extract_daemon": extract_daemon,
        "identity": identity,
    }


def _seed_dialog(conn, role, content, ts, session_id="user-sess",
                 embedding=None):
    uid = f"u-{ts}-{role}-{abs(hash(content)) % 100000}"
    conn.execute(
        "INSERT INTO dialog_messages (uuid, source, project, session_id, "
        "role, content, model, created_at, embedding) "
        "VALUES (?, 'claude-code', 'p1', ?, ?, ?, ?, ?, ?)",
        (uid, session_id, role, content, "test-model", ts, embedding),
    )
    return uid


# ──────────────────────────────────────────────────────────────────────
# Pure functions: cursor advance
# ──────────────────────────────────────────────────────────────────────

def test_cursor_initial_is_zero(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    assert pkg["extract_daemon"]._last_extract_ts(conn) == 0


def test_cursor_advances_after_pass(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    pkg["extract_daemon"]._record_extract_pass(conn, 12345, "no_dialog window=30m")
    pkg["extract_daemon"]._record_extract_pass(conn, 67890, "ok scanned=5")
    assert pkg["extract_daemon"]._last_extract_ts(conn) == 67890


# ──────────────────────────────────────────────────────────────────────
# run_extract_pass dispatch
# ──────────────────────────────────────────────────────────────────────

def test_run_extract_pass_disabled_without_force(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)  # interval=0 → disabled
    assert pkg["extract_daemon"].run_extract_pass() == "disabled"


def test_run_extract_pass_advances_cursor_when_forced(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)  # interval=0, but forced
    out = pkg["extract_daemon"].run_extract_pass(force=True)
    # extract_recent returns "no_dialog window=30m" when nothing seeded
    assert "no_dialog" in out
    conn = pkg["db"].get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='extract_pass'"
    ).fetchone()[0]
    assert n == 1


def test_run_extract_pass_picks_up_user_want_pattern(tmp_path, monkeypatch):
    """Seed a 'want' utterance, run extract, confirm a verbatim
    candidate was enqueued."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    _seed_dialog(
        conn, "user",
        "I want you to always verify the state transition, not just "
        "the destination label being visible — that's the false-"
        "positive trap we keep hitting.",
        now - 60, session_id="real-sess",
    )
    conn.commit()

    out = pkg["extract_daemon"].run_extract_pass(force=True)
    assert "ok" in out
    # Candidate must be queued
    rows = conn.execute(
        "SELECT kind, content FROM extract_candidates WHERE status='pending'"
    ).fetchall()
    kinds = {r["kind"] for r in rows}
    assert "verbatim" in kinds, f"got kinds: {kinds}"


def test_extract_filters_noise_content_prefixes(tmp_path, monkeypatch):
    """Message-level noise filter — even within a valid session,
    individual messages matching known noise prefixes (compaction
    summary, SKILL.md injection, subagent prompt, interrupt service
    string) must NOT become extract candidates."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    # Real session with mixed valid + noise messages
    _seed_dialog(
        conn, "user",
        "I want you to always run tests before merging — that's the "
        "policy I expect every CI gate to enforce.",
        now - 120, session_id="real-sess",
    )
    # Compaction summary — must be skipped
    _seed_dialog(
        conn, "user",
        "This session is being continued from a previous conversation "
        "that ran out of context. The summary below covers the earlier "
        "portion of the conversation.",
        now - 100, session_id="real-sess",
    )
    # SKILL.md injection — must be skipped
    _seed_dialog(
        conn, "user",
        "Base directory for this skill: /Users/dmytro/.claude/skills/"
        "e2e-walk-flow-manually-before-runner-iteration",
        now - 80, session_id="real-sess",
    )
    # Subagent task prompt — must be skipped
    _seed_dialog(
        conn, "user",
        "In the repo at /Users/dmytro/masthead, find all code related "
        "to the content translation language switch limit.",
        now - 60, session_id="real-sess",
    )
    # Interrupt service string — must be skipped (also short, but
    # prefix filter catches it independent of length)
    _seed_dialog(
        conn, "user",
        "[Request interrupted by user for tool use]",
        now - 40, session_id="real-sess",
    )
    conn.commit()

    pkg["extract_daemon"].run_extract_pass(force=True)
    rows = conn.execute(
        "SELECT content FROM extract_candidates WHERE status='pending'"
    ).fetchall()
    # Real "I want you to" gets through
    assert any("policy I expect" in r["content"] for r in rows)
    # None of the noise rows enqueued
    for noise in (
        "This session is being continued",
        "Base directory for this skill",
        "In the repo at /Users/",
        "[Request interrupted by user",
    ):
        assert not any(noise in r["content"] for r in rows), (
            f"noise leaked: {noise!r}"
        )


def test_extract_filters_subagent_role_prompts(tmp_path, monkeypatch):
    """Subagent / spawn-child role prompts ('You are the X', 'Research
    task', 'Design task', 'Context:') aren't user intent — they're
    parent-injected task framing that polluted the first calibration
    pass. Filter them at the message level."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    # Valid user_want
    _seed_dialog(
        conn, "user",
        "I want you to never run destructive operations without "
        "confirmation in production.",
        now - 200, session_id="real-sess",
    )
    # Subagent role prompts — each must be filtered
    for i, prompt in enumerate([
        "You are the news-arc curator for the-masthead. You have "
        "THREE jobs in this run.",
        "You are a researcher. Analyze the codebase and identify the "
        "module responsible for proxy state.",
        "You are an editor with strict formatting rules. Rewrite the "
        "passage below as a numbered list.",
        "Research task. Read /Users/dmytro/ai-memory/server.py and "
        "report the top-level functions.",
        "Design task for memory-partner. Sketch the schema migration "
        "plan, do not implement.",
        "Context: This summary will be shown in a list to help users "
        "choose which conversations are relevant.",
    ]):
        _seed_dialog(
            conn, "user", prompt, now - (180 - i * 20),
            session_id="real-sess",
        )
    conn.commit()

    pkg["extract_daemon"].run_extract_pass(force=True)
    rows = conn.execute(
        "SELECT content FROM extract_candidates WHERE status='pending'"
    ).fetchall()
    # Valid stays
    assert any("never run destructive" in r["content"] for r in rows)
    # All subagent prompts filtered
    for noise in (
        "You are the news-arc curator",
        "You are a researcher",
        "You are an editor",
        "Research task",
        "Design task for memory-partner",
        "Context: This summary",
    ):
        assert not any(noise in r["content"] for r in rows), (
            f"subagent prompt leaked: {noise!r}"
        )


def test_extract_skips_short_user_want_matches(tmp_path, monkeypatch):
    """Verbatim-specific min length — '[Request interrupted by user for
    tool use]' is 42 chars; even if its prefix didn't match the noise
    list, the length floor would catch it. Conversely, a 50+-char
    legitimate `I want you to ...` line gets through."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    # Just-above-min-length valid user_want
    _seed_dialog(
        conn, "user",
        "I want you to write down the WDA reset rule for sure.",
        now - 60, session_id="ok-sess",
    )
    # Just-below-min-length: 40 chars total, still has 'i want to'
    _seed_dialog(
        conn, "user",
        "I want to break for lunch.",
        now - 40, session_id="ok-sess",
    )
    conn.commit()
    pkg["extract_daemon"].run_extract_pass(force=True)
    rows = conn.execute(
        "SELECT content FROM extract_candidates WHERE status='pending'"
    ).fetchall()
    assert any("WDA reset rule" in r["content"] for r in rows)
    assert not any("break for lunch" in r["content"] for r in rows)


def test_extract_skips_test_runner_log_dumps(tmp_path, monkeypatch):
    """Log dumps with checkmark/cross density should not yield candidates
    — even when they contain words matching _WANT_RE inside log lines."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    log_dump = (
        "Earnings Withdrawal — Direct Deposit (OAuth) with Wallet V2 "
        "lambdas (NTV601)\n"
        "  ✓ runFixture:registerUser → $ahmed (2.0s)\n"
        "  ✓ runFixture:fundWallet → $ahmed-funded (1.2s)\n"
        "  ✓ find labelContains 'Add payout method' → $addMethod\n"
        "  ✗ tap $addMethod (no element) → I want to retry but "
        "got blocked\n"
        "  ○ find labelContains 'Congrats' → $proveCongrats skipped\n"
    )
    _seed_dialog(conn, "user", log_dump, now - 60, session_id="ok-sess")
    # Plus a clean valid utterance in same session
    _seed_dialog(
        conn, "user",
        "I want you to never retry a failing tap automatically, "
        "always surface the error to me first.",
        now - 30, session_id="ok-sess",
    )
    conn.commit()
    pkg["extract_daemon"].run_extract_pass(force=True)
    rows = conn.execute(
        "SELECT content FROM extract_candidates WHERE status='pending'"
    ).fetchall()
    assert any("never retry a failing tap" in r["content"] for r in rows)
    assert not any("runFixture:registerUser" in r["content"] for r in rows)


def test_extract_filters_shadow_observer_sessions(tmp_path, monkeypatch):
    """Shadow-observer's own session start with the marker prompt — any
    seemingly substantive utterance INSIDE that session should NOT
    become an extract candidate (same self-pollution rule as
    shadow_review._collect_window)."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    # Shadow-observer child's session — entire session must be skipped
    _seed_dialog(
        conn, "user",
        "You are a SHADOW LEARNING OBSERVER for thread-keeper. You "
        "read a slice of recent dialog…",
        now - 90, session_id="shadow-sess",
    )
    _seed_dialog(
        conn, "assistant",
        "I want to highlight a rule: always reset network before WDA "
        "start. That's a class-level recovery procedure.",
        now - 85, session_id="shadow-sess",
    )
    # Real user session — should be picked up
    _seed_dialog(
        conn, "user",
        "I want you to record decision notes automatically without "
        "waiting for the agent to remember.",
        now - 60, session_id="real-sess",
    )
    conn.commit()

    out = pkg["extract_daemon"].run_extract_pass(force=True)
    assert "ok" in out
    rows = conn.execute(
        "SELECT source_cid, content FROM extract_candidates "
        "WHERE status='pending'"
    ).fetchall()
    # Real session yielded a candidate
    assert any(r["source_cid"] == "real-sess" for r in rows)
    # Shadow session yielded NONE
    assert not any(r["source_cid"] == "shadow-sess" for r in rows)


def test_extract_filters_curator_and_candidate_children(tmp_path, monkeypatch):
    """Curator + candidate-reviewer are DAEMONS, not spawn() children — their
    sessions link into tasks.spawned_cid unreliably, so the spawned_cid
    exclusion alone misses them. But their prompt openers are fixed, so the
    prompt-prefix filter (_INTERNAL_PROMPT_PREFIXES) catches them with no
    tasks-row dependency. Was the single biggest reject class in the live
    ledger (54 curator + others of 126 rejects). Verifies BOTH daemon-child
    sessions are fully excluded while a real user session survives."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    # Curator child — NOT registered in tasks at all (no spawned_cid link),
    # so only the prompt-prefix filter can catch it.
    _seed_dialog(
        conn, "user",
        "You are an autonomous CURATOR for thread-keeper's lessons + "
        "skills library. You read the inventory below…",
        now - 120, session_id="curator-sess",
    )
    _seed_dialog(
        conn, "assistant",
        "## Findings\n\nWe want the pipeline to always dedup first. "
        "Therefore the durable rule for every future run is dedup-before-"
        "enrich, in conclusion.",
        now - 115, session_id="curator-sess",
    )
    # Candidate-reviewer child — same deal.
    _seed_dialog(
        conn, "user",
        "You are a CANDIDATE REVIEWER for thread-keeper's extract queue. "
        "For each candidate decide…",
        now - 100, session_id="candrev-sess",
    )
    _seed_dialog(
        conn, "assistant",
        "I want to record the rule: always verify the ticket before "
        "closing. That is a class-level policy worth keeping.",
        now - 95, session_id="candrev-sess",
    )
    # Real user session — must survive.
    _seed_dialog(
        conn, "user",
        "I want you to auto-record decisions without me asking each time, "
        "as a standing rule for this project.",
        now - 60, session_id="real-sess",
    )
    conn.commit()

    out = pkg["extract_daemon"].run_extract_pass(force=True)
    assert "ok" in out
    cids = [r["source_cid"] for r in conn.execute(
        "SELECT source_cid FROM extract_candidates WHERE status='pending'"
    ).fetchall()]
    assert "real-sess" in cids, "real user session should yield a candidate"
    assert "curator-sess" not in cids, "curator child must be excluded"
    assert "candrev-sess" not in cids, "candidate-reviewer child must be excluded"


def test_extract_filters_spawned_child_sessions(tmp_path, monkeypatch):
    """A session whose cid is a tasks.spawned_cid is one of OUR spawned
    children (curator, panel voter, ad-hoc research agent, ...). Its dialog
    is system-injected task framing + work artifacts, never user intent —
    exclude it wholesale, regardless of how its prompt opens. This catches
    the noise the prompt-prefix list misses: real rejects included children
    opening with 'You are auditing…', 'You are analyzing whether…',
    'Use the Write tool to…' — none matched _INTERNAL_PROMPT_PREFIXES, so
    66/107 historical rejects were exactly this class."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    child_cid = "child-cid-xyz"
    # Register the child in tasks (parent spawned it). Prompt text is
    # deliberately NOT in any prefix list — the link is what identifies it.
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, spawned_cid, cwd, prompt, "
        "started_at) VALUES ('tk_x', 0, 'parent-cid', ?, '/x', "
        "'You are auditing a slice of lessons. Analyze each one.', ?)",
        (child_cid, now - 200),
    )
    # The child emits substantive-looking dialog that WOULD trip H1/H2/H3.
    _seed_dialog(
        conn, "user",
        "I want you to record the decision: always reset the network "
        "before WDA start, every single run.",
        now - 90, session_id=child_cid,
    )
    _seed_dialog(
        conn, "assistant",
        "## Findings\n\nWe want the pipeline to always dedup first.\n"
        "Therefore the rule is: dedup before enrich. In conclusion, that "
        "is the durable pattern here for every future run of this job.",
        now - 85, session_id=child_cid,
    )
    # A genuine foreground user session — must still be picked up.
    _seed_dialog(
        conn, "user",
        "I want you to record decision notes automatically without "
        "waiting for the agent to remember each time.",
        now - 60, session_id="real-sess",
    )
    conn.commit()

    out = pkg["extract_daemon"].run_extract_pass(force=True)
    assert "ok" in out
    rows = conn.execute(
        "SELECT source_cid FROM extract_candidates WHERE status='pending'"
    ).fetchall()
    assert any(r["source_cid"] == "real-sess" for r in rows), \
        "real user session should still yield candidates"
    assert not any(r["source_cid"] == child_cid for r in rows), \
        "spawned-child session must be fully excluded"


def test_extract_filters_codex_spawned_marker_without_task_link(
    tmp_path, monkeypatch,
):
    """Codex child transcript session_id is the rollout UUID, not always the
    forced child cid stored in tasks.spawned_cid. The spawn preamble still
    identifies the whole session as agent work."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    codex_rollout = "019eb584-bee1-72a3-ada5-89eaaeab8b8f"
    _seed_dialog(
        conn,
        "user",
        "You were spawned in the background by parent conversation abc. "
        "Your own cid is child-xyz.",
        now - 100,
        session_id=codex_rollout,
    )
    _seed_dialog(
        conn,
        "user",
        "I want you to record the fake child rule as a durable preference.",
        now - 90,
        session_id=codex_rollout,
    )
    _seed_dialog(
        conn,
        "assistant",
        "## Findings\n\nWe want every future run to keep this child output. "
        "Therefore this is a durable rule. In conclusion, save it.",
        now - 85,
        session_id=codex_rollout,
    )
    _seed_dialog(
        conn,
        "user",
        "I want you to record real foreground decisions automatically "
        "when I state them as standing rules.",
        now - 60,
        session_id="real-sess",
    )
    conn.commit()

    out = pkg["extract_daemon"].run_extract_pass(force=True)
    assert "ok" in out
    rows = conn.execute(
        "SELECT source_cid FROM extract_candidates WHERE status='pending'"
    ).fetchall()
    assert any(r["source_cid"] == "real-sess" for r in rows)
    assert not any(r["source_cid"] == codex_rollout for r in rows)


def test_extract_h4_rejected_cluster_not_reharvested(tmp_path, monkeypatch):
    """H4 paraphrase-cluster path must share the rejected-counting dedup of
    H1/H2/H3. A cluster keyed by its deterministic cluster_key, once
    rejected, must NOT be re-enqueued when the daemon re-scans the
    overlapping window — the #157/#158 prod re-harvest loop, on the one
    heuristic path that bypassed _candidate_exists (issue #62)."""
    np = pytest.importorskip("numpy")
    pkg = _bootstrap(tmp_path, monkeypatch)
    # H4 only runs when semantic clustering is available; force it on so the
    # test is independent of the ambient THREADKEEPER_NO_EMBEDDINGS setting.
    monkeypatch.setattr("threadkeeper.tools.extract.SEMANTIC_AVAILABLE", True)
    conn = pkg["db"].get_db()
    now = int(time.time())
    # Identical unit-norm embeddings → pairwise cosine 1.0 ≥ 0.80, so the
    # three paraphrases collapse into one H4 note cluster. The texts differ
    # but none trips H1/H2/H3 (assistant role, short, no headers/bullets).
    vec = np.ones(8, dtype=np.float32)
    vec /= np.linalg.norm(vec)
    emb = vec.tobytes()
    for i, text in enumerate([
        "We should retry transient deploy failures before paging anyone.",
        "Transient deploy errors ought to be retried prior to alerting.",
        "Retry transient failures in the deploy step before sending a page.",
    ]):
        _seed_dialog(
            conn, "assistant", text, now - (300 - i * 10),
            session_id="cluster-sess", embedding=emb,
        )
    conn.commit()

    # First pass enqueues exactly one H4 cluster candidate.
    out = pkg["extract_daemon"].run_extract_pass(force=True)
    assert "ok" in out, out
    rows = conn.execute(
        "SELECT id FROM extract_candidates WHERE kind='note' "
        "AND source_uuid LIKE 'cluster:%'"
    ).fetchall()
    assert len(rows) == 1, f"expected one H4 cluster row, got {len(rows)}"
    cluster_id = rows[0]["id"]

    # Reviewer rejects it.
    conn.execute(
        "UPDATE extract_candidates SET status='rejected' WHERE id=?",
        (cluster_id,),
    )
    conn.commit()

    # Daemon re-scans the overlapping window: same messages → same
    # deterministic cluster_key. The rejected row must suppress re-harvest.
    pkg["extract_daemon"].run_extract_pass(force=True)
    rows = conn.execute(
        "SELECT status FROM extract_candidates WHERE kind='note' "
        "AND source_uuid LIKE 'cluster:%'"
    ).fetchall()
    assert len(rows) == 1, f"rejected H4 cluster re-harvested: {len(rows)} rows"
    assert rows[0]["status"] == "rejected"


# ──────────────────────────────────────────────────────────────────────
# Daemon lifecycle
# ──────────────────────────────────────────────────────────────────────

def test_daemon_does_not_start_in_slim_child(tmp_path, monkeypatch):
    """Slim children (SEMANTIC_AVAILABLE=False) must refuse to start
    the extract daemon — otherwise each spawn cascades into N more."""
    monkeypatch.setenv("THREADKEEPER_EXTRACT_INTERVAL_S", "600")
    pkg = _bootstrap(tmp_path, monkeypatch, interval="600")
    import threadkeeper.config as cfg
    monkeypatch.setattr(cfg, "SEMANTIC_AVAILABLE", False)
    pkg["extract_daemon"]._started = False
    pkg["extract_daemon"].start_extract_daemon()
    assert pkg["extract_daemon"]._started is False, (
        "slim child must refuse to start extract daemon"
    )


def test_daemon_disabled_at_interval_zero(tmp_path, monkeypatch):
    """interval=0 (default) → no daemon thread started."""
    pkg = _bootstrap(tmp_path, monkeypatch, interval="0")
    pkg["extract_daemon"]._started = False
    pkg["extract_daemon"].start_extract_daemon()
    assert pkg["extract_daemon"]._started is False
