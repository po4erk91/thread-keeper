"""Codex spawn should pass large prompts through stdin, not argv."""
from __future__ import annotations

import subprocess


_FAKE_CID = "11112222-3333-4444-5555-666677778888"


def test_codex_spawn_uses_stdin_file(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)

    import threadkeeper.adapters.codex as codex_mod
    import threadkeeper.identity as identity
    import threadkeeper.spawn_config as spawn_config
    import threadkeeper.tools.spawn as spawn_mod

    monkeypatch.setattr(identity, "_active_cli", "codex")
    monkeypatch.setattr(spawn_config, "resolve_agent", lambda role, active_cli=None: "codex")
    monkeypatch.setattr(spawn_config, "resolve_model", lambda cli, role="": "gpt-test")
    monkeypatch.setattr(codex_mod.shutil, "which", lambda name: "/fake/bin/codex")
    monkeypatch.setattr(spawn_mod, "_claude_bin", lambda: None)

    captured: dict[str, object] = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = list(args)
            stdin = kwargs.get("stdin")
            captured["stdin"] = stdin.read().decode("utf-8")
            captured["cwd"] = kwargs.get("cwd")
            captured["env"] = kwargs.get("env")
            captured["stdout_is_pipe"] = kwargs.get("stdout") == subprocess.DEVNULL
            self.pid = 4242

    monkeypatch.setattr(spawn_mod.subprocess, "Popen", FakePopen)

    long_prompt = "important prompt " + ("x" * 200_000)
    out = spawn_mod.spawn(
        prompt=long_prompt,
        cwd=str(pkg["tmp"]),
        visible=False,
        capture_output=False,
        role="dialectic_validator",
        slim=True,
    )

    args = captured["args"]
    assert out.startswith("ok task=")
    assert "/fake/bin/codex" in args
    assert args[-1] == "-"
    assert "--sandbox" in args
    assert "workspace-write" in args
    assert long_prompt not in args
    assert "important prompt" in captured["stdin"]
    assert "ROLE: dialectic_validator" in captured["stdin"]
    assert captured["env"]["THREADKEEPER_DB"] == str(pkg["config"].DB_PATH)


def test_codex_code_evolve_spawn_can_write_git_refs(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)

    import threadkeeper.adapters.codex as codex_mod
    import threadkeeper.identity as identity
    import threadkeeper.spawn_config as spawn_config
    import threadkeeper.tools.spawn as spawn_mod

    monkeypatch.setattr(identity, "_active_cli", "codex")
    monkeypatch.setattr(spawn_config, "resolve_agent", lambda role, active_cli=None: "codex")
    monkeypatch.setattr(spawn_config, "resolve_model", lambda cli, role="": "gpt-test")
    monkeypatch.setattr(codex_mod.shutil, "which", lambda name: "/fake/bin/codex")

    captured: dict[str, object] = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = list(args)
            captured["env"] = kwargs.get("env")
            stdin = kwargs.get("stdin")
            captured["stdin"] = stdin.read().decode("utf-8")
            self.pid = 4243

    monkeypatch.setattr(spawn_mod.subprocess, "Popen", FakePopen)

    out = spawn_mod.spawn(
        prompt="create a branch and open a PR",
        cwd=str(pkg["tmp"]),
        visible=False,
        capture_output=False,
        permission_mode="bypassPermissions",
        role="evolve_applier",
        write_origin="evolve_apply",
        slim=True,
    )

    args = captured["args"]
    assert out.startswith("ok task=")
    assert "--dangerously-bypass-approvals-and-sandbox" in args
    assert "--sandbox" not in args
    assert captured["env"]["THREADKEEPER_DB"] == str(pkg["config"].DB_PATH)
    assert captured["env"]["THREADKEEPER_TASK_LOG_DIR"] == str(
        pkg["config"].TASK_LOG_DIR
    )
    assert "ROLE: evolve_applier" in captured["stdin"]
