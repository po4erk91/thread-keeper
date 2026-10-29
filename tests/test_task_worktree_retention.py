"""Exercise cleanup against real disposable repositories and Git metadata."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from tests.test_spawn_worktree_isolation import _git, _init_repo
from tests.test_task_retention import _seed_task, _tool, _FAKE_CID


@pytest.fixture
def owned(mp_with_cid, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_TASK_RETENTION_DAYS", "7")
    monkeypatch.setenv("THREADKEEPER_TASK_RETENTION_COUNT", "0")
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper.tools import spawn
    from threadkeeper import task_worktrees

    repo = _init_repo(pkg["tmp"])
    conn = pkg["db"].get_db()
    task = "tk_cleanup"
    now = int(time.time())
    _seed_task(conn, task, now - 40 * 86400, now - 39 * 86400)
    conn.commit()
    plan, error = spawn._spawn_worktree_plan(str(repo), task)
    assert not error
    assert spawn._create_spawn_worktree(plan) == ""
    return pkg, conn, repo, plan, task_worktrees


def _consolidate(pkg, dry_run=False):
    return _tool(pkg, "consolidate")(dry_run=dry_run)


def _claim(conn):
    return dict(conn.execute("SELECT * FROM task_worktrees").fetchone())


def test_dry_run_preserves_git_files_modes_and_task_records(owned):
    pkg, conn, repo, plan, _ = owned
    before = _claim(conn)
    refs = _git(repo, "show-ref")
    trees = _git(repo, "worktree", "list", "--porcelain")
    spool = pkg["config"].TASK_LOG_DIR
    spool.chmod(0o750)
    mode = spool.stat().st_mode
    content = (plan.path / "tracked.txt").read_bytes()

    output = _consolidate(pkg, dry_run=True)

    assert "worktree_gc=1" in output
    assert f"worktree_bytes={len(content) + (plan.path / '.git').stat().st_size}" in output
    assert _claim(conn) == before
    assert conn.execute("SELECT 1 FROM tasks WHERE id=?", (plan.path.name,)).fetchone()
    assert spool.stat().st_mode == mode
    assert _git(repo, "show-ref") == refs
    assert _git(repo, "worktree", "list", "--porcelain") == trees
    assert (plan.path / "tracked.txt").read_bytes() == content


def test_expired_checkout_and_owned_branch_are_removed(owned):
    pkg, conn, repo, plan, _ = owned
    output = _consolidate(pkg)
    assert "worktrees_removed=1" in output
    assert "worktree_bytes_removed=0" not in output
    assert not plan.path.exists()
    assert plan.branch not in _git(repo, "branch", "--list")
    assert str(plan.path) not in _git(repo, "worktree", "list", "--porcelain")
    assert conn.execute("SELECT COUNT(*) FROM task_worktrees").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert "worktree_gc=0" in _consolidate(pkg)


@pytest.mark.parametrize("ended,reason", [(None, "live_task"), ("recent", "within_retention")])
def test_live_and_recent_tasks_are_retained(owned, ended, reason):
    pkg, conn, repo, plan, _ = owned
    ended_at = int(time.time()) if ended == "recent" else None
    conn.execute("UPDATE tasks SET ended_at=?", (ended_at,))
    conn.commit()
    output = _consolidate(pkg)
    assert reason in output
    assert "worktree_gc=0" in output
    assert plan.path.exists()
    assert plan.branch in _git(repo, "branch", "--list")
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


@pytest.mark.parametrize("kind", ["tracked", "untracked", "ignored", "unpublished"])
def test_recoverable_work_survives_pruning_and_can_be_retried(owned, kind):
    pkg, conn, repo, plan, _ = owned
    file = plan.path / ("tracked.txt" if kind in ("tracked", "unpublished") else "scratch")
    file.write_text("keep this work\n")
    if kind == "ignored":
        (repo / ".git" / "info" / "exclude").write_text("scratch\n")
    if kind == "unpublished":
        _git(plan.path, "add", "tracked.txt")
        _git(plan.path, "commit", "-m", "child work")
    output = _consolidate(pkg)
    reason = "commits_not_retained_elsewhere" if kind == "unpublished" else "dirty_untracked_or_ignored_files"
    assert reason in output
    assert plan.path.exists()
    assert file.read_text() == "keep this work\n"
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert _claim(conn)["eligible"] == 1
    if kind == "unpublished":
        _git(repo, "merge", "--ff-only", plan.branch)
    elif kind == "tracked":
        _git(plan.path, "restore", "tracked.txt")
    else:
        file.unlink()
    assert "worktrees_removed=1" in _consolidate(pkg)


def test_published_tip_is_retained_in_remote_tracking_ref(owned):
    pkg, _, repo, plan, _ = owned
    (plan.path / "tracked.txt").write_text("published work\n")
    _git(plan.path, "commit", "-am", "child work")
    tip = _git(plan.path, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/published", tip)
    assert "worktrees_removed=1" in _consolidate(pkg)
    assert _git(repo, "rev-parse", "refs/remotes/origin/published") == tip


@pytest.mark.parametrize("change,reason", [
    ("branch", "worktree_branch_changed_or_unregistered"),
    ("detached", "worktree_branch_changed_or_unregistered"),
    ("locked", "worktree_locked"),
    ("owner", "branch_ownership_mismatch"),
    ("path", "path_outside_owned_spool"),
    ("branch_claim", "branch_ownership_mismatch"),
    ("repo", "repository_identity_changed"),
])
def test_changed_ownership_or_git_use_is_retained(owned, change, reason):
    pkg, conn, repo, plan, _ = owned
    if change == "branch":
        _git(plan.path, "checkout", "-b", "user-work")
    elif change == "detached":
        _git(plan.path, "checkout", "--detach")
    elif change == "locked":
        _git(repo, "worktree", "lock", str(plan.path))
    elif change == "owner":
        _git(repo, "config", f"branch.{plan.branch}.threadkeeperTask", "tk_someone_else")
    elif change == "path":
        conn.execute("UPDATE task_worktrees SET path=?", (str(repo),))
    elif change == "branch_claim":
        conn.execute("UPDATE task_worktrees SET branch='main'")
    else:
        conn.execute("UPDATE task_worktrees SET repo_ino=0")
    conn.commit()
    output = _consolidate(pkg)
    assert reason in output
    assert plan.path.exists()
    assert plan.branch in _git(repo, "branch", "--list")
    assert conn.execute("SELECT COUNT(*) FROM task_worktrees").fetchone()[0] == 1


@pytest.mark.parametrize("replacement", ["symlink", "directory", "gitfile"])
def test_substituted_checkout_is_never_removed(owned, replacement):
    pkg, _, repo, plan, _ = owned
    if replacement == "gitfile":
        git_file = plan.path / ".git"
        git_file.unlink()
        git_file.symlink_to(repo / ".git" / "HEAD")
        expected = "checkout_git_file_changed"
    else:
        saved = pkg["tmp"] / "saved-checkout"
        plan.path.rename(saved)
        if replacement == "symlink":
            plan.path.symlink_to(saved, target_is_directory=True)
            expected = "symlink_or_non_directory"
        else:
            plan.path.mkdir()
            (plan.path / "user.txt").write_text("unrelated")
            expected = "worktree_identity_changed"
    assert expected in _consolidate(pkg)
    assert plan.path.exists()
    assert plan.branch in _git(repo, "branch", "--list")


def test_branch_in_another_worktree_is_retained(owned):
    pkg, _, repo, plan, _ = owned
    extra = pkg["tmp"] / "other-worktree"
    _git(repo, "worktree", "add", "--force", str(extra), plan.branch)
    assert "branch_used_by_other_worktree" in _consolidate(pkg)
    assert plan.path.exists()
    assert extra.exists()


def test_checkout_used_by_another_live_task_is_retained(owned):
    pkg, conn, _, plan, _ = owned
    _seed_task(conn, "tk_live_user", int(time.time()), None)
    conn.execute("UPDATE tasks SET cwd=? WHERE id='tk_live_user'", (str(plan.path),))
    conn.commit()
    assert "live_task_using_checkout" in _consolidate(pkg)
    assert plan.path.exists()


def test_failure_after_worktree_removal_keeps_evidence_for_retry(owned, monkeypatch):
    pkg, conn, repo, plan, module = owned
    original = module._git

    def fail_branch_delete(repo_path, *args):
        if args[0] == "update-ref":
            raise OSError("injected_branch_failure")
        return original(repo_path, *args)

    monkeypatch.setattr(module, "_git", fail_branch_delete)
    output = _consolidate(pkg)
    assert "worktrees_failed=1" in output
    assert "checkouts_removed=1" in output
    assert "branches_failed=1" in output
    assert "worktree_bytes_removed=0" not in output
    assert "injected_branch_failure" in output
    assert not plan.path.exists()
    assert plan.branch in _git(repo, "branch", "--list")
    assert _claim(conn)["eligible"] == 1
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    monkeypatch.setattr(module, "_git", original)
    assert "worktrees_removed=1" in _consolidate(pkg)
    assert conn.execute("SELECT COUNT(*) FROM task_worktrees").fetchone()[0] == 0
    event = conn.execute("SELECT summary FROM events WHERE kind='consolidate_apply' ORDER BY id DESC LIMIT 1").fetchone()
    assert "worktrees_removed=1" in event[0]


def test_missing_checkout_with_git_registration_is_retryable(owned):
    pkg, _, repo, plan, _ = owned
    shutil.rmtree(plan.path)
    assert "worktrees_removed=1" in _consolidate(pkg)
    assert str(plan.path) not in _git(repo, "worktree", "list", "--porcelain")


def test_owned_orphan_can_be_cleaned_but_unknown_orphan_is_preserved(owned):
    pkg, conn, repo, plan, _ = owned
    conn.execute("UPDATE task_worktrees SET eligible=1")
    conn.execute("DELETE FROM tasks")
    conn.commit()
    unknown = plan.path.parent / "tk_unknown"
    _git(repo, "worktree", "add", "-b", "threadkeeper/spawn-tk_unknown", str(unknown))
    output = _consolidate(pkg)
    assert "worktrees_removed=1" in output
    assert "ownership_unknown" in output
    assert unknown.exists()
    assert "threadkeeper/spawn-tk_unknown" in _git(repo, "branch", "--list")


def test_branch_changed_during_delete_is_not_lost(owned, monkeypatch):
    pkg, conn, repo, plan, module = owned
    original = module._git
    # Create an unretained commit object without changing the checkout.
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    tip = _git(repo, "commit-tree", tree, "-p", "HEAD", "-m", "concurrent work")

    def advance_branch(repo_path, *args):
        if args[0] == "update-ref":
            _git(repo, "update-ref", "refs/heads/" + plan.branch, tip)
        return original(repo_path, *args)

    monkeypatch.setattr(module, "_git", advance_branch)
    assert "worktrees_failed=1" in _consolidate(pkg)
    assert _git(repo, "rev-parse", plan.branch) == tip
    assert _claim(conn)["eligible"] == 1
    assert "commits_not_retained_elsewhere" in _consolidate(pkg)


@pytest.mark.parametrize("boundary", ["count", "disabled"])
def test_count_protection_and_disabled_pruning_keep_checkouts(owned, monkeypatch, boundary):
    pkg, _, _, plan, _ = owned
    import threadkeeper.tools.consolidate as module
    monkeypatch.setattr(module, "TASK_RETENTION_DAYS", 0)
    monkeypatch.setattr(module, "TASK_RETENTION_COUNT", 1 if boundary == "count" else 0)
    assert "within_retention_or_unknown_end" in _consolidate(pkg)
    assert plan.path.exists()


def test_dry_run_does_not_refresh_git_index(owned):
    pkg, conn, _, plan, _ = owned
    import os
    index = Path(_claim(conn)["git_dir"]) / "index"
    before = index.read_bytes(), index.stat().st_mtime_ns
    tracked = plan.path / "tracked.txt"
    os.utime(tracked, ns=(tracked.stat().st_atime_ns, tracked.stat().st_mtime_ns + 1_000_000_000))
    assert "worktree_gc=1" in _consolidate(pkg, dry_run=True)
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


def test_creation_collision_never_deletes_existing_branch(owned):
    pkg, conn, repo, plan, _ = owned
    from threadkeeper.tools import spawn
    task = "tk_collision"
    branch = "threadkeeper/spawn-" + task
    _git(repo, "branch", branch)
    candidate, error = spawn._spawn_worktree_plan(str(repo), task)
    assert not error
    assert "ERR spawn_worktree_create_failed" in spawn._create_spawn_worktree(candidate)
    output = _consolidate(pkg)
    assert "branch_ownership_unverified" in output
    assert branch in _git(repo, "branch", "--list")
    assert conn.execute("SELECT eligible FROM task_worktrees WHERE task_id=?", (task,)).fetchone()[0] == 1


def test_existing_version_database_gets_additive_ownership_table(owned):
    pkg, conn, _, _, _ = owned
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.execute("DROP TABLE task_worktrees")
    conn.commit()
    pkg["db"]._ensure_additive_runtime_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM task_worktrees").fetchone()[0] == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == version


@pytest.mark.parametrize("history", ["branch", "detached"])
def test_unpublished_reflog_work_is_preserved(owned, history):
    pkg, _, repo, plan, _ = owned
    if history == "detached":
        _git(plan.path, "checkout", "--detach")
    (plan.path / "tracked.txt").write_text("recoverable work\n")
    _git(plan.path, "commit", "-am", "work saved only in reflog")
    recovery_tip = _git(plan.path, "rev-parse", "HEAD")
    if history == "branch":
        _git(plan.path, "reset", "--hard", "main")
    else:
        _git(plan.path, "checkout", plan.branch)
    assert "reflog_commits_not_retained_elsewhere" in _consolidate(pkg)
    assert plan.path.exists()
    _git(repo, "branch", "saved-work", recovery_tip)
    assert "worktrees_removed=1" in _consolidate(pkg)


def test_symbolic_branch_cannot_redirect_cleanup_to_an_unrelated_branch(owned):
    pkg, _, repo, plan, _ = owned
    _git(repo, "worktree", "remove", str(plan.path))
    _git(repo, "symbolic-ref", "refs/heads/" + plan.branch, "refs/heads/main")
    main_tip = _git(repo, "rev-parse", "main")
    assert "symbolic_branch_ownership_unverified" in _consolidate(pkg)
    assert _git(repo, "rev-parse", "main") == main_tip
    assert _git(repo, "symbolic-ref", "refs/heads/" + plan.branch) == "refs/heads/main"


@pytest.mark.parametrize("checkout_present", [True, False])
def test_legacy_task_history_is_not_pruned_without_cleanup_ownership(owned, checkout_present):
    pkg, conn, repo, plan, _ = owned
    conn.execute("DELETE FROM task_worktrees")
    conn.execute("UPDATE tasks SET cwd=?", (str(plan.path / 'subdirectory'),))
    conn.commit()
    if not checkout_present:
        _git(repo, "worktree", "remove", str(plan.path))
    output = _consolidate(pkg)
    assert "task_prune=0" in output
    assert conn.execute("SELECT 1 FROM tasks WHERE id=?", (plan.path.name,)).fetchone()
    assert plan.branch in _git(repo, "branch", "--list")
