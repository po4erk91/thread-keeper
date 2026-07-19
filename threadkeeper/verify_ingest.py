"""Cross-CLI ingest production verification.

The contract test in ``scripts/tk_verify_ingest.py`` proves the *parse*
path: it walks each adapter, ingests into a throwaway DB, and flags any
adapter that parsed messages but failed to persist them. That answers
"does the pipeline work mechanically?" — but not the question roadmap
issue #1 actually asks: *does shared cross-CLI memory hold up against
real production data?*

This module is the production half. It reads the **live**
``dialog_messages`` table (read-only) and evaluates the three acceptance
criteria from the issue:

  1. ``dialog_messages.source`` carries rows from every targeted CLI.
  2. ``shadow_review`` sees more than one adapter in the same recent
     window (cross-adapter, not just one CLI talking to itself).
  3. The Hermes-style learning loop fires on **non-Claude** sessions
     (shadow-review passes exist *and* the windows they evaluated
     contained non-claude-code dialog).

The verdict logic is split into pure functions (``evaluate_verdict`` and
helpers) so it can be unit-tested without a database, and a thin
``live_production_report`` that does the read-only SQL and feeds the pure
layer.

Adapter slots
-------------
The production ingest contract targets the three currently ingestible CLI
families below. Antigravity (``agy``) is supported for MCP and spawning, but
its sqlite/protobuf conversation store is not parsed yet; it is therefore
reported as a capability gap rather than an impossible required ingest slot.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Optional

# Canonical slots with a production transcript parser. Order is display order.
CANONICAL_SLOTS: tuple[str, ...] = ("claude-code", "codex", "copilot")

# dialog_messages.source value -> canonical slot it satisfies.
SLOT_BY_SOURCE: dict[str, str] = {
    "claude-code": "claude-code",
    "claude-desktop": "claude-code",  # same vendor surface, Claude slot
    "codex": "codex",
    "copilot": "copilot",
    # Antigravity is supported for MCP/spawn but its protobuf/sqlite transcript
    # format is not ingestible yet, so it is intentionally not a required slot.
}

# A source with at least this many live rows counts as production-verified
# for its slot. Below it (but >0) is "thin" — present but not yet a
# convincing real-world sample (e.g. a single session-metadata row).
DEFAULT_THIN_THRESHOLD = 5

# How far back the "same recent window" check looks, anchored on the
# newest dialog message (not wall-clock now, so the harness gives a stable
# answer on a box that has been idle for a while).
DEFAULT_WINDOW_HOURS = 24


def slot_for_source(source: str) -> Optional[str]:
    """Map a ``dialog_messages.source`` tag to its canonical slot."""
    return SLOT_BY_SOURCE.get(source)


def evaluate_coverage(
    source_counts: dict[str, int],
    thin_threshold: int = DEFAULT_THIN_THRESHOLD,
) -> dict[str, dict]:
    """Roll per-source row counts up into per-slot coverage status.

    Returns a dict keyed by canonical slot, each value::

        {"sources": {<source>: <count>, ...}, "status": <status>}

    where status is one of ``verified`` (some source >= threshold),
    ``thin`` (max source count in 1..threshold-1), or ``absent`` (no rows
    for any source in the slot).
    """
    slots: dict[str, dict] = {
        slot: {"sources": {}, "status": "absent"} for slot in CANONICAL_SLOTS
    }
    for source, count in source_counts.items():
        slot = SLOT_BY_SOURCE.get(source)
        if slot is None:
            continue  # unknown/unmapped source (e.g. vscode) — not a slot
        slots[slot]["sources"][source] = count

    for slot, info in slots.items():
        counts = info["sources"].values()
        top = max(counts) if counts else 0
        if top >= thin_threshold:
            info["status"] = "verified"
        elif top > 0:
            info["status"] = "thin"
        else:
            info["status"] = "absent"
    return slots


def evaluate_verdict(
    *,
    source_counts: dict[str, int],
    window_sources: Iterable[str],
    shadow_passes: int,
    thin_threshold: int = DEFAULT_THIN_THRESHOLD,
) -> dict:
    """Pure verdict over the three issue-#1 acceptance criteria.

    Inputs are plain data so this is trivially unit-testable:

      * ``source_counts``  — {source: live_row_count}
      * ``window_sources`` — distinct sources seen in the most recent
        dialog window (the same diff shadow-review consumes)
      * ``shadow_passes``  — count of recorded ``shadow_review_pass``
        events (proves the learning loop has actually run)

    Returns a structured report dict (see module docstring / tests).
    """
    window = sorted(set(window_sources))
    slots = evaluate_coverage(source_counts, thin_threshold)
    verified_slots = [s for s, i in slots.items() if i["status"] == "verified"]

    non_claude_window = [
        s for s in window if SLOT_BY_SOURCE.get(s) not in (None, "claude-code")
    ]

    c1_pass = len(verified_slots) == len(CANONICAL_SLOTS)
    c2_pass = len(window) >= 2
    c3_pass = shadow_passes > 0 and len(non_claude_window) > 0

    criteria = {
        "all_sources_present": {
            "pass": c1_pass,
            "verified_slots": sorted(verified_slots),
            "total_slots": len(CANONICAL_SLOTS),
            "detail": (
                f"{len(verified_slots)}/{len(CANONICAL_SLOTS)} CLI slots have "
                f"production rows above the {thin_threshold}-row bar"
            ),
        },
        "cross_adapter_window": {
            "pass": c2_pass,
            "distinct_sources": window,
            "detail": (
                f"{len(window)} distinct source(s) in the most recent dialog "
                "window" + (" — single adapter only" if len(window) < 2 else "")
            ),
        },
        "learning_loop_non_claude": {
            "pass": c3_pass,
            "sources": non_claude_window,
            "shadow_passes": shadow_passes,
            "detail": (
                f"{shadow_passes} shadow-review pass(es) recorded; "
                + (
                    "recent windows include non-Claude sources "
                    f"({', '.join(non_claude_window)})"
                    if non_claude_window
                    else "no non-Claude source in the recent window"
                )
            ),
        },
    }

    if c1_pass and c2_pass and c3_pass:
        verdict = "PASS"
    elif len(verified_slots) < 2 or not (c2_pass or c3_pass):
        verdict = "FAIL"
    else:
        verdict = "PARTIAL"

    summary = _summarize(verdict, slots, criteria)
    return {"slots": slots, "criteria": criteria, "verdict": verdict,
            "summary": summary}


def _summarize(verdict: str, slots: dict, criteria: dict) -> str:
    parts = []
    for slot in CANONICAL_SLOTS:
        st = slots[slot]["status"]
        mark = {"verified": "ok", "thin": "thin", "absent": "missing"}[st]
        parts.append(f"{slot}={mark}")
    cross = "yes" if criteria["cross_adapter_window"]["pass"] else "no"
    loop = "yes" if criteria["learning_loop_non_claude"]["pass"] else "no"
    return (
        f"{verdict}: slots[{', '.join(parts)}] "
        f"cross_adapter_window={cross} learning_loop_non_claude={loop}"
    )


# ---------------------------------------------------------------------------
# Live (read-only) DB inspection
# ---------------------------------------------------------------------------

def _ro_connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the live DB strictly read-only (never mutate production data)."""
    uri = f"file:{Path(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def collect_live_signals(
    conn: sqlite3.Connection,
    window_hours: int = DEFAULT_WINDOW_HOURS,
) -> dict:
    """Pull the raw signals the verdict needs from an open connection.

    Separated from :func:`live_production_report` so tests can populate an
    in-memory sqlite and exercise the SQL without a temp file.
    """
    source_counts: dict[str, int] = {}
    for r in conn.execute(
        "SELECT source, COUNT(*) AS n FROM dialog_messages GROUP BY source"
    ):
        source_counts[r["source"]] = r["n"]

    newest_row = conn.execute(
        "SELECT MAX(created_at) AS m FROM dialog_messages"
    ).fetchone()
    newest = (newest_row["m"] if newest_row else 0) or 0
    cutoff = newest - window_hours * 3600

    window_sources: list[str] = []
    if newest:
        window_sources = [
            r["source"]
            for r in conn.execute(
                "SELECT DISTINCT source FROM dialog_messages "
                "WHERE created_at > ?",
                (cutoff,),
            )
        ]

    shadow_passes = 0
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind='shadow_review_pass'"
        ).fetchone()
        shadow_passes = (row["n"] if row else 0) or 0
    except sqlite3.OperationalError:
        shadow_passes = 0  # events table absent (fresh/empty install)

    return {
        "source_counts": source_counts,
        "window_sources": window_sources,
        "window_hours": window_hours,
        "newest_ts": newest,
        "shadow_passes": shadow_passes,
    }


def live_production_report(
    db_path: str | Path,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    thin_threshold: int = DEFAULT_THIN_THRESHOLD,
) -> dict:
    """Read the live DB read-only and return signals + verdict."""
    conn = _ro_connect(db_path)
    try:
        signals = collect_live_signals(conn, window_hours)
    finally:
        conn.close()
    verdict = evaluate_verdict(
        source_counts=signals["source_counts"],
        window_sources=signals["window_sources"],
        shadow_passes=signals["shadow_passes"],
        thin_threshold=thin_threshold,
    )
    return {"db_path": str(db_path), "signals": signals, **verdict}


def format_report(report: dict) -> str:
    """Render a :func:`live_production_report` result as human text."""
    lines: list[str] = []
    lines.append("[live production verification]")
    lines.append(f"  db: {report.get('db_path', '?')}")
    sig = report.get("signals", {})
    lines.append(
        f"  window: last {sig.get('window_hours', '?')}h "
        f"(anchored on newest dialog row)"
    )
    lines.append("")
    lines.append("  per-slot coverage:")
    for slot in CANONICAL_SLOTS:
        info = report["slots"][slot]
        mark = {"verified": "✓", "thin": "·", "absent": "✗"}[info["status"]]
        srcs = ", ".join(
            f"{s}={c}" for s, c in sorted(info["sources"].items())
        ) or "(none)"
        lines.append(f"    {mark} {slot:12s} {info['status']:8s} {srcs}")
    lines.append("")
    lines.append("  acceptance criteria:")
    for key in (
        "all_sources_present",
        "cross_adapter_window",
        "learning_loop_non_claude",
    ):
        crit = report["criteria"][key]
        mark = "PASS" if crit["pass"] else "----"
        lines.append(f"    [{mark}] {key}: {crit['detail']}")
    lines.append("")
    lines.append(f"  VERDICT: {report['verdict']}")
    lines.append(f"  {report['summary']}")
    return "\n".join(lines)
