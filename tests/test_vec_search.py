"""sqlite-vec backed semantic search.

Verifies that:
- vec0 virtual tables are created when the extension is available
- new notes get dual-written (BLOB + vec0)
- _cosine_search uses the vec0 path when available
- search results are correct (right top-k, scores in [-1, 1])
- legacy fallback still works when vec0 unavailable
- backfill migrates pre-existing BLOB embeddings into vec0
"""
from __future__ import annotations

import pytest


_FAKE_CID = "abababab-cdcd-efef-1212-343434343434"


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


@pytest.fixture()
def vec_pkg(mp_with_cid):
    """Force a fresh package import and confirm vec0 loaded — skips if it
    didn't, so the suite runs on systems without the extension."""
    pkg = mp_with_cid(_FAKE_CID)
    # Touch get_db() first so the lazy extension probe runs.
    pkg["db"].get_db()
    from threadkeeper.db import vec_available
    if not vec_available():
        pytest.skip("sqlite-vec extension not available in this environment")
    return pkg


# ─────────────────────────────────────────────────────────────────────
# Schema bootstrap
# ─────────────────────────────────────────────────────────────────────

def test_vec0_tables_exist_after_first_get_db(vec_pkg):
    conn = vec_pkg["db"].get_db()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    # vec0 virtual tables show up as regular tables in sqlite_master
    assert "notes_vec" in tables
    assert "dialog_vec" in tables
    assert "dialog_vec_map" in tables


# ─────────────────────────────────────────────────────────────────────
# Dual-write on insert
# ─────────────────────────────────────────────────────────────────────

def test_new_note_is_mirrored_into_notes_vec(vec_pkg):
    open_t = _tool(vec_pkg, "open_thread")
    note = _tool(vec_pkg, "note")
    tid = open_t(question="vec dual-write test")
    note(thread_id=tid, content="webhook idempotency keys deduplicate",
         kind="insight")
    conn = vec_pkg["db"].get_db()
    note_row = conn.execute(
        "SELECT id FROM notes WHERE thread_id=? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    assert note_row is not None
    vec_row = conn.execute(
        "SELECT id FROM notes_vec WHERE id=?", (note_row["id"],)
    ).fetchone()
    assert vec_row is not None  # mirrored


# ─────────────────────────────────────────────────────────────────────
# Cosine via vec0
# ─────────────────────────────────────────────────────────────────────

def test_cosine_search_returns_topk_via_vec0(vec_pkg):
    """Seed three thematically distinct notes, query for one, expect it
    to come first in results."""
    open_t = _tool(vec_pkg, "open_thread")
    note = _tool(vec_pkg, "note")
    tid = open_t(question="vec ranking test")
    note(thread_id=tid, content="payment webhook retry strategy",
         kind="insight")
    note(thread_id=tid, content="kubernetes pod liveness probes",
         kind="insight")
    note(thread_id=tid, content="javascript array destructuring",
         kind="insight")
    from threadkeeper.embeddings import _cosine_search
    conn = vec_pkg["db"].get_db()
    hits = _cosine_search(conn, "stripe webhook retries", k=3)
    assert len(hits) == 3
    # webhook-related note should rank top
    assert "webhook" in hits[0]["content"].lower()
    # All scores in valid cosine range
    for h in hits:
        assert -1.0 <= h["score"] <= 1.0


def test_dialog_cosine_search_uses_vec0(vec_pkg):
    """Inject synthetic dialog_messages with hand-crafted embeddings,
    verify vec0 path finds the closest one. Vectors must be in DIFFERENT
    directions in the unit sphere — varying magnitude of the same axis
    normalizes to the same direction."""
    import struct
    conn = vec_pkg["db"].get_db()

    DIM = 384

    def unit_vec(axis: int) -> bytes:
        arr = [0.0] * DIM
        arr[axis] = 1.0
        return struct.pack(f"{DIM}f", *arr)

    def diagonal_vec() -> bytes:
        """Equal-weight on two axes — sits between unit_vec(0) and unit_vec(1)."""
        arr = [0.0] * DIM
        arr[0] = arr[1] = 1.0
        n = (sum(x * x for x in arr)) ** 0.5
        arr = [x / n for x in arr]
        return struct.pack(f"{DIM}f", *arr)

    samples = [
        ("u_close",  unit_vec(0),  "axis-0 vector — closest to query"),
        ("u_medium", diagonal_vec(), "between axis-0 and axis-1"),
        ("u_far",    unit_vec(1),  "axis-1 vector — orthogonal to query"),
    ]
    import time as _t
    for uuid, emb, text in samples:
        conn.execute(
            "INSERT INTO dialog_messages (uuid, source, project, "
            "session_id, role, content, model, created_at, embedding) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (uuid, "claude-code", "x", "sess", "user", text, None,
             int(_t.time()), emb),
        )
        from threadkeeper.embeddings import _vec_upsert_dialog
        _vec_upsert_dialog(conn, uuid, emb)
    conn.commit()

    qv = unit_vec(0)
    from threadkeeper.embeddings import _vec0_dialog_search
    hits = _vec0_dialog_search(conn, qv, k=3)
    assert len(hits) == 3
    assert hits[0]["uuid"] == "u_close"
    assert hits[1]["uuid"] == "u_medium"
    assert hits[2]["uuid"] == "u_far"


# ─────────────────────────────────────────────────────────────────────
# Backfill of existing rows
# ─────────────────────────────────────────────────────────────────────

def test_backfill_vec_tables_picks_up_legacy_blobs(vec_pkg):
    """Insert a note with BLOB embedding but no vec0 entry, run backfill,
    verify the row appears in notes_vec."""
    open_t = _tool(vec_pkg, "open_thread")
    note = _tool(vec_pkg, "note")
    tid = open_t(question="backfill target")
    note(thread_id=tid, content="something to be backfilled later",
         kind="insight")
    conn = vec_pkg["db"].get_db()
    note_id = conn.execute(
        "SELECT id FROM notes WHERE thread_id=? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()["id"]
    # Simulate legacy state: remove the vec0 mirror
    conn.execute("DELETE FROM notes_vec WHERE id=?", (note_id,))
    conn.commit()

    from threadkeeper.ingest import _backfill_vec_tables
    n_notes, _ = _backfill_vec_tables(conn, batch=100)
    assert n_notes >= 1

    # Now in notes_vec again
    again = conn.execute(
        "SELECT id FROM notes_vec WHERE id=?", (note_id,)
    ).fetchone()
    assert again is not None


# ─────────────────────────────────────────────────────────────────────
# Fallback path (vec0 unavailable)
# ─────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────
# Delete-sync: notes_vec must not orphan when a note is deleted (#85)
# ─────────────────────────────────────────────────────────────────────

def _unit_blob(axis: int, dim: int) -> bytes:
    import struct
    arr = [0.0] * dim
    arr[axis] = 1.0
    return struct.pack(f"{dim}f", *arr)


def _seed_note_with_vec(conn, emb, tid: int, axis: int):
    """Insert a note row + its notes_vec mirror with a hand-crafted unit
    vector, returning the new note id. Deterministic — no model needed."""
    import time as _t
    cur = conn.execute(
        "INSERT INTO notes (thread_id, content, kind, created_at, embedding) "
        "VALUES (?,?,?,?,?)",
        (tid, f"axis {axis}", "insight", int(_t.time()), emb),
    )
    nid = cur.lastrowid
    from threadkeeper.embeddings import _vec_upsert_note
    _vec_upsert_note(conn, nid, emb)
    return nid


def test_note_delete_syncs_vec_and_search_returns_k(vec_pkg):
    """Deleting a note + _vec_delete_note leaves no orphan vec row, and KNN
    still returns the requested count of live hits."""
    from threadkeeper import embeddings as emb
    from threadkeeper.db import EMBED_DIM
    conn = vec_pkg["db"].get_db()
    tid = _tool(vec_pkg, "open_thread")(question="delete sync")

    ids = [
        _seed_note_with_vec(conn, _unit_blob(axis, EMBED_DIM), tid, axis)
        for axis in range(6)
    ]
    conn.commit()

    # Delete two notes WITH the sync helper.
    for nid in ids[:2]:
        conn.execute("DELETE FROM notes WHERE id=?", (nid,))
        emb._vec_delete_note(conn, nid)
    conn.commit()

    live = {r["id"] for r in conn.execute("SELECT id FROM notes").fetchall()}
    vec_ids = {r["id"] for r in conn.execute("SELECT id FROM notes_vec").fetchall()}
    # No orphan: every notes_vec id maps to a live note.
    assert vec_ids <= live
    assert ids[0] not in vec_ids and ids[1] not in vec_ids

    # KNN for axis-2 returns k live hits, axis-2 itself first.
    hits = emb._vec0_notes_search(conn, _unit_blob(2, EMBED_DIM), k=4)
    assert len(hits) == 4
    assert all(h["id"] in live for h in hits)
    assert hits[0]["content"] == "axis 2"


def test_vec0_search_overfetches_past_legacy_orphans(vec_pkg):
    """Pre-existing orphan vec rows (no matching notes row) must not shrink a
    KNN result below k — the over-fetch in _vec0_notes_search compensates."""
    from threadkeeper import embeddings as emb
    from threadkeeper.db import EMBED_DIM
    conn = vec_pkg["db"].get_db()
    tid = _tool(vec_pkg, "open_thread")(question="orphan overfetch")

    live_ids = [
        _seed_note_with_vec(conn, _unit_blob(axis, EMBED_DIM), tid, axis)
        for axis in range(3)
    ]
    # Legacy orphans: vec rows whose note id has no notes row, in the SAME
    # directions as the query so they'd otherwise win the top KNN slots.
    for off, axis in enumerate((0, 1)):
        conn.execute(
            "INSERT OR REPLACE INTO notes_vec(id, embedding) VALUES (?, ?)",
            (10_000 + off, _unit_blob(axis, EMBED_DIM)),
        )
    conn.commit()

    # k=3: a naive fetch(=k) would surface 2 orphans + 1 live, then the join
    # drops the orphans down to 1. Over-fetch must still return 3 live hits.
    hits = emb._vec0_notes_search(conn, _unit_blob(0, EMBED_DIM), k=3)
    assert len(hits) == 3
    assert all(h["id"] in set(live_ids) for h in hits)


def test_consolidate_apply_leaves_no_orphan_vec_rows(vec_pkg):
    """Acceptance: after consolidate() merges (deletes) a duplicate note,
    notes_vec holds no row whose id is absent from notes."""
    if not vec_pkg["config"].SEMANTIC_AVAILABLE:
        pytest.skip("needs an embedding backend to mirror + cosine-merge notes")
    tid = _tool(vec_pkg, "open_thread")(question="dup merge vec sync")
    note = _tool(vec_pkg, "note")
    note(thread_id=tid, content="exactly the same sentence for dedup", kind="insight")
    note(thread_id=tid, content="exactly the same sentence for dedup", kind="insight")
    conn = vec_pkg["db"].get_db()
    assert len({r["id"] for r in conn.execute(
        "SELECT id FROM notes_vec").fetchall()}) >= 2

    _tool(vec_pkg, "consolidate")(dry_run=False, note_cosine=0.95)

    notes_ids = {r["id"] for r in conn.execute("SELECT id FROM notes").fetchall()}
    vec_ids = {r["id"] for r in conn.execute("SELECT id FROM notes_vec").fetchall()}
    assert vec_ids <= notes_ids           # no orphan mirror rows
    assert len(vec_ids) == len(notes_ids)  # the dropped dup's vec row is gone


# ─────────────────────────────────────────────────────────────────────
# EMBED_DIM drift: a wrong-width vector warns instead of silently dying (#85)
# ─────────────────────────────────────────────────────────────────────

def test_vec_upsert_warns_and_skips_on_dim_mismatch(vec_pkg, caplog):
    import logging
    import struct
    from threadkeeper import embeddings as emb
    from threadkeeper.db import EMBED_DIM
    conn = vec_pkg["db"].get_db()

    wrong_dim = EMBED_DIM // 2  # any width != EMBED_DIM
    bad = struct.pack(f"{wrong_dim}f", *([0.1] * wrong_dim))

    emb._dim_mismatch_warned = False  # reset the once-per-process latch
    with caplog.at_level(logging.WARNING, logger="threadkeeper.embeddings"):
        emb._vec_upsert_note(conn, 987654, bad)

    # Insert was skipped (not silently attempted-and-swallowed).
    assert conn.execute(
        "SELECT id FROM notes_vec WHERE id=?", (987654,)
    ).fetchone() is None
    # One actionable, dimension-naming warning was logged.
    msgs = [r.getMessage().lower() for r in caplog.records]
    assert any("vec0" in m and "dim" in m for m in msgs)


def test_embed_dim_is_config_driven(vec_pkg):
    """EMBED_DIM flows from config (THREADKEEPER_EMBED_DIM) so a non-384 model
    can create vec0 tables at the right width."""
    assert vec_pkg["config"].EMBED_DIM == vec_pkg["db"].EMBED_DIM
    assert vec_pkg["config"].EMBED_DIM == vec_pkg["config"].settings.embed_dim


def test_legacy_cosine_still_works_when_vec_absent(mp_with_cid, monkeypatch):
    """Even when the connection can't load vec0 we should still get a
    valid top-k from the Python-side fallback. Verify by patching
    `_vec_on` to False and asserting the result shape + count."""
    pkg = mp_with_cid(_FAKE_CID)
    open_t = _tool(pkg, "open_thread")
    note = _tool(pkg, "note")
    tid = open_t(question="fallback test")
    note(thread_id=tid, content="alpha", kind="insight")
    note(thread_id=tid, content="beta", kind="insight")
    note(thread_id=tid, content="gamma", kind="insight")

    from threadkeeper import embeddings as emb_mod
    monkeypatch.setattr(emb_mod, "_vec_on", lambda: False)
    conn = pkg["db"].get_db()
    hits = emb_mod._cosine_search(conn, "alpha", k=2)
    # Fallback returns valid result set with score field
    assert len(hits) == 2
    for h in hits:
        assert "score" in h
        assert "content" in h
        # float32 dot product on unit vectors can round to 1.0 + ~1e-6;
        # allow a small epsilon.
        assert -1.001 <= h["score"] <= 1.001
