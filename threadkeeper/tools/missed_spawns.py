"""Missed-spawn detection MCP tool.

Scans recent assistant messages for response shapes that signal
decomposable work (multiple top-level numbered items or multiple
markdown section headers) and checks whether the conversation
actually called spawn() around the same time. Responses with
decomposable shape but no nearby spawn() are flagged as
`missed_spawn` candidates — places where the agent answered
linearly when it could have parallelized.

This is a behavioral mirror: it doesn't change anything, it tells
you how often spawn() reflex actually fires.
"""

import re
import sqlite3
import time
from datetime import datetime, timezone

from .._mcp import read_tool, write_tool
from ..db import get_db
from ..helpers import fmt_age, q
from ..identity import _ensure_session


# Top-level numbered enumeration in markdown. Allows up to 3 leading
# spaces, optional ** wrap. Each match = one numbered item.
_NUMBERED_RE = re.compile(r"(?m)^[ \t]{0,3}(?:\*\*)?\d+[\.\)][ \t]+")

# H2 / H3 markdown headers. We don't count H1 (rare in chat replies).
_HEADER_RE = re.compile(r"(?m)^#{2,3}\s+\S")

# Time window (seconds) around an assistant message in which a tasks row
# counts as "the spawn for this response". 10 min is generous.
_SPAWN_PROXIMITY_S = 600


@read_tool()
def find_missed_spawns(window_days: int = 14,
                       min_response_len: int = 400,
                       min_numbered: int = 2,
                       min_headers: int = 3,
                       top_n: int = 10,
                       max_messages: int = 5000) -> str:
    """Find assistant responses that decomposed into independent blocks
    but were answered linearly (no spawn() call nearby).

    Algorithm:
      1. Pull recent assistant messages (last `window_days` days,
         length ≥ `min_response_len`, excluding subagent jsonls).
      2. For each, count top-level numbered items and H2/H3 headers.
      3. Mark as `decomposable` if numbered ≥ `min_numbered` OR
         headers ≥ `min_headers`.
      4. For each decomposable response, check whether any tasks row
         with parent_cid = response's session_id has started_at within
         ±10 min of the response. If none → missed_spawn.
      5. Return top `top_n` by score (numbered + headers).

    Use this to calibrate the spawn_hint: a high missed-spawn count
    means the hint isn't strong enough, or thresholds need tuning.
    """
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    cutoff = now - max(1, int(window_days)) * 86400

    rows = conn.execute(
        "SELECT uuid, session_id, content, created_at "
        "FROM dialog_messages "
        "WHERE role='assistant' AND created_at >= ? "
        "AND project != 'subagents' "
        "AND length(content) >= ? "
        "AND content NOT LIKE '[thinking]%' "
        "AND content NOT LIKE '[tool_result]%' "
        "AND content NOT LIKE '<summary>%' "
        "ORDER BY created_at DESC LIMIT ?",
        (cutoff, max(100, int(min_response_len)), max(100, int(max_messages))),
    ).fetchall()

    if not rows:
        return f"insufficient_data scanned=0 window_days={window_days}"

    candidates = []
    for r in rows:
        content = r["content"] or ""
        n_num = len(_NUMBERED_RE.findall(content))
        n_hdr = len(_HEADER_RE.findall(content))
        if n_num < min_numbered and n_hdr < min_headers:
            continue
        candidates.append({
            "uuid": r["uuid"],
            "session_id": r["session_id"],
            "content": content,
            "created_at": r["created_at"],
            "numbered": n_num,
            "headers": n_hdr,
        })

    if not candidates:
        return (f"scanned={len(rows)} decomposable=0 — no responses "
                "matched decomp shape thresholds")

    # For each candidate, look for a tasks row close in time.
    missed = []
    for c in candidates:
        spawned = conn.execute(
            "SELECT COUNT(*) cnt FROM tasks "
            "WHERE parent_cid = ? "
            "AND started_at BETWEEN ? AND ?",
            (c["session_id"],
             c["created_at"] - _SPAWN_PROXIMITY_S,
             c["created_at"] + _SPAWN_PROXIMITY_S),
        ).fetchone()["cnt"]
        if spawned == 0:
            missed.append(c)

    if not missed:
        return (f"scanned={len(rows)} decomposable={len(candidates)} "
                "missed=0 — every decomposable response had a nearby spawn()")

    # Rank by score = numbered + headers (rough decomposition intensity).
    missed.sort(key=lambda x: -(x["numbered"] + x["headers"]))
    top = missed[: max(1, int(top_n))]

    out = [
        f"missed_spawn scanned={len(rows)} decomposable={len(candidates)} "
        f"missed={len(missed)} window={window_days}d"
    ]
    for c in top:
        iso = datetime.fromtimestamp(c["created_at"], tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%MZ"
        )
        sid_short = (c["session_id"] or "?")[:8]
        sample = c["content"][:120].replace("\n", " ")
        if len(c["content"]) > 120:
            sample += "…"
        out.append(
            f"  {iso} sid={sid_short} nbr={c['numbered']} "
            f"hdr={c['headers']} age={fmt_age(now - c['created_at'])}_ago "
            f"{q(sample)}"
        )
    return "\n".join(out)
