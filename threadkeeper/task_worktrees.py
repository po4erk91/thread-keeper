"""Owned task checkouts survive history pruning until Git cleanup succeeds."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import stat
import subprocess

_GIT_POPEN = subprocess.Popen
BRANCH_PREFIX = "threadkeeper/spawn-"


def _git(repo: Path, *args: str) -> str:
    proc = _GIT_POPEN(
        ["git", "--git-dir", str(repo), *args], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise OSError("git_timeout") from None
    if proc.returncode:
        raise OSError((stderr or stdout).strip()[:240] or "git_failed")
    return stdout.strip()


def _owned_directory(path: Path) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("symlink_or_non_directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise OSError("foreign_owner")
    return info


def register_worktree(conn: sqlite3.Connection, task: str, source: Path,
                      path: Path, branch: str) -> None:
    """Persist intent before creation; an incomplete claim never permits removal."""
    from .tools.spawn import _git_result

    common, error = _git_result(["rev-parse", "--git-common-dir"], source)
    if error:
        raise OSError(error)
    repo = (source / common).resolve()
    info = _owned_directory(repo)
    conn.execute(
        "INSERT INTO task_worktrees (task_id, repo, repo_dev, repo_ino, path, branch) "
        "VALUES (?,?,?,?,?,?)",
        (task, str(repo), info.st_dev, info.st_ino, str(path.absolute()), branch),
    )


def confirm_worktree(conn: sqlite3.Connection, task: str, path: Path) -> None:
    from .tools.spawn import _git_result

    row = conn.execute("SELECT * FROM task_worktrees WHERE task_id=?", (task,)).fetchone()
    info = _owned_directory(path)
    git_dir, error = _git_result(["rev-parse", "--absolute-git-dir"], path)
    if error:
        raise OSError(error)
    _git(Path(row["repo"]), "config", f"branch.{row['branch']}.threadkeeperTask", task)
    conn.execute("UPDATE task_worktrees SET path_dev=?, path_ino=?, git_dir=? WHERE task_id=?",
                 (info.st_dev, info.st_ino, str(Path(git_dir).resolve()), task))


def _worktrees(repo: Path) -> list[dict]:
    output = _git(repo, "worktree", "list", "--porcelain", "-z")
    entries: list[dict] = []
    current: dict = {}
    for field in output.split("\0"):
        if not field:
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = field.partition(" ")
        current[key] = value
    if current:
        entries.append(current)
    return entries


def _tree_bytes(path: Path) -> int:
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
        for name in files:
            info = (Path(root) / name).lstat()
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def _check_paths(row: dict, spool: Path) -> tuple[Path, Path]:
    _owned_directory(spool)
    parent = spool / "worktrees"
    _owned_directory(parent)
    path = Path(row["path"])
    if path.parent != parent.absolute() or path.name != row["task_id"]:
        raise OSError("path_outside_owned_spool")
    if row["branch"] != BRANCH_PREFIX + row["task_id"]:
        raise OSError("branch_ownership_mismatch")
    repo = Path(row["repo"])
    info = _owned_directory(repo)
    if (info.st_dev, info.st_ino) != (row["repo_dev"], row["repo_ino"]):
        raise OSError("repository_identity_changed")
    if path.exists() or path.is_symlink():
        info = _owned_directory(path)
        if (info.st_dev, info.st_ino) != (row["path_dev"], row["path_ino"]):
            raise OSError("worktree_identity_changed")
    return repo, path


def _live_user(conn: sqlite3.Connection, path: Path) -> bool:
    for row in conn.execute("SELECT cwd FROM tasks WHERE ended_at IS NULL"):
        if Path(row["cwd"]).resolve().is_relative_to(path.resolve()):
            return True
    return False


def _retained_tip(repo: Path, tip: str) -> bool:
    refs = _git(repo, "for-each-ref", "--format=%(refname)", "--contains=" + tip,
                "refs/heads/", "refs/remotes/").splitlines()
    return any(ref.startswith("refs/remotes/") or
               (ref.startswith("refs/heads/") and
                not ref.startswith("refs/heads/" + BRANCH_PREFIX)) for ref in refs)


def _check_reflog_work(repo: Path, row: dict, target: dict | None) -> None:
    revisions = set(_git(repo, "reflog", "show", "--format=%H",
                         "refs/heads/" + row["branch"]).splitlines())
    if target is not None:
        if not row["git_dir"]:
            raise OSError("incomplete_checkout_ownership")
        git_dir = Path(row["git_dir"])
        _owned_directory(git_dir)
        revisions.update(_git(git_dir, "reflog", "show", "--format=%H", "HEAD").splitlines())
    if not revisions:
        return
    refs = _git(repo, "for-each-ref", "--format=%(refname)",
                "refs/heads/", "refs/remotes/").splitlines()
    retained = [ref for ref in refs if not ref.startswith("refs/heads/" + BRANCH_PREFIX)]
    if not retained or _git(repo, "rev-list", "--max-count=1", *sorted(revisions), "--not", *retained):
        raise OSError("reflog_commits_not_retained_elsewhere")


def _checkout_bytes(repo: Path, path: Path, row: dict, target: dict | None) -> int:
    if not path.exists():
        return 0
    if target is None or target.get("branch") != "refs/heads/" + row["branch"]:
        raise OSError("worktree_branch_changed_or_unregistered")
    from .tools.spawn import _git_result

    if not stat.S_ISREG((path / ".git").lstat().st_mode):
        raise OSError("checkout_git_file_changed")
    git_dir, error = _git_result(["rev-parse", "--absolute-git-dir"], path)
    if error or str(Path(git_dir).resolve()) != row["git_dir"]:
        raise OSError("checkout_git_directory_changed")
    common, error = _git_result(["rev-parse", "--git-common-dir"], path)
    if error or (path / common).resolve() != repo:
        raise OSError("checkout_repository_mismatch")
    dirty, error = _git_result(
        ["--no-optional-locks", "status", "--porcelain", "--untracked-files=all",
         "--ignored=matching"], path)
    if error:
        raise OSError(error)
    if dirty:
        raise OSError("dirty_untracked_or_ignored_files")
    return _tree_bytes(path)


def _safe_branch_tip(repo: Path, row: dict, target: dict | None) -> str:
    branch_ref = "refs/heads/" + row["branch"]
    refs = _git(repo, "for-each-ref", "--format=%(objectname) %(symref)", branch_ref).splitlines()
    tip, _, symbolic_target = refs[0].partition(" ") if refs else ("", "", "")
    if symbolic_target:
        raise OSError("symbolic_branch_ownership_unverified")
    if not tip:
        if target is not None:
            raise OSError("registered_worktree_branch_missing")
        return ""
    try:
        owner = _git(repo, "config", "--get", f"branch.{row['branch']}.threadkeeperTask")
    except OSError:
        raise OSError("branch_ownership_unverified") from None
    if owner != row["task_id"]:
        raise OSError("branch_ownership_mismatch")
    if not _retained_tip(repo, tip):
        raise OSError("commits_not_retained_elsewhere")
    _check_reflog_work(repo, row, target)
    return tip


def inspect_worktree(conn: sqlite3.Connection, row: dict, spool: Path,
                     expired: set[str]) -> dict:
    result = {"task": row["task_id"], "bytes": 0, "status": "retained", "reason": ""}
    task = conn.execute("SELECT ended_at FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
    if task is not None and task["ended_at"] is None:
        return {**result, "reason": "live_task"}
    if not row["eligible"] and row["task_id"] not in expired:
        return {**result, "reason": "within_retention_or_unknown_end"}
    try:
        repo, path = _check_paths(row, spool)
        if _live_user(conn, path):
            raise OSError("live_task_using_checkout")
        entries = _worktrees(repo)
        target = next((e for e in entries if Path(e["worktree"]).resolve() == path.resolve()), None)
        branch_ref = "refs/heads/" + row["branch"]
        if any(e.get("branch") == branch_ref and e is not target for e in entries):
            raise OSError("branch_used_by_other_worktree")
        if target and "locked" in target:
            raise OSError("worktree_locked")
        result["bytes"] = _checkout_bytes(repo, path, row, target)
        tip = _safe_branch_tip(repo, row, target)
        return {**result, "status": "candidate", "reason": "safe", "tip": tip,
                "checkout_status": "candidate" if path.exists() or target else "absent",
                "branch_status": "candidate" if tip else "absent"}
    except (OSError, ValueError) as error:
        return {**result, "reason": str(error)}


def worktree_candidates(conn: sqlite3.Connection, spool: Path, expired: set[str]) -> list[dict]:
    rows = [dict(row) for row in conn.execute("SELECT * FROM task_worktrees ORDER BY task_id")]
    results = [inspect_worktree(conn, row, spool, expired) for row in rows]
    known = {row["path"] for row in rows}
    try:
        _owned_directory(spool)
        parent = spool / "worktrees"
        _owned_directory(parent)
        for path in sorted(parent.iterdir()):
            if str(path.absolute()) not in known:
                results.append({"task": path.name, "bytes": 0, "status": "retained",
                                "reason": "ownership_unknown"})
    except OSError:
        pass
    return results


def remove_worktree(conn: sqlite3.Connection, spool: Path, task: str) -> dict:
    """Recheck immediately before Git removal; keep evidence through partial failure."""
    row = conn.execute("SELECT * FROM task_worktrees WHERE task_id=?", (task,)).fetchone()
    if row is None:
        return {"task": task, "status": "retained", "reason": "ownership_unknown", "bytes": 0}
    row = dict(row)
    checked = inspect_worktree(conn, row, spool, set())
    if checked["status"] != "candidate":
        return checked
    checked["bytes_removed"] = 0
    phase = "checkout_status"
    try:
        repo, path = _check_paths(row, spool)
        entries = _worktrees(repo)
        registered = any(Path(e["worktree"]).resolve() == path.resolve() for e in entries)
        if registered or path.exists():
            _git(repo, "worktree", "remove", "--", str(path))
            checked["checkout_status"] = "removed"
            checked["bytes_removed"] = checked["bytes"]
        phase = "branch_status"
        if checked["tip"]:
            branch_ref = "refs/heads/" + row["branch"]
            if any(entry.get("branch") == branch_ref for entry in _worktrees(repo)):
                raise OSError("branch_used_by_other_worktree")
            # The expected old value prevents deleting commits added after inspection.
            _git(repo, "update-ref", "--no-deref", "-d", branch_ref, checked["tip"])
            checked["branch_status"] = "removed"
        remaining = _worktrees(repo)
        if path.exists() or any(Path(e["worktree"]).resolve() == path.resolve() for e in remaining):
            raise OSError("worktree_removal_unconfirmed")
        if _git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/" + row["branch"]):
            raise OSError("branch_removal_unconfirmed")
        # Config may already be absent after an interrupted prior pass.
        try:
            _git(repo, "config", "--remove-section", "branch." + row["branch"])
        except OSError:
            pass
        conn.execute("DELETE FROM task_worktrees WHERE task_id=?", (task,))
        return {**checked, "status": "removed", "reason": "removed"}
    except OSError as error:
        checked[phase] = "failed"
        return {**checked, "status": "failed", "reason": str(error)}
