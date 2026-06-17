"""Concept store lifecycle: dedup-on-write, evidence-bump, and the
concept_manage mutation/eviction path (#75).

Before #75 the concepts table was write-only / grow-only: register/extract
always inserted (so near-duplicate invariants piled up), `last_evidence_at`
was frozen at registration time, and no remove/consolidate/confidence tool
existed — so the curator's PRUNE_CONCEPT / CONSOLIDATE_CONCEPT rubric was
unappliable. These tests pin the new behavior:

  * re-surfacing an equivalent invariant bumps the existing row instead of
    inserting a duplicate (and advances last_evidence_at);
  * distinct invariants are NOT merged;
  * concept_manage(remove|consolidate|set_confidence) mutates the store;
  * the brief orders concepts by corroboration recency, not registration.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def mp(fresh_mp):
    return fresh_mp


def _concepts(mp):
    from threadkeeper.tools import concepts
    return concepts


def _count(mp):
    conn = mp["db"].get_db()
    return conn.execute("SELECT COUNT(*) c FROM concepts").fetchone()["c"]


def _row(mp, cid):
    conn = mp["db"].get_db()
    return conn.execute(
        "SELECT * FROM concepts WHERE id=?", (cid,)
    ).fetchone()


# ──────────────────────────────────────────────────────────────────────
# dedup-on-write via register_concept
# ──────────────────────────────────────────────────────────────────────

def test_register_exact_duplicate_bumps_not_inserts(mp):
    c = _concepts(mp)
    desc = (
        "A processing region where one part of the input catalyzes the "
        "transformation of another part through the model's own dynamics."
    )
    out1 = c.register_concept(description=desc, confidence="low")
    assert out1.startswith("ok id=")
    assert "bumped" not in out1
    assert _count(mp) == 1

    out2 = c.register_concept(description=desc, confidence="low")
    assert "bumped=1" in out2
    # No duplicate row created for a repeated description.
    assert _count(mp) == 1


def test_register_duplicate_promotes_confidence_to_max(mp):
    c = _concepts(mp)
    desc = "Self-referential editing loops that converge on a fixed point."
    c.register_concept(description=desc, confidence="low")
    cid = _row_id_only(mp)
    assert _row(mp, cid)["confidence"] == "low"

    # A higher-confidence re-registration of the same invariant promotes it.
    c.register_concept(description=desc, confidence="high")
    assert _row(mp, cid)["confidence"] == "high"
    assert _count(mp) == 1


def test_register_duplicate_does_not_demote_confidence(mp):
    c = _concepts(mp)
    desc = "Monotonic confidence: corroboration never lowers the band."
    c.register_concept(description=desc, confidence="high")
    cid = _row_id_only(mp)
    # A low-confidence re-surface (e.g. the auto-extract path) must not demote.
    c.register_concept(description=desc, confidence="low")
    assert _row(mp, cid)["confidence"] == "high"


def test_register_duplicate_advances_last_evidence_at(mp):
    c = _concepts(mp)
    desc = "Evidence recency must advance on corroboration."
    c.register_concept(description=desc, confidence="medium")
    cid = _row_id_only(mp)
    conn = mp["db"].get_db()
    # Backdate registration + evidence so we can detect a real bump forward.
    conn.execute(
        "UPDATE concepts SET registered_at=100, last_evidence_at=100 WHERE id=?",
        (cid,),
    )
    conn.commit()
    c.register_concept(description=desc, confidence="medium")
    row = _row(mp, cid)
    assert row["last_evidence_at"] > 100
    # registered_at is untouched — only the corroboration signal moved.
    assert row["registered_at"] == 100


def test_distinct_descriptions_are_not_merged(mp):
    c = _concepts(mp)
    c.register_concept(
        description="Cross-language cosine is language sensitive.",
        confidence="medium",
    )
    c.register_concept(
        description="Background daemons leak state across test boundaries.",
        confidence="medium",
    )
    assert _count(mp) == 2


def test_register_duplicate_appends_triangulation_notes(mp):
    c = _concepts(mp)
    desc = "Triangulation notes accumulate across corroborations."
    c.register_concept(
        description=desc, confidence="low", triangulation_notes="run A"
    )
    cid = _row_id_only(mp)
    c.register_concept(
        description=desc, confidence="low", triangulation_notes="run B"
    )
    notes = _row(mp, cid)["triangulation_notes"] or ""
    assert "run A" in notes and "run B" in notes


# ──────────────────────────────────────────────────────────────────────
# dedup-on-write via accept_candidate(target_kind='concept')
# ──────────────────────────────────────────────────────────────────────

def test_accept_candidate_concept_dedups(mp):
    import time
    from threadkeeper.tools import extract
    conn = mp["db"].get_db()
    content = "An extract-path invariant surfaced from paraphrase repeats."

    def _insert_candidate():
        # Insert directly to bypass the candidate-level content dedup
        # (_candidate_exists) — we are exercising the CONCEPT-level dedup that
        # accept_candidate performs when materializing, not the candidate gate.
        cur = conn.execute(
            "INSERT INTO extract_candidates (kind, source_uuid, source_cid, "
            "content, rationale, status, created_at) "
            "VALUES ('concept','','cidX',?,?,'pending',?)",
            (content, "H3 example_regularity", int(time.time())),
        )
        conn.commit()
        return cur.lastrowid

    out1 = extract.accept_candidate(id=_insert_candidate(), target_kind="concept")
    assert out1.startswith("ok accepted")
    assert _count(mp) == 1

    out2 = extract.accept_candidate(id=_insert_candidate(), target_kind="concept")
    assert "bumped=1" in out2
    # Re-accepting an equivalent candidate corroborates, does not duplicate.
    assert _count(mp) == 1


# ──────────────────────────────────────────────────────────────────────
# concept_manage — remove / consolidate / set_confidence
# ──────────────────────────────────────────────────────────────────────

def test_concept_manage_remove(mp):
    c = _concepts(mp)
    c.register_concept(description="prune me", confidence="low")
    cid = _row_id_only(mp)
    out = c.concept_manage(action="remove", concept_id=cid, reason="false_positive")
    assert out == f"ok removed={cid}"
    assert _count(mp) == 0


def test_concept_manage_set_confidence(mp):
    c = _concepts(mp)
    c.register_concept(description="regrade me", confidence="high")
    cid = _row_id_only(mp)
    out = c.concept_manage(
        action="set_confidence", concept_id=cid, confidence="low"
    )
    assert out == f"ok id={cid} conf=low"
    assert _row(mp, cid)["confidence"] == "low"


def test_concept_manage_set_confidence_rejects_bad_band(mp):
    c = _concepts(mp)
    c.register_concept(description="x y z", confidence="medium")
    cid = _row_id_only(mp)
    out = c.concept_manage(
        action="set_confidence", concept_id=cid, confidence="bogus"
    )
    assert out.startswith("ERR bad_confidence")


def test_concept_manage_consolidate_merges_and_deletes(mp):
    c = _concepts(mp)
    c.register_concept(
        description="umbrella regularity about retry backoff under load",
        confidence="low",
        triangulation_notes="keep-notes",
    )
    kept = _row_id_only(mp)
    c.register_concept(
        description="distinct idea about UI rendering glitches on rotation",
        confidence="high",
        triangulation_notes="merged-notes-1",
    )
    c.register_concept(
        description="unrelated observation on database lock contention",
        confidence="medium",
    )
    conn = mp["db"].get_db()
    ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM concepts ORDER BY id"
        ).fetchall()
    ]
    merge_ids = [i for i in ids if i != kept]
    assert len(merge_ids) == 2

    out = c.concept_manage(
        action="consolidate",
        concept_id=kept,
        merge_ids=",".join(merge_ids),
    )
    assert out.startswith(f"ok kept={kept}")
    # merged-away rows are gone; only the umbrella remains
    assert _count(mp) == 1
    row = _row(mp, kept)
    # confidence rose to the max of the merged set (one was 'high')
    assert row["confidence"] == "high"
    # merged-away triangulation notes carried over
    assert "merged-notes-1" in (row["triangulation_notes"] or "")


def test_concept_manage_consolidate_requires_merge_ids(mp):
    c = _concepts(mp)
    c.register_concept(description="lonely", confidence="low")
    cid = _row_id_only(mp)
    out = c.concept_manage(action="consolidate", concept_id=cid)
    assert out.startswith("ERR no_merge_ids")


def test_concept_manage_unknown_id_and_action(mp):
    c = _concepts(mp)
    c.register_concept(description="exists", confidence="low")
    cid = _row_id_only(mp)
    assert c.concept_manage(
        action="remove", concept_id="Czzz"
    ).startswith("ERR concept_not_found")
    assert c.concept_manage(
        action="frobnicate", concept_id=cid
    ).startswith("ERR bad_action")


# ──────────────────────────────────────────────────────────────────────
# brief ordering reflects corroboration recency, not registration time
# ──────────────────────────────────────────────────────────────────────

def test_brief_orders_concepts_by_last_evidence(mp):
    conn = mp["db"].get_db()
    # Two high-conf concepts: 'old-reg' was registered most recently but never
    # re-corroborated; 'fresh-evidence' was registered earlier but recently
    # corroborated. Corroboration recency must win the brief ordering.
    conn.execute(
        "INSERT INTO concepts (id, description, confidence, registered_at, "
        "last_evidence_at) VALUES (?,?,?,?,?)",
        ("Cold", "registered late never corroborated", "high", 5000, 5000),
    )
    conn.execute(
        "INSERT INTO concepts (id, description, confidence, registered_at, "
        "last_evidence_at) VALUES (?,?,?,?,?)",
        ("Cfrs", "registered early but freshly corroborated", "high", 1000, 9000),
    )
    conn.commit()
    text = mp["brief"].render_brief(conn, scope="full")
    assert "concepts (high-conf)" in text
    # freshly-corroborated concept appears before the stale-evidence one
    assert text.index("Cfrs") < text.index("Cold")


# ──────────────────────────────────────────────────────────────────────
# semantic near-duplicate dedup (only when embeddings are available)
# ──────────────────────────────────────────────────────────────────────

def test_near_duplicate_dedups_when_semantic_available(mp):
    if not mp["config"].SEMANTIC_AVAILABLE:
        pytest.skip("semantic embeddings unavailable")
    c = _concepts(mp)
    c.register_concept(
        description=(
            "Asymmetric in-band reactivity: the input has internal regions "
            "where one part catalyzes transformations of another part through "
            "processing dynamics."
        ),
        confidence="low",
    )
    # A lightly-reworded re-registration of the same invariant (punctuation /
    # synonym swaps) should corroborate the existing row, not insert a new one.
    out = c.register_concept(
        description=(
            "Asymmetric in-band reactivity — the input contains internal "
            "regions where one part catalyzes transformations of another part "
            "via processing dynamics."
        ),
        confidence="low",
    )
    assert "bumped=1" in out
    assert _count(mp) == 1


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────

def _row_id_only(mp):
    """Id of the single concept currently in the store (test convenience)."""
    conn = mp["db"].get_db()
    return conn.execute("SELECT id FROM concepts LIMIT 1").fetchone()["id"]
