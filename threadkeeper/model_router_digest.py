"""Deterministic telemetry digest the model router reasons over.

Everything here is computed by the parent from the `tasks` table, task logs,
past decisions and the model catalog, so the router child sees exactly the
numbers a test can assert.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from itertools import islice, zip_longest
from typing import Callable, Iterator, Optional

from .agent_metadata import role_metadata
from .github_safety import sanitize_presentation_text
from .model_routes import CliOptions, Route, recent_routes, triple

WINDOW_S = 30 * 86400
WEEK_S = 7 * 86400
OUTCOME_SAMPLES = 8
OUTCOME_CHARS = 2000
TASK_CHARS = 400
HISTORY_REASON_CHARS = 600
MAX_DIGEST_CHARS = 60_000
# Keep task-result evidence when reducing a large digest.
_TRIM_STEPS = ((OUTCOME_SAMPLES, OUTCOME_CHARS, 5), (5, 1000, 5),
               (3, 600, 3), (3, 400, 3), (2, 400, 3), (1, 400, 1))
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
    tokens_in: list[int] = field(default_factory=list)
    tokens_out: list[int] = field(default_factory=list)
    costs: list[float] = field(default_factory=list)
    failures: dict[str, int] = field(default_factory=dict)

    def count(self, status: str) -> None:
        setattr(self, status, getattr(self, status) + 1)


def percentile(values: list[int] | list[float], q: float) -> Optional[int | float]:
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
        "duration_s, tokens_total, tokens_in, tokens_out, cost_usd, prompt, spawned_cid "
        "FROM tasks WHERE role=? AND started_at>=? "
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
    for name in ("tokens_in", "tokens_out"):
        if row[name] is not None:
            getattr(stats, name).append(int(row[name]))
    if row["cost_usd"] is not None:
        stats.costs.append(float(row["cost_usd"]))
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


def _fmt(value: Optional[int | float]) -> str:
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
        f"p50_tokens={_fmt(percentile(stats.tokens, 0.5))} "
        f"input_known_runs={len(stats.tokens_in)}/{stats.runs} "
        f"p50_input_tokens={_fmt(percentile(stats.tokens_in, 0.5))} "
        f"output_known_runs={len(stats.tokens_out)}/{stats.runs} "
        f"p50_output_tokens={_fmt(percentile(stats.tokens_out, 0.5))} "
        f"cost_known_runs={len(stats.costs)}/{stats.runs} "
        f"p50_cost_usd={_fmt(percentile(stats.costs, 0.5))} "
        f"recorded_cost_usd={_fmt(sum(stats.costs) if stats.costs else None)}"
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
        f"  {triple(r.cli, r.model, r.effort)} — {_evidence_text(r.reason, HISTORY_REASON_CHARS)}"
        for r in routes
    ]


def _sample_tasks(rows: list[sqlite3.Row], samples: int) -> list[sqlite3.Row]:
    arms: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        if row["ended_at"] is not None:
            arms.setdefault(_arm_key(row), []).append(row)
    balanced = (row for batch in zip_longest(*arms.values()) for row in batch
                if row is not None)
    return list(islice(balanced, samples))


def _evidence_text(text: str, limit: int) -> str:
    clean = sanitize_presentation_text(text)
    clean = re.sub(r"\s+", " ", clean).strip()
    if len(clean) <= limit:
        return clean
    marker = " [...] "
    head = (limit - len(marker)) // 2
    return clean[:head] + marker + clean[-(limit - len(marker) - head):]


def _stored_result(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[str, str]:
    if not row["spawned_cid"]:
        return "", ""
    result = conn.execute(
        "SELECT content, model FROM dialog_messages WHERE session_id=? "
        "AND role='assistant' AND deleted=0 AND created_at>=? AND created_at<=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (row["spawned_cid"], row["started_at"], row["ended_at"]),
    ).fetchone()
    return (str(result["content"] or ""), str(result["model"] or "")) if result else ("", "")


def _render_task(conn: sqlite3.Connection, row: sqlite3.Row, outcome: TaskText,
                 chars: int) -> str:
    stored, reported_model = _stored_result(conn, row)
    result = stored or outcome(row["id"])
    source = "transcript" if stored else "log" if result else "missing"
    return (
        f"  - task={row['id']} {' / '.join(_arm_key(row))} "
        f"execution={_status(row)} duration_s={_fmt(row['duration_s'])} "
        f"tokens={_fmt(row['tokens_total'])} cost_usd={_fmt(row['cost_usd'])}\n"
        f"    input_tokens={_fmt(row['tokens_in'])} output_tokens={_fmt(row['tokens_out'])}\n"
        f"    reported_model={_evidence_text(reported_model, 120) or '(unknown)'}\n"
        f"    task: {_evidence_text(row['prompt'] or '', TASK_CHARS)}\n"
        f"    result_source={source}: "
        f"{_evidence_text(result, chars) if result else '(result unavailable; quality unknown)'}"
    )


def _render_outcomes(conn: sqlite3.Connection, rows: list[sqlite3.Row], samples: int,
                     outcome: TaskText, chars: int) -> list[str]:
    selected = _sample_tasks(rows, samples)
    if not selected:
        return []
    return ["previous task results (sampled across model/effort combinations):"] + [
        _render_task(conn, row, outcome, chars) for row in selected
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
                outcome: TaskText, outcome_chars: int = OUTCOME_CHARS) -> str:
    rows = _task_rows(conn, role, now - WINDOW_S)
    lines = (
        _render_role_header(role, current)
        + _render_history(recent_routes(conn, role, 3))
        + _render_telemetry(rows, now, weeks, failure_reason)
        + _render_outcomes(conn, rows, samples, outcome, outcome_chars)
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


def render_verified_access(conn: sqlite3.Connection, now: int) -> str:
    rows = conn.execute(
        "SELECT t.chosen_cli, t.model, t.effort, MAX(t.ended_at) AS last_success "
        "FROM tasks t JOIN model_router_passes p ON p.task_id=t.id "
        "WHERE p.status='complete' AND t.return_code=0 AND t.ended_at>=? "
        "AND t.chosen_cli IS NOT NULL AND t.model IS NOT NULL "
        "GROUP BY t.chosen_cli, t.model, t.effort ORDER BY last_success DESC",
        (now - WINDOW_S,),
    ).fetchall()
    lines = ["## VERIFIED ROUTER EXECUTION ACCESS (last 30 days)",
             "These executions saved complete routing decisions and exited successfully.",
             "They establish CLI/model/effort access at that time, not quality on other roles.",
             "A startup failure preceding a later successful run is not an unresolved access blocker."]
    lines.extend(f"  {row['chosen_cli']} / {row['model']} / {row['effort'] or '(default)'}: "
                 f"last_success_unix={row['last_success']}" for row in rows)
    return "\n".join(lines + ([] if rows else ["  (no verified access evidence)"]))


def render_execution_failures(conn: sqlite3.Connection, now: int,
                              failure_reason: TaskText) -> str:
    rows = conn.execute(
        "SELECT id, chosen_cli, model, effort, ended_at FROM tasks "
        "WHERE role='model_router' AND return_code!=0 AND ended_at>=? "
        "ORDER BY ended_at DESC LIMIT 3", (now - WEEK_S,),
    ).fetchall()
    if not rows:
        return ""
    lines = ["## RECENT ROUTER EXECUTION FAILURES",
             "Separate temporary quota/transport state from model quality; consider later successes."]
    lines.extend(f"  {row['chosen_cli']} / {row['model']} / {row['effort'] or '(default)'}: "
                 f"ended_at_unix={row['ended_at']}; "
                 f"{_evidence_text(failure_reason(row['id']), 400) or '(cause unknown)'}"
                 for row in rows)
    return "\n".join(lines)


def _default_failure_reason(task_id: str) -> str:
    from .notify import _reason_from_task_log

    for event in _reverse_log_events(task_id):
        if event.get("type") == "result" and event.get("is_error"):
            result = event.get("result")
            if isinstance(result, str) and result:
                code = event.get("api_error")
                return f"{code}: {result}" if code else result
    return _reason_from_task_log(task_id)


def _reverse_log_events(task_id: str) -> Iterator[dict]:
    from .agent_status import _read_log_sample

    for line in reversed(_read_log_sample(task_id).splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def _default_outcome(task_id: str) -> str:
    from .agent_status import extract_useful_result

    for event in _reverse_log_events(task_id):
        result = _event_result(event)
        if result:
            return result
    return extract_useful_result(task_id)


def _event_result(event: object) -> str:
    if not isinstance(event, dict):
        return ""
    if event.get("type") == "result" and not event.get("is_error"):
        result = event.get("result")
        return result if isinstance(result, str) else ""
    item = event.get("item")
    if event.get("type") != "item.completed" or not isinstance(item, dict):
        return ""
    text = item.get("text")
    return text if item.get("type") == "agent_message" and isinstance(text, str) else ""


def build_digest(conn: sqlite3.Connection, roles: list[str],
                 options: dict[str, CliOptions], current: dict[str, dict[str, str]],
                 now: int, failure_reason: Optional[TaskText] = None,
                 outcome: Optional[TaskText] = None,
                 max_chars: Optional[int] = None) -> str:
    """Render the router's input, trimmed to MAX_DIGEST_CHARS."""
    failure_reason = failure_reason or _default_failure_reason
    outcome = outcome or _default_outcome
    budget = MAX_DIGEST_CHARS if max_chars is None else min(max_chars, MAX_DIGEST_CHARS)
    access = render_verified_access(conn, now)
    failures = render_execution_failures(conn, now, failure_reason)
    text = ""
    for samples, outcome_chars, weeks in _TRIM_STEPS:
        sections = [
            render_role(conn, role, current.get(role, {}), now, samples, weeks,
                        failure_reason, outcome, outcome_chars)
            for role in roles
        ]
        text = "\n\n".join([access, failures] + sections + [render_catalog(options)])
        if len(text) <= budget:
            return text
    return text[:budget]
