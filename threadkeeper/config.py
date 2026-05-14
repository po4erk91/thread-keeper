"""Paths, env-driven defaults, semantic-search availability flag.
Imported wherever a constant or config is needed; cheap to import."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

DB_PATH: Path = Path(
    os.environ.get("THREADKEEPER_DB", "~/.threadkeeper/db.sqlite")
).expanduser()

EMBED_MODEL_NAME: str = os.environ.get(
    "THREADKEEPER_EMBED_MODEL",
    "paraphrase-multilingual-MiniLM-L12-v2",  # 118 MB, RU+EN cross-lingual
)

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

# Semantic search opt-out. When this process is light (spawned slim child that
# should never load PyTorch/transformers), set THREADKEEPER_NO_EMBEDDINGS=1.
# This process will then delegate semantic queries to a peer via the signals
# channel (search_via_parent). Notes still get inserted with embedding=NULL;
# a parent process with embeddings backfills them asynchronously.
NO_EMBEDDINGS: bool = os.environ.get(
    "THREADKEEPER_NO_EMBEDDINGS", ""
).lower() in {"1", "true", "yes", "on"}

# Optional semantic search. If sentence-transformers is not installed OR the
# no-embeddings opt-out is set, fall back to FTS5 keyword matching + delegate.
# Brief still works either way.
if NO_EMBEDDINGS:
    SEMANTIC_AVAILABLE: bool = False
else:
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore  # noqa: F401
        import numpy as np  # type: ignore  # noqa: F401
        SEMANTIC_AVAILABLE = True
    except Exception:
        SEMANTIC_AVAILABLE = False

# Client label used for `presence`/`sessions` rows.
CLIENT_LABEL: str = os.environ.get("THREADKEEPER_CLIENT", "claude")

# Write-origin for this server process. 'foreground' = a regular user-facing
# conversation; 'background_review' = a headless review fork spawned to
# auto-curate memory/skills after a complex task. Curator only ever touches
# skills created under 'background_review' so user-authored skills are safe.
WRITE_ORIGIN: str = os.environ.get(
    "THREADKEEPER_WRITE_ORIGIN", "foreground"
)

# Where Claude's user-local skills live. Used by skill_manage / curator.
CLAUDE_SKILLS_DIR: Path = Path(
    os.environ.get("CLAUDE_SKILLS_DIR", "~/.claude/skills")
).expanduser()

# Where the live ingester reads claude code transcripts from.
CLAUDE_PROJECTS_DIR: Path = Path(
    os.environ.get("CLAUDE_PROJECTS_DIR", "~/.claude/projects")
).expanduser()

# Per-session ingest cap so brief() at session start doesn't block.
INGEST_CAP_PER_CALL: int = int(os.environ.get("THREADKEEPER_INGEST_CAP", "50"))

# Background live-ingester tick (seconds). 0 disables.
INGEST_INTERVAL_S: float = float(
    os.environ.get("THREADKEEPER_INGEST_INTERVAL_S", "3")
)
INGEST_RECENT_WINDOW_S: int = int(
    os.environ.get("THREADKEEPER_INGEST_WINDOW_S", "600")
)

# Self-cid heuristic cache TTL (only matters when ppid walk fails).
SELF_CID_TTL_S: float = float(
    os.environ.get("THREADKEEPER_SELF_CID_TTL_S", "5")
)

# Per-task log directory for spawned children.
TASK_LOG_DIR: Path = Path(
    os.environ.get("THREADKEEPER_TASK_LOG_DIR", "/tmp/thread-keeper-tasks")
).expanduser()
DIALOG_LOG: Path = TASK_LOG_DIR / "dialog.log"

# Counter-driven nudge thresholds. Memory nudge fires when N mutating events
# have passed since the last memory_save event in this session; skill nudge
# fires after N events since the last skill_materialized event. 0 disables.
MEMORY_NUDGE_INTERVAL: int = int(
    os.environ.get("THREADKEEPER_MEMORY_NUDGE_INTERVAL", "10")
)
SKILL_NUDGE_INTERVAL: int = int(
    os.environ.get("THREADKEEPER_SKILL_NUDGE_INTERVAL", "10")
)
# When true, review_thread(thread_id) automatically spawns a background fork
# for rich closed threads at the moment of close_thread(). Default off so
# behavior is predictable; users opt in via env.
AUTO_REVIEW_ENABLED: bool = os.environ.get(
    "THREADKEEPER_AUTO_REVIEW", ""
).lower() in {"1", "true", "yes", "on"}

# Budget cap on combined RSS of all running spawned children (not the
# parent itself). spawn() refuses a new child whose estimated RSS would
# push total over this. Default 3 GB. Set 0 to disable budget enforcement.
SPAWN_BUDGET_MB: int = int(
    os.environ.get("THREADKEEPER_SPAWN_BUDGET_MB", "3072")
)
# Initial RSS estimate for a freshly-spawned child before its real RSS is
# measured by the budget daemon. Updated to actual value within ~10s.
SPAWN_ESTIMATE_SLIM_MB: int = int(
    os.environ.get("THREADKEEPER_SPAWN_ESTIMATE_SLIM_MB", "500")
)
SPAWN_ESTIMATE_FULL_MB: int = int(
    os.environ.get("THREADKEEPER_SPAWN_ESTIMATE_FULL_MB", "1500")
)
# Budget daemon poll interval (seconds). 0 disables the daemon (estimates
# stay frozen; not recommended outside tests).
SPAWN_BUDGET_POLL_S: float = float(
    os.environ.get("THREADKEEPER_SPAWN_BUDGET_POLL_S", "10")
)

# Shadow-review daemon. Periodically scans recently-ingested
# dialog_messages from ALL active sessions, looks for class-level
# learning signals, and spawns an LLM evaluator child to decide whether
# to materialize a skill. 0 disables (default — opt in via env).
SHADOW_REVIEW_INTERVAL_S: float = float(
    os.environ.get("THREADKEEPER_SHADOW_REVIEW_INTERVAL_S", "0")
)
# Sliding window of dialog history each shadow pass considers, in
# seconds. Combined with the dedup cursor: actual scan range is
# max(cursor_ts, now-window_s) → now.
SHADOW_REVIEW_WINDOW_S: int = int(
    os.environ.get("THREADKEEPER_SHADOW_REVIEW_WINDOW_S", "900")
)
# Minimum significant chars (user+assistant dialog combined) before a
# pass is worth spawning the evaluator. Cheap floor against periodic
# misfires on idle windows.
SHADOW_REVIEW_MIN_CHARS: int = int(
    os.environ.get("THREADKEEPER_SHADOW_REVIEW_MIN_CHARS", "500")
)
