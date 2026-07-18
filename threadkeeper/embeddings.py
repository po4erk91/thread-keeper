"""Embedding model loader, vectorization, and cosine/FTS/RRF search primitives
over notes and dialog_messages.

Two cosine paths:
- Fast: sqlite-vec `vec0` virtual tables (notes_vec, dialog_vec) when the
  extension is loaded. Sub-linear search via the vec0 KNN backend.
- Fallback: legacy Python-side dot product over BLOB column. Used when
  sqlite-vec isn't available (extension build disabled / package missing).
  Correct, just slower at scale.

Embeddings are dual-written: every new note/dialog_message gets its
vector in BOTH the BLOB column AND the vec0 virtual table, so the legacy
path keeps working and we can roll back without data loss. Old rows are
backfilled to vec0 lazily by the ingester.
"""
import logging
from importlib import metadata as importlib_metadata
import sqlite3
import threading
import time
import warnings
from typing import Optional

from .config import (
    SEMANTIC_AVAILABLE,
    EMBED_MODEL_NAME,
    EMBED_BACKEND,
    EMBED_DIM,
    FASTEMBED_MODEL_ID,
)
from . import db as _db
from . import host_embed

logger = logging.getLogger(__name__)


def _vec_on() -> bool:
    """Indirect lookup so monkeypatching db.vec_available in tests works."""
    return _db.vec_available()


# Emit the dimension-mismatch warning at most once per process — a mismatched
# model would otherwise log on every single note/dialog insert.
_dim_mismatch_warned = False


def _vec_dim_ok(emb_blob: bytes) -> bool:
    """True when `emb_blob`'s float32 width matches the dimension the vec0
    tables were created with (EMBED_DIM).

    A user-configurable `THREADKEEPER_EMBED_MODEL` can emit vectors of a width
    other than the hardcoded FLOAT[EMBED_DIM] the `*_vec` tables were CREATEd
    with. Every such `INSERT ... INTO notes_vec` raises OperationalError; left
    unchecked it is silently swallowed, so vec0 stays empty while `_vec_on()`
    still claims the fast path is live and `tk-migrate-embeddings` (same-dim
    only) never notices. We surface ONE actionable warning instead and let the
    caller skip the insert so the legacy BLOB cosine path carries the load."""
    expected = EMBED_DIM * 4  # float32 = 4 bytes/elem
    if len(emb_blob) == expected:
        return True
    global _dim_mismatch_warned
    if not _dim_mismatch_warned:
        _dim_mismatch_warned = True
        got = len(emb_blob) // 4
        logger.warning(
            "vec0 mirror disabled: embed model %r emits %d-dim vectors but the "
            "notes_vec/dialog_vec tables are FLOAT[%d]. Set THREADKEEPER_EMBED_DIM=%d "
            "(then drop & recreate the *_vec tables) to enable the fast KNN path; "
            "falling back to the legacy Python cosine path until then.",
            EMBED_MODEL_NAME, got, EMBED_DIM, got,
        )
    return False

_model = None
_model_lock = threading.RLock()
_last_used_at = 0.0

def _get_model():
    """Lazily load and cache the embedding model for the active backend.

    'onnx' (default) → fastembed.TextEmbedding (ONNX Runtime, no PyTorch).
    'sentence-transformers' → the legacy PyTorch path (opt-in fallback).
    """
    global _model
    if not SEMANTIC_AVAILABLE:
        return None
    with _model_lock:
        if _model is None:
            if EMBED_BACKEND == "sentence-transformers":
                from sentence_transformers import SentenceTransformer  # type: ignore
                _model = SentenceTransformer(EMBED_MODEL_NAME)
            else:  # 'onnx' (default)
                from fastembed import TextEmbedding  # type: ignore
                # fastembed 0.8 intentionally moved this model to mean pooling.
                # Our dependency pin + generation fingerprint make that change
                # explicit and safely migratable, so its generic legacy warning
                # is noise for every MCP process.
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore",
                        message=r"The model .* now uses mean pooling .*",
                        category=UserWarning,
                    )
                    _model = TextEmbedding(model_name=FASTEMBED_MODEL_ID)
        return _model


def model_loaded() -> bool:
    """True when this process currently holds the embedding model in RAM."""
    with _model_lock:
        return _model is not None


def last_used_at() -> float:
    """Wall-clock time of this process's last encode through the model.

    Lets the memory guard tell a HOT model apart from a cold one: with an
    active ingester, an unloaded model is lazily reloaded within seconds, so
    trimming it is net-negative (fresh copy resident while the freed arenas
    are still mapped). 0.0 when the model was never used."""
    return _last_used_at


def unload_model() -> bool:
    """Drop the cached embedding model so GC can reclaim Python references.

    Python allocators and PyTorch may keep arenas mapped, so RSS reduction is
    best-effort. The next semantic call lazily reloads the model.
    """
    global _model
    with _model_lock:
        if _model is None:
            return False
        model = _model
        _model = None
    try:
        to = getattr(model, "to", None)
        if callable(to):
            to("cpu")
    except Exception:
        pass
    del model
    return True

def _encode(texts: list[str]):
    """Backend-agnostic batch encode → L2-normalized float32 array of shape
    (len(texts), EMBED_DIM), or None when semantic search is unavailable.

    Both backends are normalized to unit length here so the dot product used
    by the vec0 and legacy paths equals cosine similarity, regardless of
    whether the backend already normalizes.
    """
    global _last_used_at
    from . import config as _cfg  # read live (hot-reloadable flag)
    if _cfg.DAEMON_HOST_ENABLED and _cfg.PROCESS_ROLE == "server":
        vecs = host_embed.embed_via_host(list(texts), _cfg.HOST_SOCK_PATH)
        if vecs is None:
            if _cfg.THIN_EMBED_FALLBACK == "local":
                pass  # fall through to the local model below
            else:
                return None  # fts fallback: caller degrades to FTS
        else:
            import numpy as np  # type: ignore
            arr = np.asarray(vecs, dtype="float32")
            norms = np.linalg.norm(arr, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            _last_used_at = time.time()
            return (arr / norms).astype("float32")
    with _model_lock:
        m = _get_model()
        if m is None:
            return None
        _last_used_at = time.time()
        import numpy as np  # type: ignore
        if EMBED_BACKEND == "sentence-transformers":
            arr = np.asarray(m.encode(list(texts)), dtype="float32")
        else:  # fastembed generator → stack
            arr = np.asarray(list(m.embed(list(texts))), dtype="float32")
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (arr / norms).astype("float32")


def encode_many(texts: list[str]):
    """Public batch encoder for the migration command. Returns the same
    normalized float32 array as `_encode`, or None when unavailable."""
    return _encode(texts)


def _runtime_major_minor(package: str) -> str:
    try:
        raw = importlib_metadata.version(package)
    except importlib_metadata.PackageNotFoundError:
        return "unknown"
    parts = raw.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else raw


def embedding_fingerprint() -> str:
    """Identity of the vector space used for both documents and queries.

    ``embed_backend='onnx'`` alone is insufficient: model, dimension, pooling
    and runtime changes can produce incompatible vectors while keeping the same
    backend label. Major.minor is intentional; pyproject pins the compatible
    runtime family and avoids a full re-embed for patch-only releases.
    """
    if EMBED_BACKEND == "sentence-transformers":
        runtime = _runtime_major_minor("sentence-transformers")
        model = EMBED_MODEL_NAME
        pooling = "model-default"
    else:
        runtime = _runtime_major_minor("fastembed")
        model = FASTEMBED_MODEL_ID
        pooling = "mean"
    return (
        f"{EMBED_BACKEND}:{model}:dim={EMBED_DIM}:"
        f"pool={pooling}:runtime={runtime}"
    )


def embed_tag(blob: Optional[bytes]) -> Optional[str]:
    """Vector-generation tag stored alongside a freshly written embedding."""
    return embedding_fingerprint() if blob is not None else None


def embedding_index_health(conn: sqlite3.Connection) -> dict[str, int | str]:
    """Return current-generation coverage without mutating/backfilling rows."""
    active = embedding_fingerprint()

    def _count(sql: str, params: tuple = ()) -> int:
        try:
            return int(conn.execute(sql, params).fetchone()[0])
        except sqlite3.OperationalError:
            return 0

    return {
        "generation": active,
        "notes_total": _count("SELECT COUNT(*) FROM notes"),
        "notes_current": _count(
            "SELECT COUNT(*) FROM notes WHERE embedding IS NOT NULL "
            "AND embed_backend=?", (active,),
        ),
        # Post-migration notes_vec is keyed by rowid via notes_vec_map.gid;
        # pre-migration it is keyed directly by the integer note id.
        "notes_vec": _count(
            "SELECT COUNT(*) FROM notes n "
            "JOIN notes_vec_map m ON m.gid=n.id "
            "JOIN notes_vec v ON v.rowid=m.rowid "
            "WHERE n.embed_backend=?", (active,),
        ) if _notes_mapped(conn) else _count(
            "SELECT COUNT(*) FROM notes n JOIN notes_vec v ON v.id=n.id "
            "WHERE n.embed_backend=?", (active,),
        ),
        "dialog_total": _count("SELECT COUNT(*) FROM dialog_messages"),
        "dialog_current": _count(
            "SELECT COUNT(*) FROM dialog_messages WHERE embedding IS NOT NULL "
            "AND embed_backend=?", (active,),
        ),
        "dialog_vec": _count(
            "SELECT COUNT(*) FROM dialog_messages d "
            "JOIN dialog_vec_map m ON m.uuid=d.uuid "
            "JOIN dialog_vec v ON v.rowid=m.rowid "
            "WHERE d.embed_backend=?", (active,),
        ),
    }


def _embed(text: str) -> Optional[bytes]:
    arr = _encode([text])
    if arr is None:
        return None
    return arr[0].astype("float32").tobytes()


def _cosine_search(conn: sqlite3.Connection, query: str, k: int) -> list[dict]:
    """Top-k cosine over notes. Uses vec0 ANN when available."""
    import numpy as np  # type: ignore
    qa = _encode([query])
    if qa is None:
        return []
    qv = qa[0]
    active = embedding_fingerprint()
    if _vec_on():
        try:
            hits = _vec0_notes_search(
                conn, qv.tobytes(), k, embed_backend=active
            )
            # A filtered vec0 query can return fewer than k rows when stale
            # embedding generations or legacy orphans consume KNN slots.
            # Re-score the current generation in Python for an exact fallback.
            if len(hits) >= k:
                return hits
        except sqlite3.OperationalError:
            pass  # fall through to legacy
    # Legacy Python-side path
    rows = conn.execute(
        "SELECT id, content, kind, thread_id, created_at, embedding "
        "FROM notes WHERE embedding IS NOT NULL AND embed_backend=?",
        (active,),
    ).fetchall()
    if not rows:
        return []
    scored = []
    for r in rows:
        v = np.frombuffer(r["embedding"], dtype="float32")
        scored.append((float(np.dot(qv, v)), r))
    scored.sort(key=lambda x: -x[0])
    return [{"score": s, **dict(r)} for s, r in scored[:k]]


def _vec0_notes_search(conn: sqlite3.Connection, qv_blob: bytes,
                       k: int, embed_backend: str = "") -> list[dict]:
    """vec0 KNN over notes_vec, joined back to notes for payload.
    Distance is squared-Euclidean on normalized vectors; we convert to
    cosine score for compatibility with the legacy result shape:
        cos(q, v) = 1 - dist²/2  for unit-norm vectors.

    Over-fetches from vec0: an orphaned vec row (a note deleted before the
    delete-sync existed — notes.id is AUTOINCREMENT so the id is never reused)
    consumes a KNN slot but is dropped by the inner join, shrinking the result
    below `k`. We pull extra candidates so the join still yields `k` live hits,
    then trim. `_vec_delete_note` keeps new deletes clean; this drains any
    legacy orphan backlog gracefully.
    """
    want = max(1, int(k))
    fetch_k = max(want * 8, 40) if embed_backend else want * 2 + 8
    if _notes_mapped(conn):
        # Post-migration: notes.id is TEXT, joined via the notes_vec_map bridge.
        sql = (
            "SELECT n.id, n.content, n.kind, n.thread_id, n.created_at, "
            "       v.distance "
            "FROM notes_vec v "
            "JOIN notes_vec_map m ON m.rowid = v.rowid "
            "JOIN notes n ON n.id = m.gid "
            "WHERE v.embedding MATCH ? AND k = ?"
        )
    else:
        sql = (
            "SELECT n.id, n.content, n.kind, n.thread_id, n.created_at, "
            "       v.distance "
            "FROM notes_vec v "
            "JOIN notes n ON n.id = v.id "
            "WHERE v.embedding MATCH ? AND k = ?"
        )
    params: list = [qv_blob, fetch_k]
    if embed_backend:
        sql += " AND n.embed_backend = ?"
        params.append(embed_backend)
    sql += " ORDER BY v.distance"
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        score = max(-1.0, min(1.0, 1.0 - (r["distance"] ** 2) / 2.0))
        d = {k_: r[k_] for k_ in ("id", "content", "kind",
                                  "thread_id", "created_at")}
        d["score"] = float(score)
        out.append(d)
    return out[:want]


def _dialog_cosine_search(conn, query: str, k: int,
                          role: str = "") -> list[dict]:
    """Top-k cosine over dialog_messages. Uses vec0 ANN when available."""
    import numpy as np  # type: ignore
    qa = _encode([query])
    if qa is None:
        return []
    qv = qa[0]
    active = embedding_fingerprint()
    if _vec_on():
        try:
            hits = _vec0_dialog_search(
                conn, qv.tobytes(), k, role=role, embed_backend=active
            )
            if len(hits) >= k:
                return hits
        except sqlite3.OperationalError:
            pass
    sql = (
        "SELECT uuid, role, project, session_id, content, created_at, embedding "
        "FROM dialog_messages WHERE embedding IS NOT NULL AND embed_backend=?"
    )
    params: tuple = (active,)
    if role:
        sql += " AND role=?"
        params = (active, role)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []
    scored = []
    for r in rows:
        v = np.frombuffer(r["embedding"], dtype="float32")
        scored.append((float(np.dot(qv, v)), r))
    scored.sort(key=lambda x: -x[0])
    return [{"score": s, **dict(r)} for s, r in scored[:k]]


def _notes_mapped(conn: sqlite3.Connection) -> bool:
    """True on a migrated DB where notes.id is TEXT and notes_vec is keyed via
    the notes_vec_map sidecar (mirrors dialog_vec_map)."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='notes_vec_map'"
    ).fetchone() is not None


def _vec_upsert_note(conn: sqlite3.Connection, note_id,
                     emb_blob: Optional[bytes]) -> None:
    """Mirror a note's embedding into notes_vec. No-op when vec0 isn't loaded
    or the blob is None. Pre-migration: keyed by integer notes.id. Post: keyed
    by the notes_vec_map rowid resolved from the note's global TEXT id."""
    if not _vec_on() or emb_blob is None or not _vec_dim_ok(emb_blob):
        return
    try:
        if _notes_mapped(conn):
            gid = str(note_id)
            row = conn.execute(
                "SELECT rowid FROM notes_vec_map WHERE gid=?", (gid,)
            ).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO notes_vec_map(gid) VALUES (?)", (gid,)
                )
                vec_rowid = cur.lastrowid
            else:
                vec_rowid = row[0] if not hasattr(row, "keys") else row["rowid"]
            conn.execute(
                "INSERT OR REPLACE INTO notes_vec(rowid, embedding) VALUES (?, ?)",
                (vec_rowid, emb_blob),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO notes_vec(id, embedding) VALUES (?, ?)",
                (note_id, emb_blob),
            )
    except sqlite3.OperationalError:
        pass  # vec0 table missing on this connection — silent fall-through


def _vec_delete_note(conn: sqlite3.Connection, note_id: int) -> None:
    """Drop a note's row from notes_vec so vec0 stays in sync with notes on
    delete. No-op when vec0 isn't loaded. Mirror of `_vec_upsert_note` for the
    delete path: without it, deleting a note (e.g. consolidate merge) leaves a
    permanent orphan vec row — notes.id is AUTOINCREMENT so the id is never
    reused — that consumes a KNN slot and is then dropped by the join in
    `_vec0_notes_search`, shrinking results below `k` and accumulating dead
    index entries over time."""
    if not _vec_on():
        return
    try:
        if _notes_mapped(conn):
            gid = str(note_id)
            row = conn.execute(
                "SELECT rowid FROM notes_vec_map WHERE gid=?", (gid,)
            ).fetchone()
            if row is not None:
                vec_rowid = row[0] if not hasattr(row, "keys") else row["rowid"]
                conn.execute("DELETE FROM notes_vec WHERE rowid=?", (vec_rowid,))
                conn.execute("DELETE FROM notes_vec_map WHERE gid=?", (gid,))
        else:
            conn.execute("DELETE FROM notes_vec WHERE id=?", (note_id,))
    except sqlite3.OperationalError:
        pass  # vec0 table missing on this connection — silent fall-through


def _vec_upsert_dialog(conn: sqlite3.Connection, uuid: str,
                       emb_blob: Optional[bytes]) -> None:
    """Mirror a dialog_message embedding into dialog_vec via the uuid map.
    Resolves or assigns a rowid for the given uuid in dialog_vec_map, then
    INSERT-OR-REPLACE keyed by that rowid in dialog_vec."""
    if not _vec_on() or emb_blob is None or not _vec_dim_ok(emb_blob):
        return
    try:
        row = conn.execute(
            "SELECT rowid FROM dialog_vec_map WHERE uuid=?", (uuid,)
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO dialog_vec_map(uuid) VALUES (?)", (uuid,)
            )
            vec_rowid = cur.lastrowid
        else:
            vec_rowid = row[0] if not hasattr(row, "keys") else row["rowid"]
        conn.execute(
            "INSERT OR REPLACE INTO dialog_vec(rowid, embedding) VALUES (?, ?)",
            (vec_rowid, emb_blob),
        )
    except sqlite3.OperationalError:
        pass


def _vec0_dialog_search(conn: sqlite3.Connection, qv_blob: bytes,
                        k: int, role: str = "",
                        embed_backend: str = "") -> list[dict]:
    """vec0 KNN over dialog_vec, joined via dialog_vec_map.uuid back to
    dialog_messages for payload."""
    sql = (
        "SELECT d.uuid, d.role, d.project, d.session_id, d.content, "
        "       d.created_at, v.distance "
        "FROM dialog_vec v "
        "JOIN dialog_vec_map m ON m.rowid = v.rowid "
        "JOIN dialog_messages d ON d.uuid = m.uuid "
        "WHERE v.embedding MATCH ? AND k = ?"
    )
    params: list = [qv_blob, max(1, int(k))]
    if role:
        sql += " AND d.role = ?"
        params.append(role)
    if embed_backend:
        sql += " AND d.embed_backend = ?"
        params.append(embed_backend)
    sql += " ORDER BY v.distance"
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        score = max(-1.0, min(1.0, 1.0 - (r["distance"] ** 2) / 2.0))
        d = {k_: r[k_] for k_ in ("uuid", "role", "project",
                                  "session_id", "content", "created_at")}
        d["score"] = float(score)
        out.append(d)
    return out

def _fts_search(conn: sqlite3.Connection, query: str,
                k: int, role: str = "") -> list[dict]:
    """FTS5 search over dialog_fts joined to dialog_messages. FTS5 ranks
    by BM25 (lower = better); we keep insertion order from the result for
    RRF (already ranked best-first by FTS5). dialog_fts is external-content
    (schema v2): rows map back via dialog_fts.rowid == dialog_messages.rowid."""
    from .helpers import _fts_query
    fq = _fts_query(query)
    if not fq:
        return []
    def _run(match_query: str):
        sql = (
            "SELECT d.uuid, d.role, d.project, d.session_id, d.content, "
            "       d.created_at, bm25(dialog_fts) AS lexical_score "
            "FROM dialog_fts f "
            "JOIN dialog_messages d ON d.rowid = f.rowid "
            "WHERE dialog_fts MATCH ?"
        )
        params: list = [match_query]
        if role:
            sql += " AND d.role = ?"
            params.append(role)
        sql += " ORDER BY lexical_score, d.rowid DESC LIMIT ?"
        params.append(max(1, int(k)))
        return conn.execute(sql, params).fetchall()

    try:
        rows = _run(fq)
        if not rows and " " in fq:
            rows = _run(fq.replace(" ", " OR "))
    except sqlite3.OperationalError:
        # FTS reserved-char syntax error or table missing
        return []
    return [
        {
            "uuid": r["uuid"],
            "role": r["role"],
            "project": r["project"],
            "session_id": r["session_id"],
            "content": r["content"],
            "created_at": r["created_at"],
            "lexical_score": float(r["lexical_score"]),
        }
        for r in rows
    ]

def _rrf_combine(lists: list[list[dict]], top_n: int,
                 k_rrf: int = 60) -> list[dict]:
    """Reciprocal Rank Fusion. score = Σ 1/(rank + k_rrf) across input lists.
    De-duplicates by uuid. Returns up to top_n payloads sorted by score."""
    scores: dict[str, float] = {}
    payloads: dict[str, dict] = {}
    for lst in lists:
        for rank, item in enumerate(lst):
            uid = item.get("uuid")
            if not uid:
                continue
            scores[uid] = scores.get(uid, 0.0) + 1.0 / (rank + k_rrf)
            if uid not in payloads:
                payloads[uid] = item
    ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_n]
    return [payloads[uid] for uid, _ in ranked]
