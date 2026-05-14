"""skill_manage, skill_record, curator_run, review_thread — Learning loop
machinery for ~/.claude/skills/.

Tests use a sandboxed CLAUDE_SKILLS_DIR (under tmp_path) so writes never
touch the real ~/.claude/skills/.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


_FAKE_CID = "cccccccc-dddd-eeee-ffff-000011112222"


@pytest.fixture()
def skills_pkg(tmp_path, monkeypatch):
    """Like mp_with_cid but also points CLAUDE_SKILLS_DIR at a sandbox AND
    pins WRITE_ORIGIN='foreground' for predictable provenance assertions."""
    import importlib
    import sys

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
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)

    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]

    import threadkeeper.server  # noqa: F401
    from threadkeeper import _mcp, identity, db

    return {
        "mcp": _mcp.mcp,
        "identity": identity,
        "db": db,
        "skills_root": skills_root,
        "tmp": tmp_path,
    }


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


# ──────────────────────────────────────────────────────────────────────
# Validator + create
# ──────────────────────────────────────────────────────────────────────

def test_create_writes_valid_skill(skills_pkg):
    sm = _tool(skills_pkg, "skill_manage")
    result = sm(
        action="create",
        name="my-test-skill",
        description="Use when testing the skill_manage tool.",
        content="# Test\n\nBody goes here.",
    )
    assert result.startswith("ok path=")
    md = skills_pkg["skills_root"] / "my-test-skill" / "SKILL.md"
    assert md.exists()
    body = md.read_text()
    assert body.startswith("---")
    assert "name: my-test-skill" in body
    assert "description: Use when testing" in body


def test_create_rejects_invalid_name(skills_pkg):
    sm = _tool(skills_pkg, "skill_manage")
    result = sm(action="create", name="BAD_NAME", description="x", content="y")
    assert result.startswith("ERR")
    assert "invalid name" in result.lower()


def test_create_rejects_oversized_description(skills_pkg):
    sm = _tool(skills_pkg, "skill_manage")
    huge_desc = "x" * 2000
    result = sm(action="create", name="foo", description=huge_desc, content="body")
    assert result.startswith("ERR")
    assert "description exceeds" in result


def test_create_rejects_duplicate(skills_pkg):
    sm = _tool(skills_pkg, "skill_manage")
    sm(action="create", name="dup", description="first", content="body")
    second = sm(action="create", name="dup", description="second", content="body")
    assert second == "ERR skill_exists=dup"


def test_create_accepts_full_content_with_own_frontmatter(skills_pkg):
    sm = _tool(skills_pkg, "skill_manage")
    body = (
        "---\nname: bring-your-own\n"
        "description: Use when shipping a custom frontmatter.\n---\n\n"
        "# Body\n"
    )
    result = sm(action="create", name="bring-your-own", content=body)
    assert result.startswith("ok"), result


def test_create_rejects_mismatched_frontmatter_name(skills_pkg):
    sm = _tool(skills_pkg, "skill_manage")
    body = (
        "---\nname: wrong-name\n"
        "description: Use when the frontmatter lies about its name.\n---\n\n"
        "# Body\n"
    )
    result = sm(action="create", name="actual-name", content=body)
    assert result.startswith("ERR")
    assert "does not match directory name" in result


# ──────────────────────────────────────────────────────────────────────
# Patch / edit / delete
# ──────────────────────────────────────────────────────────────────────

def _seed_skill(pkg, name="seed-skill"):
    _tool(pkg, "skill_manage")(
        action="create",
        name=name,
        description=f"Use when seeding {name} for a test.",
        content="# Seed\n\nOriginal body.\n",
    )
    return pkg["skills_root"] / name / "SKILL.md"


def test_patch_find_and_replace(skills_pkg):
    md = _seed_skill(skills_pkg)
    sm = _tool(skills_pkg, "skill_manage")
    result = sm(
        action="patch",
        name="seed-skill",
        old_string="Original body.",
        new_string="Updated body.",
    )
    assert result == "ok"
    assert "Updated body." in md.read_text()


def test_patch_refuses_ambiguous_old_string(skills_pkg):
    md = _seed_skill(skills_pkg)
    sm = _tool(skills_pkg, "skill_manage")
    # write a body with duplicate sentinel
    sm(action="edit", name="seed-skill", content=md.read_text().replace(
        "Original body.", "dupe line\ndupe line\n"
    ))
    result = sm(
        action="patch",
        name="seed-skill",
        old_string="dupe line",
        new_string="changed",
    )
    assert result.startswith("ERR")
    assert "ambiguous" in result


def test_patch_validates_after_replacement(skills_pkg):
    _seed_skill(skills_pkg)
    sm = _tool(skills_pkg, "skill_manage")
    # remove the closing frontmatter --- to make the patched result invalid
    result = sm(
        action="patch",
        name="seed-skill",
        old_string="---\n\n# Seed",
        new_string="(no closing fence)\n\n# Seed",
    )
    assert result.startswith("ERR")
    assert "validate_failed_after_patch" in result


def test_delete_removes_skill_dir_and_usage_row(skills_pkg):
    _seed_skill(skills_pkg, name="goner")
    sm = _tool(skills_pkg, "skill_manage")
    sdir = skills_pkg["skills_root"] / "goner"
    assert sdir.exists()
    assert sm(action="delete", name="goner") == "ok"
    assert not sdir.exists()
    conn = skills_pkg["db"].get_db()
    assert conn.execute(
        "SELECT 1 FROM skill_usage WHERE name='goner'"
    ).fetchone() is None


def test_delete_refuses_pinned(skills_pkg):
    _seed_skill(skills_pkg, name="pinned-one")
    conn = skills_pkg["db"].get_db()
    conn.execute(
        "UPDATE skill_usage SET pinned=1 WHERE name='pinned-one'"
    )
    conn.commit()
    sm = _tool(skills_pkg, "skill_manage")
    result = sm(action="delete", name="pinned-one")
    assert result.startswith("ERR pinned=")


# ──────────────────────────────────────────────────────────────────────
# write_file
# ──────────────────────────────────────────────────────────────────────

def test_write_file_under_references(skills_pkg):
    _seed_skill(skills_pkg)
    sm = _tool(skills_pkg, "skill_manage")
    result = sm(
        action="write_file",
        name="seed-skill",
        sub_path="references/extra.md",
        content="# Reference detail",
    )
    assert result.startswith("ok")
    assert (skills_pkg["skills_root"] / "seed-skill" / "references" /
            "extra.md").exists()


def test_write_file_rejects_disallowed_subdir(skills_pkg):
    _seed_skill(skills_pkg)
    sm = _tool(skills_pkg, "skill_manage")
    result = sm(
        action="write_file",
        name="seed-skill",
        sub_path="evil/payload.sh",
        content="rm -rf /",
    )
    assert result.startswith("ERR")
    assert "subdir_not_allowed" in result


def test_write_file_blocks_traversal(skills_pkg):
    _seed_skill(skills_pkg)
    sm = _tool(skills_pkg, "skill_manage")
    result = sm(
        action="write_file",
        name="seed-skill",
        sub_path="references/../../../../etc/passwd",
        content="x",
    )
    assert result.startswith("ERR")
    assert "path_traversal" in result


# ──────────────────────────────────────────────────────────────────────
# skill_record + skill_list + provenance
# ──────────────────────────────────────────────────────────────────────

def test_create_records_provenance_from_env(skills_pkg):
    _seed_skill(skills_pkg, name="prov-test")
    conn = skills_pkg["db"].get_db()
    row = conn.execute(
        "SELECT created_by_origin FROM skill_usage WHERE name='prov-test'"
    ).fetchone()
    assert row["created_by_origin"] == "foreground"


def test_skill_record_bumps_counters(skills_pkg):
    _seed_skill(skills_pkg, name="counter-test")
    rec = _tool(skills_pkg, "skill_record")
    assert rec(name="counter-test", kind="use") == "ok"
    assert rec(name="counter-test", kind="view") == "ok"
    assert rec(name="counter-test", kind="view") == "ok"
    conn = skills_pkg["db"].get_db()
    r = conn.execute(
        "SELECT use_count, view_count, last_used_at, last_viewed_at "
        "FROM skill_usage WHERE name='counter-test'"
    ).fetchone()
    assert r["use_count"] == 1
    assert r["view_count"] == 2
    assert r["last_used_at"] is not None
    assert r["last_viewed_at"] is not None


def test_skill_list_omits_archived_by_default(skills_pkg):
    _seed_skill(skills_pkg, name="alive")
    _seed_skill(skills_pkg, name="ghost")
    conn = skills_pkg["db"].get_db()
    conn.execute("UPDATE skill_usage SET state='archived' WHERE name='ghost'")
    conn.commit()
    sl = _tool(skills_pkg, "skill_list")
    txt = sl()
    assert "alive" in txt
    assert "ghost" not in txt
    txt_all = sl(include_archived=True)
    assert "ghost" in txt_all


# ──────────────────────────────────────────────────────────────────────
# curator
# ──────────────────────────────────────────────────────────────────────

def _seed_agent_created_skill(pkg, name, created_at_offset_days=0):
    """Seed an agent-created skill by overriding provenance after create."""
    _seed_skill(pkg, name=name)
    conn = pkg["db"].get_db()
    now = int(time.time())
    activity = now - created_at_offset_days * 86400
    conn.execute(
        "UPDATE skill_usage SET created_by_origin='background_review', "
        "created_at=?, last_used_at=?, last_viewed_at=?, last_patched_at=? "
        "WHERE name=?",
        (activity, activity, activity, activity, name),
    )
    conn.commit()


def test_curator_skips_foreground(skills_pkg):
    _seed_skill(skills_pkg, name="user-wrote")
    conn = skills_pkg["db"].get_db()
    # age it to ancient
    conn.execute(
        "UPDATE skill_usage SET created_at=?, last_used_at=? WHERE name=?",
        (1, 1, "user-wrote"),
    )
    conn.commit()
    cur = _tool(skills_pkg, "curator_run")
    assert cur(dry_run=False) == "nothing_to_do"


def test_curator_marks_stale_and_archives(skills_pkg):
    # 40 days idle → active → stale (default stale_after=30)
    _seed_agent_created_skill(skills_pkg, "fresh-agent", created_at_offset_days=5)
    _seed_agent_created_skill(skills_pkg, "stale-agent", created_at_offset_days=40)
    # 100 days idle but already stale → archived (default archive_after=90)
    _seed_agent_created_skill(skills_pkg, "old-stale", created_at_offset_days=100)
    conn = skills_pkg["db"].get_db()
    conn.execute("UPDATE skill_usage SET state='stale' WHERE name='old-stale'")
    conn.commit()

    cur = _tool(skills_pkg, "curator_run")
    plan = cur(dry_run=True)
    assert "stale-agent: active → stale" in plan
    assert "old-stale: stale → archived" in plan
    # fresh-agent should not appear
    assert "fresh-agent" not in plan

    apply = cur(dry_run=False)
    assert apply.startswith("plan")
    # On apply, old-stale directory moved into .archive/
    arch_dst = skills_pkg["skills_root"] / ".archive" / "old-stale"
    assert arch_dst.exists()
    # stale-agent stays in place but state=stale
    assert (skills_pkg["skills_root"] / "stale-agent").exists()
    row = conn.execute(
        "SELECT state FROM skill_usage WHERE name='stale-agent'"
    ).fetchone()
    assert row["state"] == "stale"


def test_curator_skips_pinned(skills_pkg):
    _seed_agent_created_skill(skills_pkg, "pinned-agent",
                              created_at_offset_days=100)
    conn = skills_pkg["db"].get_db()
    conn.execute("UPDATE skill_usage SET pinned=1 WHERE name='pinned-agent'")
    conn.commit()
    cur = _tool(skills_pkg, "curator_run")
    assert cur(dry_run=True) == "nothing_to_do"


# ──────────────────────────────────────────────────────────────────────
# review_thread (inline mode only — auto mode would spawn real claude)
# ──────────────────────────────────────────────────────────────────────

def test_review_thread_inline_returns_prompt_with_notes(skills_pkg):
    # Seed a closed thread with a couple of notes.
    open_t = _tool(skills_pkg, "open_thread")
    note = _tool(skills_pkg, "note")
    close = _tool(skills_pkg, "close_thread")
    tid = open_t(question="something useful happened")
    note(thread_id=tid, content="we learned X", kind="insight")
    note(thread_id=tid, content="and we tried Y", kind="move")
    close(thread_id=tid, outcome="captured X and Y")

    rev = _tool(skills_pkg, "review_thread")
    out = rev(thread_id=tid, focus="skills", mode="inline")
    assert "reviewing closed thread" in out.lower()
    assert "we learned X" in out
    assert "we tried Y" in out
    assert "skill_manage" in out
    assert "Do NOT capture" in out
    assert "mark_skill_materialized" in out


def test_review_thread_auto_bumps_learning_loop_counter(skills_pkg, monkeypatch):
    """Auto-mode spawns a learning child — record that as a use of the
    ai-memory-learning-loop skill so the counter reflects how many times
    the hermes-style loop actually fired.

    Pin AUTO_REVIEW_ENABLED=False so close_thread doesn't ALSO trigger an
    auto-review (which would double the count). We're testing the bump
    logic inside review_thread itself, not the close_thread → review path.
    """
    monkeypatch.setenv("THREADKEEPER_AUTO_REVIEW", "")
    import threadkeeper.config as cfg
    monkeypatch.setattr(cfg, "AUTO_REVIEW_ENABLED", False)

    import threadkeeper.tools.spawn as spawn_mod

    captured: list[dict] = []

    def fake_spawn(**kwargs):
        captured.append(kwargs)
        return "spawn task_id=fake-task pid=0"

    monkeypatch.setattr(spawn_mod, "spawn", fake_spawn)

    open_t = _tool(skills_pkg, "open_thread")
    note = _tool(skills_pkg, "note")
    close = _tool(skills_pkg, "close_thread")
    tid = open_t(question="rich thread for auto-review")
    for i in range(3):
        note(thread_id=tid, content=f"insight number {i}", kind="insight")
        note(thread_id=tid, content=f"move number {i}", kind="move")
    close(thread_id=tid, outcome="ready for materialization")

    rev = _tool(skills_pkg, "review_thread")
    result = rev(thread_id=tid, focus="skills", mode="auto")
    assert "fake-task" in result  # confirms fake_spawn was called

    conn = skills_pkg["db"].get_db()
    row = conn.execute(
        "SELECT use_count, created_by_origin "
        "FROM skill_usage WHERE name='ai-memory-learning-loop'"
    ).fetchone()
    assert row is not None, "auto-mode spawn must bump learning-loop counter"
    assert row["use_count"] == 1
    assert row["created_by_origin"] == "background_review"

    # Second auto-review should increment again
    tid2 = open_t(question="second rich thread")
    for i in range(3):
        note(thread_id=tid2, content=f"another insight {i}", kind="insight")
        note(thread_id=tid2, content=f"another move {i}", kind="move")
    close(thread_id=tid2, outcome="second one ready")
    rev(thread_id=tid2, focus="skills", mode="auto")
    row = conn.execute(
        "SELECT use_count FROM skill_usage WHERE name='ai-memory-learning-loop'"
    ).fetchone()
    assert row["use_count"] == 2


def test_review_thread_rejects_unknown_thread(skills_pkg):
    rev = _tool(skills_pkg, "review_thread")
    result = rev(thread_id="T_nope", mode="inline")
    assert result.startswith("ERR thread_not_found")


def test_review_thread_rejects_bad_focus(skills_pkg):
    open_t = _tool(skills_pkg, "open_thread")
    tid = open_t(question="x")
    rev = _tool(skills_pkg, "review_thread")
    assert rev(thread_id=tid, focus="garbage",
               mode="inline").startswith("ERR invalid_focus")
