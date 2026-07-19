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
    assert sc.resolve_agent("shadow_observer", "gemini") == "claude"


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
    envf.write_text("THREADKEEPER_SPAWN__LOOP__CURATOR=antigravity\n")
    sc = _reset(monkeypatch, tmp_path,
                env={"THREADKEEPER_SPAWN__LOOP__CURATOR": "codex"}, env_file=str(envf))
    assert sc.resolve_agent("curator", "claude") == "codex"
    detail = sc.resolution_details("curator", "claude")
    assert detail["cli"] == "codex"
    assert detail["cli_source"] == "process environment"
    assert detail["cli_source_key"] == "THREADKEEPER_SPAWN__LOOP__CURATOR"
    assert sc.runtime_spawn_overrides() == [{
        "key": "THREADKEEPER_SPAWN__LOOP__CURATOR",
        "value": "codex",
        "source": "process_environment",
    }]
    import threadkeeper.identity as identity
    import threadkeeper.model_catalog as model_catalog

    monkeypatch.setattr(identity, "active_cli", lambda: None)
    monkeypatch.setattr(model_catalog, "cli_catalog", lambda refresh=False: [])
    catalog = model_catalog.settings_catalog()
    curator = next(
        item for item in catalog["agent_roles"] if item["role"] == "curator"
    )
    assert curator["cli"] == "codex"
    assert curator["cli_source"] == "process environment"
    assert catalog["runtime_overrides"][0]["key"].endswith("__CURATOR")


def test_top_level_spawn_json_reports_expanded_process_sources(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN": (
            '{"default":"codex","model":{"codex":"gpt-json"},'
            '"effort":{"codex":"high"}}'
        ),
    })
    detail = sc.resolution_details("curator", "claude")
    assert detail["cli"] == "codex"
    assert detail["cli_source"] == "process environment"
    assert detail["cli_source_key"] == "THREADKEEPER_SPAWN"
    assert detail["model"] == "gpt-json"
    assert detail["model_source"] == "process environment"
    assert detail["model_source_key"] == "THREADKEEPER_SPAWN"
    assert detail["effort"] == "high"
    assert detail["effort_source"] == "process environment"
    rows = {row["key"]: row for row in sc.runtime_spawn_overrides()}
    assert rows["THREADKEEPER_SPAWN__DEFAULT"]["value"] == "codex"
    assert rows["THREADKEEPER_SPAWN__MODEL__CODEX"]["value"] == "gpt-json"
    assert rows["THREADKEEPER_SPAWN__EFFORT__CODEX"]["value"] == "high"
    assert {row["source"] for row in rows.values()} == {
        "process_environment_json"
    }


def test_process_auto_override_suppresses_file_pin_and_remains_visible(
    tmp_path, monkeypatch,
):
    envf = tmp_path / "tk.env"
    envf.write_text("THREADKEEPER_SPAWN__LOOP__CURATOR=codex\n")
    sc = _reset(
        monkeypatch,
        tmp_path,
        env={"THREADKEEPER_SPAWN__LOOP__CURATOR": "auto"},
        env_file=str(envf),
    )
    detail = sc.resolution_details("curator", "claude")
    assert detail["cli"] == "claude"
    assert detail["cli_source"] == "process environment"
    assert detail["cli_source_key"] == "THREADKEEPER_SPAWN__LOOP__CURATOR"
    assert sc.runtime_spawn_overrides() == [{
        "key": "THREADKEEPER_SPAWN__LOOP__CURATOR",
        "value": "auto",
        "source": "process_environment",
    }]
    assert sc.agent_cli_is_dynamic("curator", None) is True
    assert sc.agent_cli_is_dynamic("curator", "claude") is False


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
        'THREADKEEPER_SPAWN__MODEL__AGY="Gemini 3.1 Pro (High)"\n'
        "THREADKEEPER_SPAWN__MODEL__GEMINI=removed-model\n"
    )
    sc = _reset(monkeypatch, tmp_path, env_file=str(envf))
    assert sc.resolve_model("codex") == "gpt-5.4"
    assert sc.resolve_model("antigravity") == "Gemini 3.1 Pro (High)"
    assert sc.resolve_model("gemini") == "removed-model"  # raw value stays inspectable
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


def test_effort_resolution_role_then_cli_then_empty(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__EFFORT__CODEX": "high",
        "THREADKEEPER_SPAWN__EFFORT__CURATOR": "xhigh",
    })
    assert sc.resolve_effort("codex", "curator") == "xhigh"
    assert sc.resolve_effort("codex", "probe_runner") == "high"
    assert sc.resolve_effort("claude", "probe_runner") == ""


def test_effort_agy_alias(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__EFFORT__AGY": "high",
    })
    assert sc.resolve_effort("antigravity") == "high"


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
    assert detected in (None, "claude", "codex", "antigravity", "copilot")


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


def test_codex_spawn_argv_applies_native_effort_override(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    from threadkeeper.adapters.codex import ADAPTER
    argv = ADAPTER.spawn_argv("hello", model="gpt-test", effort="xhigh")
    if argv is None:
        pytest.skip("codex binary not installed in test env")
    assert "-c" in argv
    assert 'model_reasoning_effort="xhigh"' in argv


def test_antigravity_spawn_argv_uses_p_flag(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    from threadkeeper.adapters.antigravity import ADAPTER
    argv = ADAPTER.spawn_argv("hello", model="Gemini 3.1 Pro (High)")
    if argv is None:
        pytest.skip("agy binary not installed in test env")
    assert "-p" in argv
    assert "--model" in argv
    assert "Gemini 3.1 Pro (High)" in argv


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
    assert "--allow-all-tools" in argv


def test_copilot_spawn_argv_includes_effort(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    from threadkeeper.adapters.copilot import ADAPTER
    argv = ADAPTER.spawn_argv("hello", effort="high")
    if argv is None:
        pytest.skip("copilot binary not installed in test env")
    assert argv[-2:] == ["--effort", "high"]


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
    assert get_adapter("gemini") is None
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
    assert "warning:" not in out


def test_summary_table_shows_overrides(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={"THREADKEEPER_SPAWN__LOOP__CURATOR": "agy"})
    out = sc.summary_table("claude")
    assert "curator" in out
    assert "antigravity" in out
    assert "process environment" in out
    assert "warning:" not in out


def test_summary_table_includes_dialectic_validator_model(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__LOOP__DIALECTIC_VALIDATOR": "claude",
        "THREADKEEPER_SPAWN__MODEL__DIALECTIC_VALIDATOR": "opus",
    })
    out = sc.summary_table("claude")
    assert "dialectic_validator" in out
    assert "model=opus" in out
    assert "process environment" in out
    assert "warning:" not in out


def test_summary_table_includes_effective_effort(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__DEFAULT": "codex",
        "THREADKEEPER_SPAWN__EFFORT__CODEX": "high",
        "THREADKEEPER_SPAWN__EFFORT__CURATOR": "xhigh",
    })
    out = sc.summary_table("claude")
    assert "curator" in out and "effort=xhigh" in out
    assert "probe_runner" in out and "effort=high" in out


def test_summary_table_warns_removed_gemini_and_antigravity_effort(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__DEFAULT": "gemini",
        "THREADKEEPER_SPAWN__MODEL__GEMINI": "old-model",
        "THREADKEEPER_SPAWN__EFFORT__AGY": "high",
    })
    out = sc.summary_table("codex")
    assert "DEFAULT='gemini' is not a supported CLI" in out
    assert "MODEL__GEMINI='old-model' is not used" in out
    assert "Antigravity encodes reasoning effort" in out


def test_summary_table_warns_invalid_spawn_cli(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__LOOP__CURATOR": "claud",
    })
    out = sc.summary_table("codex")
    assert "curator" in out
    assert "codex" in out
    assert "THREADKEEPER_SPAWN__LOOP__CURATOR='claud'" in out
    assert "not a supported CLI" in out


def test_summary_table_warns_unused_model_key(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__MODEL__CLAUD": "opus",
        "THREADKEEPER_SPAWN__MODEL__CLAUDE": "sonnet",
    })
    out = sc.summary_table("claude")
    assert "model=sonnet" in out
    assert "THREADKEEPER_SPAWN__MODEL__CLAUD='opus'" in out
    assert "not used by a supported CLI or startup role" in out


def test_summary_table_warns_claude_model_on_non_claude_cli(tmp_path, monkeypatch):
    """The exact misconfig that broke the live curator loop: a Claude model
    pinned to a role that resolves to codex → provider 400 at runtime, which we
    now surface as a startup warning."""
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__DEFAULT": "codex",
        "THREADKEEPER_SPAWN__MODEL__CURATOR": "opus",
    })
    out = sc.summary_table("codex")
    assert "THREADKEEPER_SPAWN__MODEL__CURATOR='opus'" in out
    assert "Claude-family model" in out
    assert "codex" in out


def test_summary_table_warns_claude_model_on_cli_keyed_pin(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__MODEL__CODEX": "sonnet",
    })
    out = sc.summary_table("codex")
    assert "THREADKEEPER_SPAWN__MODEL__CODEX='sonnet'" in out
    assert "Claude-family model" in out


def test_summary_table_no_mismatch_for_codex_model_on_codex(tmp_path, monkeypatch):
    """The fixed config: a codex-valid model on codex draws no warning."""
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__DEFAULT": "codex",
        "THREADKEEPER_SPAWN__MODEL__CURATOR": "gpt-5.5",
        "THREADKEEPER_SPAWN__MODEL__PROBE_RUNNER": "gpt-5.5",
    })
    out = sc.summary_table("codex")
    assert "warning:" not in out


def test_summary_table_no_mismatch_for_claude_model_on_claude(tmp_path, monkeypatch):
    """opus on the claude CLI is correct — must NOT warn."""
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__LOOP__CURATOR": "claude",
        "THREADKEEPER_SPAWN__MODEL__CURATOR": "opus",
    })
    out = sc.summary_table("claude")
    assert "warning:" not in out


@pytest.mark.parametrize(
    ("cli", "model"),
    [
        ("copilot", "claude-sonnet-4.6"),
        ("antigravity", "Claude Opus 4.6 (Thinking)"),
    ],
)
def test_cross_provider_advertised_claude_models_are_not_rejected(
    tmp_path, monkeypatch, cli, model,
):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__DEFAULT": cli,
        f"THREADKEEPER_SPAWN__MODEL__{cli.upper()}": model,
    })
    out = sc.summary_table(None)
    assert "Claude-family model" not in out
