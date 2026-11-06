"""Fixture guidance stays available to maintainers and out of production."""
from __future__ import annotations

import json
import shutil
import sqlite3
import time

import pytest


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _create(pkg, name="validation-example", classification="fixture"):
    result = _tool(pkg, "skill_manage")(
        action="create", name=name, description="Use for lifecycle validation.",
        content="# Validation\n\nCheck the expected result.", classification=classification,
    )
    assert result.startswith("ok"), result
    root = pkg["config"].CLAUDE_SKILLS_DIR
    return root.parent / ".threadkeeper-fixtures" / root.name / name


def test_fixture_create_list_validate_and_mirrors(fresh_mp):
    from threadkeeper.tools.skills import _skill_roots

    directory = _create(fresh_mp)
    assert (directory / "SKILL.md").is_file()
    for root in _skill_roots():
        assert (root.parent / ".threadkeeper-fixtures" / root.name / directory.name / "SKILL.md").read_bytes() == (
            directory / "SKILL.md"
        ).read_bytes()
        assert not (root / directory.name).exists()
        assert not list(root.rglob("SKILL.md"))
    assert _tool(fresh_mp, "skill_list")() == "no_skills_tracked"
    assert "classification=fixture" in _tool(fresh_mp, "skill_list")(include_fixtures=True)
    validate = _tool(fresh_mp, "skill_validate")
    assert validate(name=directory.name).startswith("ERR skill_not_found")
    record = json.loads(validate(name=directory.name, include_fixtures=True))["skill"]
    assert record["classification"] == "fixture"
    assert record["compatible"]
    assert {m["status"] for m in record["mirrors"]} <= {"canonical", "in_sync"}


def test_fixture_discovery_routing_and_health_exclude_it(fresh_mp):
    from threadkeeper.candidate_reviewer import _active_skills_dump
    from threadkeeper.mcp_skills import skill_catalog, resource_catalog
    from threadkeeper.skill_audit import build_skill_audit
    from threadkeeper.tools.dashboard import _group_counts
    from threadkeeper.tools.skills import _recent_active_skills_dump

    fixture = _create(fresh_mp)
    _create(fresh_mp, "real-guidance", "production")
    record = _tool(fresh_mp, "skill_record")
    assert record(name=fixture.name) == "ERR fixture_requires_include_fixtures"
    assert record(name=fixture.name, include_fixtures=True) == "ok"
    conn = fresh_mp["db"].get_db()
    assert fixture.name not in _active_skills_dump(conn)
    assert fixture.name not in _recent_active_skills_dump(conn)
    assert _group_counts(conn, "skill_usage", "tier") == {"hypothesis": 1}
    audit = build_skill_audit(conn)
    assert audit["summary"]["total"] == 1
    assert [r["name"] for r in audit["skills"]] == ["real-guidance"]
    assert build_skill_audit(conn, include_fixtures=True)["summary"]["total"] == 2
    # A failed mirror cleanup must not leak a classified fixture through MCP.
    shutil.copytree(fixture, fresh_mp["config"].CLAUDE_SKILLS_DIR / fixture.name)
    assert [entry.name for entry in skill_catalog()] == ["real-guidance"]
    assert all(fixture.name not in str(resource.uri) for resource in resource_catalog())


@pytest.mark.parametrize("name", ["test-fixture", "seed-probe", "user-test-guide"])
def test_names_never_classify_production_guidance(fresh_mp, name):
    _create(fresh_mp, name, "production")
    conn = fresh_mp["db"].get_db()
    assert conn.execute(
        "SELECT classification FROM skill_usage WHERE name=?", (name,),
    ).fetchone()[0] == "production"
    assert name in _tool(fresh_mp, "skill_list")()


def test_fixture_migration_is_preview_first_and_hash_guarded(fresh_mp):
    from threadkeeper.tools.skills import _skill_roots

    _create(fresh_mp, "legacy-entry", "production")
    migrate = _tool(fresh_mp, "skill_fixture_migrate")
    evidence = "Maintainer confirms this entry belongs to the validator test run."
    preview = json.loads(migrate(name="legacy-entry", evidence=evidence))
    assert preview["evidence"] == evidence
    assert preview["origin"] == "foreground"
    root = fresh_mp["config"].CLAUDE_SKILLS_DIR
    assert (root / "legacy-entry" / "SKILL.md").is_file()
    assert not (root.parent / ".threadkeeper-fixtures").exists()
    assert migrate(name="legacy-entry", dry_run=False).startswith("ERR")
    assert migrate(name="legacy-entry", evidence=evidence, expected_sha256="outdated",
                   dry_run=False).startswith("ERR")
    result = migrate(name="legacy-entry", evidence=evidence,
                     expected_sha256=preview["sha256"], dry_run=False)
    assert result.startswith("ok classification=fixture"), result
    for target in _skill_roots():
        assert not (target / "legacy-entry").exists()
        assert (target.parent / ".threadkeeper-fixtures" / target.name / "legacy-entry" / "SKILL.md").is_file()
    assert "legacy-entry" not in _tool(fresh_mp, "skill_list")()


def test_unknown_candidate_needs_explicit_evidence(fresh_mp):
    root = fresh_mp["config"].CLAUDE_SKILLS_DIR
    directory = root / "test-user-entry"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: test-user-entry\ndescription: User instructions\n---\n\nKeep this.\n",
    )
    migrate = _tool(fresh_mp, "skill_fixture_migrate")
    preview = json.loads(migrate(name=directory.name))
    assert preview["origin"] == "unknown"
    assert migrate(name=directory.name, expected_sha256=preview["sha256"],
                   dry_run=False).startswith("ERR")
    assert directory.is_dir()
    assert fresh_mp["db"].get_db().execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0] == 0


def test_fixture_is_not_aged_or_passively_counted(fresh_mp, monkeypatch):
    from threadkeeper import ingest
    from threadkeeper.tools import skills

    fixture = _create(fresh_mp)
    conn = fresh_mp["db"].get_db()
    conn.execute("UPDATE skill_usage SET created_at=?, created_by_origin='background_review'",
                 (int(time.time()) - 200 * 86400,))
    conn.commit()
    monkeypatch.setattr(ingest, "_is_spawned_child_session", lambda *_args: False)
    ingest._record_skill_use(conn, fixture.name, int(time.time()))
    conn.commit()
    assert "validation-example:" not in skills.curator_run(dry_run=False)
    row = conn.execute("SELECT * FROM skill_usage WHERE name=?", (fixture.name,)).fetchone()
    assert row["state"] == "active"
    assert row["stale_since"] is None
    assert row["use_count"] == row["foreground_use_count"] == 0


def test_fixture_edit_delete_restore_preserve_classification(fresh_mp):
    directory = _create(fresh_mp)
    manage = _tool(fresh_mp, "skill_manage")
    assert manage(action="patch", name=directory.name, old_string="expected result",
                  new_string="fixture result").startswith("ok")
    assert manage(action="delete", name=directory.name, force=True).startswith("ok")
    assert not directory.exists()
    assert manage(action="restore", name=directory.name).startswith("ok")
    assert "fixture result" in (directory / "SKILL.md").read_text()
    assert _tool(fresh_mp, "skill_list")() == "no_skills_tracked"


def test_fixture_bundle_round_trip_stays_hidden(fresh_mp, tmp_path):
    from threadkeeper.semantic_bundle import export_semantic_bundle, import_semantic_bundle

    fixture = _create(fresh_mp)
    source = fresh_mp["db"].get_db()
    bundle = tmp_path / "fixtures.jsonl"
    export_semantic_bundle(bundle, source=source, source_namespace="fixture-tests")
    destination = sqlite3.connect(tmp_path / "destination.sqlite")
    source.backup(destination)
    destination.execute("DELETE FROM skill_usage")
    destination.commit()
    report = import_semantic_bundle(bundle, destination=destination, dry_run=False)
    assert not report.conflicts
    name, classification = destination.execute(
        "SELECT name, classification FROM skill_usage",
    ).fetchone()
    assert classification == "fixture"
    root = fresh_mp["config"].CLAUDE_SKILLS_DIR
    assert not (root / name).exists()
    assert (root.parent / ".threadkeeper-fixtures" / root.name / name / "SKILL.md").is_file()
    destination.close()


def test_current_schema_upgrade_preserves_existing_classifications(fresh_mp):
    db = fresh_mp["db"]
    conn = db.get_db()
    conn.execute("INSERT INTO skill_usage(name,created_at) VALUES ('seed-test',1)")
    conn.execute("ALTER TABLE skill_usage DROP COLUMN classification")
    conn.commit()
    conn.close()
    db._BOOTSTRAPPED = False
    db.bootstrap_db()
    conn = db.get_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.CURRENT_SCHEMA_VERSION
    assert conn.execute("SELECT classification FROM skill_usage").fetchone()[0] == "production"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE skill_usage SET classification='probe'")


def test_hidden_import_preserves_fixture_and_protects_unknown_guidance(fresh_mp, tmp_path):
    from threadkeeper.tools.skills import mirror_skill_from_path

    external = tmp_path / "external" / ".threadkeeper-fixtures" / "skills" / "external-fixture"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text(
        "---\nname: external-fixture\ndescription: Validation only\n---\n\nRun a check.\n",
    )
    assert mirror_skill_from_path(str(external)) == "external-fixture"
    root = fresh_mp["config"].CLAUDE_SKILLS_DIR
    assert (root.parent / ".threadkeeper-fixtures" / root.name / external.name / "SKILL.md").is_file()
    assert not (root / external.name).exists()
    user = root / "unknown-guidance"
    user.mkdir(parents=True)
    original = "---\nname: unknown-guidance\ndescription: Keep this\n---\n\nUser guide.\n"
    (user / "SKILL.md").write_text(original)
    candidate = external.parent / user.name
    candidate.mkdir()
    (candidate / "SKILL.md").write_text(original.replace("User guide", "Fixture"))
    assert mirror_skill_from_path(str(candidate)) is None
    assert (user / "SKILL.md").read_text() == original


def test_reconcile_preserves_untracked_hidden_canonical_source(fresh_mp):
    fixture = _create(fresh_mp)
    conn = fresh_mp["db"].get_db()
    conn.execute("DELETE FROM skill_usage")
    conn.commit()
    result = _tool(fresh_mp, "skill_manage")(action="reconcile", name=fixture.name)
    assert result.startswith("ok"), result
    assert (fixture / "SKILL.md").is_file()


def test_background_writer_cannot_apply_fixture_migration(fresh_mp, monkeypatch):
    from threadkeeper.tools import skills

    _create(fresh_mp, "production-guide", "production")
    preview = json.loads(skills.skill_fixture_migrate(name="production-guide"))
    monkeypatch.setattr(skills, "WRITE_ORIGIN", "curator")
    assert skills.skill_fixture_migrate(
        name="production-guide", evidence="Test entry", expected_sha256=preview["sha256"],
        dry_run=False,
    ) == "ERR fixture_migration_requires_foreground"


def test_fixture_events_stay_out_of_production_after_retirement(fresh_mp):
    fixture = _create(fresh_mp)
    assert _tool(fresh_mp, "skill_record")(
        name=fixture.name, outcome="helped", include_fixtures=True,
    ) == "ok"
    assert _tool(fresh_mp, "skill_manage")(
        action="delete", name=fixture.name, force=True,
    ).startswith("ok")
    rows = fresh_mp["db"].get_db().execute(
        "SELECT kind FROM events WHERE target=? AND kind IN "
        "('skill_create','skill_view','skill_use','skill_outcome')", (fixture.name,),
    ).fetchall()
    assert rows == []


def test_fixture_migration_reconciles_failed_visible_mirror_cleanup(fresh_mp, monkeypatch, tmp_path):
    from threadkeeper.tools import skills

    root = fresh_mp["config"].CLAUDE_SKILLS_DIR
    mirror = tmp_path / "mirror"
    monkeypatch.setattr(skills, "_skill_roots", lambda: [root, mirror])
    _create(fresh_mp, "legacy-probe", "production")
    original_remove = skills._remove_mirror_tree

    def failed_remove(path):
        if path == mirror / "legacy-probe":
            raise OSError("cannot remove")
        original_remove(path)

    preview = json.loads(skills.skill_fixture_migrate(name="legacy-probe"))
    monkeypatch.setattr(skills, "_remove_mirror_tree", failed_remove)
    result = skills.skill_fixture_migrate(
        name="legacy-probe", evidence="Maintainer-owned test probe",
        expected_sha256=preview["sha256"], dry_run=False,
    )
    assert result.startswith("ERR mirror_partial_failure"), result
    assert str(tmp_path) not in result
    assert (root.parent / ".threadkeeper-fixtures" / root.name / "legacy-probe" / "SKILL.md").is_file()
    assert skills.skill_list() == "no_skills_tracked"
    monkeypatch.setattr(skills, "_remove_mirror_tree", original_remove)
    assert skills.skill_manage(action="reconcile", name="legacy-probe").startswith("ok")
    assert not (mirror / "legacy-probe").exists()
    assert (mirror.parent / ".threadkeeper-fixtures" / mirror.name / "legacy-probe" / "SKILL.md").is_file()
