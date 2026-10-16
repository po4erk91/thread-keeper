"""Immutable provenance and consequential-use gate for durable memory.

Authority is intentionally separate from relevance, confidence and tier.  A
derived artifact inherits the least-authoritative root of every declared
input.  Roots are retained separately so a repeated observation from one
principal cannot be mistaken for independent corroboration.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable


UNKNOWN = "unknown"
OBSERVED = "observed"
TRUSTED = "trusted"
_LEVEL = {UNKNOWN: 0, OBSERVED: 1, TRUSTED: 2}

# Only direct foreground/user input is trusted.  Autonomous and review writers
# are known but observed; an unrecognised origin is never silently promoted.
_ORIGIN_AUTHORITY = {
    "foreground": TRUSTED,
    "user": TRUSTED,
    "background_review": OBSERVED,
    "candidate_review": OBSERVED,
    "curator": OBSERVED,
    "evolve": OBSERVED,
    "evolve_apply": OBSERVED,
    "panel_vote": OBSERVED,
    "probe": OBSERVED,
    "shadow": OBSERVED,
    "shadow_review": OBSERVED,
    "spawned": OBSERVED,
}


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str


def authority_for_origin(origin: str) -> str | None:
    """Return the fixed class for a known write origin, else ``None``."""
    return _ORIGIN_AUTHORITY.get((origin or "").strip().lower())


def _valid(authority: str) -> bool:
    return authority in _LEVEL


def _artifact(conn: sqlite3.Connection, kind: str, artifact_id: str):
    return conn.execute(
        "SELECT authority_class, source_principal, source_channel, invalidated_at "
        "FROM memory_authority WHERE artifact_kind=? AND artifact_id=?",
        (kind, str(artifact_id)),
    ).fetchone()


def record_root(
    conn: sqlite3.Connection,
    kind: str,
    artifact_id: str,
    *,
    authority: str,
    principal: str,
    channel: str,
) -> bool:
    """Stamp a direct memory write once. Unknown roots are refused.

    ``INSERT OR IGNORE`` deliberately preserves an earlier stamp: no later
    writer can upgrade an artifact by restamping it with a stronger origin.
    """
    if not (_valid(authority) and authority != UNKNOWN and principal and channel):
        return False
    # A write-time record is fixed.  Do not append a second root to an old
    # artifact: that would make its provenance depend on a later editor.
    if _artifact(conn, kind, str(artifact_id)) is not None:
        return True
    now = int(time.time())
    conn.execute(
        "INSERT OR IGNORE INTO memory_authority "
        "(artifact_kind, artifact_id, authority_class, source_principal, source_channel, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (kind, str(artifact_id), authority, principal, channel, now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO memory_authority_roots "
        "(artifact_kind, artifact_id, principal, channel, authority_class, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (kind, str(artifact_id), principal, channel, authority, now),
    )
    return True


def record_origin_root(
    conn: sqlite3.Connection,
    kind: str,
    artifact_id: str,
    *,
    write_origin: str,
    principal: str,
    channel: str = "mcp",
) -> bool:
    authority = authority_for_origin(write_origin)
    if authority is None:
        return False
    return record_root(
        conn, kind, artifact_id, authority=authority,
        principal=principal or "unknown-principal", channel=channel,
    )


def derive(
    conn: sqlite3.Connection,
    child_kind: str,
    child_id: str,
    parents: Iterable[tuple[str, str]],
) -> bool:
    """Create immutable derivation edges and copy all original roots.

    Every supplied parent must be known and live.  This is intentionally
    fail-closed: a free-form or erased source cannot be used to mint a new
    memory artifact.
    """
    unique = list(dict.fromkeys((k, str(v)) for k, v in parents))
    if not unique:
        return False
    if _artifact(conn, child_kind, str(child_id)) is not None:
        return False
    parent_rows = []
    for kind, artifact_id in unique:
        row = _artifact(conn, kind, artifact_id)
        if row is None or row["invalidated_at"] is not None:
            return False
        parent_rows.append((kind, artifact_id, row))
    roots = []
    for kind, artifact_id, _ in parent_rows:
        rows = conn.execute(
            "SELECT principal, channel, authority_class FROM memory_authority_roots "
            "WHERE artifact_kind=? AND artifact_id=?",
            (kind, artifact_id),
        ).fetchall()
        if not rows:
            return False
        roots.extend(rows)
    lowest = min(roots, key=lambda r: _LEVEL[r["authority_class"]])
    now = int(time.time())
    conn.execute(
        "INSERT OR IGNORE INTO memory_authority "
        "(artifact_kind, artifact_id, authority_class, source_principal, source_channel, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (child_kind, str(child_id), lowest["authority_class"],
         lowest["principal"], lowest["channel"], now),
    )
    for root in roots:
        conn.execute(
            "INSERT OR IGNORE INTO memory_authority_roots "
            "(artifact_kind, artifact_id, principal, channel, authority_class, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (child_kind, str(child_id), root["principal"], root["channel"],
             root["authority_class"], now),
        )
    for parent_kind, parent_id, _ in parent_rows:
        conn.execute(
            "INSERT OR IGNORE INTO memory_derivations "
            "(parent_kind, parent_id, child_kind, child_id, created_at) VALUES (?,?,?,?,?)",
            (parent_kind, parent_id, child_kind, str(child_id), now),
        )
    return True


def source_parent(source: str) -> tuple[str, str] | None:
    """Parse the intentionally small source-reference vocabulary.

    Free-form provenance used by older callers is not a derivation reference.
    It must be replaced by a direct origin stamp or rejected by the caller.
    """
    raw = (source or "").strip()
    prefixes = {
        "dialog:": "dialog",
        "evidence:": "evidence",
        "claim:": "claim",
        "lesson:": "lesson",
        "skill:": "skill",
        "note:": "note",
        "verbatim:": "verbatim",
    }
    for prefix, kind in prefixes.items():
        if raw.startswith(prefix) and raw[len(prefix):].strip():
            return kind, raw[len(prefix):].strip()
    return None


def source_is_direct(source: str) -> bool:
    return not (source or "").strip() or (source or "").strip() in {"manual", "user"}


def source_is_live(conn: sqlite3.Connection, parent: tuple[str, str]) -> bool:
    row = _artifact(conn, parent[0], parent[1])
    return row is not None and row["invalidated_at"] is None


def can_derive(
    conn: sqlite3.Connection,
    child_kind: str,
    child_id: str,
    parent: tuple[str, str],
) -> bool:
    """Preflight a derived write before its filesystem/database payload lands."""
    return _artifact(conn, child_kind, str(child_id)) is None and source_is_live(conn, parent)


def can_stamp(
    conn: sqlite3.Connection,
    kind: str,
    artifact_id: str,
    *,
    write_origin: str,
    source: str = "",
) -> bool:
    """Preflight a root or explicit-reference stamp."""
    parent = source_parent(source)
    if parent is not None:
        return can_derive(conn, kind, artifact_id, parent)
    return authority_for_origin(write_origin) is not None


def stamp(
    conn: sqlite3.Connection,
    kind: str,
    artifact_id: str,
    *,
    write_origin: str,
    principal: str,
    channel: str,
    source: str = "",
) -> bool:
    """Stamp a new artifact from a declared parent or current direct origin."""
    parent = source_parent(source)
    if parent is not None:
        return derive(conn, kind, artifact_id, [parent])
    return record_origin_root(
        conn, kind, artifact_id, write_origin=write_origin,
        principal=principal, channel=channel,
    )


def authorize_action(
    conn: sqlite3.Connection,
    kind: str,
    artifact_id: str,
    *,
    confirmed: bool = False,
) -> GateDecision:
    """Check whether an artifact may drive a consequential action.

    Trusted direct input may act.  Observed/derived-low authority needs either
    an explicit user confirmation or a trusted root from a *different*
    principal.  Multiple repeats of one source contribute one principal only.
    """
    row = _artifact(conn, kind, artifact_id)
    if row is None:
        return GateDecision(False, "unknown_authority")
    if row["invalidated_at"] is not None:
        return GateDecision(False, "invalidated")
    if row["authority_class"] == TRUSTED:
        return GateDecision(True, "trusted_source")
    if confirmed:
        return GateDecision(True, "explicit_confirmation")
    roots = conn.execute(
        "SELECT DISTINCT principal, authority_class FROM memory_authority_roots "
        "WHERE artifact_kind=? AND artifact_id=?",
        (kind, str(artifact_id)),
    ).fetchall()
    low_principals = {r["principal"] for r in roots if r["authority_class"] != TRUSTED}
    trusted_principals = {r["principal"] for r in roots if r["authority_class"] == TRUSTED}
    if trusted_principals - low_principals:
        return GateDecision(True, "independent_trusted_corroboration")
    return GateDecision(False, "needs_trusted_corroboration_or_confirmation")


def is_visible(conn: sqlite3.Connection, kind: str, artifact_id: str) -> bool:
    """Legacy rows remain visible; stamped invalidated rows never do."""
    row = _artifact(conn, kind, artifact_id)
    return row is None or row["invalidated_at"] is None


def invalidate_descendants(
    conn: sqlite3.Connection,
    roots: Iterable[tuple[str, str]],
    *,
    reason: str = "forget",
) -> int:
    """Quarantine roots and every reachable derived artifact."""
    pending = list(dict.fromkeys((k, str(v)) for k, v in roots))
    seen: set[tuple[str, str]] = set()
    while pending:
        kind, artifact_id = pending.pop()
        if (kind, artifact_id) in seen:
            continue
        seen.add((kind, artifact_id))
        rows = conn.execute(
            "SELECT child_kind, child_id FROM memory_derivations "
            "WHERE parent_kind=? AND parent_id=?",
            (kind, artifact_id),
        ).fetchall()
        pending.extend((r["child_kind"], r["child_id"]) for r in rows)
    if not seen:
        return 0
    now = int(time.time())
    changed = 0
    for kind, artifact_id in seen:
        cur = conn.execute(
            "UPDATE memory_authority SET invalidated_at=?, invalidation_reason=? "
            "WHERE artifact_kind=? AND artifact_id=? AND invalidated_at IS NULL",
            (now, reason, kind, artifact_id),
        )
        changed += int(cur.rowcount or 0)
    return changed
