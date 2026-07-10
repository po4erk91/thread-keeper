"""skill_watcher daemon — mtime-based detection of out-of-band SKILL.md edits.

Watches ~/.claude/skills/*/SKILL.md and bumps skill_usage.last_patched_at +
patch_count when the file mtime advances. Covers the case where Claude
patches a skill via the bare Edit/Write tool (or a user edits one in $EDITOR)
without going through skill_manage.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest


_FAKE_CID = "aaaa1111-2222-3333-4444-555566667777"


def _bootstrap(tmp_path, monkeypatch, *, interval_s: str = "10"):
    """Same import shape as test_skills.skills_pkg but lets us tweak the
    skill-watch interval (e.g. "0" to disable the daemon)."""
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
        "THREADKEEPER_SKILL_WATCH_INTERVAL_S": interval_s,
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import db, skill_watcher
    return {
        "db": db,
        "skill_watcher": skill_watcher,
        "skills_root": skills_root,
    }


def _write_skill(skills_root: Path, name: str, body: str = "# body\n") -> Path:
    sdir = skills_root / name
    sdir.mkdir(parents=True, exist_ok=True)
    md = sdir / "SKILL.md"
    md.write_text(
        f"---\nname: {name}\ndescription: Use when testing watcher.\n---\n\n"
        + body
    )
    return md


def test_scan_once_empty_skills_dir_returns_zero(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    assert pkg["skill_watcher"]._scan_once(conn) == 0


def test_scan_once_creates_row_for_new_skill(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    md = _write_skill(pkg["skills_root"], "watched-one")
    conn = pkg["db"].get_db()
    updates = pkg["skill_watcher"]._scan_once(conn)
    assert updates == 1
    row = conn.execute(
        "SELECT created_by_origin, last_patched_at, patch_count "
        "FROM skill_usage WHERE name='watched-one'"
    ).fetchone()
    assert row is not None
    assert row["created_by_origin"] == "foreground"
    assert row["last_patched_at"] == int(md.stat().st_mtime)
    assert row["patch_count"] == 1


def test_scan_once_bumps_when_mtime_advances(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    md = _write_skill(pkg["skills_root"], "bumpy")
    conn = pkg["db"].get_db()
    assert pkg["skill_watcher"]._scan_once(conn) == 1
    first = conn.execute(
        "SELECT last_patched_at, patch_count FROM skill_usage WHERE name='bumpy'"
    ).fetchone()

    # Advance mtime by 2 seconds.
    new_ts = int(md.stat().st_mtime) + 2
    os.utime(md, (new_ts, new_ts))
    updates = pkg["skill_watcher"]._scan_once(conn)
    assert updates == 1
    second = conn.execute(
        "SELECT last_patched_at, patch_count FROM skill_usage WHERE name='bumpy'"
    ).fetchone()
    assert second["last_patched_at"] == new_ts
    assert second["patch_count"] == first["patch_count"] + 1


def test_scan_once_noop_when_mtime_unchanged(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    _write_skill(pkg["skills_root"], "stable")
    conn = pkg["db"].get_db()
    assert pkg["skill_watcher"]._scan_once(conn) == 1
    # Second tick with no fs change should find nothing to update.
    assert pkg["skill_watcher"]._scan_once(conn) == 0


def test_scan_once_skips_archive_dir(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    archive = pkg["skills_root"] / ".archive" / "old-skill"
    archive.mkdir(parents=True)
    (archive / "SKILL.md").write_text(
        "---\nname: old-skill\ndescription: archived.\n---\n\n# old\n"
    )
    conn = pkg["db"].get_db()
    assert pkg["skill_watcher"]._scan_once(conn) == 0
    assert conn.execute(
        "SELECT 1 FROM skill_usage WHERE name='old-skill'"
    ).fetchone() is None


def test_scan_once_skips_dir_without_skill_md(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    # Directory with no SKILL.md file inside.
    (pkg["skills_root"] / "empty-dir").mkdir()
    conn = pkg["db"].get_db()
    assert pkg["skill_watcher"]._scan_once(conn) == 0
    assert conn.execute(
        "SELECT 1 FROM skill_usage WHERE name='empty-dir'"
    ).fetchone() is None


def test_scan_once_handles_missing_skills_dir(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    # Remove the entire skills root.
    import shutil
    shutil.rmtree(pkg["skills_root"])
    conn = pkg["db"].get_db()
    # Must not raise — just return 0.
    assert pkg["skill_watcher"]._scan_once(conn) == 0


def test_start_skill_watcher_is_idempotent(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    sw = pkg["skill_watcher"]
    import threading

    def _count() -> int:
        return sum(1 for t in threading.enumerate() if t.name == "skill_watcher")

    # Other tests may have spawned (still-alive daemon) skill_watcher
    # threads in earlier modules; what we assert is the *delta* from
    # calling start_skill_watcher() twice in this test.
    before = _count()
    sw.start_skill_watcher()
    after_first = _count()
    sw.start_skill_watcher()
    after_second = _count()
    # First call adds exactly one; second call adds none.
    assert after_first == before + 1
    assert after_second == after_first


def test_start_skill_watcher_disabled_when_interval_zero(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval_s="0")
    sw = pkg["skill_watcher"]
    import threading
    before = {t.name for t in threading.enumerate()}
    sw.start_skill_watcher()
    after = {t.name for t in threading.enumerate()}
    # No skill_watcher thread should have been created.
    new_threads = after - before
    assert not any(n == "skill_watcher" for n in new_threads)


def test_start_skill_watcher_respects_disable_bg_daemons(tmp_path, monkeypatch):
    """BACKGROUND_DAEMONS_ALLOWED=False must block the daemon thread even
    when the poll interval is positive."""
    monkeypatch.setenv("THREADKEEPER_DISABLE_BG_DAEMONS", "1")
    pkg = _bootstrap(tmp_path, monkeypatch, interval_s="10")
    sw = pkg["skill_watcher"]
    import threading
    before = {t.name for t in threading.enumerate()}
    sw.start_skill_watcher()
    after = {t.name for t in threading.enumerate()}
    assert not any(n == "skill_watcher" for n in (after - before))


def test_scan_once_preserves_existing_origin(tmp_path, monkeypatch):
    """If a skill already has an agent-created provenance row, the watcher
    must NOT overwrite created_by_origin — INSERT … ON CONFLICT DO NOTHING."""
    pkg = _bootstrap(tmp_path, monkeypatch)
    _write_skill(pkg["skills_root"], "agent-made")
    conn = pkg["db"].get_db()
    # Pre-seed the row with background_review origin.
    conn.execute(
        "INSERT INTO skill_usage (name, created_at, created_by_origin) "
        "VALUES (?, ?, 'background_review')",
        ("agent-made", 1),
    )
    conn.commit()
    pkg["skill_watcher"]._scan_once(conn)
    row = conn.execute(
        "SELECT created_by_origin, patch_count FROM skill_usage "
        "WHERE name='agent-made'"
    ).fetchone()
    assert row["created_by_origin"] == "background_review"
    assert row["patch_count"] >= 1
