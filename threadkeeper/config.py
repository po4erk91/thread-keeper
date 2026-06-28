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
import logging
import os
from pathlib import Path
from typing import Annotated, Optional

from pydantic import AliasChoices, BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger(__name__)

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
    # Vector width the vec0 (`notes_vec`/`dialog_vec`) tables are CREATEd with.
    # Defaults to 384 (paraphrase-multilingual-MiniLM-L12-v2). When swapping in
    # a model of a different dimension via THREADKEEPER_EMBED_MODEL, set this to
    # the new width AND drop & recreate the *_vec tables, otherwise every vec0
    # insert mismatches FLOAT[384] and the fast KNN path silently goes dead (the
    # legacy BLOB cosine path keeps working). See embeddings._vec_dim_ok, which
    # warns loudly on a width mismatch instead of swallowing it.
    embed_dim: int = Field(
        default=384,
        validation_alias=AliasChoices("THREADKEEPER_EMBED_DIM", "embed_dim"),
    )
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
    auto_update_verify_provenance: bool = True
    auto_update_pypi_base_url: str = "https://pypi.org"
    auto_update_expected_publisher_repository: str = "po4erk91/thread-keeper"
    auto_update_expected_publisher_workflow: str = "publish.yml"
    auto_update_expected_publisher_environment: str = "pypi"

    # ── Skill update daemon ─────────────────────────────────────────────────
    # Twice weekly by default. It syncs installed skills across known CLI roots
    # and can update GitHub-backed skills from configured source roots.
    skill_update_interval_s: float = 7 * 86400 / 2
    skill_update_timeout_s: int = 300
    # Comma-separated specs: owner/repo@ref:path/to/skills/root. The default
    # covers Codex's curated skill installer source; exact source metadata in a
    # skill directory is always honored even when this list is empty.
    skill_update_sources: str = "openai/skills@main:skills/.curated"
    skill_update_infer_sources: bool = True
    # Safety default: an untracked local skill that merely shares an upstream
    # name is adopted only when its tree already matches that upstream. Set this
    # to 1 to let the daemon overwrite inferred, untracked older copies.
    skill_update_allow_untracked_overwrite: bool = False

    # ── Hot-config reload (config_watcher daemon) ────────────────────────────
    # Poll interval for the watcher that re-reads ~/.claude/settings.json and
    # hot-reloads threadkeeper config in-process (no Claude Code restart). The
    # poll is a single mtime stat — cheap, so it defaults ON. 0 = off.
    config_watch_interval_s: float = 2.0
    # Override the watched file. "" => the host CLI's settings.json
    # (~/.claude/settings.json). Tests point this at a tmp file.
    config_watch_path: str = ""

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
    # Optional spend guardrails for spawned children. 0 = disabled (default,
    # preserving existing behavior). The admission path compares recorded
    # spend/tokens over the last 24h against these ceilings before starting a
    # new child.
    spawn_token_budget: int = 0
    spawn_cost_budget_usd: float = 0.0
    # Wall-clock backstop for visible (pid=0) children: a Terminal-launched
    # row whose process can't be resolved from its cid is marked ended once it
    # outlives this, so an unresolvable row can't pin budget capacity forever
    # (#64). 0 disables the reaper.
    spawn_visible_ttl_s: float = 3600.0
    # Wall-clock lifetime cap for any spawned child (#80). A child that hangs
    # while still alive — a wedged WebFetch/gh/git, an agent loop that never
    # converges, a prompt that never arrives — would otherwise stall its loop's
    # single-flight slot and burn tokens forever. The budget daemon SIGTERMs a
    # pid>0 child whose row has outlived this, then SIGKILLs it after
    # SPAWN_KILL_GRACE_S, and marks the row ended with return_code 124 (the
    # timeout(1) convention) so the loop's single-flight releases. Generous
    # default so legitimate long runs aren't cut; 0 disables (no surprise kills
    # on upgrade).
    spawn_max_runtime_s: float = 3600.0
    # Grace between the SIGTERM and the SIGKILL of a timed-out child (#80).
    spawn_kill_grace_s: float = 10.0

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

    # ── Cross-provider memory egress (issue #74) ─────────────────────────────
    # Which sensitivity classes may render into a brief() consumed by a
    # third-party LLM vendor. Personal-class memory (verbatim quotes + dialectic
    # user-model) egresses to whatever vendor backs the active/spawned CLI.
    #   all          (default) — current behavior, no gating, egress everywhere
    #   same-vendor  — personal renders only for the native vendor (Anthropic /
    #                  Claude); omitted for OpenAI / Google / Microsoft
    #   work-only    — personal never renders, for any vendor
    # Unknown values normalize to `all` (fail-open — don't regress the product).
    memory_egress: str = Field(
        default="all",
        validation_alias=AliasChoices("THREADKEEPER_MEMORY_EGRESS", "memory_egress"),
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
    # Absolute path to the thread-keeper git checkout the evolve reviewer and
    # applier operate on (branch, run tests, open PRs against). Empty => resolve
    # automatically: the package's parent dir when it is itself a checkout (the
    # editable-from-checkout install.sh), else a managed checkout under the DB
    # dir that is auto-cloned on first use (PyPI/site-packages installs). Set
    # this to pin an explicit checkout and skip auto-provisioning.
    evolve_repo_root: str = ""
    # Auto-provision (git clone + .venv with test deps) a managed checkout when
    # thread-keeper is installed without a source tree. ON by default so the
    # evolve loops work out of the box; set 0/false to disable — then the loops
    # require an editable install or an explicit EVOLVE_REPO_ROOT.
    evolve_auto_clone: bool = True
    # Canonical repo the managed checkout is cloned from, and the branch it
    # tracks. Defaults to the upstream thread-keeper project.
    evolve_repo_url: str = "https://github.com/po4erk91/thread-keeper"
    evolve_repo_branch: str = "main"
    # After posting a roadmap-issue claim comment, wait this long, re-fetch
    # comments, and retract our claim if another host raced us. Cross-host
    # TOCTOU guard. Set to 0 in tests to skip the wait.
    roadmap_claim_race_window_s: float = 3.0
    # Author-trust gate for autonomous GitHub-issue pickup (issue #63). This
    # repo is public, so any account can open an issue whose body is then
    # injected into a permission-bypassing implementer child. Auto-pickup is
    # limited to issues whose GitHub author association is in this set;
    # everything else needs explicit human promotion (a trust label below, or
    # invoking the applier on the exact issue number). CSV string or list.
    # NoDecode: keep a raw env string out of pydantic-settings' JSON decoder so
    # the CSV validator below handles it.
    evolve_trusted_author_associations: Annotated[list[str], NoDecode] = [
        "OWNER", "MEMBER", "COLLABORATOR",
    ]
    # Optional escape hatch for the author gate: issues carrying any of these
    # labels are eligible for auto-pickup regardless of author association. On
    # a public repo only collaborators can apply labels, so a trust label is
    # itself a maintainer endorsement. Empty by default — association is the
    # sole gate. CSV string or list.
    evolve_trust_labels: Annotated[list[str], NoDecode] = []
    # Poison-issue guard for the evolve applier. After an implementer child is
    # spawned for a roadmap issue but no PR results, the issue stays selectable
    # once its 24h claim TTL lapses. Without a cap that re-spawns a costly
    # bypassPermissions child every ~24h forever. So each spawn records an
    # attempt; an escalating backoff (base * 2^(attempts-1)) defers re-selection,
    # and after this many attempts the issue is dead-lettered: a `blocked` label
    # is applied and it drops out of auto-selection until a human intervenes.
    roadmap_issue_max_attempts: int = 3
    # Base backoff window after the first failed attempt (seconds). The window
    # doubles per attempt. Default 2 days so it always exceeds the 24h claim TTL.
    roadmap_issue_backoff_base_s: float = 172800.0

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

    @field_validator("memory_egress", mode="after")
    @classmethod
    def _normalize_egress(cls, v: str) -> str:
        # Light tidy only (lower/strip); canonicalization + alias mapping +
        # fail-open fallback live in egress.normalize_policy so the policy
        # vocabulary has a single owner and config stays import-cycle free.
        return v.strip().lower()

    @field_validator("panel_roles", mode="before")
    @classmethod
    def _parse_panel_roles(cls, v):
        """Accept comma-separated string or list."""
        if isinstance(v, str):
            return [r.strip() for r in v.split(",") if r.strip()]
        return v

    @field_validator("evolve_trusted_author_associations", mode="before")
    @classmethod
    def _parse_trusted_assocs(cls, v):
        """Accept CSV string or list; normalize to UPPER (GitHub's casing)."""
        if isinstance(v, str):
            v = [a for a in v.split(",")]
        return [str(a).strip().upper() for a in (v or []) if str(a).strip()]

    @field_validator("evolve_trust_labels", mode="before")
    @classmethod
    def _parse_trust_labels(cls, v):
        """Accept CSV string or list; normalize to lower (case-insensitive)."""
        if isinstance(v, str):
            v = [a for a in v.split(",")]
        return [str(a).strip().lower() for a in (v or []) if str(a).strip()]


_EXTRA_THREADKEEPER_ENV_KEYS = {
    # Consumed before Settings instantiation or by hook/app helper code.
    "THREADKEEPER_ACTIVE_CLI",
    "THREADKEEPER_AGENT_STATUS_COMMAND",
    "THREADKEEPER_EGRESS_CONSUMER",
    "THREADKEEPER_ENV_FILE",
    "THREADKEEPER_EXTRA_SKILLS_DIRS",
    "THREADKEEPER_FORCE_CID",
    "THREADKEEPER_LESSONS",
    "THREADKEEPER_MENUBAR_RESTART_RSS_MB",
    "THREADKEEPER_PYTHON",
    "THREADKEEPER_REPO",
    "THREADKEEPER_SEARCH_PROXY_POLL_S",
    "THREADKEEPER_SKILL_WATCH_INTERVAL_S",
    "THREADKEEPER_STATE_DIR",
    "THREADKEEPER_TZ",
    "THREADKEEPER_VISIBLE_STATUS",
}

_THREADKEEPER_NESTED_ENV_KEYS = {
    "THREADKEEPER_SPAWN__DEFAULT",
    "THREADKEEPER_SPAWN__LOOP",
    "THREADKEEPER_SPAWN__MODEL",
}
_THREADKEEPER_NESTED_ENV_PREFIXES = (
    "THREADKEEPER_SPAWN__LOOP__",
    "THREADKEEPER_SPAWN__MODEL__",
)


def _alias_strings(validation_alias) -> list[str]:
    if validation_alias is None:
        return []
    if isinstance(validation_alias, str):
        return [validation_alias]
    choices = getattr(validation_alias, "choices", None)
    if choices is None:
        return []
    return [choice for choice in choices if isinstance(choice, str)]


def _known_threadkeeper_env_keys() -> set[str]:
    known = set(_EXTRA_THREADKEEPER_ENV_KEYS)
    for name, field in Settings.model_fields.items():
        known.add(f"THREADKEEPER_{name.upper()}")
        for alias in _alias_strings(field.validation_alias):
            if alias.upper().startswith("THREADKEEPER_"):
                known.add(alias.upper())
    known.update(_THREADKEEPER_NESTED_ENV_KEYS)
    return known


def unknown_threadkeeper_env_keys(environ: Optional[dict] = None) -> list[str]:
    """Return THREADKEEPER_* process-env keys that Settings will not consume."""
    env = os.environ if environ is None else environ
    known = _known_threadkeeper_env_keys()
    unknown = []
    for key in env:
        upper = key.upper()
        if not upper.startswith("THREADKEEPER_"):
            continue
        if upper in known:
            continue
        if any(
            upper.startswith(prefix)
            for prefix in _THREADKEEPER_NESTED_ENV_PREFIXES
        ):
            continue
        unknown.append(key)
    return sorted(unknown, key=str.upper)


def _warn_unknown_threadkeeper_env_keys() -> None:
    keys = unknown_threadkeeper_env_keys()
    if keys:
        logger.warning(
            "Ignoring unknown THREADKEEPER_* env key(s): %s",
            ", ".join(keys),
        )


# ── Instantiate ──────────────────────────────────────────────────────────────

settings = Settings()
_warn_unknown_threadkeeper_env_keys()


# ── Compat shim: re-export all prior module-level names ──────────────────────
# Every name listed here is imported by ≥1 call site in the package. They are
# computed from `settings` by `_derive_constants` so that `reload_settings()`
# (the hot-config-reload path, issue #2) can recompute and re-publish them in
# place without a process restart. Consumers that did `from .config import X`
# still see updates because `reload_settings` propagates the new value into
# every loaded `threadkeeper.*` module that holds a copy (see `_propagate`).


def _derive_constants(s: "Settings") -> dict:
    """Map a Settings instance to the package's UPPER_CASE constants.

    Pure: no side effects, no globals touched. Used once at import and again
    on every `reload_settings()`. Only operational knobs live here — identity
    flags (SEMANTIC_AVAILABLE, BACKGROUND_DAEMONS_ALLOWED) and the embedding
    backend are computed once below and are NOT hot-reloaded.
    """
    # CURATOR_REPORTS_DIR: if not explicitly set, anchor to DB_PATH.parent/curator
    # so a custom THREADKEEPER_DB co-locates its curator reports.
    curator_reports_dir = (
        s.curator_reports_dir
        if s.curator_reports_dir is not None
        else (s.db.parent / "curator")
    )
    return {
        "DB_PATH": s.db,
        "EMBED_MODEL_NAME": s.embed_model,
        "EMBED_BACKEND": s.embed_backend,  # already normalized to lower
        "NO_EMBEDDINGS": s.no_embeddings,
        "CLIENT_LABEL": s.client,
        "WRITE_ORIGIN": s.write_origin,
        "SPAWNED_CHILD": s.spawned_child,
        "DISABLE_BG_DAEMONS": s.disable_bg_daemons,
        "MENUBAR_AUTO_LAUNCH": s.menubar_auto_launch,
        "AUTO_UPDATE_INTERVAL_S": s.auto_update_interval_s,
        "AUTO_UPDATE_RESTART": s.auto_update_restart,
        "AUTO_UPDATE_TIMEOUT_S": s.auto_update_timeout_s,
        "SKILL_UPDATE_INTERVAL_S": s.skill_update_interval_s,
        "SKILL_UPDATE_TIMEOUT_S": s.skill_update_timeout_s,
        "AUTO_UPDATE_VERIFY_PROVENANCE": s.auto_update_verify_provenance,
        "AUTO_UPDATE_PYPI_BASE_URL": s.auto_update_pypi_base_url,
        "AUTO_UPDATE_EXPECTED_PUBLISHER_REPOSITORY": (
            s.auto_update_expected_publisher_repository
        ),
        "AUTO_UPDATE_EXPECTED_PUBLISHER_WORKFLOW": (
            s.auto_update_expected_publisher_workflow
        ),
        "AUTO_UPDATE_EXPECTED_PUBLISHER_ENVIRONMENT": (
            s.auto_update_expected_publisher_environment
        ),
        "SKILL_UPDATE_SOURCES": s.skill_update_sources,
        "SKILL_UPDATE_INFER_SOURCES": s.skill_update_infer_sources,
        "SKILL_UPDATE_ALLOW_UNTRACKED_OVERWRITE": (
            s.skill_update_allow_untracked_overwrite
        ),
        "CONFIG_WATCH_INTERVAL_S": s.config_watch_interval_s,
        "CONFIG_WATCH_PATH": s.config_watch_path,
        "CLAUDE_SKILLS_DIR": s.claude_skills_dir,
        "CLAUDE_PROJECTS_DIR": s.claude_projects_dir,
        "TASK_LOG_DIR": s.task_log_dir,
        "DIALOG_LOG": s.task_log_dir / "dialog.log",
        "INGEST_CAP_PER_CALL": s.ingest_cap,
        "INGEST_INTERVAL_S": s.ingest_interval_s,
        "INGEST_RECENT_WINDOW_S": s.ingest_window_s,
        "SELF_CID_TTL_S": s.self_cid_ttl_s,
        "MEMORY_NUDGE_INTERVAL": s.memory_nudge_interval,
        "SKILL_NUDGE_INTERVAL": s.skill_nudge_interval,
        "BRIEF_LEAN": s.brief_lean,
        "BRIEF_NO_THREAD_NUDGE": s.brief_no_thread_nudge,
        "AUTO_REVIEW_ENABLED": s.auto_review,
        "MEMORY_EGRESS": s.memory_egress,
        "SPAWN_BUDGET_MB": s.spawn_budget_mb,
        "SPAWN_ESTIMATE_SLIM_MB": s.spawn_estimate_slim_mb,
        "SPAWN_ESTIMATE_FULL_MB": s.spawn_estimate_full_mb,
        "SPAWN_BUDGET_POLL_S": s.spawn_budget_poll_s,
        "SPAWN_TOKEN_BUDGET": s.spawn_token_budget,
        "SPAWN_COST_BUDGET_USD": s.spawn_cost_budget_usd,
        "SPAWN_VISIBLE_TTL_S": s.spawn_visible_ttl_s,
        "SPAWN_MAX_RUNTIME_S": s.spawn_max_runtime_s,
        "SPAWN_KILL_GRACE_S": s.spawn_kill_grace_s,
        "MEMORY_GUARD_POLL_S": s.memory_guard_poll_s,
        "MEMORY_GUARD_WARN_MB": s.memory_guard_warn_mb,
        "MEMORY_GUARD_KILL_MB": s.memory_guard_kill_mb,
        "MEMORY_GUARD_AGG_WARN_MB": s.memory_guard_agg_warn_mb,
        "MEMORY_GUARD_AGG_KILL_MB": s.memory_guard_agg_kill_mb,
        "MEMORY_GUARD_RECLAIM_MB": s.memory_guard_reclaim_mb,
        "MEMORY_GUARD_TARGET_SERVERS": s.memory_guard_target_servers,
        "MEMORY_GUARD_RETIRE_IDLE_S": s.memory_guard_retire_idle_s,
        "MEMORY_GUARD_RETIRE_LIVE": s.memory_guard_retire_live,
        "MEMORY_GUARD_NOTIFY": s.memory_guard_notify,
        "MEMORY_GUARD_COOLDOWN_S": s.memory_guard_cooldown_s,
        "SHADOW_REVIEW_INTERVAL_S": s.shadow_review_interval_s,
        "SHADOW_REVIEW_WINDOW_S": s.shadow_review_window_s,
        "SHADOW_REVIEW_MIN_CHARS": s.shadow_review_min_chars,
        "CURATOR_INTERVAL_S": s.curator_interval_s,
        "CURATOR_MIN_LESSONS": s.curator_min_lessons,
        "CURATOR_REPORTS_DIR": curator_reports_dir,
        "CURATOR_DESTRUCTIVE": s.curator_destructive,
        "EXTRACT_INTERVAL_S": s.extract_interval_s,
        "EXTRACT_WINDOW_MIN": s.extract_window_min,
        "CANDIDATE_REVIEW_INTERVAL_S": s.candidate_review_interval_s,
        "CANDIDATE_REVIEW_MIN": s.candidate_review_min,
        "PROBE_INTERVAL_S": s.probe_interval_s,
        "PROBE_COOLDOWN_S": s.probe_cooldown_s,
        "PANEL_SIZE": s.panel_size,
        "PANEL_ROLES": s.panel_roles,
        "PANEL_REQUIRE_SKEPTIC": s.panel_require_skeptic,
        "PANEL_VOTE_WEIGHT": s.panel_vote_weight,
        "PANEL_MODEL": s.panel_model,
        "PANEL_EFFORT": s.panel_effort,
        "EVOLVE_REVIEW_INTERVAL_S": s.evolve_review_interval_s,
        "EVOLVE_REVIEW_MIN": s.evolve_review_min,
        "EVOLVE_APPLY_INTERVAL_S": s.evolve_apply_interval_s,
        "EVOLVE_REPO_ROOT": s.evolve_repo_root,
        "EVOLVE_AUTO_CLONE": s.evolve_auto_clone,
        "EVOLVE_REPO_URL": s.evolve_repo_url,
        "EVOLVE_REPO_BRANCH": s.evolve_repo_branch,
        "ROADMAP_CLAIM_RACE_WINDOW_S": s.roadmap_claim_race_window_s,
        "EVOLVE_TRUSTED_AUTHOR_ASSOCIATIONS": (
            s.evolve_trusted_author_associations
        ),
        "EVOLVE_TRUST_LABELS": s.evolve_trust_labels,
        "ROADMAP_ISSUE_MAX_ATTEMPTS": s.roadmap_issue_max_attempts,
        "ROADMAP_ISSUE_BACKOFF_BASE_S": s.roadmap_issue_backoff_base_s,
        "THREAD_JANITOR_INTERVAL_S": s.thread_janitor_interval_s,
        "THREAD_IDLE_CLOSE_DAYS": s.thread_idle_close_days,
        "DIALECTIC_MINE_INTERVAL_S": s.dialectic_mine_interval_s,
        "DIALECTIC_VALIDATE_INTERVAL_S": s.dialectic_validate_interval_s,
        "DIALECTIC_VALIDATE_MIN": s.dialectic_validate_min,
        "DIALECTIC_VALIDATE_BATCH_SIZE": s.dialectic_validate_batch_size,
        "DIALECTIC_MAX_NEW_CLAIMS": s.dialectic_max_new_claims,
    }


# Publish the initial constants into this module's namespace.
globals().update(_derive_constants(settings))


def _propagate(new_values: dict) -> None:
    """Push reloaded constant values into every loaded `threadkeeper.*` module
    that imported a copy via `from .config import X`.

    This is what makes hot-reload reach consumers. A function reading a
    module-global name resolves it against that module's `__dict__` at call
    time, so overwriting the copy makes the next daemon tick / tool call see
    the new value — no re-import needed. Only names a module already defines
    are touched; we never inject new globals into unrelated modules.
    """
    import sys as _sys

    me = _sys.modules.get(__name__)
    for mod_name, mod in list(_sys.modules.items()):
        if mod is me or mod is None:
            continue
        if not mod_name.startswith("threadkeeper"):
            continue
        d = getattr(mod, "__dict__", None)
        if not d:
            continue
        for cname, val in new_values.items():
            if cname in d:
                d[cname] = val


def reload_settings(env: Optional[dict] = None,
                    remove: Optional[list] = None) -> dict:
    """Re-read configuration in place (hot-config reload — issue #2).

    Steps:
      1. Optionally mutate `os.environ`: drop `remove` keys, set `env` keys.
         (The config_watcher uses this to mirror ~/.claude/settings.json.)
      2. Re-instantiate `Settings()` (re-reads os.environ + the .env file).
      3. Recompute the UPPER_CASE constants and republish them on this module.
      4. Propagate every CHANGED constant to all loaded `threadkeeper.*`
         modules so daemons/tools that imported a copy observe the new value.

    Returns a dict of changed constants: ``{NAME: {"old": ..., "new": ...}}``.
    Embedding availability / process-identity flags are intentionally NOT
    reloaded (hot-swapping the embedding backend in a live process is unsafe).
    """
    global settings
    if remove:
        for k in remove:
            os.environ.pop(k, None)
    if env:
        for k, v in env.items():
            os.environ[k] = str(v)

    old = _derive_constants(settings)
    settings = Settings()
    _warn_unknown_threadkeeper_env_keys()
    new = _derive_constants(settings)

    globals().update(new)
    changed = {
        name: {"old": old.get(name), "new": val}
        for name, val in new.items()
        if old.get(name) != val
    }
    if changed:
        _propagate({name: c["new"] for name, c in changed.items()})
    return changed

# ── Derived constants (unchanged logic, computed after settings) ──────────────

# fastembed addresses the model under its sentence-transformers org prefix;
# SentenceTransformer accepts the bare name. Normalize for the ONNX backend.
FASTEMBED_MODEL_ID: str = (
    EMBED_MODEL_NAME
    if "/" in EMBED_MODEL_NAME
    else f"sentence-transformers/{EMBED_MODEL_NAME}"
)

# Embedding dimension the vec0 virtual tables are created with. Computed once
# here (NOT via _derive_constants) because, like the embedding backend, it is
# baked into already-created vec0 schema — hot-swapping it in a live process
# would desync the tables from the data. Override with THREADKEEPER_EMBED_DIM.
EMBED_DIM: int = int(settings.embed_dim)


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
