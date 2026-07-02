"""Step 1 of cross-machine sync: additive schema, node identity, HLC.

All non-destructive — no PK re-id here. Verifies the foundation the sync
protocol builds on. See docs/sync.md."""
from __future__ import annotations


def test_schema_has_sync_tables_and_columns(fresh_mp):
    db = fresh_mp["db"]
    conn = db.get_db()
    try:
        tbls = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"sync_state", "sync_oplog", "sync_peer_vv"} <= tbls
        # additive columns land on every replicated table
        for t in ("notes", "threads", "core_memory", "dialog_messages"):
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
            assert {"hlc", "origin_node", "deleted"} <= cols, t
    finally:
        conn.close()


def test_node_id_persistent(fresh_mp):
    from threadkeeper.sync import identity
    db = fresh_mp["db"]
    c1 = db.get_db()
    n1 = identity.get_node_id(c1)
    c1.close()
    c2 = db.get_db()
    n2 = identity.get_node_id(c2)
    c2.close()
    assert n1 == n2
    assert n1.startswith("N") and len(n1) == 27  # 'N' + 26-char ULID


def test_hlc_strictly_monotonic(fresh_mp):
    from threadkeeper.sync import identity
    db = fresh_mp["db"]
    conn = db.get_db()
    try:
        prev = identity.hlc_now(conn)
        for _ in range(200):
            cur = identity.hlc_now(conn)
            assert cur > prev  # zero-padded => lexical order == causal order
            prev = cur
    finally:
        conn.close()


def test_hlc_update_absorbs_future_remote(fresh_mp):
    from threadkeeper.sync import identity
    db = fresh_mp["db"]
    conn = db.get_db()
    try:
        identity.hlc_now(conn)
        remote = f"{99999999999999:015d}:000000:Nremotenode"
        merged = identity.hlc_update(conn, remote)
        assert merged > remote                     # local clock jumped ahead
        assert identity.hlc_now(conn) > merged     # and keeps advancing
    finally:
        conn.close()


def test_gen_global_id_unique_and_charset():
    from threadkeeper.helpers import gen_global_id, _ulid, _ULID_B32
    ids = [_ulid() for _ in range(2000)]
    assert len(set(ids)) == 2000                   # no collisions
    for i in ids:
        assert len(i) == 26
        assert all(ch in _ULID_B32 for ch in i)
    assert gen_global_id("T").startswith("T")
    assert len(gen_global_id("UC")) == 28          # 2-char prefix + 26
