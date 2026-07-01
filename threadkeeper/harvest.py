"""Shared harvest-boundary filters for dialog-derived learning loops.

Raw transcript ingest keeps agent-child rows for diagnostics, but anything
that turns dialog into durable memory must exclude autonomous loop sessions.
This module centralizes that provenance rule so shadow, extract, dialectic,
and passive skill-use accounting do not drift.
"""
from __future__ import annotations

import sqlite3


# Opening lines of prompts thread-keeper injects into autonomous children.
# A session with one of these user openers is loop output, not foreground
# user-facing signal.
INTERNAL_PROMPT_PREFIXES: tuple[str, ...] = (
    "You are a SHADOW LEARNING OBSERVER",
    "You are reviewing closed thread",
    "You are a PROBE RUNNER",
    "You are an EVOLVE REVIEWER",
    "You are an EVOLVE APPLIER",
    "You are an autonomous CURATOR",
    "You are a CANDIDATE REVIEWER",
    "You are a DIALECTIC VALIDATOR",
)

# Preamble injected by threadkeeper.spawn(). Some adapters store the rollout
# UUID rather than THREADKEEPER_FORCE_CID; the preamble still marks the whole
# transcript as child work.
SPAWNED_SESSION_MARKERS: tuple[str, ...] = (
    "You were spawned in the background by parent conversation",
)

# Native Agent/Workflow sessions commonly surface as parent_cid values rather
# than spawned_cid rows. They are not user-facing sessions; treating them as
# harvest roots keeps their descendants out of the learning loops even when
# their own prompt is ordinary task framing.
_NATIVE_AGENT_CID_PREFIXES: tuple[str, ...] = ("agent-",)


def _internal_prompt_prefix_predicate(column: str) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    for prefix in INTERNAL_PROMPT_PREFIXES:
        clauses.append(f"substr({column}, 1, ?) = ?")
        params.extend([len(prefix), prefix])
    return " OR ".join(clauses) or "0", params


def session_marker_predicate(column: str = "content") -> tuple[str, list]:
    """SQL predicate for one user-message column carrying an internal marker."""
    clauses: list[str] = []
    params: list = []
    prefix_sql, prefix_params = _internal_prompt_prefix_predicate(column)
    clauses.append(prefix_sql)
    params.extend(prefix_params)
    for marker in SPAWNED_SESSION_MARKERS:
        clauses.append(f"instr({column}, ?) > 0")
        params.append(marker)
    return " OR ".join(clauses) or "0", params


def _native_agent_predicate(column: str) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    for prefix in _NATIVE_AGENT_CID_PREFIXES:
        clauses.append(f"{column} LIKE ?")
        params.append(prefix + "%")
    return " OR ".join(clauses) or "0", params


def harvest_exclusion_cte(
    cte_name: str = "harvest_excluded_sessions",
) -> tuple[str, list]:
    """Return a recursive CTE listing sessions excluded from harvest.

    The root set keeps the old fast boundaries:
      * direct threadkeeper.spawn children from tasks.spawned_cid
      * sessions with known internal prompt openers / spawn preambles

    It also adds native Agent/Workflow parent cids and recursively expands
    through tasks.parent_cid -> tasks.spawned_cid, so descendants of the
    autonomous review tree stay excluded even when their own prompt lacks the
    daemon spawn marker.
    """
    dialog_marker_sql, dialog_marker_params = session_marker_predicate("content")
    prompt_prefix_sql, prompt_prefix_params = _internal_prompt_prefix_predicate(
        "prompt"
    )
    native_parent_sql, native_parent_params = _native_agent_predicate("parent_cid")
    sql = (
        f"WITH RECURSIVE {cte_name}(session_id) AS ("
        "  SELECT spawned_cid FROM tasks WHERE spawned_cid IS NOT NULL "
        "  UNION "
        "  SELECT DISTINCT session_id FROM dialog_messages "
        "  WHERE session_id IS NOT NULL AND role='user' "
        f"  AND ({dialog_marker_sql}) "
        "  UNION "
        "  SELECT spawned_cid FROM tasks "
        "  WHERE spawned_cid IS NOT NULL "
        f"  AND ({prompt_prefix_sql}) "
        "  UNION "
        "  SELECT DISTINCT parent_cid FROM tasks "
        "  WHERE parent_cid IS NOT NULL "
        f"  AND ({native_parent_sql}) "
        "  UNION "
        f"  SELECT t.spawned_cid FROM tasks t JOIN {cte_name} h "
        "  ON t.parent_cid = h.session_id "
        "  WHERE t.spawned_cid IS NOT NULL"
        ")"
    )
    return (
        sql + " ",
        [*dialog_marker_params, *prompt_prefix_params, *native_parent_params],
    )


def is_harvest_excluded_session(
    conn: sqlite3.Connection,
    session_id: str | None,
) -> bool:
    """True when a session belongs to the autonomous harvest-excluded tree."""
    sid = (session_id or "").strip()
    if not sid:
        return False
    try:
        if conn.execute(
            "SELECT 1 FROM tasks WHERE spawned_cid=? LIMIT 1",
            (sid,),
        ).fetchone() is not None:
            return True
        if any(sid.startswith(prefix) for prefix in _NATIVE_AGENT_CID_PREFIXES):
            if conn.execute(
                "SELECT 1 FROM tasks WHERE parent_cid=? LIMIT 1",
                (sid,),
            ).fetchone() is not None:
                return True
        marker_sql, marker_params = session_marker_predicate("content")
        if conn.execute(
            "SELECT 1 FROM dialog_messages "
            "WHERE session_id=? AND role='user' "
            f"AND ({marker_sql}) LIMIT 1",
            (sid, *marker_params),
        ).fetchone() is not None:
            return True
        cte_sql, cte_params = harvest_exclusion_cte()
        return (
            conn.execute(
                cte_sql
                + " SELECT 1 FROM harvest_excluded_sessions "
                "WHERE session_id=? LIMIT 1",
                (*cte_params, sid),
            ).fetchone()
            is not None
        )
    except sqlite3.OperationalError:
        return False
