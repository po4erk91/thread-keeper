"""Paths, env-driven defaults, semantic-search availability flag.
Imported wherever a constant or config is needed; cheap to import."""
from __future__ import annotations

import importlib.util
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

# Embedding runtime backend. 'onnx' (default) runs the model through fastembed /
# ONNX Runtime — no PyTorch, ~700MB footprint (vs ~1.8GB). 'sentence-transformers' is
# the legacy PyTorch path, kept as an opt-in fallback (install `.[semantic-st]`
# and set THREADKEEPER_EMBED_BACKEND=sentence-transformers). Both produce the
# same 384-dim vectors, but fastembed's are numerically NOT identical to ST's,
# so switching backends warrants a `tk-migrate-embeddings --all` recompute.
EMBED_BACKEND: str = os.environ.get(
    "THREADKEEPER_EMBED_BACKEND", "onnx"
).strip().lower()

# fastembed addresses the model under its sentence-transformers org prefix;
# SentenceTransformer accepts the bare name. Normalize for the ONNX backend.
FASTEMBED_MODEL_ID: str = (
    EMBED_MODEL_NAME if "/" in EMBED_MODEL_NAME
    else f"sentence-transformers/{EMBED_MODEL_NAME}"
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

# Client label used for `presence`/`sessions` rows.
CLIENT_LABEL: str = os.environ.get("THREADKEEPER_CLIENT", "claude")

# Write-origin for this server process. 'foreground' = a regular user-facing
# conversation; 'background_review' = a headless review fork spawned to
# auto-curate memory/skills after a complex task. Curator only ever touches
# skills created under 'background_review' so user-authored skills are safe.
WRITE_ORIGIN: str = os.environ.get(
    "THREADKEEPER_WRITE_ORIGIN", "foreground"
)

# Explicit marker set by spawn() for child CLI processes. Do not infer
# child-ness from THREADKEEPER_FORCE_CID: tests and manual diagnostics use it
# to pin identity for otherwise-foreground sessions.
SPAWNED_CHILD: bool = os.environ.get(
    "THREADKEEPER_SPAWNED_CHILD", ""
).lower() in {"1", "true", "yes", "on"}

# Autonomous daemons may spawn children or do expensive background work. They
# should run only in user-facing parent processes, never inside spawned review
# children, where they can recurse.
BACKGROUND_DAEMONS_ALLOWED: bool = (
    not SPAWNED_CHILD and WRITE_ORIGIN == "foreground"
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

# Memory guard for thread-keeper server processes themselves. Unlike
# spawn_budget, this watches every running `python -m threadkeeper.server`
# process and can terminate a server that crosses the hard RSS limit.
# Set poll or kill threshold to 0 to disable the daemon / killing.
MEMORY_GUARD_POLL_S: float = float(
    os.environ.get("THREADKEEPER_MEMORY_GUARD_POLL_S", "30")
)
MEMORY_GUARD_WARN_MB: int = int(
    os.environ.get("THREADKEEPER_MEMORY_GUARD_WARN_MB", "1536")
)
MEMORY_GUARD_KILL_MB: int = int(
    os.environ.get("THREADKEEPER_MEMORY_GUARD_KILL_MB", "3072")
)
MEMORY_GUARD_AGG_WARN_MB: int = int(
    os.environ.get("THREADKEEPER_MEMORY_GUARD_AGG_WARN_MB", "2048")
)
MEMORY_GUARD_AGG_KILL_MB: int = int(
    os.environ.get("THREADKEEPER_MEMORY_GUARD_AGG_KILL_MB", "3072")
)
MEMORY_GUARD_RECLAIM_MB: int = int(
    os.environ.get("THREADKEEPER_MEMORY_GUARD_RECLAIM_MB", "1024")
)
MEMORY_GUARD_TARGET_SERVERS: int = int(
    os.environ.get("THREADKEEPER_MEMORY_GUARD_TARGET_SERVERS", "1")
)
MEMORY_GUARD_RETIRE_IDLE_S: int = int(
    os.environ.get("THREADKEEPER_MEMORY_GUARD_RETIRE_IDLE_S", "900")
)
MEMORY_GUARD_RETIRE_LIVE: bool = os.environ.get(
    "THREADKEEPER_MEMORY_GUARD_RETIRE_LIVE", ""
).lower() in {"1", "true", "yes", "on"}
MEMORY_GUARD_NOTIFY: bool = os.environ.get(
    "THREADKEEPER_MEMORY_GUARD_NOTIFY", "1"
).lower() in {"1", "true", "yes", "on"}
MEMORY_GUARD_COOLDOWN_S: int = int(
    os.environ.get("THREADKEEPER_MEMORY_GUARD_COOLDOWN_S", "300")
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

# Curator daemon. Periodic LLM-driven audit of the existing
# lessons.md + ~/.claude/skills/ library — grades, suggests
# consolidation/patches/prunes, writes a per-run REPORT.md. Where
# shadow_review LOOKS FOR NEW class-level learning every few minutes,
# the Curator REVIEWS THE STORE every few days. 0 disables (default —
# opt in via env). Recommended: 604800 (7 days).
CURATOR_INTERVAL_S: float = float(
    os.environ.get("THREADKEEPER_CURATOR_INTERVAL_S", "0")
)
# Don't bother curating a tiny library; below this lessons-count there's
# nothing meaningful to consolidate.
CURATOR_MIN_LESSONS: int = int(
    os.environ.get("THREADKEEPER_CURATOR_MIN_LESSONS", "3")
)
# Where the Curator writes its REPORT-<isodate>.md per run. One file
# per pass; latest is the canonical one to read. Anchored to DB_PATH's
# parent so a custom THREADKEEPER_DB co-locates its curator reports.
CURATOR_REPORTS_DIR: Path = Path(
    os.environ.get(
        "THREADKEEPER_CURATOR_REPORTS_DIR",
        str(DB_PATH.parent / "curator"),
    )
).expanduser()
# When TRUE, curator-child gets write-mode tools (skill_manage delete/
# patch + lesson_append) and is instructed to apply its own PRUNE /
# PATCH / CONSOLIDATE recommendations directly, not just report them.
# Default OFF — Phase 1 is advisory-only, user reviews REPORT.md and
# applies manually. Flip to "1" once you trust the curator's verdicts.
CURATOR_DESTRUCTIVE: bool = bool(
    os.environ.get("THREADKEEPER_CURATOR_DESTRUCTIVE", "")
)

# Extract daemon. Periodically scans dialog_messages for heuristic
# candidates (note / concept / distill / verbatim) via extract_recent()
# and enqueues them under extract_candidates.status='pending'. Where
# shadow_review extracts CLASS-LEVEL durable rules, extract harvests
# PER-INCIDENT decision-shaped utterances ("let's use X", "next time
# we should Y", insight markers, bullet-listed regularities). Agent's
# subsequent review_candidates() / accept_candidate() materializes the
# survivors into notes/concepts/distills.
# 0 disables (default — opt in via env). Recommended: 600 (every 10
# min) — extract is cheap, just regex + cosine clustering on the
# already-ingested dialog window.
EXTRACT_INTERVAL_S: float = float(
    os.environ.get("THREADKEEPER_EXTRACT_INTERVAL_S", "0")
)
# Sliding window of dialog history each extract pass considers, in
# minutes. Defaults align with the typical agent-task duration so a
# whole task's worth of decisions gets harvested at once.
EXTRACT_WINDOW_MIN: int = int(
    os.environ.get("THREADKEEPER_EXTRACT_WINDOW_MIN", "30")
)

# Candidate-reviewer daemon — periodically consumes the pending queue
# extract_daemon builds up, spawns an LLM child to decide per
# candidate: SKILL.create / SKILL.patch / NOTE / VERBATIM / REJECT.
# Closes the loop between heuristic extract and SKILL.md
# materialization that previously only happened via close_thread
# auto-review (which agents rarely trigger). 0 disables (default —
# opt in). Recommended: 3600 (hourly) — extract typically adds
# ~10 candidates/h with the daemon's 30-min window, hourly review
# keeps the queue from backing up.
CANDIDATE_REVIEW_INTERVAL_S: float = float(
    os.environ.get("THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S", "0")
)
# Minimum pending candidates before the daemon engages — below this
# floor there's not enough signal to justify spawning an Opus child.
CANDIDATE_REVIEW_MIN: int = int(
    os.environ.get("THREADKEEPER_CANDIDATE_REVIEW_MIN", "3")
)

# Probe daemon. Periodically spawns a CONTEXT-FREE child to attempt one due
# self-test probe (a known weak spot: token counting, date math, format
# compliance, …); the parent grades the child's raw answer mechanically and
# records it to probe_results → reliability → brief weak_spots. Only OBJECTIVE
# graders (regex/exact with a pattern) are driven — manual probes have no
# mechanical key and stay on the manual run_probe loop. 0 disables (default —
# opt in). Recommended: 86400 (daily) — probes are a slow-moving tripwire on
# model-version drift, not a hot loop.
PROBE_INTERVAL_S: float = float(
    os.environ.get("THREADKEEPER_PROBE_INTERVAL_S", "0")
)
# Don't re-test a category whose probe ran within this window. Defaults to a
# week so each weak-spot gets a fresh reading without burning tokens daily.
PROBE_COOLDOWN_S: int = int(
    os.environ.get("THREADKEEPER_PROBE_COOLDOWN_S", str(7 * 86400))
)
