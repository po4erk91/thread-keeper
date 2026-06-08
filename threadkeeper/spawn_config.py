"""Per-role spawn agent resolution.

Resolves the question "for this learning-loop role, which CLI should
the spawned child agent be?" by combining three signals in priority
order:

  1. Per-role env override:
       THREADKEEPER_SPAWN_LOOP_<ROLE_UPPERCASE>=<cli>
     Example: THREADKEEPER_SPAWN_LOOP_SHADOW_OBSERVER=codex
     Highest priority — wins over everything below.

  2. File overrides at ~/.threadkeeper/spawn.toml (one TOML file,
     stable structure):
         [default]
         agent = "auto"     # "auto" → use active CLI; or pin: "claude"

         [loops]
         shadow_observer    = "claude"
         curator            = "auto"
         candidate_reviewer = "codex"
         archivist          = "auto"   # close_thread auto-review
         extract            = "auto"

         [models]
         claude = "opus"        # optional per-CLI model pin
         codex  = "gpt-5.4"
         gemini = "gemini-2.5-pro"

  3. Default env override:
       THREADKEEPER_SPAWN_DEFAULT=<cli>
     Affects every loop that has no explicit override above.

  4. Auto fallback: the CLI thread-keeper detected at startup (the
     process that's running thread-keeper.server right now —
     identity._active_cli_name).

Roles map to spawn() call-sites:

  shadow_observer    → shadow_review daemon
  archivist          → review_thread (close_thread auto-review)
  curator            → curator daemon
  candidate_reviewer → candidate_reviewer daemon
  extract            → extract daemon (no spawn — local heuristic)

The role string is whatever spawn(role=...) gets passed; resolution
is case-insensitive.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    tomllib = None  # type: ignore


SUPPORTED_CLIS = ("claude", "codex", "gemini", "copilot")

_DEFAULT_CONFIG_PATH = Path("~/.threadkeeper/spawn.toml").expanduser()


def _config_path() -> Path:
    override = os.environ.get("THREADKEEPER_SPAWN_CONFIG")
    if override:
        return Path(override).expanduser()
    return _DEFAULT_CONFIG_PATH


def _load_file() -> dict:
    """Read spawn.toml. Returns {} on missing file, unreadable file,
    or malformed TOML — never raises (spawn config is opt-in)."""
    fp = _config_path()
    if not fp.exists() or tomllib is None:
        return {}
    try:
        return tomllib.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _env_role_override(role: str) -> Optional[str]:
    key = "THREADKEEPER_SPAWN_LOOP_" + role.upper().replace("-", "_")
    val = os.environ.get(key, "").strip().lower()
    return val if val in SUPPORTED_CLIS or val == "auto" else None


def _env_default_override() -> Optional[str]:
    val = os.environ.get("THREADKEEPER_SPAWN_DEFAULT", "").strip().lower()
    return val if val in SUPPORTED_CLIS or val == "auto" else None


def _file_role_override(role: str, cfg: dict) -> Optional[str]:
    loops = cfg.get("loops") or {}
    val = loops.get(role) or loops.get(role.lower())
    if not isinstance(val, str):
        return None
    val = val.strip().lower()
    return val if val in SUPPORTED_CLIS or val == "auto" else None


def _file_default_override(cfg: dict) -> Optional[str]:
    default = (cfg.get("default") or {}).get("agent")
    if not isinstance(default, str):
        return None
    default = default.strip().lower()
    return default if default in SUPPORTED_CLIS or default == "auto" else None


def _file_agent_assignment(role: str, cfg: dict) -> dict:
    """Return the [agents.<role>] table ({} if absent/malformed)."""
    agents = cfg.get("agents") or {}
    val = agents.get(role) or agents.get(role.lower())
    return val if isinstance(val, dict) else {}


def resolve_agent(role: str, active_cli: Optional[str] = None) -> str:
    """Return which CLI ('claude' / 'codex' / 'gemini' / 'copilot')
    should run the spawned child for this role.

    Resolution order: per-role env → [agents.<role>].cli → per-role
    file → default env → default file → active CLI. The final fallback
    is 'claude' for backward compatibility — pre-existing installs
    without a config keep working unchanged.
    """
    cfg = _load_file()
    chosen: Optional[str] = None

    def _agent_cli():
        v = _file_agent_assignment(role, cfg).get("cli")
        if isinstance(v, str):
            v = v.strip().lower()
            return v if v in SUPPORTED_CLIS or v == "auto" else None
        return None

    for resolver in (
        lambda: _env_role_override(role),
        _agent_cli,
        lambda: _file_role_override(role, cfg),
        lambda: _env_default_override(),
        lambda: _file_default_override(cfg),
    ):
        candidate = resolver()
        if candidate and candidate != "auto":
            return candidate
        if candidate == "auto":
            # explicit auto — break out of override chain and fall
            # through to active-CLI fallback
            chosen = None
            break

    if chosen is None:
        if active_cli and active_cli in SUPPORTED_CLIS:
            return active_cli

    # Last-resort default. Backward compat: existing users who never
    # touched config get the original 'claude' behavior.
    return "claude"


def resolve_model(cli: str, role: str = "") -> str:
    """Configured model for this spawn, or "" (let the CLI use its default).

    Priority (highest first):
      1. per-role env   THREADKEEPER_SPAWN_MODEL_<ROLE>
      2. file           [agents.<role>].model
      3. per-CLI env    THREADKEEPER_SPAWN_MODEL_<CLI>   (legacy)
      4. file           [models].<cli>                   (legacy)
      5. ""

    `role` is optional so legacy positional callers — resolve_model("claude") —
    keep working unchanged.
    """
    if role:
        env_role = os.environ.get(
            "THREADKEEPER_SPAWN_MODEL_" + role.upper().replace("-", "_"), ""
        ).strip()
        if env_role:
            return env_role
    cfg = _load_file()
    if role:
        m = _file_agent_assignment(role, cfg).get("model")
        if isinstance(m, str) and m.strip():
            return m.strip()
    env_cli = os.environ.get("THREADKEEPER_SPAWN_MODEL_" + cli.upper(), "").strip()
    if env_cli:
        return env_cli
    models = cfg.get("models") or {}
    val = models.get(cli) or models.get(cli.lower())
    return val.strip() if isinstance(val, str) else ""


def summary_table(active_cli: Optional[str]) -> str:
    """Human-readable per-role assignment table for the startup
    validator. Lines like:

        shadow_observer    → claude   (active CLI)
        curator            → codex    (env override)
        candidate_reviewer → claude   (file override)
        archivist          → auto     → claude (active CLI)
    """
    roles = (
        "archivist",
        "shadow_observer",
        "extract",
        "candidate_reviewer",
        "curator",
    )
    cfg = _load_file()
    out = []
    for role in roles:
        chosen = resolve_agent(role, active_cli)
        # Pick the source label
        if _env_role_override(role):
            src = "env override"
        elif _file_role_override(role, cfg):
            src = "file override"
        elif _env_default_override():
            src = "env default"
        elif _file_default_override(cfg):
            src = "file default"
        elif active_cli:
            src = "active CLI"
        else:
            src = "fallback"
        model = resolve_model(chosen)
        model_suffix = f" model={model}" if model else ""
        out.append(f"  {role:<18} → {chosen:<8}{model_suffix} ({src})")
    return "\n".join(out)
