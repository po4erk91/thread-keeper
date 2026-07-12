"""Search-proxy daemon: parent processes (SEMANTIC_AVAILABLE=True) serve
semantic-search requests from spawned slim children (SEMANTIC_AVAILABLE=False).

Mechanism:
- A child without embeddings posts a `signals` row with kind='search_request'
  addressed to the parent's cid. Payload is JSON: {query, k, mode, scope}.
- This daemon, running ONLY in processes where SEMANTIC_AVAILABLE=True,
  polls signals every 500ms for unread 'search_request' rows addressed to
  me (or broadcast). For each, runs the requested search and writes back
  a 'search_response' signal to the requester. Marks the request read.
- The child's `search_via_parent` MCP tool wraps post + wait.

Why this exists: loading sentence-transformers in every spawned child costs
~300-500MB. Most spawned children only need to *write* a few notes/skills;
they rarely need to *search* semantically. When they do, delegating to
the existing parent is far cheaper than each child loading its own model.

Daemon is started lazily on first _ensure_session() call. No-op when
SEMANTIC_AVAILABLE=False — children's daemons stay silent, so each request
is answered by exactly one parent (or zero if none exists, in which case
the child's tool times out and falls back to FTS).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

from .config import BACKGROUND_DAEMONS_ALLOWED, SEMANTIC_AVAILABLE
from .db import get_db
from . import identity

logger = logging.getLogger(__name__)

_started = False
_POLL_INTERVAL_S = float(
    __import__("os").environ.get("THREADKEEPER_SEARCH_PROXY_POLL_S", "0.5")
)

# Maximum number of requests per poll tick — guard against runaway loops.
_MAX_BATCH = 10


def _serve_request(conn, sig_row) -> None:
    """Run the requested search and post a 'search_response' signal back."""
    from .embeddings import _cosine_search, _dialog_cosine_search, _fts_search
    from .config import SEMANTIC_AVAILABLE as _sa

    try:
        payload = json.loads(sig_row["content"])
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    query = str(payload.get("query", "")).strip()
    if not query:
        _write_response(conn, sig_row, {"error": "empty_query", "results": []})
        return

    k = int(payload.get("k", 5) or 5)
    if k <= 0 or k > 100:
        k = 5
    scope = str(payload.get("scope", "notes")).lower()  # 'notes' | 'dialog'
    mode = str(payload.get("mode", "hybrid")).lower()   # for dialog only

    hits: list[dict] = []
    try:
        if scope == "dialog":
            sem = _dialog_cosine_search(conn, query, k * 3) if _sa else []
            fts = _fts_search(conn, query, k * 3)
            if mode == "semantic":
                hits = sem[:k]
            elif mode == "fts":
                hits = fts[:k]
            else:
                from .embeddings import _rrf_combine
                hits = _rrf_combine([sem, fts], top_n=k)
        else:
            hits = _cosine_search(conn, query, k) if _sa else []
    except Exception as e:
        logger.debug("search_proxy serve failed: %s", e, exc_info=True)
        _write_response(conn, sig_row, {"error": str(e), "results": []})
        return

    # Trim payload: drop embedding blob, cap content length to keep signal small.
    out = []
    for h in hits:
        h2 = {k_: v for k_, v in h.items()
              if k_ not in ("embedding",)}
        if isinstance(h2.get("content"), str) and len(h2["content"]) > 400:
            h2["content"] = h2["content"][:400] + "…"
        out.append(h2)
    _write_response(conn, sig_row, {"results": out, "scope": scope})


def _write_response(conn, request_row, body: dict) -> None:
    """Post a kind='search_response' whisper back to the requester and mark
    the original request read."""
    now = int(time.time())
    self_cid = identity._detect_self_cid() or ""
    requester = request_row["from_cid"]
    try:
        conn.execute(
            "INSERT INTO signals (from_cid, to_cid, kind, content, created_at) "
            "VALUES (?, ?, 'search_response', ?, ?)",
            (self_cid, requester, json.dumps(body), now),
        )
        conn.execute(
            "UPDATE signals SET read_at=? WHERE id=?",
            (now, request_row["id"]),
        )
        conn.commit()
    except Exception as e:
        logger.debug("search_proxy write_response failed: %s", e, exc_info=True)


def _serve_loop() -> None:
    while True:
        try:
            self_cid = identity._detect_self_cid()
            if not self_cid:
                time.sleep(_POLL_INTERVAL_S)
                continue
            conn = get_db()
            rows = conn.execute(
                "SELECT id, from_cid, to_cid, content, created_at "
                "FROM signals "
                "WHERE kind='search_request' AND read_at IS NULL "
                "  AND (to_cid = ? OR to_cid IS NULL) "
                "  AND from_cid != ? "
                "ORDER BY id ASC LIMIT ?",
                (self_cid, self_cid, _MAX_BATCH),
            ).fetchall()
            for r in rows:
                _serve_request(conn, r)
            conn.close()
        except Exception:
            logger.debug("search_proxy loop tick failed", exc_info=True)
        time.sleep(_POLL_INTERVAL_S)


def start_search_proxy() -> None:
    """Idempotent daemon-thread starter. No-op when SEMANTIC_AVAILABLE=False
    so light children don't compete with the parent to answer requests."""
    global _started
    if _started:
        return
    if not SEMANTIC_AVAILABLE:
        return
    if _POLL_INTERVAL_S <= 0:
        return  # disabled via env (test environments, or explicit opt-out)
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    t = threading.Thread(
        target=_serve_loop, name="search_proxy", daemon=True,
    )
    t.start()
    _started = True
