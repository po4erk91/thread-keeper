"""Hybrid retrieval must remain complete when vector coverage is partial."""
from __future__ import annotations

import time


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _seed_note(pkg, content: str) -> tuple[str, int]:
    tid = _tool(pkg, "open_thread")(question=f"seed {content[:20]}")
    conn = pkg["db"].get_db()
    try:
        cur = conn.execute(
            "INSERT INTO notes(thread_id, content, kind, created_at) "
            "VALUES (?, ?, 'insight', ?)",
            (tid, content, int(time.time())),
        )
        conn.commit()
        return tid, cur.lastrowid
    finally:
        conn.close()


def test_notes_hybrid_uses_fts_when_dense_has_no_coverage(fresh_mp,
                                                          monkeypatch):
    from threadkeeper import retrieval

    _seed_note(fresh_mp, "zebra-quux exact recovery marker")
    monkeypatch.setattr(retrieval.config, "SEMANTIC_AVAILABLE", True)
    monkeypatch.setattr(retrieval, "_cosine_search", lambda *_args, **_kw: [])

    out = _tool(fresh_mp, "search")(query="zebra-quux", k=5)
    assert "zebra-quux" in out
    assert "no_matches" not in out


def test_notes_hybrid_preserves_fusion_provenance(fresh_mp, monkeypatch):
    from threadkeeper import retrieval

    _, exact_id = _seed_note(fresh_mp, "postgres billing database")
    other_tid, other_id = _seed_note(fresh_mp, "generic database discussion")
    monkeypatch.setattr(retrieval.config, "SEMANTIC_AVAILABLE", True)

    def _dense(_conn, _query, _k):
        return [
            {
                "id": other_id, "thread_id": other_tid, "kind": "insight",
                "content": "generic database discussion",
                "created_at": int(time.time()), "score": 0.91,
            },
            {
                "id": exact_id, "thread_id": None, "kind": "insight",
                "content": "postgres billing database",
                "created_at": int(time.time()), "score": 0.89,
            },
        ]

    monkeypatch.setattr(retrieval, "_cosine_search", _dense)
    with fresh_mp["db"].read_db() as conn:
        hits = retrieval.retrieve_notes(conn, "postgres billing", k=2)
    exact = next(hit for hit in hits if hit.id == str(exact_id))
    assert exact.matched_by == {"dense", "fts"}
    assert exact.fused_score is not None
    assert hits[0].id == str(exact_id)


def test_dense_noise_below_threshold_abstains(fresh_mp, monkeypatch):
    from threadkeeper import retrieval

    tid, note_id = _seed_note(fresh_mp, "unrelated nearest neighbour")
    monkeypatch.setattr(retrieval.config, "SEMANTIC_AVAILABLE", True)
    monkeypatch.setattr(
        retrieval,
        "_cosine_search",
        lambda *_args, **_kwargs: [{
            "id": note_id,
            "thread_id": tid,
            "kind": "insight",
            "content": "unrelated nearest neighbour",
            "created_at": int(time.time()),
            "score": retrieval.DENSE_MIN_SCORE - 0.01,
        }],
    )
    with fresh_mp["db"].read_db() as conn:
        hits = retrieval.retrieve_notes(conn, "absent vocabulary", k=5)
    assert hits == []


def test_brief_query_uses_same_hybrid_fallback(fresh_mp, monkeypatch):
    from threadkeeper import retrieval

    _seed_note(fresh_mp, "alpha-omega brief fallback marker")
    monkeypatch.setattr(retrieval.config, "SEMANTIC_AVAILABLE", True)
    monkeypatch.setattr(retrieval, "_cosine_search", lambda *_args, **_kw: [])
    monkeypatch.setenv("THREADKEEPER_BRIEF_NO_THREAD_NUDGE", "1")

    out = _tool(fresh_mp, "brief")(
        query="alpha-omega", k=5, scope="query"
    )
    assert "alpha-omega brief fallback marker" in out


def test_dialog_role_filter_applies_before_limit(fresh_mp):
    conn = fresh_mp["db"].get_db()
    now = int(time.time())
    try:
        for idx in range(45):
            conn.execute(
                "INSERT INTO dialog_messages(uuid, source, project, session_id, "
                "role, content, created_at) VALUES (?,?,?,?,?,?,?)",
                (f"assistant-{idx}", "test", "p", "s", "assistant",
                 f"needle assistant noise {idx}", now + idx),
            )
        conn.execute(
            "INSERT INTO dialog_messages(uuid, source, project, session_id, "
            "role, content, created_at) VALUES (?,?,?,?,?,?,?)",
            ("user-target", "test", "p", "s", "user",
             "needle user target", now - 1),
        )
        conn.commit()
    finally:
        conn.close()

    out = _tool(fresh_mp, "dialog_search")(
        query="needle", k=1, role="user", mode="fts"
    )
    assert "user target" in out
    assert "assistant noise" not in out
