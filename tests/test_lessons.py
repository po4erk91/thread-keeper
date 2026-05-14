"""CLI-agnostic lessons store at ~/.threadkeeper/lessons.md.

Backs the materialization side of the learning loop for non-Claude
CLIs (Codex, Gemini, Copilot) — see issue #7.
"""
from __future__ import annotations

import re
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
        "THREADKEEPER_LESSONS": str(tmp_path / "lessons.md"),
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import lessons
    from threadkeeper._mcp import mcp
    return {
        "lessons": lessons,
        "path": Path(env["THREADKEEPER_LESSONS"]),
        "mcp": mcp,
    }


# ─────────────────────────────────────────────────────────────────────
# Core API
# ─────────────────────────────────────────────────────────────────────

def test_append_creates_file_with_header(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    assert not pkg["path"].exists()
    slug = pkg["lessons"].append_lesson(
        title="Always paginate", body="Use offset+limit, never slice locally.",
        summary="DB queries — paginate at the source.", source="Tabc",
    )
    assert slug == "always-paginate"
    body = pkg["path"].read_text()
    assert "# thread-keeper lessons" in body
    assert "## always-paginate" in body
    assert "Use offset+limit" in body
    assert "source=Tabc" in body


def test_slugify_handles_messy_titles(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    slug = pkg["lessons"].append_lesson(
        title="Don't trim TRAILING whitespace!!! (it's load-bearing)",
        body="some body",
    )
    # All non-alnum collapsed to single hyphens, lowercased
    assert re.fullmatch(r"[a-z0-9-]+", slug)
    assert "trailing" in slug
    assert "whitespace" in slug


def test_append_with_same_slug_replaces_in_place(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    pkg["lessons"].append_lesson(title="x", body="original body")
    pkg["lessons"].append_lesson(title="x", body="updated body")
    body = pkg["path"].read_text()
    assert body.count("<!-- LESSON:BEGIN slug=x ") == 1
    assert "original body" not in body
    assert "updated body" in body


def test_iter_lessons_returns_in_file_order(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    pkg["lessons"].append_lesson(title="first", body="b1", source="T1")
    pkg["lessons"].append_lesson(title="second", body="b2", source="T2")
    pkg["lessons"].append_lesson(title="third", body="b3", source="shadow")
    items = list(pkg["lessons"].iter_lessons())
    assert [i["slug"] for i in items] == ["first", "second", "third"]
    assert items[2]["source"] == "shadow"


def test_count_zero_when_no_file(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    assert pkg["lessons"].count_lessons() == 0


# ─────────────────────────────────────────────────────────────────────
# MCP tools
# ─────────────────────────────────────────────────────────────────────

def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def test_mcp_lesson_append_validates_inputs(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    assert la(title="", body="x").startswith("ERR empty_title")
    assert la(title="x", body="").startswith("ERR empty_body")
    out = la(title="ok one", body="content")
    assert out.startswith("ok slug=ok-one")


def test_mcp_lesson_list_returns_summary(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    ll = _tool(pkg, "lesson_list")
    assert ll() == "no_lessons"
    la(title="pagination", body="use cursor not offset", source="T1")
    la(title="error-handling", body="wrap external calls", source="T2")
    out = ll()
    assert "total=2" in out
    assert "pagination" in out
    assert "error-handling" in out


def test_mcp_lesson_get_returns_body(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    lg = _tool(pkg, "lesson_get")
    la(title="retry-strategy", body="exponential backoff with jitter")
    out = lg(slug="retry-strategy")
    assert "exponential backoff" in out
    assert lg(slug="does-not-exist").startswith("ERR not_found")
