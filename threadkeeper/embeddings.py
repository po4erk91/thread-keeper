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
import sqlite3
import threading
from typing import Optional

from .config import SEMANTIC_AVAILABLE, EMBED_MODEL_NAME
from . import db as _db


def _vec_on() -> bool:
    """Indirect lookup so monkeypatching db.vec_available in tests works."""
    return _db.vec_available()

_model = None
_model_lock = threading.RLock()

def _get_model():
    global _model
    if not SEMANTIC_AVAILABLE:
        return None
    with _model_lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer  # type: ignore
            _model = SentenceTransformer(EMBED_MODEL_NAME)
        return _model


def model_loaded() -> bool:
    """True when this process currently holds the embedding model in RAM."""
    with _model_lock:
        return _model is not None


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

def _embed(text: str) -> Optional[bytes]:
    with _model_lock:
        m = _get_model()
        if m is None:
            return None
        v = m.encode([text], normalize_embeddings=True)[0].astype("float32")
    return v.tobytes()


def _cosine_search(conn: sqlite3.Connection, query: str, k: int) -> list[dict]:
    """Top-k cosine over notes. Uses vec0 ANN when available."""
    with _model_lock:
        m = _get_model()
        if m is None:
            return []
        import numpy as np  # type: ignore
        qv = m.encode([query], normalize_embeddings=True)[0].astype("float32")
    if _vec_on():
        try:
            return _vec0_notes_search(conn, qv.tobytes(), k)
        except sqlite3.OperationalError:
            pass  # fall through to legacy
    # Legacy Python-side path
    rows = conn.execute(
        "SELECT id, content, kind, thread_id, created_at, embedding "
        "FROM notes WHERE embedding IS NOT NULL"
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
                       k: int) -> list[dict]:
    """vec0 KNN over notes_vec, joined back to notes for payload.
    Distance is squared-Euclidean on normalized vectors; we convert to
    cosine score for compatibility with the legacy result shape:
        cos(q, v) = 1 - dist²/2  for unit-norm vectors.
    """
    rows = conn.execute(
        "SELECT n.id, n.content, n.kind, n.thread_id, n.created_at, "
        "       v.distance "
        "FROM notes_vec v "
        "JOIN notes n ON n.id = v.id "
        "WHERE v.embedding MATCH ? AND k = ? "
        "ORDER BY v.distance",
        (qv_blob, max(1, int(k))),
    ).fetchall()
    out = []
    for r in rows:
        score = max(-1.0, min(1.0, 1.0 - (r["distance"] ** 2) / 2.0))
        d = {k_: r[k_] for k_ in ("id", "content", "kind",
                                  "thread_id", "created_at")}
        d["score"] = float(score)
        out.append(d)
    return out


def _dialog_cosine_search(conn, query: str, k: int) -> list[dict]:
    """Top-k cosine over dialog_messages. Uses vec0 ANN when available."""
    with _model_lock:
        m = _get_model()
        if m is None:
            return []
        import numpy as np  # type: ignore
        qv = m.encode([query], normalize_embeddings=True)[0].astype("float32")
    if _vec_on():
        try:
            return _vec0_dialog_search(conn, qv.tobytes(), k)
        except sqlite3.OperationalError:
            pass
    rows = conn.execute(
        "SELECT uuid, role, project, session_id, content, created_at, embedding "
        "FROM dialog_messages WHERE embedding IS NOT NULL"
    ).fetchall()
    if not rows:
        return []
    scored = []
    for r in rows:
        v = np.frombuffer(r["embedding"], dtype="float32")
        scored.append((float(np.dot(qv, v)), r))
    scored.sort(key=lambda x: -x[0])
    return [{"score": s, **dict(r)} for s, r in scored[:k]]


def _vec_upsert_note(conn: sqlite3.Connection, note_id: int,
                     emb_blob: Optional[bytes]) -> None:
    """Mirror a note's embedding into notes_vec. No-op when vec0 isn't
    loaded or the blob is None. Safe to call multiple times — uses
    INSERT OR REPLACE keyed by integer id."""
    if not _vec_on() or emb_blob is None:
        return
    try:
        conn.execute(
            "INSERT OR REPLACE INTO notes_vec(id, embedding) VALUES (?, ?)",
            (note_id, emb_blob),
        )
    except sqlite3.OperationalError:
        pass  # vec0 table missing on this connection — silent fall-through


def _vec_upsert_dialog(conn: sqlite3.Connection, uuid: str,
                       emb_blob: Optional[bytes]) -> None:
    """Mirror a dialog_message embedding into dialog_vec via the uuid map.
    Resolves or assigns a rowid for the given uuid in dialog_vec_map, then
    INSERT-OR-REPLACE keyed by that rowid in dialog_vec."""
    if not _vec_on() or emb_blob is None:
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
                        k: int) -> list[dict]:
    """vec0 KNN over dialog_vec, joined via dialog_vec_map.uuid back to
    dialog_messages for payload."""
    rows = conn.execute(
        "SELECT d.uuid, d.role, d.project, d.session_id, d.content, "
        "       d.created_at, v.distance "
        "FROM dialog_vec v "
        "JOIN dialog_vec_map m ON m.rowid = v.rowid "
        "JOIN dialog_messages d ON d.uuid = m.uuid "
        "WHERE v.embedding MATCH ? AND k = ? "
        "ORDER BY v.distance",
        (qv_blob, max(1, int(k))),
    ).fetchall()
    out = []
    for r in rows:
        score = max(-1.0, min(1.0, 1.0 - (r["distance"] ** 2) / 2.0))
        d = {k_: r[k_] for k_ in ("uuid", "role", "project",
                                  "session_id", "content", "created_at")}
        d["score"] = float(score)
        out.append(d)
    return out

def _fts_search(conn: sqlite3.Connection, query: str,
                k: int) -> list[dict]:
    """FTS5 search over dialog_fts joined to dialog_messages. FTS5 ranks
    by BM25 (lower = better); we keep insertion order from the result for
    RRF (already ranked best-first by FTS5)."""
    try:
        rows = conn.execute(
            "SELECT f.uuid, d.role, d.session_id, d.content, d.created_at "
            "FROM dialog_fts f "
            "JOIN dialog_messages d ON d.uuid = f.uuid "
            "WHERE dialog_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, max(1, int(k))),
        ).fetchall()
    except sqlite3.OperationalError:
        # FTS reserved-char syntax error or table missing
        return []
    return [
        {
            "uuid": r["uuid"],
            "role": r["role"],
            "session_id": r["session_id"],
            "content": r["content"],
            "created_at": r["created_at"],
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
