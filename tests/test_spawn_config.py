"""Per-role spawn agent resolution + active-CLI detection.

Covers the priority chain:
  env-role override → file-role override → env-default override →
  file-default override → active-CLI → 'claude' fallback.

Plus model resolution and the adapter-level spawn_argv shape per CLI.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


def _reset(monkeypatch, tmp_path):
    """Wipe env + module caches so each test starts clean."""
    for var in (
        "THREADKEEPER_SPAWN_LOOP_SHADOW_OBSERVER",
        "THREADKEEPER_SPAWN_LOOP_CURATOR",
        "THREADKEEPER_SPAWN_LOOP_ARCHIVIST",
        "THREADKEEPER_SPAWN_LOOP_CANDIDATE_REVIEWER",
        "THREADKEEPER_SPAWN_LOOP_EXTRACT",
        "THREADKEEPER_SPAWN_DEFAULT",
        "THREADKEEPER_SPAWN_MODEL_CLAUDE",
        "THREADKEEPER_SPAWN_MODEL_CODEX",
        "THREADKEEPER_SPAWN_MODEL_GEMINI",
        "THREADKEEPER_SPAWN_MODEL_COPILOT",
        "THREADKEEPER_SPAWN_CONFIG",
        "THREADKEEPER_ACTIVE_CLI",
    ):
        monkeypatch.delenv(var, raising=False)
    # Point spawn.toml at a non-existent path so file lookups return {}
    monkeypatch.setenv("THREADKEEPER_SPAWN_CONFIG", str(tmp_path / "no.toml"))
    # Reset import cache so spawn_config picks up env changes fresh
    for name in [m for m in list(sys.modules)
                 if m.startswith("threadkeeper.spawn_config")
                    or m == "threadkeeper.identity"]:
        del sys.modules[name]


# ──────────────────────────────────────────────────────────────────────
# resolve_agent
# ──────────────────────────────────────────────────────────────────────

def test_resolve_falls_back_to_claude_without_overrides(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    from threadkeeper.spawn_config import resolve_agent
    assert resolve_agent("shadow_observer", None) == "claude"
    assert resolve_agent("curator", None) == "claude"


def test_resolve_uses_active_cli_when_no_override(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    from threadkeeper.spawn_config import resolve_agent
    assert resolve_agent("shadow_observer", "codex") == "codex"
    assert resolve_agent("shadow_observer", "gemini") == "gemini"


def test_resolve_unknown_active_cli_falls_back_to_claude(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    from threadkeeper.spawn_config import resolve_agent
    assert resolve_agent("shadow_observer", "weirdly") == "claude"


def test_resolve_env_default_override(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    monkeypatch.setenv("THREADKEEPER_SPAWN_DEFAULT", "gemini")
    from threadkeeper.spawn_config import resolve_agent
    # No per-role override, no file → env default wins over active CLI
    assert resolve_agent("shadow_observer", "codex") == "gemini"


def test_resolve_per_role_env_beats_default(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    monkeypatch.setenv("THREADKEEPER_SPAWN_DEFAULT", "copilot")
    monkeypatch.setenv("THREADKEEPER_SPAWN_LOOP_CURATOR", "codex")
    from threadkeeper.spawn_config import resolve_agent
    assert resolve_agent("curator", "claude") == "codex"
    # Other roles still use default
    assert resolve_agent("shadow_observer", "claude") == "copilot"


def test_resolve_auto_passes_through_to_active_cli(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    monkeypatch.setenv("THREADKEEPER_SPAWN_LOOP_CURATOR", "auto")
    from threadkeeper.spawn_config import resolve_agent
    # 'auto' explicitly defers to active CLI
    assert resolve_agent("curator", "codex") == "codex"


def test_resolve_invalid_override_ignored(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    monkeypatch.setenv("THREADKEEPER_SPAWN_LOOP_CURATOR", "notacli")
    from threadkeeper.spawn_config import resolve_agent
    # Invalid value falls through to active CLI / fallback
    assert resolve_agent("curator", "codex") == "codex"


def test_resolve_file_loops_override(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    cfg = tmp_path / "spawn.toml"
    cfg.write_text(
        '[default]\nagent = "auto"\n'
        '[loops]\nshadow_observer = "codex"\ncurator = "gemini"\n'
    )
    monkeypatch.setenv("THREADKEEPER_SPAWN_CONFIG", str(cfg))
    from threadkeeper.spawn_config import resolve_agent
    assert resolve_agent("shadow_observer", "claude") == "codex"
    assert resolve_agent("curator", "claude") == "gemini"
    # Not configured → active CLI
    assert resolve_agent("archivist", "claude") == "claude"


def test_resolve_env_beats_file(tmp_path, monkeypatch):
    """Per-role env override has highest priority — wins over the file."""
    _reset(monkeypatch, tmp_path)
    cfg = tmp_path / "spawn.toml"
    cfg.write_text('[loops]\ncurator = "gemini"\n')
    monkeypatch.setenv("THREADKEEPER_SPAWN_CONFIG", str(cfg))
    monkeypatch.setenv("THREADKEEPER_SPAWN_LOOP_CURATOR", "codex")
    from threadkeeper.spawn_config import resolve_agent
    assert resolve_agent("curator", "claude") == "codex"


def test_resolve_malformed_toml_ignored(tmp_path, monkeypatch):
    """Broken TOML doesn't crash the daemon — falls through to env / fallback."""
    _reset(monkeypatch, tmp_path)
    cfg = tmp_path / "spawn.toml"
    cfg.write_text("garbage\nwithout proper [structure")
    monkeypatch.setenv("THREADKEEPER_SPAWN_CONFIG", str(cfg))
    from threadkeeper.spawn_config import resolve_agent
    assert resolve_agent("curator", "gemini") == "gemini"


# ──────────────────────────────────────────────────────────────────────
# resolve_model
# ──────────────────────────────────────────────────────────────────────

def test_resolve_model_env_beats_file(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    cfg = tmp_path / "spawn.toml"
    cfg.write_text('[models]\nclaude = "sonnet"\n')
    monkeypatch.setenv("THREADKEEPER_SPAWN_CONFIG", str(cfg))
    monkeypatch.setenv("THREADKEEPER_SPAWN_MODEL_CLAUDE", "opus")
    from threadkeeper.spawn_config import resolve_model
    assert resolve_model("claude") == "opus"


def test_resolve_model_file_only(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    cfg = tmp_path / "spawn.toml"
    cfg.write_text('[models]\ncodex = "gpt-5.4"\ngemini = "gemini-2.5-pro"\n')
    monkeypatch.setenv("THREADKEEPER_SPAWN_CONFIG", str(cfg))
    from threadkeeper.spawn_config import resolve_model
    assert resolve_model("codex") == "gpt-5.4"
    assert resolve_model("gemini") == "gemini-2.5-pro"
    assert resolve_model("claude") == ""  # no entry


def test_resolve_model_empty_when_unconfigured(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    from threadkeeper.spawn_config import resolve_model
    assert resolve_model("claude") == ""


# ──────────────────────────────────────────────────────────────────────
# Active-CLI detection (env override path; live process-tree walk
# isn't deterministic enough for unit tests)
# ──────────────────────────────────────────────────────────────────────

def test_active_cli_env_override(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    monkeypatch.setenv("THREADKEEPER_ACTIVE_CLI", "codex")
    # Force fresh import + cache reset
    for name in [m for m in list(sys.modules)
                 if m.startswith("threadkeeper.identity")]:
        del sys.modules[name]
    from threadkeeper import identity
    identity._active_cli = None
    assert identity.active_cli() == "codex"


def test_active_cli_invalid_env_ignored(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    monkeypatch.setenv("THREADKEEPER_ACTIVE_CLI", "nonesuch")
    for name in [m for m in list(sys.modules)
                 if m.startswith("threadkeeper.identity")]:
        del sys.modules[name]
    from threadkeeper import identity
    identity._active_cli = None
    # Invalid env → falls through to ppid walk, which from pytest
    # context most likely returns None
    detected = identity.active_cli()
    assert detected in (None, "claude", "codex", "gemini", "copilot")


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
    # Always has --output-format and --permission-mode
    joined = " ".join(argv)
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
    assert "hello" in argv


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
    # 'claude' (short) resolves to claude-code adapter
    assert get_adapter("claude").name == "claude-code"
    assert get_adapter("claude-code").name == "claude-code"
    assert get_adapter("codex").name == "codex"
    assert get_adapter("gemini").name == "gemini"
    assert get_adapter("copilot").name == "copilot"
    assert get_adapter("claude-desktop").name == "claude-desktop"
    assert get_adapter("vscode").name == "vscode"
    assert get_adapter("nonsense") is None


# ── per-role agent assignments ([agents.<role>]) ──────────────────────

def test_agents_section_sets_cli_and_model(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    cfg = tmp_path / "spawn.toml"
    cfg.write_text(
        '[agents.dialectic_validator]\ncli = "claude"\nmodel = "opus"\n'
        '[models]\nclaude = "sonnet"\n'
    )
    monkeypatch.setenv("THREADKEEPER_SPAWN_CONFIG", str(cfg))
    from threadkeeper.spawn_config import resolve_agent, resolve_model
    assert resolve_agent("dialectic_validator", "codex") == "claude"
    assert resolve_model("claude", "dialectic_validator") == "opus"
    assert resolve_model("claude", "curator") == "sonnet"


def test_resolve_model_back_compat_cli_only(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    cfg = tmp_path / "spawn.toml"
    cfg.write_text('[models]\nclaude = "sonnet"\n')
    monkeypatch.setenv("THREADKEEPER_SPAWN_CONFIG", str(cfg))
    from threadkeeper.spawn_config import resolve_model
    assert resolve_model("claude") == "sonnet"


def test_per_role_model_env_beats_file(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    cfg = tmp_path / "spawn.toml"
    cfg.write_text('[agents.dialectic_validator]\nmodel = "opus"\n')
    monkeypatch.setenv("THREADKEEPER_SPAWN_CONFIG", str(cfg))
    monkeypatch.setenv("THREADKEEPER_SPAWN_MODEL_DIALECTIC_VALIDATOR", "haiku")
    from threadkeeper.spawn_config import resolve_model
    assert resolve_model("claude", "dialectic_validator") == "haiku"


# ──────────────────────────────────────────────────────────────────────
# summary_table
# ──────────────────────────────────────────────────────────────────────

def test_summary_table_shows_active_cli(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    from threadkeeper.spawn_config import summary_table
    out = summary_table("codex")
    assert "shadow_observer" in out
    assert "codex" in out
    assert "active CLI" in out


def test_summary_table_shows_overrides(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    monkeypatch.setenv("THREADKEEPER_SPAWN_LOOP_CURATOR", "gemini")
    from threadkeeper.spawn_config import summary_table
    out = summary_table("claude")
    assert "curator" in out
    assert "gemini" in out
    assert "env override" in out
