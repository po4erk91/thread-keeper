"""Extract-candidate dedup: rejected candidates must NOT be re-harvested.

Two bugs this covers:

1. `_candidate_exists` only checked `status IN ('pending','accepted')`, so a
   REJECTED candidate dropped out of the dedup. The extract daemon re-scans
   overlapping time windows, so the same source message got re-enqueued on
   the next pass; the same heuristic trips the same noise and the reviewer
   re-rejects it — an endless re-review loop. (Confirmed in prod: candidate
   #158 was an identical re-harvest of #157, same source_uuid, ~19m later.)

2. The content-fallback compared the full stored `content` against a
   500-char key (`content[:500]`), while `_enqueue` stores up to 4000 chars.
   So content-dedup never matched for any candidate longer than 500 chars —
   it only ever worked via source_uuid. Both sides must use a 500-char
   prefix.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def mp(fresh_mp):
    return fresh_mp


def _enqueue(db, **kw):
    from threadkeeper.tools.extract import _enqueue
    conn = db.get_db()
    rowid = _enqueue(
        conn,
        kw.get("kind", "distill"),
        kw.get("source_uuid", ""),
        kw.get("source_cid", "cidX"),
        kw["content"],
        kw.get("rationale", "test"),
    )
    conn.commit()
    return rowid


def _set_status(db, rowid, status):
    conn = db.get_db()
    conn.execute(
        "UPDATE extract_candidates SET status=? WHERE id=?", (status, rowid)
    )
    conn.commit()


def _count(db, **where):
    conn = db.get_db()
    if where:
        col, val = next(iter(where.items()))
        return conn.execute(
            f"SELECT COUNT(*) c FROM extract_candidates WHERE {col}=?", (val,)
        ).fetchone()["c"]
    return conn.execute(
        "SELECT COUNT(*) c FROM extract_candidates"
    ).fetchone()["c"]


# ── Bug 1: rejected must suppress re-harvest ──────────────────────────────

def test_rejected_source_uuid_not_reharvested(mp):
    """Same source_uuid, previously rejected → second enqueue is a no-op."""
    db = mp["db"]
    rid = _enqueue(db, source_uuid="u-1", content="some candidate text")
    assert rid is not None
    _set_status(db, rid, "rejected")
    # Re-scan harvests the same source message again.
    again = _enqueue(db, source_uuid="u-1", content="some candidate text")
    assert again is None, "rejected source_uuid was re-harvested"
    assert _count(db, source_uuid="u-1") == 1


def test_rejected_content_not_reharvested(mp):
    """Different source_uuid but identical content, previously rejected →
    still suppressed (recurring-noise template)."""
    db = mp["db"]
    body = "Done. Wrote /tmp/curate-candidates.json with all entries. " * 20
    rid = _enqueue(db, source_uuid="u-a", content=body)
    _set_status(db, rid, "rejected")
    again = _enqueue(db, source_uuid="u-b", content=body)
    assert again is None, "rejected identical content was re-harvested"


def test_pending_and_accepted_still_dedup(mp):
    """Regression guard: the original pending/accepted dedup still holds."""
    db = mp["db"]
    p = _enqueue(db, source_uuid="u-p", content="pending one")
    assert _enqueue(db, source_uuid="u-p", content="pending one") is None
    a = _enqueue(db, source_uuid="u-acc", content="accepted one")
    _set_status(db, a, "accepted")
    assert _enqueue(db, source_uuid="u-acc", content="accepted one") is None
    assert p is not None


# ── Bug 2: content dedup must work for >500-char candidates ────────────────

def test_long_content_dedup_via_content_fallback(mp):
    """No source_uuid; content > 500 chars. The fallback must still dedup —
    it previously compared full stored content to a 500-char key and never
    matched."""
    db = mp["db"]
    long_body = "X" * 2000  # _enqueue stores up to 2000-4000 chars
    rid = _enqueue(db, source_uuid="", content=long_body)
    assert rid is not None
    dup = _enqueue(db, source_uuid="", content=long_body)
    assert dup is None, "long content was not deduped (500-char-key mismatch)"
    assert _count(db) == 1


def test_long_content_dedup_after_reject(mp):
    """The two fixes compose: long content, no source_uuid, rejected → not
    re-harvested."""
    db = mp["db"]
    long_body = "Y" * 1800
    rid = _enqueue(db, source_uuid="", content=long_body)
    _set_status(db, rid, "rejected")
    dup = _enqueue(db, source_uuid="", content=long_body)
    assert dup is None
    assert _count(db) == 1


def test_distinct_long_content_not_falsely_deduped(mp):
    """Two candidates whose first 500 chars DIFFER must both enqueue —
    the prefix key must not collapse genuinely distinct content."""
    db = mp["db"]
    a = "A" * 600 + "tail-a"
    b = "B" * 600 + "tail-b"
    assert _enqueue(db, source_uuid="", content=a) is not None
    assert _enqueue(db, source_uuid="", content=b) is not None
    assert _count(db) == 2
