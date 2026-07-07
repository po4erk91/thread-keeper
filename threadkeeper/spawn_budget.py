"""RSS budget enforcement for spawned children.

Cap on combined RSS of all running spawned children. The parent process
itself is not counted (we don't constrain the user's main agent). Default
budget is `SPAWN_BUDGET_MB` (3072 MB).

Flow:
  spawn() pre-flight:
    estimate child RSS (slim → SPAWN_ESTIMATE_SLIM_MB, else FULL)
    BEGIN IMMEDIATE, check_budget(conn, new_kb), and INSERT the tasks
    row with rss_kb = estimate before Popen. The SQLite write transaction
    serializes concurrent admission checks; if Popen fails, the reservation
    rolls back and spawn() returns ERR + reason.

  background daemon (start_budget_daemon):
    every SPAWN_BUDGET_POLL_S seconds, walk running tasks, compute real
    RSS of each process tree via `ps`, write back into tasks.rss_kb.
    Visible (Terminal-launched) children persist pid=0, so their live pid
    is resolved from their forced session-id (spawned_cid) in `ps` argv and
    measured the same way — they contribute real RSS, not the static
    estimate. A visible row whose cid never resolves to a live process is
    reaped past SPAWN_VISIBLE_TTL_S so it can't pin budget capacity forever.
    Tasks that have ended → no update (their rss_kb stays as last seen
    but they're filtered out by ended_at IS NOT NULL anyway).

  spawn_budget_status() (MCP):
    "budget=N MB used=N MB free=N MB | per_task..."

Set SPAWN_BUDGET_MB=0 to disable enforcement entirely.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Optional, Tuple

from .config import (
    SPAWN_BUDGET_MB,
    SPAWN_ESTIMATE_SLIM_MB,
    SPAWN_ESTIMATE_FULL_MB,
    SPAWN_BUDGET_POLL_S,
    SPAWN_TOKEN_BUDGET,
    SPAWN_COST_BUDGET_USD,
    SPAWN_VISIBLE_TTL_S,
    SPAWN_MAX_RUNTIME_S,
    SPAWN_KILL_GRACE_S,
    SPAWN_TIMEOUT_RETRY_LIMIT,
    SPAWN_TIMEOUT_RETRY_DELAY_S,
    TASK_LOG_DIR,
)
from .helpers import daemon_sleep
from .db import get_db
from .helpers import alive

logger = logging.getLogger(__name__)

_started = False

# return_code stamped on a child the wall-clock watchdog kills (#80). 124 is
# the GNU `timeout(1)` convention for "command timed out", so it reads as a
# timeout marker rather than a normal exit (0-255) or a signal-kill (negative).
# Surfaced by agent_status / mp_dashboard so a runtime kill is observable.
SPAWN_TIMEOUT_RETURN_CODE = 124


def _row_get(row, key: str, default=None):
    """Read a sqlite Row field that may not exist in pre-migration tests."""
    try:
        val = row[key]
    except (KeyError, IndexError):
        return default
    return default if val is None else val


# ─────────────────────────────────────────────────────────────────────
# Estimates
# ─────────────────────────────────────────────────────────────────────

def estimate_child_rss_kb(slim: bool) -> int:
    """Initial RSS guess for a not-yet-running child, used by admission
    control. Real value replaces this within `SPAWN_BUDGET_POLL_S`."""
    mb = SPAWN_ESTIMATE_SLIM_MB if slim else SPAWN_ESTIMATE_FULL_MB
    return int(mb) * 1024


# ─────────────────────────────────────────────────────────────────────
# Tree walker — sum RSS of pid and all its descendants via `ps`
# ─────────────────────────────────────────────────────────────────────

def _ps_pairs() -> list[tuple[int, int]]:
    """Snapshot of (pid, ppid) for every process visible to `ps`."""
    try:
        r = subprocess.run(
            ["ps", "-ax", "-o", "pid=,ppid="],
            capture_output=True, text=True, timeout=3,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    out: list[tuple[int, int]] = []
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            out.append((int(parts[0]), int(parts[1])))
        except ValueError:
            continue
    return out


def _rss_for_pids(pids: list[int]) -> int | None:
    """Sum RSS (KB) for the given pids via `ps`.

    Missing pids contribute 0, but a failed or garbled `ps` sample returns
    None so callers keep the last known RSS instead of treating the process as
    freed.
    """
    if not pids:
        return 0
    try:
        r = subprocess.run(
            ["ps", "-o", "pid=,rss="] + ["-p"] + [str(p) for p in pids],
            capture_output=True, text=True, timeout=3,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    total = 0
    saw_row = False
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            return None
        try:
            total += int(parts[1])
        except ValueError:
            return None
        saw_row = True
    return total if saw_row else None


def measure_tree_rss_kb(root_pid: int) -> Optional[int]:
    """Walk descendants of root_pid and return summed RSS in KB.
    Returns None when the root is gone or RSS is unreadable this tick."""
    if root_pid is None or root_pid <= 0:
        return None
    pairs = _ps_pairs()
    if not pairs:
        return None
    # Bail when the root isn't visible — process ended.
    root_alive = any(pid == root_pid for pid, _ in pairs)
    if not root_alive:
        return None
    # BFS descendants
    children_by_parent: dict[int, list[int]] = {}
    for pid, ppid in pairs:
        children_by_parent.setdefault(ppid, []).append(pid)
    tree = [root_pid]
    frontier = [root_pid]
    while frontier:
        nxt: list[int] = []
        for p in frontier:
            for kid in children_by_parent.get(p, ()):
                tree.append(kid)
                nxt.append(kid)
        frontier = nxt
    return _rss_for_pids(tree)


def _pid_for_cid(cid: str) -> Optional[int]:
    """Resolve the live OS pid of a visible (Terminal-launched) child from its
    forced session-id. spawn() persists pid=0 for visible children, but a
    claude child carries `--session-id <cid>` in its argv, so the cid is a
    stable handle into `ps`. Returns the first process whose command line
    contains the cid, or None when no live process carries it (child not yet
    started, already exited, or a non-claude CLI that keeps the cid in env)."""
    if not cid:
        return None
    try:
        r = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            capture_output=True, text=True, timeout=3,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    for line in (r.stdout or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        pid_s, cmdline = parts
        if cid in cmdline:
            try:
                return int(pid_s)
            except ValueError:
                continue
    return None


# ─────────────────────────────────────────────────────────────────────
# Budget check
# ─────────────────────────────────────────────────────────────────────

def _running_tasks_rss(conn) -> int:
    """Sum rss_kb across tasks that are not ended. NULL rss_kb means we
    haven't measured yet — assume the FULL estimate as a conservative
    placeholder, otherwise a spawn flood could squeeze past the cap before
    the daemon catches up."""
    rows = conn.execute(
        "SELECT rss_kb FROM tasks WHERE ended_at IS NULL"
    ).fetchall()
    total = 0
    fallback_kb = SPAWN_ESTIMATE_FULL_MB * 1024
    for r in rows:
        total += (r["rss_kb"] if r["rss_kb"] is not None else fallback_kb)
    return total


def _daily_spawn_usage(conn, now: int | None = None) -> tuple[int, float]:
    """Return (tokens, cost_usd) recorded over the last 24 hours.

    Missing columns mean the DB has not yet been migrated in this process; fail
    open so introducing the accounting columns cannot block spawns on partial
    installations.
    """
    cut = (int(time.time()) if now is None else int(now)) - 86400
    try:
        row = conn.execute(
            "SELECT "
            "COALESCE(SUM(COALESCE(tokens_total, "
            "COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0))), 0), "
            "COALESCE(SUM(COALESCE(cost_usd, 0.0)), 0.0) "
            "FROM tasks WHERE COALESCE(ended_at, started_at) >= ?",
            (cut,),
        ).fetchone()
    except Exception:
        return 0, 0.0
    if not row:
        return 0, 0.0
    return int(row[0] or 0), float(row[1] or 0.0)


def _check_daily_spend_budget(conn) -> Tuple[bool, str]:
    tokens_24h, cost_24h = _daily_spawn_usage(conn)
    if SPAWN_TOKEN_BUDGET > 0 and tokens_24h >= SPAWN_TOKEN_BUDGET:
        return False, (
            f"token_budget_exceeded: tokens_24h={tokens_24h} >= "
            f"limit={SPAWN_TOKEN_BUDGET}. Raise "
            "THREADKEEPER_SPAWN_TOKEN_BUDGET, wait for the 24h window to "
            "roll over, or disable with 0."
        )
    if SPAWN_COST_BUDGET_USD > 0 and cost_24h >= SPAWN_COST_BUDGET_USD:
        return False, (
            f"cost_budget_exceeded: cost_24h=${cost_24h:.4f} >= "
            f"limit=${SPAWN_COST_BUDGET_USD:.4f}. Raise "
            "THREADKEEPER_SPAWN_COST_BUDGET_USD, wait for the 24h window to "
            "roll over, or disable with 0."
        )
    return True, (
        f"spend_ok: tokens_24h={tokens_24h}"
        f"{('/' + str(SPAWN_TOKEN_BUDGET)) if SPAWN_TOKEN_BUDGET > 0 else ''} "
        f"cost_24h=${cost_24h:.4f}"
        f"{('/$' + format(SPAWN_COST_BUDGET_USD, '.4f')) if SPAWN_COST_BUDGET_USD > 0 else ''}"
    )


def check_budget(conn, new_child_kb: int) -> Tuple[bool, str]:
    """Decide whether spawning a child of `new_child_kb` would breach the
    budget. Returns (ok, message). When all budgets are unset/disabled, always
    ok."""
    spend_ok, spend_msg = _check_daily_spend_budget(conn)
    if not spend_ok:
        return False, spend_msg
    if SPAWN_BUDGET_MB <= 0:
        return True, "budget_disabled " + spend_msg
    budget_kb = SPAWN_BUDGET_MB * 1024
    current = _running_tasks_rss(conn)
    projected = current + new_child_kb
    if projected > budget_kb:
        cur_mb = current // 1024
        new_mb = new_child_kb // 1024
        proj_mb = projected // 1024
        return False, (
            f"budget_exceeded: running_subagents={cur_mb}MB + "
            f"new_child={new_mb}MB = {proj_mb}MB > "
            f"limit={SPAWN_BUDGET_MB}MB. Wait for a child to finish, "
            f"raise THREADKEEPER_SPAWN_BUDGET_MB, or use task_kill()."
        )
    return True, (
        f"ok: current={current // 1024}MB + new={new_child_kb // 1024}MB "
        f"≤ {SPAWN_BUDGET_MB}MB"
    )


# ─────────────────────────────────────────────────────────────────────
# Daemon — refresh real RSS values
# ─────────────────────────────────────────────────────────────────────

def _measure_visible(conn, row, now: int) -> int:
    """Account a visible (pid=0) child's memory, or reap an unresolvable row.

    Resolves the live pid from the row's forced session-id (spawned_cid) and
    writes its real RSS tree into rss_kb, so visible children contribute true
    memory to the budget instead of the static pre-launch estimate (#64). When
    no live process carries the cid and the row has outlived
    SPAWN_VISIBLE_TTL_S, mark it ended so an unresolvable visible row cannot
    pin budget capacity forever.

    Returns 1 when rss_kb was refreshed, 2 when the row was reaped, else 0."""
    cid = row["spawned_cid"]
    vpid = _pid_for_cid(cid) if cid else None
    if vpid:
        rss = measure_tree_rss_kb(vpid)
        if rss is not None:
            conn.execute(
                "UPDATE tasks SET rss_kb=?, rss_updated_at=? WHERE id=?",
                (rss, now, row["id"]),
            )
            return 1
        return 0  # live pid found but RSS unreadable this tick — leave as-is
    started = row["started_at"] or 0
    if SPAWN_VISIBLE_TTL_S > 0 and started and (now - started) >= SPAWN_VISIBLE_TTL_S:
        conn.execute(
            "UPDATE tasks SET ended_at=? WHERE id=? AND ended_at IS NULL",
            (now, row["id"]),
        )
        return 2
    return 0


def _terminate_tree(pid: int, grace_s: float) -> None:
    """Kill a timed-out child: SIGTERM the process group, wait up to `grace_s`,
    then SIGKILL whatever is left (#80).

    Spawned children start in their own session (`start_new_session=True`), so
    the tracked pid is the session/group leader and signalling the whole group
    reaches both the `_spawn_wrap` recorder and the real CLI underneath it —
    SIGTERM is forwarded by the wrapper, and the SIGKILL fallback reaps any
    child that ignored it without leaving an orphan. Falls back to a bare
    per-pid signal when the group can't be resolved."""
    import signal as _sig

    def _send(sig) -> None:
        try:
            os.killpg(os.getpgid(pid), sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    _send(_sig.SIGTERM)
    deadline = time.time() + max(0.0, float(grace_s or 0.0))
    while time.time() < deadline:
        if not alive(pid):
            return
        time.sleep(0.2)
    if alive(pid):
        _send(_sig.SIGKILL)


def _over_runtime_cap(row, now: int) -> bool:
    """True when this running row has outlived SPAWN_MAX_RUNTIME_S (#80).
    Cap of 0 disables the watchdog entirely."""
    if SPAWN_MAX_RUNTIME_S <= 0:
        return False
    started = row["started_at"] or 0
    return bool(started) and (now - started) >= SPAWN_MAX_RUNTIME_S


def _reap_timed_out(conn, row, now: int) -> bool:
    """Kill a child that has run past the wall-clock cap and close its row so
    the spawning loop's single-flight slot releases (#80).

    SIGTERM→grace→SIGKILL the process tree, then stamp ended_at + the timeout
    return_code. The ended_at guard keeps it idempotent: a second sweep over an
    already-reaped row is a no-op. Returns True when this call closed the row."""
    pid = int(row["pid"] or 0)
    if pid > 0:
        _terminate_tree(pid, SPAWN_KILL_GRACE_S)
    cur = conn.execute(
        "UPDATE tasks SET ended_at=?, return_code=? "
        "WHERE id=? AND ended_at IS NULL",
        (now, SPAWN_TIMEOUT_RETURN_CODE, row["id"]),
    )
    closed = cur.rowcount > 0
    if closed:
        age = now - (row["started_at"] or now)
        logger.warning(
            "spawn watchdog: killed task %s after %ss (cap=%ss)",
            row["id"], age, SPAWN_MAX_RUNTIME_S,
        )
        try:
            from .identity import _emit
            _emit(conn, "spawn_timeout", target=row["id"],
                  summary=f"runtime {age}s exceeded cap {SPAWN_MAX_RUNTIME_S}s")
        except Exception:
            pass
    return closed


def _retry_chain_root(conn, row) -> str:
    root = str(_row_get(row, "retry_root", "") or "")
    if root:
        return root
    prev = str(_row_get(row, "retry_of", "") or "")
    return prev or str(row["id"])


def _root_prompt(conn, root_id: str, fallback: str) -> str:
    try:
        r = conn.execute(
            "SELECT prompt FROM tasks WHERE id=?", (root_id,)
        ).fetchone()
    except Exception:
        return fallback
    if not r:
        return fallback
    return str(r["prompt"] or fallback)


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _continuation_prompt(conn, row, age: int, attempt: int, root_id: str) -> str:
    original_prompt = _root_prompt(conn, root_id, str(row["prompt"] or ""))
    prev_id = str(row["id"])
    prev_cid = str(_row_get(row, "spawned_cid", "") or "")
    log_path = TASK_LOG_DIR / f"{prev_id}.log"
    log_hint = str(log_path) if log_path.exists() else "(no captured log file)"
    return (
        "You are an immediate ThreadKeeper watchdog restart for a task that "
        "timed out.\n\n"
        "Previous run:\n"
        f"- previous_task_id: {prev_id}\n"
        f"- previous_child_cid: {prev_cid or '(unknown)'}\n"
        f"- elapsed_before_timeout_s: {age}\n"
        f"- retry_attempt: {attempt} of {SPAWN_TIMEOUT_RETRY_LIMIT}\n"
        f"- cwd: {row['cwd']}\n"
        f"- previous_log: {log_hint}\n\n"
        "Continue the same assignment from the last safe state. Do not restart "
        "blindly. First inspect the current workspace state and, when present, "
        "the previous log/transcript. Preserve useful work already completed, "
        "repair partial work if needed, then finish the original assignment. "
        "If the prior run already completed the substantive work but was killed "
        "before reporting, verify that and report the final status instead of "
        "duplicating side effects.\n\n"
        "Original assignment:\n"
        f"{original_prompt}"
    )


def _parse_spawned_task_id(result: str) -> Optional[str]:
    for part in str(result or "").split():
        if part.startswith("task=tk_"):
            return part.split("=", 1)[1]
    return None


def _respawn_timed_out(conn, row, age: int) -> None:
    """Immediately re-launch a watchdog-killed task with continuation context.

    This is retry, not an exact token-stream restore: the restarted agent gets
    the original assignment plus pointers to the previous task/cid/log and must
    continue from the workspace state it finds. A hard retry cap prevents an
    infinite kill/restart loop on pathological prompts.
    """
    if SPAWN_TIMEOUT_RETRY_LIMIT <= 0:
        try:
            from .identity import _emit
            _emit(conn, "spawn_timeout_retry_skipped", target=row["id"],
                  summary="disabled limit=0")
            conn.commit()
        except Exception:
            pass
        return

    current_attempt = int(_row_get(row, "retry_attempt", 0) or 0)
    next_attempt = current_attempt + 1
    if next_attempt > SPAWN_TIMEOUT_RETRY_LIMIT:
        try:
            from .identity import _emit
            _emit(
                conn,
                "spawn_timeout_retry_skipped",
                target=row["id"],
                summary=(
                    f"attempts_exhausted attempt={current_attempt} "
                    f"limit={SPAWN_TIMEOUT_RETRY_LIMIT}"
                ),
            )
            conn.commit()
        except Exception:
            pass
        return

    if SPAWN_TIMEOUT_RETRY_DELAY_S > 0:
        time.sleep(float(SPAWN_TIMEOUT_RETRY_DELAY_S))

    root_id = _retry_chain_root(conn, row)
    prompt = _continuation_prompt(conn, row, age, next_attempt, root_id)
    try:
        from .tools.spawn import _spawn_impl
        result = _spawn_impl(
            prompt=prompt,
            cwd=str(row["cwd"] or os.getcwd()),
            append_system=str(_row_get(row, "append_system", "") or ""),
            model=str(_row_get(row, "model", "") or ""),
            effort=str(_row_get(row, "effort", "") or ""),
            permission_mode=str(_row_get(row, "permission_mode", "auto") or "auto"),
            extra_allowed_tools=str(_row_get(row, "extra_allowed_tools", "") or ""),
            capture_output=_as_bool(_row_get(row, "capture_output", 1), True),
            visible=_as_bool(_row_get(row, "visible", 0), False),
            role=str(_row_get(row, "role", "") or ""),
            write_origin=str(_row_get(row, "write_origin", "") or ""),
            slim=_as_bool(_row_get(row, "slim", 1), True),
            retry_of=str(row["id"]),
            retry_root=root_id,
            retry_attempt=next_attempt,
            parent_cid_override=str(_row_get(row, "parent_cid", "") or ""),
            cli=str(_row_get(row, "chosen_cli", "") or ""),
        )
    except Exception as e:
        logger.warning("spawn watchdog retry failed for %s: %s", row["id"], e)
        result = f"ERR exception={type(e).__name__}: {e}"

    retry_id = _parse_spawned_task_id(result)
    try:
        from .identity import _emit
        if retry_id:
            conn.execute(
                "UPDATE tasks SET timeout_respawned_as=? WHERE id=?",
                (retry_id, row["id"]),
            )
            _emit(
                conn,
                "spawn_timeout_retry",
                target=row["id"],
                summary=f"respawned_as={retry_id} attempt={next_attempt}",
            )
        else:
            _emit(
                conn,
                "spawn_timeout_retry_failed",
                target=row["id"],
                summary=str(result)[:240],
            )
        conn.commit()
    except Exception:
        pass


def _refresh_all_running(conn) -> int:
    """Sweep running tasks, update rss_kb with real measurement.

    pid>0 (headless) children are measured directly from their pid. Visible
    (pid<=0, Terminal-launched) children are resolved to a live pid via their
    forced session-id and measured too — and reaped past a TTL when no live
    process carries the cid (#64). Returns the number of rows whose rss_kb was
    refreshed."""
    rows = conn.execute(
        "SELECT * FROM tasks "
        "WHERE ended_at IS NULL ORDER BY started_at DESC"
    ).fetchall()
    now = int(time.time())
    updated = 0
    changed = False
    timed_out: list[tuple[object, int]] = []
    for r in rows:
        pid = r["pid"]
        if not pid or pid <= 0:
            code = _measure_visible(conn, r, now)
            if code:
                changed = True
                if code == 1:
                    updated += 1
            continue
        if not alive(pid):
            # Process gone — mark ended, leave rss_kb as last-known.
            conn.execute(
                "UPDATE tasks SET ended_at=? WHERE id=? AND ended_at IS NULL",
                (now, r["id"]),
            )
            changed = True
            continue
        if _over_runtime_cap(r, now):
            # Alive but hung past the wall-clock cap — kill it and close the
            # row so the loop's single-flight releases (#80).
            if _reap_timed_out(conn, r, now):
                changed = True
                timed_out.append((r, now - (r["started_at"] or now)))
            continue
        rss = measure_tree_rss_kb(pid)
        if rss is None:
            continue
        conn.execute(
            "UPDATE tasks SET rss_kb=?, rss_updated_at=? WHERE id=?",
            (rss, now, r["id"]),
        )
        updated += 1
        changed = True
    if changed:
        try:
            conn.commit()
        except Exception:
            pass
    for row, age in timed_out:
        _respawn_timed_out(conn, row, age)
    return updated


def _daemon_loop() -> None:
    while True:
        try:
            conn = get_db()
            try:
                _refresh_all_running(conn)
            finally:
                conn.close()
        except Exception:
            logger.debug("spawn_budget daemon tick failed", exc_info=True)
        daemon_sleep(SPAWN_BUDGET_POLL_S)


def start_budget_daemon() -> None:
    """Idempotent — call from _ensure_session lazily."""
    global _started
    if _started:
        return
    if SPAWN_BUDGET_POLL_S <= 0:
        return
    if SPAWN_BUDGET_MB <= 0 and SPAWN_MAX_RUNTIME_S <= 0:
        return  # both RSS budget and wall-clock watchdog (#80) off — nothing to do
    t = threading.Thread(
        target=_daemon_loop, name="spawn_budget", daemon=True,
    )
    t.start()
    _started = True
