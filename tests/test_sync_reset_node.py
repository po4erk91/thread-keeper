"""R3-B3: cloned-DB identity reset (tk-sync-reset-node).

Copying a migrated DB onto a second active machine duplicates the sync node_id;
because the version vector is MAX(hlc) per origin, two independent HLC streams
under one origin lose a writer's changes. reset_node gives the clone a fresh
identity while preserving the HLC high-water and leaving historical origins.
"""
from __future__ import annotations

import shutil
import sqlite3


def _build_migrated(dbmod, migrate, path):
    """Materialize + migrate a standalone DB file at `path` (own directory so
    its node.id mirror doesn't collide with another DB's)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    old = dbmod.DB_PATH
    dbmod.DB_PATH = path
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


def _node_id(conn):
    return conn.execute("SELECT node_id FROM sync_state WHERE id=1").fetchone()[0]


def _add_thread(conn, gen, question):
    tid = gen("T")
    conn.execute(
        "INSERT INTO threads(id,question,state,opened_at,last_touched_at)"
        " VALUES(?,?,?,?,?)", (tid, question, "active", 1, 1))
    conn.commit()
    return tid


def _questions(conn):
    return {r[0] for r in conn.execute(
        "SELECT question FROM threads WHERE deleted=0 OR deleted IS NULL")}


def test_reset_gives_distinct_id_and_keeps_history(fresh_mp, tmp_path):
    from threadkeeper.sync import migrate, reset_node
    from threadkeeper.helpers import gen_global_id
    db = fresh_mp["db"]

    src = _build_migrated(db, migrate, tmp_path / "src" / "db.sqlite")
    a = _open(src)
    tid = _add_thread(a, gen_global_id, "from-source")
    src_node = _node_id(a)
    origin_before = a.execute("SELECT origin_node FROM threads WHERE id=?",
                              (tid,)).fetchone()[0]
    a.close()

    # clone the migrated DB and reset the clone's identity
    clone = tmp_path / "clone" / "db.sqlite"
    clone.parent.mkdir(parents=True)
    shutil.copy2(src, clone)
    assert reset_node.reset_node(clone, do_apply=True) == 0

    b = _open(clone)
    try:
        assert _node_id(b) != src_node  # distinct identity
        # historical row origin is unchanged (that history is already present)
        assert b.execute("SELECT origin_node FROM threads WHERE id=?",
                         (tid,)).fetchone()[0] == origin_before
    finally:
        b.close()

    # dry-run writes nothing
    before = _open(clone)
    dry_id = _node_id(before)
    before.close()
    assert reset_node.reset_node(clone, do_apply=False) == 0
    after = _open(clone)
    assert _node_id(after) == dry_id
    after.close()


def test_reset_lets_two_replicas_converge(fresh_mp, tmp_path):
    from threadkeeper.sync import migrate, reset_node, protocol
    from threadkeeper.sync import identity as sync_id
    from threadkeeper.helpers import gen_global_id
    db = fresh_mp["db"]

    src = _build_migrated(db, migrate, tmp_path / "src" / "db.sqlite")
    clone = tmp_path / "clone" / "db.sqlite"
    clone.parent.mkdir(parents=True)
    shutil.copy2(src, clone)
    assert reset_node.reset_node(clone, do_apply=True) == 0

    a, b = _open(src), _open(clone)
    try:
        # independent writes; skew the clone's clock forward
        _add_thread(a, gen_global_id, "on-source")
        future = sync_id._now_ms() + 120_000
        b.execute("UPDATE sync_state SET hlc_phys_ms=?, hlc_counter=0 WHERE id=1",
                  (future,))
        b.commit()
        _add_thread(b, gen_global_id, "on-clone")

        for _ in range(3):
            protocol.sync_pair(a, b)
        for conn in (a, b):
            assert {"on-source", "on-clone"} <= _questions(conn), conn
        # both origins appear in each node's version vector
        for conn in (a, b):
            vv = protocol.version_vector(conn)
            assert len(vv) == 2, vv
    finally:
        a.close()
        b.close()


def test_reset_preserves_hlc_high_water(fresh_mp, tmp_path):
    from threadkeeper.sync import migrate, reset_node, protocol
    from threadkeeper.helpers import gen_global_id
    db = fresh_mp["db"]

    src = _build_migrated(db, migrate, tmp_path / "src" / "db.sqlite")
    conn = _open(src)
    try:
        # apply a remote row carrying a far-future HLC → DB has now observed it
        future_hlc = "999999999999999:000000:Nfuture"
        row = {"id": gen_global_id("T"), "question": "future", "state": "active",
               "opened_at": 1, "last_touched_at": 1,
               "hlc": future_hlc, "origin_node": "Nfuture", "deleted": 0}
        protocol.apply_changes(conn, [{"tbl": "threads", "op": "put",
                                       "hlc": future_hlc, "origin": "Nfuture",
                                       "row": row}])
        conn.close()

        assert reset_node.reset_node(src, do_apply=True) == 0

        # a local write after the reset must still out-rank the future HLC
        conn = _open(src)
        tid = _add_thread(conn, gen_global_id, "local-after-reset")
        local_hlc = conn.execute("SELECT hlc FROM threads WHERE id=?",
                                 (tid,)).fetchone()[0]
        assert local_hlc > future_hlc, f"{local_hlc!r} !> {future_hlc!r}"
    finally:
        conn.close()


def test_reset_replaces_node_id_mirror(fresh_mp, tmp_path):
    from threadkeeper.sync import migrate, reset_node
    from threadkeeper.sync import identity as sync_id
    db = fresh_mp["db"]

    src = _build_migrated(db, migrate, tmp_path / "src" / "db.sqlite")
    mirror = src.parent / "node.id"

    # seed the mirror with the OLD id (get_node_id writes it when missing)
    conn = _open(src)
    old_db = _node_id(conn)
    conn.close()
    mirror.write_text(old_db + "\n")

    assert reset_node.reset_node(src, do_apply=True) == 0

    conn = _open(src)
    new_db = _node_id(conn)
    conn.close()
    assert new_db != old_db
    # mirror replaced with the new id, not left stale
    assert mirror.read_text().strip() == new_db


def test_reset_dry_run_is_read_only_before_sync_migration(fresh_mp, capsys):
    """The additive schema already has an empty sync_state table. A reset
    dry-run must reject that DB without initializing the singleton row."""
    from threadkeeper.sync import reset_node
    db = fresh_mp["db"]
    db.get_db().close()

    conn = sqlite3.connect(str(db.DB_PATH))
    before = conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0]
    conn.close()
    assert before == 0

    assert reset_node.reset_node(db.DB_PATH, do_apply=False) == 2
    captured = capsys.readouterr()
    assert "run tk-sync-migrate first" in captured.err

    conn = sqlite3.connect(str(db.DB_PATH))
    after = conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0]
    conn.close()
    assert after == before
