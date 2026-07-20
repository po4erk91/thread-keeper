"""Notifier daemon — surfaces silent background-loop degradation + materialization.

thread-keeper's learning loops spawn paid children. When a loop can't do its
work — a CLI subscription runs out of credits/limits, auth expires, the binary
is missing, a spawn times out, or (the common one) the spawned child dies
mid-run — TK quietly stops learning. For a memory system that silent
degradation is the worst failure mode: you keep trusting it while it has
stopped. This daemon watches the already-emitted signals and tells the user.

Three detection sources, read-only (no LLM spawn, no credit cost):

  1. Admission failures / terminal timeouts — `<loop>_pass` events whose summary
     is a spawn/budget failure (classify_summary), plus `spawn_timeout_retry_failed`.
  2. Dead children — `tasks` rows that ended with a non-zero, non-timeout
     return_code. This is what catches "subscription ran out mid-run": spawn()
     returns `ok task=…` at LAUNCH, so the *_pass summary is a false success; the
     real outcome only lands in tasks.return_code.
  3. Positive materialization — `skill_materialized`/`skill_create`/`lesson_append`.

`events`, `tasks` and `daemon_state` are node-local (never synced), so each
machine notifies about its own loops and keeps its own per-loop cooldown.

All off by default (NOTIFY_POLL_S=0). Categories individually toggleable. Channels
macOS (osascript) + log now; webhook is a follow-up.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import threading
import time

from .config import (
    NOTIFY_POLL_S,
    NOTIFY_LOOP_FAILURE,
    NOTIFY_SKILL_MATERIALIZED,
    NOTIFY_LESSON,
    NOTIFY_CHANNEL,
    NOTIFY_FAILURE_COOLDOWN_S,
)
from .db import get_db
from . import daemon_state, identity
from .helpers import daemon_sleep, single_flight_lock

logger = logging.getLogger(__name__)

_started = False

# SOURCE 1 — loop-pass kinds whose failure summaries we surface. Their pass
# summaries embed the spawn/budget outcome. `spawn_timeout_retry_failed` is a
# terminal timeout (transient `spawn_timeout`/`retry_skipped` are excluded — they
# retry).
_LOOP_PASS_KINDS = (
    "shadow_review_pass", "candidate_review_pass", "curator_pass",
    "probe_pass", "extract_pass", "dialectic_validate_pass",
    "evolve_review_pass", "evolve_apply_pass", "auto_update_pass",
    "skill_update_pass", "spawn_timeout_retry_failed",
)
# SOURCE 3 — positive materialization event kinds, split by toggle.
_SKILL_KINDS = ("skill_materialized", "skill_create")
_LESSON_KINDS = ("lesson_append",)

# Failure tokens shared with shadow_review._classify_pass / agent_status._human_summary.
_FAIL_TOKENS = (
    "spawn_error", "spawn_failed", ":: err", "budget_exceeded",
    "token_budget_exceeded", "cost_budget_exceeded",
)
_SPAWN_TIMEOUT_RETURN_CODE = 124  # spawn_budget.SPAWN_TIMEOUT_RETURN_CODE


# ── classification ──────────────────────────────────────────────────────────

def classify_summary(summary: str | None) -> str:
    """Bucket a `*_pass` event summary: 'failure' | 'neutral'.

    Failure = a spawn/budget rejection or embedded ERR. Everything else
    (not_due / no_window / too_short / below_threshold / *_skip / *_child_running
    / no_apply_work / claim_lost / unchanged_inventory / `ok task=…`) is neutral.
    Mirrors shadow_review._classify_pass and agent_status._human_summary.
    """
    s = (summary or "").strip()
    if not s:
        return "neutral"
    if s.startswith("ERR") or s.startswith("spawn_error"):
        return "failure"
    low = s.lower()
    if any(tok in low for tok in _FAIL_TOKENS):
        return "failure"
    return "neutral"


def _reason_from_summary(summary: str) -> str:
    """Human, actionable reason from a failure summary."""
    s = summary or ""
    low = s.lower()
    if "budget_exceeded" in low or "token_budget" in low or "cost_budget" in low:
        return "subscription/credit budget exhausted"
    if "binary_not_found" in low or "cli_not_found" in low:
        return "CLI binary missing / not installed"
    if "argument list too long" in low:
        return "prompt too large"
    idx = s.find("ERR")
    tail = (s[idx:] if idx >= 0 else s).strip()
    return (tail[:120] or "spawn failed")


def _reason_from_task_log(task_id: str) -> str:
    """Last meaningful line of a dead child's captured log (the failure reason)."""
    try:
        from .tools.spawn import task_logs  # lazy: spawn imports identity/config
        txt = task_logs(task_id, tail_lines=12)
    except Exception:
        return "no_log"
    lines = [ln for ln in txt.splitlines() if ln.strip()]
    return (lines[-1][:180] if lines else "no_log")


# ── watermark (dual cursor in one notify_pass row) ──────────────────────────

def _read_watermark(conn: sqlite3.Connection) -> tuple[int, int]:
    """Parse `ev=<events.id>;tk=<tasks.ended_at>` from the latest notify_pass
    event. Returns (0, 0) when there is no prior pass."""
    try:
        row = conn.execute(
            "SELECT target FROM events WHERE kind='notify_pass' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0, 0
    if not row or not row["target"]:
        return 0, 0
    ev = tk = 0
    for part in str(row["target"]).split(";"):
        k, _, v = part.partition("=")
        try:
            if k == "ev":
                ev = int(v)
            elif k == "tk":
                tk = int(v)
        except (ValueError, TypeError):
            pass
    return ev, tk


def _has_prior_notify_pass(conn: sqlite3.Connection) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM events WHERE kind='notify_pass' LIMIT 1"
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def _max_event_id(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(id) FROM events").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def _max_task_ended(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(ended_at) FROM tasks").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def _record_notify_pass(conn: sqlite3.Connection, ev_id: int, tk_ended: int,
                        outcome: str) -> None:
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, 'notify_pass', ?, ?, ?)",
            (identity._session_id or "", f"ev={ev_id};tk={tk_ended}",
             outcome[:300], int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("notify: failed to record pass", exc_info=True)


# ── scans ───────────────────────────────────────────────────────────────────

def _scan_failures(conn: sqlite3.Connection, ev_floor: int,
                   ev_ceil: int) -> list[dict]:
    """SOURCE 1: loop-pass failures in (ev_floor, ev_ceil]."""
    out: list[dict] = []
    ph = ",".join("?" for _ in _LOOP_PASS_KINDS)
    try:
        rows = conn.execute(
            f"SELECT id, kind, summary FROM events "
            f"WHERE id > ? AND id <= ? AND kind IN ({ph}) ORDER BY id",
            (ev_floor, ev_ceil, *_LOOP_PASS_KINDS),
        ).fetchall()
    except sqlite3.OperationalError:
        return out
    for r in rows:
        summary = r["summary"] or ""
        # spawn_timeout_retry_failed is itself terminal; classify others.
        if r["kind"] == "spawn_timeout_retry_failed" or \
                classify_summary(summary) == "failure":
            loop = r["kind"][:-5] if r["kind"].endswith("_pass") else r["kind"]
            out.append({"kind": loop, "reason": _reason_from_summary(summary)})
    return out


def _scan_dead_children(conn: sqlite3.Connection, tk_floor: int,
                        tk_ceil: int) -> list[dict]:
    """SOURCE 2: spawned children that ended non-zero (excluding timeouts and
    children superseded by a retry). Catches subscription-exhausted-mid-run."""
    out: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT id, role, chosen_cli, return_code FROM tasks "
            "WHERE ended_at IS NOT NULL AND ended_at > ? AND ended_at <= ? "
            "AND return_code IS NOT NULL AND return_code != 0 AND return_code != ? "
            "AND timeout_respawned_as IS NULL ORDER BY ended_at",
            (tk_floor, tk_ceil, _SPAWN_TIMEOUT_RETURN_CODE),
        ).fetchall()
    except sqlite3.OperationalError:
        return out
    for r in rows:
        label = r["role"] or r["chosen_cli"] or "child"
        out.append({
            "kind": f"child:{label}",
            "label": label,
            "rc": r["return_code"],
            "reason": _reason_from_task_log(r["id"]),
        })
    return out


def _scan_positive(conn: sqlite3.Connection, ev_floor: int, ev_ceil: int,
                   kinds: tuple[str, ...]) -> list[dict]:
    """SOURCE 3: positive materialization events in (ev_floor, ev_ceil]."""
    out: list[dict] = []
    ph = ",".join("?" for _ in kinds)
    try:
        rows = conn.execute(
            f"SELECT id, kind, target, summary FROM events "
            f"WHERE id > ? AND id <= ? AND kind IN ({ph}) ORDER BY id",
            (ev_floor, ev_ceil, *kinds),
        ).fetchall()
    except sqlite3.OperationalError:
        return out
    for r in rows:
        out.append({"kind": r["kind"], "target": r["target"] or "",
                    "summary": r["summary"] or ""})
    return out


def _cooldown_ok(kind: str, now: float, conn: sqlite3.Connection) -> bool:
    """One failure notification per loop-kind per NOTIFY_FAILURE_COOLDOWN_S,
    de-duped cross-process via daemon_state (a lapsed subscription emits a
    failure every tick — this suppresses the storm)."""
    return daemon_state.claim_pass(
        f"notify_fail_{kind}", NOTIFY_FAILURE_COOLDOWN_S,
        scheduled=True, conn=conn, now=now,
    )


# ── channels ────────────────────────────────────────────────────────────────

def _notify_log(title: str, body: str) -> None:
    logger.warning("[notify] %s | %s", title, body)


def _notify_macos(title: str, body: str) -> bool:
    """Best-effort macOS banner (mirrors memory_guard._notify_user)."""
    if os.uname().sysname != "Darwin":
        return False
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_body = body.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{safe_body}" with title "{safe_title}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        logger.debug("notify: osascript failed", exc_info=True)
        return False


def _dispatch(title: str, body: str) -> None:
    """Fan out to the configured channels (comma list: macos,log[,webhook])."""
    channels = {c.strip().lower() for c in (NOTIFY_CHANNEL or "").split(",") if c.strip()}
    if "log" in channels:
        _notify_log(title, body)
    if "macos" in channels:
        _notify_macos(title, body)
    # "webhook" — follow-up (urllib POST, see sync/daemon._post).


def _fmt_failure(f: dict) -> tuple[str, str]:
    return (f"Thread-keeper: {f['kind']} loop failed", f["reason"][:200])


def _fmt_child(c: dict) -> tuple[str, str]:
    return (f"Thread-keeper: {c['label']} child died (rc={c['rc']})",
            c["reason"][:200])


def _fmt_skill(s: dict) -> tuple[str, str]:
    return ("Thread-keeper: skill materialized", (s["summary"] or s["target"])[:200])


def _fmt_lesson(l: dict) -> tuple[str, str]:
    return ("Thread-keeper: lesson added", f"{l['target']} ({l['summary']})"[:200])


# ── pass + daemon lifecycle ─────────────────────────────────────────────────

def run_notify_pass(force: bool = False, *, scheduled: bool = False) -> str:
    """One notifier tick. Returns a short outcome string.

    Reads new signals since the watermark, dispatches notifications for enabled
    categories, advances the watermark. The dispatch (a non-idempotent side
    effect) happens under single_flight_lock and strictly between the read and
    the watermark write, so a SQLite lock-retry can never double-fire.
    """
    if NOTIFY_POLL_S <= 0 and not force:
        return "disabled"
    conn = get_db()
    if not daemon_state.claim_pass("notify", NOTIFY_POLL_S,
                                   scheduled=scheduled, conn=conn):
        return "not_due"

    ev_floor, tk_floor = _read_watermark(conn)
    ev_ceil = _max_event_id(conn)
    tk_ceil = _max_task_ended(conn)

    # First run: seed to current position so historical backlog never fires.
    if not _has_prior_notify_pass(conn):
        _record_notify_pass(conn, ev_ceil, tk_ceil, "seed")
        return "seed"

    fails = _scan_failures(conn, ev_floor, ev_ceil) if NOTIFY_LOOP_FAILURE else []
    children = _scan_dead_children(conn, tk_floor, tk_ceil) if NOTIFY_LOOP_FAILURE else []
    skills = _scan_positive(conn, ev_floor, ev_ceil, _SKILL_KINDS) if NOTIFY_SKILL_MATERIALIZED else []
    lessons = _scan_positive(conn, ev_floor, ev_ceil, _LESSON_KINDS) if NOTIFY_LESSON else []

    fired = 0
    with single_flight_lock("notify-daemon") as locked:
        if not locked:
            return "notify_child_running"
        now = time.time()
        for f in fails:
            if _cooldown_ok(f["kind"], now, conn):
                _dispatch(*_fmt_failure(f))
                fired += 1
        for c in children:
            if _cooldown_ok(c["kind"], now, conn):
                _dispatch(*_fmt_child(c))
                fired += 1
        for s in skills:
            _dispatch(*_fmt_skill(s))
            fired += 1
        for l in lessons:
            _dispatch(*_fmt_lesson(l))
            fired += 1

    # Advance to the ceilings we scanned (disabled categories are skipped, never
    # replayed — matching the no-backlog principle).
    _record_notify_pass(
        conn, ev_ceil, tk_ceil,
        f"fired={fired} fails={len(fails)} children={len(children)} "
        f"skills={len(skills)} lessons={len(lessons)}",
    )
    return f"ok fired={fired}"


def _serve_loop() -> None:
    while True:
        try:
            run_notify_pass(scheduled=True)
        except Exception:
            logger.debug("notify_daemon tick failed", exc_info=True)
        daemon_sleep(NOTIFY_POLL_S)


def start_notify_daemon() -> None:
    """Idempotent starter. No-op when NOTIFY_POLL_S<=0 (feature off by default)."""
    global _started
    if _started:
        return
    if NOTIFY_POLL_S <= 0:
        return
    from .config import BACKGROUND_DAEMONS_ALLOWED
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    t = threading.Thread(target=_serve_loop, name="notify_daemon", daemon=True)
    t.start()
    _started = True
