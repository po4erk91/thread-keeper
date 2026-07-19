"""Consolidate MCP tool: periodic memory hygiene.

Extracted from server.py. Provides a dry-run-by-default sweep that
reports (and optionally applies) several kinds of cleanup:

  merge_dup_notes : intra-thread cosine ≥ note_cosine, keep oldest
  idle_stale      : active threads not touched in stale_days
  dedupe_verbatim : exact text + (if embeddings) cosine ≥ verbatim_cosine
  release_orphan  : claim ≥ orphan_days old, no progress past claim mark
  prune_tasks     : ended tasks outside retention age/count bounds
  gc_task_spool   : task spool files with no retained tasks row
"""

import sqlite3
import stat
import time

from .._mcp import read_tool, write_tool
from ..db import get_db
from ..config import (
    SEMANTIC_AVAILABLE,
    TASK_LOG_DIR,
    TASK_RETENTION_COUNT,
    TASK_RETENTION_DAYS,
)
from ..helpers import fmt_age, q, normalize_text
from ..identity import _ensure_session, _emit
from ..embeddings import (
    _get_model,
    _encode,
    _vec_delete_note,
    _note_embedding_parts,
)
from ..task_spool import ensure_task_spool_dir


CONSOLIDATE_NOTE_COSINE = 0.95
CONSOLIDATE_VERBATIM_COSINE = 0.90
CONSOLIDATE_STALE_THREAD_DAYS = 30
CONSOLIDATE_ORPHAN_CLAIM_DAYS = 7
TASK_SPOOL_SUFFIXES = (".stdin.txt", ".command", ".log")


def _task_retention_label() -> str:
    days = max(0, int(TASK_RETENTION_DAYS))
    count = max(0, int(TASK_RETENTION_COUNT))
    parts = []
    if days:
        parts.append(f"ended within {days}d")
    if count:
        parts.append(f"newest {count} ended")
    return " or ".join(parts) if parts else "row pruning disabled"


def _task_rows_to_prune(conn: sqlite3.Connection, now: int) -> list[dict]:
    """Ended tasks outside the configured age/count retention protections.

    Live rows are excluded at the SQL boundary because spawn single-flight and
    budget accounting depend on `ended_at IS NULL` rows being durable while the
    child is still alive.
    """
    days = max(0, int(TASK_RETENTION_DAYS))
    count = max(0, int(TASK_RETENTION_COUNT))
    if days <= 0 and count <= 0:
        return []

    keep_by_count = set()
    if count > 0:
        keep_by_count = {
            r["id"] for r in conn.execute(
                "SELECT id FROM tasks WHERE ended_at IS NOT NULL "
                "ORDER BY ended_at DESC, started_at DESC LIMIT ?",
                (count,),
            ).fetchall()
        }
    cutoff = now - days * 86400 if days > 0 else None
    out = []
    for r in conn.execute(
        "SELECT id, prompt, started_at, ended_at FROM tasks "
        "WHERE ended_at IS NOT NULL ORDER BY ended_at ASC, started_at ASC"
    ).fetchall():
        keep_age = cutoff is not None and int(r["ended_at"]) >= cutoff
        keep_count = r["id"] in keep_by_count
        if keep_age or keep_count:
            continue
        out.append({
            "task": r["id"],
            "prompt": (r["prompt"] or "")[:120],
            "ended_age": fmt_age(max(0, now - int(r["ended_at"]))),
        })
    return out


def _spool_task_id(name: str) -> tuple[str, str] | None:
    for suffix in TASK_SPOOL_SUFFIXES:
        if not name.endswith(suffix):
            continue
        task_id = name[:-len(suffix)]
        # TASK_LOG_DIR also holds non-task logs/configs (dialog.log,
        # memory-guard.log, slim-mcp-*.json). Only spawn task ids are GC'd.
        if task_id.startswith("tk_"):
            return task_id, suffix
    return None


def _task_spool_gc_candidates(
    conn: sqlite3.Connection,
    pruned_task_ids: set[str],
) -> list[dict]:
    retained = {
        r["id"] for r in conn.execute("SELECT id FROM tasks").fetchall()
    } - pruned_task_ids
    try:
        ensure_task_spool_dir(TASK_LOG_DIR)
        entries = sorted(TASK_LOG_DIR.iterdir(), key=lambda p: p.name)
    except (FileNotFoundError, OSError):
        return []

    out = []
    for p in entries:
        try:
            st = p.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        parsed = _spool_task_id(p.name)
        if parsed is None:
            continue
        task_id, suffix = parsed
        if task_id in retained:
            continue
        out.append({
            "task": task_id,
            "name": p.name,
            "path": str(p),
            "kind": suffix.lstrip("."),
        })
    return out


@write_tool(destructive=True)
def consolidate(dry_run: bool = True,
                stale_days: int = CONSOLIDATE_STALE_THREAD_DAYS,
                orphan_days: int = CONSOLIDATE_ORPHAN_CLAIM_DAYS,
                note_cosine: float = CONSOLIDATE_NOTE_COSINE,
                verbatim_cosine: float = CONSOLIDATE_VERBATIM_COSINE) -> str:
    """Periodic memory hygiene. dry_run=True (default) reports only.

      merge_dup_notes : intra-thread cosine ≥ note_cosine, keep oldest
      idle_stale      : active threads not touched in stale_days
      dedupe_verbatim : exact text + (if embeddings) cosine ≥ verbatim_cosine
      release_orphan  : claim ≥ orphan_days old, no progress past claim mark
      prune_tasks     : ended task rows outside retention bounds
      gc_task_spool   : task spool files with no retained task row"""
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    findings = {
        "merge_dup_notes": [], "idle_stale": [],
        "dedupe_verbatim": [], "release_orphan": [],
        "prune_tasks": [], "gc_task_spool": [],
    }
    np = None
    if SEMANTIC_AVAILABLE:
        try:
            import numpy as np  # type: ignore
        except ImportError:
            np = None

    if np is not None:
        note_join, note_embedding = _note_embedding_parts(conn, "n")
        thread_ids = [
            r["thread_id"] for r in conn.execute(
                f"SELECT n.thread_id FROM notes n {note_join} "
                "WHERE n.thread_id IS NOT NULL "
                f"AND {note_embedding} IS NOT NULL "
                "GROUP BY n.thread_id HAVING COUNT(*) >= 2"
            ).fetchall()
        ]
        for tid in thread_ids:
            ns = conn.execute(
                "SELECT n.id, n.content, "
                f"       {note_embedding} AS embedding, n.created_at "
                f"FROM notes n {note_join} "
                f"WHERE n.thread_id=? AND {note_embedding} IS NOT NULL "
                "ORDER BY n.created_at ASC", (tid,)
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

    findings["prune_tasks"].extend(_task_rows_to_prune(conn, now))
    pruned_task_ids = {f["task"] for f in findings["prune_tasks"]}
    findings["gc_task_spool"].extend(
        _task_spool_gc_candidates(conn, pruned_task_ids)
    )

    applied = {
        "merge_dup_notes": 0, "idle_stale": 0,
        "dedupe_verbatim": 0, "release_orphan": 0,
        "prune_tasks": 0, "gc_task_spool": 0,
    }
    if not dry_run:
        for f in findings["merge_dup_notes"]:
            conn.execute("DELETE FROM notes WHERE id=?", (f["drop"],))
            # Keep the vec0 mirror in sync — notes_fts is trigger-synced but
            # notes_vec is not, so an explicit delete prevents orphan KNN rows.
            _vec_delete_note(conn, f["drop"])
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
        for f in findings["prune_tasks"]:
            conn.execute(
                "DELETE FROM tasks WHERE id=? AND ended_at IS NOT NULL",
                (f["task"],),
            )
            applied["prune_tasks"] += 1
        for f in findings["gc_task_spool"]:
            try:
                TASK_LOG_DIR.joinpath(f["name"]).unlink()
            except FileNotFoundError:
                applied["gc_task_spool"] += 1
            except OSError:
                continue
            else:
                applied["gc_task_spool"] += 1
        _emit(conn, "consolidate_apply",
              summary=" ".join(f"{k}={v}" for k, v in applied.items()))
        conn.commit()

    out = [
        f"consolidate dry_run={dry_run} "
        f"merge={len(findings['merge_dup_notes'])} "
        f"idle={len(findings['idle_stale'])} "
        f"dedupe={len(findings['dedupe_verbatim'])} "
        f"orphan={len(findings['release_orphan'])} "
        f"task_prune={len(findings['prune_tasks'])} "
        f"spool_gc={len(findings['gc_task_spool'])}"
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
    if findings["prune_tasks"]:
        out.append("")
        out.append(
            "task_retention "
            f"({_task_retention_label()}; live rows protected)"
        )
        for f in findings["prune_tasks"][:10]:
            out.append(
                f"  task={f['task']} ended={f['ended_age']} "
                f"prompt={q(f['prompt'])}"
            )
    if findings["gc_task_spool"]:
        out.append("")
        out.append(
            f"task_spool_gc (dir={TASK_LOG_DIR}; no retained task row)"
        )
        for f in findings["gc_task_spool"][:10]:
            out.append(
                f"  task={f['task']} kind={f['kind']} file={q(f['name'])}"
            )
    return "\n".join(out)
