from __future__ import annotations

import json
import sqlite3
import time

import pytest


def _copy_empty_store(source: sqlite3.Connection, destination_path):
    destination = sqlite3.connect(str(destination_path))
    source.backup(destination)
    return destination


def _mapping(conn: sqlite3.Connection, record_type: str, source_id: str) -> str:
    row = conn.execute(
        "SELECT local_id FROM semantic_bundle_import_records "
        "WHERE source_namespace='source-a' AND record_type=? AND source_id=?",
        (record_type, source_id),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _seed_semantic_rows(conn: sqlite3.Connection, now: int) -> None:
    conn.execute(
        "INSERT INTO threads (id, question, state, opened_at, last_touched_at) "
        "VALUES ('thread-1', 'source question', 'closed', ?, ?)",
        (now, now + 1),
    )
    conn.execute(
        "INSERT INTO notes (thread_id, content, kind, created_at) "
        "VALUES ('thread-1', 'portable note', 'summary', ?)",
        (now + 2,),
    )
    conn.execute(
        "INSERT INTO concepts (id, description, registered_at) "
        "VALUES ('concept-1', 'a portable concept', ?)",
        (now + 3,),
    )
    conn.execute(
        "INSERT INTO distill (id, content, created_at) VALUES ('distill-1', 'durable fact', ?)",
        (now + 4,),
    )
    conn.execute(
        "INSERT INTO edges (from_kind, from_id, to_kind, to_id, relation, created_at) "
        "VALUES ('thread', 'thread-1', 'note', '1', 'refines', ?)",
        (now + 5,),
    )
    conn.execute(
        "INSERT INTO core_memory (key, content, priority, updated_at) "
        "VALUES ('private-rule', 'private core memory', 50, ?)",
        (now + 6,),
    )
    conn.execute(
        "INSERT INTO user_dialectic "
        "(id, claim, state, created_at, valid_from, valid_to) "
        "VALUES ('claim-1', 'private preference', 'retired', ?, ?, ?)",
        (now + 7, now + 7, now + 9),
    )
    conn.execute(
        "INSERT INTO dialectic_evidence (claim_id, kind, quote, created_at) "
        "VALUES ('claim-1', 'support', 'because', ?)",
        (now + 8,),
    )
    conn.execute(
        "INSERT INTO dialog_messages "
        "(uuid, source, role, content, created_at) "
        "VALUES ('dialog-1', 'pytest', 'user', 'raw dialog', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO verbatim (speaker, content, created_at) VALUES ('user', 'raw quote', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO memory_authority "
        "(artifact_kind, artifact_id, authority_class, source_principal, source_channel, "
        "created_at, invalidated_at, invalidation_reason) "
        "VALUES ('claim', 'claim-1', 'trusted', 'person', 'foreground', ?, ?, 'superseded')",
        (now, now + 10),
    )
    conn.execute(
        "INSERT INTO memory_provenance "
        "(memory_kind, memory_id, writer_provider, writer_model, writer_revision, recorded_at) "
        "VALUES ('claim', 'claim-1', 'test', 'test-model', 'v1', ?)",
        (now,),
    )
    conn.execute("UPDATE user_dialectic SET deleted=1 WHERE id='claim-1'")
    conn.commit()


def test_default_export_redacts_sensitive_classes_and_full_round_trip(fresh_mp, tmp_path):
    from threadkeeper import config
    from threadkeeper.lessons import append_lesson, get_path
    from threadkeeper.semantic_bundle import export_semantic_bundle, import_semantic_bundle

    source = fresh_mp["db"].get_db()
    destination = _copy_empty_store(source, tmp_path / "destination.sqlite")
    now = int(time.time())
    _seed_semantic_rows(source, now)
    append_lesson("Portable lesson", "Keep semantic exports portable.", source="thread-1")
    skill_dir = config.CLAUDE_SKILLS_DIR / "portable-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: portable-skill\ndescription: Portable test skill.\n---\n\nUse it.\n"
    )
    source.execute(
        "INSERT INTO skill_usage (name, created_at, created_by_origin) "
        "VALUES ('portable-skill', ?, 'foreground')",
        (now,),
    )
    config.TASK_LOG_DIR.mkdir(parents=True)
    (config.TASK_LOG_DIR / "task-1.log").write_text("raw task log")
    source.commit()

    redacted = tmp_path / "redacted.bundle"
    export_semantic_bundle(redacted, source=source, source_namespace="source-a")
    redacted_types = {
        json.loads(line)["type"]
        for line in (redacted / "records.jsonl").read_text().splitlines()
    }
    assert {"thread", "note", "lesson", "skill", "edge"} <= redacted_types
    assert {"dialog", "verbatim", "task_log", "core_memory", "claim", "evidence"}.isdisjoint(
        redacted_types
    )

    full = tmp_path / "full.bundle"
    export_semantic_bundle(
        full,
        source=source,
        source_namespace="source-a",
        include_dialog=True,
        include_task_logs=True,
        include_personal_memory=True,
    )
    preview = import_semantic_bundle(full, destination=destination)
    assert preview.dry_run is True
    assert preview.additions
    assert not preview.conflicts
    assert not preview.invalid_records
    assert destination.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0

    applied = import_semantic_bundle(full, destination=destination, dry_run=False)
    assert applied.dry_run is False
    thread_id = _mapping(destination, "thread", "thread-1")
    note_id = _mapping(destination, "note", "1")
    claim_id = _mapping(destination, "claim", "claim-1")
    evidence_id = _mapping(destination, "evidence", "1")
    assert destination.execute("SELECT question FROM threads WHERE id=?", (thread_id,)).fetchone()[0] == "source question"
    assert destination.execute("SELECT thread_id FROM notes WHERE id=?", (note_id,)).fetchone()[0] == thread_id
    assert destination.execute("SELECT claim_id FROM dialectic_evidence WHERE id=?", (evidence_id,)).fetchone()[0] == claim_id
    assert destination.execute("SELECT valid_from, valid_to, deleted FROM user_dialectic WHERE id=?", (claim_id,)).fetchone() == (now + 7, now + 9, 1)
    assert destination.execute(
        "SELECT invalidated_at FROM memory_authority WHERE artifact_id=?", (claim_id,)
    ).fetchone()[0] == now + 10
    assert destination.execute(
        "SELECT memory_id FROM memory_provenance WHERE memory_kind='claim'"
    ).fetchone()[0] == claim_id
    assert destination.execute(
        "SELECT from_id, to_id FROM edges"
    ).fetchone() == (thread_id, note_id)
    imported_skill = _mapping(destination, "skill", "portable-skill")
    assert (config.CLAUDE_SKILLS_DIR / imported_skill / "SKILL.md").is_file()
    imported_lesson = _mapping(destination, "lesson", "portable-lesson")
    assert imported_lesson in get_path().read_text()
    assert import_semantic_bundle(full, destination=destination, dry_run=False).existing


def test_import_rejects_tampered_hashes_and_schema_versions_without_writes(fresh_mp, tmp_path):
    from threadkeeper.semantic_bundle import (
        SemanticBundleError,
        export_semantic_bundle,
        import_semantic_bundle,
    )

    source = fresh_mp["db"].get_db()
    destination = _copy_empty_store(source, tmp_path / "destination.sqlite")
    now = int(time.time())
    _seed_semantic_rows(source, now)
    bundle = tmp_path / "bundle"
    export_semantic_bundle(bundle, source=source, source_namespace="source-a")
    records_path = bundle / "records.jsonl"
    records_path.write_text(records_path.read_text().replace("source question", "tampered question"))
    preview = import_semantic_bundle(bundle, destination=destination)
    assert any(item["reason"] == "records_sha256_mismatch" for item in preview.invalid_records)
    with pytest.raises(SemanticBundleError, match="invalid"):
        import_semantic_bundle(bundle, destination=destination, dry_run=False)
    assert destination.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0

    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = 99
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(SemanticBundleError, match="schema version"):
        import_semantic_bundle(bundle, destination=destination)
    assert destination.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0


def test_dry_import_reports_deterministic_changed_source_conflict(fresh_mp, tmp_path):
    from threadkeeper.semantic_bundle import export_semantic_bundle, import_semantic_bundle

    source = fresh_mp["db"].get_db()
    destination = _copy_empty_store(source, tmp_path / "destination.sqlite")
    now = int(time.time())
    _seed_semantic_rows(source, now)
    first = tmp_path / "first.bundle"
    export_semantic_bundle(first, source=source, source_namespace="source-a")
    import_semantic_bundle(first, destination=destination, dry_run=False)
    source.execute("UPDATE notes SET content='changed after export' WHERE id=1")
    source.commit()
    changed = tmp_path / "changed.bundle"
    export_semantic_bundle(changed, source=source, source_namespace="source-a")
    report = import_semantic_bundle(changed, destination=destination)
    assert {item["reason"] for item in report.conflicts} == {"source_record_changed"}
    assert {item["type"] for item in report.conflicts} == {"note"}
