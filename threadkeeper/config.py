"""Paths, env-driven defaults, semantic-search availability flag.
Imported wherever a constant or config is needed; cheap to import.

All configuration is read from ~/.threadkeeper/.env (or the file at
THREADKEEPER_ENV_FILE) via pydantic-settings. Real environment variables
override .env values, which override field defaults.

Nested spawn config uses double-underscore notation:
  THREADKEEPER_SPAWN__DEFAULT=claude
  THREADKEEPER_SPAWN__LOOP__SHADOW_OBSERVER=codex
  THREADKEEPER_SPAWN__LOOP__CURATOR=agy
  THREADKEEPER_SPAWN__MODEL__CLAUDE=sonnet
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ── env-file path resolved at module load so THREADKEEPER_ENV_FILE override works ──
_ENV_FILE: str = os.environ.get(
    "THREADKEEPER_ENV_FILE",
    str(Path("~/.threadkeeper/.env").expanduser()),
)


# ── Nested spawn config ──────────────────────────────────────────────────────

class SpawnSettings(BaseModel):
    """Spawn routing config. All keys are lowercased (case_sensitive=False)."""

    default: str = ""           # "" = no pin -> resolve_agent uses the active CLI
    loop: dict[str, str] = {}   # role -> cli
    model: dict[str, str] = {}  # cli/role -> model


# ── Main Settings class ──────────────────────────────────────────────────────

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="THREADKEEPER_",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
    )

    # ── Paths ────────────────────────────────────────────────────────────────
    # env: THREADKEEPER_DB (not THREADKEEPER_DB_PATH — old key is "THREADKEEPER_DB")
    db: Path = Field(
        default=Path("~/.threadkeeper/db.sqlite"),
        validation_alias=AliasChoices("THREADKEEPER_DB", "db"),
    )
    # Unprefixed env names — bypass prefix via AliasChoices
    claude_skills_dir: Path = Field(
        default=Path("~/.claude/skills"),
        validation_alias=AliasChoices(
            "CLAUDE_SKILLS_DIR", "THREADKEEPER_CLAUDE_SKILLS_DIR", "claude_skills_dir"
        ),
    )
    claude_projects_dir: Path = Field(
        default=Path("~/.claude/projects"),
        validation_alias=AliasChoices(
            "CLAUDE_PROJECTS_DIR",
            "THREADKEEPER_CLAUDE_PROJECTS_DIR",
            "claude_projects_dir",
        ),
    )
    task_log_dir: Path = Field(default=Path("/tmp/thread-keeper-tasks"))

    # ── Embeddings ───────────────────────────────────────────────────────────
    # env: THREADKEEPER_EMBED_MODEL (field name: embed_model → EMBED_MODEL_NAME)
    embed_model: str = Field(
        default="paraphrase-multilingual-MiniLM-L12-v2",
        validation_alias=AliasChoices("THREADKEEPER_EMBED_MODEL", "embed_model"),
    )
    embed_backend: str = "onnx"
    no_embeddings: bool = False

    # ── Client / process identity ────────────────────────────────────────────
    client: str = Field(
        default="claude",
        validation_alias=AliasChoices("THREADKEEPER_CLIENT", "client"),
    )
    write_origin: str = "foreground"
    spawned_child: bool = False
    # Hard kill-switch for every background daemon (memory_guard, spawn_budget,
    # search_proxy, ingest, skill_watcher, shadow_review, ...). Independent of
    # each daemon's poll/interval knob so flipping one of those back on (e.g. a
    # test monkeypatching MEMORY_GUARD_POLL_S to exercise status output) cannot
    # accidentally spin up a live thread. Tests set THREADKEEPER_DISABLE_BG_DAEMONS=1.
    disable_bg_daemons: bool = False
    menubar_auto_launch: bool = True

    # ── Auto-update daemon ──────────────────────────────────────────────────
    auto_update_interval_s: float = 86400.0
    auto_update_restart: bool = True
    auto_update_timeout_s: int = 600

    # ── Ingest ───────────────────────────────────────────────────────────────
    # env: THREADKEEPER_INGEST_CAP (not INGEST_CAP_PER_CALL)
    ingest_cap: int = Field(
        default=50,
        validation_alias=AliasChoices("THREADKEEPER_INGEST_CAP", "ingest_cap"),
    )
    ingest_interval_s: float = 3.0
    # env: THREADKEEPER_INGEST_WINDOW_S (not INGEST_RECENT_WINDOW_S)
    ingest_window_s: int = Field(
        default=600,
        validation_alias=AliasChoices(
            "THREADKEEPER_INGEST_WINDOW_S", "ingest_window_s"
        ),
    )

    # ── Identity / session ───────────────────────────────────────────────────
    self_cid_ttl_s: float = 5.0

    # ── Nudges / brief ───────────────────────────────────────────────────────
    memory_nudge_interval: int = 10
    skill_nudge_interval: int = 10
    brief_lean: bool = False
    brief_no_thread_nudge: bool = False

    # ── Spawn budget ─────────────────────────────────────────────────────────
    spawn_budget_mb: int = 3072
    spawn_estimate_slim_mb: int = 500
    spawn_estimate_full_mb: int = 1500
    spawn_budget_poll_s: float = 10.0

    # ── Memory guard ─────────────────────────────────────────────────────────
    memory_guard_poll_s: float = 30.0
    memory_guard_warn_mb: int = 1536
    memory_guard_kill_mb: int = 3072
    memory_guard_agg_warn_mb: int = 2048
    memory_guard_agg_kill_mb: int = 3072
    memory_guard_reclaim_mb: int = 1024
    memory_guard_target_servers: int = 1
    memory_guard_retire_idle_s: int = 900
    memory_guard_retire_live: bool = False
    memory_guard_notify: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "THREADKEEPER_MEMORY_GUARD_NOTIFY", "memory_guard_notify"
        ),
    )
    memory_guard_cooldown_s: int = 300

    # ── Auto-review ──────────────────────────────────────────────────────────
    auto_review: bool = Field(
        default=False,
        validation_alias=AliasChoices("THREADKEEPER_AUTO_REVIEW", "auto_review"),
    )

    # ── Shadow review daemon ─────────────────────────────────────────────────
    shadow_review_interval_s: float = 0.0
    shadow_review_window_s: int = 900
    shadow_review_min_chars: int = 500

    # ── Curator daemon ───────────────────────────────────────────────────────
    curator_interval_s: float = 0.0
    curator_min_lessons: int = 3
    # THREADKEEPER_CURATOR_REPORTS_DIR — default is relative to db dir; computed post-init
    curator_reports_dir: Optional[Path] = None
    # Destructive-by-default: once the curator daemon is enabled
    # (curator_interval_s > 0) the child writes its REPORT, then applies its own
    # PATCH/PRUNE/CONSOLIDATE directly. Set THREADKEEPER_CURATOR_DESTRUCTIVE=0
    # for advisory REPORT-only. [PROTECTED] (foreground/user/pinned/validated)
    # entries are never mutated regardless.
    curator_destructive: bool = True

    # ── Extract daemon ───────────────────────────────────────────────────────
    extract_interval_s: float = 0.0
    extract_window_min: int = 30

    # ── Candidate reviewer daemon ─────────────────────────────────────────────
    candidate_review_interval_s: float = 0.0
    candidate_review_min: int = 3

    # ── Probe daemon ─────────────────────────────────────────────────────────
    probe_interval_s: float = 0.0
    probe_cooldown_s: int = 7 * 86400  # one week

    # ── Judge panel ──────────────────────────────────────────────────────────
    panel_size: int = 3
    panel_roles: list[str] = ["skeptic", "critic", "generator"]
    panel_require_skeptic: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "THREADKEEPER_PANEL_REQUIRE_SKEPTIC", "panel_require_skeptic"
        ),
    )
    panel_vote_weight: float = 1.0
    panel_model: str = ""
    panel_effort: str = ""

    # ── Evolve review daemon ──────────────────────────────────────────────────
    evolve_review_interval_s: float = 0.0
    evolve_review_min: int = 2

    # ── Evolve applier daemon ─────────────────────────────────────────────────
    # Periodically picks the top promoted+unapplied evolve suggestion and fires
    # evolve_apply (spawns a child that implements it + opens a PR). 0 = off.
    evolve_apply_interval_s: float = 0.0
    # After posting a roadmap-issue claim comment, wait this long, re-fetch
    # comments, and retract our claim if another host raced us. Cross-host
    # TOCTOU guard. Set to 0 in tests to skip the wait.
    roadmap_claim_race_window_s: float = 3.0

    # ── Thread janitor daemon ─────────────────────────────────────────────────
    thread_janitor_interval_s: float = 0.0
    thread_idle_close_days: float = 1.0

    # ── Dialectic auto-feed daemons ───────────────────────────────────────────
    dialectic_mine_interval_s: float = 0.0
    dialectic_validate_interval_s: float = 0.0
    dialectic_validate_min: int = 5
    dialectic_validate_batch_size: int = 50
    dialectic_max_new_claims: int = 3

    # ── Nested spawn config ───────────────────────────────────────────────────
    spawn: SpawnSettings = SpawnSettings()

    # ── Validators ───────────────────────────────────────────────────────────

    @field_validator(
        "db",
        "claude_skills_dir",
        "claude_projects_dir",
        "task_log_dir",
        mode="after",
    )
    @classmethod
    def _expand_path(cls, p: Path) -> Path:
        return p.expanduser()

    @field_validator("curator_reports_dir", mode="before")
    @classmethod
    def _parse_curator_dir(cls, v):
        if v is None:
            return None
        return Path(str(v)).expanduser()

    @field_validator("embed_backend", mode="after")
    @classmethod
    def _normalize_backend(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("panel_roles", mode="before")
    @classmethod
    def _parse_panel_roles(cls, v):
        """Accept comma-separated string or list."""
        if isinstance(v, str):
            return [r.strip() for r in v.split(",") if r.strip()]
        return v


# ── Instantiate ──────────────────────────────────────────────────────────────

settings = Settings()

# ── Derived paths that depend on db field ────────────────────────────────────

# CURATOR_REPORTS_DIR: if not explicitly set, anchor to DB_PATH.parent/curator
# so a custom THREADKEEPER_DB co-locates its curator reports.
_curator_reports_dir: Path = (
    settings.curator_reports_dir
    if settings.curator_reports_dir is not None
    else (settings.db.parent / "curator")
)


# ── Compat shim: re-export all prior module-level names ──────────────────────
# Every name listed here is imported by ≥1 call site in the package.

DB_PATH: Path = settings.db
EMBED_MODEL_NAME: str = settings.embed_model
EMBED_BACKEND: str = settings.embed_backend  # already normalized to lower
NO_EMBEDDINGS: bool = settings.no_embeddings
CLIENT_LABEL: str = settings.client
WRITE_ORIGIN: str = settings.write_origin
SPAWNED_CHILD: bool = settings.spawned_child
DISABLE_BG_DAEMONS: bool = settings.disable_bg_daemons
MENUBAR_AUTO_LAUNCH: bool = settings.menubar_auto_launch
AUTO_UPDATE_INTERVAL_S: float = settings.auto_update_interval_s
AUTO_UPDATE_RESTART: bool = settings.auto_update_restart
AUTO_UPDATE_TIMEOUT_S: int = settings.auto_update_timeout_s
CLAUDE_SKILLS_DIR: Path = settings.claude_skills_dir
CLAUDE_PROJECTS_DIR: Path = settings.claude_projects_dir
TASK_LOG_DIR: Path = settings.task_log_dir
DIALOG_LOG: Path = TASK_LOG_DIR / "dialog.log"
INGEST_CAP_PER_CALL: int = settings.ingest_cap
INGEST_INTERVAL_S: float = settings.ingest_interval_s
INGEST_RECENT_WINDOW_S: int = settings.ingest_window_s
SELF_CID_TTL_S: float = settings.self_cid_ttl_s
MEMORY_NUDGE_INTERVAL: int = settings.memory_nudge_interval
SKILL_NUDGE_INTERVAL: int = settings.skill_nudge_interval
BRIEF_LEAN: bool = settings.brief_lean
BRIEF_NO_THREAD_NUDGE: bool = settings.brief_no_thread_nudge
AUTO_REVIEW_ENABLED: bool = settings.auto_review
SPAWN_BUDGET_MB: int = settings.spawn_budget_mb
SPAWN_ESTIMATE_SLIM_MB: int = settings.spawn_estimate_slim_mb
SPAWN_ESTIMATE_FULL_MB: int = settings.spawn_estimate_full_mb
SPAWN_BUDGET_POLL_S: float = settings.spawn_budget_poll_s
MEMORY_GUARD_POLL_S: float = settings.memory_guard_poll_s
MEMORY_GUARD_WARN_MB: int = settings.memory_guard_warn_mb
MEMORY_GUARD_KILL_MB: int = settings.memory_guard_kill_mb
MEMORY_GUARD_AGG_WARN_MB: int = settings.memory_guard_agg_warn_mb
MEMORY_GUARD_AGG_KILL_MB: int = settings.memory_guard_agg_kill_mb
MEMORY_GUARD_RECLAIM_MB: int = settings.memory_guard_reclaim_mb
MEMORY_GUARD_TARGET_SERVERS: int = settings.memory_guard_target_servers
MEMORY_GUARD_RETIRE_IDLE_S: int = settings.memory_guard_retire_idle_s
MEMORY_GUARD_RETIRE_LIVE: bool = settings.memory_guard_retire_live
MEMORY_GUARD_NOTIFY: bool = settings.memory_guard_notify
MEMORY_GUARD_COOLDOWN_S: int = settings.memory_guard_cooldown_s
SHADOW_REVIEW_INTERVAL_S: float = settings.shadow_review_interval_s
SHADOW_REVIEW_WINDOW_S: int = settings.shadow_review_window_s
SHADOW_REVIEW_MIN_CHARS: int = settings.shadow_review_min_chars
CURATOR_INTERVAL_S: float = settings.curator_interval_s
CURATOR_MIN_LESSONS: int = settings.curator_min_lessons
CURATOR_REPORTS_DIR: Path = _curator_reports_dir
CURATOR_DESTRUCTIVE: bool = settings.curator_destructive
EXTRACT_INTERVAL_S: float = settings.extract_interval_s
EXTRACT_WINDOW_MIN: int = settings.extract_window_min
CANDIDATE_REVIEW_INTERVAL_S: float = settings.candidate_review_interval_s
CANDIDATE_REVIEW_MIN: int = settings.candidate_review_min
PROBE_INTERVAL_S: float = settings.probe_interval_s
PROBE_COOLDOWN_S: int = settings.probe_cooldown_s
PANEL_SIZE: int = settings.panel_size
PANEL_ROLES: list[str] = settings.panel_roles
PANEL_REQUIRE_SKEPTIC: bool = settings.panel_require_skeptic
PANEL_VOTE_WEIGHT: float = settings.panel_vote_weight
PANEL_MODEL: str = settings.panel_model
PANEL_EFFORT: str = settings.panel_effort
EVOLVE_REVIEW_INTERVAL_S: float = settings.evolve_review_interval_s
EVOLVE_REVIEW_MIN: int = settings.evolve_review_min
EVOLVE_APPLY_INTERVAL_S: float = settings.evolve_apply_interval_s
ROADMAP_CLAIM_RACE_WINDOW_S: float = settings.roadmap_claim_race_window_s
THREAD_JANITOR_INTERVAL_S: float = settings.thread_janitor_interval_s
THREAD_IDLE_CLOSE_DAYS: float = settings.thread_idle_close_days
DIALECTIC_MINE_INTERVAL_S: float = settings.dialectic_mine_interval_s
DIALECTIC_VALIDATE_INTERVAL_S: float = settings.dialectic_validate_interval_s
DIALECTIC_VALIDATE_MIN: int = settings.dialectic_validate_min
DIALECTIC_VALIDATE_BATCH_SIZE: int = settings.dialectic_validate_batch_size
DIALECTIC_MAX_NEW_CLAIMS: int = settings.dialectic_max_new_claims

# ── Derived constants (unchanged logic, computed after settings) ──────────────

# fastembed addresses the model under its sentence-transformers org prefix;
# SentenceTransformer accepts the bare name. Normalize for the ONNX backend.
FASTEMBED_MODEL_ID: str = (
    EMBED_MODEL_NAME
    if "/" in EMBED_MODEL_NAME
    else f"sentence-transformers/{EMBED_MODEL_NAME}"
)


def _installed(*mods: str) -> bool:
    """True if every module is importable, checked WITHOUT importing it.

    `find_spec` locates the module via the import machinery but never executes
    it — so probing availability here doesn't pull PyTorch / ONNX Runtime /
    tokenizers (and their thread pools) into every process that imports config.
    The heavy import stays lazy in `embeddings._get_model()`.
    """
    try:
        return all(importlib.util.find_spec(m) is not None for m in mods)
    except (ImportError, ValueError):
        return False


if NO_EMBEDDINGS:
    SEMANTIC_AVAILABLE: bool = False
elif EMBED_BACKEND == "sentence-transformers":
    SEMANTIC_AVAILABLE = _installed("sentence_transformers", "numpy")
else:  # 'onnx' (default)
    SEMANTIC_AVAILABLE = _installed("fastembed", "numpy")

# Autonomous daemons may spawn children or do expensive background work. They
# should run only in user-facing parent processes, never inside spawned review
# children, where they can recurse.
BACKGROUND_DAEMONS_ALLOWED: bool = (
    not SPAWNED_CHILD and WRITE_ORIGIN == "foreground" and not DISABLE_BG_DAEMONS
)

# ── DB-path setup + legacy migration ─────────────────────────────────────────

DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# One-shot migration from the historical name `memory_partner`. If the new
# DB doesn't exist yet but the legacy one does, copy it (including the WAL
# sidecars) so users can rename mid-life without losing memory. After this
# import the legacy directory is left in place — caller can `rm -rf` once
# they've verified the new path is working.
#
# Gate: only run when DB_PATH is the default `~/.threadkeeper/db.sqlite`.
# Tests + custom paths must NOT trigger the migration — otherwise every
# test would copy the user's ~683MB DB into its tmp dir and exhaust disk.
_DEFAULT_DB = Path("~/.threadkeeper/db.sqlite").expanduser()
_LEGACY_DIR = Path("~/.memory_partner").expanduser()
_LEGACY_DB = _LEGACY_DIR / "db.sqlite"
if (
    DB_PATH == _DEFAULT_DB
    and not DB_PATH.exists()
    and _LEGACY_DB.exists()
):
    import shutil
    for fname in ("db.sqlite", "db.sqlite-wal", "db.sqlite-shm"):
        src = _LEGACY_DIR / fname
        if src.exists():
            shutil.copy2(src, DB_PATH.parent / fname)
