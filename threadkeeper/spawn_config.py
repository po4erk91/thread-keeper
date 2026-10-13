"""Per-role spawn agent + model resolution, sourced from `Settings.spawn`.

All spawn routing now lives in the single `~/.threadkeeper/.env` (read via
pydantic-settings in `config.py`) — there is no separate `spawn.toml`. Nested
keys (double-underscore), dict keys lowercased:

    THREADKEEPER_SPAWN__DEFAULT=claude
    THREADKEEPER_SPAWN__LOOP__SHADOW_OBSERVER=codex     # role -> cli
    THREADKEEPER_SPAWN__LOOP__CURATOR=agy               # alias -> antigravity
    THREADKEEPER_SPAWN__MODEL__CLAUDE=sonnet            # cli or role -> model

Agent resolution per role (highest first):
    spawn.loop[role] -> ROLE_DEFAULTS[role] -> spawn.default -> active CLI -> "claude"
`"auto"` in loop/default explicitly defers to the active CLI.

Model and effort resolution: role override -> role default (same CLI only)
-> CLI default -> "".

A role whose loop value is `auto` first uses the model router's latest valid
decision (`model_routes.active_route`); all three values then come from that
decision. Without one, `auto` keeps the active-CLI meaning.

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
from .agent_metadata import AGENT_ROLE_NAMES, ROUTER_ROLE

# A model name that clearly belongs to Anthropic's Claude family. Used only to
# warn when such a model is routed to a non-Claude CLI (e.g. `opus` on codex),
# which a provider silently rejects at runtime with a 400 rather than at
# config-validation time. Deliberately narrow to avoid false positives on
# provider-neutral names.
_CLAUDE_MODEL_RE = re.compile(r"^(?:opus|sonnet|haiku|claude)\b", re.IGNORECASE)


def _looks_like_claude_model(model: str) -> bool:
    return bool(isinstance(model, str) and _CLAUDE_MODEL_RE.match(model.strip()))


# OpenAI model names Codex serves. Narrow on purpose, like the Claude pattern.
_OPENAI_MODEL_RE = re.compile(r"^(?:gpt-|o\d\b|codex-)", re.IGNORECASE)


def model_home_cli(model: str) -> str:
    """The CLI whose provider owns an explicitly requested model, or "".

    `spawn(model="opus")` routed to Codex (or a GPT model routed to Claude)
    launches a child the provider rejects at once, so an explicit model
    decides between those two CLIs when the caller did not pick one.
    """
    if _looks_like_claude_model(model):
        return "claude"
    if isinstance(model, str) and _OPENAI_MODEL_RE.match(model.strip()):
        return "codex"
    return ""

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

# Code-level defaults for roles whose right model differs from the CLI default.
# Model and effort apply only while the role still resolves to `cli`, so
# pinning another CLI never inherits a model that CLI would reject.
ROLE_DEFAULTS: dict[str, dict[str, str]] = {
    ROUTER_ROLE: {"cli": "claude", "model": "opus", "effort": "max"},
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


def _role_cli_unset(role: str) -> bool:
    return not _norm(_spawn().loop.get(_norm(role)))


def _role_default(role: str, cli: str, field: str) -> str:
    default = ROLE_DEFAULTS.get(_norm(role))
    if default is None or default["cli"] != _norm_cli(cli):
        return ""
    return default[field]


def roles_on_auto() -> list[str]:
    """Agent roles whose CLI is set to `auto`, excluding the router itself."""
    loop = _spawn().loop
    return [
        role for role in SUMMARY_ROLES
        if role != ROUTER_ROLE and _norm(loop.get(role)) == "auto"
    ]


def route_for(role: str):
    """The model router's decision for a role set to `auto`, or None."""
    role_key = _norm(role)
    if role_key == ROUTER_ROLE or _norm(_spawn().loop.get(role_key)) != "auto":
        return None
    from .model_routes import active_route

    return active_route(role_key)


def _routed_details(role_key: str, route) -> dict[str, str]:
    source = "model router"
    return {
        "role": role_key,
        "cli": route.cli, "cli_source": source, "cli_source_key": "",
        "model": route.model, "model_source": source, "model_source_key": "",
        "effort": route.effort, "effort_source": source, "effort_source_key": "",
    }


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
    model-router route (role on auto) -> spawn.loop[role] -> role default
    -> spawn.default -> active CLI -> 'claude'.
    'auto' without a route defers to the active CLI."""
    route = route_for(role)
    if route is not None:
        return route.cli
    sp = _spawn()
    role_key = _norm(role)
    cli = _norm_cli(sp.loop.get(role_key))
    if cli and cli != "auto" and cli in SUPPORTED_CLIS:
        return cli
    if _role_cli_unset(role_key) and role_key in ROLE_DEFAULTS:
        return ROLE_DEFAULTS[role_key]["cli"]
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
    if route_for(role) is not None:
        return False
    sp = _spawn()
    role_key = _norm(role)
    if _norm_cli(sp.loop.get(role_key)) in SUPPORTED_CLIS:
        return False
    if _role_cli_unset(role_key) and role_key in ROLE_DEFAULTS:
        return False
    return _norm_cli(sp.default) not in SUPPORTED_CLIS


def _cli_value_keys(cli: str) -> list[str]:
    keys = [_norm_cli(cli), _norm(cli)]
    if _norm_cli(cli) == "antigravity":
        keys.append("agy")
    return list(dict.fromkeys(k for k in keys if k))


def _role_value(values: dict, cli: str, role: str, field: str) -> str:
    """Route (role on auto, same CLI) -> role pin -> role default."""
    route = route_for(role)
    if route is not None and route.cli == _norm_cli(cli):
        return getattr(route, field)
    pinned = values.get(role.lower())
    if isinstance(pinned, str) and pinned.strip():
        return pinned.strip()
    return _role_default(role, cli, field)


def _configured_value(values: dict, cli: str, role: str, field: str) -> str:
    if role:
        role_value = _role_value(values, cli, role, field)
        if role_value:
            return role_value
    for key in _cli_value_keys(cli):
        value = values.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def resolve_model(cli: str, role: str = "") -> str:
    """Configured model for this spawn, or "" (let the CLI use its default).
    Priority: route (role on auto) -> spawn.model[role] -> role default ->
    spawn.model[cli] -> "". `role` is optional so legacy positional callers —
    resolve_model("claude") — keep working."""
    return _configured_value(_spawn().model, cli, role, "model")


def resolve_effort(cli: str, role: str = "") -> str:
    """Configured effort for a spawn, or empty for the CLI-native default.

    Priority mirrors model resolution. Agy's ``AGY`` alias is accepted, but
    Antigravity does not support an independent effort flag; validation warns
    and adapters intentionally ignore it.
    """
    return _configured_value(_spawn().effort, cli, role, "effort").lower()


def resolution_details(role: str, active_cli: Optional[str] = None) -> dict[str, str]:
    """Effective role settings plus the inheritance source for each value."""
    role_key = _norm(role)
    route = route_for(role_key)
    if route is not None:
        return _routed_details(role_key, route)
    sp = _spawn()
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
    elif _role_cli_unset(role_key) and role_key in ROLE_DEFAULTS:
        cli_source_key = ""
        cli_source = "role default"
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
    elif _role_default(role_key, cli, "model"):
        model_source_key = ""
        model_source = "role default"
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
    elif _role_default(role_key, cli, "effort"):
        effort_source_key = ""
        effort_source = "role default"
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


def _pinned_sections(sp, role: str) -> list[str]:
    return [
        section for section, values in (("MODEL", sp.model), ("EFFORT", sp.effort))
        if _norm(values.get(role))
    ]


def _stale_route_warning(role: str) -> list[str]:
    from .model_routes import stale_route

    route = stale_route(role)
    if route is None:
        return []
    return [
        "  warning: model router decision for "
        f"{role} ({route.cli} {route.model} {route.effort}) is no longer in the "
        "model catalog; the role follows the active CLI until the next pass"
    ]


def _routed_role_warnings(sp) -> tuple[list[str], set[str]]:
    """Warnings for pins a route overrides, plus the set of routed roles."""
    warnings: list[str] = []
    routed: set[str] = set()
    for role in roles_on_auto():
        if route_for(role) is None:
            warnings.extend(_stale_route_warning(role))
            continue
        routed.add(role)
        warnings.extend(
            "  warning: "
            f"{_env_suffix(f'THREADKEEPER_SPAWN__{section}__', role)} is ignored "
            "while the role is on auto and routed by the model router"
            for section in _pinned_sections(sp, role)
        )
    return warnings, routed


def _spawn_warnings(
    active_cli: Optional[str],
    advertised_models: Optional[dict[str, list[str]]] = None,
) -> list[str]:
    """Warnings for configured spawn values that summary_table would ignore."""
    sp = _spawn()
    warnings = []
    routed_warnings, routed_roles = _routed_role_warnings(sp)
    warnings.extend(routed_warnings)
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
        if role_key in routed_roles:
            continue
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
        if role_key in routed_roles:
            continue
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
