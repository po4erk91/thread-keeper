"""Replay Claude Code PreToolUse events through the hook installed by setup.

The payload contract is documented at:
https://code.claude.com/docs/en/hooks#pretooluse-input
Task is the legacy name used before Claude Code 2.1.63:
https://code.claude.com/docs/en/sub-agents#restrict-which-subagents-can-be-spawned
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def installed_gate(tmp_path, monkeypatch):
    import threadkeeper._setup as setup_mod
    import threadkeeper.adapters as adapters_mod
    from threadkeeper.adapters.claude_code import ClaudeCodeAdapter

    hooks_dir = tmp_path / "managed-hooks"
    monkeypatch.setattr(setup_mod, "TK_HOOKS_DIR", hooks_dir)
    monkeypatch.setattr(
        adapters_mod, "installed_adapters", lambda: [ClaudeCodeAdapter()],
    )
    setup_mod.install_hooks(dry_run=False)

    settings_path = Path.home() / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text())
    blocks = settings["hooks"]["PreToolUse"]
    assert len(blocks) == 1, "setup must install one managed parallel-tool gate"
    return blocks[0], hooks_dir / "tk-task-gate.sh"


def _pre_tool_use(tool_name, prompt, description="Inspect repository", **inputs):
    return {
        "session_id": "task-gate-contract",
        "transcript_path": "/tmp/task-gate-contract.jsonl",
        "cwd": "/tmp/project",
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_use_id": "toolu_task_gate_contract",
        "tool_name": tool_name,
        "tool_input": {
            "prompt": prompt,
            "description": description,
            "subagent_type": "general-purpose",
            **inputs,
        },
    }


def _run_gate(installed_gate, payload, mode=None):
    assert shutil.which("jq"), "task-gate contract tests require jq on PATH"
    block, _ = installed_gate
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    if mode is not None:
        env["TK_TASK_GATE"] = mode
    return subprocess.run(
        [block["hooks"][0]["command"]],
        input=json.dumps(payload), capture_output=True, text=True,
        env=env, timeout=10,
    )


def test_setup_installs_supported_parallel_tool_matcher_and_managed_script(
    installed_gate,
):
    block, script = installed_gate
    assert block == {
        "matcher": "^(Task|Agent)$",
        "hooks": [{"type": "command", "command": str(script)}],
    }
    source = Path(__file__).resolve().parents[1] / "scripts/hooks/tk-task-gate.sh"
    assert script.read_bytes() == source.read_bytes()
    assert os.access(script, os.X_OK)
    for tool_name in ("Task", "Agent"):
        assert re.search(block["matcher"], tool_name), tool_name


@pytest.mark.parametrize("signal_field", ["prompt", "description"])
@pytest.mark.parametrize(
    ("mode", "exit_code", "warns"),
    [(None, 2, True), ("deny", 2, True), ("warn", 0, True), ("off", 0, False)],
)
def test_legacy_task_modes_use_nested_prompt_and_description(
    installed_gate, signal_field, mode, exit_code, warns,
):
    payload = _pre_tool_use("Task", "Inspect the repository carefully. " * 24)
    payload["tool_input"][signal_field] += " Migrate all packages in parallel."

    proc = _run_gate(installed_gate, payload, mode)

    assert proc.returncode == exit_code, proc.stderr
    assert proc.stdout == ""
    if warns:
        assert "tk-task-gate: Task tool" in proc.stderr
        assert "mcp__thread-keeper__spawn()" in proc.stderr
    else:
        assert proc.stderr == ""


@pytest.mark.parametrize("signal_field", ["prompt", "description"])
@pytest.mark.parametrize("mode", [None, "deny", "warn", "off"])
def test_agent_persistence_is_advisory_in_every_mode(
    installed_gate, signal_field, mode,
):
    payload = _pre_tool_use("Agent", "Inspect the repository.")
    payload["tool_input"][signal_field] += " Coordinate across sessions."

    proc = _run_gate(installed_gate, payload, mode)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    if mode == "off":
        assert proc.stderr == ""
    else:
        assert "tk-task-gate: native Agent" in proc.stderr
        assert "PERSISTENCE" in proc.stderr
        assert "mcp__thread-keeper__spawn()" in proc.stderr


def test_agent_in_turn_fanout_and_background_work_are_silent(installed_gate):
    payload = _pre_tool_use(
        "Agent", "Inspect all packages in parallel. " * 24,
        run_in_background=True,
    )

    proc = _run_gate(installed_gate, payload)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == proc.stderr == ""


def test_current_workflow_script_payload_is_unsupported(installed_gate):
    # Claude Code 2.1.296's Workflow schema takes script/name/scriptPath,
    # not Agent's prompt; a prompt-only persistence guard cannot cover it.
    payload = _pre_tool_use("Workflow", "")
    payload["tool_input"] = {
        "script": "return await agent('Coordinate across sessions.');",
    }
    block, _ = installed_gate
    assert re.search(block["matcher"], payload["tool_name"]) is None

    proc = _run_gate(installed_gate, payload)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == proc.stderr == ""


@pytest.mark.parametrize(
    "inputs",
    [
        {"prompt": "Inspect all packages in parallel and synthesize results. " * 16},
        {"subagent_type": "Explore"},
        {"subagent_type": "claude-code-guide"},
        {"subagent_type": "statusline-setup"},
        {"prompt": "Inspect all packages in parallel."},
    ],
    ids=["synthesis", "explore", "guide", "statusline", "short-prompt"],
)
def test_legacy_task_exemptions_remain_silent(installed_gate, inputs):
    payload = _pre_tool_use("Task", "Inspect all packages in parallel. " * 24)
    payload["tool_input"].update(inputs)

    proc = _run_gate(installed_gate, payload)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == proc.stderr == ""


@pytest.mark.parametrize("tool_name", ["Bash", "Workflow", "AgentStatus", "OtherTask"])
def test_unrelated_and_unsupported_tools_are_unmatched_and_silent(
    installed_gate, tool_name,
):
    block, _ = installed_gate
    assert re.search(block["matcher"], tool_name) is None
    payload = _pre_tool_use(
        tool_name, "Coordinate across sessions and migrate all packages. " * 24,
    )

    proc = _run_gate(installed_gate, payload)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == proc.stderr == ""
