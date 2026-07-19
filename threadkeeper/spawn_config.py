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

Model and effort resolution: role override -> CLI default -> "".

Roles map to LLM-backed spawn() call-sites: shadow_observer, archivist,
candidate_reviewer, curator, dialectic_validator, probe_runner,
evolve_researcher, evolve_reviewer, and evolve_applier. Mechanical extract and
other deterministic jobs are intentionally excluded. Resolution is
case-insensitive.
"""
from __future__ import annotations

import os
import json
import re
from typing import Optional

from . import config
from .agent_metadata import AGENT_ROLE_NAMES

# A model name that clearly belongs to Anthropic's Claude family. Used only to
# warn when such a model is routed to a non-Claude CLI (e.g. `opus` on codex),
# which a provider silently rejects at runtime with a 400 rather than at
# config-validation time. Deliberately narrow to avoid false positives on
# provider-neutral names.
_CLAUDE_MODEL_RE = re.compile(r"^(?:opus|sonnet|haiku|claude)\b", re.IGNORECASE)


def _looks_like_claude_model(model: str) -> bool:
    return bool(isinstance(model, str) and _CLAUDE_MODEL_RE.match(model.strip()))

SUPPORTED_CLIS = ("claude", "codex", "antigravity", "copilot")
CLI_ALIASES = {
    "agy": "antigravity",
}
SUMMARY_ROLES = AGENT_ROLE_NAMES
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
KNOWN_EFFORT_KEYS = KNOWN_MODEL_KEYS

EFFORT_OPTIONS = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "codex": ("low", "medium", "high", "xhigh"),
    "copilot": ("low", "medium", "high", "xhigh"),
    "antigravity": (),
}


def _spawn():
    """Live read of spawn settings (re-read each call so tests that reload
    config see fresh values)."""
    return config.settings.spawn


def _norm(v) -> str:
    return v.strip().lower() if isinstance(v, str) else ""


def _norm_cli(v) -> str:
    cli = _norm(v)
    return CLI_ALIASES.get(cli, cli)


def _top_level_spawn_overrides(
    environ: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Expand Pydantic's supported ``THREADKEEPER_SPAWN={...}`` JSON form.

    Only the four public spawn-routing fields are surfaced. This keeps the
    settings metadata useful without reflecting arbitrary process environment
    content back to the UI.
    """
    source = os.environ if environ is None else environ
    raw = next(
        (value for key, value in source.items() if key.upper() == "THREADKEEPER_SPAWN"),
        None,
    )
    if raw is None:
        return {}
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}

    rows: dict[str, str] = {}
    default = payload.get("default")
    if default is not None:
        rows["THREADKEEPER_SPAWN__DEFAULT"] = str(default)
    for section in ("loop", "model", "effort"):
        values = payload.get(section)
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            if value is None:
                continue
            rows[_env_suffix(f"THREADKEEPER_SPAWN__{section.upper()}__", key)] = str(value)
    return rows


def runtime_spawn_overrides(environ: Optional[dict[str, str]] = None) -> list[dict[str, str]]:
    """Safe spawn-only process overrides visible to the settings client."""
    source = os.environ if environ is None else environ
    prefixes = (
        "THREADKEEPER_SPAWN__LOOP__",
        "THREADKEEPER_SPAWN__MODEL__",
        "THREADKEEPER_SPAWN__EFFORT__",
    )
    rows = {
        key: {"key": key, "value": value, "source": "process_environment_json"}
        for key, value in _top_level_spawn_overrides(source).items()
    }
    for key, value in sorted(source.items()):
        upper = key.upper()
        if not str(value).strip():
            continue
        if upper == "THREADKEEPER_SPAWN__DEFAULT" or upper.startswith(prefixes):
            rows[upper] = {
                "key": upper,
                "value": str(value),
                "source": "process_environment",
            }
    return [rows[key] for key in sorted(rows)]


def _process_override_key(*keys: str) -> str:
    environ = {
        key.upper(): key for key, value in os.environ.items()
        if str(value).strip()
    }
    for key in keys:
        if key.upper() in environ:
            return key.upper()
    expanded = _top_level_spawn_overrides()
    if any(key.upper() in expanded for key in keys):
        return "THREADKEEPER_SPAWN"
    return ""


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


def agent_cli_is_dynamic(role: str, active_cli: Optional[str] = None) -> bool:
    """Whether routing still depends on the host CLI selected at spawn time."""
    if _norm_cli(active_cli) in SUPPORTED_CLIS:
        return False
    sp = _spawn()
    role_cli = _norm_cli(sp.loop.get(_norm(role)))
    if role_cli in SUPPORTED_CLIS:
        return False
    default = _norm_cli(sp.default)
    return default not in SUPPORTED_CLIS


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


def resolve_effort(cli: str, role: str = "") -> str:
    """Configured effort for a spawn, or empty for the CLI-native default.

    Priority mirrors model resolution: role override, then CLI default.  Agy's
    ``AGY`` alias is accepted, but Antigravity does not support an independent
    effort flag; validation warns and adapters intentionally ignore it.
    """
    efforts = _spawn().effort
    if role:
        value = efforts.get(role.lower())
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    keys = [_norm_cli(cli), _norm(cli)]
    for key in dict.fromkeys(k for k in keys if k):
        value = efforts.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    if _norm_cli(cli) == "antigravity":
        value = efforts.get("agy")
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def resolution_details(role: str, active_cli: Optional[str] = None) -> dict[str, str]:
    """Effective role settings plus the inheritance source for each value."""
    sp = _spawn()
    role_key = _norm(role)
    cli = resolve_agent(role_key, active_cli)
    role_cli = _norm_cli(sp.loop.get(role_key))
    role_cli_key = _env_suffix("THREADKEEPER_SPAWN__LOOP__", role_key)
    default_key = "THREADKEEPER_SPAWN__DEFAULT"
    role_cli_process_key = _process_override_key(role_cli_key)
    default_process_key = _process_override_key(default_key)
    if role_cli_process_key:
        cli_source_key = role_cli_process_key
        cli_source = "process environment"
    elif role_cli in SUPPORTED_CLIS:
        cli_source_key = ""
        cli_source = "role override"
    elif default_process_key:
        cli_source_key = default_process_key
        cli_source = "process environment"
    elif _norm_cli(sp.default) in SUPPORTED_CLIS and _norm(sp.default) != "auto":
        cli_source_key = ""
        cli_source = "CLI default"
    elif _norm_cli(active_cli) in SUPPORTED_CLIS:
        cli_source_key = ""
        cli_source = "active CLI"
    else:
        cli_source_key = ""
        cli_source = "fallback"

    model = resolve_model(cli, role_key)
    role_model_key = _env_suffix("THREADKEEPER_SPAWN__MODEL__", role_key)
    cli_model_keys = [
        _env_suffix("THREADKEEPER_SPAWN__MODEL__", _norm_cli(cli)),
        _env_suffix("THREADKEEPER_SPAWN__MODEL__", _norm(cli)),
    ]
    if cli == "antigravity":
        cli_model_keys.append("THREADKEEPER_SPAWN__MODEL__AGY")
    if role_key in sp.model and _norm(sp.model.get(role_key)):
        model_source_key = _process_override_key(role_model_key)
        model_source = "process environment" if model_source_key else "role override"
    elif any(_norm(sp.model.get(key)) for key in (_norm_cli(cli), _norm(cli))):
        model_source_key = _process_override_key(*cli_model_keys)
        model_source = "process environment" if model_source_key else "CLI default"
    elif cli == "antigravity" and _norm(sp.model.get("agy")):
        model_source_key = _process_override_key(*cli_model_keys)
        model_source = "process environment" if model_source_key else "CLI default"
    else:
        model_source_key = ""
        model_source = "CLI native default"

    effort = resolve_effort(cli, role_key)
    role_effort_key = _env_suffix("THREADKEEPER_SPAWN__EFFORT__", role_key)
    cli_effort_keys = [
        _env_suffix("THREADKEEPER_SPAWN__EFFORT__", _norm_cli(cli)),
        _env_suffix("THREADKEEPER_SPAWN__EFFORT__", _norm(cli)),
    ]
    if cli == "antigravity":
        cli_effort_keys.append("THREADKEEPER_SPAWN__EFFORT__AGY")
    if role_key in sp.effort and _norm(sp.effort.get(role_key)):
        effort_source_key = _process_override_key(role_effort_key)
        effort_source = "process environment" if effort_source_key else "role override"
    elif any(_norm(sp.effort.get(key)) for key in (_norm_cli(cli), _norm(cli))):
        effort_source_key = _process_override_key(*cli_effort_keys)
        effort_source = "process environment" if effort_source_key else "CLI default"
    elif cli == "antigravity" and _norm(sp.effort.get("agy")):
        effort_source_key = _process_override_key(*cli_effort_keys)
        effort_source = "process environment" if effort_source_key else "CLI default"
    elif cli == "antigravity":
        effort_source_key = ""
        effort_source = "encoded in model"
    else:
        effort_source_key = ""
        effort_source = "CLI native default"
    return {
        "role": role_key,
        "cli": cli,
        "cli_source": cli_source,
        "cli_source_key": cli_source_key,
        "model": model,
        "model_source": model_source,
        "model_source_key": model_source_key,
        "effort": effort,
        "effort_source": effort_source,
        "effort_source_key": effort_source_key,
    }


def _env_suffix(prefix: str, key: str) -> str:
    return f"{prefix}{str(key).upper()}"


def _spawn_warnings(
    active_cli: Optional[str],
    advertised_models: Optional[dict[str, list[str]]] = None,
) -> list[str]:
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
                advertised = {
                    item.casefold()
                    for item in (advertised_models or {}).get(effective_cli, [])
                }
                if str(model).strip().casefold() in advertised:
                    continue
                # Copilot and Antigravity intentionally advertise
                # cross-provider Claude models. Their adapter catalog (and
                # custom-model support) is more authoritative than the family
                # prefix heuristic; retain the mismatch guard for Codex.
                if effective_cli in {"copilot", "antigravity"}:
                    continue
                warnings.append(
                    "  warning: "
                    f"{_env_suffix('THREADKEEPER_SPAWN__MODEL__', key)}="
                    f"{model!r} is a Claude-family model but resolves to CLI "
                    f"{effective_cli!r}; that provider will reject it at "
                    "runtime — pin a model that CLI supports"
                )
    role_keys = set(SUMMARY_ROLES) | set(PREDEFINED_ROLE_PROMPTS)
    for key, raw_effort in sorted(sp.effort.items()):
        model_key = _norm_cli(key)
        role_key = _norm(key)
        if model_key not in KNOWN_EFFORT_KEYS and role_key not in KNOWN_EFFORT_KEYS:
            warnings.append(
                "  warning: "
                f"{_env_suffix('THREADKEEPER_SPAWN__EFFORT__', key)}="
                f"{raw_effort!r} is not used by a supported CLI or startup role"
            )
            continue
        effective_cli = (
            resolve_agent(role_key, active_cli)
            if role_key in role_keys
            else model_key
        )
        effort = _norm(raw_effort)
        allowed = EFFORT_OPTIONS.get(effective_cli, ())
        if effective_cli == "antigravity":
            warnings.append(
                "  warning: "
                f"{_env_suffix('THREADKEEPER_SPAWN__EFFORT__', key)}="
                f"{raw_effort!r} is ignored because Antigravity encodes "
                "reasoning effort in the selected model"
            )
        elif effort not in allowed:
            warnings.append(
                "  warning: "
                f"{_env_suffix('THREADKEEPER_SPAWN__EFFORT__', key)}="
                f"{raw_effort!r} is invalid for CLI {effective_cli!r}; "
                f"expected one of {', '.join(allowed)}"
            )
    return warnings


def summary_table(active_cli: Optional[str]) -> str:
    """Human-readable per-role assignment table for the startup validator."""
    sp = _spawn()
    out = []
    for role in SUMMARY_ROLES:
        detail = resolution_details(role, active_cli)
        chosen = detail["cli"]
        src = detail["cli_source"]
        model = detail["model"]
        model_suffix = f" model={model}" if model else ""
        effort = detail["effort"]
        effort_suffix = f" effort={effort}" if effort else ""
        out.append(
            f"  {role:<18} → {chosen:<11}{model_suffix}{effort_suffix} ({src})"
        )
    out.extend(_spawn_warnings(active_cli))
    return "\n".join(out)
