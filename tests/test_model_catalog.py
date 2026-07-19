from __future__ import annotations

import json
import subprocess


class _FakeAdapter:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def spawn_argv(self, prompt, *, model="", effort=""):
        return ["/fake/bin/agent", "-p", prompt]

    def discover_models(self, timeout_s=5.0):
        self.calls += 1
        return dict(self.result)


def _catalog(monkeypatch, tmp_path, adapter, configured=""):
    from threadkeeper import model_catalog

    monkeypatch.setenv(
        "THREADKEEPER_MODEL_CATALOG_CACHE", str(tmp_path / "models.json")
    )
    monkeypatch.setattr(model_catalog.spawn_config, "SUPPORTED_CLIS", ("codex",))
    monkeypatch.setattr(model_catalog, "get_adapter", lambda cli: adapter)
    monkeypatch.setattr(model_catalog, "_version", lambda binary: ("1.2.3", None))
    monkeypatch.setattr(
        model_catalog.spawn_config, "resolve_model", lambda cli: configured
    )
    monkeypatch.setattr(
        model_catalog.spawn_config, "resolve_effort", lambda cli: "high"
    )
    monkeypatch.setattr(
        model_catalog,
        "_latest_release",
        lambda cli: {
            "latest_version": "1.2.4",
            "latest_version_source": "official test cloud",
            "release_url": "https://example.test/release",
            "version_check_error": None,
        },
    )
    monkeypatch.setattr(
        model_catalog,
        "_update_command",
        lambda cli, binary: [binary, "update"] if binary else [],
    )
    return model_catalog


def test_catalog_caches_by_executable_version_and_reports_freshness(monkeypatch, tmp_path):
    adapter = _FakeAdapter({
        "models": ["gpt-a", "gpt-b"],
        "source": "native test models",
        "source_updated_at": 100,
        "error": None,
        "effort_options": ["low", "high"],
        "model_efforts": {"gpt-a": ["low"], "gpt-b": ["high"]},
    })
    catalog = _catalog(monkeypatch, tmp_path, adapter)
    first = catalog.cli_catalog(refresh=False, now=1_000)[0]
    second = catalog.cli_catalog(refresh=False, now=1_100)[0]
    assert adapter.calls == 1
    assert first["version"] == "1.2.3"
    assert first["model_source"] == "native test models"
    assert first["source_updated_at"] == 100
    assert second["catalog_age_s"] == 100
    assert second["stale"] is False
    assert second["effort_options"] == ["low", "high"]
    assert second["model_efforts"] == {"gpt-a": ["low"], "gpt-b": ["high"]}
    assert first["latest_version"] == "1.2.4"
    assert first["update_available"] is True
    assert first["update_supported"] is True
    assert first["update_command_label"] == "agent update"


def test_configured_custom_model_survives_missing_live_catalog(monkeypatch, tmp_path):
    adapter = _FakeAdapter({
        "models": ["live-model"], "source": "native",
        "source_updated_at": 1, "error": None,
    })
    catalog = _catalog(monkeypatch, tmp_path, adapter, configured="pinned-old")
    item = catalog.cli_catalog(refresh=True, now=2_000)[0]
    assert item["models"] == ["live-model", "pinned-old"]
    assert item["configured_model"] == "pinned-old"
    assert item["configured_model_in_catalog"] is False
    assert item["supports_custom_model"] is True


def test_catalog_discovery_failure_keeps_configured_selection(monkeypatch, tmp_path):
    adapter = _FakeAdapter({
        "models": [], "source": "native", "source_updated_at": None,
        "error": "timed out",
    })
    catalog = _catalog(monkeypatch, tmp_path, adapter, configured="custom-model")
    item = catalog.cli_catalog(refresh=True, now=3_000)[0]
    assert item["models"] == ["custom-model"]
    assert item["error"] == "timed out"


def test_failed_refresh_keeps_last_successful_catalog_as_stale(monkeypatch, tmp_path):
    adapter = _FakeAdapter({
        "models": ["live-model"], "source": "native",
        "source_updated_at": 1, "error": None,
    })
    catalog = _catalog(monkeypatch, tmp_path, adapter)
    assert catalog.cli_catalog(refresh=True, now=1_000)[0]["models"] == ["live-model"]
    adapter.result = {
        "models": [], "source": "native", "source_updated_at": None,
        "error": "refresh timed out",
    }
    item = catalog.cli_catalog(refresh=True, now=2_000)[0]
    assert item["models"] == ["live-model"]
    assert item["stale"] is True
    assert item["catalog_age_s"] == 1_000
    assert "last successful" in item["error"]
    cached = catalog.cli_catalog(refresh=False, now=2_100)[0]
    assert cached["stale"] is True
    assert "last successful" in cached["error"]


def test_codex_native_catalog_parses_models_and_efforts(monkeypatch, tmp_path):
    from threadkeeper.adapters import codex

    adapter = codex.CodexAdapter()
    adapter._skills_dir = tmp_path / "skills"
    monkeypatch.setattr(codex, "find_cli_executable", lambda *names: "/bin/codex")

    payload = {
        "models": [
            {
                "slug": "gpt-live",
                "supported_reasoning_levels": [
                    {"effort": "low"}, {"effort": "xhigh"},
                ],
            },
            {
                "slug": "gpt-fast",
                "supported_reasoning_levels": [{"effort": "medium"}],
            },
        ]
    }
    monkeypatch.setattr(
        codex.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps(payload), ""
        ),
    )
    found = adapter.discover_models(timeout_s=0.1)
    assert found["models"] == ["gpt-live", "gpt-fast"]
    assert found["effort_options"] == ["low", "xhigh", "medium"]
    assert found["model_efforts"] == {
        "gpt-live": ["low", "xhigh"],
        "gpt-fast": ["medium"],
    }
    assert found["source"] == "codex debug models"


def test_settings_catalog_does_not_claim_helper_fallback_as_current_cli(
    monkeypatch,
):
    from threadkeeper import identity, model_catalog

    monkeypatch.setattr(identity, "active_cli", lambda: None)
    monkeypatch.setattr(model_catalog, "cli_catalog", lambda refresh=False: [])
    monkeypatch.setattr(model_catalog.spawn_config, "runtime_spawn_overrides", lambda: [])
    monkeypatch.setattr(model_catalog.spawn_config, "_spawn_warnings", lambda *args: [])
    monkeypatch.setattr(
        model_catalog.spawn_config, "agent_cli_is_dynamic", lambda role, active: True
    )
    monkeypatch.setattr(
        model_catalog.spawn_config,
        "resolution_details",
        lambda role, active: {
            "role": role,
            "cli": "claude",
            "cli_source": "fallback",
            "cli_source_key": "",
            "model": "",
            "model_source": "CLI native default",
            "model_source_key": "",
            "effort": "",
            "effort_source": "CLI native default",
            "effort_source_key": "",
        },
    )
    catalog = model_catalog.settings_catalog()
    role = catalog["agent_roles"][0]
    assert role["cli"] == ""
    assert role["cli_dynamic"] is True
    assert role["cli_source"] == "active host CLI (fallback Claude)"
    assert role["model_source"] == "selected active host CLI"


def test_antigravity_timeout_is_honest_fallback(monkeypatch):
    from threadkeeper.adapters import antigravity

    adapter = antigravity.AntigravityAdapter()
    monkeypatch.setattr(
        antigravity, "find_cli_executable", lambda *names: "/bin/agy"
    )
    monkeypatch.setattr(
        antigravity.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(args[0], 0.1)
        ),
    )
    found = adapter.discover_models(timeout_s=0.1)
    assert found["models"] == []
    assert "failed" in found["error"].lower()


def test_latest_release_uses_official_vendor_contracts(monkeypatch):
    from threadkeeper import model_catalog

    payloads = {
        "registry.npmjs.org": {"version": "2.1.215"},
        "openai/codex": {
            "tag_name": "rust-v0.144.6",
            "html_url": "https://github.com/openai/codex/releases/tag/rust-v0.144.6",
        },
        "github/copilot-cli": {
            "tag_name": "v1.0.71",
            "html_url": "https://github.com/github/copilot-cli/releases/tag/v1.0.71",
        },
        "manifests/darwin_arm64": {"version": "1.0.7"},
    }

    def fake_request(url, timeout_s):
        return next(payload for marker, payload in payloads.items() if marker in url)

    monkeypatch.setattr(model_catalog, "_request_json", fake_request)
    monkeypatch.setattr(model_catalog, "_antigravity_platform", lambda: "darwin_arm64")

    assert model_catalog._latest_release("claude")["latest_version"] == "2.1.215"
    assert model_catalog._latest_release("codex")["latest_version"] == "0.144.6"
    assert model_catalog._latest_release("copilot")["latest_version"] == "1.0.71"
    antigravity = model_catalog._latest_release("antigravity")
    assert antigravity["latest_version"] == "1.0.7"
    assert "Google Antigravity" in antigravity["latest_version_source"]


def test_update_cli_runs_only_allowlisted_direct_command(monkeypatch):
    from threadkeeper import model_catalog

    monkeypatch.setattr(
        model_catalog,
        "cli_catalog",
        lambda refresh: [{
            "id": "copilot", "name": "GitHub Copilot", "installed": True,
            "executable": "/opt/copilot", "version": "1.0.48",
            "latest_version": "1.0.71", "update_available": True,
            "update_command_label": "copilot update",
        }],
    )
    monkeypatch.setattr(
        model_catalog, "_update_command",
        lambda cli, binary: [binary, "update"],
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "updated", "")

    monkeypatch.setattr(model_catalog.subprocess, "run", fake_run)
    result = model_catalog.update_cli("copilot", timeout_s=31)

    assert calls[0][0] == ["/opt/copilot", "update"]
    assert "shell" not in calls[0][1]
    assert result["updated"] is True
    assert result["latest_version"] == "1.0.71"


def test_update_cli_rejects_unknown_cli():
    from threadkeeper import model_catalog

    import pytest
    with pytest.raises(ValueError, match="unsupported CLI"):
        model_catalog.update_cli("not-a-cli")


def test_agent_status_update_cli_json_contract(monkeypatch, capsys):
    from threadkeeper import agent_status, model_catalog

    monkeypatch.setattr(
        model_catalog,
        "update_cli",
        lambda cli: {
            "cli": cli, "updated": True,
            "current_version": "1.0", "latest_version": "2.0",
            "command": "copilot update", "message": "updated", "output": "ok",
        },
    )

    assert agent_status.main(["--update-cli", "copilot"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cli"] == "copilot"
    assert payload["updated"] is True


def test_minimal_path_finds_versioned_node_cli(monkeypatch, tmp_path):
    from threadkeeper.adapters import base

    fake_home = tmp_path / "home"
    old_binary = fake_home / ".nvm/versions/node/v9.20.0/bin/codex"
    binary = fake_home / ".nvm/versions/node/v24.10.0/bin/codex"
    for item in (old_binary, binary):
        item.parent.mkdir(parents=True)
        item.write_text("#!/bin/sh\n")
        item.chmod(0o755)
    monkeypatch.setattr(base.Path, "home", lambda: fake_home)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert base.find_cli_executable("codex") == str(binary)
