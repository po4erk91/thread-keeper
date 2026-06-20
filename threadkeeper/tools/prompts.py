"""User-controlled MCP **Prompts** for thread-keeper (roadmap #78).

The third MCP server primitive (after tools and resources): **Prompts** are
user-controlled, parameterized templates a host surfaces as native entry points.
Claude Code renders server prompts as ``/mcp__thread-keeper__<name>`` slash
commands; other hosts list them in a prompt picker.

These map the existing curation / audit / review flows onto discoverable,
parameterized commands. Each returns a single instruction message that drives the
matching read/act tools — it does NOT act on its own (a prompt is a template the
user invokes, not an automatic action). Hosts that don't advertise the
``prompts`` capability simply don't see them; every underlying tool stays usable.

  * ``review_recent_threads`` — summarize + triage recent threads
  * ``run_library_curation``  — fire a Curator audit of lessons + skills
  * ``audit_threadkeeper``    — whole-system health + hygiene audit
"""
from __future__ import annotations

from .._mcp import mcp


@mcp.prompt(
    name="review_recent_threads",
    title="Review my recent threads",
    description="Summarize and triage the most recent thread-keeper threads "
    "(open / idle / recently closed) and propose next moves.",
)
def review_recent_threads(limit: int = 10) -> str:
    """Triage the `limit` most recent threads via brief() + validate_threads()."""
    return (
        f"Review my {limit} most recent thread-keeper threads. "
        "Start by calling brief() for the live working set, then for each open "
        "or idle thread give a one-line status and a concrete next move; flag "
        "any that look stale or abandoned. Use validate_threads() to spot "
        "structural issues (orphans, missing outcomes) and review_thread(<id>) "
        "to pull detail on anything that needs it. Finish with the single "
        "highest-leverage thread to act on next."
    )


@mcp.prompt(
    name="run_library_curation",
    title="Run library curation",
    description="Fire one Curator audit pass over the lessons + skills library "
    "(KEEP / PATCH / CONSOLIDATE / PRUNE recommendations).",
)
def run_library_curation(force: bool = False) -> str:
    """Drive curator_review() and apply the resulting recommendations."""
    force_note = (
        " Pass force=True — the curator daemon interval may be disabled."
        if force else ""
    )
    return (
        "Run a curation pass over the thread-keeper lessons + skills library. "
        f"Call curator_review(force={str(force).lower()}) to spawn the audit "
        "child, which writes a REPORT.md with KEEP / PATCH / CONSOLIDATE / "
        "PRUNE recommendations." + force_note + " When it returns, summarize the "
        "recommendations and, for any PATCH / CONSOLIDATE / PRUNE you agree with, "
        "apply it via skill_manage / lesson_remove / consolidate. Use "
        "curator_review_status() first if you need to check the last pass."
    )


@mcp.prompt(
    name="audit_threadkeeper",
    title="Audit thread-keeper",
    description="Whole-system health + hygiene audit: telemetry rollup, thread "
    "integrity, and pending evolve suggestions.",
)
def audit_threadkeeper() -> str:
    """Run an end-to-end health audit via mp_dashboard/validate_threads/evolve."""
    return (
        "Audit the thread-keeper system end to end. Call mp_dashboard() for the "
        "store sizes + loop-activity rollup (look for loops that fire but "
        "produce nothing, or a climbing backlog), validate_threads() for "
        "structural integrity, and evolve_review() for pending self-improvement "
        "suggestions. Summarize overall health, call out the top 3 issues, and "
        "recommend a concrete fix for each."
    )
