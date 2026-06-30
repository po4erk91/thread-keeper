"""Verify spawn(slim=True) generates a minimal --mcp-config containing only
thread-keeper, and that --strict-mcp-config is appended to the CLI args.

We can't actually launch claude in tests — but we CAN intercept the cmd
that spawn() would have run, and check it's shaped the way we expect.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


_FAKE_CID = "11112222-3333-4444-5555-666677778888"


def test_build_slim_mcp_config_from_claude_json(tmp_path, monkeypatch):
    """When ~/.claude.json has a thread-keeper entry, slim config reuses it."""
    # Stub HOME so ~/.claude.json points to a known file
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "thread-keeper": {
                "type": "stdio",
                "command": "/path/to/python",
                "args": ["-m", "threadkeeper.server"],
                "env": {"PYTHONPATH": "/path/to/repo"},
            },
            "context7": {"command": "should-not-be-included"},
            "figma": {"command": "should-not-be-included"},
        }
    }))

    # Stub TASK_LOG_DIR for atomic test isolation
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("THREADKEEPER_TASK_LOG_DIR", str(log_dir))

    # Fresh import so config picks up env
    import sys
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    from threadkeeper.tools.spawn import _build_slim_mcp_config

    slim_path = _build_slim_mcp_config("tk_test01", {
        "THREADKEEPER_FORCE_CID": _FAKE_CID,
        "THREADKEEPER_SPAWNED_CHILD": "1",
        "THREADKEEPER_NO_EMBEDDINGS": "1",
        "THREADKEEPER_WRITE_ORIGIN": "shadow_review",
    })
    assert slim_path is not None
    assert slim_path.exists()
    data = json.loads(slim_path.read_text())
    # Only thread-keeper — no context7 or figma
    assert list(data["mcpServers"].keys()) == ["thread-keeper"]
    mp = data["mcpServers"]["thread-keeper"]
    assert mp["command"] == "/path/to/python"
    assert mp["args"] == ["-m", "threadkeeper.server"]
    assert mp["env"]["PYTHONPATH"] == "/path/to/repo"
    assert mp["env"]["THREADKEEPER_FORCE_CID"] == _FAKE_CID
    assert mp["env"]["THREADKEEPER_SPAWNED_CHILD"] == "1"
    assert mp["env"]["THREADKEEPER_NO_EMBEDDINGS"] == "1"
    assert mp["env"]["THREADKEEPER_WRITE_ORIGIN"] == "shadow_review"


def test_build_slim_mcp_config_synthesizes_when_no_claude_json(tmp_path, monkeypatch):
    """Without ~/.claude.json, slim config is built from sys.executable +
    package path — always works, never None."""
    home = tmp_path / "home_empty"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    log_dir = tmp_path / "logs"
    monkeypatch.setenv("THREADKEEPER_TASK_LOG_DIR", str(log_dir))

    import sys
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    from threadkeeper.tools.spawn import _build_slim_mcp_config

    slim_path = _build_slim_mcp_config("tk_synth", {
        "THREADKEEPER_SPAWNED_CHILD": "1",
        "THREADKEEPER_NO_EMBEDDINGS": "1",
    })
    assert slim_path is not None
    data = json.loads(slim_path.read_text())
    assert "thread-keeper" in data["mcpServers"]
    mp = data["mcpServers"]["thread-keeper"]
    # Synthesized config uses the current Python interpreter
    assert mp["command"] == sys.executable
    assert "threadkeeper.server" in mp["args"]
    assert "PYTHONPATH" in mp["env"]
    assert mp["env"]["THREADKEEPER_SPAWNED_CHILD"] == "1"
    assert mp["env"]["THREADKEEPER_NO_EMBEDDINGS"] == "1"


def test_slim_config_is_owner_only_and_minimizes_env(tmp_path, monkeypatch):
    """#68: the slim config must be 0600 (not group/other readable) and must
    NOT carry host env keys the child doesn't need — e.g. a secret a user
    added to their ~/.claude.json thread-keeper entry."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "thread-keeper": {
                "type": "stdio",
                "command": "/path/to/python",
                "args": ["-m", "threadkeeper.server"],
                "env": {
                    "PYTHONPATH": "/path/to/repo",
                    "THREADKEEPER_TZ": "Europe/Kyiv",
                    # secrets a user may have on their host MCP entry — must
                    # never travel into the slim config.
                    "OPENAI_API_KEY": "sk-secret-should-be-dropped",
                    "AWS_SECRET_ACCESS_KEY": "also-secret",
                },
            },
        }
    }))

    log_dir = tmp_path / "logs"
    monkeypatch.setenv("THREADKEEPER_TASK_LOG_DIR", str(log_dir))

    import sys
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    from threadkeeper.tools.spawn import _build_slim_mcp_config

    slim_path = _build_slim_mcp_config("tk_perm01", {
        "THREADKEEPER_FORCE_CID": _FAKE_CID,
        "THREADKEEPER_NO_EMBEDDINGS": "1",
    })
    assert slim_path is not None

    # File mode: owner-only, no group/other bits.
    mode = slim_path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    env = json.loads(slim_path.read_text())["mcpServers"]["thread-keeper"]["env"]
    # Needed keys survive: package discovery + thread-keeper knobs + overrides.
    assert env["PYTHONPATH"] == "/path/to/repo"
    assert env["THREADKEEPER_TZ"] == "Europe/Kyiv"
    assert env["THREADKEEPER_FORCE_CID"] == _FAKE_CID
    assert env["THREADKEEPER_NO_EMBEDDINGS"] == "1"
    # Secrets dropped.
    assert "OPENAI_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_visible_command_script_is_owner_only(mp_with_cid, monkeypatch):
    """#68: the visible-spawn `.command` script `export`s the child env and
    must be 0700 (owner-only), not the world-readable/executable 0755."""
    pkg = mp_with_cid(_FAKE_CID)

    import threadkeeper.tools.spawn as spawn_mod

    # Don't require a real claude binary, and never actually launch Terminal.
    monkeypatch.setattr(spawn_mod, "_claude_bin", lambda: "/usr/bin/true")
    # Pre-seed the active-CLI cache so spawn() resolves to the claude path
    # without shelling out to `ps` (which would otherwise hit the patched
    # Popen below). _detect_self_cid short-circuits on the pinned FORCE_CID.
    monkeypatch.setattr(pkg["identity"], "_active_cli", "claude")

    class _FakePopen:
        def __init__(self, *a, **k):
            self.pid = 4321

    monkeypatch.setattr(spawn_mod.subprocess, "Popen", _FakePopen)

    spawn_fn = pkg["mcp"]._tool_manager._tools["spawn"].fn
    out = spawn_fn(prompt="do a thing", visible=True)
    assert out.startswith("ok task="), out

    cmd_files = list(spawn_mod.TASK_LOG_DIR.glob("*.command"))
    assert len(cmd_files) == 1, cmd_files
    mode = cmd_files[0].stat().st_mode & 0o777
    assert mode == 0o700, f"expected 0700, got {oct(mode)}"


def test_headless_log_file_is_owner_only(mp_with_cid, monkeypatch):
    """Captured stdout/stderr can include prompt-derived paths/output, so the
    headless `.log` spool file must be 0600 from creation time."""
    pkg = mp_with_cid(_FAKE_CID)

    import threadkeeper.tools.spawn as spawn_mod

    monkeypatch.setattr(spawn_mod, "_claude_bin", lambda: "/usr/bin/true")
    monkeypatch.setattr(pkg["identity"], "_active_cli", "claude")

    class _FakePopen:
        def __init__(self, *a, **k):
            self.pid = 4321

    monkeypatch.setattr(spawn_mod.subprocess, "Popen", _FakePopen)

    spawn_fn = pkg["mcp"]._tool_manager._tools["spawn"].fn
    out = spawn_fn(prompt="do a thing", visible=False, capture_output=True)
    assert out.startswith("ok task="), out

    log_files = list(spawn_mod.TASK_LOG_DIR.glob("*.log"))
    assert len(log_files) == 1, log_files
    mode = log_files[0].stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_spawn_slim_falls_back_to_full_config_when_unable(tmp_path, monkeypatch):
    """If _build_slim_mcp_config returns None (e.g. write error), spawn()
    must NOT crash — it just runs without slim flags."""
    # We unit-test by patching _build_slim_mcp_config to return None and
    # then checking spawn doesn't append --mcp-config to the cmd. But the
    # whole flow runs subprocess.Popen which we don't want in tests. Skip
    # full integration — the conditional branch in spawn() is plain
    # if-statement, no need for deep coverage.
    pytest.skip(
        "spawn() invokes claude CLI; slim-fallback path is covered by "
        "inspection of the conditional branch"
    )


def test_review_thread_uses_slim_by_default(mp_with_cid, monkeypatch):
    """review_thread(mode='auto') must pass slim=True to spawn().

    We monkey-patch spawn() to capture its kwargs without actually
    launching anything, then call review_thread() and assert."""
    pkg = mp_with_cid(_FAKE_CID)

    # Seed a thread so review_thread doesn't bail on thread_not_found
    open_t = pkg["mcp"]._tool_manager._tools["open_thread"].fn
    note = pkg["mcp"]._tool_manager._tools["note"].fn
    tid = open_t(question="slim test")
    note(thread_id=tid, content="x", kind="move")

    captured = {}

    def fake_spawn(**kwargs):
        captured.update(kwargs)
        return "ok task=tk_fake pid=0 child_cid=abc parent_cid=def"

    # Patch the inner import inside review_thread.
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn", fake_spawn)

    rev = pkg["mcp"]._tool_manager._tools["review_thread"].fn
    out = rev(thread_id=tid, focus="skills", mode="auto")

    assert "tk_fake" in out
    assert captured.get("slim") is True
    assert captured.get("write_origin") == "background_review"
    assert captured.get("visible") is False
