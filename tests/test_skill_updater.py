from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _bootstrap(tmp_path, monkeypatch, *, interval="302400", sources=""):
    claude_root = tmp_path / "claude_skills"
    codex_home = tmp_path / "codex_home"
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "CLAUDE_SKILLS_DIR": str(claude_root),
        "CODEX_HOME": str(codex_home),
        "THREADKEEPER_SKILL_UPDATE_INTERVAL_S": interval,
        "THREADKEEPER_SKILL_UPDATE_SOURCES": sources,
        "THREADKEEPER_DISABLE_BG_DAEMONS": "1",
        "THREADKEEPER_AUTO_UPDATE_INTERVAL_S": "0",
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_INGEST_CAP": "0",
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
        "THREADKEEPER_THREAD_JANITOR_INTERVAL_S": "0",
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
        "THREADKEEPER_CLIENT": "pytest",
        "THREADKEEPER_NO_EMBEDDINGS": "1",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    claude_root.mkdir(parents=True, exist_ok=True)
    (codex_home / "skills").mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import db, skill_updater

    return {
        "db": db,
        "skill_updater": skill_updater,
        "claude_root": claude_root,
        "codex_root": codex_home / "skills",
    }


def _skill_body(name: str, marker: str) -> str:
    return (
        "---\n"
        f"name: \"{name}\"\n"
        "description: \"Use when testing skill updater behavior.\"\n"
        "---\n\n"
        f"# {name}\n\n{marker}\n"
    )


def _write_skill(root: Path, name: str, marker: str, mtime: int | None = None) -> Path:
    sdir = root / name
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "SKILL.md").write_text(_skill_body(name, marker), encoding="utf-8")
    if mtime is not None:
        for path in [sdir, sdir / "SKILL.md"]:
            os.utime(path, (mtime, mtime))
    return sdir


def test_disabled_without_force(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="0")

    assert pkg["skill_updater"].run_skill_update_pass() == "disabled"


def test_force_pass_imports_newer_local_skill_copy(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    _write_skill(pkg["claude_root"], "demo-skill", "old copy", mtime=100)
    _write_skill(pkg["codex_root"], "demo-skill", "new copy", mtime=200)

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "local_updates=1" in out
    assert "new copy" in (
        pkg["claude_root"] / "demo-skill" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "new copy" in (
        pkg["codex_root"] / "demo-skill" / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_force_pass_imports_skill_installed_only_in_non_primary_root(
    tmp_path,
    monkeypatch,
):
    pkg = _bootstrap(tmp_path, monkeypatch)
    _write_skill(pkg["codex_root"], "codex-only", "installed in codex", mtime=200)

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "local_updates=1" in out
    assert "installed in codex" in (
        pkg["claude_root"] / "codex-only" / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_force_pass_preserves_explicit_fixture_classification(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO skill_usage "
        "(name, created_at, created_by_origin, classification) VALUES (?, ?, ?, ?)",
        ("fixture-copy", 1, "foreground", "fixture"),
    )
    conn.commit()
    _write_skill(
        pkg["claude_root"] / ".fixtures", "fixture-copy", "old copy", mtime=100,
    )
    _write_skill(
        pkg["codex_root"] / ".fixtures", "fixture-copy", "new copy", mtime=200,
    )

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "local_updates=1" in out
    assert "new copy" in (
        pkg["claude_root"] / ".fixtures" / "fixture-copy" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "new copy" in (
        pkg["codex_root"] / ".fixtures" / "fixture-copy" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert not (pkg["claude_root"] / "fixture-copy").exists()


def test_force_pass_does_not_infer_a_fixture_from_a_hidden_path(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    _write_skill(
        pkg["codex_root"] / ".fixtures", "unknown-fixture", "not registered", mtime=200,
    )

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "local_checked=0" in out
    assert not (pkg["claude_root"] / ".fixtures" / "unknown-fixture").exists()
    row = pkg["db"].get_db().execute(
        "SELECT 1 FROM skill_usage WHERE name='unknown-fixture'"
    ).fetchone()
    assert row is None


def test_source_tracked_skill_updates_and_mirrors(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    updater = pkg["skill_updater"]
    local = _write_skill(pkg["claude_root"], "remote-skill", "v1", mtime=100)
    old_hash = updater._tree_sha256(local)
    source = updater.GithubSource(
        repo="example/skills",
        ref="main",
        path="skills/remote-skill",
    )
    (local / updater.SOURCE_FILE).write_text(
        json.dumps({
            "type": "github",
            "repo": source.repo,
            "ref": source.ref,
            "path": source.path,
            "tree_sha256": old_hash,
        }),
        encoding="utf-8",
    )
    remote = _write_skill(tmp_path / "remote_repo" / "skills", "remote-skill", "v2")
    remote_skill = updater.RemoteSkill(
        name="remote-skill",
        source=source,
        path=remote,
        tree_sha256=updater._tree_sha256(remote),
    )

    monkeypatch.setattr(
        updater,
        "_build_remote_index",
        lambda sources, tmp_root: (
            {(source.repo, source.ref, source.path): remote_skill},
            {"remote-skill": remote_skill},
            [],
        ),
    )

    out = updater.run_skill_update_pass(force=True)

    assert "remote_updated=1" in out
    assert "v2" in (pkg["claude_root"] / "remote-skill" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "v2" in (pkg["codex_root"] / "remote-skill" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    meta = json.loads((pkg["claude_root"] / "remote-skill" / updater.SOURCE_FILE).read_text(
        encoding="utf-8"
    ))
    assert meta["tree_sha256"] == remote_skill.tree_sha256


def test_source_tracked_skill_skips_local_changes(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    updater = pkg["skill_updater"]
    local = _write_skill(pkg["claude_root"], "remote-skill", "local edit")
    source = updater.GithubSource(
        repo="example/skills",
        ref="main",
        path="skills/remote-skill",
    )
    (local / updater.SOURCE_FILE).write_text(
        json.dumps({
            "type": "github",
            "repo": source.repo,
            "ref": source.ref,
            "path": source.path,
            "tree_sha256": "previous-upstream-hash",
        }),
        encoding="utf-8",
    )
    remote = _write_skill(tmp_path / "remote_repo" / "skills", "remote-skill", "v2")
    remote_skill = updater.RemoteSkill(
        name="remote-skill",
        source=source,
        path=remote,
        tree_sha256=updater._tree_sha256(remote),
    )
    monkeypatch.setattr(
        updater,
        "_build_remote_index",
        lambda sources, tmp_root: (
            {(source.repo, source.ref, source.path): remote_skill},
            {"remote-skill": remote_skill},
            [],
        ),
    )

    out = updater.run_skill_update_pass(force=True)

    assert "remote_skipped=1" in out
    assert "local edit" in (
        pkg["claude_root"] / "remote-skill" / "SKILL.md"
    ).read_text(encoding="utf-8")


def _record_skill_delete(pkg, name: str, at: int) -> None:
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES ('pytest', 'skill_delete', ?, 'trash=x', ?)",
        (name, at),
    )
    conn.commit()


def test_deleted_skill_is_not_revived_from_an_older_mirror(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    _write_skill(pkg["codex_root"], "deleted-junk", "stale mirror", mtime=200)
    _record_skill_delete(pkg, "deleted-junk", at=300)

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "removed_strays=1" in out
    assert "local_updates=0" in out
    assert not (pkg["claude_root"] / "deleted-junk").exists()
    assert not (pkg["codex_root"] / "deleted-junk").exists()


def test_reinstall_after_deletion_is_still_imported(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    _write_skill(pkg["codex_root"], "reinstalled", "fresh install", mtime=400)
    _record_skill_delete(pkg, "reinstalled", at=300)

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "local_updates=1" in out
    assert "fresh install" in (
        pkg["claude_root"] / "reinstalled" / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_invalid_primary_skill_is_not_mirrored(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    _write_skill(pkg["codex_root"], "broken-skill", "older valid mirror", mtime=100)
    broken = pkg["claude_root"] / "broken-skill"
    broken.mkdir()
    (broken / "SKILL.md").write_text("no frontmatter at all\n", encoding="utf-8")
    for path in (broken, broken / "SKILL.md"):
        os.utime(path, (300, 300))

    out = pkg["skill_updater"].run_skill_update_pass(force=True)

    assert "conflicts=1" in out
    assert "older valid mirror" in (
        pkg["codex_root"] / "broken-skill" / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_removing_a_stray_keeps_a_backup(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    _write_skill(pkg["codex_root"], "old-copy", "stale mirror", mtime=200)
    _record_skill_delete(pkg, "old-copy", at=300)
    backups: list[str] = []
    monkeypatch.setattr(
        pkg["skill_updater"], "_backup_skill_dir",
        lambda path: backups.append(Path(path).name),
    )

    pkg["skill_updater"].run_skill_update_pass(force=True)

    assert backups == ["old-copy"]
    assert not (pkg["codex_root"] / "old-copy").exists()
