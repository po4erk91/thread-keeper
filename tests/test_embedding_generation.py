"""Stored vectors are searched only within the active embedding generation."""
from __future__ import annotations

import time


def test_fingerprint_names_vector_space(fresh_mp):
    from threadkeeper import embeddings

    tag = embeddings.embedding_fingerprint()
    assert fresh_mp["config"].EMBED_BACKEND in tag
    assert fresh_mp["config"].EMBED_MODEL_NAME in tag
    assert f"dim={fresh_mp['config'].EMBED_DIM}" in tag
    assert "pool=" in tag
    assert "runtime=" in tag


def test_legacy_cosine_ignores_stale_generation(fresh_mp, monkeypatch):
    import numpy as np
    from threadkeeper import embeddings

    active = embeddings.embedding_fingerprint()
    vector = np.zeros((fresh_mp["config"].EMBED_DIM,), dtype="float32")
    vector[0] = 1.0
    blob = vector.tobytes()
    conn = fresh_mp["db"].get_db()
    try:
        conn.execute(
            "INSERT INTO notes(content, kind, created_at, embedding, embed_backend) "
            "VALUES ('active vector', 'insight', ?, ?, ?)",
            (int(time.time()), blob, active),
        )
        conn.execute(
            "INSERT INTO notes(content, kind, created_at, embedding, embed_backend) "
            "VALUES ('stale vector', 'insight', ?, ?, 'onnx')",
            (int(time.time()), blob),
        )
        conn.commit()
        monkeypatch.setattr(embeddings, "_vec_on", lambda: False)
        monkeypatch.setattr(
            embeddings, "_encode", lambda _texts: np.asarray([vector])
        )
        hits = embeddings._cosine_search(conn, "query", k=5)
    finally:
        conn.close()

    assert [hit["content"] for hit in hits] == ["active vector"]
