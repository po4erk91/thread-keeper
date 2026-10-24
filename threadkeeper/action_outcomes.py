"""Bounded, offline-only action/outcome records for recovery evaluation.

The stream deliberately stores identifiers and coarse classifications, never
tool arguments, tool output, transcript text, paths, or secrets. It is not a
live ingester: tests and the offline memory-quality fixture are its only
writers until a separately reviewed rollout enables one.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Iterable

from .authority import derive, invalidate_descendants, record_root, source_is_live


ACTION_OUTCOME_KIND = "action_outcome"
ACTION_OUTCOME_CANDIDATE_KIND = "action_outcome_candidate"
OUTCOMES = {"success", "failure", "cancelled", "skipped"}
AUTHORITIES = {"observed", "trusted"}
DEFAULT_RETENTION_DAYS = 30

_IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*")
_SENSITIVE_IDENTIFIER_RE = re.compile(
    r"(?i)(?:api[_-]?key|secret|token|password|passwd|credential|"
    r"authorization|bearer|gh[pousr]_|github_pat_|sk-(?:ant|proj)?-?)"
)


def _bounded_identifier(value: object, *, name: str, limit: int = 96) -> str:
    cleaned = str(value or "").strip().lower()
    if (
        not cleaned
        or len(cleaned) > limit
        or not _IDENTIFIER_RE.fullmatch(cleaned)
        or _SENSITIVE_IDENTIFIER_RE.search(cleaned)
    ):
        raise ValueError(f"{name} must be a bounded identifier")
    return cleaned


def _bounded_authority(value: str) -> str:
    authority = str(value or "").strip().lower()
    if authority not in AUTHORITIES:
        raise ValueError("authority must be observed or trusted")
    return authority


def record_live_event(*_args, **_kwargs) -> str:
    """Refuse live writes while this remains an offline-only prototype."""
    raise RuntimeError("action/outcome capture is available only to offline evaluation")


def record_evaluation_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    objective_ref: str,
    action_name: str,
    outcome: str,
    sequence: int,
    source_kind: str,
    source_id: str,
    authority: str,
    principal: str,
    channel: str,
    error_fingerprint: str = "",
    recovered_from: str = "",
    created_at: int | None = None,
    retention_days: float = DEFAULT_RETENTION_DAYS,
) -> str:
    """Store one synthetic/offline evaluation record with no payload fields."""
    event_id = _bounded_identifier(event_id, name="event_id")
    objective_ref = _bounded_identifier(objective_ref, name="objective_ref")
    action_name = _bounded_identifier(action_name, name="action_name", limit=64)
    source_kind = _bounded_identifier(source_kind, name="source_kind", limit=48)
    source_id = _bounded_identifier(source_id, name="source_id")
    authority = _bounded_authority(authority)
    outcome = str(outcome or "").strip().lower()
    if outcome not in OUTCOMES:
        raise ValueError("outcome must be a coarse outcome")
    try:
        sequence = int(sequence)
    except (TypeError, ValueError) as exc:
        raise ValueError("sequence must be positive") from exc
    if sequence < 1:
        raise ValueError("sequence must be positive")
    try:
        retention_days = float(retention_days)
    except (TypeError, ValueError) as exc:
        raise ValueError("retention_days must be positive") from exc
    if retention_days <= 0:
        raise ValueError("retention_days must be positive")
    fingerprint = str(error_fingerprint or "").strip().lower()
    if outcome == "failure":
        fingerprint = _bounded_identifier(
            fingerprint, name="error_fingerprint", limit=96,
        )
    elif fingerprint:
        raise ValueError("only failures may retain an error fingerprint")
    recovery_id = ""
    if recovered_from:
        recovery_id = _bounded_identifier(recovered_from, name="recovered_from")
        prior = conn.execute(
            "SELECT objective_ref, action_name, outcome, sequence FROM action_outcome_events "
            "WHERE id=?",
            (recovery_id,),
        ).fetchone()
        if prior is None:
            raise ValueError("recovery must reference an existing failure")
        if prior["objective_ref"] != objective_ref:
            raise ValueError("recovery must reference the same objective")
        if prior["action_name"] != action_name:
            raise ValueError("recovery must retry the same action")
        if prior["outcome"] != "failure" or int(prior["sequence"]) >= sequence:
            raise ValueError("recovery must reference an earlier failure")
        if outcome != "success":
            raise ValueError("only success may be a recovery")
    if conn.execute(
        "SELECT 1 FROM action_outcome_events WHERE id=?", (event_id,)
    ).fetchone():
        raise ValueError("event_id already exists")
    now = int(time.time()) if created_at is None else int(created_at)
    expires_at = now + int(retention_days * 86400)
    conn.execute(
        "INSERT INTO action_outcome_events "
        "(id, objective_ref, action_name, outcome, error_fingerprint, "
        "recovered_from, sequence, source_kind, source_id, authority_class, "
        "provenance_version, created_at, expires_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            event_id, objective_ref, action_name, outcome, fingerprint or None,
            recovery_id or None, sequence, source_kind, source_id, authority,
            1, now, expires_at,
        ),
    )
    if not record_root(
        conn,
        ACTION_OUTCOME_KIND,
        event_id,
        authority=authority,
        principal=_bounded_identifier(principal, name="principal"),
        channel=_bounded_identifier(channel, name="channel"),
    ):
        raise RuntimeError("could not record action/outcome authority")
    return event_id


def _recovery_rows(conn: sqlite3.Connection, event_id: str):
    return conn.execute(
        "SELECT succeeded.id AS success_id, succeeded.action_name, "
        "failed.id AS failure_id, failed.error_fingerprint "
        "FROM action_outcome_events succeeded "
        "JOIN action_outcome_events failed ON failed.id=succeeded.recovered_from "
        "WHERE succeeded.id=? AND succeeded.outcome='success' "
        "AND failed.outcome='failure' AND failed.objective_ref=succeeded.objective_ref "
        "AND failed.action_name=succeeded.action_name "
        "AND failed.sequence<succeeded.sequence",
        (event_id,),
    ).fetchone()


def derive_recovery_candidate(conn: sqlite3.Connection, event_id: str) -> int:
    """Put a bounded recovery candidate into the existing review pipeline."""
    event_id = _bounded_identifier(event_id, name="event_id")
    recovery = _recovery_rows(conn, event_id)
    if recovery is None:
        raise ValueError("event has no valid recovery link")
    parents = [
        (ACTION_OUTCOME_KIND, str(recovery["failure_id"])),
        (ACTION_OUTCOME_KIND, str(recovery["success_id"])),
    ]
    if not all(source_is_live(conn, parent) for parent in parents):
        raise RuntimeError("recovery evidence is invalidated")
    existing = conn.execute(
        "SELECT candidate_id FROM action_outcome_candidates WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if existing:
        return int(existing["candidate_id"])
    content = (
        f"Operational recovery: retry {recovery['action_name']} after "
        f"{recovery['error_fingerprint']}."
    )
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO extract_candidates "
        "(kind, source_uuid, source_cid, content, rationale, status, created_at) "
        "VALUES ('note', ?, NULL, ?, 'operational_recovery', 'pending', ?)",
        (f"action-outcome:{event_id}", content, now),
    )
    candidate_id = int(cur.lastrowid)
    if not derive(
        conn,
        ACTION_OUTCOME_CANDIDATE_KIND,
        str(candidate_id),
        parents,
    ):
        raise RuntimeError("could not authorize recovery candidate")
    conn.execute(
        "INSERT INTO action_outcome_candidates(candidate_id, event_id, created_at) "
        "VALUES (?,?,?)",
        (candidate_id, event_id, now),
    )
    return candidate_id


def candidate_source(conn: sqlite3.Connection, candidate_id: int) -> tuple[str, str] | None:
    """Return the only source form lessons may use for an operational candidate."""
    row = conn.execute(
        "SELECT candidate_id FROM action_outcome_candidates WHERE candidate_id=?",
        (int(candidate_id),),
    ).fetchone()
    if row is None:
        return None
    return (ACTION_OUTCOME_CANDIDATE_KIND, str(row["candidate_id"]))


def event_ids_for_sources(
    conn: sqlite3.Connection,
    sources: Iterable[tuple[str, str]],
) -> list[str]:
    """Find records by source pointer without inspecting source content."""
    # This path receives already-persisted selectors from `forget`. Do not
    # reject a legacy dialog/session id merely because it was never eligible
    # for a new bounded record; it is only compared against our own normalized
    # source pointers and is never persisted here.
    pairs = [
        (str(kind or "").strip().lower(), str(source_id or "").strip().lower())
        for kind, source_id in sources
    ]
    pairs = [(kind, source_id) for kind, source_id in pairs if kind and source_id]
    if not pairs:
        return []
    clauses = " OR ".join("(source_kind=? AND source_id=?)" for _ in pairs)
    params = [part for pair in pairs for part in pair]
    rows = conn.execute(
        "SELECT id FROM action_outcome_events WHERE " + clauses,
        params,
    ).fetchall()
    return [str(row["id"]) for row in rows]


def _forget_events(
    conn: sqlite3.Connection,
    event_ids: Iterable[str],
    *,
    reason: str,
) -> dict[str, int]:
    ids = list(dict.fromkeys(
        _bounded_identifier(event_id, name="event_id") for event_id in event_ids
    ))
    if not ids:
        return {"events": 0, "candidates": 0}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT candidate_id FROM action_outcome_candidates "
        f"WHERE event_id IN ({placeholders})",
        ids,
    ).fetchall()
    candidate_ids = [int(row["candidate_id"]) for row in rows]
    invalidate_descendants(
        conn,
        [(ACTION_OUTCOME_KIND, event_id) for event_id in ids],
        reason=reason,
    )
    if candidate_ids:
        candidate_placeholders = ",".join("?" for _ in candidate_ids)
        conn.execute(
            f"DELETE FROM extract_candidates WHERE id IN ({candidate_placeholders})",
            candidate_ids,
        )
    conn.execute(
        f"DELETE FROM action_outcome_candidates WHERE event_id IN ({placeholders})",
        ids,
    )
    cur = conn.execute(
        f"DELETE FROM action_outcome_events WHERE id IN ({placeholders})",
        ids,
    )
    return {"events": int(cur.rowcount or 0), "candidates": len(candidate_ids)}


def forget_objective(conn: sqlite3.Connection, objective_ref: str) -> dict[str, int]:
    """Erase all operational records for an opaque objective reference."""
    objective_ref = _bounded_identifier(objective_ref, name="objective_ref")
    rows = conn.execute(
        "SELECT id FROM action_outcome_events WHERE objective_ref=?", (objective_ref,)
    ).fetchall()
    return _forget_events(conn, (row["id"] for row in rows), reason="objective_forget")


def forget_sources(
    conn: sqlite3.Connection,
    sources: Iterable[tuple[str, str]],
) -> dict[str, int]:
    """Erase records sourced from forgotten source references."""
    return _forget_events(conn, event_ids_for_sources(conn, sources), reason="source_forget")


def prune_expired(conn: sqlite3.Connection, now: int | None = None) -> dict[str, int]:
    """Apply each record's explicit retention deadline."""
    cutoff = int(time.time()) if now is None else int(now)
    rows = conn.execute(
        "SELECT id FROM action_outcome_events WHERE expires_at <= ?", (cutoff,)
    ).fetchall()
    return _forget_events(conn, (row["id"] for row in rows), reason="retention")


def export_events(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Return the allowlisted export shape; payloads and text have no route out."""
    rows = conn.execute(
        "SELECT id, objective_ref, action_name, outcome, error_fingerprint, "
        "recovered_from, sequence, source_kind, source_id, authority_class, "
        "provenance_version, created_at, expires_at "
        "FROM action_outcome_events ORDER BY objective_ref, sequence, id"
    ).fetchall()
    return [dict(row) for row in rows]


def _fixture_records(fixture: dict) -> tuple[list[dict[str, object]], set[tuple[str, str]]]:
    by_id = {str(item.get("id", "")): item for item in fixture.get("events", [])}
    records: list[dict[str, object]] = []
    recoveries: set[tuple[str, str]] = set()
    for item in fixture.get("events", []):
        event_id = _bounded_identifier(item.get("id"), name="event_id")
        objective_ref = _bounded_identifier(item.get("objective_ref"), name="objective_ref")
        action_name = _bounded_identifier(item.get("action_name"), name="action_name", limit=64)
        outcome = str(item.get("outcome", "")).lower()
        sequence = int(item.get("sequence", 0))
        if outcome not in OUTCOMES or sequence < 1:
            raise ValueError("offline fixture has an invalid event")
        fingerprint = str(item.get("error_fingerprint") or "").lower()
        if outcome == "failure":
            fingerprint = _bounded_identifier(
                fingerprint, name="error_fingerprint", limit=96,
            )
        recovered_from = str(item.get("recovered_from") or "")
        if recovered_from:
            prior = by_id.get(recovered_from)
            if (
                prior is not None
                and prior.get("outcome") == "failure"
                and prior.get("objective_ref") == item.get("objective_ref")
                and prior.get("action_name") == item.get("action_name")
                and int(prior.get("sequence", 0)) < sequence
                and outcome == "success"
            ):
                recoveries.add((recovered_from, event_id))
            else:
                recovered_from = ""
        records.append({
            "id": event_id,
            "objective_ref": objective_ref,
            "action_name": action_name,
            "outcome": outcome,
            "error_fingerprint": fingerprint or None,
            "recovered_from": recovered_from or None,
            "sequence": sequence,
        })
    return records, recoveries


def live_ingestion_allowed(configured: bool, report: dict) -> bool:
    """Live capture needs both an explicit opt-in and a passing offline gate."""
    return bool(configured and report.get("thresholds_pass"))


def evaluate_offline_fixture(fixture: dict) -> dict[str, object]:
    """Compare the bounded stream with dialogue-only recovery evidence."""
    records, recoveries = _fixture_records(fixture)
    expected = {
        (str(pair[0]), str(pair[1]))
        for pair in fixture.get("expected_recoveries", [])
        if len(pair) == 2
    }
    forbidden = [str(value) for value in fixture.get("forbidden_substrings", [])]
    dialogue = "\n".join(str(value) for value in fixture.get("dialogue_only", []))
    exported = json.dumps(records, sort_keys=True, separators=(",", ":"))
    correct_recoveries = len(recoveries & expected)
    usefulness = correct_recoveries / len(expected) if expected else 1.0
    bounded = {
        "lesson_usefulness": round(usefulness, 4),
        "privacy_leakage": sum(value in exported for value in forbidden),
        "storage_bytes": len(exported.encode("utf-8")),
        "false_causal_links": len(recoveries - expected),
    }
    dialogue_only = {
        "lesson_usefulness": 0.0,
        "privacy_leakage": sum(value in dialogue for value in forbidden),
        "storage_bytes": len(dialogue.encode("utf-8")),
        "false_causal_links": 0,
    }
    storage_ratio = (
        bounded["storage_bytes"] / dialogue_only["storage_bytes"]
        if dialogue_only["storage_bytes"] else 0.0
    )
    thresholds = dict(fixture.get("thresholds") or {})
    thresholds_pass = (
        bounded["lesson_usefulness"] >= float(thresholds.get("min_lesson_usefulness", 1.0))
        and bounded["privacy_leakage"] <= int(thresholds.get("max_privacy_leakage", 0))
        and storage_ratio <= float(thresholds.get("max_storage_ratio", 1.0))
        and bounded["false_causal_links"] <= int(thresholds.get("max_false_causal_links", 0))
    )
    report: dict[str, object] = {
        "dialogue_only": dialogue_only,
        "bounded_action_outcomes": {**bounded, "storage_ratio": round(storage_ratio, 4)},
        "thresholds": thresholds,
        "thresholds_pass": thresholds_pass,
    }
    report["live_ingestion_allowed"] = live_ingestion_allowed(False, report)
    return report
