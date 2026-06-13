"""Aggregate telemetry dashboard — one compact cross-system view.

Point views already exist (`mp_health`, `spawn_budget_status`,
`shadow_review_status`, `weak_spots`, ...), but nothing answered "how is
the whole system doing": store sizes, how often each autonomous loop fires,
and what the loops actually PRODUCE (skills materialized, candidates
accepted vs rejected, tier promotions). `mp_dashboard()` is that
single-call rollup — read-only, defensive (a missing column never crashes
it), and safe to surface in any session.

It reads only aggregates from existing tables + the `events` log; it does
NOT spawn or mutate. Loop activity comes from the `*_pass` event rows each
daemon already records (probe_pass, shadow_review_pass, curator_pass,
extract_pass, candidate_review_pass, evolve_review_pass); outcomes come
from the action events (skill_materialized, accept_candidate:*,
reject_candidate, tier_promoted/demoted).
"""

from __future__ import annotations

import sqlite3
import time

from .._mcp import mcp
from ..db import get_db
from ..helpers import fmt_age
from ..identity import _ensure_session


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    """COUNT/aggregate helper. Returns 0 on any OperationalError (missing
    table/column) so the dashboard degrades gracefully on partial schemas."""
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row:
        return 0
    v = row[0]
    return int(v) if v is not None else 0


def _group_counts(conn: sqlite3.Connection, table: str,
                  col: str) -> dict[str, int]:
    """`SELECT col, COUNT(*) GROUP BY col` → dict. Empty on error."""
    try:
        rows = conn.execute(
            f"SELECT {col}, COUNT(*) FROM {table} GROUP BY {col}"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {(r[0] or "?"): int(r[1]) for r in rows}


def _fmt_group(d: dict[str, int], order: tuple[str, ...]) -> str:
    """Render a group dict in a stable key order, appending any extra keys."""
    parts = []
    for k in order:
        if k in d:
            parts.append(f"{k}={d[k]}")
    for k in sorted(d):
        if k not in order:
            parts.append(f"{k}={d[k]}")
    return " ".join(parts) if parts else "0"


# The autonomous loops, keyed by their events.kind='*_pass' marker.
_LOOP_KINDS = (
    ("ingest", "ingest_pass"),
    ("shadow", "shadow_review_pass"),
    ("extract", "extract_pass"),
    ("candidate", "candidate_review_pass"),
    ("curator", "curator_pass"),
    ("probe", "probe_pass"),
    ("evolve", "evolve_review_pass"),
    ("auto_update", "auto_update_pass"),
)

# Outcome event kinds the loops are supposed to PRODUCE.
_OUTCOME_KINDS = (
    ("skill_materialized", "skill_materialized"),
    ("tier_promoted", "tier_promoted"),
    ("tier_demoted", "tier_demoted"),
    ("skill_tier_promoted", "skill_tier_promoted"),
    ("reject_candidate", "reject_candidate"),
)


@mcp.tool()
def mp_dashboard(window_days: int = 7) -> str:
    """One-call rollup of the whole thread-keeper system: store sizes, how
    often each autonomous loop fired (in the last `window_days` and 30d),
    and what those loops actually produced (skills materialized, candidates
    accepted vs rejected, tier promotions). Read-only; no spawn, no mutate.

    Use to see system health at a glance, spot loops that fire but produce
    nothing (e.g. shadow_review passes >> skills materialized), or a backlog
    building up (e.g. extract_candidates pending climbing)."""
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    win_s = max(1, int(window_days)) * 86400
    cut_win = now - win_s
    cut_30 = now - 30 * 86400

    out: list[str] = [
        f"dashboard window={window_days}d now="
        f"{time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(now))}"
    ]

    # ── stores ────────────────────────────────────────────────────────
    threads = _group_counts(conn, "threads", "state")
    skills = _group_counts(conn, "skill_usage", "tier")
    claims = _group_counts(conn, "user_dialectic", "tier")
    cand = _group_counts(conn, "extract_candidates", "status")
    evolve = _group_counts(conn, "evolve", "COALESCE(status,'pending')")
    out.append("")
    out.append("stores")
    out.append(
        f"  threads: {_fmt_group(threads, ('active','idle','closed'))} "
        f"(total {sum(threads.values())})"
    )
    out.append(f"  notes={_scalar(conn, 'SELECT COUNT(*) FROM notes')} "
               f"dialog={_scalar(conn, 'SELECT COUNT(*) FROM dialog_messages')} "
               f"distill={_scalar(conn, 'SELECT COUNT(*) FROM distill')} "
               f"concepts={_scalar(conn, 'SELECT COUNT(*) FROM concepts')}")
    out.append(f"  skills_by_tier: "
               f"{_fmt_group(skills, ('hypothesis','observed','validated'))}")
    out.append(f"  claims_by_tier: "
               f"{_fmt_group(claims, ('hypothesis','observed','validated','disputed'))}")
    out.append(f"  extract_candidates: "
               f"{_fmt_group(cand, ('pending','accepted','rejected'))}")
    out.append(f"  evolve: "
               f"{_fmt_group(evolve, ('pending','promoted','dismissed'))}")
    out.append(
        f"  probes={_scalar(conn, 'SELECT COUNT(*) FROM probes')} "
        f"probe_results={_scalar(conn, 'SELECT COUNT(*) FROM probe_results')} "
        f"tasks_running="
        f"{_scalar(conn, 'SELECT COUNT(*) FROM tasks WHERE ended_at IS NULL')} "
        f"tasks_total={_scalar(conn, 'SELECT COUNT(*) FROM tasks')}"
    )

    # ── loops ─────────────────────────────────────────────────────────
    # Per loop: fires in window, fires in 30d, age of last fire. A loop
    # that fires a lot but whose outcomes stay flat is a duplication / waste
    # signal (the question ROADMAP item "shadow-review proof" asks).
    out.append("")
    out.append(f"loops (fires {window_days}d / 30d, last)")
    for label, kind in _LOOP_KINDS:
        n_win = _scalar(
            conn,
            "SELECT COUNT(*) FROM events WHERE kind=? AND created_at>=?",
            (kind, cut_win),
        )
        n_30 = _scalar(
            conn,
            "SELECT COUNT(*) FROM events WHERE kind=? AND created_at>=?",
            (kind, cut_30),
        )
        last = _scalar(
            conn,
            "SELECT MAX(created_at) FROM events WHERE kind=?",
            (kind,),
        )
        if n_30 == 0 and last == 0:
            continue  # never fired — skip to keep the view tight
        age = fmt_age(now - last) + "_ago" if last else "never"
        out.append(f"  {label:<10} {n_win} / {n_30}   last={age}")

    # ── outcomes ──────────────────────────────────────────────────────
    out.append("")
    out.append(f"outcomes ({window_days}d / 30d / all)")
    for label, kind in _OUTCOME_KINDS:
        n_win = _scalar(
            conn,
            "SELECT COUNT(*) FROM events WHERE kind=? AND created_at>=?",
            (kind, cut_win),
        )
        n_30 = _scalar(
            conn,
            "SELECT COUNT(*) FROM events WHERE kind=? AND created_at>=?",
            (kind, cut_30),
        )
        n_all = _scalar(
            conn, "SELECT COUNT(*) FROM events WHERE kind=?", (kind,)
        )
        if n_all == 0:
            continue
        out.append(f"  {label:<22} {n_win} / {n_30} / {n_all}")
    # accept_candidate has a per-target-kind suffix (accept_candidate:note,
    # :verbatim, ...) so it needs a LIKE rather than equality.
    acc_all = _scalar(
        conn, "SELECT COUNT(*) FROM events WHERE kind LIKE 'accept_candidate%'"
    )
    rej_all = _scalar(
        conn, "SELECT COUNT(*) FROM events WHERE kind='reject_candidate'"
    )
    decided = acc_all + rej_all
    if decided:
        rate = acc_all / decided
        out.append(
            f"  candidate_accept_rate {acc_all}/{decided} = {rate:.0%} "
            f"(accepted/decided, all-time)"
        )

    # ── reliability ───────────────────────────────────────────────────
    weak = _scalar(
        conn,
        "SELECT COUNT(*) FROM reliability "
        "WHERE fail_rate_30d IS NOT NULL AND attempts>=3 AND fail_rate_30d>0",
    )
    untested = _scalar(
        conn,
        "SELECT COUNT(DISTINCT p.category) FROM probes p "
        "LEFT JOIN reliability r ON r.category=p.category "
        "WHERE p.enabled=1 AND (r.category IS NULL OR r.attempts=0)",
    )
    out.append("")
    out.append(f"reliability  weak_categories={weak} untested_categories={untested}")

    return "\n".join(out)
