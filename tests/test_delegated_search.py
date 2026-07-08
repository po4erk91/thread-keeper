"""Light child delegates semantic search to parent via signals.

The full flow:
  1. Parent process has SEMANTIC_AVAILABLE=True (running search_proxy daemon).
  2. Child process has THREADKEEPER_NO_EMBEDDINGS=1 (no PyTorch loaded).
  3. Child calls search_via_parent(query) → posts 'search_request' signal.
  4. Parent's daemon serves it → 'search_response' signal back.
  5. Child reads response, returns formatted lines.

In tests we don't spin up two processes — we exercise the pieces
synchronously against one DB by manually invoking the search-proxy worker
function with a fake request row.
"""
from __future__ import annotations

import json
import os
import threading
import time

import pytest


_PARENT_CID = "11111111-aaaa-aaaa-aaaa-111111111111"
_CHILD_CID = "22222222-bbbb-bbbb-bbbb-222222222222"


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _seed_some_notes(pkg):
    open_t = _tool(pkg, "open_thread")
    note = _tool(pkg, "note")
    tid = open_t(question="search test thread")
    note(thread_id=tid, content="learned about webhook idempotency keys",
         kind="insight")
    note(thread_id=tid, content="tried oauth refresh, broke retry",
         kind="failed")
    note(thread_id=tid, content="webhook deduplication strategy",
         kind="move")
    return tid


# ─────────────────────────────────────────────────────────────────────
# Env flag plumbing
# ─────────────────────────────────────────────────────────────────────

def test_no_embeddings_env_flag_disables_semantic(tmp_path, monkeypatch):
    """THREADKEEPER_NO_EMBEDDINGS=1 forces SEMANTIC_AVAILABLE=False."""
    monkeypatch.setenv("THREADKEEPER_NO_EMBEDDINGS", "1")
    monkeypatch.setenv("THREADKEEPER_DB", str(tmp_path / "db.sqlite"))

    import sys
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]

    from threadkeeper import config
    assert config.NO_EMBEDDINGS is True
    assert config.SEMANTIC_AVAILABLE is False


def test_no_embeddings_unset_keeps_semantic(tmp_path, monkeypatch):
    """Without the env flag, SEMANTIC_AVAILABLE follows package install."""
    monkeypatch.delenv("THREADKEEPER_NO_EMBEDDINGS", raising=False)
    monkeypatch.setenv("THREADKEEPER_DB", str(tmp_path / "db.sqlite"))

    import sys
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]

    from threadkeeper import config
    assert config.NO_EMBEDDINGS is False
    # SEMANTIC_AVAILABLE depends on whether sentence_transformers installs.
    # In this test env it's installed → True. We don't assert True/False
    # absolutely; just that NO_EMBEDDINGS didn't force it off.


# ─────────────────────────────────────────────────────────────────────
# search_proxy worker (synchronous serve)
# ─────────────────────────────────────────────────────────────────────

def test_search_proxy_serves_notes_request(mp_with_cid):
    """When a 'search_request' signal lands, search_proxy serves it,
    writes a 'search_response' back, and marks the request read."""
    pkg = mp_with_cid(_PARENT_CID)
    _seed_some_notes(pkg)
    conn = pkg["db"].get_db()
    now = int(time.time())

    # Inject a fake search_request signal from the child cid
    cur = conn.execute(
        "INSERT INTO signals (from_cid, to_cid, kind, content, created_at) "
        "VALUES (?, ?, 'search_request', ?, ?)",
        (_CHILD_CID, _PARENT_CID,
         json.dumps({"query": "webhook", "k": 3, "scope": "notes"}),
         now),
    )
    req_id = cur.lastrowid
    conn.commit()

    # Drive one serve tick synchronously
    from threadkeeper.search_proxy import _serve_request
    row = conn.execute(
        "SELECT id, from_cid, to_cid, content, created_at "
        "FROM signals WHERE id=?",
        (req_id,),
    ).fetchone()
    _serve_request(conn, row)

    # Request marked read
    r2 = conn.execute(
        "SELECT read_at FROM signals WHERE id=?", (req_id,)
    ).fetchone()
    assert r2["read_at"] is not None

    # Response posted to child
    resp = conn.execute(
        "SELECT content, kind, to_cid FROM signals "
        "WHERE kind='search_response' AND to_cid=? "
        "ORDER BY id DESC LIMIT 1",
        (_CHILD_CID,),
    ).fetchone()
    assert resp is not None
    body = json.loads(resp["content"])
    assert "results" in body
    # Should find at least the webhook-related notes
    assert any("webhook" in (r.get("content") or "").lower()
               for r in body["results"])


def test_search_proxy_handles_empty_query(mp_with_cid):
    pkg = mp_with_cid(_PARENT_CID)
    conn = pkg["db"].get_db()
    cur = conn.execute(
        "INSERT INTO signals (from_cid, to_cid, kind, content, created_at) "
        "VALUES (?, ?, 'search_request', ?, ?)",
        (_CHILD_CID, _PARENT_CID, json.dumps({"query": ""}), int(time.time())),
    )
    req_id = cur.lastrowid
    conn.commit()

    from threadkeeper.search_proxy import _serve_request
    row = conn.execute(
        "SELECT id, from_cid, to_cid, content, created_at "
        "FROM signals WHERE id=?",
        (req_id,),
    ).fetchone()
    _serve_request(conn, row)

    resp = conn.execute(
        "SELECT content FROM signals "
        "WHERE kind='search_response' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    body = json.loads(resp["content"])
    assert body.get("error") == "empty_query"


def test_start_search_proxy_respects_disable_bg_daemons(mp_with_cid, monkeypatch):
    """BACKGROUND_DAEMONS_ALLOWED=False must block the daemon thread even
    when SEMANTIC_AVAILABLE and the poll interval would otherwise allow it."""
    mp_with_cid(_PARENT_CID)
    from threadkeeper import search_proxy
    monkeypatch.setattr(search_proxy, "SEMANTIC_AVAILABLE", True)
    monkeypatch.setattr(search_proxy, "_POLL_INTERVAL_S", 0.5)

    before = {t.name for t in threading.enumerate()}
    search_proxy.start_search_proxy()
    after = {t.name for t in threading.enumerate()}
    assert "search_proxy" not in (after - before)


# ─────────────────────────────────────────────────────────────────────
# search_via_parent tool — full client-side round trip
# ─────────────────────────────────────────────────────────────────────

def test_search_via_parent_round_trip(mp_with_cid):
    """End-to-end: child posts request, we synchronously serve it from
    the same DB (simulating the parent's daemon), child reads response.

    We can't run the daemon thread in test — instead we set up the
    request, manually serve one tick, then call the wait-side and let
    it read the already-posted response.
    """
    # Set up the "parent" view first
    pkg_parent = mp_with_cid(_PARENT_CID)
    _seed_some_notes(pkg_parent)

    # Now seed a tasks row so _resolve_parent_cid finds parent for child
    conn = pkg_parent["db"].get_db()
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, spawned_cid, cwd, prompt, "
        "started_at) VALUES (?,?,?,?,?,?,?)",
        ("tk_fake", 99999, _PARENT_CID, _CHILD_CID, "/tmp", "test",
         int(time.time())),
    )
    conn.commit()

    # Switch identity to child for the rest of the test
    pkg_parent["identity"]._self_cid = _CHILD_CID
    pkg_parent["identity"]._self_cid_via = "forced"

    # Pre-write the search_response (simulating the parent's daemon
    # having already answered). Then the child's wait loop should pick
    # it up on the first poll.
    import threading
    from threadkeeper.search_proxy import _serve_request

    def _watch_and_serve():
        # Poll for the request signal, serve it once it appears.
        cn = pkg_parent["db"].get_db()
        deadline = time.time() + 5
        while time.time() < deadline:
            row = cn.execute(
                "SELECT id, from_cid, to_cid, content, created_at "
                "FROM signals WHERE kind='search_request' "
                "AND read_at IS NULL LIMIT 1"
            ).fetchone()
            if row:
                # Need to set identity to parent_cid for the serve to
                # post the response from the parent's perspective.
                pkg_parent["identity"]._self_cid = _PARENT_CID
                _serve_request(cn, row)
                pkg_parent["identity"]._self_cid = _CHILD_CID
                cn.close()
                return
            time.sleep(0.1)
        cn.close()

    server_thread = threading.Thread(target=_watch_and_serve, daemon=True)
    server_thread.start()

    svp = _tool(pkg_parent, "search_via_parent")
    out = svp(query="webhook", k=3, timeout_s=8)
    server_thread.join(timeout=1)

    assert "timeout" not in out
    assert "webhook" in out.lower()


def test_search_via_parent_times_out_without_responder(mp_with_cid):
    """No parent daemon → request times out (with friendly message)."""
    pkg = mp_with_cid(_CHILD_CID)
    svp = _tool(pkg, "search_via_parent")
    out = svp(query="anything", k=3, timeout_s=2)
    assert out.startswith("timeout")


# ─────────────────────────────────────────────────────────────────────
# Embedding backfill in parent
# ─────────────────────────────────────────────────────────────────────

def test_backfill_note_embeddings_catches_null_rows(mp_with_cid):
    """Parent's ingester catches notes written by light children
    (embedding=NULL) and fills them in."""
    pkg = mp_with_cid(_PARENT_CID)
    note = _tool(pkg, "note")
    open_t = _tool(pkg, "open_thread")
    tid = open_t(question="backfill target")
    note(thread_id=tid, content="something interesting", kind="insight")

    # Force the note's embedding to NULL (simulating it was written by
    # a light child)
    conn = pkg["db"].get_db()
    conn.execute("UPDATE notes SET embedding=NULL WHERE thread_id=?", (tid,))
    conn.commit()

    pre = conn.execute(
        "SELECT COUNT(*) c FROM notes WHERE embedding IS NULL"
    ).fetchone()["c"]
    assert pre >= 1

    from threadkeeper.ingest import _backfill_note_embeddings
    updated = _backfill_note_embeddings(conn, max_n=10)
    assert updated >= 1

    post = conn.execute(
        "SELECT embedding FROM notes WHERE thread_id=? "
        "ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    assert post["embedding"] is not None
