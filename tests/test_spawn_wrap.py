"""Exit-code recorder (`_spawn_wrap.py`) test.

The parent reaper (`_reap_finished_tasks`) is built on `os.waitpid`, which
can only reap a process's own live children. Spawned tasks outlive the MCP
process that launched them, so the later reaper is almost never the parent →
`waitpid` raises `ChildProcessError` and the exit code is lost. Measured:
0 of 900+ ended tasks ever had a `return_code`.

`_spawn_wrap` fixes this by wrapping the child: it runs the child and writes
`return_code` itself from inside the child's lifecycle. These tests cover:
  - `_record` DB write (sets return_code, COALESCEs ended_at)
  - run-and-record end-to-end via a real subprocess (success + failure)
  - `--record` shell mode used by the visible/Terminal launch path
  - signal forwarding so `task_kill` still terminates the real child
  - robustness: bad db / bad args never raise
"""
from __future__ import annotations

import importlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_WRAP = Path("threadkeeper/_spawn_wrap.py").resolve()


@pytest.fixture
def db_mod(tmp_path, monkeypatch):
    """Fresh isolated DB; returns (db module, db_path str)."""
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("THREADKEEPER_DB", str(db_path))
    import threadkeeper.config as cfg
    importlib.reload(cfg)
    import threadkeeper.db as db
    importlib.reload(db)
    return db, str(db_path)


def _mk_task(db, task_id):
    conn = db.get_db()
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, spawned_cid, cwd, prompt, "
        "started_at, rss_kb, rss_updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, 0, "p", "c", "/tmp", "x", int(time.time()), 0,
         int(time.time())),
    )
    conn.commit()


def _read(db, task_id):
    conn = db.get_db()
    return conn.execute(
        "SELECT ended_at, return_code, tokens_in, tokens_out, tokens_total, "
        "cost_usd, duration_s FROM tasks WHERE id=?", (task_id,)
    ).fetchone()


def _run_wrap(args):
    """Run the recorder as its own process (so its signal handlers stay out
    of the pytest process). Returns the completed-process handle."""
    return subprocess.run(
        [sys.executable, str(_WRAP), *args],
        capture_output=True, text=True, timeout=30,
    )


# ── _record unit ────────────────────────────────────────────────────────

def test_record_sets_return_code_and_ended_at(db_mod):
    db, db_path = db_mod
    _mk_task(db, "tk_r0")
    import threadkeeper._spawn_wrap as w
    w._record(db_path, "tk_r0", 0)
    row = _read(db, "tk_r0")
    assert row["return_code"] == 0
    assert row["ended_at"] is not None
    assert row["duration_s"] is not None


def test_record_coalesces_existing_ended_at(db_mod):
    db, db_path = db_mod
    _mk_task(db, "tk_r1")
    conn = db.get_db()
    conn.execute("UPDATE tasks SET ended_at=? WHERE id=?", (111, "tk_r1"))
    conn.commit()
    import threadkeeper._spawn_wrap as w
    w._record(db_path, "tk_r1", 7)
    row = _read(db, "tk_r1")
    assert row["return_code"] == 7
    assert row["ended_at"] == 111  # original preserved, not overwritten


def test_record_bad_db_never_raises(db_mod):
    import threadkeeper._spawn_wrap as w
    # Must not raise — the recorder may never hold the child's status hostage.
    w._record("/nonexistent/dir/db.sqlite", "tk_x", 3)
    w._record("", "", 0)


def test_parse_usage_json_result_line(db_mod):
    import threadkeeper._spawn_wrap as w
    usage = w.parse_usage(
        '{"type":"result","usage":{"input_tokens":1234,'
        '"output_tokens":56},"total_cost_usd":0.0123}'
    )
    assert usage.tokens_in == 1234
    assert usage.tokens_out == 56
    assert usage.tokens_total == 1290
    assert usage.cost_usd == 0.0123


def test_parse_usage_codex_tokens_used_trailer(db_mod):
    import threadkeeper._spawn_wrap as w
    usage = w.parse_usage("codex\nanswer\ntokens used\n44,777\n")
    assert usage.tokens_in is None
    assert usage.tokens_out is None
    assert usage.tokens_total == 44777


def test_parse_usage_ignores_prose_dollar_amounts(db_mod):
    """A child that merely DISCUSSES money must not record it as spend —
    observed in production as a $4,445 ad-spend figure landing in cost_usd.
    Only an explicit cost label counts."""
    import threadkeeper._spawn_wrap as w
    usage = w.parse_usage(
        "the ad spend was $4,445 across campaigns\nfinal answer: done\n"
    )
    assert usage.cost_usd is None


def test_parse_usage_ignores_trailer_lookalikes_far_from_end(db_mod):
    """Usage trailers print at the END of a run; a lookalike buried deep in
    the child's narrative output is not a trailer."""
    import threadkeeper._spawn_wrap as w
    filler = "\n".join(f"narrative line {i}" for i in range(200))
    usage = w.parse_usage("tokens used\n9,876\n" + filler)
    assert usage.tokens_total is None


def test_parse_usage_drops_implausible_values(db_mod):
    """Values beyond plausibility bounds are mis-parses, not measurements —
    better NULL than poisoning the 24h budget-admission sums."""
    import threadkeeper._spawn_wrap as w
    usage = w.parse_usage(
        "total cost: $99,999\ntokens used\n999,999,999,999\n"
    )
    assert usage.cost_usd is None
    assert usage.tokens_total is None


def test_record_persists_usage_and_duration(db_mod):
    db, db_path = db_mod
    _mk_task(db, "tk_usage")
    import threadkeeper._spawn_wrap as w
    w._record(
        db_path,
        "tk_usage",
        0,
        "input tokens: 1,200\noutput tokens: 34\ntotal cost: $0.0045\n",
    )
    row = _read(db, "tk_usage")
    assert row["tokens_in"] == 1200
    assert row["tokens_out"] == 34
    assert row["tokens_total"] == 1234
    assert row["cost_usd"] == 0.0045
    assert row["duration_s"] is not None


# ── run-and-record end-to-end (real subprocess) ──────────────────────────

def test_run_and_record_success(db_mod):
    db, db_path = db_mod
    _mk_task(db, "tk_ok")
    r = _run_wrap([db_path, "tk_ok", "--", "true"])
    assert r.returncode == 0
    row = _read(db, "tk_ok")
    assert row["return_code"] == 0
    assert row["ended_at"] is not None


def test_run_and_record_failure(db_mod):
    db, db_path = db_mod
    _mk_task(db, "tk_fail")
    r = _run_wrap([db_path, "tk_fail", "--", "false"])
    assert r.returncode == 1
    row = _read(db, "tk_fail")
    assert row["return_code"] == 1


def test_run_and_record_parses_child_usage_output(db_mod):
    db, db_path = db_mod
    _mk_task(db, "tk_usage_e2e")
    child = "print('answer'); print('tokens used'); print('9,876')"
    r = _run_wrap([db_path, "tk_usage_e2e", "--", sys.executable, "-c", child])
    assert r.returncode == 0
    assert "tokens used" in r.stdout
    row = _read(db, "tk_usage_e2e")
    assert row["tokens_total"] == 9876


def test_run_missing_binary_records_127(db_mod):
    db, db_path = db_mod
    _mk_task(db, "tk_nobin")
    r = _run_wrap([db_path, "tk_nobin", "--",
                   "this_binary_does_not_exist_xyzzy"])
    assert r.returncode == 127
    row = _read(db, "tk_nobin")
    assert row["return_code"] == 127


# ── --record shell mode (visible/Terminal path) ──────────────────────────

def test_record_mode_writes_code(db_mod):
    db, db_path = db_mod
    _mk_task(db, "tk_rec")
    r = _run_wrap(["--record", db_path, "tk_rec", "42"])
    assert r.returncode == 0
    row = _read(db, "tk_rec")
    assert row["return_code"] == 42
    assert row["ended_at"] is not None


def test_record_mode_bad_rc_defaults(db_mod):
    db, db_path = db_mod
    _mk_task(db, "tk_recbad")
    r = _run_wrap(["--record", db_path, "tk_recbad", "not_a_number"])
    assert r.returncode == 0
    row = _read(db, "tk_recbad")
    assert row["return_code"] == 1  # non-numeric rc → fallback 1


# ── usage errors ─────────────────────────────────────────────────────────

def test_usage_errors_return_2(db_mod):
    _, db_path = db_mod
    assert _run_wrap([db_path, "tk_u", "--"]).returncode == 2  # no child cmd
    assert _run_wrap(["only_one_arg"]).returncode == 2         # too few args


# ── signal forwarding (task_kill compatibility) ──────────────────────────

def test_signal_forwarded_to_child_and_recorded(db_mod):
    """task_kill sends SIGTERM to the tracked pid — which is now the wrapper.
    The wrapper must forward it to the real child, and the child's
    signal-death must be recorded as a negative return_code."""
    db, db_path = db_mod
    _mk_task(db, "tk_sig")
    proc = subprocess.Popen(
        [sys.executable, str(_WRAP), db_path, "tk_sig", "--", "sleep", "30"],
        start_new_session=True,
    )
    # Give the wrapper time to launch the child and install handlers.
    time.sleep(1.5)
    proc.send_signal(signal.SIGTERM)
    rc = proc.wait(timeout=10)
    # Wrapper encodes signal-death shell-style in its own exit (128+15).
    assert rc == 128 + signal.SIGTERM
    row = _read(db, "tk_sig")
    # Child was killed by SIGTERM → waitstatus negative → stored as -15.
    assert row["return_code"] == -signal.SIGTERM
    assert row["ended_at"] is not None


def test_group_kill_reaps_orphan_child(db_mod):
    """SIGKILL is uncatchable, so the wrapper can't forward it — a pid-only
    kill of the wrapper would orphan the live child. task_kill signals the
    process GROUP instead. This replicates that path end-to-end: launch the
    wrapper detached (group leader, as the real launcher does), SIGKILL the
    group, and assert the *real child* (not just the wrapper) is dead."""
    db, db_path = db_mod
    _mk_task(db, "tk_grp")
    pidfile = Path(db_path).parent / "child.pid"
    child = (
        "import os,sys,time; "
        "open(sys.argv[1],'w').write(str(os.getpid())); "
        "time.sleep(30)"
    )
    proc = subprocess.Popen(
        [sys.executable, str(_WRAP), db_path, "tk_grp", "--",
         sys.executable, "-c", child, str(pidfile)],
        start_new_session=True,
    )
    # Wait for the real child to come up and report its pid.
    deadline = time.time() + 10
    while time.time() < deadline and not pidfile.exists():
        time.sleep(0.05)
    assert pidfile.exists(), "child never started"
    child_pid = int(pidfile.read_text())

    # The wrapper is the group leader (start_new_session=True); the child
    # shares its group. Kill the group — exactly what task_kill(force=True)
    # does via killpg(getpgid(pid)).
    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    proc.wait(timeout=10)

    # The real child must be gone, not orphaned.
    gone = False
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            gone = True
            break
        time.sleep(0.05)
    assert gone, f"child {child_pid} orphaned after group kill"


def test_task_kill_refuses_pid_zero(fresh_mp):
    """Visible/Terminal tasks store pid=0. task_kill must refuse rather than
    os.kill(0, …), which would signal the server's own process group."""
    import threadkeeper.tools.spawn as sp
    conn = fresh_mp["db"].get_db()
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at) "
        "VALUES (?,?,?,?,?)",
        ("tk_vis", 0, "/tmp", "x", int(time.time())),
    )
    conn.commit()
    out = sp.task_kill("tk_vis")
    assert "not_killable_by_pid" in out
    # Row must remain open — we didn't (and can't) kill it by pid.
    row = conn.execute(
        "SELECT ended_at FROM tasks WHERE id=?", ("tk_vis",)
    ).fetchone()
    assert row["ended_at"] is None
