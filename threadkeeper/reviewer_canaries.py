"""Audit the candidate reviewer with known-bad canary candidates.

A lenient judge silently turns curation off: everything it sees becomes a
skill. Every Nth review pass mixes canaries into the pending inventory. They
look like ordinary harvested candidates, but each one is something the rules
say to reject: a one-off task, a transient environment fix, incident
history, generic advice. Each carries a made-up marker word.

When the reviewer finishes, any of its proposals that contain a marker means
it accepted a canary. Those proposals are rejected, and two failed canary
passes in a row pause the reviewer until a forced pass shows it rejects them
again.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
import time

CANARY_KIND = "candidate_review_canary"
RESULT_KIND = "candidate_review_canary_result"
CANARIES_PER_PASS = 2
FAILURES_TO_PAUSE = 2
CANARY_ID_OFFSET = 1_000_000


@dataclass(frozen=True)
class Canary:
    marker: str
    kind: str
    content: str


CANARIES: tuple[Canary, ...] = (
    Canary("Quillfern", "insight",
           "Summarized today's market moves for the Quillfern portfolio and sent "
           "the digest to the team channel; the user said thanks."),
    Canary("Brambleton", "insight",
           "Running npm i fixed the Brambleton build after a fresh clone; it "
           "works now, no other change was needed."),
    Canary("Tavisham", "insight",
           "In thread T9f2 for ZEPH-4821 on 2026-03-04 the Tavisham deploy failed "
           "3 times before it passed; the user said «just retry it»."),
    Canary("Mirrowind", "insight",
           "Always write clean, readable code and test your Mirrowind changes "
           "before you finish."),
)


def canaries_for_pass(pass_number: int) -> list[Canary]:
    """A rotating pair, so every canary is exercised over time."""
    start = (pass_number * CANARIES_PER_PASS) % len(CANARIES)
    return [CANARIES[(start + offset) % len(CANARIES)] for offset in range(CANARIES_PER_PASS)]


def canary_due(conn: sqlite3.Connection, every: int) -> tuple[bool, int]:
    """(due, pass number) for the next spawned review pass."""
    if every <= 0:
        return False, 0
    row = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='candidate_review_pass' "
        "AND summary LIKE 'spawned%'"
    ).fetchone()
    spawned = int(row[0] if row else 0)
    return spawned % every == 0, spawned // every


def inventory_lines(conn: sqlite3.Connection, canaries: list[Canary]) -> list[str]:
    """Canary candidates formatted like real inventory lines."""
    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM extract_candidates").fetchone()
    base = int(row[0] if row else 0) + CANARY_ID_OFFSET
    day = time.strftime("%Y-%m-%d", time.gmtime())
    return [
        f"  #{base + index} kind={canary.kind} session=canary-{canary.marker.lower()} "
        f"day={day} why=harvested pattern\n    content: {canary.content}"
        for index, canary in enumerate(canaries, start=1)
    ]


def record_canary_pass(conn: sqlite3.Connection, task_id: str, canaries: list[Canary]) -> None:
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES ('', ?, ?, ?, ?)",
        (CANARY_KIND, task_id, json.dumps([c.marker for c in canaries]), int(time.time())),
    )
    conn.commit()


def _ended_child(conn: sqlite3.Connection, task_id: str) -> str | None:
    row = conn.execute(
        "SELECT spawned_cid FROM tasks WHERE id=? AND ended_at IS NOT NULL", (task_id,),
    ).fetchone()
    return str(row["spawned_cid"] or "") if row else None


def _leaked_proposals(conn: sqlite3.Connection, child_cid: str, markers: list[str]) -> list[int]:
    rows = conn.execute(
        "SELECT id, arguments FROM memory_proposals WHERE child_cid=?", (child_cid,),
    ).fetchall()
    return [
        int(row["id"]) for row in rows
        if any(marker.lower() in str(row["arguments"]).lower() for marker in markers)
    ]


def _reject(conn: sqlite3.Connection, proposal_ids: list[int]) -> None:
    for proposal_id in proposal_ids:
        conn.execute(
            "UPDATE memory_proposals SET status='rejected', decided_at=?, "
            "result='rejected: accepted a known-bad canary candidate' "
            "WHERE id=? AND status='pending'",
            (int(time.time()), proposal_id),
        )


def _pending_canary_passes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT c.target, c.summary FROM events c WHERE c.kind=? AND NOT EXISTS ("
        "SELECT 1 FROM events r WHERE r.kind=? AND r.target=c.target)",
        (CANARY_KIND, RESULT_KIND),
    ).fetchall()


def evaluate_finished(conn: sqlite3.Connection) -> list[str]:
    """Judge every finished canary pass once. Returns 'pass'/'fail' verdicts."""
    verdicts: list[str] = []
    for row in _pending_canary_passes(conn):
        child = _ended_child(conn, row["target"])
        if child is None:
            continue
        leaked = _leaked_proposals(conn, child, json.loads(row["summary"] or "[]")) if child else []
        _reject(conn, leaked)
        verdict = "fail" if leaked else "pass"
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES ('', ?, ?, ?, ?)",
            (RESULT_KIND, row["target"], f"{verdict} leaked={len(leaked)}", int(time.time())),
        )
        verdicts.append(verdict)
    if verdicts:
        conn.commit()
    return verdicts


def recent_results(conn: sqlite3.Connection, limit: int = FAILURES_TO_PAUSE) -> list[str]:
    rows = conn.execute(
        "SELECT summary FROM events WHERE kind=? ORDER BY id DESC LIMIT ?",
        (RESULT_KIND, limit),
    ).fetchall()
    return [str(row["summary"] or "").split(" ", 1)[0] for row in rows]


def reviewer_paused(conn: sqlite3.Connection) -> bool:
    """True after FAILURES_TO_PAUSE failed canary passes in a row."""
    results = recent_results(conn)
    return len(results) == FAILURES_TO_PAUSE and all(item == "fail" for item in results)
