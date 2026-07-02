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
    try:
        dbmod.get_db().close()
        assert migrate.apply(path, do_apply=True) == 0
    finally:
        dbmod.DB_PATH = old
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
        winner = c.execute("SELECT hlc FROM threads WHERE id=?", (ta,)).fetchone()[0]
        # C edited after A (later wall/HLC in this sequential test) → C wins
        for _ in range(3):
            protocol.sync_pair(a, b)
            protocol.sync_pair(b, c)
        for conn in (a, b, c):
            q = conn.execute("SELECT question FROM threads WHERE id=?", (ta,)).fetchone()[0]
            assert q == "edited-on-C", conn

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
