"""Git worktree isolation on the shared spawn path."""
from __future__ import annotations

import subprocess
from pathlib import Path


_FAKE_CID = "aaaa0000-bbbb-1111-cccc-222233334444"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "base")
    return repo


def _configure_spawn(monkeypatch, pkg):
    import threadkeeper.identity as identity
    import threadkeeper.spawn_config as spawn_config
    import threadkeeper.tools.spawn as spawn_mod

    monkeypatch.setattr(spawn_mod, "_claude_bin", lambda: "/bin/true")
    monkeypatch.setattr(identity, "_active_cli", "claude")
    monkeypatch.setattr(
        spawn_config, "resolve_agent", lambda role, active_cli=None: "claude"
    )
    monkeypatch.setattr(spawn_config, "resolve_model", lambda cli, role="": "")
    return spawn_mod


def test_git_spawns_get_distinct_branch_worktrees(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    repo = _init_repo(pkg["tmp"])
    spawn_mod = _configure_spawn(monkeypatch, pkg)
    child_cwds: list[Path] = []

    class _FakePopen:
        def __init__(self, _args, **kwargs):
            child_cwds.append(Path(kwargs["cwd"]))
            self.pid = 4200 + len(child_cwds)

    monkeypatch.setattr(spawn_mod.subprocess, "Popen", _FakePopen)

    first = spawn_mod.spawn(
        prompt="first child", cwd=str(repo), visible=False, capture_output=False
    )
    second = spawn_mod.spawn(
        prompt="second child", cwd=str(repo), visible=False, capture_output=False
    )

    assert first.startswith("ok task="), first
    assert second.startswith("ok task="), second
    assert len(child_cwds) == 2
    assert child_cwds[0] != child_cwds[1]
    assert all(path != repo and path.is_dir() for path in child_cwds)
    roots = [
        spawn_mod._git_result(["rev-parse", "--show-toplevel"], path)[0]
        for path in child_cwds
    ]
    assert roots == [str(path) for path in child_cwds]
    branches = [
        spawn_mod._git_result(["branch", "--show-current"], path)[0]
        for path in child_cwds
    ]
    assert len(set(branches)) == 2
    assert all(branch.startswith("threadkeeper/spawn-tk_") for branch in branches)
    assert spawn_mod._git_result(
        ["status", "--porcelain", "--untracked-files=no"], repo
    )[0] == ""


def test_dirty_git_checkout_is_refused_before_child_launch(
    mp_with_cid, monkeypatch,
):
    pkg = mp_with_cid(_FAKE_CID)
    repo = _init_repo(pkg["tmp"])
    spawn_mod = _configure_spawn(monkeypatch, pkg)
    (repo / "tracked.txt").write_text("uncommitted\n", encoding="utf-8")

    def _unexpected_launch(*_args, **_kwargs):
        raise AssertionError("dirty checkout must be rejected before Popen")

    monkeypatch.setattr(spawn_mod.subprocess, "Popen", _unexpected_launch)

    out = spawn_mod.spawn(
        prompt="must not launch", cwd=str(repo), visible=False, capture_output=False
    )

    assert out == "ERR spawn_dirty_worktree mode=git"
    worktrees, err = spawn_mod._git_result(
        ["worktree", "list", "--porcelain"], repo
    )
    assert not err
    assert worktrees.count("worktree ") == 1
    conn = pkg["db"].get_db()
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
