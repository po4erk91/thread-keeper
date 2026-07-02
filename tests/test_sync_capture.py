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
