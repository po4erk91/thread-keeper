"""Typed output schemas for tools that advertise an MCP ``outputSchema``
and return ``structuredContent``.

Each model is used as the return annotation of its tool so MCPServer derives
the ``outputSchema`` exposed in ``tools/list``. Status and search result builders
keep the legacy human-readable text block alongside the structured JSON;
searches can also attach exact-evidence resource links.

Nested/list members allow extra keys so the snapshot producers can grow
new fields without breaking validation.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SearchHit(BaseModel):
    """Compact provenance and optional exact-evidence references."""

    id: str
    source: str
    snippet: str
    created_at: int
    thread_id: str | None = None
    session_id: str | None = None
    role: str | None = None
    kind: str | None = None
    project_id: str | None = None
    memory_scope: str | None = None
    score: float | None = None
    matched_by: list[str] = Field(default_factory=list)
    resource_uri: str | None = None
    thread_uri: str | None = None


class SearchResults(BaseModel):
    """Legacy search text plus progressive evidence-loading metadata."""

    text: str
    hits: list[SearchHit] = Field(default_factory=list)


class _Lenient(BaseModel):
    """Base for nested rows that may gain fields over time."""

    model_config = ConfigDict(extra="allow")


# --- context -----------------------------------------------------------------
class ContextStatus(BaseModel):
    """Runtime context: session, semantic flag, db path, thread counts."""

    session_id: str | None = None
    started_age_s: int = 0
    semantic: bool = False
    db_path: str = ""
    thread_counts: dict[str, int] = Field(default_factory=dict)
    now: str = ""


# --- spawn_budget_status -----------------------------------------------------
class SpawnTaskRss(_Lenient):
    id: str
    pid: int = 0
    cid: str = "-"
    rss_mb: int = 0
    age_s: int = 0
    prompt: str = ""


class SpawnBudgetStatus(BaseModel):
    """Spawn-budget usage: cap, used, free, plus per-running-task RSS."""

    enabled: bool = True
    cap_mb: int | None = None
    used_mb: int = 0
    free_mb: int | None = None
    token_budget_enabled: bool = False
    token_budget: int | None = None
    tokens_24h: int = 0
    tokens_free: int | None = None
    cost_budget_enabled: bool = False
    cost_budget_usd: float | None = None
    cost_usd_24h: float = 0.0
    cost_free_usd: float | None = None
    running: int = 0
    poll_s: int | None = None
    tasks: list[SpawnTaskRss] = Field(default_factory=list)


# --- spawn_status ------------------------------------------------------------
class CliCapability(_Lenient):
    cli: str
    available: bool = False
    bin: str | None = None
    note: str | None = None
    version: str = ""
    executable: str = ""
    models: list[str] = Field(default_factory=list)
    model_source: str = ""
    source_updated_at: int | None = None
    catalog_refreshed_at: int | None = None
    catalog_age_s: int = 0
    stale: bool = False
    error: str | None = None
    configured_model: str = ""
    configured_effort: str = ""
    effort_options: list[str] = Field(default_factory=list)
    effort_mode: str = "independent"
    effort_note: str = ""


class SpawnRoleResolution(_Lenient):
    role: str
    cli: str
    cli_source: str = ""
    model: str = ""
    model_source: str = ""
    effort: str = ""
    effort_source: str = ""


class SpawnStatus(BaseModel):
    """Detected host CLI plus per-role spawn resolution and capabilities."""

    active_cli: str | None = None
    role_resolution: str = ""
    capabilities: list[CliCapability] = Field(default_factory=list)
    roles: list[SpawnRoleResolution] = Field(default_factory=list)
    catalog_generated_at: int | None = None


# --- mp_health ---------------------------------------------------------------
class MpProcess(_Lenient):
    pid: int
    ppid: int = 0
    parent_alive: bool = False
    rss_kb: int = 0
    heartbeat_age_s: int | None = None
    etime: str = ""
    is_self: bool = False
    is_orphaned: bool = False
    orphan_reason: str | None = None


class MpHealth(BaseModel):
    """Snapshot of every running thread-keeper server process on the host."""

    total: int = 0
    live: int = 0
    orphans: int = 0
    rss_total_mb: int = 0
    processes: list[MpProcess] = Field(default_factory=list)


# --- agent_status ------------------------------------------------------------
class AgentStatusSnapshot(_Lenient):
    """Autonomous learning loops + running children (JSON-ready snapshot)."""

    generated_at: int = 0
    rss_freshness: str = "cached"
    running_count: int = 0
    total_rss_kb: int = 0
    total_rss_mb: int = 0
    enabled_loop_count: int = 0
    running_loop_count: int = 0
    ready_loop_count: int = 0
    loops: list[dict[str, Any]] = Field(default_factory=list)
    github_budget: dict[str, Any] = Field(default_factory=dict)
    recent_results: list[dict[str, Any]] = Field(default_factory=list)
    timed_out_count: int = 0
    agents: list[dict[str, Any]] = Field(default_factory=list)
