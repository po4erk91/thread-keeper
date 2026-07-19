"""Runtime CLI capability/model catalog for settings clients."""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .adapters import get_adapter
from .adapters.base import find_cli_executable
from .agent_metadata import AGENT_ROLES, MECHANICAL_JOBS
from . import spawn_config


CATALOG_TTL_S = 6 * 60 * 60
RELEASE_CHECK_TIMEOUT_S = 3.0
_ANTIGRAVITY_RELEASE_BASE = (
    "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app"
)
_VERSION_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?)")


def _request_json(url: str, timeout_s: float = RELEASE_CHECK_TIMEOUT_S) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "threadkeeper-agent-status",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("release endpoint returned a non-object payload")
    return payload


def _antigravity_platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    os_name = "darwin" if system == "darwin" else "linux" if system == "linux" else ""
    arch = "arm64" if machine in {"arm64", "aarch64"} else "amd64" if machine in {"x86_64", "amd64"} else ""
    if not os_name or not arch:
        return ""
    return f"{os_name}_{arch}"


def _latest_release(cli: str, timeout_s: float = RELEASE_CHECK_TIMEOUT_S) -> dict[str, Any]:
    """Read the latest stable release from the vendor's official cloud source."""
    try:
        if cli == "claude":
            url = "https://registry.npmjs.org/@anthropic-ai/claude-code/latest"
            payload = _request_json(url, timeout_s)
            version = str(payload.get("version") or "").strip()
            source = "Anthropic npm registry"
            release_url = "https://www.npmjs.com/package/@anthropic-ai/claude-code"
        elif cli == "codex":
            url = "https://api.github.com/repos/openai/codex/releases/latest"
            payload = _request_json(url, timeout_s)
            version = str(payload.get("tag_name") or "").removeprefix("rust-v").strip()
            source = "OpenAI GitHub Releases"
            release_url = str(payload.get("html_url") or "https://github.com/openai/codex/releases/latest")
        elif cli == "copilot":
            url = "https://api.github.com/repos/github/copilot-cli/releases/latest"
            payload = _request_json(url, timeout_s)
            version = str(payload.get("tag_name") or "").removeprefix("v").strip()
            source = "GitHub Copilot Releases"
            release_url = str(payload.get("html_url") or "https://github.com/github/copilot-cli/releases/latest")
        elif cli == "antigravity":
            platform_id = _antigravity_platform()
            if not platform_id:
                raise ValueError("unsupported platform for Antigravity release checks")
            url = f"{_ANTIGRAVITY_RELEASE_BASE}/manifests/{platform_id}.json"
            payload = _request_json(url, timeout_s)
            version = str(payload.get("version") or "").strip()
            source = "Google Antigravity release manifest"
            release_url = url
        else:
            raise ValueError(f"unsupported CLI: {cli}")
        if not version:
            raise ValueError("release endpoint did not return a version")
        return {
            "latest_version": version,
            "latest_version_source": source,
            "release_url": release_url,
            "version_check_error": None,
        }
    except (OSError, ValueError, TypeError, urllib.error.URLError, TimeoutError) as exc:
        return {
            "latest_version": "",
            "latest_version_source": "",
            "release_url": "",
            "version_check_error": f"Latest-version check failed: {exc}",
        }


def _version_token(value: str) -> str:
    match = _VERSION_RE.search(value or "")
    return match.group(1).lower() if match else ""


def _update_command(cli: str, binary: str) -> list[str]:
    if not binary:
        return []
    if cli in {"claude", "antigravity", "copilot"}:
        return [binary, "update"]
    if cli != "codex":
        return []
    brew = find_cli_executable("brew")
    lowered = binary.lower()
    if brew and any(marker in lowered for marker in ("/homebrew/", "/cellar/", "/caskroom/")):
        return [brew, "upgrade", "--cask", "codex"]
    npm = find_cli_executable("npm")
    if npm:
        return [npm, "install", "-g", "@openai/codex@latest"]
    if brew:
        return [brew, "upgrade", "--cask", "codex"]
    return []


def _command_label(command: list[str]) -> str:
    if not command:
        return ""
    return " ".join([Path(command[0]).name, *command[1:]])


def _cache_path() -> Path:
    raw = os.environ.get("THREADKEEPER_MODEL_CATALOG_CACHE", "")
    return Path(raw).expanduser() if raw else Path("~/.threadkeeper/model_catalog.json").expanduser()


def _binary_for(adapter) -> str:
    argv = adapter.spawn_argv("catalog-probe", model="", effort="")
    return str(argv[0]) if argv else ""


def _version(binary: str) -> tuple[str, str | None]:
    if not binary:
        return "", None
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True,
            timeout=3.0, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", f"Version check failed: {exc}"
    line = (result.stdout or result.stderr or "").strip().splitlines()
    return (line[0][:160] if line else ""), (
        None if result.returncode == 0 else "Version command failed"
    )


def _read_cache() -> dict[str, Any]:
    path = _cache_path()
    try:
        payload = json.loads(path.read_text())
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_cache(payload: dict[str, Any]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        pass


def cli_catalog(refresh: bool = False, now: int | None = None) -> list[dict[str, Any]]:
    """Return current spawn-CLI capabilities with bounded local discovery.

    The cache is keyed by executable path + version, so upgrading a CLI forces
    immediate discovery even inside the normal six-hour freshness window.
    """
    now_i = int(time.time() if now is None else now)
    cached = _read_cache()
    cached_clis = cached.get("clis", {}) if isinstance(cached.get("clis"), dict) else {}
    next_cache: dict[str, Any] = {"generated_at": now_i, "clis": {}}
    result: list[dict[str, Any]] = []

    for cli in spawn_config.SUPPORTED_CLIS:
        adapter = get_adapter(cli)
        if not adapter:
            result.append({
                "id": cli, "name": cli, "installed": False,
                "executable": "", "version": "", "models": [],
                "model_source": "No adapter", "source_updated_at": None,
                "catalog_refreshed_at": now_i, "catalog_age_s": 0,
                "stale": False, "error": "No adapter is registered.",
                "configured_model": spawn_config.resolve_model(cli),
                "configured_model_in_catalog": False,
                "effort_options": list(spawn_config.EFFORT_OPTIONS.get(cli, ())),
                "model_efforts": {},
                "effort_mode": "model" if cli == "antigravity" else "independent",
                "effort_note": "Reasoning level is encoded in the model label." if cli == "antigravity" else "",
                "configured_effort": spawn_config.resolve_effort(cli),
                "latest_version": "", "latest_version_source": "",
                "release_url": "", "version_check_error": None,
                "update_available": False, "update_supported": False,
                "update_command_label": "",
            })
            continue

        binary = _binary_for(adapter)
        version, version_error = _version(binary)
        cache_key = f"{binary}\0{version}"
        previous = cached_clis.get(cli, {})
        previous_age = now_i - int(previous.get("catalog_refreshed_at", 0) or 0)
        cache_valid = (
            not refresh and previous.get("cache_key") == cache_key
            and previous_age >= 0 and previous_age < CATALOG_TTL_S
            and "latest_version" in previous
        )
        if cache_valid:
            discovered = dict(previous)
            discovered["catalog_age_s"] = previous_age
            # Failed refreshes cache the last successful catalog with
            # stale=True. Preserve that truth until a manual/TTL refresh
            # succeeds instead of relabelling fallback data as live.
            discovered["stale"] = bool(previous.get("stale", False))
        else:
            native = adapter.discover_models(timeout_s=5.0)
            models = [str(v).strip() for v in native.get("models", []) if str(v).strip()]
            models = list(dict.fromkeys(models))
            native_error = native.get("error") or version_error
            if (
                not models and native_error
                and previous.get("cache_key") == cache_key
                and previous.get("models")
            ):
                discovered = dict(previous)
                discovered.update({
                    "catalog_age_s": max(0, previous_age),
                    "stale": True,
                    "error": f"{native_error} Showing the last successful catalog.",
                })
            else:
                discovered = {
                    "cache_key": cache_key,
                    "models": models,
                    "model_source": native.get("source") or "CLI discovery",
                    "source_updated_at": native.get("source_updated_at"),
                    "catalog_refreshed_at": now_i,
                    "catalog_age_s": 0,
                    "stale": False,
                    "error": native_error,
                    "effort_options": native.get("effort_options"),
                    "model_efforts": native.get("model_efforts") or {},
                }
            release = _latest_release(cli)
            if (
                release.get("version_check_error")
                and previous.get("cache_key") == cache_key
                and previous.get("latest_version")
            ):
                release.update({
                    "latest_version": previous.get("latest_version", ""),
                    "latest_version_source": previous.get("latest_version_source", ""),
                    "release_url": previous.get("release_url", ""),
                    "version_check_error": (
                        f"{release['version_check_error']} Showing the last known release."
                    ),
                })
            discovered.update(release)

        configured_model = spawn_config.resolve_model(cli)
        live_models = list(discovered.get("models", []))
        configured_in_catalog = configured_model in live_models if configured_model else True
        # A configured value never disappears from the picker just because the
        # provider removed it or discovery is unavailable. The UI labels it as
        # configured/custom rather than claiming it is currently advertised.
        if configured_model and configured_model not in live_models:
            live_models.append(configured_model)

        update_command = _update_command(cli, binary)
        installed_version = _version_token(version)
        latest_version = _version_token(str(discovered.get("latest_version") or ""))
        entry = {
            "id": cli,
            "name": {
                "claude": "Claude Code", "codex": "Codex",
                "antigravity": "Antigravity", "copilot": "GitHub Copilot",
            }.get(cli, cli),
            "installed": bool(binary),
            "executable": binary,
            "version": version,
            **{k: v for k, v in discovered.items() if k != "cache_key"},
            "models": live_models,
            "configured_model": configured_model,
            "configured_model_in_catalog": configured_in_catalog,
            "supports_custom_model": True,
            "effort_options": (
                discovered.get("effort_options")
                or list(spawn_config.EFFORT_OPTIONS.get(cli, ()))
            ),
            "model_efforts": discovered.get("model_efforts") or {},
            "effort_mode": "model" if cli == "antigravity" else "independent",
            "effort_note": (
                "Antigravity encodes reasoning effort in the selected model; "
                "there is no independent effort flag."
                if cli == "antigravity" else ""
            ),
            "configured_effort": spawn_config.resolve_effort(cli),
            "update_available": bool(
                binary and installed_version and latest_version
                and installed_version != latest_version
            ),
            "update_supported": bool(update_command),
            "update_command_label": _command_label(update_command),
        }
        if not binary:
            entry["error"] = "CLI executable is not on PATH."
        result.append(entry)
        next_cache["clis"][cli] = {**discovered, "cache_key": cache_key}

    _write_cache(next_cache)
    return result


def update_cli(cli: str, timeout_s: float = 300.0) -> dict[str, Any]:
    """Update one allowlisted CLI through its vendor-supported mechanism."""
    cli_id = (cli or "").strip().lower()
    if cli_id not in spawn_config.SUPPORTED_CLIS:
        raise ValueError(f"unsupported CLI: {cli_id or '<empty>'}")
    entry = next(
        (item for item in cli_catalog(refresh=True) if item["id"] == cli_id),
        None,
    )
    if not entry or not entry.get("installed"):
        raise RuntimeError(f"{cli_id} is not installed")
    if not entry.get("update_available"):
        return {
            "cli": cli_id,
            "updated": False,
            "current_version": entry.get("version", ""),
            "latest_version": entry.get("latest_version", ""),
            "command": entry.get("update_command_label", ""),
            "message": f"{entry['name']} is already up to date.",
            "output": "",
        }
    command = _update_command(cli_id, str(entry.get("executable") or ""))
    if not command:
        raise RuntimeError(
            f"No supported updater was detected for {entry['name']}; use its official installer."
        )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(30.0, timeout_s),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{entry['name']} update failed: {exc}") from exc
    output = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )[-4000:]
    if completed.returncode != 0:
        raise RuntimeError(
            f"{entry['name']} updater exited {completed.returncode}: "
            f"{output or 'no diagnostic output'}"
        )
    return {
        "cli": cli_id,
        "updated": True,
        "current_version": entry.get("version", ""),
        "latest_version": entry.get("latest_version", ""),
        "command": _command_label(command),
        "message": f"Updated {entry['name']} to the latest available release.",
        "output": output,
    }


def settings_catalog(refresh: bool = False) -> dict[str, Any]:
    """Stable JSON contract consumed by the native settings window."""
    from .identity import active_cli

    active = active_cli()
    clis = cli_catalog(refresh=refresh)
    roles: list[dict[str, Any]] = []
    for meta in AGENT_ROLES:
        role = meta["role"]
        detail = spawn_config.resolution_details(role, active)
        cli_dynamic = spawn_config.agent_cli_is_dynamic(role, active)
        if cli_dynamic:
            detail["cli"] = ""
            if detail["cli_source"] != "process environment":
                detail["cli_source"] = "active host CLI (fallback Claude)"
                detail["cli_source_key"] = ""
            if detail["model_source"] not in {"role override", "process environment"}:
                detail["model"] = ""
                detail["model_source"] = "selected active host CLI"
                detail["model_source_key"] = ""
            if detail["effort_source"] not in {"role override", "process environment"}:
                detail["effort"] = ""
                detail["effort_source"] = "selected active host CLI"
                detail["effort_source_key"] = ""
        roles.append({
            **meta,
            **detail,
            "cli_dynamic": cli_dynamic,
            "cli_inherited": detail["cli_source"] != "role override",
            "model_inherited": detail["model_source"] != "role override",
            "effort_inherited": detail["effort_source"] != "role override",
        })
    return {
        "generated_at": int(time.time()),
        "active_cli": active,
        "clis": clis,
        "agent_roles": roles,
        "mechanical_jobs": [dict(job) for job in MECHANICAL_JOBS],
        "runtime_overrides": spawn_config.runtime_spawn_overrides(),
        "warnings": [
            line.strip()
            for line in spawn_config._spawn_warnings(
                active,
                {
                    item["id"]: [
                        model for model in item.get("models", [])
                        if item.get("configured_model_in_catalog", True)
                        or model != item.get("configured_model")
                    ]
                    for item in clis
                },
            )
        ],
    }
