"""Deterministic telemetry digest the model router reasons over.

Everything here is computed by the parent from the `tasks` table, task logs,
past decisions and the model catalog, so the router child sees exactly the
numbers a test can assert.
"""
from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Optional

from .agent_metadata import role_metadata
from .model_routes import CliOptions, Route, recent_routes, triple

WINDOW_S = 30 * 86400
WEEK_S = 7 * 86400
OUTCOME_SAMPLES = 5
OUTCOME_CHARS = 240
MAX_DIGEST_CHARS = 60_000
# Trim order when the digest is too long: outcome samples first, then the
# oldest weekly buckets.
_TRIM_STEPS = ((OUTCOME_SAMPLES, 5), (0, 5), (0, 3), (0, 1))
_ID_RE = re.compile(r"\b(?:tk_[0-9a-f]+|[0-9a-f]{8}-[0-9a-f-]{27,})\b")
_NUMBER_RE = re.compile(r"\d+")

TaskText = Callable[[str], str]


@dataclass
class ArmWeek:
    runs: int = 0
    ok: int = 0
    failed: int = 0
    unknown: int = 0
    running: int = 0
    durations: list[int] = field(default_factory=list)
    tokens: list[int] = field(default_factory=list)
    failures: dict[str, int] = field(default_factory=dict)

    def count(self, status: str) -> None:
        setattr(self, status, getattr(self, status) + 1)


def percentile(values: list[int], q: float) -> Optional[int]:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def normalize_failure(line: str) -> str:
    """Collapse task ids, UUIDs and numbers so equal failures group together."""
    text = _NUMBER_RE.sub("<n>", _ID_RE.sub("<id>", line))
    return text.strip()[:160]


def _status(row: sqlite3.Row) -> str:
    if row["ended_at"] is None:
        return "running"
    if row["return_code"] is None:
        return "unknown"
    return "ok" if row["return_code"] == 0 else "failed"


def _arm_key(row: sqlite3.Row) -> tuple[str, str, str]:
    return (
        row["chosen_cli"] or "?",
        row["model"] or "(cli default)",
        row["effort"] or "(cli default)",
    )


def _task_rows(conn: sqlite3.Connection, role: str, since: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, chosen_cli, model, effort, started_at, ended_at, return_code, "
        "duration_s, tokens_total FROM tasks WHERE role=? AND started_at>=? "
        "ORDER BY started_at DESC",
        (role, since),
    ).fetchall()


def _add_run(stats: ArmWeek, row: sqlite3.Row, failure_reason: TaskText) -> None:
    status = _status(row)
    stats.runs += 1
    stats.count(status)
    if row["duration_s"] is not None:
        stats.durations.append(int(row["duration_s"]))
    if row["tokens_total"] is not None:
        stats.tokens.append(int(row["tokens_total"]))
    if status == "failed":
        reason = normalize_failure(failure_reason(row["id"]))
        stats.failures[reason] = stats.failures.get(reason, 0) + 1


def collect_arms(rows: list[sqlite3.Row], now: int,
                 failure_reason: TaskText) -> dict[tuple[str, str, str], dict[int, ArmWeek]]:
    arms: dict[tuple[str, str, str], dict[int, ArmWeek]] = {}
    for row in rows:
        week = int((now - int(row["started_at"])) // WEEK_S)
        stats = arms.setdefault(_arm_key(row), {}).setdefault(week, ArmWeek())
        _add_run(stats, row, failure_reason)
    return arms


def _fmt(value: Optional[int]) -> str:
    return "n/a" if value is None else str(value)


def _top_failures(failures: dict[str, int]) -> str:
    ranked = sorted(failures.items(), key=lambda item: (-item[1], item[0]))[:3]
    return ", ".join(f"{reason} ×{count}" for reason, count in ranked)


def render_week(week: int, stats: ArmWeek) -> str:
    label = "this week" if week == 0 else f"{week} week(s) ago"
    line = (
        f"    {label}: runs={stats.runs} ok={stats.ok} failed={stats.failed} "
        f"unknown={stats.unknown} running={stats.running} "
        f"p50_s={_fmt(percentile(stats.durations, 0.5))} "
        f"p90_s={_fmt(percentile(stats.durations, 0.9))} "
        f"p50_tokens={_fmt(percentile(stats.tokens, 0.5))}"
    )
    failures = _top_failures(stats.failures)
    return line + (f"\n      failures: {failures}" if failures else "")


def _render_telemetry(rows: list[sqlite3.Row], now: int, weeks: int,
                      failure_reason: TaskText) -> list[str]:
    arms = collect_arms(rows, now, failure_reason)
    if not arms:
        return ["telemetry: (no runs in the last 30 days)"]
    lines = ["telemetry (last 30 days, weekly buckets):"]
    for key, by_week in sorted(arms.items()):
        lines.append("  " + " / ".join(key))
        lines.extend(render_week(week, by_week[week]) for week in sorted(by_week) if week < weeks)
    return lines


def _render_history(routes: list[Route]) -> list[str]:
    if not routes:
        return ["recent decisions: (none)"]
    return ["recent decisions:"] + [
        f"  {triple(r.cli, r.model, r.effort)} — {r.reason}" for r in routes
    ]


def _render_outcomes(rows: list[sqlite3.Row], samples: int, outcome: TaskText) -> list[str]:
    finished = [row for row in rows if _status(row) == "ok"][:samples]
    if not finished:
        return []
    return ["recent outcomes (newest first):"] + [
        f"  - {' / '.join(_arm_key(row))}: "
        f"{(outcome(row['id']) or '(no result line)')[:OUTCOME_CHARS]}"
        for row in finished
    ]


def _render_role_header(role: str, current: dict[str, str]) -> list[str]:
    meta = role_metadata(role) or {}
    return [
        f"## ROLE {role}",
        f"description: {meta.get('description', '')}",
        f"reads: {meta.get('reads', '')}",
        f"writes: {meta.get('writes', '')}",
        f"impact: {meta.get('impact', '')}",
        f"current: {current.get('cli') or '?'} / "
        f"{current.get('model') or '(cli default)'} / "
        f"{current.get('effort') or '(cli default)'} ({current.get('cli_source', '')})",
    ]


def render_role(conn: sqlite3.Connection, role: str, current: dict[str, str], now: int,
                samples: int, weeks: int, failure_reason: TaskText,
                outcome: TaskText) -> str:
    rows = _task_rows(conn, role, now - WINDOW_S)
    lines = (
        _render_role_header(role, current)
        + _render_history(recent_routes(conn, role, 3))
        + _render_telemetry(rows, now, weeks, failure_reason)
        + _render_outcomes(rows, samples, outcome)
    )
    return "\n".join(lines)


def _efforts_text(options: CliOptions, model: str) -> str:
    if options.effort_mode == "model":
        return "effort is encoded in the model; use an empty effort"
    return ", ".join(options.efforts_for(model))


def render_catalog(options: dict[str, CliOptions]) -> str:
    lines = ["## AVAILABLE OPTIONS (cli / model: allowed efforts)"]
    for cli in sorted(options):
        lines.extend(
            f"  {cli} / {model}: {_efforts_text(options[cli], model)}"
            for model in options[cli].models
        )
    return "\n".join(lines)


def _default_failure_reason(task_id: str) -> str:
    from .notify import _reason_from_task_log

    return _reason_from_task_log(task_id)


def _default_outcome(task_id: str) -> str:
    from .agent_status import extract_useful_result

    return extract_useful_result(task_id)


def build_digest(conn: sqlite3.Connection, roles: list[str],
                 options: dict[str, CliOptions], current: dict[str, dict[str, str]],
                 now: int, failure_reason: Optional[TaskText] = None,
                 outcome: Optional[TaskText] = None) -> str:
    """Render the router's input, trimmed to MAX_DIGEST_CHARS."""
    failure_reason = failure_reason or _default_failure_reason
    outcome = outcome or _default_outcome
    text = ""
    for samples, weeks in _TRIM_STEPS:
        sections = [
            render_role(conn, role, current.get(role, {}), now, samples, weeks,
                        failure_reason, outcome)
            for role in roles
        ]
        text = "\n\n".join(sections + [render_catalog(options)])
        if len(text) <= MAX_DIGEST_CHARS:
            return text
    return text[:MAX_DIGEST_CHARS]
