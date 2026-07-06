"""Probe daemon — periodic isolated self-tests of known weak spots.

Drives the probe loop that was defined but never run: `register_probe`
created 12 probes, but `run_probe` / `record_attempt` are a MANUAL cycle
nobody triggered, so `probe_results` and `reliability` stayed empty and
the brief shows every category as `never_tested`.

Design:
  - WHY a child, not the main session: an isolated, context-free child
    focused solely on the probe task is a clean capability measurement,
    uncontaminated by whatever the parent conversation was doing.
  - WHY parent-graded: `run_probe` leaks the expected pattern (`expect=…`),
    and a model that has seen the answer key can't honestly self-grade.
    So the child only ATTEMPTS (bare prompt, no key) and writes its raw
    answer to a file; the PARENT grades it mechanically via _grade_probe.
  - WHICH probes: only OBJECTIVE graders (regex / exact WITH a pattern).
    `manual`-grader probes have no mechanical answer key — they stay on
    the manual run_probe loop and are NOT driven here.

Two-phase, non-blocking (mirrors shadow_review's fire-and-forget):
  each tick first GRADES any answer files left by a child that finished
  since last tick, then — if no probe child is currently running — spawns
  the next due probe. So tick N's answer is graded at tick N+1.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from .config import PROBE_INTERVAL_S, PROBE_COOLDOWN_S, TASK_LOG_DIR
from .db import get_db
from . import daemon_state, identity
from .identity import _detect_self_cid, _emit
from .helpers import alive, single_flight_lock

logger = logging.getLogger(__name__)

_started = False

# First line of the prompt we inject into a probe child. Mirrors
# shadow_review._INTERNAL_PROMPT_PREFIXES so the child's own transcript is
# excluded from extract/shadow windows when it gets ingested back.
PROBE_PROMPT_PREFIX = "You are a PROBE RUNNER"


def _answer_dir() -> Path:
    d = TASK_LOG_DIR / "probe-answers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _last_probe_ts(conn: sqlite3.Connection) -> int:
    """High-water timestamp of the most recent probe pass, or 0."""
    try:
        row = conn.execute(
            "SELECT target FROM events WHERE kind='probe_pass' "
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


def _record_probe_pass(conn: sqlite3.Connection, ts: int, outcome: str) -> None:
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, "
            "created_at) VALUES (?, 'probe_pass', ?, ?, ?)",
            (identity._session_id or "", str(ts), outcome[:300],
             int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("probe_daemon: failed to record pass", exc_info=True)


def _running_probe_children(conn: sqlite3.Connection) -> list[str]:
    """Running probe-runner task ids, reaping dead rows. Machine-wide
    single-flight guard: one probe child at a time across all servers."""
    try:
        rows = conn.execute(
            "SELECT id, pid FROM tasks WHERE ended_at IS NULL "
            "AND prompt LIKE ?",
            (PROBE_PROMPT_PREFIX + "%",),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    now = int(time.time())
    running: list[str] = []
    touched = False
    for r in rows:
        pid = int(r["pid"] or 0)
        if pid > 0 and not alive(pid):
            conn.execute(
                "UPDATE tasks SET ended_at=? WHERE id=? AND ended_at IS NULL",
                (now, r["id"]),
            )
            touched = True
            continue
        running.append(r["id"])
    if touched:
        conn.commit()
    return running


def _due_probes(conn: sqlite3.Connection, now_t: int) -> list[sqlite3.Row]:
    """Enabled OBJECTIVE probes (regex/exact + pattern) whose category has
    no probe_result inside the cooldown window, oldest-tested first."""
    try:
        return conn.execute(
            "SELECT * FROM probes p "
            "WHERE p.enabled = 1 "
            "  AND p.grader IN ('regex','exact') "
            "  AND p.expected_pattern IS NOT NULL AND p.expected_pattern != '' "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM probe_results r "
            "    WHERE r.category = p.category AND r.created_at >= ?"
            "  ) "
            "ORDER BY COALESCE("
            "  (SELECT MAX(created_at) FROM probe_results r2 "
            "   WHERE r2.category = p.category), 0) ASC, p.created_at ASC",
            (now_t - PROBE_COOLDOWN_S,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _record_probe_result(conn: sqlite3.Connection, probe: sqlite3.Row,
                         success: bool, note: str) -> None:
    """Write one probe_results row + refresh the reliability aggregate.
    Caller commits."""
    from .tools.probes import _recompute_reliability
    conn.execute(
        "INSERT INTO probe_results (probe_id, category, session_id, cid, "
        "success, latency_ms, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (probe["id"], probe["category"], identity._session_id,
         _detect_self_cid(), 1 if success else 0, None, note[:300],
         int(time.time())),
    )
    _recompute_reliability(conn, probe["category"])


def _grade_pending(conn: sqlite3.Connection) -> int:
    """Grade every answer file a finished probe child left behind, record
    the result, and delete the file. Returns the number graded."""
    from .tools.probes import _grade_probe
    graded = 0
    for f in sorted(_answer_dir().glob("*.txt")):
        probe_id = f.name.split("__", 1)[0]
        probe = conn.execute(
            "SELECT * FROM probes WHERE id=?", (probe_id,)
        ).fetchone()
        if not probe:
            f.unlink(missing_ok=True)  # orphan answer — nothing to grade
            continue
        try:
            answer = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        success = _grade_probe(
            probe["grader"], probe["expected_pattern"], answer
        )
        note = f"daemon-graded ans_len={len(answer)} ok={success}"
        _record_probe_result(conn, probe, success, note)
        graded += 1
        f.unlink(missing_ok=True)
    if graded:
        conn.commit()
    return graded


def _spawn_probe_child(probe: sqlite3.Row) -> str:
    """Spawn an isolated child to attempt one probe. Bare prompt (no answer
    key); the child writes ONLY its answer to a per-probe file we grade
    next tick."""
    token = secrets.token_hex(3)
    answer_path = _answer_dir() / f"{probe['id']}__{token}.txt"
    prompt = (
        f"{PROBE_PROMPT_PREFIX} for category '{probe['category']}'.\n\n"
        "Attempt the TASK below as accurately as you can. Then write ONLY "
        "your final answer — no preamble, no explanation, no markdown "
        f"fences — to this exact file using the Write tool:\n"
        f"  {answer_path}\n\n"
        "The file's entire contents must be just the answer the task asks "
        "for. Do not write anything else anywhere.\n\n"
        f"TASK:\n{probe['prompt']}"
    )
    from .tools.spawn import spawn  # late import — avoids import cycle
    return spawn(
        prompt=prompt,
        visible=False,
        capture_output=True,
        permission_mode="auto",
        role="probe_runner",
        write_origin="probe",
        slim=True,
        extra_allowed_tools="Write",
    )


def run_probe_pass(force: bool = False, *, scheduled: bool = False) -> str:
    """One probe pass. Phase 1: grade finished children's answers. Phase 2:
    spawn the next due probe (unless one is already running).

    Status strings (for observability / tests):
      'disabled'                     — knob off and not forced
      'not_due'                      — scheduled tick, another server already
                                       ran this loop within the interval
      'graded=N no_due'              — nothing due to spawn this tick
      'graded=N probe_child_running' — a probe child is still in flight
      'graded=N spawned …'           — launched the next probe child
      'graded=N spawn_error: …'      — spawn rejected (budget cap, no CLI)
    """
    if PROBE_INTERVAL_S <= 0 and not force:
        return "disabled"
    conn = get_db()
    if not daemon_state.claim_pass(
        "probe", PROBE_INTERVAL_S, scheduled=scheduled, conn=conn,
    ):
        return "not_due"
    now_t = int(time.time())
    graded = _grade_pending(conn)

    with single_flight_lock("probe-daemon") as locked:
        if not locked:
            out = f"graded={graded} probe_child_running n=1 (single-flight lock)"
            _record_probe_pass(conn, now_t, out)
            return out

        running = _running_probe_children(conn)
        if running:
            out = f"graded={graded} probe_child_running n={len(running)}"
            _record_probe_pass(conn, now_t, out)
            return out

        due = _due_probes(conn, now_t)
        if not due:
            out = f"graded={graded} no_due"
            _record_probe_pass(conn, now_t, out)
            return out

        try:
            result = _spawn_probe_child(due[0])
        except Exception as e:  # noqa: BLE001 — never crash the daemon
            out = f"graded={graded} spawn_error: {e}"
            _record_probe_pass(conn, now_t, out)
            return out
        out = (
            f"graded={graded} spawned cat={due[0]['category']} "
            f"{str(result)[:120]}"
        )
        _record_probe_pass(conn, now_t, out)
        return out


def _serve_loop() -> None:
    while True:
        try:
            run_probe_pass(scheduled=True)
        except Exception:
            logger.debug("probe_daemon tick failed", exc_info=True)
        daemon_sleep(PROBE_INTERVAL_S)


def start_probe_daemon() -> None:
    """Idempotent starter. No-op when PROBE_INTERVAL_S<=0. Same cascade
    prevention as shadow_review/extract: spawned children and non-foreground
    origins refuse to start the daemon so spawn() can't recurse."""
    global _started
    if _started:
        return
    if PROBE_INTERVAL_S <= 0:
        return
    from .config import BACKGROUND_DAEMONS_ALLOWED
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    t = threading.Thread(
        target=_serve_loop, name="probe_daemon", daemon=True,
    )
    t.start()
    _started = True
