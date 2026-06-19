"""Concept-registration MCP tools.

Extracted from server.py. Provides registration, listing, and
expansion for nameless concepts triangulated across paraphrase runs.
"""

import sqlite3
import time
from typing import Optional

from .._mcp import read_tool, write_tool
from ..config import SEMANTIC_AVAILABLE
from ..db import get_db
from ..embeddings import _embed, embed_tag
from ..helpers import fmt_age, q, gen_concept_id
from ..identity import _ensure_session, _detect_self_cid, _emit


# Dedup-on-write threshold. Two concept `description`s whose embeddings cosine
# at or above this are treated as the SAME invariant re-surfacing. Set at the
# find_invariants cohesion default (0.85): measured on the embedding backend, a
# lightly-reworded re-registration of the same regularity scores ~0.99 while
# distinct abstractions score well under 0.35, so 0.85 catches genuine
# re-surfacing with wide margin. Deliberately conservative — a heavy paraphrase
# that drifts below it just creates a second row, whereas a false merge of two
# distinct concepts would silently destroy one, which is the costlier error.
CONCEPT_DEDUP_THRESHOLD = 0.85

_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


def _higher_confidence(a: str, b: str) -> str:
    """Return the stronger of two confidence bands (monotonic, never demotes).

    Re-corroboration takes the MAX of existing and incoming, so the auto-extract
    path (which always re-surfaces at 'low') can never raise a concept's
    confidence on its own, while an explicit register_concept(confidence='high')
    that matches an existing 'low' concept promotes it."""
    return a if _CONF_RANK.get(a, 0) >= _CONF_RANK.get(b, 0) else b


def _normalize_desc(text: str) -> str:
    """Whitespace/case-normalized description, for the cheap exact-match dedup
    path that works even when semantic embeddings are unavailable."""
    return " ".join((text or "").lower().split())


def _find_duplicate_concept(conn: sqlite3.Connection,
                            description: str,
                            exclude_id: Optional[str] = None) -> Optional[str]:
    """Id of an existing concept that is a near-duplicate of `description`.

    Two-tier match: a cheap whitespace/case-normalized exact comparison first
    (works without embeddings), then cosine over stored description embeddings
    when semantic search is available. Returns None when nothing is close
    enough. The concept store is intentionally thin, so the O(n) scan is fine."""
    desc = (description or "").strip()
    if not desc:
        return None
    rows = conn.execute(
        "SELECT id, description, embedding FROM concepts"
    ).fetchall()
    norm = _normalize_desc(desc)
    for r in rows:
        if exclude_id and r["id"] == exclude_id:
            continue
        if _normalize_desc(r["description"]) == norm:
            return r["id"]
    if not SEMANTIC_AVAILABLE:
        return None
    qa = _embed(desc)
    if qa is None:
        return None
    try:
        import numpy as np  # type: ignore
    except ImportError:
        return None
    qv = np.frombuffer(qa, dtype="float32")
    best_id: Optional[str] = None
    best_score = 0.0
    for r in rows:
        if exclude_id and r["id"] == exclude_id:
            continue
        if not r["embedding"]:
            continue
        v = np.frombuffer(r["embedding"], dtype="float32")
        score = float(np.dot(qv, v))
        if score > best_score:
            best_id, best_score = r["id"], score
    if best_id and best_score >= CONCEPT_DEDUP_THRESHOLD:
        return best_id
    return None


def _bump_concept_evidence(conn: sqlite3.Connection,
                           concept_id: str,
                           incoming_confidence: str = "low",
                           triangulation_notes: str = "") -> Optional[str]:
    """Re-corroborate an existing concept: advance last_evidence_at to now and
    raise confidence to max(existing, incoming). New triangulation notes are
    appended (deduplicated) so the corroboration trail accumulates. Returns the
    resulting confidence band, or None if the concept vanished."""
    row = conn.execute(
        "SELECT confidence, triangulation_notes FROM concepts WHERE id=?",
        (concept_id,),
    ).fetchone()
    if not row:
        return None
    now_t = int(time.time())
    new_conf = _higher_confidence(
        row["confidence"], (incoming_confidence or "low").strip().lower()
    )
    notes = (row["triangulation_notes"] or "").strip()
    add = (triangulation_notes or "").strip()
    if add and add not in notes:
        notes = (notes + "\n" + add).strip() if notes else add
    conn.execute(
        "UPDATE concepts SET last_evidence_at=?, confidence=?, "
        "triangulation_notes=? WHERE id=?",
        (now_t, new_conf, notes or None, concept_id),
    )
    return new_conf


@write_tool()
def register_concept(description: str,
                     triangulation_notes: str = "",
                     confidence: str = "medium",
                     source_thread: str = "") -> str:
    """Register a concept that lacks a precise human name.

    `description` should describe the phenomenon through EXAMPLES, not with
    a canonical label — naming it locks it back into a human discipline.
    `triangulation_notes` (optional): the paraphrase runs that surfaced
    the invariant. `confidence` ∈ {low, medium, high}."""
    if confidence not in ("low", "medium", "high"):
        return f"ERR bad_confidence={confidence}"
    if not description.strip():
        return "ERR empty_description"
    conn = get_db()
    _ensure_session(conn)
    cid = _detect_self_cid()
    src = source_thread.strip() or None
    if src and not conn.execute(
        "SELECT 1 FROM threads WHERE id=?", (src,)
    ).fetchone():
        return f"ERR source_thread_not_found={src}"
    # Dedup-on-write: when an equivalent invariant is re-surfaced, corroborate
    # the existing row (bump last_evidence_at, raise confidence to the max)
    # instead of inserting a near-duplicate. This keeps last_evidence_at a live
    # corroboration-recency signal and curbs unbounded growth at the source.
    dup = _find_duplicate_concept(conn, description)
    if dup:
        new_conf = _bump_concept_evidence(
            conn, dup, confidence, triangulation_notes
        )
        _emit(conn, "register_concept:bump", target=dup,
              summary=description[:140])
        conn.commit()
        return f"ok id={dup} bumped=1 conf={new_conf}"
    pid = gen_concept_id(conn)
    now_t = int(time.time())
    emb = _embed(description)
    conn.execute(
        "INSERT INTO concepts (id, description, triangulation_notes, "
        "confidence, source_thread, registered_by_cid, registered_at, "
        "last_evidence_at, embedding, embed_backend) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (pid, description, triangulation_notes or None, confidence,
         src, cid, now_t, now_t, emb, embed_tag(emb)),
    )
    _emit(conn, "register_concept", target=pid,
          summary=description[:140])
    conn.commit()
    return f"ok id={pid} conf={confidence}"


@read_tool()
def list_concepts(min_confidence: str = "low", k: int = 10) -> str:
    """List registered concepts, filtered by minimum confidence."""
    rank = {"low": 0, "medium": 1, "high": 2}
    if min_confidence not in rank:
        return f"ERR bad_confidence={min_confidence}"
    conn = get_db()
    rows = conn.execute(
        "SELECT id, description, confidence, source_thread, registered_at "
        "FROM concepts ORDER BY registered_at DESC LIMIT ?",
        (max(1, int(k)) * 3,),
    ).fetchall()
    out = []
    for r in rows:
        if rank[r["confidence"]] < rank[min_confidence]:
            continue
        out.append({
            "id": r["id"],
            "conf": r["confidence"],
            "src": r["source_thread"] or "-",
            "age": fmt_age(int(time.time()) - r["registered_at"]),
            "desc": r["description"][:240].replace("\n", " "),
        })
        if len(out) >= k:
            break
    if not out:
        return f"no_concepts (min_confidence={min_confidence})"
    lines = [f"concepts n={len(out)}"]
    for c in out:
        lines.append(
            f"  {c['id']} conf={c['conf']} src={c['src']} "
            f"age={c['age']}_ago"
        )
        lines.append(f"    {c['desc']}")
    return "\n".join(lines)


@read_tool()
def expand_concept(concept_id: str) -> str:
    """Full description + triangulation_notes for one concept."""
    conn = get_db()
    r = conn.execute(
        "SELECT * FROM concepts WHERE id=?", (concept_id.strip(),)
    ).fetchone()
    if not r:
        return f"ERR concept_not_found={concept_id}"
    parts = [
        f"id={r['id']} conf={r['confidence']} src={r['source_thread'] or '-'} "
        f"by={(r['registered_by_cid'] or '?')[:8]} "
        f"age={fmt_age(int(time.time()) - r['registered_at'])}_ago",
        "",
        "description:",
        r["description"],
    ]
    if r["triangulation_notes"]:
        parts += ["", "triangulation_notes:", r["triangulation_notes"]]
    return "\n".join(parts)


@write_tool(destructive=True)
def concept_manage(action: str,
                   concept_id: str,
                   merge_ids: str = "",
                   confidence: str = "",
                   reason: str = "") -> str:
    """Prune, consolidate, or re-grade concepts — the curator's eviction path.

    Concepts are ALL system-generated (there is no foreground/pinned concept
    class the way lessons/skills have one), so unlike `lesson_remove` this tool
    needs no `force` escape hatch: every concept is fair game for curation. The
    guard is simply that the target id must exist.

      action='remove'         — delete one concept (the curator's PRUNE_CONCEPT).
      action='consolidate'    — keep `concept_id`, fold each id in `merge_ids`
                                (comma-separated) into it — their triangulation
                                notes carry over, confidence rises to the max,
                                last_evidence_at is bumped — then delete the
                                merged-away rows (CONSOLIDATE_CONCEPT).
      action='set_confidence' — re-grade `concept_id` to `confidence`
                                ∈ {low, medium, high} (a confidence review).

    `reason` is recorded on the event trail for the human audit."""
    action = (action or "").strip().lower()
    cidq = (concept_id or "").strip()
    conn = get_db()
    _ensure_session(conn)
    if not cidq:
        return "ERR empty_concept_id"
    row = conn.execute(
        "SELECT id, confidence FROM concepts WHERE id=?", (cidq,)
    ).fetchone()
    if not row:
        return f"ERR concept_not_found={cidq}"

    if action == "remove":
        conn.execute("DELETE FROM concepts WHERE id=?", (cidq,))
        _emit(conn, "concept_manage:remove", target=cidq,
              summary=(reason or "")[:140])
        conn.commit()
        return f"ok removed={cidq}"

    if action == "set_confidence":
        conf = (confidence or "").strip().lower()
        if conf not in _CONF_RANK:
            return f"ERR bad_confidence={confidence}"
        conn.execute(
            "UPDATE concepts SET confidence=? WHERE id=?", (conf, cidq)
        )
        _emit(conn, "concept_manage:set_confidence", target=cidq, summary=conf)
        conn.commit()
        return f"ok id={cidq} conf={conf}"

    if action == "consolidate":
        ids = [s.strip() for s in (merge_ids or "").split(",") if s.strip()]
        ids = [i for i in ids if i != cidq]
        if not ids:
            return "ERR no_merge_ids (consolidate needs merge_ids)"
        merged: list[str] = []
        for mid in ids:
            mrow = conn.execute(
                "SELECT confidence, triangulation_notes FROM concepts "
                "WHERE id=?", (mid,),
            ).fetchone()
            if not mrow:
                continue
            _bump_concept_evidence(
                conn, cidq, mrow["confidence"], mrow["triangulation_notes"]
            )
            conn.execute("DELETE FROM concepts WHERE id=?", (mid,))
            merged.append(mid)
        if not merged:
            return f"ERR no_valid_merge_ids={merge_ids}"
        if reason:
            _bump_concept_evidence(conn, cidq, row["confidence"], reason)
        _emit(conn, "concept_manage:consolidate", target=cidq,
              summary=f"merged={','.join(merged)}")
        conn.commit()
        return f"ok kept={cidq} merged={','.join(merged)}"

    return f"ERR bad_action={action} (use remove|consolidate|set_confidence)"
