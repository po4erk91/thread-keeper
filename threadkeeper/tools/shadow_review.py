"""MCP tools for the shadow-review machinery.

  shadow_review_run(force=False, dry_run=False)
    Trigger one shadow pass NOW. `force=True` overrides the
    SHADOW_REVIEW_INTERVAL_S=0 disable. `dry_run=True` returns the prompt
    that WOULD be spawned (no actual spawn) — useful for inspecting
    candidate windows or building tests.

  shadow_review_status(snapshot_path="")
    Diagnostic snapshot: env config, cursor position, last 5 passes, and
    aggregated production telemetry (24h / 7d tick counts, outcome mix,
    MATERIALIZED-vs-SKIP hit rate, shadow-origin skill writes, spawn-time
    cost). Pass `snapshot_path` to also dump a markdown report for humans.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from .._mcp import mcp
from ..db import get_db
from ..helpers import fmt_age
from ..identity import _ensure_session
from ..shadow_review import (
    SHADOW_REVIEW_PROMPT,
    _collect_window,
    _last_shadow_ts,
    _record_shadow_pass,
    run_shadow_pass,
    shadow_telemetry,
)
from ..config import (
    SHADOW_REVIEW_INTERVAL_S,
    SHADOW_REVIEW_MIN_CHARS,
    SHADOW_REVIEW_WINDOW_S,
)


@mcp.tool()
def shadow_review_run(force: bool = False, dry_run: bool = False) -> str:
    """Fire one shadow-review pass.

    `force=True` runs even when the daemon is disabled (interval=0). Used
    by tests and one-shot triage.

    `dry_run=True` short-circuits before the spawn — returns the dialog
    dump that WOULD be evaluated, plus n_chars and high-water cursor. No
    spawn. No cursor advance. Use this to inspect candidate windows
    before paying for an evaluator child.
    """
    conn = get_db()
    _ensure_session(conn)
    if dry_run:
        floor = _last_shadow_ts(conn)
        dump, high_water, n_chars = _collect_window(
            conn, floor, SHADOW_REVIEW_WINDOW_S,
        )
        if n_chars == 0:
            return "dry_run: no_window (nothing new since last cursor)"
        head = dump[:2000]
        suffix = "…(truncated for display)" if len(dump) > 2000 else ""
        return (
            f"dry_run: n_chars={n_chars} high_water_ts={high_water} "
            f"min_chars={SHADOW_REVIEW_MIN_CHARS} "
            f"would_spawn={'yes' if n_chars >= SHADOW_REVIEW_MIN_CHARS else 'no'}\n\n"
            f"--- prompt preview ---\n"
            f"{SHADOW_REVIEW_PROMPT[:400]}…\n\n"
            f"--- dialog window head ---\n{head}{suffix}"
        )
    return run_shadow_pass(force=force)


def _fmt_hit_rate(hit_rate: Optional[float]) -> str:
    return f"{hit_rate:.0%}" if hit_rate is not None else "n/a"


def _telemetry_lines(tel: dict) -> list[str]:
    """Render shadow_telemetry() output as compact human-readable lines."""
    lines = ["", "telemetry (production validation — spawn cost vs hit rate)"]
    for w in tel["windows"]:
        oc = w["outcomes"]
        vd = w["verdicts"]
        avg = fmt_age(int(w["avg_spawn_s"])) if w["avg_spawn_s"] else "-"
        lines.append(
            f"  {w['label']:<4} ticks={w['ticks']}  "
            f"no_window={oc['no_window']} too_short={oc['too_short']} "
            f"spawned={oc['spawned']} deferred={oc['deferred']} "
            f"error={oc['error']}"
        )
        lines.append(
            f"       children={w['children']}  "
            f"materialized={vd['materialized']} skip={vd['skip']} "
            f"unknown={vd['unknown']}  hit_rate={_fmt_hit_rate(w['hit_rate'])}"
        )
        lines.append(
            f"       skill_writes(shadow)={w['skill_writes']}  "
            f"spawn_time={fmt_age(w['spawn_seconds'])} "
            f"(avg {avg}, ended {w['ended']})"
        )
    if tel.get("logs_unread"):
        lines.append(
            f"  note: {tel['logs_unread']} child verdict log(s) skipped past "
            f"the read cap (counted as unknown)"
        )
    return lines


def _telemetry_markdown(tel: dict, now: int) -> str:
    """Side-channel markdown snapshot of the telemetry for human review."""
    when = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(now))
    out = [
        "# Shadow-review telemetry",
        "",
        f"_snapshot {when} — read-only; reads only the trail each pass "
        f"already leaves (events, tasks, child logs, skill_usage)._",
        "",
        f"config: interval_s={SHADOW_REVIEW_INTERVAL_S:.0f} "
        f"window_s={SHADOW_REVIEW_WINDOW_S} min_chars={SHADOW_REVIEW_MIN_CHARS}",
        "",
        "| window | ticks | no_window | too_short | spawned | deferred | "
        "error | children | materialized | skip | unknown | hit_rate | "
        "skill_writes | spawn_time | avg |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for w in tel["windows"]:
        oc, vd = w["outcomes"], w["verdicts"]
        avg = fmt_age(int(w["avg_spawn_s"])) if w["avg_spawn_s"] else "-"
        out.append(
            f"| {w['label']} | {w['ticks']} | {oc['no_window']} | "
            f"{oc['too_short']} | {oc['spawned']} | {oc['deferred']} | "
            f"{oc['error']} | {w['children']} | {vd['materialized']} | "
            f"{vd['skip']} | {vd['unknown']} | {_fmt_hit_rate(w['hit_rate'])} "
            f"| {w['skill_writes']} | {fmt_age(w['spawn_seconds'])} | {avg} |"
        )
    if tel.get("logs_unread"):
        out += ["", f"> {tel['logs_unread']} child verdict log(s) skipped "
                f"past the read cap (counted as unknown)."]
    out += ["", f"task log dir: `{tel['log_dir']}`", ""]
    return "\n".join(out)


@mcp.tool()
def shadow_review_status(snapshot_path: str = "") -> str:
    """Show shadow-review config, recent passes, and production telemetry.

    Snapshot for sanity-checking that the daemon is alive and advancing
    its cursor, PLUS the production-validation rollup (issue #6): for the
    24h and 7d windows it aggregates how often the daemon fired, the
    outcome mix (no_window / too_short / spawned / deferred / error), the
    MATERIALIZED-vs-SKIP hit rate of spawned evaluator children, durable
    skill writes attributable to shadow_review, and the total Claude-spawn
    time spent — so you can tell whether the loop earns its Opus minutes or
    just emits SKIPs.

    `snapshot_path`: when set, also writes a markdown report to that path
    for human review (the side-channel snapshot)."""
    conn = get_db()
    _ensure_session(conn)
    floor = _last_shadow_ts(conn)
    now = int(time.time())
    age_s = (now - floor) if floor else None
    lines = [
        f"interval_s={SHADOW_REVIEW_INTERVAL_S:.0f} "
        f"window_s={SHADOW_REVIEW_WINDOW_S} "
        f"min_chars={SHADOW_REVIEW_MIN_CHARS}",
        f"cursor_ts={floor} (age={age_s}s)" if floor
        else "cursor_ts=0 (no prior pass)",
        "",
        "recent passes (newest first):",
    ]
    try:
        rows = conn.execute(
            "SELECT created_at, summary FROM events "
            "WHERE kind='shadow_review_pass' "
            "ORDER BY id DESC LIMIT 5"
        ).fetchall()
    except Exception:
        rows = []
    if not rows:
        lines.append("  (none)")
    else:
        for r in rows:
            ts = r["created_at"]
            age = now - int(ts) if ts else 0
            snip = (r["summary"] or "")[:120]
            lines.append(f"  {age}s_ago  {snip}")

    tel = shadow_telemetry(conn, now=now)
    lines += _telemetry_lines(tel)

    if snapshot_path:
        try:
            p = Path(snapshot_path).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_telemetry_markdown(tel, now), encoding="utf-8")
            lines.append(f"\nwrote markdown snapshot to {p}")
        except OSError as e:
            lines.append(f"\nsnapshot write failed: {e}")
    return "\n".join(lines)
