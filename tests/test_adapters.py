"""CLI adapter contract tests.

For each adapter we verify:
  * is_installed() honors the file/exec presence heuristic
  * register_mcp_server creates/updates the right config file
  * iter_messages parses synthetic transcripts into NormalizedMessage
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest


def _bootstrap(tmp_path, monkeypatch):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_INGEST_CAP": "0",
        "THREADKEEPER_SKILL_WATCH_INTERVAL_S": "0",
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_SEARCH_PROXY_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import db
    from threadkeeper.adapters import (
        ADAPTERS, installed_adapters,
        claude_code as cc_mod,
        claude_desktop as cd_mod,
        codex as codex_mod,
        antigravity as agy_mod,
        gemini as gem_mod,
        copilot as cop_mod,
        vscode as vsc_mod,
    )
    return {
        "db": db,
        "ADAPTERS": ADAPTERS,
        "installed_adapters": installed_adapters,
        "claude": cc_mod.ADAPTER,
        "claude_desktop": cd_mod.ADAPTER,
        "codex": codex_mod.ADAPTER,
        "antigravity": agy_mod.ADAPTER,
        "gemini": gem_mod.ADAPTER,
        "copilot": cop_mod.ADAPTER,
        "vscode": vsc_mod.ADAPTER,
    }


# ---------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------

def test_registry_lists_seven_adapters(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    names = {a.name for a in pkg["ADAPTERS"]}
    assert names == {
        "claude-code", "claude-desktop", "codex", "antigravity",
        "gemini", "copilot", "vscode",
    }


# ---------------------------------------------------------------------
# Claude Code: round-trip MCP register + parse
# ---------------------------------------------------------------------

def test_claude_register_mcp_writes_config(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cfg = fake_home / ".claude.json"
    monkeypatch.setattr(pkg["claude"], "config_path", cfg)

    result = pkg["claude"].register_mcp_server(
        name="thread-keeper",
        command="/opt/python",
        args=["-m", "threadkeeper.server"],
        env={"PYTHONPATH": "/repo"},
    )
    assert "add" in result or "update" in result
    body = json.loads(cfg.read_text())
    assert body["mcpServers"]["thread-keeper"]["command"] == "/opt/python"

    # Idempotency
    result2 = pkg["claude"].register_mcp_server(
        name="thread-keeper",
        command="/opt/python",
        args=["-m", "threadkeeper.server"],
        env={"PYTHONPATH": "/repo"},
    )
    assert "already current" in result2


def test_claude_iter_messages_parses_jsonl(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    fp = tmp_path / "claude_session.jsonl"
    fp.write_text(
        json.dumps({
            "uuid": "c-1",
            "type": "assistant",
            "timestamp": "2026-05-14T10:00:00Z",
            "sessionId": "sess-a",
            "message": {
                "role": "assistant",
                "model": "claude-opus",
                "content": [{"type": "text", "text": "hello world"}],
            },
        }) + "\n"
        + json.dumps({
            "uuid": "c-2",
            "type": "user",
            "timestamp": "2026-05-14T10:00:01Z",
            "sessionId": "sess-a",
            "message": {"role": "user", "content": "hi back"},
        }) + "\n"
    )
    msgs = list(pkg["claude"].iter_messages(fp))
    assert [m.uuid for m in msgs] == ["c-1", "c-2"]
    assert msgs[0].content == "hello world"
    assert msgs[0].role == "assistant"
    assert msgs[1].content == "hi back"
    assert msgs[1].role == "user"


# ---------------------------------------------------------------------
# Claude Desktop — Electron app, separate config from Claude Code CLI
# ---------------------------------------------------------------------

def test_claude_desktop_register_mcp_writes_config(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    cfg = tmp_path / "Application Support" / "Claude" / "claude_desktop_config.json"
    monkeypatch.setattr(pkg["claude_desktop"], "config_path", cfg)

    result = pkg["claude_desktop"].register_mcp_server(
        name="thread-keeper",
        command="/opt/python",
        args=["-m", "threadkeeper.server"],
        env={"PYTHONPATH": "/repo"},
    )
    assert "add" in result or "update" in result
    body = json.loads(cfg.read_text())
    assert body["mcpServers"]["thread-keeper"]["command"] == "/opt/python"
    assert body["mcpServers"]["thread-keeper"]["env"]["PYTHONPATH"] == "/repo"

    # Idempotency
    result2 = pkg["claude_desktop"].register_mcp_server(
        name="thread-keeper",
        command="/opt/python",
        args=["-m", "threadkeeper.server"],
        env={"PYTHONPATH": "/repo"},
    )
    assert "already current" in result2


def test_claude_desktop_preserves_other_servers_and_preferences(
    tmp_path, monkeypatch,
):
    """A live Claude Desktop config typically holds other MCP entries +
    GUI preferences. Registration must merge in place without touching
    sibling keys."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"ghost-ai": {"command": "ghost-ai"}},
        "preferences": {"sidebarMode": "epitaxy"},
    }))
    monkeypatch.setattr(pkg["claude_desktop"], "config_path", cfg)
    pkg["claude_desktop"].register_mcp_server(
        name="thread-keeper",
        command="/opt/python",
        args=["-m", "threadkeeper.server"],
        env={},
    )
    body = json.loads(cfg.read_text())
    assert "ghost-ai" in body["mcpServers"]
    assert "thread-keeper" in body["mcpServers"]
    assert body["preferences"]["sidebarMode"] == "epitaxy"


def test_claude_desktop_no_transcripts_no_hooks(tmp_path, monkeypatch):
    """Adapter intentionally skips transcript ingest (Electron IndexedDB
    is fragile to parse on disk) and has no hook mechanism."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    assert pkg["claude_desktop"].transcript_files() == []
    assert list(pkg["claude_desktop"].iter_messages(tmp_path / "x")) == []
    assert pkg["claude_desktop"].hooks_supported() is False
    assert pkg["claude_desktop"].instructions_path() is None


def test_claude_desktop_unregister_removes_entry(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "thread-keeper": {"command": "/opt/python", "args": []},
            "ghost-ai": {"command": "ghost-ai"},
        }
    }))
    monkeypatch.setattr(pkg["claude_desktop"], "config_path", cfg)
    msg = pkg["claude_desktop"].unregister_mcp_server("thread-keeper")
    assert "removed" in msg
    body = json.loads(cfg.read_text())
    assert "thread-keeper" not in body["mcpServers"]
    assert "ghost-ai" in body["mcpServers"]  # sibling preserved


# ---------------------------------------------------------------------
# VS Code — user-level mcp.json shared by every MCP-aware extension
# (Copilot Chat, Claude IDE, Codex IDE, Continue, …)
# ---------------------------------------------------------------------

def test_vscode_register_mcp_uses_servers_key_with_type_stdio(
    tmp_path, monkeypatch,
):
    """VS Code's schema chose `servers` (not `mcpServers`) and requires
    a per-entry `type` field — extensions reject entries without it."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    cfg = tmp_path / "Code" / "User" / "mcp.json"
    monkeypatch.setattr(pkg["vscode"], "config_path", cfg)

    result = pkg["vscode"].register_mcp_server(
        name="thread-keeper",
        command="/opt/python",
        args=["-m", "threadkeeper.server"],
        env={"PYTHONPATH": "/repo"},
    )
    assert "add" in result or "update" in result
    body = json.loads(cfg.read_text())
    assert "servers" in body  # not "mcpServers"
    entry = body["servers"]["thread-keeper"]
    assert entry["type"] == "stdio"
    assert entry["command"] == "/opt/python"
    assert entry["args"] == ["-m", "threadkeeper.server"]
    assert entry["env"] == {"PYTHONPATH": "/repo"}

    # Idempotency
    result2 = pkg["vscode"].register_mcp_server(
        name="thread-keeper",
        command="/opt/python",
        args=["-m", "threadkeeper.server"],
        env={"PYTHONPATH": "/repo"},
    )
    assert "already current" in result2


def test_vscode_preserves_inputs_and_sibling_servers(tmp_path, monkeypatch):
    """A real VS Code mcp.json typically carries `inputs` (for secret
    prompts) and other servers (Copilot's GitHub MCP, Sonar, …). Our
    write must merge in place."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({
        "inputs": [
            {"id": "Authorization", "type": "promptString", "password": True}
        ],
        "servers": {
            "sonarqube": {"command": "docker", "args": ["run"], "type": "stdio"}
        },
    }))
    monkeypatch.setattr(pkg["vscode"], "config_path", cfg)
    pkg["vscode"].register_mcp_server(
        name="thread-keeper",
        command="/opt/python",
        args=["-m", "threadkeeper.server"],
        env={},
    )
    body = json.loads(cfg.read_text())
    assert body["inputs"][0]["id"] == "Authorization"
    assert "sonarqube" in body["servers"]
    assert "thread-keeper" in body["servers"]


def test_vscode_no_transcripts_no_hooks(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    assert pkg["vscode"].transcript_files() == []
    assert list(pkg["vscode"].iter_messages(tmp_path / "x")) == []
    assert pkg["vscode"].hooks_supported() is False
    assert pkg["vscode"].instructions_path() is None


def test_vscode_unregister_removes_entry(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({
        "servers": {
            "thread-keeper": {"command": "/opt/python", "args": [], "type": "stdio"},
            "sonarqube": {"command": "docker", "args": ["run"], "type": "stdio"},
        }
    }))
    monkeypatch.setattr(pkg["vscode"], "config_path", cfg)
    msg = pkg["vscode"].unregister_mcp_server("thread-keeper")
    assert "removed" in msg
    body = json.loads(cfg.read_text())
    assert "thread-keeper" not in body["servers"]
    assert "sonarqube" in body["servers"]


# ---------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------

def test_codex_register_mcp_writes_toml(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(pkg["codex"], "config_path", cfg)
    result = pkg["codex"].register_mcp_server(
        name="thread-keeper",
        command="/opt/python",
        args=["-m", "threadkeeper.server"],
        env={"PYTHONPATH": "/repo"},
    )
    assert "config.toml" in result
    body = cfg.read_text()
    assert "[mcp_servers.thread-keeper]" in body
    assert '"/opt/python"' in body
    assert "[mcp_servers.thread-keeper.env]" in body
    assert '"/repo"' in body
    assert "[mcp_servers.thread-keeper.tools.dialectic_claim]" in body
    assert "[mcp_servers.thread-keeper.tools.dialectic_observation_resolve]" in body
    assert "[mcp_servers.thread-keeper.tools.accept_candidate]" in body
    assert 'approval_mode = "approve"' in body


def test_codex_spawn_argv_skips_git_repo_check(tmp_path, monkeypatch):
    """`codex exec` refuses to run outside a trusted git worktree unless
    --skip-git-repo-check is passed. The spawn cwd is inherited from the host
    server, so without the flag the autonomous loops fail intermittently."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    import threadkeeper.adapters.codex as codex_mod
    monkeypatch.setattr(codex_mod.shutil, "which", lambda _bin: "/usr/local/bin/codex")

    argv = pkg["codex"].spawn_argv("hi", model="gpt-5.5")
    assert argv is not None
    assert argv[:3] == ["/usr/local/bin/codex", "exec", "--skip-git-repo-check"]
    assert argv[-1] == "-"
    assert "-m" in argv and "gpt-5.5" in argv
    # Default (non-bypass) path still sandboxes.
    assert "--sandbox" in argv and "workspace-write" in argv

    # Flag is present on the bypass path too.
    argv_bypass = pkg["codex"].spawn_argv(
        "hi", permission_mode="bypassPermissions"
    )
    assert "--skip-git-repo-check" in argv_bypass
    assert "--dangerously-bypass-approvals-and-sandbox" in argv_bypass


def test_codex_spawn_argv_enables_native_search_for_curator(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    import threadkeeper.adapters.codex as codex_mod
    monkeypatch.setattr(
        codex_mod.shutil, "which", lambda _bin: "/usr/local/bin/codex",
    )

    argv = pkg["codex"].spawn_argv(
        "deep audit",
        extra_allowed_tools="Read,WebSearch,WebFetch",
    )

    assert argv is not None
    assert argv[:4] == [
        "/usr/local/bin/codex", "--search", "exec", "--skip-git-repo-check",
    ]


def test_codex_iter_messages_filters_developer_turns(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    fp = tmp_path / "rollout-2026-05-14T10-00-00.jsonl"
    fp.write_text("\n".join([
        json.dumps({"timestamp": "2026-05-14T10:00:00Z", "type": "session_meta",
                    "payload": {"id": "sess-x", "cwd": "/x"}}),
        json.dumps({"timestamp": "2026-05-14T10:00:01Z", "type": "response_item",
                    "payload": {"type": "message", "role": "developer", "id": "dev-1",
                                "content": [{"type": "input_text", "text": "internal"}]}}),
        json.dumps({"timestamp": "2026-05-14T10:00:02Z", "type": "response_item",
                    "payload": {"type": "message", "role": "user", "id": "u-1",
                                "content": [{"type": "input_text", "text": "hi"}]}}),
        json.dumps({"timestamp": "2026-05-14T10:00:03Z", "type": "response_item",
                    "payload": {"type": "message", "role": "assistant", "id": "a-1",
                                "model": "gpt-5", "content": [{"type": "output_text",
                                                                "text": "hello"}]}}),
    ]) + "\n")
    msgs = list(pkg["codex"].iter_messages(fp))
    assert [m.uuid for m in msgs] == ["u-1", "a-1"]
    assert msgs[0].session_id == "sess-x"
    assert msgs[1].model == "gpt-5"
    assert msgs[1].content == "hello"


def test_codex_fallback_uuid_uses_line_index_for_same_timestamp(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    fp = tmp_path / "rollout-2026-05-14T10-00-00.jsonl"
    ts = "2026-05-14T10:00:02Z"
    fp.write_text("\n".join([
        json.dumps({"timestamp": "2026-05-14T10:00:00Z", "type": "session_meta",
                    "payload": {"id": "sess-x", "cwd": "/x"}}),
        json.dumps({"timestamp": ts, "type": "response_item",
                    "payload": {"type": "message", "role": "user",
                                "content": [{"type": "input_text",
                                             "text": "first colliding turn"}]}}),
        json.dumps({"timestamp": ts, "type": "response_item",
                    "payload": {"type": "message", "role": "assistant",
                                "content": [{"type": "output_text",
                                             "text": "second colliding turn"}]}}),
    ]) + "\n")

    msgs = list(pkg["codex"].iter_messages(fp))

    assert [m.uuid for m in msgs] == [
        f"codex:{fp.name}:2:{ts}",
        f"codex:{fp.name}:3:{ts}",
    ]
    assert len({m.uuid for m in msgs}) == 2


def test_codex_ingest_keeps_timestamp_colliding_fallback_messages(
    fresh_mp, tmp_path
):
    from threadkeeper import ingest
    from threadkeeper.adapters.codex import ADAPTER

    ingest.SEMANTIC_AVAILABLE = False
    conn = fresh_mp["db"].get_db()
    rollout_dir = tmp_path / "2026" / "05" / "14"
    rollout_dir.mkdir(parents=True)
    fp = rollout_dir / "rollout-2026-05-14T10-00-00.jsonl"
    ts = "2026-05-14T10:00:02Z"
    fp.write_text("\n".join([
        json.dumps({"timestamp": "2026-05-14T10:00:00Z", "type": "session_meta",
                    "payload": {"id": "sess-x", "cwd": "/x"}}),
        json.dumps({"timestamp": ts, "type": "response_item",
                    "payload": {"type": "message", "role": "user",
                                "content": [{"type": "input_text",
                                             "text": "first colliding payload"}]}}),
        json.dumps({"timestamp": ts, "type": "response_item",
                    "payload": {"type": "message", "role": "assistant",
                                "content": [{"type": "output_text",
                                             "text": "second colliding payload"}]}}),
    ]) + "\n")

    added = ingest._ingest_file(conn, fp, max_msgs=100, adapter=ADAPTER)
    conn.commit()

    assert added == 2
    rows = conn.execute(
        "SELECT uuid, content FROM dialog_messages ORDER BY rowid"
    ).fetchall()
    assert [row["uuid"] for row in rows] == [
        f"codex:{fp.name}:2:{ts}",
        f"codex:{fp.name}:3:{ts}",
    ]
    assert [row["content"] for row in rows] == [
        "first colliding payload",
        "second colliding payload",
    ]


def test_codex_iter_messages_uses_forced_child_cid_from_spawn_preamble(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    fp = tmp_path / "rollout-2026-06-11T10-00-00.jsonl"
    forced_cid = "af389b3f-8e17-46b5-87f1-402769a74e58"
    fp.write_text("\n".join([
        json.dumps({
            "timestamp": "2026-06-11T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "019eb5d0-6753-7c31-bce6-b887761090c6", "cwd": "/x"},
        }),
        json.dumps({
            "timestamp": "2026-06-11T10:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "id": "u-agents",
                "content": [{"type": "input_text", "text": "# AGENTS.md instructions"}],
            },
        }),
        json.dumps({
            "timestamp": "2026-06-11T10:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "id": "u-spawn",
                "content": [{
                    "type": "input_text",
                    "text": (
                        "You were spawned in the background by parent conversation "
                        "8877cab4-1f45-4d05-9a1c-09c6ab28adf1. "
                        f"Your own cid is {forced_cid} (forced via --session-id "
                        "and THREADKEEPER_FORCE_CID env)."
                    ),
                }],
            },
        }),
        json.dumps({
            "timestamp": "2026-06-11T10:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "id": "a-1",
                "content": [{"type": "output_text", "text": "processed"}],
            },
        }),
    ]) + "\n")

    opened = 0
    original_open = Path.open

    def _counting_open(self, *args, **kwargs):
        nonlocal opened
        if self == fp:
            opened += 1
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _counting_open)
    msgs = list(pkg["codex"].iter_messages(fp))

    assert opened == 1
    assert [m.uuid for m in msgs] == ["u-agents", "u-spawn", "a-1"]
    assert {m.session_id for m in msgs} == {forced_cid}


# ---------------------------------------------------------------------
# Antigravity
# ---------------------------------------------------------------------

def test_antigravity_register_mcp_writes_mcp_config_json(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    cfg = tmp_path / "mcp_config.json"
    monkeypatch.setattr(pkg["antigravity"], "config_path", cfg)
    result = pkg["antigravity"].register_mcp_server(
        name="thread-keeper",
        command="/opt/python",
        args=["-m", "threadkeeper.server"],
        env={"PYTHONPATH": "/repo"},
    )
    body = json.loads(cfg.read_text())
    assert "add" in result
    assert body["mcpServers"]["thread-keeper"]["command"] == "/opt/python"
    assert body["mcpServers"]["thread-keeper"]["env"]["PYTHONPATH"] == "/repo"


def test_antigravity_register_mcp_accepts_empty_config_file(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    cfg = tmp_path / "mcp_config.json"
    cfg.write_text("")
    monkeypatch.setattr(pkg["antigravity"], "config_path", cfg)
    result = pkg["antigravity"].register_mcp_server(
        name="thread-keeper",
        command="/opt/python",
        args=["-m", "threadkeeper.server"],
        env={},
    )
    body = json.loads(cfg.read_text())
    assert "add" in result
    assert body["mcpServers"]["thread-keeper"]["args"] == [
        "-m", "threadkeeper.server",
    ]


# Gemini legacy
# ---------------------------------------------------------------------

def test_gemini_register_mcp_writes_settings_json(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    cfg = tmp_path / "settings.json"
    monkeypatch.setattr(pkg["gemini"], "config_path", cfg)
    result = pkg["gemini"].register_mcp_server(
        name="thread-keeper",
        command="/opt/python",
        args=["-m", "threadkeeper.server"],
        env={"PYTHONPATH": "/repo"},
    )
    body = json.loads(cfg.read_text())
    assert body["mcpServers"]["thread-keeper"]["command"] == "/opt/python"
    assert body["mcpServers"]["thread-keeper"]["env"]["PYTHONPATH"] == "/repo"


def test_gemini_iter_messages_normalizes_types(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    fp = tmp_path / "session-2026-05-14T10-00-id.jsonl"
    fp.write_text("\n".join([
        json.dumps({"sessionId": "g-sess", "projectHash": "abc",
                    "startTime": "2026-05-14T10:00:00Z"}),
        json.dumps({"id": "g-info", "timestamp": "2026-05-14T10:00:01Z",
                    "type": "info", "content": "warmup"}),  # skipped
        json.dumps({"id": "g-u", "timestamp": "2026-05-14T10:00:02Z",
                    "type": "user", "content": "hi gemini"}),
        json.dumps({"id": "g-a", "timestamp": "2026-05-14T10:00:03Z",
                    "type": "model", "content": "hi back"}),
    ]) + "\n")
    msgs = list(pkg["gemini"].iter_messages(fp))
    assert [m.uuid for m in msgs] == ["g-u", "g-a"]
    assert msgs[1].role == "assistant"  # "model" → "assistant"
    assert msgs[0].session_id == "g-sess"


# ---------------------------------------------------------------------
# Copilot
# ---------------------------------------------------------------------

def test_copilot_register_mcp_uses_mcpServers_key(tmp_path, monkeypatch):
    """Copilot v1.0.43+ requires `mcpServers` (not `servers`). Earlier
    bundles documented `servers`; the validator now rejects that."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    cfg = tmp_path / "mcp-config.json"
    monkeypatch.setattr(pkg["copilot"], "config_path", cfg)
    pkg["copilot"].register_mcp_server(
        name="thread-keeper",
        command="/opt/python",
        args=["-m", "threadkeeper.server"],
        env={"PYTHONPATH": "/repo"},
    )
    body = json.loads(cfg.read_text())
    assert "mcpServers" in body
    assert body["mcpServers"]["thread-keeper"]["command"] == "/opt/python"


def test_copilot_register_mcp_migrates_legacy_servers_key(tmp_path, monkeypatch):
    """Existing config with legacy `servers` key must be migrated to
    `mcpServers` (so Copilot's validator stops rejecting the file) and
    the legacy entries preserved."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    cfg = tmp_path / "mcp-config.json"
    cfg.write_text(json.dumps({
        "servers": {
            "ghost-ai": {"command": "ghost-ai", "args": []},
        }
    }))
    monkeypatch.setattr(pkg["copilot"], "config_path", cfg)
    result = pkg["copilot"].register_mcp_server(
        name="thread-keeper",
        command="/opt/python",
        args=["-m", "threadkeeper.server"],
        env={},
    )
    assert "migrated" in result
    body = json.loads(cfg.read_text())
    assert "servers" not in body  # legacy key removed
    assert "mcpServers" in body
    assert "ghost-ai" in body["mcpServers"]
    assert "thread-keeper" in body["mcpServers"]


# ---------------------------------------------------------------------
# Hooks: shared Claude-style format used by claude-code, gemini legacy, copilot
# ---------------------------------------------------------------------

def _specs():
    return [
        {"event": "SessionStart", "command": "/tk/brief.sh", "matcher": ""},
        {"event": "PostToolUse", "command": "/tk/status.sh",
         "matcher": "mcp__thread-keeper__.*"},
    ]


def test_claude_register_hooks_writes_settings_json(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    sp = tmp_path / "settings.json"
    monkeypatch.setattr(pkg["claude"], "_settings_path", sp)
    assert pkg["claude"].hooks_supported()
    result = pkg["claude"].register_hooks(_specs())
    assert "updated" in result
    body = json.loads(sp.read_text())
    assert body["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "/tk/brief.sh"
    assert body["hooks"]["PostToolUse"][0]["matcher"] == "mcp__thread-keeper__.*"
    # Idempotency
    assert "already current" in pkg["claude"].register_hooks(_specs())


def test_gemini_register_hooks_writes_settings_json(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    sp = tmp_path / "gemini-settings.json"
    monkeypatch.setattr(pkg["gemini"], "config_path", sp)
    assert pkg["gemini"].hooks_supported()
    pkg["gemini"].register_hooks(_specs())
    body = json.loads(sp.read_text())
    assert "SessionStart" in body["hooks"]


def test_copilot_register_hooks_writes_hooks_json(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    hp = tmp_path / "hooks.json"
    monkeypatch.setattr(pkg["copilot"], "_hooks_path", hp)
    assert pkg["copilot"].hooks_supported()
    pkg["copilot"].register_hooks(_specs())
    body = json.loads(hp.read_text())
    # Copilot expects hooks under a top-level "hooks" key in this file
    # (same as Claude's settings.json shape).
    assert "hooks" in body
    assert "SessionStart" in body["hooks"]


def test_codex_hooks_not_supported(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    assert pkg["codex"].hooks_supported() is False
    result = pkg["codex"].register_hooks(_specs())
    assert "unsupported" in result


def test_register_hooks_preserves_existing_user_blocks(tmp_path, monkeypatch):
    """A pre-existing user-defined hook in the same event must NOT be
    clobbered — adapter appends our entry alongside."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    sp = tmp_path / "settings.json"
    sp.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "/user/own.sh"}]}
            ]
        }
    }))
    monkeypatch.setattr(pkg["claude"], "_settings_path", sp)
    pkg["claude"].register_hooks([
        {"event": "SessionStart", "command": "/tk/brief.sh", "matcher": ""},
    ])
    body = json.loads(sp.read_text())
    cmds = [b["hooks"][0]["command"] for b in body["hooks"]["SessionStart"]]
    assert "/user/own.sh" in cmds
    assert "/tk/brief.sh" in cmds


def test_copilot_iter_messages_splits_turns(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    # Build a minimal session-store.db with one session and two turns
    db_path = tmp_path / "session-store.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, host_type TEXT,
            branch TEXT, summary TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            turn_index INTEGER NOT NULL,
            user_message TEXT,
            assistant_response TEXT,
            timestamp TEXT
        );
    """)
    conn.execute("INSERT INTO sessions(id,cwd) VALUES (?,?)", ("s1", "/x"))
    conn.execute(
        "INSERT INTO turns(session_id,turn_index,user_message,"
        "assistant_response,timestamp) VALUES (?,?,?,?,?)",
        ("s1", 0, "hello", "hi there", "2026-05-14 10:00:00"),
    )
    conn.execute(
        "INSERT INTO turns(session_id,turn_index,user_message,"
        "assistant_response,timestamp) VALUES (?,?,?,?,?)",
        ("s1", 1, "do X", "done", "2026-05-14 10:00:01"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(pkg["copilot"], "session_db", db_path)

    msgs = list(pkg["copilot"].iter_messages(db_path))
    # 2 turns × 2 (user + assistant) = 4 normalized messages
    assert len(msgs) == 4
    assert msgs[0].role == "user" and msgs[0].content == "hello"
    assert msgs[1].role == "assistant" and msgs[1].content == "hi there"
    assert msgs[2].role == "user" and msgs[2].content == "do X"
    assert msgs[3].role == "assistant" and msgs[3].content == "done"
