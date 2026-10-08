"""Background loop children run from a neutral workspace, not the host's cwd."""
from __future__ import annotations

import stat
import subprocess
from pathlib import Path


_FAKE_CID = "bbbb0000-cccc-1111-dddd-222233334444"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout.strip()


def _dirty_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "base")
    (root / "tracked.txt").write_text("uncommitted\n", encoding="utf-8")
    return root


def _capture_launch_cwds(monkeypatch):
    import threadkeeper.identity as identity
    import threadkeeper.spawn_config as spawn_config
    import threadkeeper.tools.spawn as spawn_mod

    monkeypatch.setattr(spawn_mod, "_claude_bin", lambda: "/bin/true")
    monkeypatch.setattr(identity, "_active_cli", "claude")
    monkeypatch.setattr(
        spawn_config, "resolve_agent", lambda role, active_cli=None: "claude"
    )
    monkeypatch.setattr(spawn_config, "resolve_model", lambda cli, role="": "")
    cwds: list[Path] = []

    class _FakePopen:
        def __init__(self, _args, **kwargs):
            cwds.append(Path(kwargs["cwd"]))
            self.pid = 4300 + len(cwds)

    monkeypatch.setattr(spawn_mod.subprocess, "Popen", _FakePopen)
    return spawn_mod, cwds


def test_loop_spawn_does_not_inherit_a_dirty_git_checkout_cwd(
    mp_with_cid, monkeypatch,
):
    # The daemon host inherits the cwd of whichever session started it. A loop
    # child spawned without a cwd ran there: inside a user project, or — from a
    # git checkout with uncommitted changes — every loop spawn was refused
    # with spawn_dirty_worktree.
    pkg = mp_with_cid(_FAKE_CID)
    project = _dirty_repo(pkg["tmp"] / "project")
    monkeypatch.chdir(project)
    spawn_mod, cwds = _capture_launch_cwds(monkeypatch)

    out = spawn_mod.spawn(
        prompt="audit the library",
        visible=False,
        capture_output=False,
        role="curator",
        write_origin="curator",
    )

    assert out.startswith("ok task="), out
    workspace = pkg["config"].BACKGROUND_WORKSPACE_DIR
    assert cwds == [workspace]
    assert workspace.is_dir()
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
    assert project not in workspace.parents
    worktrees, err = spawn_mod._git_result(
        ["worktree", "list", "--porcelain"], project
    )
    assert not err
    assert worktrees.count("worktree ") == 1


def test_only_background_spawns_move_to_the_workspace(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    caller = pkg["tmp"] / "caller"
    caller.mkdir()
    explicit = pkg["tmp"] / "explicit"
    explicit.mkdir()
    monkeypatch.chdir(caller)
    spawn_mod, cwds = _capture_launch_cwds(monkeypatch)

    # A foreground agent's helper keeps working in the caller's project.
    assert spawn_mod.spawn(
        prompt="help with this repo", visible=False, capture_output=False,
    ).startswith("ok task=")
    # Callers that name a cwd (the Evolve reviewer/applier) keep it.
    assert spawn_mod.spawn(
        prompt="work in the managed checkout", cwd=str(explicit),
        visible=False, capture_output=False, write_origin="evolve",
    ).startswith("ok task=")
    # Every spawn made by the daemon host is background work.
    monkeypatch.setattr(pkg["config"], "PROCESS_ROLE", "host")
    assert spawn_mod.spawn(
        prompt="loop work", visible=False, capture_output=False,
    ).startswith("ok task=")

    assert cwds == [caller, explicit, pkg["config"].BACKGROUND_WORKSPACE_DIR]


def test_workspace_inside_a_dirty_repository_still_launches(
    mp_with_cid, monkeypatch,
):
    # A repository around the state dir (a dotfiles repo in $HOME) must not turn
    # the workspace into a project checkout — also for timeout retries, which
    # pass the recorded workspace cwd back explicitly.
    pkg = mp_with_cid(_FAKE_CID)
    _dirty_repo(pkg["tmp"])
    spawn_mod, cwds = _capture_launch_cwds(monkeypatch)
    workspace = pkg["config"].BACKGROUND_WORKSPACE_DIR

    first = spawn_mod.spawn(
        prompt="audit", visible=False, capture_output=False,
        write_origin="curator",
    )
    retry = spawn_mod.spawn(
        prompt="continue the audit", cwd=str(workspace), visible=False,
        capture_output=False, write_origin="curator",
    )

    assert first.startswith("ok task="), first
    assert retry.startswith("ok task="), retry
    assert cwds == [workspace, workspace]
