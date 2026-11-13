"""Context is searchable without changing evidence or widening its scope."""
from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def _project(monkeypatch):
    monkeypatch.setenv("THREADKEEPER_PROJECT_NAMESPACE", "prj_aaaaaaaaaaaaaaaaaaaa")


def _note(pkg, topic="Aurora deployment rollback", body="Fixed by switching it off."):
    tools = pkg["mcp"]._tool_manager._tools
    tid = tools["open_thread"].fn(question=topic)
    assert tools["note"].fn(thread_id=tid, content=body).startswith("ok")
    conn = pkg["db"].get_db()
    row = conn.execute("SELECT id FROM notes WHERE thread_id=?", (tid,)).fetchone()
    return conn, tid, str(row[0])


@pytest.mark.parametrize("mode", ["fts", "hybrid"])
def test_short_note_is_found_by_topic_and_displays_original(fresh_mp, mode):
    from threadkeeper.retrieval import retrieve_notes
    conn, _, nid = _note(fresh_mp)
    hits = retrieve_notes(conn, "Aurora deployment rollback", mode=mode)
    assert [hit.id for hit in hits] == [nid]
    assert hits[0].content == "Fixed by switching it off."
    conn.close()


def test_long_entries_keep_bounded_original_text(fresh_mp):
    conn, _, nid = _note(fresh_mp, body="self contained " * 400)
    text = conn.execute("SELECT content FROM retrieval_documents WHERE memory_id=?", (nid,)).fetchone()[0]
    assert text == "self contained " * 400
    conn.close()


def test_bookkeeping_labels_do_not_create_lexical_matches(fresh_mp):
    from threadkeeper.retrieval import retrieve_notes
    conn, _, nid = _note(fresh_mp)
    assert retrieve_notes(conn, "GCP project", mode="fts") == []
    assert retrieve_notes(conn, "Aurora", mode="fts")[0].id == nid
    conn.close()


def test_topic_edit_updates_lexical_index_and_rejects_stale_vector(fresh_mp, monkeypatch):
    import numpy as np
    from threadkeeper import embeddings, memory_compat
    from threadkeeper.retrieval import retrieve_notes
    conn, tid, nid = _note(fresh_mp)
    generation = embeddings.embedding_fingerprint()
    rows = [(nid, conn.execute("SELECT content FROM note_retrieval_sources WHERE memory_id=?", (nid,)).fetchone()[0])]
    vector = np.zeros(fresh_mp["config"].EMBED_DIM, dtype="float32")
    vector[0] = 1
    memory_compat.stage_vectors(conn, generation, "note", rows, [vector.tobytes()])
    conn.commit()
    monkeypatch.setattr(embeddings, "_encode_for_generation", lambda *_: np.asarray([vector]))
    monkeypatch.setattr(embeddings, "_vec_on", lambda: False)
    assert embeddings._cosine_search(conn, "Aurora", 1)[0]["id"] == int(nid)
    conn.execute("UPDATE threads SET question='Borealis database restart' WHERE id=?", (tid,))
    conn.commit()
    assert retrieve_notes(conn, "Aurora", mode="fts") == []
    assert retrieve_notes(conn, "Borealis", mode="fts")[0].id == nid
    assert embeddings._cosine_search(conn, "Aurora", 1) == []
    assert memory_compat.coverage(conn, generation)["complete"] is False
    conn.close()


def test_dialog_topic_is_partitioned_and_erased_with_source(fresh_mp):
    from threadkeeper.project_scope import current_project_id
    from threadkeeper.retrieval import retrieve_dialogs
    conn = fresh_mp["db"].get_db()
    project = current_project_id()
    for uuid, role, text, project_id in [
        ("subject", "user", "Aurora deployment rollback", project),
        ("reply", "assistant", "Fixed by switching it off.", project),
        ("private", "user", "Secret nebula account", "other-project"),
    ]:
        conn.execute(
            "INSERT INTO dialog_messages(uuid,source,session_id,role,content,created_at,project_id) VALUES(?,?,?,?,?,?,?)",
            (uuid, "test", "session", role, text, int(time.time()), project_id),
        )
    conn.commit()
    hits = retrieve_dialogs(conn, "Aurora", role="assistant", mode="fts")
    assert [h.id for h in hits] == ["reply"]
    assert hits[0].content == "Fixed by switching it off."
    assert retrieve_dialogs(conn, "nebula", mode="fts") == []
    conn.execute("DELETE FROM dialog_messages WHERE uuid='subject'")
    conn.commit()
    assert retrieve_dialogs(conn, "Aurora", mode="fts") == []
    conn.close()


def test_filters_run_before_limit(fresh_mp):
    from threadkeeper.retrieval import retrieve_notes
    conn, tid, target = _note(fresh_mp)
    for idx in range(90):
        conn.execute(
            "INSERT INTO notes(content,kind,created_at,project_id,memory_scope) VALUES(?,?,?,?,?)",
            ("Aurora deployment rollback", "move", idx, "other-project", "project"),
        )
    conn.commit()
    assert [h.id for h in retrieve_notes(conn, "Aurora", k=1, mode="fts")] == [target]
    conn.execute("UPDATE notes SET deleted=1 WHERE id=?", (target,))
    conn.commit()
    assert retrieve_notes(conn, "Aurora", k=1, mode="fts") == []
    conn.close()


def test_invalidated_notes_and_expired_dialogs_never_enter_ranking(fresh_mp):
    from threadkeeper import config
    from threadkeeper.retrieval import retrieve_notes, retrieve_dialogs
    conn, _, nid = _note(fresh_mp)
    conn.execute("UPDATE memory_authority SET invalidated_at=1 WHERE artifact_kind='note' AND artifact_id=?", (nid,))
    conn.execute(
        "INSERT INTO dialog_messages(uuid,source,role,content,created_at) VALUES('old','test','user','Aurora expired',1)"
    )
    conn.commit()
    original = config.DIALOG_RETENTION_DAYS
    config.DIALOG_RETENTION_DAYS = 1
    try:
        assert retrieve_notes(conn, "Aurora", mode="fts") == []
        assert retrieve_dialogs(conn, "Aurora", mode="fts") == []
    finally:
        config.DIALOG_RETENTION_DAYS = original
        conn.close()


def test_provider_policy_blocks_dialog_search_including_like_fallback(fresh_mp, monkeypatch):
    from threadkeeper import config, egress
    from threadkeeper.retrieval import retrieve_dialogs
    conn = fresh_mp["db"].get_db()
    conn.execute("INSERT INTO dialog_messages(uuid,source,role,content,created_at) VALUES('x','test','user','Aurora secret',1)")
    conn.commit()
    monkeypatch.setattr(config.settings, "memory_egress", "work-only")
    monkeypatch.setitem(egress.CATEGORY_CLASS, "dialogs", egress.PERSONAL)
    assert retrieve_dialogs(conn, "Aurora", mode="fts") == []
    result = fresh_mp["mcp"]._tool_manager._tools["dialog_search"].fn(query="Aurora", mode="fts")
    assert "Aurora secret" not in str(result)
    conn.close()


def test_background_backfill_uses_context_and_is_idempotent(fresh_mp, monkeypatch):
    import numpy as np
    from threadkeeper import embeddings, ingest, memory_compat
    from threadkeeper.tools import threads
    monkeypatch.setattr(threads, "_embed", lambda _text: None)
    conn, _, _ = _note(fresh_mp)
    inputs = []
    def encode(texts):
        inputs.extend(texts)
        return np.zeros((len(texts), fresh_mp["config"].EMBED_DIM), dtype="float32")
    monkeypatch.setattr(embeddings, "encode_many", encode)
    monkeypatch.setattr(fresh_mp["config"], "SEMANTIC_AVAILABLE", True)
    assert ingest._backfill_background_embeddings(conn, max_rows=1) == 1
    assert "Aurora deployment rollback" in inputs[0]
    assert ingest._backfill_background_embeddings(conn, max_rows=1) == 0
    assert memory_compat.coverage(conn, embeddings.embedding_fingerprint())["complete"]
    conn.close()


def test_existing_store_migration_switches_fts_and_rolls_back(fresh_mp, monkeypatch):
    import numpy as np
    from threadkeeper import embeddings, memory_compat, migrate_embeddings
    from threadkeeper.retrieval import retrieve_notes
    conn, _, nid = _note(fresh_mp)
    old = embeddings.embedding_fingerprint(contextual=False)
    target = embeddings.embedding_fingerprint()
    conn.execute("UPDATE embedding_generation_state SET active_generation=? WHERE id=1", (old,))
    conn.execute("DELETE FROM retrieval_documents")
    conn.execute("DELETE FROM embedding_generation_vectors")
    conn.commit()
    assert retrieve_notes(conn, "Aurora", mode="fts") == []
    monkeypatch.setattr(migrate_embeddings, "SEMANTIC_AVAILABLE", True)
    monkeypatch.setattr(embeddings, "encode_many", lambda texts: np.zeros((len(texts), fresh_mp["config"].EMBED_DIM), dtype="float32"))
    monkeypatch.setattr(migrate_embeddings, "_upgrade_replay_gate", lambda *a, **kw: True)
    assert migrate_embeddings.run(do_notes=True, do_dialog=False, batch=1, dry_run=False, log=lambda _: None) == 0
    assert memory_compat.generation_state(conn, target)["active_generation"] == old
    assert retrieve_notes(conn, "Aurora", mode="fts") == []
    assert migrate_embeddings.run(do_notes=True, do_dialog=True, batch=1, dry_run=False, log=lambda _: None) == 0
    assert retrieve_notes(conn, "Aurora", mode="fts")[0].id == nid
    assert memory_compat.rollback_active(conn, target)
    assert retrieve_notes(conn, "Aurora", mode="fts") == []
    assert retrieve_notes(conn, "switching", mode="fts")[0].content == "Fixed by switching it off."
    conn.close()
