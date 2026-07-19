"""Shared metadata for configurable agent-backed roles and mechanical jobs.

The macOS settings client consumes this through ``tk-agent-status
--settings-catalog``.  Keeping the role taxonomy in Python prevents the UI from
silently drifting away from the actual ``spawn(role=...)`` call sites.
"""
from __future__ import annotations

from typing import Final


AGENT_ROLES: Final[tuple[dict[str, str], ...]] = (
    {
        "role": "shadow_observer",
        "name": "Shadow observer",
        "description": (
            "Scans recent dialog for durable lessons, workflow corrections, "
            "and skill materialization opportunities."
        ),
        "reads": "Recent conversation transcripts and closed-thread notes",
        "writes": "Lessons, skills, and review signals",
        "impact": "memory_write",
        "interval_key": "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S",
    },
    {
        "role": "archivist",
        "name": "Archivist",
        "description": (
            "Materializes a closed thread into durable memory or a reusable "
            "skill when explicitly requested."
        ),
        "reads": "One closed thread and its notes",
        "writes": "Lessons and Skill.md files",
        "impact": "memory_write",
        "interval_key": "",
    },
    {
        "role": "candidate_reviewer",
        "name": "Candidate reviewer",
        "description": (
            "Reviews extracted conversation candidates and decides what should "
            "become memory, a skill update, or a rejected false positive."
        ),
        "reads": "Pending extraction candidates and nearby dialog",
        "writes": "Lessons, skills, notes, or rejection decisions",
        "impact": "memory_write",
        "interval_key": "THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S",
    },
    {
        "role": "curator",
        "name": "Curator",
        "description": (
            "Audits existing lessons and skills for stale, duplicate, or "
            "patch-worthy knowledge."
        ),
        "reads": "The lessons and skills library plus usage telemetry",
        "writes": "Lesson/skill patches, merges, archives, and reports",
        "impact": "memory_write",
        "interval_key": "THREADKEEPER_CURATOR_INTERVAL_S",
    },
    {
        "role": "dialectic_validator",
        "name": "Dialectic validator",
        "description": (
            "Turns buffered user-model observations into evidence-backed "
            "claims about preferences, constraints, and working style."
        ),
        "reads": "Pending dialectic observations and supporting dialog",
        "writes": "User-model claims and evidence records",
        "impact": "memory_write",
        "interval_key": "THREADKEEPER_DIALECTIC_VALIDATE_INTERVAL_S",
    },
    {
        "role": "probe_runner",
        "name": "Probe runner",
        "description": (
            "Runs isolated reliability probes and records whether known "
            "failure cases are still handled correctly."
        ),
        "reads": "Enabled probe definitions and grader expectations",
        "writes": "Probe attempts and reliability results",
        "impact": "memory_write",
        "interval_key": "THREADKEEPER_PROBE_INTERVAL_S",
    },
    {
        "role": "evolve_researcher",
        "name": "Evolve researcher",
        "description": (
            "Investigates the repository and external evidence used by the "
            "evolve reviewer before a roadmap decision."
        ),
        "reads": "Repository code, documentation, and research sources",
        "writes": "Research findings for the evolve review",
        "impact": "read_only",
        "interval_key": "THREADKEEPER_EVOLVE_REVIEW_INTERVAL_S",
    },
    {
        "role": "evolve_reviewer",
        "name": "Evolve reviewer",
        "description": (
            "Audits ThreadKeeper for safety, leaks, optimization, and new "
            "ideas, then updates the roadmap or opens issues."
        ),
        "reads": "Repository, runtime reports, and researcher findings",
        "writes": "Roadmap proposals and GitHub issues",
        "impact": "external_write",
        "interval_key": "THREADKEEPER_EVOLVE_REVIEW_INTERVAL_S",
    },
    {
        "role": "evolve_applier",
        "name": "Evolve applier",
        "description": (
            "Implements one approved roadmap issue, repairs conflicted applier "
            "PRs, or applies a promoted maintenance report."
        ),
        "reads": "Repository code and one approved work item",
        "writes": "Code, documentation, commits, branches, and pull requests",
        "impact": "code_write",
        "interval_key": "THREADKEEPER_EVOLVE_APPLY_INTERVAL_S",
    },
)


MECHANICAL_JOBS: Final[tuple[dict[str, str], ...]] = (
    {
        "id": "ingest",
        "name": "Ingest",
        "description": "Imports new CLI transcript messages into local dialog memory.",
        "interval_key": "THREADKEEPER_INGEST_INTERVAL_S",
    },
    {
        "id": "retention",
        "name": "Retention",
        "description": "Prunes opted-in aged database rows and compacts local storage.",
        "interval_key": "THREADKEEPER_RETENTION_INTERVAL_S",
    },
    {
        "id": "extract",
        "name": "Extract",
        "description": "Mechanically captures decision-shaped dialog into a review queue.",
        "interval_key": "THREADKEEPER_EXTRACT_INTERVAL_S",
    },
    {
        "id": "dialectic_miner",
        "name": "Dialectic miner",
        "description": "Captures user replies into a buffer without LLM interpretation.",
        "interval_key": "THREADKEEPER_DIALECTIC_MINE_INTERVAL_S",
    },
    {
        "id": "thread_janitor",
        "name": "Thread janitor",
        "description": "Closes stale working-memory threads using deterministic rules.",
        "interval_key": "THREADKEEPER_THREAD_JANITOR_INTERVAL_S",
    },
    {
        "id": "auto_update",
        "name": "Auto update",
        "description": "Checks for and applies safe ThreadKeeper package updates.",
        "interval_key": "THREADKEEPER_AUTO_UPDATE_INTERVAL_S",
    },
    {
        "id": "skill_update",
        "name": "Skill updater",
        "description": "Checks skill sources and mirrors successful updates across CLIs.",
        "interval_key": "THREADKEEPER_SKILL_UPDATE_INTERVAL_S",
    },
)


AGENT_ROLE_NAMES: Final[tuple[str, ...]] = tuple(item["role"] for item in AGENT_ROLES)


def role_metadata(role: str) -> dict[str, str] | None:
    key = (role or "").strip().lower()
    return next((item for item in AGENT_ROLES if item["role"] == key), None)
