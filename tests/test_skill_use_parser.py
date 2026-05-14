"""Skill tool_use parser in ingest.py — catches /<skill> activations buried
in assistant turns of the Claude Code jsonl transcript stream.

Two layers:
  1. _scan_message_for_skill_use(msg) — pure function. Walks a message dict
     and returns names of Skill tool_use invocations.
  2. _ingest_file — after writing a dialog_messages row for an assistant
     turn, bumps skill_usage.last_used_at + use_count for each Skill call.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest


_FAKE_CID = "bbbb2222-3333-4444-5555-666677778888"


def _bootstrap(tmp_path, monkeypatch):
    skills_root = tmp_path / "claude_skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_INGEST_CAP": "0",
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
        "THREADKEEPER_CLIENT": "pytest",
        "THREADKEEPER_FORCE_CID": _FAKE_CID,
        "CLAUDE_SKILLS_DIR": str(skills_root),
        "THREADKEEPER_WRITE_ORIGIN": "foreground",
        "THREADKEEPER_SKILL_WATCH_INTERVAL_S": "0",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import db, ingest
    return {
        "db": db,
        "ingest": ingest,
        "projects": Path(env["CLAUDE_PROJECTS_DIR"]),
    }


# ──────────────────────────────────────────────────────────────────────
# Pure-function tests for _scan_message_for_skill_use
# ──────────────────────────────────────────────────────────────────────

def test_scan_empty_content_returns_no_skills(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    scan = pkg["ingest"]._scan_message_for_skill_use
    assert scan({"role": "assistant", "content": ""}) == []
    assert scan({"role": "assistant", "content": []}) == []
    assert scan({}) == []


def test_scan_finds_skill_tool_use_with_skill_key(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    scan = pkg["ingest"]._scan_message_for_skill_use
    msg = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "let me try a skill"},
            {"type": "tool_use", "name": "Skill",
             "input": {"skill": "swift-ios"}},
        ],
    }
    assert scan(msg) == ["swift-ios"]


def test_scan_finds_skill_with_name_key_fallback(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    scan = pkg["ingest"]._scan_message_for_skill_use
    msg = {
        "content": [
            {"type": "tool_use", "name": "Skill",
             "input": {"name": "kotlin-android"}},
        ]
    }
    assert scan(msg) == ["kotlin-android"]


def test_scan_handles_nested_content_arrays(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    scan = pkg["ingest"]._scan_message_for_skill_use
    # Simulate an envelope where 'message.content' contains the tool_use.
    msg = {
        "message": {
            "content": [
                {"type": "tool_use", "name": "Skill",
                 "input": {"skill": "neon-postgres"}},
            ]
        }
    }
    assert scan(msg) == ["neon-postgres"]


def test_scan_returns_multiple_skill_invocations(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    scan = pkg["ingest"]._scan_message_for_skill_use
    msg = {
        "content": [
            {"type": "tool_use", "name": "Skill",
             "input": {"skill": "first"}},
            {"type": "text", "text": "in between"},
            {"type": "tool_use", "name": "Skill",
             "input": {"skill": "second"}},
        ]
    }
    assert scan(msg) == ["first", "second"]


def test_scan_ignores_non_skill_tool_use(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    scan = pkg["ingest"]._scan_message_for_skill_use
    msg = {
        "content": [
            {"type": "tool_use", "name": "Read",
             "input": {"file_path": "/tmp/x"}},
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "ls"}},
        ]
    }
    assert scan(msg) == []


def test_scan_ignores_tool_use_with_missing_input(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    scan = pkg["ingest"]._scan_message_for_skill_use
    # tool_use Skill but no resolvable name → not counted.
    msg = {
        "content": [
            {"type": "tool_use", "name": "Skill", "input": {}},
            {"type": "tool_use", "name": "Skill"},
        ]
    }
    assert scan(msg) == []


# ──────────────────────────────────────────────────────────────────────
# Integration: _ingest_file bumps skill_usage when assistant turn calls Skill
# ──────────────────────────────────────────────────────────────────────

def _write_jsonl(projects: Path, lines: list[dict]) -> Path:
    sess_dir = projects / "fake-session"
    sess_dir.mkdir(parents=True, exist_ok=True)
    fp = sess_dir / "session.jsonl"
    with fp.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return fp


def test_ingest_file_bumps_skill_usage(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    fp = _write_jsonl(pkg["projects"], [
        {
            "uuid": "msg-1",
            "type": "assistant",
            "timestamp": "2026-05-13T10:00:00Z",
            "sessionId": "sess-x",
            "message": {
                "role": "assistant",
                "model": "claude-opus",
                "content": [
                    {"type": "text", "text": "I'll invoke a skill now."},
                    {"type": "tool_use", "name": "Skill",
                     "input": {"skill": "swift-ios"}},
                ],
            },
        },
    ])
    added = pkg["ingest"]._ingest_file(conn, fp, max_msgs=100)
    conn.commit()
    assert added == 1
    row = conn.execute(
        "SELECT use_count, last_used_at, created_by_origin "
        "FROM skill_usage WHERE name='swift-ios'"
    ).fetchone()
    assert row is not None
    assert row["use_count"] == 1
    assert row["last_used_at"] is not None
    assert row["created_by_origin"] == "foreground"


def test_ingest_file_bumps_skill_for_tool_only_message(tmp_path, monkeypatch):
    """REGRESSION: assistant turn that contains ONLY a tool_use(Skill) block
    (no `text` and no `thinking` content) was previously dropped by the
    `len(text) < 10` early-return in _ingest_file, so skill_usage stayed
    at zero. This is the most common shape — Claude Code emits a single
    Skill tool_use without prose when invoking a skill from a slash command.
    """
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    fp = _write_jsonl(pkg["projects"], [
        {
            "uuid": "msg-tool-only",
            "type": "assistant",
            "timestamp": "2026-05-13T10:00:00Z",
            "sessionId": "sess-t",
            "message": {
                "role": "assistant",
                "model": "claude-opus",
                "content": [
                    {"type": "tool_use", "name": "Skill",
                     "input": {"skill": "anthropic-skills:pdf"}},
                ],
            },
        },
    ])
    pkg["ingest"]._ingest_file(conn, fp, max_msgs=100)
    conn.commit()
    row = conn.execute(
        "SELECT use_count, last_used_at, created_by_origin "
        "FROM skill_usage WHERE name='anthropic-skills:pdf'"
    ).fetchone()
    assert row is not None, (
        "tool-only assistant turn should still bump skill_usage"
    )
    assert row["use_count"] == 1
    assert row["last_used_at"] is not None
    assert row["created_by_origin"] == "foreground"


def test_backfill_skill_usage_from_jsonls(tmp_path, monkeypatch):
    """The one-shot backfill helper must catch tool-only Skill calls in
    historical jsonl files that already-ingested files skipped past via
    last_size seek. Idempotent: re-running on same data must not
    double-count thanks to the UPDATE-with-ts-guard."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    # Pretend the file was already ingested past — _ingest_file already
    # advanced last_size beyond these lines, so the normal path would
    # never see them again.
    fp = _write_jsonl(pkg["projects"], [
        {
            "uuid": "histo-1",
            "type": "assistant",
            "timestamp": "2026-05-13T09:00:00Z",
            "sessionId": "histo",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Skill",
                     "input": {"skill": "histo-skill-a"}},
                ],
            },
        },
        {
            "uuid": "histo-2",
            "type": "assistant",
            "timestamp": "2026-05-13T10:00:00Z",
            "sessionId": "histo",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Skill",
                     "input": {"skill": "histo-skill-a"}},
                ],
            },
        },
    ])
    # Mark ingest_state as if file was fully processed already.
    conn.execute(
        "INSERT INTO ingest_state (file_path, last_size, last_mtime, "
        "ingested_at, msg_count) VALUES (?, ?, ?, ?, ?)",
        (str(fp), fp.stat().st_size, int(fp.stat().st_mtime),
         int(time.time()), 2),
    )
    conn.commit()
    processed = pkg["ingest"]._backfill_skill_usage_from_jsonls(conn)
    assert processed == 2
    row = conn.execute(
        "SELECT use_count, last_used_at "
        "FROM skill_usage WHERE name='histo-skill-a'"
    ).fetchone()
    assert row is not None
    assert row["use_count"] == 2
    # Idempotency: re-run shouldn't bump again — same timestamps fail the
    # UPDATE guard.
    pkg["ingest"]._backfill_skill_usage_from_jsonls(conn)
    row = conn.execute(
        "SELECT use_count FROM skill_usage WHERE name='histo-skill-a'"
    ).fetchone()
    assert row["use_count"] == 2, "second backfill must not double-count"


def test_ingest_file_does_not_bump_for_user_turn(tmp_path, monkeypatch):
    """A 'user' role turn that happens to contain a tool_use Skill block
    (malformed transcript) must NOT be counted as a skill activation."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    fp = _write_jsonl(pkg["projects"], [
        {
            "uuid": "msg-u",
            "type": "user",
            "timestamp": "2026-05-13T10:00:00Z",
            "sessionId": "sess-y",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text",
                     "text": "this is just a long user message"},
                    {"type": "tool_use", "name": "Skill",
                     "input": {"skill": "should-not-count"}},
                ],
            },
        },
    ])
    pkg["ingest"]._ingest_file(conn, fp, max_msgs=100)
    conn.commit()
    assert conn.execute(
        "SELECT 1 FROM skill_usage WHERE name='should-not-count'"
    ).fetchone() is None


def test_ingest_file_increments_use_count_across_calls(tmp_path, monkeypatch):
    """Two assistant turns invoking the same skill in two separate files
    should both bump the counter (idempotent on uuid, additive on count)."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    fp1 = _write_jsonl(pkg["projects"], [
        {
            "uuid": "msg-a",
            "type": "assistant",
            "timestamp": "2026-05-13T10:00:00Z",
            "sessionId": "sess-z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "first invocation here"},
                    {"type": "tool_use", "name": "Skill",
                     "input": {"skill": "repeated"}},
                ],
            },
        },
    ])
    pkg["ingest"]._ingest_file(conn, fp1, max_msgs=100)
    conn.commit()

    # New file, later timestamp.
    sess_dir2 = pkg["projects"] / "fake-session-2"
    sess_dir2.mkdir(parents=True, exist_ok=True)
    fp2 = sess_dir2 / "session.jsonl"
    with fp2.open("w", encoding="utf-8") as f:
        f.write(json.dumps({
            "uuid": "msg-b",
            "type": "assistant",
            "timestamp": "2026-05-13T11:00:00Z",
            "sessionId": "sess-z2",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "second invocation here"},
                    {"type": "tool_use", "name": "Skill",
                     "input": {"skill": "repeated"}},
                ],
            },
        }) + "\n")
    pkg["ingest"]._ingest_file(conn, fp2, max_msgs=100)
    conn.commit()

    row = conn.execute(
        "SELECT use_count FROM skill_usage WHERE name='repeated'"
    ).fetchone()
    assert row["use_count"] == 2
