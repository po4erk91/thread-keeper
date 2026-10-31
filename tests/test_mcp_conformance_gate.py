"""Regression coverage for the official MCP conformance release gate."""
from __future__ import annotations

from importlib import import_module
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFORMANCE_REF = "c37eec888e1c6ff140af79987a40008548b7cc5f"
BASELINES = (
    ROOT / "tests" / "conformance" / "expected-failures.2025-11-25.yml",
    ROOT / "tests" / "conformance" / "expected-failures.2026-07-28.yml",
)


def test_conformance_transport_reuses_the_stdio_mcp_registry(fresh_mp):
    conformance = import_module("threadkeeper.conformance_server")
    production = import_module("threadkeeper.server")

    assert conformance.mcp is production.mcp is fresh_mp["mcp"]
    registered_tools = set(conformance.mcp._tool_manager._tools)
    assert registered_tools
    assert "test_image_content" not in registered_tools


def test_expected_failure_baselines_are_narrow_server_lists():
    for path in BASELINES:
        parsed = yaml.safe_load(path.read_text())

        assert set(parsed) == {"server"}
        assert parsed["server"]
        assert len(parsed["server"]) == len(set(parsed["server"]))
        assert all(isinstance(entry, str) and entry for entry in parsed["server"])


def test_ci_pins_and_runs_both_official_requirement_sets():
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text()

    assert f"CONFORMANCE_REF={CONFORMANCE_REF}" in workflow
    assert "python -m threadkeeper.conformance_server" in workflow
    for version in ("2025-11-25", "2026-07-28"):
        assert f"--requirements {version}" in workflow
        assert f"expected-failures.{version}.yml" in workflow
