"""Per-role spawn agent + model resolution (from Settings.spawn) + active-CLI
detection + per-adapter spawn_argv shape.

Spawn routing lives in ~/.threadkeeper/.env via pydantic-settings; nested keys
THREADKEEPER_SPAWN__LOOP__<ROLE> / __MODEL__<KEY> / __DEFAULT (keys lowercased).
"""
from __future__ import annotations

import importlib
import os
import sys

import pytest


def _reset(monkeypatch, tmp_path, env=None, env_file=None):
    """Clean env slate + reload config (so Settings.spawn is fresh) + reload
    spawn_config. Points THREADKEEPER_ENV_FILE at a nonexistent file by default
    so the machine's real ~/.threadkeeper/.env can't leak in."""
    for k in list(os.environ):
        if k.startswith("THREADKEEPER_") or k in ("CLAUDE_SKILLS_DIR", "CLAUDE_PROJECTS_DIR"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("THREADKEEPER_ENV_FILE", env_file or str(tmp_path / "none.env"))
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    import threadkeeper.config as c
    importlib.reload(c)
    import threadkeeper.spawn_config as sc
    importlib.reload(sc)
    return sc


# ──────────────────────────────────────────────────────────────────────
# resolve_agent
# ──────────────────────────────────────────────────────────────────────

def test_resolve_falls_back_to_claude_without_overrides(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path)
    assert sc.resolve_agent("shadow_observer", None) == "claude"
    assert sc.resolve_agent("curator", None) == "claude"


def test_resolve_uses_active_cli_when_no_override(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path)
    assert sc.resolve_agent("shadow_observer", "codex") == "codex"
    assert sc.resolve_agent("shadow_observer", "agy") == "antigravity"
    assert sc.resolve_agent("shadow_observer", "gemini") == "gemini"


def test_resolve_unknown_active_cli_falls_back_to_claude(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path)
    assert sc.resolve_agent("shadow_observer", "weirdly") == "claude"


def test_resolve_default_override(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={"THREADKEEPER_SPAWN__DEFAULT": "agy"})
    # No per-role override → spawn.default wins over active CLI
    assert sc.resolve_agent("shadow_observer", "codex") == "antigravity"


def test_resolve_per_role_beats_default(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__DEFAULT": "copilot",
        "THREADKEEPER_SPAWN__LOOP__CURATOR": "codex",
    })
    assert sc.resolve_agent("curator", "claude") == "codex"
    assert sc.resolve_agent("shadow_observer", "claude") == "copilot"


def test_resolve_auto_passes_through_to_active_cli(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={"THREADKEEPER_SPAWN__LOOP__CURATOR": "auto"})
    assert sc.resolve_agent("curator", "codex") == "codex"


def test_resolve_invalid_override_ignored(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={"THREADKEEPER_SPAWN__LOOP__CURATOR": "notacli"})
    assert sc.resolve_agent("curator", "codex") == "codex"


def test_resolve_from_dotenv_file(tmp_path, monkeypatch):
    envf = tmp_path / "tk.env"
    envf.write_text(
        "THREADKEEPER_SPAWN__DEFAULT=claude\n"
        "THREADKEEPER_SPAWN__LOOP__SHADOW_OBSERVER=codex\n"
        "THREADKEEPER_SPAWN__LOOP__CURATOR=antigravity\n"
    )
    sc = _reset(monkeypatch, tmp_path, env_file=str(envf))
    assert sc.resolve_agent("shadow_observer", "claude") == "codex"
    assert sc.resolve_agent("curator", "claude") == "antigravity"
    assert sc.resolve_agent("archivist", "claude") == "claude"  # active CLI


def test_resolve_env_beats_dotenv(tmp_path, monkeypatch):
    envf = tmp_path / "tk.env"
    envf.write_text("THREADKEEPER_SPAWN__LOOP__CURATOR=gemini\n")
    sc = _reset(monkeypatch, tmp_path,
                env={"THREADKEEPER_SPAWN__LOOP__CURATOR": "codex"}, env_file=str(envf))
    assert sc.resolve_agent("curator", "claude") == "codex"


# ──────────────────────────────────────────────────────────────────────
# resolve_model
# ──────────────────────────────────────────────────────────────────────

def test_resolve_model_env_beats_dotenv(tmp_path, monkeypatch):
    envf = tmp_path / "tk.env"
    envf.write_text("THREADKEEPER_SPAWN__MODEL__CLAUDE=sonnet\n")
    sc = _reset(monkeypatch, tmp_path,
                env={"THREADKEEPER_SPAWN__MODEL__CLAUDE": "opus"}, env_file=str(envf))
    assert sc.resolve_model("claude") == "opus"


def test_resolve_model_from_dotenv(tmp_path, monkeypatch):
    envf = tmp_path / "tk.env"
    envf.write_text(
        "THREADKEEPER_SPAWN__MODEL__CODEX=gpt-5.4\n"
        "THREADKEEPER_SPAWN__MODEL__AGY=gemini-3.1-pro\n"
        "THREADKEEPER_SPAWN__MODEL__GEMINI=gemini-2.5-pro\n"
    )
    sc = _reset(monkeypatch, tmp_path, env_file=str(envf))
    assert sc.resolve_model("codex") == "gpt-5.4"
    assert sc.resolve_model("antigravity") == "gemini-3.1-pro"
    assert sc.resolve_model("gemini") == "gemini-2.5-pro"
    assert sc.resolve_model("claude") == ""  # no entry


def test_resolve_model_empty_when_unconfigured(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path)
    assert sc.resolve_model("claude") == ""


def test_per_role_agent_and_model(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__LOOP__DIALECTIC_VALIDATOR": "claude",
        "THREADKEEPER_SPAWN__MODEL__DIALECTIC_VALIDATOR": "opus",
        "THREADKEEPER_SPAWN__MODEL__CLAUDE": "sonnet",
    })
    assert sc.resolve_agent("dialectic_validator", "codex") == "claude"
    assert sc.resolve_model("claude", "dialectic_validator") == "opus"
    assert sc.resolve_model("claude", "curator") == "sonnet"


def test_per_role_model_beats_cli_model(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__MODEL__DIALECTIC_VALIDATOR": "haiku",
        "THREADKEEPER_SPAWN__MODEL__CLAUDE": "sonnet",
    })
    assert sc.resolve_model("claude", "dialectic_validator") == "haiku"


# ──────────────────────────────────────────────────────────────────────
# Active-CLI detection (env override path)
# ──────────────────────────────────────────────────────────────────────

def test_active_cli_env_override(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    monkeypatch.setenv("THREADKEEPER_ACTIVE_CLI", "codex")
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper.identity")]:
        del sys.modules[name]
    from threadkeeper import identity
    identity._active_cli = None
    assert identity.active_cli() == "codex"


def test_active_cli_invalid_env_ignored(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    monkeypatch.setenv("THREADKEEPER_ACTIVE_CLI", "nonesuch")
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper.identity")]:
        del sys.modules[name]
    from threadkeeper import identity
    identity._active_cli = None
    detected = identity.active_cli()
    assert detected in (None, "claude", "codex", "antigravity", "gemini", "copilot")


# ──────────────────────────────────────────────────────────────────────
# Per-adapter spawn_argv shape
# ──────────────────────────────────────────────────────────────────────

def test_claude_spawn_argv_includes_model_and_tools(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    from threadkeeper.adapters.claude_code import ADAPTER
    argv = ADAPTER.spawn_argv(
        "hello", model="opus",
        extra_allowed_tools="Bash,mcp__thread-keeper__broadcast",
    )
    if argv is None:
        pytest.skip("claude binary not installed in test env")
    assert "-p" in argv
    assert "--output-format" in argv
    assert "--permission-mode" in argv
    assert "--model" in argv
    assert "opus" in argv


def test_codex_spawn_argv_uses_exec_subcommand(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    from threadkeeper.adapters.codex import ADAPTER
    argv = ADAPTER.spawn_argv("hello", model="gpt-5.4")
    if argv is None:
        pytest.skip("codex binary not installed in test env")
    assert "exec" in argv
    assert "-m" in argv
    assert "gpt-5.4" in argv
    assert argv[-1] == "-"
    assert "hello" not in argv
    assert ADAPTER.uses_stdin_prompt is True


def test_antigravity_spawn_argv_uses_p_flag(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    from threadkeeper.adapters.antigravity import ADAPTER
    argv = ADAPTER.spawn_argv("hello", model="gemini-3.1-pro")
    if argv is None:
        pytest.skip("agy binary not installed in test env")
    assert "-p" in argv
    assert "--model" in argv
    assert "gemini-3.1-pro" in argv


def test_gemini_spawn_argv_uses_p_flag(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    from threadkeeper.adapters.gemini import ADAPTER
    argv = ADAPTER.spawn_argv("hello", model="gemini-2.5-pro")
    if argv is None:
        pytest.skip("gemini binary not installed in test env")
    assert "-p" in argv
    assert "--model" in argv
    assert "gemini-2.5-pro" in argv


def test_copilot_spawn_argv_uses_p_flag(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    from threadkeeper.adapters.copilot import ADAPTER
    argv = ADAPTER.spawn_argv("hello")
    if argv is None:
        pytest.skip("copilot binary not installed in test env")
    assert "-p" in argv
    assert "hello" in argv


def test_vscode_does_not_support_spawn(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    from threadkeeper.adapters.vscode import ADAPTER
    assert ADAPTER.supports_spawn() is False
    assert ADAPTER.spawn_argv("x") is None


def test_claude_desktop_does_not_support_spawn(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    from threadkeeper.adapters.claude_desktop import ADAPTER
    assert ADAPTER.supports_spawn() is False
    assert ADAPTER.spawn_argv("x") is None


# ──────────────────────────────────────────────────────────────────────
# get_adapter
# ──────────────────────────────────────────────────────────────────────

def test_get_adapter_recognises_short_names(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    from threadkeeper.adapters import get_adapter
    assert get_adapter("claude").name == "claude-code"
    assert get_adapter("claude-code").name == "claude-code"
    assert get_adapter("codex").name == "codex"
    assert get_adapter("antigravity").name == "antigravity"
    assert get_adapter("agy").name == "antigravity"
    assert get_adapter("gemini").name == "gemini"
    assert get_adapter("copilot").name == "copilot"
    assert get_adapter("claude-desktop").name == "claude-desktop"
    assert get_adapter("vscode").name == "vscode"
    assert get_adapter("nonsense") is None


# ──────────────────────────────────────────────────────────────────────
# summary_table
# ──────────────────────────────────────────────────────────────────────

def test_summary_table_shows_active_cli(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path)
    out = sc.summary_table("codex")
    assert "shadow_observer" in out
    assert "codex" in out
    assert "active CLI" in out


def test_summary_table_shows_overrides(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={"THREADKEEPER_SPAWN__LOOP__CURATOR": "agy"})
    out = sc.summary_table("claude")
    assert "curator" in out
    assert "antigravity" in out
    assert "spawn config" in out


def test_summary_table_includes_dialectic_validator_model(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__LOOP__DIALECTIC_VALIDATOR": "claude",
        "THREADKEEPER_SPAWN__MODEL__DIALECTIC_VALIDATOR": "opus",
    })
    out = sc.summary_table("claude")
    assert "dialectic_validator" in out
    assert "model=opus" in out
    assert "spawn config" in out
