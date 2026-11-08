from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tomllib
import venv
import zipfile

from jsonschema import Draft202012Validator, ValidationError
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "tests" / "schemas" / "agent-plugins" / "1.0.0"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


@pytest.mark.parametrize("name", ["plugin", "mcp"])
def test_manifests_match_published_schemas(name):
    from threadkeeper.agent_plugin import BUNDLE_ROOT

    schema = json.loads((SCHEMAS / f"{name}.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    manifest = json.loads((BUNDLE_ROOT / f"{name}.json").read_text())
    validator.validate(manifest)
    manifest["unexpected"] = True
    with pytest.raises(ValidationError):
        validator.validate(manifest)


def test_bundle_is_current_and_bootstrap_has_installer_guidance():
    from threadkeeper import _setup
    from threadkeeper.agent_plugin import BUNDLE_ROOT, check_bundle
    from threadkeeper.instructions import LIFECYCLE_GUIDANCE

    assert check_bundle(BUNDLE_ROOT, VERSION) == []
    skill = (BUNDLE_ROOT / "skills/thread-keeper/SKILL.md").read_text()
    frontmatter, body = skill.removeprefix("---\n").split("\n---\n", 1)
    metadata = yaml.safe_load(frontmatter)
    assert metadata["name"] == "thread-keeper"
    assert 0 < len(metadata["description"]) <= 1024
    assert _setup.CLAUDE_MD_BLOCK in body
    for rule in LIFECYCLE_GUIDANCE:
        assert rule in body


def test_plugin_registration_matches_setup(tmp_path, monkeypatch):
    import threadkeeper._setup as setup
    import threadkeeper.adapters as adapters
    from threadkeeper.agent_plugin import BUNDLE_ROOT

    captured = {}

    class FakeAdapter:
        name = "fake"

        def register_mcp_server(self, **kwargs):
            captured.update(kwargs)
            return "ok"

    monkeypatch.setattr(setup, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(adapters, "installed_adapters", lambda: [FakeAdapter()])
    setup.install_mcp_servers(dry_run=True)
    servers = json.loads((BUNDLE_ROOT / "mcp.json").read_text())["mcpServers"]
    portable = servers[captured["name"]]
    assert portable["type"] == "stdio"
    assert portable["command"] == "uvx"
    assert portable["args"] == [
        "--from", f"threadkeeper[semantic]=={VERSION}", "python", *captured["args"],
    ]
    installed_env = captured["env"].copy()
    assert installed_env.pop("PYTHONPATH") == str(tmp_path)
    portable_env = portable["env"].copy()
    assert portable_env.pop("THREADKEEPER_AUTO_UPDATE_INTERVAL_S") == "0"
    assert portable_env.pop("PYTHONPATH") == ""
    assert portable_env == installed_env
    assert "cwd" not in portable  # The host uses the plugin root.


def test_export_is_separate_from_host_setup_and_dry_run_writes_nothing(tmp_path, monkeypatch):
    from threadkeeper import _setup
    from threadkeeper.agent_plugin import check_bundle

    def unexpected(*args, **kwargs):
        pytest.fail("Plugin export must not change host setup")

    for name in ("install_tk_dir", "install_mcp_servers", "install_instructions", "install_hooks", "install_lesson_index"):
        monkeypatch.setattr(_setup, name, unexpected)
    target = tmp_path / "plugin with spaces"
    assert _setup.main(["--export-plugin", str(target), "--dry-run"]) == 0
    assert not target.exists()
    assert _setup.main(["--export-plugin", str(target)]) == 0
    assert check_bundle(target, VERSION) == []
    before = {p: p.read_bytes() for p in target.rglob("*") if p.is_file()}
    assert _setup.main(["--export-plugin", str(target)]) == 0
    assert {p: p.read_bytes() for p in before} == before


def test_check_fails_for_missing_stale_or_extra_files(tmp_path):
    from threadkeeper.agent_plugin import check_bundle, main, write_bundle

    write_bundle(tmp_path, VERSION)
    (tmp_path / "plugin.json").write_text("{}\n")
    (tmp_path / "mcp.json").unlink()
    (tmp_path / "extra.txt").write_text("unexpected")
    assert check_bundle(tmp_path, VERSION) == [
        "unexpected file: extra.txt", "stale file: plugin.json", "missing file: mcp.json",
    ]
    assert main(["--check", "--output", str(tmp_path)]) == 1


def test_sdist_wheel_and_clean_install_contain_current_bundle(tmp_path):
    from threadkeeper.agent_plugin import bundle_files

    # Build from an isolated source tree so stale local build files cannot help.
    import shutil

    source = tmp_path / "source"
    source.mkdir()
    shutil.copytree(ROOT / "threadkeeper", source / "threadkeeper", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, source / name)
    dist = tmp_path / "dist"
    subprocess.run([sys.executable, "-m", "build", "--no-isolation", "--outdir", str(dist)],
                   cwd=source, capture_output=True, text=True, check=True)
    expected = bundle_files(VERSION)
    with tarfile.open(next(dist.glob("*.tar.gz"))) as archive:
        for name, content in expected.items():
            member = f"threadkeeper-{VERSION}/threadkeeper/assets/agent-plugin/{name}"
            assert archive.extractfile(member).read().decode() == content
    wheel = next(dist.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        for name, content in expected.items():
            assert archive.read(f"threadkeeper/assets/agent-plugin/{name}").decode() == content
    clean_env = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(clean_env)
    python = clean_env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run([str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
                   cwd=tmp_path, capture_output=True, text=True, check=True)
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    subprocess.run([str(python), "-I", "-m", "threadkeeper.agent_plugin", "--check"],
                   cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    target = tmp_path / "exported"
    subprocess.run([str(python), "-I", "-m", "threadkeeper._setup", "--export-plugin", str(target)],
                   cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    assert {p.relative_to(target).as_posix(): p.read_text() for p in target.rglob("*") if p.is_file()} == expected
