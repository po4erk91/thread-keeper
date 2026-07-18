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
