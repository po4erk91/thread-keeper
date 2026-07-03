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
daemon records — the loop list is derived from `agent_status._LOOP_DEFS`
so it never drifts from the menu-bar status surface (it covers every loop,
including the paid-spawn dialectic_validate and evolve_apply daemons and the
thread_janitor). Outcomes come from the action events: skill/tier changes,
accept/reject_candidate, AND knowledge-store mutations (lesson_append /
lesson_remove, curator_report_applied, roadmap_issue_applied, evolve_applied,
dialectic_claim / _supersede), plus a `curator_net_change` line so a loop
silently shrinking the lessons store is visible at a glance.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import time

from .._mcp import read_tool, write_tool
from ..agent_status import _LOOP_DEFS
from ..config import DB_PATH
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


def _fmt_bytes(n: int) -> str:
    n = max(0, int(n or 0))
    units = ("B", "KiB", "MiB", "GiB")
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)}B"
            return f"{value:.1f}{unit}"
        value /= 1024


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _db_sizes() -> tuple[int, int, int, int]:
    db = Path(DB_PATH)
    main = _file_size(db)
    wal = _file_size(Path(str(db) + "-wal"))
    shm = _file_size(Path(str(db) + "-shm"))
    return main, wal, shm, main + wal + shm


def _task_spend_24h(
    conn: sqlite3.Connection,
    prompt_prefix: str,
    cut_24: int,
) -> tuple[int, int, float, int]:
    """Return (spawns, tokens, cost_usd, seconds) for matching task prompts."""
    if not prompt_prefix:
        return 0, 0, 0.0, 0
    try:
        row = conn.execute(
            "SELECT COUNT(*), "
            "COALESCE(SUM(COALESCE(tokens_total, "
            "COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0))), 0), "
            "COALESCE(SUM(COALESCE(cost_usd, 0.0)), 0.0), "
            "COALESCE(SUM(COALESCE(duration_s, "
            "COALESCE(ended_at, started_at) - started_at)), 0) "
            "FROM tasks WHERE prompt LIKE ? "
            "AND COALESCE(ended_at, started_at) >= ?",
            (prompt_prefix + "%", cut_24),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0, 0, 0.0, 0
    if not row:
        return 0, 0, 0.0, 0
    return int(row[0] or 0), int(row[1] or 0), float(row[2] or 0.0), int(row[3] or 0)


def _loop_mutations_24h(conn: sqlite3.Connection, label: str, cut_24: int) -> int:
    """Best-effort count of mutation events attributable to a loop."""
    if label == "shadow_review":
        return (
            _scalar(
                conn,
                "SELECT COUNT(*) FROM events WHERE kind='lesson_append' "
                "AND summary LIKE '%source=shadow%' AND created_at>=?",
                (cut_24,),
            )
            + _scalar(
                conn,
                "SELECT COUNT(*) FROM events WHERE kind='skill_materialized' "
                "AND created_at>=?",
                (cut_24,),
            )
        )
    if label == "candidate_reviewer":
        return (
            _scalar(
                conn,
                "SELECT COUNT(*) FROM events WHERE kind LIKE 'accept_candidate%' "
                "AND created_at>=?",
                (cut_24,),
            )
            + _scalar(
                conn,
                "SELECT COUNT(*) FROM events WHERE kind='reject_candidate' "
                "AND created_at>=?",
                (cut_24,),
            )
        )
    if label == "curator":
        action_events = _scalar(
            conn,
            "SELECT COUNT(*) FROM events "
            "WHERE kind='curator_destructive_action' AND created_at>=?",
            (cut_24,),
        )
        if action_events:
            return action_events
        return (
            _scalar(
                conn,
                "SELECT COUNT(*) FROM events WHERE kind='lesson_append' "
                "AND summary LIKE '%source=curator%' AND created_at>=?",
                (cut_24,),
            )
            + _scalar(
                conn,
                "SELECT COUNT(*) FROM events WHERE kind='lesson_remove' "
                "AND created_at>=?",
                (cut_24,),
            )
        )
    if label == "dialectic_validator":
        return _scalar(
            conn,
            "SELECT COUNT(*) FROM events WHERE kind IN "
            "('dialectic_claim','dialectic_supersede','tier_promoted',"
            "'tier_demoted') AND created_at>=?",
            (cut_24,),
        )
    if label == "evolve_apply":
        return _scalar(
            conn,
            "SELECT COUNT(*) FROM events WHERE kind IN "
            "('curator_report_applied','roadmap_issue_applied',"
            "'evolve_applied') AND created_at>=?",
            (cut_24,),
        )
    return 0


# The autonomous loops, keyed by their events.kind='*_pass' marker. Derived
# from agent_status._LOOP_DEFS — the SAME source the menu-bar status surface
# reads — so the two telemetry views can never disagree on which loops exist.
# (Previously hand-listed here, which silently omitted dialectic_mine,
# dialectic_validate, evolve_apply, and thread_janitor — two of which spawn
# paid LLM children.) Label is the loop id; kind is the '*_pass' event.
_LOOP_KINDS = tuple((d["id"], d["event"]) for d in _LOOP_DEFS)
_LOOP_LABEL_W = max((len(label) for label, _ in _LOOP_KINDS), default=10)
_LOOP_TASK_PREFIXES: dict[str, str] = {
    "shadow_review": "You are a SHADOW LEARNING OBSERVER",
    "candidate_reviewer": "You are a CANDIDATE REVIEWER",
    "curator": "You are an autonomous CURATOR",
    "dialectic_validator": "You are a DIALECTIC VALIDATOR",
    "evolve_review": "You are an EVOLVE REVIEWER",
    "evolve_apply": "You are an EVOLVE APPLIER",
    "probe": "You are a PROBE RUNNER",
}

# Outcome event kinds the loops are supposed to PRODUCE. Beyond the original
# skill/tier/reject set, this now counts knowledge-store MUTATIONS — the most
# consequential autonomous behavior — so a daemon adding to or deleting from
# the lessons/claims store produces a visible number (issue #61):
#   lesson_append / lesson_remove   — curator + shadow lesson writes/prunes
#   curator_report_applied          — evolve_applier applied a curator report
#   roadmap_issue_applied           — evolve_applier opened a roadmap-issue PR
#   roadmap_issue_skipped           — evolve_applier refused human-gated issue
#   evolve_applied                  — evolve_applier marked a suggestion done
#   dialectic_claim / _supersede    — user-model claim mutations
_OUTCOME_KINDS = (
    ("skill_materialized", "skill_materialized"),
    ("tier_promoted", "tier_promoted"),
    ("tier_demoted", "tier_demoted"),
    ("skill_tier_promoted", "skill_tier_promoted"),
    ("reject_candidate", "reject_candidate"),
    ("lesson_append", "lesson_append"),
    ("lesson_remove", "lesson_remove"),
    ("curator_snapshot", "curator_snapshot"),
    ("curator_destructive_action", "curator_destructive_action"),
    ("curator_restore", "curator_restore"),
    ("curator_report_applied", "curator_report_applied"),
    ("roadmap_issue_applied", "roadmap_issue_applied"),
    ("roadmap_issue_skipped", "roadmap_issue_skipped"),
    ("evolve_applied", "evolve_applied"),
    ("dialectic_claim", "dialectic_claim"),
    ("dialectic_supersede", "dialectic_supersede"),
)
_OUTCOME_LABEL_W = max((len(label) for label, _ in _OUTCOME_KINDS), default=22)


@read_tool()
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
    cut_24 = now - 86400
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
    db_main, db_wal, db_shm, db_total = _db_sizes()
    out.append(
        f"  db_size={_fmt_bytes(db_total)} "
        f"main={_fmt_bytes(db_main)} wal={_fmt_bytes(db_wal)} "
        f"shm={_fmt_bytes(db_shm)}"
    )
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
    # tasks_timed_out: children the wall-clock watchdog killed for running past
    # SPAWN_MAX_RUNTIME_S (#80), keyed off the timeout sentinel return_code so a
    # runtime kill is observable here rather than silent.
    from ..spawn_budget import SPAWN_TIMEOUT_RETURN_CODE
    timed_out = _scalar(
        conn, "SELECT COUNT(*) FROM tasks WHERE return_code=?",
        (SPAWN_TIMEOUT_RETURN_CODE,),
    )
    out.append(
        f"  probes={_scalar(conn, 'SELECT COUNT(*) FROM probes')} "
        f"probe_results={_scalar(conn, 'SELECT COUNT(*) FROM probe_results')} "
        f"tasks_running="
        f"{_scalar(conn, 'SELECT COUNT(*) FROM tasks WHERE ended_at IS NULL')} "
        f"tasks_total={_scalar(conn, 'SELECT COUNT(*) FROM tasks')} "
        f"tasks_timed_out={timed_out}"
    )
    out.append(
        "  high_volume: "
        f"dialog_messages={_scalar(conn, 'SELECT COUNT(*) FROM dialog_messages')} "
        f"dialog_fts={_scalar(conn, 'SELECT COUNT(*) FROM dialog_fts')} "
        f"dialog_vec={_scalar(conn, 'SELECT COUNT(*) FROM dialog_vec')} "
        f"dialog_vec_map={_scalar(conn, 'SELECT COUNT(*) FROM dialog_vec_map')} "
        f"events={_scalar(conn, 'SELECT COUNT(*) FROM events')} "
        f"signals={_scalar(conn, 'SELECT COUNT(*) FROM signals')} "
        f"tasks={_scalar(conn, 'SELECT COUNT(*) FROM tasks')} "
        f"probe_results={_scalar(conn, 'SELECT COUNT(*) FROM probe_results')}"
    )

    # ── loops ─────────────────────────────────────────────────────────
    # Per loop: fires in window, fires in 30d, age of last fire. A loop
    # that fires a lot but whose outcomes stay flat is a duplication / waste
    # signal (the question ROADMAP item "shadow-review proof" asks).
    out.append("")
    out.append(
        f"loops (fires {window_days}d / 30d, last, 24h spend/mutations)"
    )
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
        spawns24, tokens24, cost24, seconds24 = _task_spend_24h(
            conn, _LOOP_TASK_PREFIXES.get(label, ""), cut_24,
        )
        mutations24 = _loop_mutations_24h(conn, label, cut_24)
        out.append(
            f"  {label:<{_LOOP_LABEL_W}} {n_win} / {n_30}   last={age} "
            f"spawns24={spawns24} tokens24={tokens24} "
            f"spend24=${cost24:.4f} time24={seconds24}s "
            f"mutations24={mutations24}"
        )

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
        out.append(f"  {label:<{_OUTCOME_LABEL_W}} {n_win} / {n_30} / {n_all}")
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

    # Knowledge-store net change in the window. A loop silently shrinking the
    # lessons store (e.g. a destructive curator pass auto-pruning) is otherwise
    # invisible — curator_pass only logs "spawned lessons=N..." at spawn time,
    # not the deletions the async child makes. lesson_append events carry an
    # `op=create|replace` summary prefix so we can split brand-new additions
    # from in-place patches; lesson_remove is always a deletion. Always shown
    # (even all-zero) so a shrink is visible at a glance.
    added = _scalar(
        conn,
        "SELECT COUNT(*) FROM events WHERE kind='lesson_append' "
        "AND summary LIKE 'op=create%' AND created_at>=?",
        (cut_win,),
    )
    patched = _scalar(
        conn,
        "SELECT COUNT(*) FROM events WHERE kind='lesson_append' "
        "AND summary LIKE 'op=replace%' AND created_at>=?",
        (cut_win,),
    )
    removed = _scalar(
        conn,
        "SELECT COUNT(*) FROM events WHERE kind='lesson_remove' AND created_at>=?",
        (cut_win,),
    )
    out.append(
        f"  curator_net_change added={added} removed={removed} "
        f"patched={patched} net={added - removed:+d} (lessons, {window_days}d)"
    )
    action_labels = (
        "lesson_pruned",
        "lesson_consolidated",
        "lesson_patched",
        "skill_deleted",
        "skill_consolidated",
        "skill_patched",
    )
    action_counts = {
        label: _scalar(
            conn,
            "SELECT COUNT(*) FROM events "
            "WHERE kind='curator_destructive_action' "
            "AND summary LIKE ? AND created_at>=?",
            (f"action={label} %", cut_win),
        )
        for label in action_labels
    }
    snapshots = _scalar(
        conn,
        "SELECT COUNT(*) FROM events WHERE kind='curator_snapshot' "
        "AND created_at>=?",
        (cut_win,),
    )
    out.append(
        "  curator_destructive_actions "
        f"snapshots={snapshots} "
        + " ".join(f"{k}={v}" for k, v in action_counts.items())
        + f" ({window_days}d)"
    )
    # ── roadmap applier (poison-issue backoff / dead-letter) ───────────
    # Issues the evolve applier has spawned a child for, split by outcome.
    # `stuck` = attempted but neither handed off (PR) nor dead-lettered yet
    # (in backoff or eligible-again); `dead_letter` = capped out and excluded
    # from the auto-drain pending a human. A climbing dead_letter count is a
    # poison-issue / cost-waste signal.
    rm_attempted = _scalar(
        conn,
        "SELECT COUNT(DISTINCT target) FROM events "
        "WHERE kind='roadmap_issue_attempt'",
    )
    if rm_attempted:
        rm_applied = _scalar(
            conn,
            "SELECT COUNT(DISTINCT target) FROM events "
            "WHERE kind='roadmap_issue_applied'",
        )
        rm_dead = _scalar(
            conn,
            "SELECT COUNT(DISTINCT target) FROM events "
            "WHERE kind='roadmap_issue_dead_letter'",
        )
        rm_stuck = _scalar(
            conn,
            "SELECT COUNT(*) FROM (SELECT target FROM events "
            "WHERE kind='roadmap_issue_attempt' AND target NOT IN "
            "(SELECT target FROM events WHERE kind='roadmap_issue_applied') "
            "AND target NOT IN (SELECT target FROM events "
            "WHERE kind='roadmap_issue_dead_letter') GROUP BY target)",
        )
        out.append("")
        out.append(
            f"roadmap applier  attempted={rm_attempted} applied={rm_applied} "
            f"stuck={rm_stuck} dead_letter={rm_dead}"
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
