"""Per-role spawn agent + model resolution, sourced from `Settings.spawn`.

All spawn routing now lives in the single `~/.threadkeeper/.env` (read via
pydantic-settings in `config.py`) — there is no separate `spawn.toml`. Nested
keys (double-underscore), dict keys lowercased:

    THREADKEEPER_SPAWN__DEFAULT=claude
    THREADKEEPER_SPAWN__LOOP__SHADOW_OBSERVER=codex     # role -> cli
    THREADKEEPER_SPAWN__MODEL__CLAUDE=sonnet            # cli or role -> model

Agent resolution per role (highest first):
    spawn.loop[role] -> spawn.default -> active CLI -> "claude"
`"auto"` in loop/default explicitly defers to the active CLI.

Model resolution: spawn.model[role] -> spawn.model[cli] -> "".

Roles map to spawn() call-sites: shadow_observer, archivist, curator,
candidate_reviewer, extract, evolve_reviewer, evolve_applier, probe_runner,
dialectic_validator. Resolution is case-insensitive.
"""
from __future__ import annotations

from typing import Optional

from . import config

SUPPORTED_CLIS = ("claude", "codex", "gemini", "copilot")


def _spawn():
    """Live read of spawn settings (re-read each call so tests that reload
    config see fresh values)."""
    return config.settings.spawn


def _norm(v) -> str:
    return v.strip().lower() if isinstance(v, str) else ""


def resolve_agent(role: str, active_cli: Optional[str] = None) -> str:
    """Which CLI runs the spawned child for this role. Priority:
    spawn.loop[role] -> spawn.default -> active CLI -> 'claude'.
    'auto' in loop/default defers to the active CLI."""
    sp = _spawn()
    cli = _norm(sp.loop.get((role or "").lower()))
    if cli and cli != "auto" and cli in SUPPORTED_CLIS:
        return cli
    default = _norm(sp.default)
    if default and default != "auto" and default in SUPPORTED_CLIS:
        return default
    if active_cli and active_cli in SUPPORTED_CLIS:
        return active_cli
    return "claude"


def resolve_model(cli: str, role: str = "") -> str:
    """Configured model for this spawn, or "" (let the CLI use its default).
    Priority: spawn.model[role] -> spawn.model[cli] -> "". `role` is optional so
    legacy positional callers — resolve_model("claude") — keep working."""
    models = _spawn().model
    if role:
        v = models.get(role.lower())
        if isinstance(v, str) and v.strip():
            return v.strip()
    v = models.get((cli or "").lower())
    return v.strip() if isinstance(v, str) and v.strip() else ""


def summary_table(active_cli: Optional[str]) -> str:
    """Human-readable per-role assignment table for the startup validator."""
    roles = (
        "archivist",
        "shadow_observer",
        "extract",
        "candidate_reviewer",
        "curator",
        "dialectic_validator",
        "evolve_applier",
    )
    sp = _spawn()
    out = []
    for role in roles:
        chosen = resolve_agent(role, active_cli)
        if _norm(sp.loop.get(role.lower())) in SUPPORTED_CLIS:
            src = "spawn config"
        elif _norm(sp.default) in SUPPORTED_CLIS and _norm(sp.default) != "auto":
            src = "spawn default"
        elif active_cli:
            src = "active CLI"
        else:
            src = "fallback"
        model = resolve_model(chosen, role)
        model_suffix = f" model={model}" if model else ""
        out.append(f"  {role:<18} → {chosen:<8}{model_suffix} ({src})")
    return "\n".join(out)
