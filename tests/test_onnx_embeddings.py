"""ONNX embedding backend + tk-migrate-embeddings.

Verifies that:
- the active backend encodes to L2-normalized 384-dim float32 vectors
- embed_tag stamps the active backend for a real blob, None otherwise
- freshly inserted notes carry the embed_backend tag
- the migration recomputes stale (NULL-tagged) rows, tags them, and is
  idempotent + dry-run-safe

Skips entirely when no embedding backend is installed.
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.slow  # model warmup on first encode


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


@pytest.fixture()
def sem_pkg(fresh_mp):
    """Fresh package against a clean tmp DB; skip if semantic search is off."""
    if not fresh_mp["config"].SEMANTIC_AVAILABLE:
        pytest.skip("no embedding backend installed in this environment")
    return fresh_mp


def _seed_legacy_notes(conn, n: int):
    """Insert n notes with a real embedding blob but a NULL backend tag,
    simulating rows written before the ONNX migration."""
    from threadkeeper import embeddings as emb
    for i in range(n):
        blob = emb._embed(f"legacy seeded note {i} about webhooks and retries")
        conn.execute(
            "INSERT INTO notes (content, kind, created_at, embedding, embed_backend) "
            "VALUES (?,?,?,?,NULL)",
            (f"legacy seeded note {i}", "insight", int(time.time()), blob),
        )
    conn.commit()


# ── encode primitives ────────────────────────────────────────────────

def test_encode_is_normalized_384_float32(sem_pkg):
    import numpy as np
    from threadkeeper import embeddings as emb
    arr = emb._encode(["привет мир", "hello world"])
    assert arr is not None
    assert arr.shape == (2, 384)
    assert arr.dtype == np.dtype("float32")
    assert np.allclose(np.linalg.norm(arr, axis=1), 1.0, atol=1e-3)


def test_encode_is_cross_lingual(sem_pkg):
    """A RU/EN translation pair must score higher than an unrelated phrase."""
    from threadkeeper import embeddings as emb
    v = emb._encode(["кошка", "cat", "quarterly financial report"])
    assert float(v[0] @ v[1]) > float(v[0] @ v[2])


def test_embed_tag(sem_pkg):
    from threadkeeper import embeddings as emb
    active = emb.embedding_fingerprint()
    assert emb.embed_tag(b"\x00\x01") == active
    assert emb.embed_tag(None) is None
    assert sem_pkg["config"].EMBED_MODEL_NAME in active
    assert f"dim={sem_pkg['config'].EMBED_DIM}" in active


# ── write-path tagging ───────────────────────────────────────────────

def test_new_note_carries_backend_tag(sem_pkg):
    tid = _tool(sem_pkg, "open_thread")(question="backend tag test")
    _tool(sem_pkg, "note")(thread_id=tid,
                           content="tagged note about idempotency keys",
                           kind="insight")
    conn = sem_pkg["db"].get_db()
    from threadkeeper import embeddings as emb
    active = emb.embedding_fingerprint()
    row = conn.execute(
        "SELECT embedding, embed_backend FROM notes "
        "WHERE thread_id=? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    assert row["embedding"] is not None
    assert row["embed_backend"] == active


# ── migration ────────────────────────────────────────────────────────

def test_migration_recomputes_tags_and_is_idempotent(sem_pkg):
    from threadkeeper import migrate_embeddings as mig
    from threadkeeper import embeddings as emb
    active = emb.embedding_fingerprint()
    conn = sem_pkg["db"].get_db()
    _seed_legacy_notes(conn, 3)

    assert mig._count_stale(conn, "notes", active) == 3

    rc = mig.run(do_notes=True, do_dialog=False, batch=2,
                 dry_run=False, log=lambda _m: None)
    assert rc == 0
    assert mig._count_stale(conn, "notes", active) == 0
    tagged = conn.execute(
        "SELECT COUNT(*) FROM notes WHERE embed_backend=?", (active,)
    ).fetchone()[0]
    assert tagged >= 3

    # idempotent: a second pass finds nothing stale and changes nothing.
    rc2 = mig.run(do_notes=True, do_dialog=False, batch=2,
                  dry_run=False, log=lambda _m: None)
    assert rc2 == 0
    assert mig._count_stale(conn, "notes", active) == 0


def test_migration_dry_run_writes_nothing(sem_pkg):
    from threadkeeper import migrate_embeddings as mig
    from threadkeeper import embeddings as emb
    active = emb.embedding_fingerprint()
    conn = sem_pkg["db"].get_db()
    _seed_legacy_notes(conn, 2)

    assert mig._count_stale(conn, "notes", active) == 2
    mig.run(do_notes=True, do_dialog=False, batch=10,
            dry_run=True, log=lambda _m: None)
    # still stale — dry run must not touch the rows
    assert mig._count_stale(conn, "notes", active) == 2


def test_migration_requires_a_scope_flag(sem_pkg):
    from threadkeeper import migrate_embeddings as mig
    with pytest.raises(SystemExit):
        mig.main([])  # argparse error → SystemExit(2)
