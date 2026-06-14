"""Structured status snapshots for thread-keeper autonomous loops.

The MCP tools already expose human-readable task, loop, and RSS summaries.
This module is the stable, scriptable shape used by external UI clients such
as the macOS menu-bar widget.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any

from .config import TASK_LOG_DIR
from .db import get_db
from .helpers import alive, fmt_age


_ROLE_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bCANDIDATE REVIEWER\b", re.I), "candidate_reviewer"),
    (re.compile(r"\bDIALECTIC VALIDATOR\b", re.I), "dialectic_validator"),
    (re.compile(r"\bEVOLVE REVIEWER\b", re.I), "evolve_reviewer"),
    (re.compile(r"\bEVOLVE APPLIER\b", re.I), "evolve_applier"),
    (re.compile(r"\bPROBE RUNNER\b", re.I), "probe_runner"),
    (re.compile(r"\bCURATOR\b", re.I), "curator"),
    (re.compile(r"\bSHADOW LEARNING OBSERVER\b", re.I), "shadow_observer"),
    (re.compile(r"\bSHADOW REVIEW\b", re.I), "shadow_observer"),
    (re.compile(r"\bEXTRACT\b", re.I), "extract"),
    (re.compile(r"\bARCHIVIST\b", re.I), "archivist"),
)

_ROLE_DESCRIPTIONS: dict[str, str] = {
    "archivist": (
        "Imports and normalizes historical conversation data so older work "
        "can be searched and reused."
    ),
    "candidate_reviewer": (
        "Reviews extracted conversation candidates and decides what should "
        "become memory, a skill update, or a rejected false positive."
    ),
    "curator": (
        "Audits existing lessons and skills, looking for stale, duplicate, "
        "or patch-worthy knowledge."
    ),
    "dialectic_validator": (
        "Turns buffered user-model observations into evidence-backed claims "
        "about preferences, constraints, and working style."
    ),
    "evolve_applier": (
        "Implements one open roadmap issue at a time, then falls back to "
        "Curator memory-maintenance reports and promoted evolve suggestions."
    ),
    "evolve_reviewer": (
        "Audits thread-keeper for safety, leaks, optimization, and new ideas; "
        "updates the roadmap and opens GitHub issues."
    ),
    "extract": (
        "Converts decision-shaped conversation moments into pending review "
        "candidates for the learning pipeline."
    ),
    "probe_runner": (
        "Runs isolated reliability probes and records whether the system still "
        "handles known failure cases."
    ),
    "shadow_observer": (
        "Scans recent dialog for durable lessons, workflow corrections, and "
        "skill materialization opportunities."
    ),
    "auto_update": (
        "Checks the installed thread-keeper package or git checkout and applies "
        "safe daily updates when a newer version is available."
    ),
}

_LOOP_DEFS: tuple[dict[str, Any], ...] = (
    {
        "id": "ingest",
        "name": "Ingest",
        "event": "ingest_pass",
        "interval": "INGEST_INTERVAL_S",
        "description": (
            "Watches CLI transcript files and imports new conversation messages "
            "into thread-keeper's dialog store."
        ),
        "work": "Reads CLI transcripts into dialog memory",
    },
    {
        "id": "shadow_review",
        "name": "Shadow review",
        "event": "shadow_review_pass",
        "interval": "SHADOW_REVIEW_INTERVAL_S",
        "role": "shadow_observer",
        "description": _ROLE_DESCRIPTIONS["shadow_observer"],
        "work": "Scans recent dialog for durable skills and lessons",
    },
    {
        "id": "extract",
        "name": "Extract",
        "event": "extract_pass",
        "interval": "EXTRACT_INTERVAL_S",
        "description": _ROLE_DESCRIPTIONS["extract"],
        "work": "Harvests decision-shaped dialog into review candidates",
        "backlog_sql": "SELECT COUNT(*) FROM extract_candidates WHERE status='pending'",
        "backlog_label": "pending candidates",
    },
    {
        "id": "candidate_reviewer",
        "name": "Candidate reviewer",
        "event": "candidate_review_pass",
        "interval": "CANDIDATE_REVIEW_INTERVAL_S",
        "threshold": "CANDIDATE_REVIEW_MIN",
        "role": "candidate_reviewer",
        "description": _ROLE_DESCRIPTIONS["candidate_reviewer"],
        "work": "Reviews pending candidates into skills, notes, or rejects",
        "backlog_sql": "SELECT COUNT(*) FROM extract_candidates WHERE status='pending'",
        "backlog_label": "pending candidates",
    },
    {
        "id": "curator",
        "name": "Curator",
        "event": "curator_pass",
        "interval": "CURATOR_INTERVAL_S",
        "threshold": "CURATOR_MIN_LESSONS",
        "role": "curator",
        "description": _ROLE_DESCRIPTIONS["curator"],
        "work": "Audits lessons and skills for patch, prune, consolidate",
        "backlog_sql": "SELECT COUNT(*) FROM skill_usage",
        "backlog_label": "tracked skills",
    },
    {
        "id": "dialectic_miner",
        "name": "Dialectic miner",
        "event": "dialectic_mine_pass",
        "interval": "DIALECTIC_MINE_INTERVAL_S",
        "description": (
            "Mechanically captures user replies and nearby context into a "
            "buffer before any LLM interpretation happens."
        ),
        "work": "Captures user replies into dialectic observation buffer",
        "backlog_sql": (
            "SELECT COUNT(*) FROM dialectic_observations "
            "WHERE status='pending' AND claimed_at IS NULL"
        ),
        "backlog_label": "pending observations",
    },
    {
        "id": "dialectic_validator",
        "name": "Dialectic validator",
        "event": "dialectic_validate_pass",
        "interval": "DIALECTIC_VALIDATE_INTERVAL_S",
        "threshold": "DIALECTIC_VALIDATE_MIN",
        "role": "dialectic_validator",
        "description": _ROLE_DESCRIPTIONS["dialectic_validator"],
        "work": "Turns pending observations into user-model claims",
        "backlog_sql": (
            "SELECT COUNT(*) FROM dialectic_observations "
            "WHERE status='pending' AND claimed_at IS NULL"
        ),
        "backlog_label": "pending observations",
    },
    {
        "id": "evolve_review",
        "name": "Evolve reviewer",
        "event": "evolve_review_pass",
        "interval": "EVOLVE_REVIEW_INTERVAL_S",
        "threshold": "EVOLVE_REVIEW_MIN",
        "role": "evolve_reviewer",
        "description": _ROLE_DESCRIPTIONS["evolve_reviewer"],
        "work": "Audits thread-keeper and creates roadmap issues",
        "backlog_sql": (
            "SELECT COUNT(*) FROM evolve WHERE applied=0 "
            "AND COALESCE(status,'pending')='pending'"
        ),
        "backlog_label": "legacy pending suggestions",
        "ready_on_due": True,
    },
    {
        "id": "evolve_apply",
        "name": "Evolve applier",
        "event": "evolve_apply_pass",
        "result_events": (
            "evolve_apply_pass",
            "curator_report_applied",
            "evolve_applied",
        ),
        "interval": "EVOLVE_APPLY_INTERVAL_S",
        "role": "evolve_applier",
        "description": _ROLE_DESCRIPTIONS["evolve_applier"],
        "work": "Implements one open roadmap issue",
        "backlog_metric": "evolve_apply",
        "backlog_label": "apply work items",
        "ready_backlog_min": 1,
    },
    {
        "id": "probe",
        "name": "Probe",
        "event": "probe_pass",
        "interval": "PROBE_INTERVAL_S",
        "role": "probe_runner",
        "description": _ROLE_DESCRIPTIONS["probe_runner"],
        "work": "Runs isolated reliability probes and grades results",
        "backlog_metric": "probe_due",
        "backlog_label": "due probes",
        "ready_backlog_min": 1,
    },
    {
        "id": "thread_janitor",
        "name": "Thread janitor",
        "event": "janitor_pass",
        "interval": "THREAD_JANITOR_INTERVAL_S",
        "description": (
            "Closes stale active threads so completed work can be reviewed, "
            "learned from, and removed from the active queue."
        ),
        "work": "Closes stale threads so review/learning can fire",
        "backlog_sql": "SELECT COUNT(*) FROM threads WHERE state IN ('active','idle')",
        "backlog_label": "open or idle threads",
    },
    {
        "id": "auto_update",
        "name": "Auto update",
        "event": "auto_update_pass",
        "interval": "AUTO_UPDATE_INTERVAL_S",
        "description": _ROLE_DESCRIPTIONS["auto_update"],
        "work": "Checks for and applies safe thread-keeper updates",
    },
)

_RESULT_WINDOW_S = 3600
_RESULT_SUMMARY_LIMIT = 240
_ISSUE_BACKLOG_CACHE: dict[str, int] = {"at": 0, "count": 0}


def _detect_role(prompt: str) -> str:
    haystack = (prompt or "")[:1200]
    for pattern, role in _ROLE_HINTS:
        if pattern.search(haystack):
            return role
    return ""


def _compact_work(prompt: str, limit: int = 140) -> str:
    text = " ".join(line.strip() for line in prompt.splitlines() if line.strip())
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _description_for_role(role: str) -> str:
    if role in _ROLE_DESCRIPTIONS:
        return _ROLE_DESCRIPTIONS[role]
    return "Spawned child task; inspect the current work line for its prompt."


def _status_for(pid: int | None, ended_at: int | None) -> str:
    if ended_at:
        return "done"
    if not pid or pid <= 0:
        return "visible"
    return "running" if alive(pid) else "dead?"


def _refresh_rss(conn) -> None:
    """Refresh task liveness/RSS using the existing spawn-budget sweeper.

    This intentionally reuses the production measurement path, so the widget
    and spawn-budget tool agree on memory numbers.
    """
    try:
        from .spawn_budget import _refresh_all_running

        _refresh_all_running(conn)
    except Exception:
        # A status widget should degrade to the last cached RSS instead of
        # failing when ps is briefly unavailable or the DB is locked.
        return


def _running_agents(conn, now: int, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, pid, parent_cid, spawned_cid, cwd, prompt, started_at, "
        "ended_at, return_code, rss_kb, rss_updated_at "
        "FROM tasks WHERE ended_at IS NULL "
        "ORDER BY started_at DESC LIMIT ?",
        (int(limit),),
    ).fetchall()

    agents: list[dict[str, Any]] = []
    for row in rows:
        prompt = row["prompt"] or ""
        role = _detect_role(prompt)
        cid = row["spawned_cid"] or ""
        name = role or (cid[:8] if cid else row["id"])
        rss_kb = row["rss_kb"] or 0
        elapsed_s = max(0, now - int(row["started_at"] or now))
        agents.append({
            "task_id": row["id"],
            "name": name,
            "role": role,
            "description": _description_for_role(role),
            "status": _status_for(row["pid"], row["ended_at"]),
            "work": _compact_work(prompt),
            "pid": row["pid"],
            "parent_cid": row["parent_cid"],
            "spawned_cid": row["spawned_cid"],
            "cwd": row["cwd"],
            "started_at": row["started_at"],
            "elapsed_s": elapsed_s,
            "elapsed": fmt_age(elapsed_s),
            "rss_kb": rss_kb,
            "rss_mb": rss_kb // 1024,
            "rss_updated_at": row["rss_updated_at"],
            "return_code": row["return_code"],
        })

    return agents


def _scalar(conn, sql: str) -> int:
    try:
        row = conn.execute(sql).fetchone()
    except Exception:
        return 0
    if not row:
        return 0
    value = row[0]
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _probe_due_count(conn, now: int) -> int:
    from . import config

    cutoff = int(now) - int(getattr(config, "PROBE_COOLDOWN_S", 0) or 0)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM probes p "
            "WHERE p.enabled = 1 "
            "  AND p.grader IN ('regex','exact') "
            "  AND p.expected_pattern IS NOT NULL AND p.expected_pattern != '' "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM probe_results r "
            "    WHERE r.category = p.category AND r.created_at >= ?"
            "  )",
            (cutoff,),
        ).fetchone()
    except Exception:
        return 0
    return int((row[0] if row else 0) or 0)


def _curator_report_apply_count(conn) -> int:
    try:
        from .evolve_applier import _pending_curator_reports

        return len(_pending_curator_reports(conn))
    except Exception:
        return 0


def _roadmap_issue_apply_count(conn, now: int) -> int:
    if now - _ISSUE_BACKLOG_CACHE["at"] < 300:
        return _ISSUE_BACKLOG_CACHE["count"]
    try:
        from .evolve_applier import _open_roadmap_issues

        issues, err = _open_roadmap_issues(conn)
    except Exception:
        return _ISSUE_BACKLOG_CACHE["count"]
    if err:
        return _ISSUE_BACKLOG_CACHE["count"]
    _ISSUE_BACKLOG_CACHE["at"] = int(now)
    _ISSUE_BACKLOG_CACHE["count"] = len(issues)
    return len(issues)


def _backlog_count(conn, loop: dict[str, Any], now: int) -> int:
    if loop.get("backlog_metric") == "probe_due":
        return _probe_due_count(conn, now)
    if loop.get("backlog_metric") == "evolve_apply":
        return _scalar(
            conn,
            "SELECT COUNT(*) FROM evolve WHERE applied=0 "
            "AND COALESCE(status,'pending')='promoted'",
        ) + _curator_report_apply_count(conn) + _roadmap_issue_apply_count(
            conn, now
        )
    if loop.get("backlog_sql"):
        return _scalar(conn, loop["backlog_sql"])
    return 0


def _last_event(conn, kind: str | tuple[str, ...], now: int) -> dict[str, Any]:
    kinds = (kind,) if isinstance(kind, str) else tuple(k for k in kind if k)
    if not kinds:
        kinds = ("",)
    placeholders = ",".join("?" for _ in kinds)
    try:
        row = conn.execute(
            f"SELECT summary, created_at FROM events WHERE kind IN ({placeholders}) "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            kinds,
        ).fetchone()
    except Exception:
        return {
            "summary": "",
            "at": None,
            "age_s": None,
            "age": "never",
        }
    if not row:
        return {
            "summary": "",
            "at": None,
            "age_s": None,
            "age": "never",
        }
    age_s = max(0, now - int(row["created_at"] or now))
    return {
        "summary": row["summary"] or "",
        "at": row["created_at"],
        "age_s": age_s,
        "age": fmt_age(age_s),
    }


def _human_summary(summary: str, fallback: str) -> str:
    s = (summary or "").strip()
    if not s:
        return fallback
    if s.startswith("spawn_error"):
        if "budget_exceeded" in s:
            return "Spawn blocked: memory budget"
        if "Argument list too long" in s:
            return "Spawn failed: prompt too large"
        return "Spawn failed"
    if ":: ERR" in s:
        if "budget_exceeded" in s:
            return "Spawn blocked: memory budget"
        if "Argument list too long" in s:
            return "Spawn failed: prompt too large"
        return "Spawn failed"
    if "spawn_failed" in s:
        if "Argument list too long" in s:
            return "Spawn failed: prompt too large"
        return "Spawn failed"
    if " ok task=" in s or s.startswith("ok task=") or s.startswith("spawned"):
        m = re.search(r"pending_batch=(\d+)\s+total=(\d+)", s)
        if m:
            return f"Spawned reviewer for {m.group(1)} of {m.group(2)} pending items"
        if "pending=" in s:
            m = re.search(r"pending=(\d+)", s)
            if m:
                return f"Spawned reviewer for {m.group(1)} pending items"
        if "lessons=" in s or "skills=" in s or "concepts=" in s:
            bits = " ".join(re.findall(r"(?:lessons|skills|concepts)=\d+", s))
            return f"Spawned curator ({bits})" if bits else "Spawned curator"
        return "Spawned child worker"
    if s == "no_pending":
        return "No pending work"
    if s == "no_promoted":
        return "No promoted suggestions"
    if s == "no_apply_work":
        return "No apply work"
    if s == "no_due":
        return "No due work"
    if s == "no_stale":
        return "No stale threads"
    if s == "no_user_dialog":
        return "No new user dialog"
    if s.startswith("graded=") or s.startswith("ok "):
        return s
    if s.startswith("curator_report="):
        return "Applying curator report"
    if s.startswith("below_threshold"):
        return s.replace("_", " ")
    return s[:140]


def _loop_status(
    conn,
    loop: dict[str, Any],
    agents_by_role: dict[str, list[dict[str, Any]]],
    now: int,
) -> dict[str, Any]:
    from . import config

    interval_s = float(getattr(config, loop["interval"], 0) or 0)
    threshold = int(getattr(config, loop.get("threshold", ""), 0) or 0)
    ready_backlog_min = int(loop.get("ready_backlog_min", 0) or 0)
    backlog = _backlog_count(conn, loop, now)
    last = _last_event(conn, loop.get("result_events", loop["event"]), now)
    running = agents_by_role.get(loop.get("role", ""), [])
    rss_mb = sum(a["rss_mb"] for a in running)
    due = False
    if interval_s > 0:
        last_at = last["at"]
        if last_at is None:
            due = loop.get("event") != "ingest_pass"
        else:
            due = now >= int(last_at) + int(interval_s)

    if running:
        status = "running"
    elif interval_s <= 0:
        status = "off"
    elif loop.get("ready_on_due") and due:
        status = "ready"
    elif ready_backlog_min and backlog >= ready_backlog_min:
        status = "ready"
    elif threshold and backlog >= threshold and due:
        status = "ready"
    else:
        status = "idle"

    work = loop["work"]
    last_summary = last["summary"]
    if running:
        work = running[0]["work"]
    elif status == "ready":
        work = loop["work"]
    elif last_summary:
        work = _human_summary(last_summary, loop["work"])

    return {
        "id": loop["id"],
        "name": loop["name"],
        "description": loop.get("description", loop["work"]),
        "status": status,
        "enabled": interval_s > 0,
        "due": due,
        "interval_s": interval_s,
        "threshold": threshold,
        "ready_backlog_min": ready_backlog_min,
        "work": work,
        "last_summary": last_summary,
        "last_at": last["at"],
        "last_age_s": last["age_s"],
        "last_age": last["age"],
        "backlog_count": backlog,
        "backlog_label": loop.get("backlog_label", ""),
        "running_agents": running,
        "running_agent_count": len(running),
        "rss_mb": rss_mb,
        "rss_kb": rss_mb * 1024,
    }


def _loop_statuses(conn, agents: list[dict[str, Any]], now: int) -> list[dict[str, Any]]:
    agents_by_role: dict[str, list[dict[str, Any]]] = {}
    for agent in agents:
        role = agent.get("role") or ""
        if role:
            agents_by_role.setdefault(role, []).append(agent)
    loops: list[dict[str, Any]] = []
    for index, loop in enumerate(_LOOP_DEFS):
        row = _loop_status(conn, loop, agents_by_role, now)
        row["_order"] = index
        loops.append(row)

    status_order = {"running": 0, "ready": 1, "idle": 2, "off": 3}
    loops.sort(key=lambda row: (status_order.get(row["status"], 9), row["_order"]))
    for row in loops:
        row.pop("_order", None)
    return loops


def _role_to_loop() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for loop in _LOOP_DEFS:
        role = loop.get("role")
        if role:
            out[role] = {"id": loop["id"], "name": loop["name"]}
    return out


def _read_log_sample(task_id: str, max_head: int = 16_384, max_tail: int = 65_536) -> str:
    path = TASK_LOG_DIR / f"{task_id}.log"
    try:
        with path.open("rb") as f:
            head = f.read(max_head)
            try:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - max_tail))
                tail = f.read(max_tail)
            except OSError:
                tail = b""
    except OSError:
        return ""
    if tail and tail != head:
        data = head + b"\n" + tail
    else:
        data = head
    return data.decode("utf-8", errors="replace")


def _clean_result_line(line: str) -> str:
    line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
    line = re.sub(r"\s+", " ", line).strip()
    return line


def _is_result_line(line: str) -> bool:
    clean = _clean_result_line(line)
    if not clean:
        return False
    lower = clean.lower()
    if lower.startswith(("skip:", "output", "do,", "hard constraints")):
        return False
    if "write the report.md" in lower or "close with the literal" in lower:
        return False
    if "not inside a trusted directory" in lower:
        return False
    if re.search(r"\bprocessed\s+\d+\s+candidates\b", lower):
        return True
    if "curator pass complete" in lower:
        return True
    if "report written" in lower or "report is at" in lower:
        return True
    if "evolve_apply_complete" in lower:
        return True
    if "materialized" in lower and ("skill" in lower or "lesson" in lower):
        return True
    if re.search(r"\b(created|patched|updated)\s+(a\s+)?skill\b", lower):
        return True
    return False


def _extract_useful_result(task_id: str) -> str:
    sample = _read_log_sample(task_id)
    if not sample:
        return ""
    lines = [_clean_result_line(line) for line in sample.splitlines()]
    lines = [line for line in lines if line]

    # Prefer final summaries near the end, but still catch one-line outputs at
    # the top of short logs such as "Report written to ...".
    for line in reversed(lines):
        if _is_result_line(line):
            return line[:_RESULT_SUMMARY_LIMIT]
    for line in lines[:40]:
        if _is_result_line(line):
            return line[:_RESULT_SUMMARY_LIMIT]
    return ""


def _recent_results(conn, now: int, limit: int = 10) -> list[dict[str, Any]]:
    role_loop = _role_to_loop()
    rows = conn.execute(
        "SELECT id, prompt, ended_at, return_code FROM tasks "
        "WHERE ended_at IS NOT NULL AND return_code=0 AND ended_at>=? "
        "ORDER BY ended_at DESC LIMIT ?",
        (now - _RESULT_WINDOW_S, int(limit) * 3),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        prompt = row["prompt"] or ""
        role = _detect_role(prompt)
        loop = role_loop.get(role)
        if not loop:
            continue
        summary = _extract_useful_result(row["id"])
        if not summary:
            continue
        ended_at = int(row["ended_at"] or now)
        age_s = max(0, now - ended_at)
        results.append({
            "id": f"{row['id']}:{ended_at}",
            "task_id": row["id"],
            "role": role,
            "loop_id": loop["id"],
            "loop_name": loop["name"],
            "title": f"{loop['name']} completed",
            "summary": summary,
            "ended_at": ended_at,
            "age_s": age_s,
            "age": fmt_age(age_s),
        })
        if len(results) >= limit:
            break
    return results


def memory_cleanup(dry_run: bool = False, force: bool = False) -> dict[str, Any]:
    """Run the safe ThreadKeeper memory cleanup path.

    This trims peer server caches, applies the configured memory guard, and
    removes orphaned MCP server processes. It does not kill active spawned child
    agents; use task_kill() when ending active agent work is intentional.
    """
    conn = get_db()
    _refresh_rss(conn)

    from . import memory_guard, process_health

    before = agent_status_snapshot(refresh=False)
    if dry_run:
        trim_request = {
            "requested": [],
            "count": 0,
            "reason": "agent_status_cleanup",
        }
    else:
        trim_request = memory_guard.request_reclaim(reason="agent_status_cleanup")
    guard = memory_guard.check_once(dry_run=dry_run, notify=False)
    orphan_cleanup = process_health.cleanup(dry_run=dry_run, force=force)
    after = agent_status_snapshot(refresh=True)

    return {
        "dry_run": dry_run,
        "force": force,
        "before": {
            "running_count": before["running_count"],
            "child_rss_mb": before["total_rss_mb"],
        },
        "after": {
            "running_count": after["running_count"],
            "child_rss_mb": after["total_rss_mb"],
        },
        "peer_trim_requested": trim_request,
        "guard": {
            "warn": len(guard.get("warn", [])),
            "kill": len(guard.get("kill", [])),
            "killed": guard.get("killed", []),
            "retired": guard.get("retired", []),
            "failed": guard.get("failed", []),
            "aggregate": guard.get("aggregate", {}),
            "reclaim_requests": guard.get("reclaim_requests", {}),
            "local_reclaim": guard.get("local_reclaim"),
            "handled_controls": guard.get("handled_controls", []),
        },
        "orphans": {
            "count": len(orphan_cleanup.get("orphans", [])),
            "killed": orphan_cleanup.get("killed", []),
            "failed": orphan_cleanup.get("failed", []),
        },
    }


def format_memory_cleanup(result: dict[str, Any]) -> str:
    action = "dry_run" if result.get("dry_run") else "applied"
    before = result["before"]
    after = result["after"]
    trim = result["peer_trim_requested"]
    guard = result["guard"]
    orphans = result["orphans"]
    lines = [
        (
            f"{action}: child_agents "
            f"{before['running_count']}->{after['running_count']} "
            f"child_rss {before['child_rss_mb']}MB->{after['child_rss_mb']}MB"
        ),
        (
            f"peer_trim_requested={trim.get('count', 0)} "
            f"pids={','.join(str(p) for p in trim.get('requested', [])) or '-'}"
        ),
        (
            f"guard warn={guard['warn']} kill={guard['kill']} "
            f"killed={len(guard['killed'])} retired={len(guard['retired'])} "
            f"failed={len(guard['failed'])}"
        ),
        (
            f"orphans={orphans['count']} killed={len(orphans['killed'])} "
            f"failed={len(orphans['failed'])}"
        ),
    ]
    local = guard.get("local_reclaim")
    if local:
        lines.append(
            f"local_reclaim before={local['before_mb']}MB "
            f"after={local['after_mb']}MB freed={local['freed_mb']}MB"
        )
    return "\n".join(lines)


def agent_status_snapshot(refresh: bool = True, limit: int = 50) -> dict[str, Any]:
    """Return a JSON-ready snapshot of autonomous loops and running children."""
    conn = get_db()
    if refresh:
        _refresh_rss(conn)
    now = int(time.time())
    agents = _running_agents(conn, now, limit)
    loops = _loop_statuses(conn, agents, now)

    return {
        "generated_at": now,
        "running_count": len(agents),
        "total_rss_kb": sum(a["rss_kb"] for a in agents),
        "total_rss_mb": sum(a["rss_mb"] for a in agents),
        "enabled_loop_count": sum(1 for loop in loops if loop["enabled"]),
        "running_loop_count": sum(1 for loop in loops if loop["status"] == "running"),
        "ready_loop_count": sum(1 for loop in loops if loop["status"] == "ready"),
        "loops": loops,
        "recent_results": _recent_results(conn, now),
        "agents": agents,
    }


def format_agent_status(snapshot: dict[str, Any]) -> str:
    lines = [
        f"loops enabled={snapshot.get('enabled_loop_count', 0)} "
        f"running={snapshot.get('running_loop_count', 0)} "
        f"ready={snapshot.get('ready_loop_count', 0)} "
        f"child_rss={snapshot['total_rss_mb']}MB"
    ]
    for loop in snapshot.get("loops", []):
        backlog = ""
        if loop.get("backlog_label"):
            backlog = f" backlog={loop['backlog_count']} {loop['backlog_label']}"
        lines.append(
            f"  {loop['name']} status={loop['status']} "
            f"last={loop['last_age']} rss={loop['rss_mb']}MB{backlog} "
            f"desc={json.dumps(loop.get('description', ''), ensure_ascii=False)} "
            f"work={json.dumps(loop['work'], ensure_ascii=False)}"
        )
    if snapshot["agents"]:
        lines.append(
            f"agents={snapshot['running_count']} rss_total={snapshot['total_rss_mb']}MB"
        )
    for agent in snapshot["agents"]:
        lines.append(
            f"  {agent['name']} task={agent['task_id']} "
            f"pid={agent['pid']} status={agent['status']} "
            f"rss={agent['rss_mb']}MB elapsed={agent['elapsed']} "
            f"desc={json.dumps(agent.get('description', ''), ensure_ascii=False)} "
            f"work={json.dumps(agent['work'], ensure_ascii=False)}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show thread-keeper autonomous learning loop status."
    )
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="use cached RSS values instead of measuring live processes",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="maximum number of running child agents to return",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="refresh continuously until interrupted",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="seconds between --watch refreshes",
    )
    parser.add_argument(
        "--cleanup-memory",
        action="store_true",
        help="trim ThreadKeeper caches and retire orphan/over-limit server processes",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="with --cleanup-memory, show the cleanup plan without applying it",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --cleanup-memory, use SIGKILL for orphan cleanup",
    )
    args = parser.parse_args(argv)

    if args.cleanup_memory:
        result = memory_cleanup(dry_run=args.dry_run, force=args.force)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            print(format_memory_cleanup(result))
        return 0

    while True:
        snap = agent_status_snapshot(
            refresh=not args.no_refresh,
            limit=max(1, args.limit),
        )
        if args.json:
            print(json.dumps(snap, ensure_ascii=False, sort_keys=True))
        else:
            print(format_agent_status(snap))
        if not args.watch:
            return 0
        sys.stdout.flush()
        time.sleep(max(0.5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
