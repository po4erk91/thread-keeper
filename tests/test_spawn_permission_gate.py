"""Permission-mode admission for exposed spawn() MCP tool."""
from __future__ import annotations


_FAKE_CID = "99990000-1111-2222-3333-444455556666"


def test_public_spawn_refuses_forged_evolve_bypass_metadata(
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
        role="evolve_applier",
        write_origin="evolve_apply",
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


def test_internal_evolve_applier_bypass_gets_gh_safety_wrapper(
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

    out = spawn_mod._spawn_evolve_applier(
        prompt="create a branch and open a PR",
        cwd=str(pkg["tmp"]),
        visible=False,
        capture_output=False,
    )

    assert out.startswith("ok task="), out
    assert captured["env"]["THREADKEEPER_WRITE_ORIGIN"] == "evolve_apply"
    wrapper_dir = captured["env"]["THREADKEEPER_GH_WRAPPER_DIR"]
    assert captured["env"]["PATH"].split(":")[0] == wrapper_dir
    assert (spawn_mod.Path(wrapper_dir) / "gh").exists()


def test_public_spawn_allows_bypass_with_explicit_operator_override(
    mp_with_cid, monkeypatch,
):
    pkg = mp_with_cid(_FAKE_CID)
    import threadkeeper.identity as identity
    import threadkeeper.spawn_config as spawn_config
    import threadkeeper.tools.spawn as spawn_mod

    monkeypatch.setenv("THREADKEEPER_ALLOW_BYPASS_PERMISSIONS_SPAWN", "1")
    monkeypatch.setattr(spawn_mod, "_claude_bin", lambda: "/bin/true")
    monkeypatch.setattr(identity, "_active_cli", "claude")
    monkeypatch.setattr(
        spawn_config, "resolve_agent", lambda role, active_cli=None: "claude"
    )
    monkeypatch.setattr(spawn_config, "resolve_model", lambda cli, role="": "")

    class _FakePopen:
        pid = 4244

        def __init__(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(spawn_mod.subprocess, "Popen", _FakePopen)

    out = spawn_mod.spawn(
        prompt="operator-approved maintenance",
        cwd=str(pkg["tmp"]),
        visible=False,
        capture_output=False,
        permission_mode="bypassPermissions",
        role="executor",
        write_origin="untrusted",
    )

    assert out.startswith("ok task="), out
