"""Role-scoped MCP tool selection and deferred capability metadata.

Profiles are intentionally a server-side boundary.  A client-side allowlist
may add another restriction, but it must never be the only thing preventing a
tool from being advertised or called.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Iterable, Mapping


TOOL_PROFILE_ENV = "THREADKEEPER_TOOL_PROFILE"
TOOL_INCLUDE_ENV = "THREADKEEPER_TOOL_INCLUDE"
TOOL_EXCLUDE_ENV = "THREADKEEPER_TOOL_EXCLUDE"
TOOL_PARENT_ALLOWLIST_ENV = "THREADKEEPER_TOOL_PARENT_ALLOWLIST"
CHILD_TOOL_PROFILE_ENV = "THREADKEEPER_CHILD_TOOL_PROFILE"


class ToolProfileError(ValueError):
    """Raised when a tool profile configuration cannot be applied safely."""


CORE_TOOLS = frozenset({
    "ask",
    "brief",
    "broadcast",
    "capability_search",
    "close_thread",
    "context",
    "dialog_search",
    "inbox",
    "live_status",
    "note",
    "open_thread",
    "peers",
    "presence",
    "respond",
    "search",
    "session_end",
    "verbatim_user",
    "wait",
    "whoami",
    "whisper",
})

COORDINATION_TOOLS = CORE_TOOLS | {
    "claim_pickup",
    "pickup_candidates",
    "release_pickup",
    "search_via_parent",
    "spawn",
    "spawn_budget_set",
    "spawn_budget_status",
    "spawn_status",
    "tag_signal",
    "task_logs",
    "task_thread",
    "tasks",
    "tournament",
}

MEMORY_ADMIN_TOOLS = CORE_TOOLS | {
    "compost",
    "memory_proposals",
    "memory_proposal_decide",
    "concept_manage",
    "core_get",
    "core_list",
    "core_remove",
    "core_set",
    "forget",
    "ingest",
    "lesson_append",
    "lesson_get",
    "lesson_list",
    "lesson_neighbors",
    "lesson_patch",
    "lesson_remove",
    "lesson_restore",
    "lesson_verifications",
    "link",
    "list_concepts",
    "neighbors",
    "open_dialog_window",
    "register_concept",
    "style_set",
    "unlink",
}

LEARNING_LOOP_TOOLS = CORE_TOOLS | {
    "accept_candidate",
    "auto_review_trigger",
    "candidate_review_run",
    "candidate_review_status",
    "consolidate",
    "curator_report_write",
    "curator_restore",
    "curator_review",
    "curator_review_status",
    "curator_run",
    "curator_verify_lesson",
    "lesson_verifications",
    "dialectic_claim",
    "dialectic_evidence",
    "dialectic_mine_run",
    "dialectic_mine_status",
    "dialectic_observation_resolve",
    "dialectic_review",
    "dialectic_supersede",
    "dialectic_synthesis",
    "dialectic_validate_run",
    "dialectic_validate_status",
    "distill",
    "export_distillates",
    "extract_recent",
    "find_invariants",
    "find_missed_spawns",
    "idle_thread",
    "mark_skill_materialized",
    "pending_distillates",
    "record_attempt",
    "register_probe",
    "reject_candidate",
    "reliability_for",
    "review_candidates",
    "review_thread",
    "run_probe",
    "shadow_review_run",
    "shadow_review_status",
    "skill_list",
    "skill_manage",
    "skill_record",
    "skill_validate",
    "validate_threads",
    "vote_distill",
    "weak_spots",
}

PROFILE_TOOL_NAMES: Mapping[str, frozenset[str]] = {
    "core": CORE_TOOLS,
    "coordination": frozenset(COORDINATION_TOOLS),
    "memory-admin": frozenset(MEMORY_ADMIN_TOOLS),
    "learning-loops": frozenset(LEARNING_LOOP_TOOLS),
    "model-router": frozenset({"model_route_write"}),
}
PROFILE_NAMES = frozenset((*PROFILE_TOOL_NAMES, "full"))
DECLARED_TOOL_NAMES = frozenset().union(*PROFILE_TOOL_NAMES.values())


@dataclass(frozen=True)
class CapabilityFamily:
    name: str
    summary: str
    profile: str
    tools: frozenset[str]
    keywords: frozenset[str]


CAPABILITY_FAMILIES = (
    CapabilityFamily(
        "coordination",
        "Spawn children, inspect tasks, and route parent-mediated search.",
        "coordination",
        frozenset(COORDINATION_TOOLS - CORE_TOOLS),
        frozenset({"child", "coordination", "spawn", "task", "parent"}),
    ),
    CapabilityFamily(
        "memory administration",
        "Manage durable memory, concepts, lessons, graph links, and erasure.",
        "memory-admin",
        frozenset(MEMORY_ADMIN_TOOLS - CORE_TOOLS),
        frozenset({"admin", "concept", "erase", "graph", "lesson", "memory"}),
    ),
    CapabilityFamily(
        "learning loops",
        "Run extraction, validation, curation, probes, and skill learning workflows.",
        "learning-loops",
        frozenset(LEARNING_LOOP_TOOLS - CORE_TOOLS),
        frozenset({"candidate", "curator", "learn", "probe", "review", "skill"}),
    ),
    CapabilityFamily(
        "evolve automation",
        "Review and apply roadmap-backed repository improvements.",
        "full",
        frozenset({
            "evolve_apply", "evolve_apply_conflicted_pr",
            "evolve_apply_curator_report", "evolve_apply_roadmap_issue",
            "evolve_apply_status", "evolve_decide", "evolve_format",
            "evolve_issue_create", "evolve_mark_applied",
            "evolve_mark_curator_report_applied",
            "evolve_mark_roadmap_issue_applied", "evolve_prune_managed_venv",
            "evolve_review",
        }),
        frozenset({"apply", "evolve", "issue", "roadmap", "repository"}),
    ),
    CapabilityFamily(
        "operations and maintenance",
        "Inspect runtime health, reload configuration, and maintain local storage.",
        "full",
        frozenset({
            "agent_memory_cleanup", "agent_status", "config_reload",
            "config_watch_status", "db_compact", "db_deduplicate_embeddings",
            "memory_guard_check", "memory_guard_reclaim", "memory_guard_status",
            "mp_cleanup", "mp_dashboard", "mp_health",
        }),
        frozenset({"config", "database", "health", "maintenance", "operations"}),
    ),
    CapabilityFamily(
        "synchronization",
        "Inspect peers and synchronize replicated memory across machines.",
        "full",
        frozenset({"sync_now", "sync_peers", "sync_status"}),
        frozenset({"peer", "replica", "sync", "synchronization"}),
    ),
    CapabilityFamily(
        "panel deliberation",
        "Convene a multi-role panel for a bounded decision.",
        "full",
        frozenset({"convene_panel"}),
        frozenset({"decision", "panel", "roles"}),
    ),
)


def _parse_names(value: str | Iterable[str] | None, label: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        values = value.split(",")
    else:
        values = value
    names = frozenset(name.strip() for name in values if name and name.strip())
    if any("," in name for name in names):
        raise ToolProfileError(f"invalid {label} tool name")
    return names


def resolve_tool_names(
    profile: str | None,
    *,
    include: str | Iterable[str] | None = None,
    exclude: str | Iterable[str] | None = None,
    parent_allowlist: str | Iterable[str] | None = None,
    available_names: Iterable[str],
) -> frozenset[str]:
    """Resolve one profile without silently accepting unknown tool names."""
    profile_name = (profile or "full").strip().lower()
    if profile_name not in PROFILE_NAMES:
        valid = ", ".join(sorted(PROFILE_NAMES))
        raise ToolProfileError(
            f"unknown tool profile={profile_name!r}; expected one of {valid}"
        )

    available = frozenset(available_names)
    included = _parse_names(include, "include")
    excluded = _parse_names(exclude, "exclude")
    inherited = _parse_names(parent_allowlist, "parent allowlist")
    unknown = (included | excluded | inherited) - available
    if unknown:
        raise ToolProfileError(f"unknown tool override(s): {', '.join(sorted(unknown))}")
    overlap = included & excluded
    if overlap:
        raise ToolProfileError(
            f"tool cannot be both included and excluded: {', '.join(sorted(overlap))}"
        )

    if profile_name == "full":
        selected = available
    else:
        selected = PROFILE_TOOL_NAMES[profile_name] & available
    selected = (selected | included) - excluded
    if parent_allowlist is not None:
        selected &= inherited
    return frozenset(selected)


def child_profile_for_role(role: str, configured_profile: str = "") -> str:
    """Choose the smallest built-in profile that covers a spawned role."""
    requested = configured_profile.strip().lower()
    if requested:
        if requested not in PROFILE_NAMES:
            valid = ", ".join(sorted(PROFILE_NAMES))
            raise ToolProfileError(
                f"unknown child tool profile={requested!r}; expected one of {valid}"
            )
        return requested

    role_name = role.strip().lower()
    if role_name == "model_router":
        return "model-router"
    if role_name in {
        "archivist", "background_review", "candidate_reviewer", "curator",
        "dialectic_validator", "probe_runner", "shadow_observer",
    }:
        return "learning-loops"
    if role_name in {"evolve_applier", "evolve_researcher", "evolve_reviewer"}:
        return "full"
    if role_name == "memory_admin":
        return "memory-admin"
    if role_name == "coordinator":
        return "coordination"
    return "core"


def child_profile_environment(
    parent_allowed_names: Iterable[str],
    role: str,
    configured_profile: str = "",
) -> dict[str, str]:
    """Build a child constraint that can only narrow its parent's surface."""
    parent_names = frozenset(parent_allowed_names)
    profile_name = child_profile_for_role(role, configured_profile)
    child_names = resolve_tool_names(
        profile_name,
        parent_allowlist=parent_names,
        available_names=DECLARED_TOOL_NAMES | parent_names,
    )
    return {
        TOOL_PROFILE_ENV: profile_name,
        TOOL_PARENT_ALLOWLIST_ENV: ",".join(sorted(child_names)),
    }


def deferred_capabilities(
    query: str,
    active_tool_names: Iterable[str],
    *,
    limit: int = 6,
) -> dict[str, object]:
    """Return bounded family metadata, never hidden concrete tool names."""
    normalized_query = query.strip().lower()[:120]
    active = frozenset(active_tool_names)
    matches = []
    for family in CAPABILITY_FAMILIES:
        if family.tools <= active:
            continue
        searchable = " ".join((family.name, family.summary, *family.keywords))
        if normalized_query and normalized_query not in searchable.lower():
            continue
        matches.append({
            "family": family.name,
            "summary": family.summary,
            "profile": family.profile,
        })
    return {
        "query": normalized_query,
        "matches": matches[:limit],
        "truncated": len(matches) > limit,
    }


def environment_selection(
    available_names: Iterable[str],
    *,
    profile: str | None = None,
    include: str | Iterable[str] | None = None,
    exclude: str | Iterable[str] | None = None,
    parent_allowlist: str | Iterable[str] | None = None,
) -> frozenset[str]:
    """Resolve public configuration and an optional parent constraint."""
    return resolve_tool_names(
        profile if profile is not None else os.environ.get(TOOL_PROFILE_ENV, "full"),
        include=include if include is not None else os.environ.get(TOOL_INCLUDE_ENV),
        exclude=exclude if exclude is not None else os.environ.get(TOOL_EXCLUDE_ENV),
        parent_allowlist=(parent_allowlist if parent_allowlist is not None else (
            os.environ[TOOL_PARENT_ALLOWLIST_ENV]
            if TOOL_PARENT_ALLOWLIST_ENV in os.environ else None
        )),
        available_names=available_names,
    )
