"""Invariance detection MCP tool.

Extracted from server.py. Finds recurring assistant-side response patterns
that survive prompt variance — clusters of responses with high mutual
similarity whose preceding user prompts are diverse. High-scoring clusters
are candidates for "things I always say" regardless of what was asked.

Requires semantic embeddings (sentence-transformers) — without them the
tool returns ERR.
"""

import sqlite3
import time
from typing import Optional

from .._mcp import read_tool, write_tool
from ..db import get_db
from ..config import SEMANTIC_AVAILABLE
from ..helpers import fmt_age, q
from ..identity import _ensure_session
from ..embeddings import _dialog_embedding_parts


@read_tool()
def find_invariants(window_days: int = 14,
                    min_cluster_size: int = 3,
                    response_cohesion: float = 0.85,
                    top_n: int = 10,
                    max_messages: int = 10000) -> str:
    """Find recurring assistant-side patterns that survive prompt variance.

    Algorithm:
      1. Pull recent assistant messages from dialog_messages (with embeddings).
      2. Greedy cluster by response embedding cosine ≥ response_cohesion.
      3. For each cluster (size ≥ min_cluster_size), find each response's
         immediately-preceding user prompt in the same conversation.
      4. Score = avg_response_similarity × (1 - avg_prompt_similarity).
         High = my response stays the same shape while prompts vary widely.

    Returns top_n clusters with sample response, scores, and counts.
    Requires semantic embeddings (sentence-transformers) — without them
    returns ERR.
    """
    if not SEMANTIC_AVAILABLE:
        return "ERR semantic_off (need sentence-transformers + embeddings)"
    try:
        import numpy as _np  # type: ignore
    except ImportError:
        return "ERR numpy_unavailable"

    conn = get_db()
    cutoff = int(time.time()) - max(1, int(window_days)) * 86400
    # Aggressive filter: subagent jsonls (project='subagents') are mostly
    # boilerplate role-intros and pollute clusters. Skip those + common
    # subagent-shape kickoff phrases. We want main-conversation responses.
    dialog_join, dialog_embedding = _dialog_embedding_parts(conn, "d")
    rows = conn.execute(
        "SELECT d.uuid, d.session_id, d.content, d.created_at, "
        f"       {dialog_embedding} AS embedding "
        f"FROM dialog_messages d {dialog_join} "
        f"WHERE d.role='assistant' AND {dialog_embedding} IS NOT NULL "
        "AND d.created_at >= ? "
        "AND d.project != 'subagents' "
        "AND d.content NOT LIKE '[thinking]%' "
        "AND d.content NOT LIKE 'I''m Claude Code%' "
        "AND d.content NOT LIKE 'Hello! I''m Claude Code%' "
        "AND d.content NOT LIKE 'I''ll help you%' "
        "AND d.content NOT LIKE 'I understand you want me to%' "
        "AND d.content NOT LIKE '<summary>%' "
        "AND length(d.content) >= 120 "
        "ORDER BY d.created_at DESC LIMIT ?",
        (cutoff, max(100, int(max_messages))),
    ).fetchall()
    if len(rows) < min_cluster_size:
        return f"insufficient_data n={len(rows)} need>={min_cluster_size}"

    embs = _np.stack([
        _np.frombuffer(r["embedding"], dtype="float32") for r in rows
    ])  # (N, D)
    N = embs.shape[0]
    sim = embs @ embs.T  # (N, N), embeddings already normalized

    # Greedy single-link clustering from each unassigned seed.
    assigned = [False] * N
    clusters: list[list[int]] = []
    threshold = float(response_cohesion)
    for i in range(N):
        if assigned[i]:
            continue
        cluster = [i]
        assigned[i] = True
        # vectorized scan of remaining
        for j in range(i + 1, N):
            if assigned[j]:
                continue
            if sim[i, j] >= threshold:
                cluster.append(j)
                assigned[j] = True
        if len(cluster) >= min_cluster_size:
            clusters.append(cluster)

    if not clusters:
        return (
            f"no_clusters (n={N}, threshold={threshold}, "
            f"min={min_cluster_size}) — try lower threshold"
        )

    invariants = []
    for cl in clusters:
        cl_arr = _np.array(cl)
        sub_sim = sim[_np.ix_(cl_arr, cl_arr)]
        n = len(cl)
        # mean of off-diagonal
        if n > 1:
            cohesion = (sub_sim.sum() - n) / (n * (n - 1))
        else:
            cohesion = 1.0

        # gather preceding user prompts (one per cluster member, same session)
        prompt_embs = []
        for idx in cl:
            r = rows[idx]
            ts = r["created_at"]
            sid = r["session_id"]
            if not sid:
                continue
            prompt_join, prompt_embedding = _dialog_embedding_parts(conn, "p")
            ur = conn.execute(
                f"SELECT {prompt_embedding} AS embedding "
                f"FROM dialog_messages p {prompt_join} "
                "WHERE p.session_id=? AND p.role='user' AND p.created_at < ? "
                f"AND {prompt_embedding} IS NOT NULL "
                "AND p.content NOT LIKE '[tool_result]%' "
                "AND p.content NOT LIKE '[Image%' "
                "ORDER BY p.created_at DESC LIMIT 1",
                (sid, ts),
            ).fetchone()
            if ur and ur["embedding"]:
                prompt_embs.append(
                    _np.frombuffer(ur["embedding"], dtype="float32")
                )
        if len(prompt_embs) < min_cluster_size:
            continue
        pe = _np.stack(prompt_embs)
        psim = pe @ pe.T
        pn = len(prompt_embs)
        if pn > 1:
            avg_psim = (psim.sum() - pn) / (pn * (pn - 1))
        else:
            avg_psim = 1.0
        diversity = 1.0 - float(avg_psim)
        score = float(cohesion) * diversity

        # representative: longest message in cluster
        rep_idx = max(cl, key=lambda i: len(rows[i]["content"]))
        rep = rows[rep_idx]["content"][:240].replace("\n", " ")
        if len(rows[rep_idx]["content"]) > 240:
            rep += "…"
        invariants.append({
            "size": n,
            "cohesion": float(cohesion),
            "diversity": diversity,
            "score": score,
            "sample": rep,
        })

    invariants.sort(key=lambda x: x["score"], reverse=True)
    invariants = invariants[: max(1, int(top_n))]
    if not invariants:
        return f"no_invariants (clusters had insufficient prompt variety)"

    out = [
        f"invariants n={len(invariants)} window={window_days}d "
        f"threshold={threshold} pool={N}"
    ]
    for inv in invariants:
        out.append(
            f"  size={inv['size']} cohesion={inv['cohesion']:.2f} "
            f"diversity={inv['diversity']:.2f} score={inv['score']:.2f}"
        )
        out.append(f"    sample: {inv['sample']}")
    return "\n".join(out)
