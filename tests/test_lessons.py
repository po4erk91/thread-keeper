"""CLI-agnostic lessons store at ~/.threadkeeper/lessons.md.

Backs the materialization side of the learning loop for clients that
still need a CLI-agnostic fallback (Gemini legacy, Copilot, bare MCP) —
see issue #7.
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
    from threadkeeper import db, lessons
    from threadkeeper._mcp import mcp
    return {
        "db": db,
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


def test_shadow_lesson_append_rejects_overlong_body(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    body = "word " * 451
    out = la(title="compact rules only", body=body, source="shadow")
    assert out.startswith("ERR shadow_lesson_too_long")
    assert "max=450" in out


def test_shadow_lesson_append_rejects_near_duplicate_slug(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    first = la(
        title="better auth jwks poisoning recovery",
        body="Delete poisoned JWKS via the stage shell.",
        source="shadow",
    )
    assert first.startswith("ok")
    second = la(
        title="better auth jwks poisoning diagnosis and recovery",
        body="Diagnose and delete poisoned JWKS via the stage shell.",
        source="shadow",
    )
    assert second.startswith("ERR likely_duplicate_lesson")
    assert "better-auth-jwks-poisoning-recovery" in second


def test_foreground_lesson_append_allows_near_duplicate_slug(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    la(
        title="better auth jwks poisoning recovery",
        body="Delete poisoned JWKS via the stage shell.",
        source="shadow",
    )
    out = la(
        title="better auth jwks poisoning diagnosis and recovery",
        body="User-authored foreground correction may be intentionally close.",
        source="foreground",
    )
    assert out.startswith(
        "ok slug=better-auth-jwks-poisoning-diagnosis-and-recovery"
    )


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


def test_mcp_lesson_reads_bump_usage_counters(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    ll = _tool(pkg, "lesson_list")
    lg = _tool(pkg, "lesson_get")
    la(title="pagination", body="use cursor not offset", source="T1")
    la(title="error-handling", body="wrap external calls", source="T2")

    ll(k=2)
    lg(slug="pagination")

    conn = pkg["db"].get_db()
    rows = {
        r["slug"]: dict(r)
        for r in conn.execute("SELECT * FROM lesson_usage").fetchall()
    }
    assert rows["pagination"]["view_count"] == 1
    assert rows["pagination"]["use_count"] == 1
    assert rows["pagination"]["last_viewed_at"] is not None
    assert rows["pagination"]["last_used_at"] is not None
    assert rows["error-handling"]["view_count"] == 1
    assert rows["error-handling"]["use_count"] == 0


def test_mcp_lesson_get_returns_body(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    lg = _tool(pkg, "lesson_get")
    la(title="retry-strategy", body="exponential backoff with jitter")
    out = lg(slug="retry-strategy")
    assert "exponential backoff" in out
    assert lg(slug="does-not-exist").startswith("ERR not_found")


def test_rank_stale_lessons_scores_and_skips_protected(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    now = 1_800_000_000
    old = now - 90 * 86400
    monkeypatch.setattr(pkg["lessons"].time, "time", lambda: old)
    for title in [
        "never pulled",
        "one old pull",
        "pinned lesson",
        "validated lesson",
    ]:
        pkg["lessons"].append_lesson(title=title, body=f"{title} body",
                                     source="shadow")
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO lesson_usage "
        "(slug, created_at, source, last_used_at, use_count) "
        "VALUES (?, ?, 'shadow', ?, 1)",
        ("one-old-pull", old, now - 70 * 86400),
    )
    conn.execute(
        "INSERT INTO lesson_usage "
        "(slug, created_at, source, last_viewed_at, view_count, pinned) "
        "VALUES (?, ?, 'shadow', ?, 1, 1)",
        ("pinned-lesson", old, now - 80 * 86400),
    )
    conn.execute(
        "INSERT INTO lesson_usage "
        "(slug, created_at, source, last_viewed_at, view_count, tier) "
        "VALUES (?, ?, 'shadow', ?, 1, 'validated')",
        ("validated-lesson", old, now - 80 * 86400),
    )
    conn.commit()

    ranked = pkg["lessons"].rank_stale_lessons(
        conn, now=now, stale_after_days=30, low_pull_count=1,
    )

    assert [r["slug"] for r in ranked] == ["never-pulled", "one-old-pull"]
    assert ranked[0]["decay_score"] < ranked[1]["decay_score"]
    assert all(not r["protected"] for r in ranked)


def test_mcp_lesson_remove_deletes_nonprotected_section(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    lr = _tool(pkg, "lesson_remove")
    lg = _tool(pkg, "lesson_get")
    la(title="stale duplicate", body="old duplicate rule", source="shadow")

    out = lr(slug="stale-duplicate")

    assert out == "ok removed=stale-duplicate"
    assert lg(slug="stale-duplicate").startswith("ERR not_found")
    assert "stale-duplicate" not in pkg["path"].read_text()
    conn = pkg["db"].get_db()
    n_usage = conn.execute(
        "SELECT COUNT(*) FROM lesson_usage WHERE slug='stale-duplicate'"
    ).fetchone()[0]
    assert n_usage == 0
    trash_root = tmp_path / "curator" / "trash"
    artifacts = list(trash_root.glob("*-lesson-stale-duplicate"))
    assert len(artifacts) == 1
    assert (artifacts[0] / "section.md").read_text().startswith(
        "<!-- LESSON:BEGIN slug=stale-duplicate "
    )


def test_mcp_lesson_restore_recreates_original_bytes(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    lr = _tool(pkg, "lesson_remove")
    restore = _tool(pkg, "lesson_restore")
    la(title="first lesson", body="first body", source="shadow")
    la(title="stale duplicate", body="old duplicate rule", source="shadow")
    conn = pkg["db"].get_db()
    conn.execute(
        "UPDATE lesson_usage SET use_count=3, view_count=4 "
        "WHERE slug='stale-duplicate'"
    )
    conn.commit()
    before = pkg["path"].read_bytes()

    assert lr(slug="stale-duplicate") == "ok removed=stale-duplicate"
    assert restore(slug="stale-duplicate").startswith(
        "ok restored=stale-duplicate"
    )

    assert pkg["path"].read_bytes() == before
    row = conn.execute(
        "SELECT use_count, view_count FROM lesson_usage "
        "WHERE slug='stale-duplicate'"
    ).fetchone()
    assert dict(row) == {"use_count": 3, "view_count": 4}


def test_mcp_lesson_remove_refuses_protected_without_force(
    tmp_path, monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    la = _tool(pkg, "lesson_append")
    lr = _tool(pkg, "lesson_remove")
    la(title="human policy", body="keep this", source="foreground")

    out = lr(slug="human-policy")

    assert out.startswith("ERR protected_lesson")
    assert "human-policy" in pkg["path"].read_text()
    assert not (tmp_path / "curator" / "trash").exists()
    assert lr(slug="human-policy", force=True) == "ok removed=human-policy"


def test_curator_trash_sweep_removes_expired_artifacts(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    from threadkeeper import trash

    monkeypatch.setattr(trash, "CURATOR_TRASH_TTL_DAYS", 1)
    artifact = tmp_path / "curator" / "trash" / "old-lesson"
    artifact.mkdir(parents=True)
    (artifact / "meta.json").write_text(
        '{"kind":"lesson","slug":"old","created_at":100}\n',
        encoding="utf-8",
    )

    assert trash.sweep_expired_trash(now=100 + 2 * 86400) == 1
    assert not artifact.exists()
