"""What a spawned child inherits and which CLI runs it."""
from __future__ import annotations

import pytest

_FAKE_CID = "44445555-6666-7777-8888-999900001111"


def _setup(mp_with_cid, monkeypatch, routed_cli="codex"):
    pkg = mp_with_cid(_FAKE_CID)
    import threadkeeper.identity as identity
    import threadkeeper.spawn_config as spawn_config
    import threadkeeper.tools.spawn as spawn_mod

    monkeypatch.setattr(spawn_mod, "_claude_bin", lambda: "/bin/true")
    monkeypatch.setattr(identity, "_active_cli", "claude")
    monkeypatch.setattr(
        spawn_config, "resolve_agent", lambda role, active_cli=None: routed_cli
    )
    monkeypatch.setattr(spawn_config, "resolve_model", lambda cli, role="": "")
    launched: list[dict] = []

    class _Popen:
        def __init__(self, args, **kwargs):
            launched.append({"args": list(args), "env": dict(kwargs.get("env") or {})})
            self.pid = 4545

    monkeypatch.setattr(spawn_mod.subprocess, "Popen", _Popen)
    return pkg, spawn_mod, launched


def _chosen_cli(pkg):
    conn = pkg["db"].get_db()
    try:
        return conn.execute("SELECT chosen_cli FROM tasks").fetchone()[0]
    finally:
        conn.close()


def test_explicit_claude_model_runs_on_claude(mp_with_cid, monkeypatch):
    """`spawn(model="opus")` from a Codex-routed setup used to launch codex
    with a Claude model, which the provider rejects at once."""
    pkg, spawn_mod, launched = _setup(mp_with_cid, monkeypatch, routed_cli="codex")

    out = spawn_mod.spawn(
        prompt="implement the task", cwd=str(pkg["tmp"]), model="opus",
        visible=False, capture_output=False,
    )

    assert out.startswith("ok task="), out
    assert _chosen_cli(pkg) == "claude"
    args = launched[0]["args"]
    assert "--model" in args and args[args.index("--model") + 1] == "opus"


def test_explicit_cli_with_a_foreign_model_is_refused(mp_with_cid, monkeypatch):
    pkg, spawn_mod, launched = _setup(mp_with_cid, monkeypatch)

    out = spawn_mod._spawn_impl(
        prompt="x", cwd=str(pkg["tmp"]), cli="codex", model="sonnet",
        visible=False, capture_output=False,
    )

    assert out == "ERR model_cli_mismatch model=sonnet cli=codex"
    assert launched == []


def test_child_env_drops_the_host_role(mp_with_cid, monkeypatch):
    """The daemon host runs with THREADKEEPER_ROLE=host; a child that kept it
    would start its MCP server believing it is the host."""
    pkg, spawn_mod, launched = _setup(mp_with_cid, monkeypatch, routed_cli="claude")
    monkeypatch.setenv("THREADKEEPER_ROLE", "host")

    out = spawn_mod.spawn(
        prompt="x", cwd=str(pkg["tmp"]), visible=False, capture_output=False,
    )

    assert out.startswith("ok task="), out
    env = launched[0]["env"]
    assert "THREADKEEPER_ROLE" not in env
    assert env["THREADKEEPER_SPAWNED_CHILD"] == "1"


def test_child_preamble_skips_the_user_session_protocol(mp_with_cid, monkeypatch):
    pkg, spawn_mod, launched = _setup(mp_with_cid, monkeypatch, routed_cli="claude")

    spawn_mod.spawn(prompt="x", cwd=str(pkg["tmp"]), visible=False, capture_output=False)

    args = launched[0]["args"]
    preamble = args[args.index("--append-system-prompt") + 1]
    assert "not a user session" in preamble
    assert "session_end" in preamble


def test_model_home_cli():
    from threadkeeper.spawn_config import model_home_cli

    assert model_home_cli("opus") == "claude"
    assert model_home_cli("claude-sonnet-5") == "claude"
    assert model_home_cli("gpt-5.6-terra") == "codex"
    assert model_home_cli("o3") == "codex"
    assert model_home_cli("gemini-3-pro") == ""
    assert model_home_cli("") == ""


@pytest.mark.parametrize('origin', ['', 'foreground', 'user', 'curator', 'evolve_apply'])
def test_public_spawn_memory_is_read_only_even_with_spoofed_origin(mp_with_cid, monkeypatch, origin):
    pkg, spawn_mod, launched = _setup(mp_with_cid, monkeypatch, routed_cli='claude')
    monkeypatch.setenv('THREADKEEPER_WRITE_ORIGIN', 'foreground')
    monkeypatch.setenv('THREADKEEPER_MEMORY_ACCESS', 'write')
    out = spawn_mod.spawn(prompt='x', cwd=str(pkg['tmp']), visible=False,
                         capture_output=False, write_origin=origin)
    assert out.startswith('ok task='), out
    env = launched[0]['env']
    assert env['THREADKEEPER_MEMORY_ACCESS'] == 'read_only'
    expected_origin = origin if origin not in {'', 'foreground', 'user'} else 'spawned'
    assert env['THREADKEEPER_WRITE_ORIGIN'] == expected_origin
    assert env['THREADKEEPER_MEMORY_TOKEN']
    conn = pkg['db'].get_db()
    row = conn.execute('SELECT access,origin FROM child_memory_grants').fetchone()
    assert tuple(row) == ('read_only', 'spawned')
    conn.close()


def test_public_and_private_generic_spawn_cannot_grant_write(mp_with_cid, monkeypatch):
    pkg, spawn_mod, launched = _setup(mp_with_cid, monkeypatch, routed_cli='claude')
    for fn in [spawn_mod.spawn, spawn_mod._spawn_impl]:
        assert fn(prompt='x', cwd=str(pkg['tmp']), visible=False,
                  capture_output=False, memory_access='write',
                  write_origin='evolve_apply') == 'ERR memory_write_requires_trusted_launcher'
    assert launched == []
    for origin in ('model_router', 'evolve'):
        out = spawn_mod._spawn_impl(
            prompt='x', cwd=str(pkg['tmp']), visible=False, capture_output=False,
            write_origin=origin, memory_access='read_only',
        )
        assert out.startswith('ok task='), out
        assert launched[-1]['env']['THREADKEEPER_WRITE_ORIGIN'] == origin
    conn = pkg['db'].get_db()
    assert {r[0] for r in conn.execute('SELECT origin FROM child_memory_grants')} == {'spawned'}
    conn.close()


@pytest.mark.parametrize('launcher', ['_spawn_evolve_reviewer', '_spawn_evolve_applier', '_spawn_evolve_applier_maintenance'])
def test_trusted_evolve_launchers_have_explicit_memory_write(mp_with_cid, monkeypatch, launcher):
    pkg, spawn_mod, launched = _setup(mp_with_cid, monkeypatch, routed_cli='claude')
    monkeypatch.setattr(spawn_mod, '_install_gh_safety_wrapper', lambda _: (pkg['tmp'], 'gh'))
    out = getattr(spawn_mod, launcher)(prompt='x', cwd=str(pkg['tmp']),
                                     visible=False, capture_output=False)
    assert out.startswith('ok task='), out
    assert launched[0]['env']['THREADKEEPER_MEMORY_ACCESS'] == 'write'
    assert launched[0]['env']['THREADKEEPER_WRITE_ORIGIN'] in {'evolve', 'evolve_apply'}


def test_curator_evaluator_is_trusted_but_researcher_is_read_only(mp_with_cid, monkeypatch):
    pkg, spawn_mod, launched = _setup(mp_with_cid, monkeypatch, routed_cli='claude')
    from threadkeeper import curator
    for role, origin, access in [
        ('curator', 'curator', 'write'),
        ('curator_researcher', 'curator_research', 'read_only'),
    ]:
        out = curator._spawn_batch_child(
            'pass-test', None, 'x', '', role=role, write_origin=origin,
            phase='research' if role == 'curator_researcher' else 'evaluate',
            batch_index=1, batch_total=1,
        )
        assert out.startswith('ok task='), out
        assert launched[-1]['env']['THREADKEEPER_MEMORY_ACCESS'] == access
        assert launched[-1]['env']['THREADKEEPER_WRITE_ORIGIN'] == origin
