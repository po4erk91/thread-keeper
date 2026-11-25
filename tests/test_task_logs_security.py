from __future__ import annotations

import os
import stat

import pytest


def register_task(pkg, task_id="tk_allowed", capture_output=1):
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at, capture_output) "
        "VALUES (?, 0, '.', '', 0, ?)", (task_id, capture_output),
    )
    conn.commit()


def tool(pkg):
    return pkg["mcp"]._tool_manager._tools["task_logs"].fn


def test_registered_log_tail_and_missing_log(fresh_mp):
    register_task(fresh_mp)
    spool = fresh_mp["config"].TASK_LOG_DIR
    spool.mkdir()
    (spool / "tk_allowed.log").write_bytes(b"first\nsecond\nthird\xff\n")
    assert tool(fresh_mp)("tk_allowed", 2) == "second\nthird\ufffd"
    assert tool(fresh_mp)("tk_allowed", 0) == "first\nsecond\nthird\ufffd"
    (spool / "tk_allowed.log").unlink()
    assert tool(fresh_mp)("tk_allowed").startswith("no_log path=")


@pytest.mark.parametrize("task_id", ["../outside", "tk_../outside", "/tmp/outside", "tk_x/y", "tk_x\\\\y", "tk_ x", "", "tk_x\x00"])
def test_malformed_ids_do_not_touch_spool(fresh_mp, task_id, monkeypatch):
    from threadkeeper import task_spool
    monkeypatch.setattr(task_spool.os, "open", lambda *a, **kw: pytest.fail("opened a file"))
    assert tool(fresh_mp)(task_id) == "ERR invalid_task_id"


def test_unknown_and_uncaptured_tasks_do_not_open_logs(fresh_mp, monkeypatch):
    from threadkeeper.tools import spawn
    register_task(fresh_mp, capture_output=0)
    monkeypatch.setattr(spawn, "open_task_log_read", lambda *a: pytest.fail("opened a log"))
    assert tool(fresh_mp)("tk_unknown") == "ERR task_not_found=tk_unknown"
    assert tool(fresh_mp)("tk_allowed").startswith("no_log path=")


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink and mode checks")
@pytest.mark.parametrize("substitution", ["spool", "ancestor", "file", "hardlink"])
def test_substituted_paths_refused_without_chmod_or_read(fresh_mp, tmp_path, monkeypatch, substitution):
    from threadkeeper.tools import spawn
    register_task(fresh_mp)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    target = outside / "tk_allowed.log"
    target.write_text("outside secret\n")
    target.chmod(0o644)
    spool = tmp_path / "parent" / "tasks"
    if substitution == "ancestor":
        (outside / "tasks").mkdir()
        (outside / "tasks" / "tk_allowed.log").write_text("outside secret\n")
        (tmp_path / "parent").symlink_to(outside, target_is_directory=True)
    else:
        spool.parent.mkdir()
        if substitution == "spool":
            spool.symlink_to(outside, target_is_directory=True)
        else:
            spool.mkdir(mode=0o755)
            if substitution == "file":
                (spool / target.name).symlink_to(target)
            else:
                os.link(target, spool / target.name)
    monkeypatch.setattr(spawn, "TASK_LOG_DIR", spool)
    before = (stat.S_IMODE(outside.stat().st_mode), stat.S_IMODE(target.stat().st_mode))
    result = tool(fresh_mp)("tk_allowed")
    assert result.startswith("ERR read_failed=")
    assert "outside secret" not in result
    assert (stat.S_IMODE(outside.stat().st_mode), stat.S_IMODE(target.stat().st_mode)) == before
    if spool.is_dir() and not spool.is_symlink():
        assert stat.S_IMODE(spool.stat().st_mode) == 0o755


def test_traversal_cannot_read_existing_outside_log(fresh_mp, tmp_path):
    register_task(fresh_mp, "tk_../outside")
    outside = tmp_path / "outside.log"
    outside.write_text("outside secret\n")
    tmp_path.chmod(0o755)
    assert tool(fresh_mp)("tk_../outside") == "ERR invalid_task_id"
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755
    assert outside.read_text() == "outside secret\n"
