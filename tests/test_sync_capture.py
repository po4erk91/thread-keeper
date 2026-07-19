"""Step 3: change capture. On a migrated DB, local writes to replicated tables
are stamped with hlc/origin and logged to sync_oplog by triggers; the apply
guard suppresses that so merging a peer's rows is not re-captured."""
from __future__ import annotations


def _fresh_migrated(fresh_mp):
    from threadkeeper.sync import migrate
    db = fresh_mp["db"]
    db.get_db().close()  # materialize the DB file + schema first
    assert migrate.apply(db.DB_PATH, do_apply=True) == 0
    return db


def test_capture_stamps_and_logs(fresh_mp):
    from threadkeeper.sync import identity
    from threadkeeper.helpers import gen_global_id
    db = _fresh_migrated(fresh_mp)
    conn = db.get_db()
    try:
        node = identity.get_node_id(conn)
        tid = gen_global_id("T")
        conn.execute(
            "INSERT INTO threads(id,question,state,opened_at,last_touched_at)"
            " VALUES(?,?,?,?,?)", (tid, "q", "active", 1, 1))
        conn.commit()

        row = conn.execute(
            "SELECT hlc,origin_node FROM threads WHERE id=?", (tid,)).fetchone()
        assert row["origin_node"] == node and row["hlc"]
        op = conn.execute(
            "SELECT tbl,op FROM sync_oplog WHERE gid=?", (tid,)).fetchone()
        assert op["tbl"] == "threads" and op["op"] == "put"

        # a local UPDATE is a fresh write → advances hlc + logs again
        conn.execute("UPDATE threads SET state='closed' WHERE id=?", (tid,))
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM sync_oplog WHERE gid=?", (tid,)).fetchone()[0] >= 2
        h2 = conn.execute("SELECT hlc FROM threads WHERE id=?", (tid,)).fetchone()[0]
        assert h2 > row["hlc"]

        # a DELETE leaves a tombstone
        conn.execute("DELETE FROM threads WHERE id=?", (tid,))
        conn.commit()
        assert conn.execute(
            "SELECT op FROM sync_oplog WHERE gid=? ORDER BY seq DESC LIMIT 1",
            (tid,)).fetchone()[0] == "del"
    finally:
        conn.close()


def test_applying_guard_suppresses_capture(fresh_mp):
    from threadkeeper.sync import capture
    from threadkeeper.helpers import gen_global_id
    db = _fresh_migrated(fresh_mp)
    conn = db.get_db()
    try:
        before = conn.execute("SELECT COUNT(*) FROM sync_oplog").fetchone()[0]
        with capture.applying_guard(conn):
            # simulate applying a peer row: id + origin + hlc all pre-set
            conn.execute(
                "INSERT INTO threads(id,question,state,origin_node,hlc,"
                "opened_at,last_touched_at) VALUES(?,?,?,?,?,?,?)",
                (gen_global_id("T"), "peer-q", "active", "Npeerxxx",
                 "000000000000001:000000:Npeerxxx", 1, 1))
            conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM sync_oplog").fetchone()[0]
        assert after == before  # nothing captured while applying
        # and the peer's origin/hlc were preserved, not overwritten
        r = conn.execute(
            "SELECT origin_node,hlc FROM threads WHERE question='peer-q'").fetchone()
        assert r["origin_node"] == "Npeerxxx"
    finally:
        conn.close()


def test_applying_guard_is_connection_local(fresh_mp):
    """Blocker #3 regression: the apply guard must suppress capture on ITS
    connection only. A concurrent local write on another connection must still
    be captured — even when the guarded connection commits mid-guard, as
    rebuild_derived's internal backfills do."""
    import sqlite3
    from threadkeeper.sync import capture
    from threadkeeper.helpers import gen_global_id
    db = _fresh_migrated(fresh_mp)
    a = sqlite3.connect(str(db.DB_PATH))
    b = sqlite3.connect(str(db.DB_PATH))
    try:
        tid = gen_global_id("T")
        with capture.applying_guard(a):
            # A commits WHILE the guard is active — mimics an inner commit in
            # the guarded path leaking a shared suppression flag to others.
            a.commit()
            # B makes an ordinary local write and commits.
            b.execute(
                "INSERT INTO threads(id,question,state,opened_at,last_touched_at)"
                " VALUES(?,?,?,?,?)", (tid, "local-on-B", "active", 1, 1))
            b.commit()
        # B's row must be fully captured despite A being mid-guard: stamped with
        # origin/hlc and logged to the oplog exactly once.
        row = b.execute(
            "SELECT origin_node, hlc FROM threads WHERE id=?", (tid,)).fetchone()
        assert row is not None and row[0] is not None and row[1], row
        n = b.execute(
            "SELECT COUNT(*) FROM sync_oplog WHERE gid=? AND op='put'", (tid,)
        ).fetchone()[0]
        assert n == 1, n
    finally:
        a.close()
        b.close()


def test_single_write_logs_one_oplog_put(fresh_mp):
    """A local insert must log exactly one oplog 'put', and a later edit exactly
    one more. The AFTER-INSERT trigger's own stamping UPDATE fires the
    AFTER-UPDATE trigger (recursive_triggers OFF only blocks self-recursion), so
    without the OLD.origin_node guard each insert was double-logged."""
    from threadkeeper.helpers import gen_global_id
    db = _fresh_migrated(fresh_mp)
    conn = db.get_db()
    try:
        tid = gen_global_id("T")
        conn.execute(
            "INSERT INTO threads(id,question,state,opened_at,last_touched_at)"
            " VALUES(?,?,?,?,?)", (tid, "q", "active", 1, 1))
        conn.commit()
        puts = conn.execute(
            "SELECT COUNT(*) FROM sync_oplog WHERE gid=? AND op='put'", (tid,)
        ).fetchone()[0]
        assert puts == 1, puts

        conn.execute("UPDATE threads SET state='closed' WHERE id=?", (tid,))
        conn.commit()
        puts = conn.execute(
            "SELECT COUNT(*) FROM sync_oplog WHERE gid=? AND op='put'", (tid,)
        ).fetchone()[0]
        assert puts == 2, puts  # exactly one more for the edit
    finally:
        conn.close()
