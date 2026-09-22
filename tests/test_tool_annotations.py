"""MCP tool-annotation contract (roadmap #67).

Every thread-keeper tool must carry an explicit read/write annotation so a
confirmation/elicitation client (issue #26) can tell pure reads from
mutations without calling them. This test pins that contract:

  * every registered tool sets ``readOnlyHint`` explicitly (never None);
  * the curated read / write / delete-class sets match their annotations;
  * no mutating tool is marked ``readOnlyHint=True``;
  * every delete-class tool carries ``destructiveHint=True``;
  * a tool added without being classified here fails the name-set check;
  * the five status tools advertise an ``outputSchema`` and return
    ``structuredContent`` that validates against it, with the legacy text
    block preserved.

The classification mirrors :mod:`threadkeeper._mcp` (``read_tool`` /
``write_tool``); it is intentionally duplicated here so a miscategorised or
unannotated tool is caught in CI.
"""
from __future__ import annotations

import jsonschema
import pytest

# --- read-only tools: pure queries, no state mutation ------------------------
READ_TOOLS = {
    "agent_status", "brief", "candidate_review_status", "compost",
    "config_watch_status", "context", "core_get", "core_list",
    "curator_review_status", "dialectic_mine_status", "dialectic_review",
    "dialectic_synthesis", "dialectic_validate_status", "dialog_search",
    "evolve_apply_status", "evolve_review", "expand_concept", "find_invariants",
    "find_missed_spawns", "list_concepts",
    "memory_guard_status", "mp_dashboard", "mp_health", "neighbors", "peers",
    "pending_distillates", "pickup_candidates", "reliability_for",
    "review_candidates", "run_probe", "search", "shadow_review_status",
    "skill_list", "skill_validate", "spawn_budget_status", "spawn_status",
    "sync_peers", "sync_status", "task_logs",
    "task_thread", "tasks", "weak_spots", "whoami",
}

# --- delete/overwrite/kill tools: destructiveHint=True -----------------------
DELETE_CLASS_TOOLS = {
    "agent_memory_cleanup", "concept_manage", "consolidate", "core_remove",
    "curator_restore", "curator_run", "forget", "lesson_remove",
    "evolve_prune_managed_venv", "memory_guard_check", "mp_cleanup",
    "skill_manage", "unlink",
}

# --- non-destructive mutating tools ------------------------------------------
WRITE_TOOLS = {
    "accept_candidate", "ask", "auto_review_trigger", "broadcast",
    "candidate_review_run", "claim_pickup", "close_thread", "config_reload",
    "convene_panel", "core_set", "curator_report_write", "curator_review",
    "db_compact", "db_deduplicate_embeddings", "dialectic_claim",
    "dialectic_evidence", "dialectic_mine_run", "dialectic_observation_resolve",
    "dialectic_supersede", "dialectic_validate_run", "distill", "evolve_apply",
    "evolve_apply_conflicted_pr", "evolve_apply_curator_report",
    "evolve_apply_roadmap_issue", "evolve_decide",
    "evolve_format", "evolve_issue_create", "evolve_mark_applied",
    "evolve_mark_curator_report_applied",
    "evolve_mark_roadmap_issue_applied", "export_distillates", "extract_recent",
    "idle_thread", "inbox", "ingest", "lesson_append", "lesson_restore",
    "link", "live_status", "lesson_get", "lesson_list", "mark_skill_materialized",
    "memory_guard_reclaim", "note",
    "open_dialog_window", "open_thread", "presence", "record_attempt",
    "register_concept", "register_probe", "reject_candidate", "release_pickup",
    "respond", "review_thread", "search_via_parent", "session_end",
    "shadow_review_run", "skill_record", "spawn", "spawn_budget_set",
    "style_set", "sync_now", "tag_signal", "tournament", "validate_threads",
    "verbatim_user", "vote_distill", "wait", "whisper",
}

# status tools that must expose outputSchema + structuredContent
STATUS_TOOLS = {
    "context", "spawn_budget_status", "spawn_status", "mp_health",
    "agent_status",
}

ALL_CLASSIFIED = READ_TOOLS | DELETE_CLASS_TOOLS | WRITE_TOOLS


def _tools(pkg):
    return {t.name: t for t in pkg["mcp"]._tool_manager.list_tools()}


def _annotation(annotation, wire_name):
    """Read an annotation through either SDK's Python field spelling."""
    if hasattr(annotation, wire_name):
        return getattr(annotation, wire_name)
    fields = {
        "readOnlyHint": "read_only_hint",
        "destructiveHint": "destructive_hint",
        "idempotentHint": "idempotent_hint",
    }
    return getattr(annotation, fields[wire_name])


def _structured_content(result):
    """SDK 2.x renamed the Python field but retained the MCP wire key."""
    if hasattr(result, "structuredContent"):
        return result.structuredContent
    return result.structured_content


def test_every_tool_is_classified(fresh_mp):
    """A new tool that is not added to one of the curated sets fails here,
    forcing an explicit read/write decision."""
    registered = set(_tools(fresh_mp))
    assert registered == ALL_CLASSIFIED, {
        "unclassified": sorted(registered - ALL_CLASSIFIED),
        "stale_in_test": sorted(ALL_CLASSIFIED - registered),
    }


def test_every_tool_has_explicit_read_write_hint(fresh_mp):
    tools = _tools(fresh_mp)
    missing = [
        n for n, t in tools.items()
        if t.annotations is None or _annotation(t.annotations, "readOnlyHint") is None
    ]
    assert not missing, f"tools missing explicit readOnlyHint: {sorted(missing)}"


def test_read_tools_are_read_only(fresh_mp):
    tools = _tools(fresh_mp)
    for n in READ_TOOLS:
        a = tools[n].annotations
        assert _annotation(a, "readOnlyHint") is True, f"{n} should be readOnlyHint=True"
        assert _annotation(a, "destructiveHint") in (None, False), f"{n} read tool is destructive?"


def test_no_mutating_tool_marked_read_only(fresh_mp):
    tools = _tools(fresh_mp)
    bad = [
        n for n in (WRITE_TOOLS | DELETE_CLASS_TOOLS)
        if _annotation(tools[n].annotations, "readOnlyHint") is not False
    ]
    assert not bad, f"mutating tools wrongly marked read-only: {sorted(bad)}"


def test_delete_class_tools_are_destructive(fresh_mp):
    tools = _tools(fresh_mp)
    for n in DELETE_CLASS_TOOLS:
        a = tools[n].annotations
        assert _annotation(a, "readOnlyHint") is False, f"{n} delete-class must not be read-only"
        assert _annotation(a, "destructiveHint") is True, f"{n} must carry destructiveHint=True"


def test_non_destructive_writes_not_flagged_destructive(fresh_mp):
    tools = _tools(fresh_mp)
    bad = [
        n for n in WRITE_TOOLS
        if _annotation(tools[n].annotations, "destructiveHint") is True
    ]
    assert not bad, f"non-destructive writes flagged destructive: {sorted(bad)}"


def test_annotation_consistency(fresh_mp):
    """destructive/idempotent hints are only meaningful when not read-only."""
    tools = _tools(fresh_mp)
    for n, t in tools.items():
        a = t.annotations
        if _annotation(a, "readOnlyHint"):
            assert _annotation(a, "destructiveHint") in (None, False), n
            assert _annotation(a, "idempotentHint") in (None, False), n
        if _annotation(a, "destructiveHint"):
            assert _annotation(a, "readOnlyHint") is False, n


@pytest.mark.parametrize("name", sorted(STATUS_TOOLS))
def test_status_tools_emit_validating_structured_content(fresh_mp, name):
    pkg = fresh_mp
    tools = _tools(pkg)
    tool = tools[name]
    assert tool.output_schema is not None, f"{name} must advertise outputSchema"

    fn = pkg["mcp"]._tool_manager._tools[name].fn
    kwargs = {"refresh": False} if name == "agent_status" else {}
    result = fn(**kwargs)

    # legacy human-readable text block preserved
    text_blocks = [
        c for c in result.content
        if getattr(c, "type", None) == "text" and c.text
    ]
    assert text_blocks, f"{name} dropped its legacy text block"

    # structured content present and validates against the advertised schema
    structured_content = _structured_content(result)
    assert structured_content is not None, f"{name} returned no structuredContent"
    jsonschema.validate(structured_content, tool.output_schema)
