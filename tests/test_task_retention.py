from __future__ import annotations

import time


_FAKE_CID = "77778888-9999-aaaa-bbbb-ccccdddd0000"
_SPOOL_SUFFIXES = (".log", ".stdin.txt", ".command")


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _seed_task(conn, task_id: str, started_at: int, ended_at: int | None):
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, spawned_cid, cwd, prompt, "
        "started_at, ended_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            task_id,
            0,
            _FAKE_CID,
            f"child-{task_id}",
            "/tmp",
            f"prompt for {task_id}",
            started_at,
            ended_at,
        ),
    )


def _write_spool(log_dir, task_id: str):
    log_dir.mkdir(parents=True, exist_ok=True)
    for suffix in _SPOOL_SUFFIXES:
        (log_dir / f"{task_id}{suffix}").write_text(
            f"{task_id}{suffix}", encoding="utf-8"
        )


def _seed_retention_case(pkg):
    now = int(time.time())
    conn = pkg["db"].get_db()
    log_dir = pkg["config"].TASK_LOG_DIR

    _seed_task(conn, "tk_live_old", now - 40 * 86400, None)
    _seed_task(conn, "tk_recent", now - 2 * 86400, now - 1 * 86400)
    _seed_task(conn, "tk_keep_count", now - 9 * 86400, now - 8 * 86400)
    _seed_task(conn, "tk_prune", now - 21 * 86400, now - 20 * 86400)
    conn.commit()

    for task_id in (
        "tk_live_old",
        "tk_recent",
        "tk_keep_count",
        "tk_prune",
        "tk_orphan",
    ):
        _write_spool(log_dir, task_id)

    return conn, log_dir


def test_consolidate_dry_run_reports_task_retention_without_deleting(
    mp_with_cid,
    monkeypatch,
):
    monkeypatch.setenv("THREADKEEPER_TASK_RETENTION_DAYS", "7")
    monkeypatch.setenv("THREADKEEPER_TASK_RETENTION_COUNT", "2")
    pkg = mp_with_cid(_FAKE_CID)
    conn, log_dir = _seed_retention_case(pkg)

    out = _tool(pkg, "consolidate")(dry_run=True)

    assert "task_prune=1" in out
    assert "spool_gc=6" in out
    assert "task=tk_prune" in out
    assert "task=tk_orphan" in out
    assert conn.execute(
        "SELECT 1 FROM tasks WHERE id='tk_prune'"
    ).fetchone() is not None
    for task_id in ("tk_prune", "tk_orphan"):
        for suffix in _SPOOL_SUFFIXES:
            assert (log_dir / f"{task_id}{suffix}").exists()


def test_consolidate_prunes_old_ended_tasks_and_orphan_spool(
    mp_with_cid,
    monkeypatch,
):
    monkeypatch.setenv("THREADKEEPER_TASK_RETENTION_DAYS", "7")
    monkeypatch.setenv("THREADKEEPER_TASK_RETENTION_COUNT", "2")
    pkg = mp_with_cid(_FAKE_CID)
    conn, log_dir = _seed_retention_case(pkg)

    out = _tool(pkg, "consolidate")(dry_run=False)

    assert "prune_tasks=1" in out
    assert "gc_task_spool=6" in out
    remaining = {
        r["id"] for r in conn.execute("SELECT id FROM tasks").fetchall()
    }
    assert remaining == {"tk_live_old", "tk_recent", "tk_keep_count"}

    for task_id in ("tk_live_old", "tk_recent", "tk_keep_count"):
        for suffix in _SPOOL_SUFFIXES:
            assert (log_dir / f"{task_id}{suffix}").exists()
    for task_id in ("tk_prune", "tk_orphan"):
        for suffix in _SPOOL_SUFFIXES:
            assert not (log_dir / f"{task_id}{suffix}").exists()
