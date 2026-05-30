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

import signal
import sqlite3
import subprocess
import sys
import time


def _record(db_path: str, task_id: str, rc: int) -> None:
    """Best-effort persist of the child's exit code. Never raises.

    ``return_code`` is set unconditionally (this is the authoritative
    writer); ``ended_at`` is filled only if no one set it first, so a value
    already written by the parent reaper's liveness path is preserved.
    """
    if not db_path or not task_id:
        return
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute(
                "UPDATE tasks SET return_code=?, "
                "ended_at=COALESCE(ended_at, ?) WHERE id=?",
                (rc, int(time.time()), task_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # The recorder must never take the child's exit status hostage.
        pass


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
        # Inherit cwd / env / stdio from this wrapper (set by the launcher's
        # Popen), so the real child sees exactly what it would have without
        # the wrapper in between.
        proc = subprocess.Popen(child_cmd)
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

    rc = proc.wait()
    _record(db_path, task_id, rc)

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
