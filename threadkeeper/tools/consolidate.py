"""Consolidate MCP tool: periodic memory hygiene.

Extracted from server.py. Provides a dry-run-by-default sweep that
reports (and optionally applies) four kinds of cleanup:

  merge_dup_notes : intra-thread cosine ≥ note_cosine, keep oldest
  idle_stale      : active threads not touched in stale_days
  dedupe_verbatim : exact text + (if embeddings) cosine ≥ verbatim_cosine
  release_orphan  : claim ≥ orphan_days old, no progress past claim mark
"""

import sqlite3
import time

from .._mcp import mcp
from ..db import get_db
from ..config import SEMANTIC_AVAILABLE
from ..helpers import fmt_age, q, normalize_text
from ..identity import _ensure_session, _emit
from ..embeddings import _get_model, _encode


CONSOLIDATE_NOTE_COSINE = 0.95
CONSOLIDATE_VERBATIM_COSINE = 0.90
CONSOLIDATE_STALE_THREAD_DAYS = 30
CONSOLIDATE_ORPHAN_CLAIM_DAYS = 7


@mcp.tool()
def consolidate(dry_run: bool = True,
                stale_days: int = CONSOLIDATE_STALE_THREAD_DAYS,
                orphan_days: int = CONSOLIDATE_ORPHAN_CLAIM_DAYS,
                note_cosine: float = CONSOLIDATE_NOTE_COSINE,
                verbatim_cosine: float = CONSOLIDATE_VERBATIM_COSINE) -> str:
    """Periodic memory hygiene. dry_run=True (default) reports only.

      merge_dup_notes : intra-thread cosine ≥ note_cosine, keep oldest
      idle_stale      : active threads not touched in stale_days
      dedupe_verbatim : exact text + (if embeddings) cosine ≥ verbatim_cosine
      release_orphan  : claim ≥ orphan_days old, no progress past claim mark"""
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    findings = {
        "merge_dup_notes": [], "idle_stale": [],
        "dedupe_verbatim": [], "release_orphan": [],
    }
    np = None
    if SEMANTIC_AVAILABLE:
        try:
            import numpy as np  # type: ignore
        except ImportError:
            np = None

    if np is not None:
        thread_ids = [
            r["thread_id"] for r in conn.execute(
                "SELECT thread_id FROM notes WHERE thread_id IS NOT NULL "
                "AND embedding IS NOT NULL "
                "GROUP BY thread_id HAVING COUNT(*) >= 2"
            ).fetchall()
        ]
        for tid in thread_ids:
            ns = conn.execute(
                "SELECT id, content, embedding, created_at FROM notes "
                "WHERE thread_id=? AND embedding IS NOT NULL "
                "ORDER BY created_at ASC", (tid,)
            ).fetchall()
            if len(ns) < 2:
                continue
            embs = np.stack([
                np.frombuffer(n["embedding"], dtype="float32") for n in ns
            ])
            sim = embs @ embs.T
            kept = [True] * len(ns)
            for i in range(len(ns)):
                if not kept[i]:
                    continue
                for j in range(i + 1, len(ns)):
                    if kept[j] and sim[i, j] >= note_cosine:
                        kept[j] = False
                        findings["merge_dup_notes"].append({
                            "thread": tid, "keep": ns[i]["id"],
                            "drop": ns[j]["id"],
                            "cos": float(sim[i, j]),
                            "snip": ns[j]["content"][:120],
                        })

    cutoff_stale = now - max(1, int(stale_days)) * 86400
    for t in conn.execute(
        "SELECT id, question, last_touched_at FROM threads "
        "WHERE state='active' AND last_touched_at < ?", (cutoff_stale,)
    ).fetchall():
        findings["idle_stale"].append({
            "thread": t["id"], "question": t["question"][:120],
            "stale_for": fmt_age(now - t["last_touched_at"]),
        })

    vb = conn.execute(
        "SELECT id, speaker, content, created_at FROM verbatim "
        "ORDER BY created_at DESC"
    ).fetchall()
    seen_text: dict = {}
    semantic_pool: list = []
    for v in vb:
        key = (v["speaker"], normalize_text(v["content"]))
        if key in seen_text:
            findings["dedupe_verbatim"].append({
                "keep": seen_text[key], "drop": v["id"],
                "via": "text_exact", "snip": v["content"][:120],
            })
        else:
            seen_text[key] = v["id"]
            semantic_pool.append(v)
    if np is not None and 1 < len(semantic_pool) <= 200:
        m = _get_model()
        if m is not None:
            by_speaker: dict = {}
            for v in semantic_pool:
                by_speaker.setdefault(v["speaker"], []).append(v)
            for sp, vs in by_speaker.items():
                if len(vs) < 2:
                    continue
                vecs = _encode([v["content"] for v in vs])
                if vecs is None:
                    continue
                sim = vecs @ vecs.T
                kept = [True] * len(vs)
                for i in range(len(vs)):
                    if not kept[i]:
                        continue
                    for j in range(i + 1, len(vs)):
                        if kept[j] and sim[i, j] >= verbatim_cosine:
                            kept[j] = False
                            findings["dedupe_verbatim"].append({
                                "keep": vs[i]["id"], "drop": vs[j]["id"],
                                "via": f"cos={float(sim[i, j]):.2f}",
                                "snip": vs[j]["content"][:120],
                            })

    cutoff_orphan = now - max(1, int(orphan_days)) * 86400
    for t in conn.execute(
        "SELECT id, question, claimed_at, claimed_by_cid, last_touched_at "
        "FROM threads WHERE claimed_at IS NOT NULL AND claimed_at < ? "
        "AND last_touched_at <= claimed_at + 60", (cutoff_orphan,)
    ).fetchall():
        findings["release_orphan"].append({
            "thread": t["id"],
            "claimed_by": (t["claimed_by_cid"] or "?")[:8],
            "claimed_age": fmt_age(now - t["claimed_at"]),
            "question": t["question"][:120],
        })

    applied = {
        "merge_dup_notes": 0, "idle_stale": 0,
        "dedupe_verbatim": 0, "release_orphan": 0,
    }
    if not dry_run:
        for f in findings["merge_dup_notes"]:
            conn.execute("DELETE FROM notes WHERE id=?", (f["drop"],))
            applied["merge_dup_notes"] += 1
        for f in findings["idle_stale"]:
            conn.execute(
                "UPDATE threads SET state='idle', last_touched_at=? WHERE id=?",
                (now, f["thread"]),
            )
            applied["idle_stale"] += 1
        for f in findings["dedupe_verbatim"]:
            conn.execute("DELETE FROM verbatim WHERE id=?", (f["drop"],))
            applied["dedupe_verbatim"] += 1
        for f in findings["release_orphan"]:
            conn.execute(
                "UPDATE threads SET claimed_at=NULL, claimed_by_cid=NULL "
                "WHERE id=?", (f["thread"],)
            )
            applied["release_orphan"] += 1
        _emit(conn, "consolidate_apply",
              summary=" ".join(f"{k}={v}" for k, v in applied.items()))
        conn.commit()

    out = [
        f"consolidate dry_run={dry_run} "
        f"merge={len(findings['merge_dup_notes'])} "
        f"idle={len(findings['idle_stale'])} "
        f"dedupe={len(findings['dedupe_verbatim'])} "
        f"orphan={len(findings['release_orphan'])}"
    ]
    if not dry_run:
        out.append("applied " + " ".join(f"{k}={v}" for k, v in applied.items()))
    if findings["merge_dup_notes"]:
        out.append("")
        out.append("merge_dup_notes (keep oldest)")
        for f in findings["merge_dup_notes"][:10]:
            out.append(
                f"  thread={f['thread']} keep=#{f['keep']} drop=#{f['drop']} "
                f"cos={f['cos']:.3f} {q(f['snip'])}"
            )
    if findings["idle_stale"]:
        out.append("")
        out.append(f"idle_stale (>{stale_days}d untouched)")
        for f in findings["idle_stale"][:10]:
            out.append(
                f"  {f['thread']} stale={f['stale_for']} "
                f"q={q(f['question'])}"
            )
    if findings["dedupe_verbatim"]:
        out.append("")
        out.append("dedupe_verbatim (keep most-recent)")
        for f in findings["dedupe_verbatim"][:10]:
            out.append(
                f"  keep=#{f['keep']} drop=#{f['drop']} "
                f"via={f['via']} {q(f['snip'])}"
            )
    if findings["release_orphan"]:
        out.append("")
        out.append(f"release_orphan (claimed >{orphan_days}d, no progress)")
        for f in findings["release_orphan"][:10]:
            out.append(
                f"  {f['thread']} by={f['claimed_by']} "
                f"age={f['claimed_age']} q={q(f['question'])}"
            )
    return "\n".join(out)
