"""Per-role spawn agent + model resolution, sourced from `Settings.spawn`.

All spawn routing now lives in the single `~/.threadkeeper/.env` (read via
pydantic-settings in `config.py`) — there is no separate `spawn.toml`. Nested
keys (double-underscore), dict keys lowercased:

    THREADKEEPER_SPAWN__DEFAULT=claude
    THREADKEEPER_SPAWN__LOOP__SHADOW_OBSERVER=codex     # role -> cli
    THREADKEEPER_SPAWN__LOOP__CURATOR=agy               # alias -> antigravity
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

import re
from typing import Optional

from . import config

# A model name that clearly belongs to Anthropic's Claude family. Used only to
# warn when such a model is routed to a non-Claude CLI (e.g. `opus` on codex),
# which a provider silently rejects at runtime with a 400 rather than at
# config-validation time. Deliberately narrow to avoid false positives on
# provider-neutral names.
_CLAUDE_MODEL_RE = re.compile(r"^(?:opus|sonnet|haiku|claude)\b", re.IGNORECASE)


def _looks_like_claude_model(model: str) -> bool:
    return bool(isinstance(model, str) and _CLAUDE_MODEL_RE.match(model.strip()))

SUPPORTED_CLIS = ("claude", "codex", "antigravity", "gemini", "copilot")
CLI_ALIASES = {
    "agy": "antigravity",
}
SUMMARY_ROLES = (
    "archivist",
    "shadow_observer",
    "extract",
    "candidate_reviewer",
    "curator",
    "dialectic_validator",
    "evolve_researcher",
    "evolve_reviewer",
    "evolve_applier",
    "probe_runner",
)
PREDEFINED_ROLE_PROMPTS = (
    "skeptic",
    "generator",
    "critic",
    "synthesizer",
    "explorer",
    "executor",
)
KNOWN_MODEL_KEYS = (
    set(SUPPORTED_CLIS)
    | set(CLI_ALIASES)
    | set(SUMMARY_ROLES)
    | set(PREDEFINED_ROLE_PROMPTS)
)


def _spawn():
    """Live read of spawn settings (re-read each call so tests that reload
    config see fresh values)."""
    return config.settings.spawn


def _norm(v) -> str:
    return v.strip().lower() if isinstance(v, str) else ""


def _norm_cli(v) -> str:
    cli = _norm(v)
    return CLI_ALIASES.get(cli, cli)


def resolve_agent(role: str, active_cli: Optional[str] = None) -> str:
    """Which CLI runs the spawned child for this role. Priority:
    spawn.loop[role] -> spawn.default -> active CLI -> 'claude'.
    'auto' in loop/default defers to the active CLI."""
    sp = _spawn()
    cli = _norm_cli(sp.loop.get((role or "").lower()))
    if cli and cli != "auto" and cli in SUPPORTED_CLIS:
        return cli
    default = _norm_cli(sp.default)
    if default and default != "auto" and default in SUPPORTED_CLIS:
        return default
    active = _norm_cli(active_cli)
    if active and active in SUPPORTED_CLIS:
        return active
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
    keys = [_norm_cli(cli), _norm(cli)]
    for key in dict.fromkeys(k for k in keys if k):
        v = models.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    if _norm_cli(cli) == "antigravity":
        v = models.get("agy")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _env_suffix(prefix: str, key: str) -> str:
    return f"{prefix}{str(key).upper()}"


def _spawn_warnings(active_cli: Optional[str]) -> list[str]:
    """Warnings for configured spawn values that summary_table would ignore."""
    sp = _spawn()
    warnings = []
    default_raw = _norm(sp.default)
    default = _norm_cli(sp.default)
    if default_raw and default_raw != "auto" and default not in SUPPORTED_CLIS:
        warnings.append(
            "  warning: THREADKEEPER_SPAWN__DEFAULT="
            f"{sp.default!r} is not a supported CLI; falling back"
        )

    for role, raw_cli in sorted(sp.loop.items()):
        cli_raw = _norm(raw_cli)
        cli = _norm_cli(raw_cli)
        if cli_raw and cli_raw != "auto" and cli not in SUPPORTED_CLIS:
            warnings.append(
                "  warning: "
                f"{_env_suffix('THREADKEEPER_SPAWN__LOOP__', role)}="
                f"{raw_cli!r} is not a supported CLI; falling back for that role"
            )

    active = _norm_cli(active_cli)
    if active_cli and active not in SUPPORTED_CLIS:
        warnings.append(
            f"  warning: active CLI {active_cli!r} is not supported; falling back"
        )

    for key, model in sorted(sp.model.items()):
        model_key = _norm_cli(key)
        role_key = _norm(key)
        if (
            role_key
            and model_key not in KNOWN_MODEL_KEYS
            and role_key not in KNOWN_MODEL_KEYS
        ):
            warnings.append(
                "  warning: "
                f"{_env_suffix('THREADKEEPER_SPAWN__MODEL__', key)}="
                f"{model!r} is not used by a supported CLI or startup role"
            )
            continue
        # Provider mismatch: a Claude-family model pinned onto a non-Claude CLI
        # is silently rejected at runtime (e.g. `opus` on codex → HTTP 400). The
        # effective CLI is the role's resolved agent for a role-keyed pin, or the
        # key itself for a CLI-keyed pin. Surface it at validation instead.
        if _looks_like_claude_model(model):
            if role_key in {r.lower() for r in SUMMARY_ROLES} | set(
                PREDEFINED_ROLE_PROMPTS
            ):
                effective_cli = resolve_agent(role_key, active_cli)
            elif model_key in SUPPORTED_CLIS:
                effective_cli = model_key
            else:
                effective_cli = ""
            if effective_cli and effective_cli != "claude":
                warnings.append(
                    "  warning: "
                    f"{_env_suffix('THREADKEEPER_SPAWN__MODEL__', key)}="
                    f"{model!r} is a Claude-family model but resolves to CLI "
                    f"{effective_cli!r}; that provider will reject it at "
                    "runtime — pin a model that CLI supports"
                )
    return warnings


def summary_table(active_cli: Optional[str]) -> str:
    """Human-readable per-role assignment table for the startup validator."""
    sp = _spawn()
    out = []
    for role in SUMMARY_ROLES:
        chosen = resolve_agent(role, active_cli)
        if _norm_cli(sp.loop.get(role.lower())) in SUPPORTED_CLIS:
            src = "spawn config"
        elif (
            _norm_cli(sp.default) in SUPPORTED_CLIS
            and _norm(sp.default) != "auto"
        ):
            src = "spawn default"
        elif active_cli:
            src = "active CLI"
        else:
            src = "fallback"
        model = resolve_model(chosen, role)
        model_suffix = f" model={model}" if model else ""
        out.append(f"  {role:<18} → {chosen:<8}{model_suffix} ({src})")
    out.extend(_spawn_warnings(active_cli))
    return "\n".join(out)
