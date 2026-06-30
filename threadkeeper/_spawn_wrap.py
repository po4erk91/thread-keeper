"""Spawn exit-code recorder.

Wraps a spawned child process so its real exit code reliably lands in
``tasks.return_code`` — regardless of which session (if any) is still alive
when the child finishes.

Why this exists
---------------
Spawned children are launched detached (``start_new_session=True``) and the
shared SQLite ``tasks`` table outlives the MCP process that launched them.
The parent-side reaper (``_reap_finished_tasks`` in ``tools/spawn.py``) is
built on ``os.waitpid``, which can only reap a process's *own live*
children. Cross-session — i.e. almost always — the process that later tries
to reap is no longer the spawning parent, so ``waitpid`` raises
``ChildProcessError`` and the exit code is lost (``return_code`` stays
NULL). Measured: 0 of 900+ ended tasks ever had a code.

This module is *our* code sitting between the launcher and the real CLI
child. It runs the child, waits for it, and writes the exit code itself —
no waitpid race, no dependency on the parent staying alive. It is the
single reliable writer of ``return_code``; the parent reaper remains as a
harmless fallback.

Invocation (from ``tools/spawn.py``)::

    <python> <this file> <db_path> <task_id> -- <child argv...>

Run by file *path* (not ``python -m``) on purpose: that way it never
imports the ``threadkeeper`` package, so there is zero package-init cost and
no import side effects on every spawn. Pure stdlib only.
"""

import json
import re
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass


_TAIL_LIMIT = 256 * 1024


@dataclass
class Usage:
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_total: int | None = None
    cost_usd: float | None = None


def _intish(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    s = s.replace(",", "").replace("_", "")
    try:
        return int(float(s))
    except ValueError:
        return None


def _floatish(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("$", "").replace(",", "").replace("_", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _first_not_none(*values):
    for v in values:
        if v is not None:
            return v
    return None


def _walk_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_dicts(v)


def _parse_json_usage(text: str) -> Usage:
    found = Usage()
    for line in text.splitlines():
        s = line.strip()
        if not s or "{" not in s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        for d in _walk_dicts(obj):
            if found.tokens_in is None:
                found.tokens_in = _first_not_none(
                    _intish(d.get("tokens_in")),
                    _intish(d.get("input_tokens")),
                    _intish(d.get("prompt_tokens")),
                )
            if found.tokens_out is None:
                found.tokens_out = _first_not_none(
                    _intish(d.get("tokens_out")),
                    _intish(d.get("output_tokens")),
                    _intish(d.get("completion_tokens")),
                )
            if found.tokens_total is None:
                found.tokens_total = _first_not_none(
                    _intish(d.get("tokens_total")),
                    _intish(d.get("total_tokens")),
                    _intish(d.get("tokens_used")),
                )
            if found.cost_usd is None:
                found.cost_usd = _first_not_none(
                    _floatish(d.get("cost_usd")),
                    _floatish(d.get("total_cost_usd")),
                    _floatish(d.get("total_cost")),
                    _floatish(d.get("cost")),
                )
    return found


_NUM = r"([0-9][0-9,_]*(?:\.[0-9]+)?)"


def _last_int(pattern: str, text: str) -> int | None:
    vals = [_intish(m.group(1)) for m in re.finditer(pattern, text, re.I)]
    vals = [v for v in vals if v is not None]
    return vals[-1] if vals else None


def _last_float(pattern: str, text: str) -> float | None:
    vals = [_floatish(m.group(1)) for m in re.finditer(pattern, text, re.I)]
    vals = [v for v in vals if v is not None]
    return vals[-1] if vals else None


def parse_usage(text: str) -> Usage:
    """Extract token/cost totals from common CLI trailers.

    Claude/Codex output formats are not a stable API, so this intentionally
    accepts both JSON result lines and human-readable summaries such as
    ``tokens used`` followed by a number. Missing fields remain ``None``.
    """
    text = text or ""
    usage = _parse_json_usage(text)
    if usage.tokens_in is None:
        usage.tokens_in = _first_not_none(
            _last_int(rf"\binput(?:\s+tokens?)?\s*[:=]\s*{_NUM}", text),
            _last_int(rf"{_NUM}\s+input(?:\s+tokens?)?\b", text),
        )
    if usage.tokens_out is None:
        usage.tokens_out = _first_not_none(
            _last_int(rf"\boutput(?:\s+tokens?)?\s*[:=]\s*{_NUM}", text),
            _last_int(rf"{_NUM}\s+output(?:\s+tokens?)?\b", text),
        )
    if usage.tokens_total is None:
        usage.tokens_total = _first_not_none(
            _last_int(rf"(?m)^\s*total\s+tokens?\s*[:=]\s*{_NUM}", text),
            _last_int(rf"(?m)^\s*tokens(?:\s+used)?\s*[:=]\s*{_NUM}", text),
            _last_int(rf"{_NUM}\s+tokens\s+used\b", text),
        )
    if usage.tokens_total is None:
        lines = [ln.strip() for ln in text.splitlines()]
        for i, line in enumerate(lines[:-1]):
            if line.lower() == "tokens used":
                usage.tokens_total = _intish(lines[i + 1])
    if usage.cost_usd is None:
        usage.cost_usd = _first_not_none(
            _last_float(
                rf"\b(?:total[_\s-]*)?cost(?:[_\s-]*usd)?\s*[:=]\s*\$?\s*{_NUM}",
                text,
            ),
            _last_float(rf"\$\s*{_NUM}\s*(?:usd)?", text),
        )
    if usage.tokens_total is None and (
        usage.tokens_in is not None or usage.tokens_out is not None
    ):
        usage.tokens_total = (usage.tokens_in or 0) + (usage.tokens_out or 0)
    return usage


def _record(db_path: str, task_id: str, rc: int, usage_text: str = "") -> None:
    """Best-effort persist of the child's exit code. Never raises.

    ``return_code`` is set unconditionally (this is the authoritative
    writer); ``ended_at`` is filled only if no one set it first, so a value
    already written by the parent reaper's liveness path is preserved.
    """
    if not db_path or not task_id:
        return
    usage = parse_usage(usage_text)
    ended_at = int(time.time())
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            try:
                conn.execute(
                    "UPDATE tasks SET return_code=?, "
                    "ended_at=COALESCE(ended_at, ?), "
                    "duration_s=CASE "
                    "  WHEN started_at IS NULL THEN duration_s "
                    "  ELSE MAX(0, COALESCE(ended_at, ?) - started_at) "
                    "END, "
                    "tokens_in=COALESCE(?, tokens_in), "
                    "tokens_out=COALESCE(?, tokens_out), "
                    "tokens_total=COALESCE(?, tokens_total), "
                    "cost_usd=COALESCE(?, cost_usd) "
                    "WHERE id=?",
                    (
                        rc,
                        ended_at,
                        ended_at,
                        usage.tokens_in,
                        usage.tokens_out,
                        usage.tokens_total,
                        usage.cost_usd,
                        task_id,
                    ),
                )
            except sqlite3.OperationalError:
                conn.execute(
                    "UPDATE tasks SET return_code=?, "
                    "ended_at=COALESCE(ended_at, ?) WHERE id=?",
                    (rc, ended_at, task_id),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # The recorder must never take the child's exit status hostage.
        pass


def _append_tail(tail: str, chunk: str) -> str:
    tail += chunk
    if len(tail) > _TAIL_LIMIT:
        tail = tail[-_TAIL_LIMIT:]
    return tail


def main(argv: list) -> int:
    # Record-only mode for the visible/Terminal launch path, which runs the
    # child itself in a shell and only needs the exit code persisted:
    #   <python> <this> --record <db_path> <task_id> <rc>
    if argv and argv[0] == "--record":
        if len(argv) >= 4:
            try:
                rc = int(argv[3])
            except (ValueError, TypeError):
                rc = 1
            _record(argv[1], argv[2], rc)
        return 0
    # Run-and-record mode. argv layout: [db_path, task_id, '--', *child_cmd]
    if len(argv) < 4 or argv[2] != "--":
        sys.stderr.write(
            "spawn_wrap: usage: <db_path> <task_id> -- <cmd...>\n"
        )
        return 2
    db_path, task_id = argv[0], argv[1]
    child_cmd = argv[3:]
    if not child_cmd:
        return 2

    try:
        # Inherit cwd / env / stdin from this wrapper (set by the launcher).
        # Tee stdout/stderr through us so we can parse the final usage trailer
        # while preserving the exact log stream the caller expects.
        proc = subprocess.Popen(
            child_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
    except (FileNotFoundError, OSError) as e:
        sys.stderr.write(f"spawn_wrap: launch failed: {e}\n")
        _record(db_path, task_id, 127)
        return 127

    # The wrapper is now the tracked pid, so forward the termination signals
    # task_kill might send on to the real child — otherwise a kill would hit
    # the wrapper and leave the child orphaned.
    def _forward(signum, _frame):
        try:
            proc.send_signal(signum)
        except Exception:
            pass

    for _s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(_s, _forward)
        except (ValueError, OSError):
            pass

    tail = ""
    if proc.stdout is not None:
        try:
            for chunk in proc.stdout:
                sys.stdout.write(chunk)
                sys.stdout.flush()
                tail = _append_tail(tail, chunk)
        except Exception:
            pass
    rc = proc.wait()
    _record(db_path, task_id, rc, tail)

    # Mirror the child's disposition in our own exit status so the parent's
    # waitpid fallback (if it does reap us) reads the same outcome. Exit
    # codes can't be negative, so encode signal-kills shell-style (128+N).
    if rc is None:
        return 1
    if rc < 0:
        return 128 + (-rc)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
