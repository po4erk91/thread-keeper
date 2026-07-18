"""Anti-entropy replication protocol (version-vector + LWW).

Two nodes reconcile by exchanging a *version vector* — the highest HLC each
has seen per origin node — and then sending each other every replicated row
(and tombstone) the peer is missing. Because a row carries its ORIGIN's hlc
(not the sender's), relaying is transitive: B forwards A's rows to C without a
direct A–C link. Merges are last-writer-wins by HLC; deletes propagate as
tombstones so a peer that still holds a row cannot resurrect it.

Derived indexes (FTS5, vec) are never shipped — they are rebuilt locally from
the base rows after a merge. Embedding BLOBs are omitted from the wire payload
and recomputed locally too (every node has the model), keeping changesets small
and JSON-serializable.

`version_vector`, `collect_changes`, and `apply_changes` are the primitives the
HTTP transport (sync/server.py, sync/daemon.py) calls; `sync_pair` runs a full
bidirectional reconcile between two local connections (used by tests and the
loopback path).
"""
from __future__ import annotations

import json
import sqlite3

from .capture import _PK, _COMPOSITE, applying_guard

# Columns never shipped: recomputed locally from content on each node.
_PAYLOAD_EXCLUDE = {"embedding", "embed_backend"}
# Cap on rows re-embedded synchronously in rebuild_derived (the /sync/push
# request path). A large initial corpus would otherwise blow the client's ~30s
# timeout and retry into the same expensive path. NULL embeddings are a valid
# eventual state; the background ingester finishes the remainder in bounded ticks.
_SYNC_EMBED_PUSH_BUDGET = 200
_ALL_TABLES = list(_PK) + list(_COMPOSITE)
# Replicated tables whose embedding is derived from a text column — used to
# preserve an existing local embedding across an upsert when the source text is
# unchanged (the wire payload never carries the embedding itself).
_EMBED_TEXT_COL = {
    "notes": "content", "dialog_messages": "content", "concepts": "description",
}


def _pk_where(table: str):
    """(where_sql, key_extractor(rowdict)) for a table's primary key."""
    if table in _COMPOSITE:
        a, b = _COMPOSITE[table]
        return f"{a}=? AND {b}=?", (lambda r: (r[a], r[b]))
    col = _PK[table]
    return f"{col}=?", (lambda r: (r[col],))


def version_vector(conn: sqlite3.Connection) -> dict:
    """Highest hlc seen per origin node, across all replicated rows + delete
    tombstones. Missing origin => '' (peer sends everything for it)."""
    vv: dict[str, str] = {}
    for t in _ALL_TABLES:
        try:
            rows = conn.execute(
                f"SELECT origin_node AS o, MAX(hlc) AS m FROM {t} "
                f"WHERE origin_node IS NOT NULL GROUP BY origin_node"
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        for r in rows:
            o, m = r[0], r[1]
            if m and (o not in vv or m > vv[o]):
                vv[o] = m
    for r in conn.execute(
        "SELECT origin_node AS o, MAX(hlc) AS m FROM sync_oplog "
        "WHERE op='del' GROUP BY origin_node"
    ).fetchall():
        o, m = r[0], r[1]
        if m and (o not in vv or m > vv[o]):
            vv[o] = m
    return vv


def _row_payload(table: str, row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys() if k not in _PAYLOAD_EXCLUDE}


def collect_changes(conn: sqlite3.Connection, peer_vv: dict) -> list[dict]:
    """Every row/tombstone whose hlc exceeds what the peer knows for its origin."""
    conn.row_factory = sqlite3.Row
    out: list[dict] = []
    for t in _ALL_TABLES:
        try:
            rows = conn.execute(
                f"SELECT * FROM {t} WHERE origin_node IS NOT NULL"
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        for r in rows:
            o, h = r["origin_node"], r["hlc"]
            if h and h > peer_vv.get(o, ""):
                out.append({"tbl": t, "op": "put", "hlc": h,
                            "origin": o, "row": _row_payload(t, r)})
    for r in conn.execute(
        "SELECT tbl,gid,hlc,origin_node FROM sync_oplog WHERE op='del'"
    ).fetchall():
        o, h = r["origin_node"], r["hlc"]
        if h and h > peer_vv.get(o, ""):
            out.append({"tbl": r["tbl"], "op": "del", "hlc": h,
                        "origin": o, "gid": r["gid"]})
    return out


def _local_hlc(conn, table, key_where, key_vals):
    row = conn.execute(
        f"SELECT hlc FROM {table} WHERE {key_where}", key_vals
    ).fetchone()
    return row[0] if row else None


def _has_newer_tombstone(conn, table, gid, hlc) -> bool:
    row = conn.execute(
        "SELECT MAX(hlc) FROM sync_oplog WHERE tbl=? AND gid=? AND op='del'",
        (table, gid),
    ).fetchone()
    return bool(row and row[0] and row[0] >= hlc)


def apply_changes(conn: sqlite3.Connection, changes: list[dict]) -> int:
    """Merge a peer changeset (LWW by hlc). Runs under applying_guard so the
    capture triggers don't re-log these as local writes. Returns count applied.

    Before committing, every received HLC is absorbed into the local clock so a
    subsequent local edit is stamped from a clock that already dominates the
    merged rows — otherwise the local write lands with a lower HLC and the next
    reconcile drops it via LWW. Done inside the guard's transaction so the clock
    advance is atomic with the applied rows."""
    from . import identity as sync_identity

    n = 0
    max_hlc = ""
    with applying_guard(conn):
        for c in changes:
            t = c["tbl"]
            if t not in _PK and t not in _COMPOSITE:
                continue
            where, keyfn = _pk_where(t)
            hlc = c["hlc"]
            if hlc and hlc > max_hlc:
                max_hlc = hlc
            if c["op"] == "del":
                gid = c["gid"]
                # resolve pk value(s) from gid (composite is 'a:b')
                if t in _COMPOSITE:
                    kv = tuple(gid.split(":", 1))
                else:
                    kv = (gid,)
                local = _local_hlc(conn, t, where, kv)
                if local is None or hlc > local:
                    conn.execute(f"DELETE FROM {t} WHERE {where}", kv)
                # persist tombstone for relay + resurrection guard
                conn.execute(
                    "INSERT INTO sync_oplog(tbl,gid,op,hlc,origin_node) "
                    "VALUES(?,?,?,?,?)", (t, gid, "del", hlc, c["origin"]))
                n += 1
                continue
            # put
            row = dict(c["row"])
            keyvals = keyfn(row)
            gid = ":".join(str(v) for v in keyvals)
            if _has_newer_tombstone(conn, t, gid, hlc):
                continue  # deleted later than this write — stay deleted
            local = _local_hlc(conn, t, where, keyvals)
            if local is not None and hlc <= local:
                continue  # local copy is newer-or-equal
            # embedding/embed_backend are never shipped, so INSERT OR REPLACE
            # would NULL any existing local embedding. Preserve it when the row's
            # embed-source text is unchanged; a content change leaves it NULL for
            # rebuild_derived to recompute.
            etext = _EMBED_TEXT_COL.get(t)
            preserve = None
            if etext is not None:
                lr = conn.execute(
                    f"SELECT {etext} AS tc, embedding, embed_backend "
                    f"FROM {t} WHERE {where}", keyvals
                ).fetchone()
                if lr is not None and lr[1] is not None and lr[0] == row.get(etext):
                    preserve = (lr[1], lr[2])
            cols = list(row.keys())
            placeholders = ",".join("?" for _ in cols)
            conn.execute(
                f"INSERT OR REPLACE INTO {t} ({','.join(cols)}) "
                f"VALUES ({placeholders})",
                [row[k] for k in cols],
            )
            if preserve is not None:
                conn.execute(
                    f"UPDATE {t} SET embedding=?, embed_backend=? WHERE {where}",
                    (preserve[0], preserve[1], *keyvals),
                )
            n += 1
        # Absorb the highest HLC just received so the next local write is
        # stamped from a clock that dominates it (LWW would otherwise drop the
        # local edit). Inside the guard's transaction → atomic with the rows.
        if max_hlc:
            sync_identity.hlc_absorb(conn, max_hlc)
    conn.commit()
    return n


def rebuild_derived(conn: sqlite3.Connection) -> None:
    """Rebuild FTS5 + vec locally after a merge (idempotent, self-healing).

    Wrapped in applying_guard because these helpers write incidental control
    state to a replicated table (e.g. the `style['fts_backfilled']` marker);
    without the guard the capture triggers would log that as a fresh local
    change on every sync, making reconcile non-idempotent and churning the
    oplog forever."""
    with applying_guard(conn):
        try:
            conn.execute("INSERT INTO notes_fts(notes_fts) VALUES('rebuild')")
        except sqlite3.OperationalError:
            pass
        try:
            from ..ingest import (
                _backfill_dialog_fts_if_empty, _backfill_vec_tables,
                _backfill_sync_embeddings,
            )
            _backfill_dialog_fts_if_empty(conn)
            # Re-embed synced rows (notes/dialog/concepts) that arrived without
            # an embedding BEFORE mirroring BLOBs into the vec indexes. Bounded
            # so this request path can't time out on a large corpus; the
            # background ingester finishes the remainder in later ticks.
            _backfill_sync_embeddings(conn, max_rows=_SYNC_EMBED_PUSH_BUDGET)
            while _backfill_vec_tables(conn)[0]:
                pass
        except Exception:
            pass
        conn.commit()


def sync_pair(a: sqlite3.Connection, b: sqlite3.Connection) -> tuple[int, int]:
    """Full bidirectional reconcile between two local connections. Returns
    (applied_into_a, applied_into_b). Convergent + idempotent."""
    vv_a, vv_b = version_vector(a), version_vector(b)
    into_a = apply_changes(a, collect_changes(b, vv_a))
    into_b = apply_changes(b, collect_changes(a, vv_b))
    rebuild_derived(a)
    rebuild_derived(b)
    return into_a, into_b


# Wire helpers: changesets are plain dicts, so JSON is enough.
def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def loads(s: str):
    return json.loads(s)
