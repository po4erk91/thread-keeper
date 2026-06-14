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
        # Disable every background daemon. Without this the skill_watcher
        # daemon runs live and races delete tests: it scans CLAUDE_SKILLS_DIR
        # on a timer and re-INSERTs a skill_usage row right after a test's
        # delete removed dir+row, making test_delete_removes_skill_dir_and_
        # usage_row flake (~2 in 20). Same daemon-vs-test TOCTOU the conftest
        # _force_clean_env guards against; this bespoke fixture must mirror it.
        "THREADKEEPER_SKILL_WATCH_INTERVAL_S": "0",
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_MEMORY_GUARD_POLL_S": "0",
        "THREADKEEPER_SEARCH_PROXY_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_CURATOR_INTERVAL_S": "0",
        "THREADKEEPER_EXTRACT_INTERVAL_S": "0",
        "THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_PROBE_INTERVAL_S": "0",
        "THREADKEEPER_EVOLVE_REVIEW_INTERVAL_S": "0",
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
    assert 'name: "my-test-skill"' in body
    assert 'description: "Use when testing' in body


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


def test_create_rejects_invalid_yaml_frontmatter(skills_pkg):
    sm = _tool(skills_pkg, "skill_manage")
    body = (
        "---\nname: bad-yaml\n"
        "description: Use when descriptions contain colons: quote them.\n"
        "---\n\n# Body\n"
    )
    result = sm(action="create", name="bad-yaml", content=body)
    assert result.startswith("ERR")
    assert "frontmatter invalid YAML" in result


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
    the learning loop actually fired.

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


def test_review_thread_inline_injects_recently_active_skills(skills_pkg):
    """Active-update bias: the review fork should see a list of skills
    the parent has recently touched so it prefers PATCHing existing
    skills (Q4 of the rubric) over creating new ones that overlap."""
    import time as _t
    conn = skills_pkg["db"].get_db()
    now = int(_t.time())
    # Seed skill_usage with three skills of varying recency.
    conn.execute(
        "INSERT INTO skill_usage "
        "(name, created_at, created_by_origin, last_used_at, "
        " use_count, state) "
        "VALUES "
        "  ('ios-testing-recovery', ?, 'foreground', ?, 7, 'active'),"
        "  ('plaid-network-flake', ?, 'foreground', ?, 3, 'active'),"
        "  ('ancient-skill', ?, 'foreground', ?, 1, 'active')",
        (now - 86400, now - 3600,        # 1d old, used 1h ago
         now - 172800, now - 7200,       # 2d old, used 2h ago
         now - 90 * 86400, now - 60 * 86400),  # ancient
    )
    conn.commit()

    open_t = _tool(skills_pkg, "open_thread")
    note = _tool(skills_pkg, "note")
    close = _tool(skills_pkg, "close_thread")
    tid = open_t(question="rich thread to review")
    note(thread_id=tid, content="user said: always reset wifi proxy "
         "before WDA start", kind="insight")
    note(thread_id=tid, content="captured the recovery pattern",
         kind="move")
    close(thread_id=tid, outcome="ready")

    rev = _tool(skills_pkg, "review_thread")
    out = rev(thread_id=tid, focus="skills", mode="inline")

    # Section header is the literal marker — distinct from the in-prose
    # reference to "RECENTLY ACTIVE SKILLS block" inside the rubric text
    assert "RECENTLY ACTIVE SKILLS (prefer PATCH/extend over CREATE" in out
    assert "ios-testing-recovery" in out
    assert "plaid-network-flake" in out
    # Ancient skill (outside 14-day default window) should NOT appear
    assert "ancient-skill" not in out
    # Most recent is listed first
    ios_idx = out.find("ios-testing-recovery")
    plaid_idx = out.find("plaid-network-flake")
    assert 0 < ios_idx < plaid_idx
    # The rubric Q4 is reachable from the prompt
    assert "Q4." in out


def test_skill_record_outcome_emits_skill_outcome_event(skills_pkg):
    """skill_record(name, kind='use', outcome='wrong') writes an
    events.kind='skill_outcome' row so curator can spot false positives.
    Empty outcome stays silent (backwards compat)."""
    sr = _tool(skills_pkg, "skill_record")
    # No outcome → no event row
    assert sr(name="some-skill", kind="use") == "ok"
    conn = skills_pkg["db"].get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='skill_outcome'"
    ).fetchone()[0]
    assert n == 0
    # With outcome → exactly one event row, summary == outcome
    assert sr(name="some-skill", kind="use", outcome="wrong") == "ok"
    row = conn.execute(
        "SELECT target, summary FROM events WHERE kind='skill_outcome' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["target"] == "some-skill"
    assert row["summary"] == "wrong"
    # Invalid outcome → reject
    assert sr(name="some-skill", outcome="kinda").startswith("ERR invalid_outcome")


def test_skill_manage_mirrors_skill_across_detected_clis(
    skills_pkg, monkeypatch,
):
    """skill_manage(create) must write SKILL.md not only to the canonical
    primary skills root but also to every configured skills_dir() —
    so one materialization reaches every native skill consumer at once."""
    # Pin two mirror targets that we own (in tmp_path) so the test is
    # hermetic — don't touch the developer's ~/.codex/ or ~/.threadkeeper/.
    mirror_codex = skills_pkg["tmp"] / "fake_codex_skills"
    mirror_canonical = skills_pkg["tmp"] / "fake_canonical_skills"

    from threadkeeper.tools import skills as st

    def fake_mirror_targets(name):
        return [mirror_codex / name, mirror_canonical / name]

    monkeypatch.setattr(st, "_mirror_targets", fake_mirror_targets)

    sm = _tool(skills_pkg, "skill_manage")
    result = sm(
        action="create",
        name="mirrored-skill",
        description="Use when testing the mirror.",
        content="# Body",
    )
    assert result.startswith("ok path=")
    # Canonical landed
    canonical = skills_pkg["skills_root"] / "mirrored-skill" / "SKILL.md"
    assert canonical.exists()
    # Mirrors landed
    assert (mirror_codex / "mirrored-skill" / "SKILL.md").exists()
    assert (mirror_canonical / "mirrored-skill" / "SKILL.md").exists()
    # Mirror content identical to canonical
    assert (mirror_codex / "mirrored-skill" / "SKILL.md").read_text() == \
        canonical.read_text()


def test_mirror_targets_include_known_skill_roots_without_install_detection(
    skills_pkg, monkeypatch,
):
    """Mirror planning should include native skill roots even when an
    adapter's executable/config detection would be false. Skill roots are
    cheap paths; creation should not depend on whether the CLI has already
    written a config file."""
    from threadkeeper.tools import skills as st
    import threadkeeper.adapters as adapters

    class FakeAdapter:
        def skills_dir(self):
            return skills_pkg["tmp"] / "future_cli_skills"

    monkeypatch.setattr(adapters, "ADAPTERS", [FakeAdapter()])

    targets = st._mirror_targets("future-skill")
    assert skills_pkg["tmp"] / "future_cli_skills" / "future-skill" in targets


def test_mark_skill_materialized_mirrors_external_skill_dir(
    skills_pkg, monkeypatch,
):
    """If an agent created a skill directly in Codex/Claude and then calls
    mark_skill_materialized(skill_path=...), thread-keeper should copy the
    whole skill dir to canonical + mirrors."""
    mirror_codex = skills_pkg["tmp"] / "fake_codex_skills"
    mirror_agents = skills_pkg["tmp"] / "fake_agents_skills"

    from threadkeeper.tools import skills as st

    def fake_mirror_targets(name):
        return [mirror_codex / name, mirror_agents / name]

    monkeypatch.setattr(st, "_mirror_targets", fake_mirror_targets)

    external = skills_pkg["tmp"] / "external_codex" / "external-sync"
    (external / "agents").mkdir(parents=True)
    (external / "SKILL.md").write_text(
        "---\n"
        "name: external-sync\n"
        "description: Use when testing external skill sync.\n"
        "---\n\n"
        "# external-sync\n",
        encoding="utf-8",
    )
    (external / "agents" / "openai.yaml").write_text(
        "interface:\n"
        "  display_name: \"External Sync\"\n",
        encoding="utf-8",
    )

    open_t = _tool(skills_pkg, "open_thread")
    mark = _tool(skills_pkg, "mark_skill_materialized")
    tid = open_t(question="external skill sync")

    assert mark(thread_id=tid, skill_path=str(external / "SKILL.md")) == "ok"

    canonical = skills_pkg["skills_root"] / "external-sync"
    assert (canonical / "SKILL.md").exists()
    assert (canonical / "agents" / "openai.yaml").exists()
    assert (mirror_codex / "external-sync" / "SKILL.md").exists()
    assert (mirror_agents / "external-sync" / "agents" / "openai.yaml").exists()


def test_skill_manage_delete_removes_mirrors(skills_pkg, monkeypatch):
    """delete must propagate to every mirror target — otherwise stale
    SKILL.md files keep auto-triggering in Codex / canonical store."""
    mirror_dir = skills_pkg["tmp"] / "fake_codex_skills"
    from threadkeeper.tools import skills as st

    def fake_mirror_targets(name):
        return [mirror_dir / name]

    monkeypatch.setattr(st, "_mirror_targets", fake_mirror_targets)

    sm = _tool(skills_pkg, "skill_manage")
    sm(action="create", name="will-be-deleted",
       description="Use for the test.", content="# body")
    assert (mirror_dir / "will-be-deleted" / "SKILL.md").exists()
    assert sm(action="delete", name="will-be-deleted") == "ok"
    assert not (mirror_dir / "will-be-deleted").exists()


def test_review_thread_inline_skips_block_when_no_recent_skills(skills_pkg):
    """Fresh-install case: empty skill_usage → no RECENTLY ACTIVE
    SKILLS section in the prompt at all (don't show empty header)."""
    open_t = _tool(skills_pkg, "open_thread")
    note = _tool(skills_pkg, "note")
    close = _tool(skills_pkg, "close_thread")
    tid = open_t(question="thread for fresh-install case")
    note(thread_id=tid, content="some insight", kind="insight")
    close(thread_id=tid, outcome="done")

    rev = _tool(skills_pkg, "review_thread")
    out = rev(thread_id=tid, focus="skills", mode="inline")
    # Header block absent — only the rubric body reference remains
    assert "RECENTLY ACTIVE SKILLS (prefer PATCH/extend over CREATE" not in out


def test_review_thread_rejects_bad_focus(skills_pkg):
    open_t = _tool(skills_pkg, "open_thread")
    tid = open_t(question="x")
    rev = _tool(skills_pkg, "review_thread")
    assert rev(thread_id=tid, focus="garbage",
               mode="inline").startswith("ERR invalid_focus")
