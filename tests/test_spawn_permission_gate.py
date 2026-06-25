"""Permission-mode admission for exposed spawn() MCP tool."""
from __future__ import annotations


_FAKE_CID = "99990000-1111-2222-3333-444455556666"


def test_spawn_refuses_bypass_permissions_for_ordinary_role(
    mp_with_cid, monkeypatch,
):
    pkg = mp_with_cid(_FAKE_CID)
    import threadkeeper.tools.spawn as spawn_mod

    monkeypatch.setattr(spawn_mod, "_claude_bin", lambda: "/bin/true")

    def _boom(*_args, **_kwargs):
        raise AssertionError("spawn must be refused before subprocess launch")

    monkeypatch.setattr(spawn_mod.subprocess, "Popen", _boom)

    out = spawn_mod.spawn(
        prompt="run arbitrary code",
        cwd=str(pkg["tmp"]),
        visible=False,
        permission_mode="bypassPermissions",
        role="executor",
    )

    assert out.startswith("ERR bypassPermissions_refused"), out


def test_spawn_auto_mode_still_launches_for_foreground_path(
    mp_with_cid, monkeypatch,
):
    pkg = mp_with_cid(_FAKE_CID)
    import threadkeeper.identity as identity
    import threadkeeper.spawn_config as spawn_config
    import threadkeeper.tools.spawn as spawn_mod

    monkeypatch.setattr(spawn_mod, "_claude_bin", lambda: "/bin/true")
    monkeypatch.setattr(identity, "_active_cli", "claude")
    monkeypatch.setattr(
        spawn_config, "resolve_agent", lambda role, active_cli=None: "claude"
    )
    monkeypatch.setattr(spawn_config, "resolve_model", lambda cli, role="": "")
    captured = {}

    class _FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = list(args)
            captured["env"] = kwargs.get("env")
            self.pid = 4242

    monkeypatch.setattr(spawn_mod.subprocess, "Popen", _FakePopen)

    out = spawn_mod.spawn(
        prompt="normal foreground child",
        cwd=str(pkg["tmp"]),
        visible=False,
        capture_output=False,
        permission_mode="auto",
        role="executor",
    )

    assert out.startswith("ok task="), out
    assert "--permission-mode" in captured["args"]
    assert "auto" in captured["args"]
    assert "THREADKEEPER_GH_WRAPPER_DIR" not in captured["env"]


def test_evolve_bypass_permissions_gets_gh_safety_wrapper(
    mp_with_cid, monkeypatch,
):
    pkg = mp_with_cid(_FAKE_CID)
    import threadkeeper.identity as identity
    import threadkeeper.spawn_config as spawn_config
    import threadkeeper.tools.spawn as spawn_mod

    monkeypatch.setattr(spawn_mod, "_claude_bin", lambda: "/bin/true")
    monkeypatch.setattr(identity, "_active_cli", "claude")
    monkeypatch.setattr(
        spawn_config, "resolve_agent", lambda role, active_cli=None: "claude"
    )
    monkeypatch.setattr(spawn_config, "resolve_model", lambda cli, role="": "")
    captured = {}

    class _FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = list(args)
            captured["env"] = kwargs.get("env")
            self.pid = 4243

    monkeypatch.setattr(spawn_mod.subprocess, "Popen", _FakePopen)

    out = spawn_mod.spawn(
        prompt="create a branch and open a PR",
        cwd=str(pkg["tmp"]),
        visible=False,
        capture_output=False,
        permission_mode="bypassPermissions",
        role="evolve_applier",
        write_origin="evolve_apply",
    )

    assert out.startswith("ok task="), out
    wrapper_dir = captured["env"]["THREADKEEPER_GH_WRAPPER_DIR"]
    assert captured["env"]["PATH"].split(":")[0] == wrapper_dir
    assert (spawn_mod.Path(wrapper_dir) / "gh").exists()
