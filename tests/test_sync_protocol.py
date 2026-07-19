"""Step 4: anti-entropy convergence. Three migrated DBs with distinct node
ids write concurrently; reconciling along a chain (A-B, B-C — no direct A-C)
must converge all three to the union, resolve concurrent edits by LWW, and
propagate deletes as tombstones. See docs/sync.md."""
from __future__ import annotations

import sqlite3


def _build_db(dbmod, migrate, path):
    """Materialize + migrate a standalone DB file at `path`."""
    old = dbmod.DB_PATH
    dbmod.DB_PATH = path
    # bootstrap_db latches once per process; force it to re-materialize schema
    # at this fresh path (the fixture only resets the latch once per test).
    dbmod.bootstrap_db(force=True)
    try:
        dbmod.get_db().close()
        assert migrate.apply(path, do_apply=True) == 0
    finally:
        dbmod.DB_PATH = old
        dbmod._BOOTSTRAPPED = False
    return path


def _open(path):
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    return c


def _add_thread(conn, gen, question):
    tid = gen("T")
    conn.execute(
        "INSERT INTO threads(id,question,state,opened_at,last_touched_at)"
        " VALUES(?,?,?,?,?)", (tid, question, "active", 1, 1))
    conn.commit()  # capture trigger stamps hlc/origin
    return tid


def _questions(conn):
    return {r[0] for r in conn.execute(
        "SELECT question FROM threads WHERE deleted=0 OR deleted IS NULL")}


def test_three_node_convergence_lww_and_delete(fresh_mp, tmp_path):
    from threadkeeper.sync import migrate, protocol
    from threadkeeper.helpers import gen_global_id
    db = fresh_mp["db"]

    pa = _build_db(db, migrate, tmp_path / "A.sqlite")
    pb = _build_db(db, migrate, tmp_path / "B.sqlite")
    pc = _build_db(db, migrate, tmp_path / "C.sqlite")
    a, b, c = _open(pa), _open(pb), _open(pc)
    try:
        # concurrent independent writes on each node
        ta = _add_thread(a, gen_global_id, "from-A")
        _add_thread(b, gen_global_id, "from-B")
        _add_thread(c, gen_global_id, "from-C")

        # reconcile along a CHAIN only: A<->B, B<->C. No direct A<->C link.
        for _ in range(3):
            protocol.sync_pair(a, b)
            protocol.sync_pair(b, c)

        # transitive union: every node has all three (A's data reached C via B)
        for conn in (a, b, c):
            assert {"from-A", "from-B", "from-C"} <= _questions(conn)

        # concurrent edit of the SAME row on A and C → LWW by hlc.
        a.execute("UPDATE threads SET question='edited-on-A' WHERE id=?", (ta,))
        a.commit()
        c.execute("UPDATE threads SET question='edited-on-C' WHERE id=?", (ta,))
        c.commit()
        # Concurrent (non-causal) edits: LWW resolves by HLC total order, which
        # is NOT the same as wall order for two sub-millisecond-apart writes on
        # different nodes. Whichever edit carries the higher HLC must win, and
        # every node must converge on it.
        ha = a.execute("SELECT hlc FROM threads WHERE id=?", (ta,)).fetchone()[0]
        hc = c.execute("SELECT hlc FROM threads WHERE id=?", (ta,)).fetchone()[0]
        expected = "edited-on-C" if hc > ha else "edited-on-A"
        for _ in range(3):
            protocol.sync_pair(a, b)
            protocol.sync_pair(b, c)
        for conn in (a, b, c):
            q = conn.execute("SELECT question FROM threads WHERE id=?", (ta,)).fetchone()[0]
            assert q == expected, (conn, q, expected)

        # delete on B propagates everywhere (tombstone), no resurrection
        b.execute("DELETE FROM threads WHERE question='from-B'")
        b.commit()
        for _ in range(3):
            protocol.sync_pair(a, b)
            protocol.sync_pair(b, c)
        for conn in (a, b, c):
            assert "from-B" not in _questions(conn), conn

        # idempotent: another round changes nothing
        assert protocol.sync_pair(a, b) == (0, 0)
    finally:
        a.close(); b.close(); c.close()


def test_receive_advances_hlc_so_later_local_edit_wins(fresh_mp, tmp_path):
    """Blocker #1 regression: after receiving a clock-ahead remote row, a
    subsequent LOCAL edit must carry an HLC greater than the received value —
    otherwise LWW silently drops the user's edit on the next reconcile."""
    from threadkeeper.sync import migrate, protocol
    from threadkeeper.sync import identity as sync_id
    from threadkeeper.helpers import gen_global_id
    db = fresh_mp["db"]

    pa = _build_db(db, migrate, tmp_path / "A.sqlite")
    pb = _build_db(db, migrate, tmp_path / "B.sqlite")
    a, b = _open(pa), _open(pb)
    try:
        # Skew B's clock ~60s into the future, then write a row on B.
        future = sync_id._now_ms() + 60_000
        b.execute("UPDATE sync_state SET hlc_phys_ms=?, hlc_counter=0 WHERE id=1",
                  (future,))
        b.commit()
        tid = _add_thread(b, gen_global_id, "from-B-future")

        # A pulls B's future-clocked row.
        protocol.apply_changes(a, protocol.collect_changes(b, protocol.version_vector(a)))

        # The user edits that row locally on A (A's wall clock is normal).
        a.execute("UPDATE threads SET question='edited-on-A' WHERE id=?", (tid,))
        a.commit()

        a_hlc = a.execute("SELECT hlc FROM threads WHERE id=?", (tid,)).fetchone()[0]
        b_hlc = b.execute("SELECT hlc FROM threads WHERE id=?", (tid,)).fetchone()[0]
        assert a_hlc > b_hlc, f"local edit hlc {a_hlc!r} !> received {b_hlc!r}"

        # Reconcile: both nodes must converge on A's edit (LWW picks the later write).
        for _ in range(2):
            protocol.sync_pair(a, b)
        for conn in (a, b):
            q = conn.execute("SELECT question FROM threads WHERE id=?",
                             (tid,)).fetchone()[0]
            assert q == "edited-on-A", (conn, q)
    finally:
        a.close(); b.close()


def test_sync_recomputes_missing_embeddings(fresh_mp, tmp_path):
    """F4 regression: embeddings are stripped from the wire, so a synced concept
    (or dialog row) lands with NULL embedding and must be re-embedded locally
    during rebuild_derived — else it is invisible to semantic search."""
    from threadkeeper.config import SEMANTIC_AVAILABLE
    if not SEMANTIC_AVAILABLE:
        import pytest
        pytest.skip("needs embeddings")
    from threadkeeper.sync import migrate, protocol
    from threadkeeper.helpers import gen_global_id
    db = fresh_mp["db"]
    pa = _build_db(db, migrate, tmp_path / "A.sqlite")
    pb = _build_db(db, migrate, tmp_path / "B.sqlite")
    a, b = _open(pa), _open(pb)
    try:
        cid = gen_global_id("C")
        a.execute("INSERT INTO concepts(id,description,registered_at) VALUES(?,?,?)",
                  (cid, "octopus camouflage behaviour", 1))
        a.commit()
        protocol.sync_pair(a, b)
        # B received the concept (content) and re-embedded it locally.
        emb = b.execute("SELECT embedding FROM concepts WHERE id=?", (cid,)).fetchone()[0]
        assert emb is not None, "synced concept embedding was not recomputed on B"
    finally:
        a.close()
        b.close()


def test_apply_preserves_local_embedding_on_unchanged_content(fresh_mp):
    """F4 regression: a winning remote put must not clobber the local embedding
    (never shipped) when the embed-source text is unchanged."""
    from threadkeeper.sync import migrate, protocol
    from threadkeeper.helpers import gen_global_id
    db = fresh_mp["db"]
    db.get_db().close()
    assert migrate.apply(db.DB_PATH, do_apply=True) == 0
    conn = db.get_db()
    try:
        cid = gen_global_id("C")
        conn.execute(
            "INSERT INTO concepts(id,description,embedding,embed_backend,registered_at)"
            " VALUES(?,?,?,?,?)", (cid, "same text", b"\x01\x02\x03\x04", "onnx", 1))
        conn.commit()
        # a remote put with the SAME description but a dominating hlc
        row = dict(conn.execute("SELECT * FROM concepts WHERE id=?", (cid,)).fetchone())
        row.pop("embedding", None)
        row.pop("embed_backend", None)
        newer = "999999999999999:000000:Npeer"
        row["hlc"] = newer
        row["origin_node"] = "Npeer"
        assert protocol.apply_changes(
            conn, [{"tbl": "concepts", "op": "put", "hlc": newer,
                    "origin": "Npeer", "row": row}]) == 1
        emb = conn.execute("SELECT embedding FROM concepts WHERE id=?", (cid,)).fetchone()[0]
        assert emb == b"\x01\x02\x03\x04", "local embedding clobbered on unchanged-content upsert"
    finally:
        conn.close()


def test_push_embedding_backfill_is_bounded(fresh_mp, tmp_path):
    """R3-F regression: /sync/push (rebuild_derived) must re-embed at most the
    budget per call so a large corpus can't blow the client timeout; the
    remainder is finished by later (background) passes."""
    from threadkeeper.config import SEMANTIC_AVAILABLE
    if not SEMANTIC_AVAILABLE:
        import pytest
        pytest.skip("needs embeddings")
    from threadkeeper.sync import migrate, protocol
    from threadkeeper.ingest import _backfill_sync_embeddings
    from threadkeeper.helpers import gen_global_id
    db = fresh_mp["db"]
    _build_db(db, migrate, tmp_path / "A.sqlite")
    conn = _open(tmp_path / "A.sqlite")
    try:
        # seed more NULL-embedding concepts than the push budget
        for i in range(5):
            conn.execute(
                "INSERT INTO concepts(id,description,registered_at) VALUES(?,?,?)",
                (gen_global_id("C"), f"anteater fact number {i}", 1))
        conn.commit()

        def _null():
            return conn.execute(
                "SELECT COUNT(*) FROM concepts WHERE embedding IS NULL").fetchone()[0]
        assert _null() == 5

        # one bounded pass processes no more than the budget
        did = _backfill_sync_embeddings(conn, max_rows=2)
        assert did == 2, did
        assert _null() == 3

        # subsequent bounded passes finish the remainder
        _backfill_sync_embeddings(conn, max_rows=2)
        _backfill_sync_embeddings(conn, max_rows=2)
        assert _null() == 0

        # the /sync/push path honors the module budget: shrink it and confirm
        # rebuild_derived leaves the excess for later.
        for i in range(3):
            conn.execute(
                "INSERT INTO concepts(id,description,registered_at) VALUES(?,?,?)",
                (gen_global_id("C"), f"badger fact {i}", 1))
        conn.commit()
        import threadkeeper.sync.protocol as pmod
        old = pmod._SYNC_EMBED_PUSH_BUDGET
        pmod._SYNC_EMBED_PUSH_BUDGET = 1
        try:
            protocol.rebuild_derived(conn)
        finally:
            pmod._SYNC_EMBED_PUSH_BUDGET = old
        assert _null() == 2, "rebuild_derived exceeded the push budget"
    finally:
        conn.close()


def test_background_embedding_backfill_is_not_an_lww_write(
    fresh_mp, tmp_path, monkeypatch,
):
    """A derived embedding update must not re-stamp a replicated note.

    Otherwise a background backfill on B can get a higher HLC than a concurrent
    content edit on A and make the old content win LWW. This covers the actual
    bounded background helper used after /sync/push leaves NULL rows behind.
    """
    import numpy as np

    from threadkeeper import embeddings, ingest
    from threadkeeper.sync import migrate, protocol
    from threadkeeper.sync import identity as sync_id
    from threadkeeper.helpers import gen_global_id

    db = fresh_mp["db"]
    pa = _build_db(db, migrate, tmp_path / "A.sqlite")
    pb = _build_db(db, migrate, tmp_path / "B.sqlite")
    a, b = _open(pa), _open(pb)
    try:
        tid = _add_thread(a, gen_global_id, "embedding-race")
        nid = gen_global_id("")
        a.execute(
            "INSERT INTO notes(id,thread_id,content,kind,created_at) "
            "VALUES(?,?,?,?,?)",
            (nid, tid, "old content", "move", 1),
        )
        a.commit()

        # Model a note beyond the bounded /sync/push slice.
        old_budget = protocol._SYNC_EMBED_PUSH_BUDGET
        protocol._SYNC_EMBED_PUSH_BUDGET = 0
        try:
            protocol.sync_pair(a, b)
        finally:
            protocol._SYNC_EMBED_PUSH_BUDGET = old_budget
        before = b.execute(
            "SELECT origin_node,hlc FROM notes WHERE id=?", (nid,)
        ).fetchone()
        oplog_before = b.execute("SELECT COUNT(*) FROM sync_oplog").fetchone()[0]

        # A real user edit races with B's later derived-only backfill.
        a.execute("UPDATE notes SET content='user edit wins' WHERE id=?", (nid,))
        a.commit()
        future = sync_id._now_ms() + 120_000
        b.execute(
            "UPDATE sync_state SET hlc_phys_ms=?,hlc_counter=0 WHERE id=1",
            (future,),
        )
        b.commit()

        monkeypatch.setattr(fresh_mp["config"], "SEMANTIC_AVAILABLE", True)
        monkeypatch.setattr(
            embeddings,
            "encode_many",
            lambda texts: np.zeros(
                (len(texts), fresh_mp["config"].EMBED_DIM), dtype="float32"
            ),
        )
        assert ingest._backfill_background_embeddings(b, max_rows=20) == 1

        after = b.execute(
            "SELECT origin_node,hlc,embedding FROM notes WHERE id=?", (nid,)
        ).fetchone()
        assert after["embedding"] is not None
        assert (after["origin_node"], after["hlc"]) == (
            before["origin_node"], before["hlc"]
        )
        assert (
            b.execute("SELECT COUNT(*) FROM sync_oplog").fetchone()[0]
            == oplog_before
        )

        # Reconcile the user edit. With no synthetic B-side LWW write, the edit
        # must win on both replicas despite B's future local clock.
        protocol.sync_pair(a, b)
        for conn in (a, b):
            assert conn.execute(
                "SELECT content FROM notes WHERE id=?", (nid,)
            ).fetchone()[0] == "user edit wins"
    finally:
        a.close()
        b.close()
