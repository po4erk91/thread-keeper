"""Staged embedding activation and derived-memory provenance."""
from __future__ import annotations

import time


def _vector(dim: int):
    import numpy as np

    vector = np.zeros((dim,), dtype="float32")
    vector[0] = 1.0
    return vector


def _seed_old_notes(conn, generation: str, blob: bytes) -> None:
    for text in ("old generation first", "old generation second"):
        conn.execute(
            "INSERT INTO notes(content, kind, created_at, embedding, embed_backend) "
            "VALUES (?,?,?,?,?)",
            (text, "insight", int(time.time()), blob, generation),
        )
    conn.commit()


def test_staging_never_changes_active_generation_then_switches_atomically(
    fresh_mp, monkeypatch,
):
    from threadkeeper import embeddings, memory_compat, migrate_embeddings

    conn = fresh_mp["db"].get_db()
    target = embeddings.embedding_fingerprint()
    old = "onnx:legacy-model:revision=old:dim=384:pool=mean:runtime=0.8"
    vector = _vector(fresh_mp["config"].EMBED_DIM)
    _seed_old_notes(conn, old, vector.tobytes())
    # Simulate the v4→v5 bootstrap over an existing tagged corpus rather than
    # this fresh fixture's already-initialized empty store.
    conn.execute(
        "UPDATE embedding_generation_state SET active_generation=NULL, "
        "staging_generation=NULL, previous_generation=NULL, state='ready' WHERE id=1"
    )
    assert memory_compat.generation_state(conn, target)["active_generation"] == old
    conn.commit()

    monkeypatch.setattr(migrate_embeddings, "SEMANTIC_AVAILABLE", True)
    monkeypatch.setattr(
        embeddings, "encode_many", lambda texts: __import__("numpy").asarray(
            [vector for _ in texts], dtype="float32"
        ),
    )
    monkeypatch.setattr(embeddings, "_vec_on", lambda: False)
    monkeypatch.setattr(
        embeddings, "_encode_for_generation", lambda _texts, _generation: __import__(
            "numpy"
        ).asarray([vector]),
    )

    # Commit one batch, then stop at a partial scope as a crash would. The old
    # pointer and old query space must stay live even though target rows exist.
    rc = migrate_embeddings.run(
        do_notes=True, do_dialog=False, batch=1, dry_run=False,
        log=lambda _message: None,
    )
    assert rc == 0
    state = memory_compat.generation_state(conn, target)
    assert state["active_generation"] == old
    assert state["staging_generation"] == target
    assert memory_compat.coverage(conn, target)["notes_valid"] == 2
    assert [h["content"] for h in embeddings._cosine_search(conn, "query", 5)] == [
        "old generation first", "old generation second"
    ]

    # A second --all run resumes from staged rows and flips the pointer once
    # only after validation sees every source row.
    rc = migrate_embeddings.run(
        do_notes=True, do_dialog=True, batch=1, dry_run=False,
        log=lambda _message: None,
    )
    assert rc == 0
    state = memory_compat.generation_state(conn, target)
    assert state["active_generation"] == target
    assert state["staging_generation"] is None
    assert state["previous_generation"] == old
    assert [h["content"] for h in embeddings._cosine_search(conn, "query", 5)] == [
        "old generation first", "old generation second"
    ]
    # The source table still has the prior vectors: activation was a pointer
    # update, not a destructive in-place rewrite.
    assert conn.execute(
        "SELECT COUNT(*) FROM notes WHERE embed_backend=?", (old,)
    ).fetchone()[0] == 2

    assert memory_compat.rollback_active(conn, target) is True
    assert memory_compat.generation_state(conn, target)["active_generation"] == old
    conn.close()


def test_new_note_records_writer_identity_and_source_pointer(fresh_mp):
    tools = fresh_mp["mcp"]._tool_manager._tools
    open_thread = tools["open_thread"].fn
    note = tools["note"].fn
    tid = open_thread(question="capture a durable decision")
    assert note(thread_id=tid, content="Use a staged switch for model upgrades", kind="insight").startswith("ok")

    conn = fresh_mp["db"].get_db()
    try:
        row = conn.execute(
            "SELECT writer_provider, writer_model, writer_revision, "
            "source_event_kind, source_thread_id FROM memory_provenance "
            "WHERE memory_kind='note'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "pytest"
    assert row[1]  # exact model may be host-specific, but it is never absent
    assert row[2]
    assert row[3] == "note:insight"
    assert row[4] == tid
