"""Isolation coverage for project namespaces and explicit recall overrides."""
from __future__ import annotations

import subprocess
import time


PROJECT_A = "prj_aaaaaaaaaaaaaaaaaaaa"
PROJECT_B = "prj_bbbbbbbbbbbbbbbbbbbb"


def _tool(pkg, name):
    fn = pkg["mcp"]._tool_manager._tools[name].fn
    if name in {"search", "dialog_search"}:
        return lambda *args, **kwargs: fn(*args, **kwargs).content[0].text
    return fn


def _seed_note(conn, *, suffix: str, project_id: str | None,
               memory_scope: str = "project") -> None:
    now = int(time.time())
    tid = f"T-{suffix}"
    conn.execute(
        "INSERT INTO threads (id, question, state, opened_at, last_touched_at, depth, "
        "project_id, memory_scope) VALUES (?,?,?,?,?,?,?,?)",
        (tid, f"thread {suffix}", "active", now, now, 0,
         project_id, memory_scope),
    )
    conn.execute(
        "INSERT INTO notes (thread_id, content, kind, created_at, project_id, memory_scope) "
        "VALUES (?,?,?,?,?,?)",
        (tid, f"overlap-memory-{suffix}", "insight", now,
         project_id, memory_scope),
    )


def _seed_dialog(conn, *, suffix: str, project_id: str | None,
                 memory_scope: str = "project") -> None:
    conn.execute(
        "INSERT INTO dialog_messages (uuid, source, project, session_id, role, content, "
        "created_at, project_id, memory_scope) VALUES (?,?,?,?,?,?,?,?,?)",
        (f"dialog-{suffix}", "test", "test", f"session-{suffix}", "user",
         f"overlap-dialog-{suffix}", int(time.time()), project_id, memory_scope),
    )


def test_default_recall_isolated_and_cross_project_is_audited(fresh_mp, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_PROJECT_NAMESPACE", PROJECT_A)
    conn = fresh_mp["db"].get_db()
    try:
        _seed_note(conn, suffix="a", project_id=PROJECT_A)
        _seed_note(conn, suffix="b", project_id=PROJECT_B)
        _seed_note(conn, suffix="global", project_id=None, memory_scope="global")
        _seed_note(conn, suffix="legacy", project_id=None, memory_scope="unresolved")
        _seed_dialog(conn, suffix="a", project_id=PROJECT_A)
        _seed_dialog(conn, suffix="b", project_id=PROJECT_B)
        _seed_dialog(conn, suffix="global", project_id=None, memory_scope="shared")
        _seed_dialog(conn, suffix="legacy", project_id=None, memory_scope="unresolved")
        conn.commit()
    finally:
        conn.close()

    search = _tool(fresh_mp, "search")
    default_notes = search("overlap-memory", k=10)
    assert "overlap-memory-a" in default_notes
    assert "overlap-memory-global" in default_notes
    assert "overlap-memory-b" not in default_notes
    assert "overlap-memory-legacy" not in default_notes

    cross_notes = search("overlap-memory", k=10, cross_project=True)
    assert "overlap-memory-b" in cross_notes
    assert "overlap-memory-legacy" not in cross_notes
    assert "scope=project project=" + PROJECT_B in cross_notes

    dialogs = _tool(fresh_mp, "dialog_search")
    default_dialogs = dialogs("overlap-dialog", k=10, mode="fts")
    assert "overlap-dialog-a" in default_dialogs
    assert "overlap-dialog-global" in default_dialogs
    assert "overlap-dialog-b" not in default_dialogs
    cross_dialogs = dialogs("overlap-dialog", k=10, mode="fts", cross_project=True)
    assert "overlap-dialog-b" in cross_dialogs
    assert "overlap-dialog-legacy" not in cross_dialogs

    conn = fresh_mp["db"].get_db()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='cross_project_recall'"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_namespace_uses_remote_and_is_stable_for_a_worktree(tmp_path):
    from threadkeeper.project_scope import resolve_project_namespace

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "tracked.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "git@github.example:team/private-repo.git"], check=True)
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", str(worktree)],
                   check=True, capture_output=True)

    primary = resolve_project_namespace(repo)
    sibling = resolve_project_namespace(worktree)

    assert primary.project_id == sibling.project_id
    assert primary.source == "remote"
    assert str(repo) not in primary.project_id
