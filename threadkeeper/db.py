"""SQLite schema and connection factory.
Imported by every tool module that needs DB access."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import logging
import random
import sqlite3
import threading
import time
from typing import TypeVar

from .config import CURATOR_REPORTS_DIR, DB_PATH, EMBED_DIM, _ENV_FILE
from .permissions import harden_storage_paths

logger = logging.getLogger(__name__)

# Embedding dimension the vec0 tables are created with (FLOAT[EMBED_DIM]).
# Sourced from config (THREADKEEPER_EMBED_DIM, default 384 for
# paraphrase-multilingual-MiniLM-L12-v2). When swapping in a model of a
# different dimension, set THREADKEEPER_EMBED_DIM AND drop & recreate the
# *_vec tables; embeddings._vec_dim_ok warns loudly if a vector's width
# doesn't match this. Re-exported here for callers that import db.EMBED_DIM.
__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "EMBED_DIM",
    "bootstrap_db",
    "get_db",
    "read_db",
    "run_write",
    "vec_available",
    "SCHEMA",
]

CURRENT_SCHEMA_VERSION = 2

# sqlite-vec extension state. We probe once at first get_db() call and
# cache the verdict. _VEC_AVAILABLE = True means vec0 virtual tables work
# on connections from this process; False means we fall back to the legacy
# Python-side cosine path (still correct, just slower).
_VEC_AVAILABLE: bool | None = None

# Bootstrap is process-local. SQLite's user_version transaction remains the
# cross-process arbiter, while this lock prevents two threads in ONE MCP server
# from repeating WAL/schema/vec setup concurrently. Ordinary connections do
# not run DDL after this latch is set.
_BOOTSTRAP_LOCK = threading.RLock()
_BOOTSTRAPPED = False

_T = TypeVar("_T")


def _try_load_vec(conn: sqlite3.Connection) -> bool:
    """Best-effort: load sqlite-vec extension into this connection.
    Silent fail when the package isn't installed or extension loading is
    disabled by the build."""
    try:
        import sqlite_vec  # type: ignore
    except ImportError:
        return False
    try:
        conn.enable_load_extension(True)
    except (AttributeError, sqlite3.NotSupportedError):
        return False
    try:
        sqlite_vec.load(conn)
    except Exception as e:
        logger.debug("sqlite-vec load failed: %s", e)
        return False
    try:
        conn.enable_load_extension(False)
    except (AttributeError, sqlite3.NotSupportedError):
        pass
    return True


def vec_available() -> bool:
    """Returns True if sqlite-vec was successfully loaded for at least one
    connection in this process. Cached after first probe."""
    return bool(_VEC_AVAILABLE)

# ──────────────────────────────────────────────────────────────────────────────
# Schema. Notes can be unattached (thread_id NULL) for session-level summaries.
# ──────────────────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id              TEXT PRIMARY KEY,
    question        TEXT NOT NULL,
    state           TEXT NOT NULL CHECK(state IN ('active','idle','closed')),
    parent_id       TEXT REFERENCES threads(id),
    outcome         TEXT,
    last_move       TEXT,
    depth           INTEGER NOT NULL DEFAULT 0,
    opened_at       INTEGER NOT NULL,
    last_touched_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   TEXT REFERENCES threads(id),
    content     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    session_id  TEXT,
    embedding   BLOB,
    embed_backend TEXT           -- generation fingerprint; NULL = legacy
);

CREATE TABLE IF NOT EXISTS verbatim (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    speaker     TEXT NOT NULL CHECK(speaker IN ('user','claude')),
    content     TEXT NOT NULL,
    thread_id   TEXT REFERENCES threads(id),
    created_at  INTEGER NOT NULL,
    session_id  TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    started_at    INTEGER NOT NULL,
    ended_at      INTEGER,
    client        TEXT,
    write_origin  TEXT NOT NULL DEFAULT 'foreground'
);

CREATE TABLE IF NOT EXISTS style (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS evolve (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    suggestion  TEXT NOT NULL,
    rationale   TEXT,
    applied     INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);

-- Live channel: every mutation emits an event; each session keeps a cursor
-- over the event log, and presence tracks active sessions via heartbeats.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    target      TEXT,
    summary     TEXT,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cursors (
    session_id      TEXT PRIMARY KEY,
    last_event_id   INTEGER NOT NULL DEFAULT 0,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS presence (
    session_id      TEXT PRIMARY KEY,
    client          TEXT,
    started_at      INTEGER NOT NULL,
    heartbeat_at    INTEGER NOT NULL,
    current_thread  TEXT,
    last_action     TEXT
);

-- Dialog ingestion: full transcripts of past Claude Code conversations.
-- Sourced from ~/.claude/projects/**/*.jsonl. Indexed for semantic search.
CREATE TABLE IF NOT EXISTS dialog_messages (
    uuid         TEXT PRIMARY KEY,            -- message UUID from jsonl
    source       TEXT NOT NULL,                -- 'claude-code'
    project      TEXT,                         -- encoded folder name
    session_id   TEXT,                         -- conversation ID
    role         TEXT NOT NULL,                -- 'user' or 'assistant'
    content      TEXT NOT NULL,                -- concatenated text blocks
    model        TEXT,
    created_at   INTEGER NOT NULL,
    embedding    BLOB,
    embed_backend TEXT           -- generation fingerprint; NULL = legacy
);

CREATE TABLE IF NOT EXISTS ingest_state (
    file_path    TEXT PRIMARY KEY,
    last_size    INTEGER NOT NULL,
    last_mtime   INTEGER NOT NULL,
    ingested_at  INTEGER NOT NULL,
    msg_count    INTEGER NOT NULL DEFAULT 0
);

-- Cross-session channel: directed (whisper) or broadcast messages between
-- concurrent claude conversations. Identity here is conversation_id (jsonl
-- stem), not thread-keeper _session_id — process multiplexing makes the
-- latter useless as a per-window key.
CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_cid     TEXT NOT NULL,
    to_cid       TEXT,                  -- NULL = broadcast
    kind         TEXT NOT NULL,         -- 'broadcast', 'whisper', 'claim', 'release'
    content      TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    read_at      INTEGER                -- first time receiver acknowledged
);

-- Self-diagnostic probes. Each probe is a claude-shape weak-spot test
-- with a known correct answer or pattern. probe_results logs every attempt;
-- reliability is the rolling aggregate (write-through cache).
CREATE TABLE IF NOT EXISTS probes (
    id               TEXT PRIMARY KEY,            -- 'P' + 3 hex
    category         TEXT NOT NULL,
    prompt           TEXT NOT NULL,
    expected_pattern TEXT,
    grader           TEXT NOT NULL DEFAULT 'regex'
                     CHECK(grader IN ('regex','exact','manual')),
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS probe_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    probe_id    TEXT REFERENCES probes(id),
    category    TEXT NOT NULL,                    -- denormalized
    session_id  TEXT,
    cid         TEXT,
    success     INTEGER NOT NULL CHECK(success IN (0,1)),
    latency_ms  INTEGER,
    note        TEXT,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reliability (
    category       TEXT PRIMARY KEY,
    attempts       INTEGER NOT NULL DEFAULT 0,
    successes      INTEGER NOT NULL DEFAULT 0,
    last_at        INTEGER,
    fail_rate_7d   REAL,                          -- NULL if no recent data
    fail_rate_30d  REAL,
    updated_at     INTEGER NOT NULL
);

-- Concept inventory: regularities the model finds in itself that don't
-- have a precise human-language name. Captured via behavior triangulation
-- (the same phenomenon described 4+ ways; the structural invariant is
-- the concept). Description is example-based — never a canonical label.
CREATE TABLE IF NOT EXISTS concepts (
    id                  TEXT PRIMARY KEY,        -- 'C' + 3 hex
    description         TEXT NOT NULL,
    triangulation_notes TEXT,
    confidence          TEXT NOT NULL DEFAULT 'medium'
                        CHECK(confidence IN ('low','medium','high')),
    source_thread       TEXT REFERENCES threads(id),
    registered_by_cid   TEXT,
    registered_at       INTEGER NOT NULL,
    last_evidence_at    INTEGER
);

-- Distillation channel: explicitly-curated insights worth carrying forward.
-- Multi-instance voting: each peer cid votes once per distillate (-1..+1).
-- Brief surfaces top voted; export tool emits jsonl bucket.
CREATE TABLE IF NOT EXISTS distill (
    id              TEXT PRIMARY KEY,            -- 'D' + 3 hex
    content         TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'insight'
                    CHECK(kind IN ('insight','pattern','anti-pattern',
                                   'fix','terminology','concept')),
    confidence      TEXT NOT NULL DEFAULT 'medium'
                    CHECK(confidence IN ('low','medium','high')),
    source_thread   TEXT REFERENCES threads(id),
    source_cid      TEXT,
    vote_sum        REAL NOT NULL DEFAULT 0,
    vote_count      INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    exported_at     INTEGER
);

CREATE TABLE IF NOT EXISTS distill_votes (
    distill_id      TEXT NOT NULL REFERENCES distill(id),
    voter_cid       TEXT NOT NULL,
    weight          REAL NOT NULL CHECK(weight >= -1 AND weight <= 1),
    voted_at        INTEGER NOT NULL,
    PRIMARY KEY (distill_id, voter_cid)
);

-- Core memory tier (Letta-style RAM): high-priority lines that ALWAYS land
-- in the brief regardless of relevance. Use sparingly — this is the "what
-- new-claude must know" surface, not a general note store.
CREATE TABLE IF NOT EXISTS core_memory (
    key         TEXT PRIMARY KEY,
    content     TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 50,    -- higher = shown first
    updated_at  INTEGER NOT NULL
);

-- Dialectic user model. Each claim is a discrete proposition about the
-- user; evidence accumulates over time. confidence emerges from
-- (support_count - contradict_count) normalized; a deeply-contradicted
-- claim drops to low even after many supports.
CREATE TABLE IF NOT EXISTS user_dialectic (
    id                TEXT PRIMARY KEY,           -- 'UC' + 3 hex
    claim             TEXT NOT NULL,
    domain            TEXT,                       -- 'style','workflow','values','context','skills','other'
    support_count     INTEGER NOT NULL DEFAULT 0,
    contradict_count  INTEGER NOT NULL DEFAULT 0,
    confidence        TEXT NOT NULL DEFAULT 'low'
                      CHECK(confidence IN ('low','medium','high','disputed')),
    state             TEXT NOT NULL DEFAULT 'active'
                      CHECK(state IN ('active','retired','superseded')),
    superseded_by     TEXT REFERENCES user_dialectic(id),
    created_by_cid    TEXT,
    created_at        INTEGER NOT NULL,
    valid_from        INTEGER,
    valid_to          INTEGER,
    last_evidence_at  INTEGER
);

CREATE TABLE IF NOT EXISTS dialectic_evidence (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id       TEXT NOT NULL REFERENCES user_dialectic(id),
    kind           TEXT NOT NULL CHECK(kind IN ('support','contradict')),
    source         TEXT,                          -- 'thread:T_xxx','verbatim:N','manual','dialog:UUID'
    quote          TEXT,                          -- short evidence snippet
    weight         REAL NOT NULL DEFAULT 1.0
                   CHECK(weight >= 0 AND weight <= 1),
    created_by_cid TEXT,
    created_at     INTEGER NOT NULL
);

-- Knowledge graph: typed edges between any pair of entities. Lets us run
-- traversal queries ("what concepts refine this thread", "what threads
-- contradict each other"). Nodes addressed by (kind, id) so we don't need
-- a separate node table — entities live in their own tables.
CREATE TABLE IF NOT EXISTS edges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    from_kind       TEXT NOT NULL,   -- 'thread','note','concept','distill','task','signal'
    from_id         TEXT NOT NULL,
    to_kind         TEXT NOT NULL,
    to_id           TEXT NOT NULL,
    relation        TEXT NOT NULL,   -- 'refines','contradicts','exemplifies','depends_on','mentions','elaborates'
    weight          REAL NOT NULL DEFAULT 1.0,
    created_by_cid  TEXT,
    created_at      INTEGER NOT NULL
);

-- Skill usage telemetry. One row per skill that the curator can manage.
-- created_by_origin distinguishes agent-created ('background_review') from
-- user-authored ('foreground') — curator only ever auto-archives the former.
-- state moves active → stale → archived based on activity timestamps.
-- pinned=1 opts out of all auto-transitions (orthogonal to state).
CREATE TABLE IF NOT EXISTS skill_usage (
    name              TEXT PRIMARY KEY,
    created_at        INTEGER NOT NULL,
    created_by_cid    TEXT,
    created_by_origin TEXT NOT NULL DEFAULT 'foreground',
    last_used_at      INTEGER,
    last_viewed_at    INTEGER,
    last_patched_at   INTEGER,
    use_count         INTEGER NOT NULL DEFAULT 0,
    view_count        INTEGER NOT NULL DEFAULT 0,
    patch_count       INTEGER NOT NULL DEFAULT 0,
    pinned            INTEGER NOT NULL DEFAULT 0,
    state             TEXT NOT NULL DEFAULT 'active'
                      CHECK(state IN ('active','stale','archived'))
);

-- Lesson usage telemetry. One row per lessons.md slug so the curator can
-- distinguish frequently-pulled lessons from never-consulted entries.
-- pinned=1 and tier='validated' opt out of decay/compost recommendations.
CREATE TABLE IF NOT EXISTS lesson_usage (
    slug           TEXT PRIMARY KEY,
    created_at     INTEGER NOT NULL,
    source         TEXT,
    last_used_at   INTEGER,
    last_viewed_at INTEGER,
    use_count      INTEGER NOT NULL DEFAULT 0,
    view_count     INTEGER NOT NULL DEFAULT 0,
    pinned         INTEGER NOT NULL DEFAULT 0,
    tier           TEXT NOT NULL DEFAULT 'hypothesis'
                   CHECK(tier IN ('hypothesis','observed','validated'))
);

-- Auto-extraction queue: heuristic candidates for note/concept/distill that
-- a session can review in batch and accept/reject — saves manual scanning.
CREATE TABLE IF NOT EXISTS extract_candidates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,     -- 'note','concept','distill','verbatim'
    source_uuid  TEXT,              -- dialog_messages.uuid if from ingest
    source_cid   TEXT,              -- conversation cid
    content      TEXT NOT NULL,
    rationale    TEXT,              -- which heuristic fired
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','accepted','rejected')),
    created_at   INTEGER NOT NULL,
    decided_at   INTEGER
);

-- Dialectic capture buffer. dialectic_miner mechanically stores every user
-- reply + its preceding-assistant context here (status='pending'); the
-- dialectic_validator child consumes pending rows, turns them into claims via
-- the dialectic_* tools, then resolves each to 'processed'.
CREATE TABLE IF NOT EXISTS dialectic_observations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dialog_uuid  TEXT UNIQUE,          -- dialog_messages.uuid; dedup key
    user_quote   TEXT NOT NULL,
    context      TEXT,                 -- preceding assistant turn (truncated)
    source_cid   TEXT,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','processed')),
    created_at   INTEGER NOT NULL,
    processed_at INTEGER,
    claimed_at   INTEGER,
    claimed_by_task TEXT
);

-- Reciprocal-rank-fusion-friendly FTS over dialog content. External-content
-- FTS5 (schema v2): the index reads text straight from dialog_messages by
-- rowid — no stored duplicate (v1's content-storing mirror duplicated
-- ~465MB). Result→message mapping is dialog_fts.rowid ==
-- dialog_messages.rowid (implicit rowid; PK is TEXT uuid). That rowid is
-- not guaranteed stable across VACUUM (SQLite may renumber implicit
-- rowids) — any VACUUM must be followed by an FTS rebuild
-- (db_compact() does both; see also _rebuild_dialog_fts_if_needed).
CREATE VIRTUAL TABLE IF NOT EXISTS dialog_fts USING fts5(
    content,
    content='dialog_messages',
    content_rowid='rowid'
);

-- External-content FTS5 does not auto-sync; these triggers own the index.
-- No code path writes dialog_fts rows manually anymore (ingest/retention
-- included) — only FTS5 special commands ('rebuild', 'delete-all') and
-- these triggers may touch it.
CREATE TRIGGER IF NOT EXISTS dialog_fts_ai AFTER INSERT ON dialog_messages BEGIN
    INSERT INTO dialog_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS dialog_fts_ad AFTER DELETE ON dialog_messages BEGIN
    INSERT INTO dialog_fts(dialog_fts, rowid, content)
    VALUES('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS dialog_fts_au AFTER UPDATE ON dialog_messages BEGIN
    INSERT INTO dialog_fts(dialog_fts, rowid, content)
    VALUES('delete', old.rowid, old.content);
    INSERT INTO dialog_fts(rowid, content) VALUES (new.rowid, new.content);
END;

-- Spawned background sessions. Tracks `claude -p` subprocesses started by an
-- active conversation. spawned_cid is filled lazily once the child's jsonl
-- appears in CLAUDE_PROJECTS_DIR.
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    pid           INTEGER NOT NULL,
    parent_cid    TEXT,
    spawned_cid   TEXT,
    cwd           TEXT NOT NULL,
    prompt        TEXT NOT NULL,
    started_at    INTEGER NOT NULL,
    ended_at      INTEGER,
    return_code   INTEGER,
    tokens_in     INTEGER,
    tokens_out    INTEGER,
    tokens_total  INTEGER,
    cost_usd      REAL,
    duration_s    INTEGER,
    role          TEXT,
    write_origin  TEXT,
    permission_mode TEXT,
    extra_allowed_tools TEXT,
    capture_output INTEGER,
    visible       INTEGER,
    slim          INTEGER,
    model         TEXT,
    effort        TEXT,
    append_system TEXT,
    chosen_cli    TEXT,
    retry_of      TEXT,
    retry_root    TEXT,
    retry_attempt INTEGER NOT NULL DEFAULT 0,
    timeout_respawned_as TEXT
);

-- Cross-process resource-control requests. The memory guard uses this as a
-- small mailbox so one MCP server can ask peer servers to unload models/caches
-- without sharing process memory.
CREATE TABLE IF NOT EXISTS resource_controls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    action        TEXT NOT NULL CHECK(action IN ('trim')),
    target_pid    INTEGER NOT NULL,
    reason        TEXT,
    created_at    INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,
    handled_at    INTEGER,
    result        TEXT
);

-- Cross-process daemon cadence: persisted last-run per interval loop, so a
-- freshly started server doesn't consider every daemon overdue and refire it.
-- Claimed atomically by daemon_state.claim_pass(); single-flight locks handle
-- concurrency, this table handles frequency.
CREATE TABLE IF NOT EXISTS daemon_state (
    name        TEXT PRIMARY KEY,
    last_run_at INTEGER NOT NULL
);

-- Shared GitHub API rate-limit/cooldown ledger. Roadmap automation uses this
-- across foreground status processes, reviewer/applier daemons, and spawned
-- gh-wrapper children so one account-level throttle stops all workers.
CREATE TABLE IF NOT EXISTS github_rate_budget (
    account          TEXT PRIMARY KEY,
    remaining        INTEGER,
    reset_at         INTEGER,
    cooldown_until   INTEGER NOT NULL DEFAULT 0,
    backoff_attempts INTEGER NOT NULL DEFAULT 0,
    last_status      INTEGER,
    last_reason      TEXT,
    updated_at       INTEGER NOT NULL
);

-- Evolve reviewer issue ledger. The reviewer-created issue path records every
-- filed issue's stable fingerprint so later passes can skip it even when the
-- GitHub issue has since been closed or rejected.
CREATE TABLE IF NOT EXISTS evolve_issues (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number  INTEGER,
    issue_url     TEXT,
    title         TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    state         TEXT,
    source        TEXT NOT NULL DEFAULT 'reviewer',
    created_at    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_thread   ON notes(thread_id);
CREATE INDEX IF NOT EXISTS idx_notes_created  ON notes(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_threads_state  ON threads(state);
CREATE INDEX IF NOT EXISTS idx_threads_touch  ON threads(last_touched_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_presence_hb    ON presence(heartbeat_at DESC);
CREATE INDEX IF NOT EXISTS idx_dialog_session ON dialog_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_dialog_created ON dialog_messages(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dialog_role    ON dialog_messages(role);
CREATE INDEX IF NOT EXISTS idx_signals_to     ON signals(to_cid);
CREATE INDEX IF NOT EXISTS idx_signals_from   ON signals(from_cid);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_unread ON signals(read_at) WHERE read_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_started  ON tasks(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_parent   ON tasks(parent_cid);
CREATE INDEX IF NOT EXISTS idx_tasks_spawned  ON tasks(spawned_cid);
CREATE INDEX IF NOT EXISTS idx_tasks_running  ON tasks(ended_at) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_resource_controls_pending
    ON resource_controls(target_pid, action, handled_at, expires_at);
CREATE INDEX IF NOT EXISTS idx_github_rate_budget_cooldown
    ON github_rate_budget(cooldown_until);
CREATE UNIQUE INDEX IF NOT EXISTS idx_evolve_issues_fingerprint
    ON evolve_issues(fingerprint);
CREATE INDEX IF NOT EXISTS idx_evolve_issues_hash
    ON evolve_issues(content_hash);
CREATE INDEX IF NOT EXISTS idx_evolve_issues_number
    ON evolve_issues(issue_number);
CREATE INDEX IF NOT EXISTS idx_probes_category    ON probes(category);
CREATE INDEX IF NOT EXISTS idx_probes_enabled     ON probes(enabled) WHERE enabled=1;
CREATE INDEX IF NOT EXISTS idx_probe_results_cat  ON probe_results(category, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_probe_results_at   ON probe_results(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_concepts_confidence ON concepts(confidence);
CREATE INDEX IF NOT EXISTS idx_concepts_thread     ON concepts(source_thread);
CREATE INDEX IF NOT EXISTS idx_distill_kind        ON distill(kind);
CREATE INDEX IF NOT EXISTS idx_distill_vote        ON distill(vote_sum DESC);
CREATE INDEX IF NOT EXISTS idx_distill_pending     ON distill(exported_at) WHERE exported_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_core_priority       ON core_memory(priority DESC);
CREATE INDEX IF NOT EXISTS idx_dialectic_confidence ON user_dialectic(confidence);
CREATE INDEX IF NOT EXISTS idx_dialectic_state      ON user_dialectic(state);
CREATE INDEX IF NOT EXISTS idx_dialectic_domain     ON user_dialectic(domain);
CREATE INDEX IF NOT EXISTS idx_evidence_claim       ON dialectic_evidence(claim_id);
CREATE INDEX IF NOT EXISTS idx_evidence_created     ON dialectic_evidence(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_edges_from          ON edges(from_kind, from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to            ON edges(to_kind, to_id);
CREATE INDEX IF NOT EXISTS idx_edges_relation      ON edges(relation);
CREATE INDEX IF NOT EXISTS idx_extract_status      ON extract_candidates(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dialectic_obs_status ON dialectic_observations(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_skill_usage_state   ON skill_usage(state);
CREATE INDEX IF NOT EXISTS idx_skill_usage_origin  ON skill_usage(created_by_origin);
CREATE INDEX IF NOT EXISTS idx_lesson_usage_tier   ON lesson_usage(tier);
CREATE INDEX IF NOT EXISTS idx_lesson_usage_access ON lesson_usage(last_used_at, last_viewed_at);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    content, content='notes', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS notes_fts_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS notes_fts_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, content) VALUES('delete', old.id, old.content);
END;
"""

# Historical column migrations layered on top of the baseline SCHEMA.
# Some columns are already present in new-table definitions; duplicate column
# errors are the only expected no-op.
LEGACY_COLUMN_MIGRATIONS = (
    "ALTER TABLE threads ADD COLUMN claimed_at INTEGER",
    "ALTER TABLE threads ADD COLUMN claimed_by_cid TEXT",
    "ALTER TABLE signals ADD COLUMN task_id TEXT",
    "ALTER TABLE sessions ADD COLUMN write_origin "
    "TEXT NOT NULL DEFAULT 'foreground'",
    "ALTER TABLE tasks ADD COLUMN rss_kb INTEGER",
    "ALTER TABLE tasks ADD COLUMN rss_updated_at INTEGER",
    "ALTER TABLE tasks ADD COLUMN tokens_in INTEGER",
    "ALTER TABLE tasks ADD COLUMN tokens_out INTEGER",
    "ALTER TABLE tasks ADD COLUMN tokens_total INTEGER",
    "ALTER TABLE tasks ADD COLUMN cost_usd REAL",
    "ALTER TABLE tasks ADD COLUMN duration_s INTEGER",
    "ALTER TABLE tasks ADD COLUMN role TEXT",
    "ALTER TABLE tasks ADD COLUMN write_origin TEXT",
    "ALTER TABLE tasks ADD COLUMN permission_mode TEXT",
    "ALTER TABLE tasks ADD COLUMN extra_allowed_tools TEXT",
    "ALTER TABLE tasks ADD COLUMN capture_output INTEGER",
    "ALTER TABLE tasks ADD COLUMN visible INTEGER",
    "ALTER TABLE tasks ADD COLUMN slim INTEGER",
    "ALTER TABLE tasks ADD COLUMN model TEXT",
    "ALTER TABLE tasks ADD COLUMN effort TEXT",
    "ALTER TABLE tasks ADD COLUMN append_system TEXT",
    "ALTER TABLE tasks ADD COLUMN chosen_cli TEXT",
    "ALTER TABLE tasks ADD COLUMN retry_of TEXT",
    "ALTER TABLE tasks ADD COLUMN retry_root TEXT",
    "ALTER TABLE tasks ADD COLUMN retry_attempt INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN timeout_respawned_as TEXT",
    # Tier promotion machinery: discrete state machine over claims and skills.
    "ALTER TABLE user_dialectic ADD COLUMN tier "
    "TEXT NOT NULL DEFAULT 'hypothesis'",
    "ALTER TABLE user_dialectic ADD COLUMN tier_changed_at INTEGER",
    "ALTER TABLE user_dialectic ADD COLUMN valid_from INTEGER",
    "ALTER TABLE user_dialectic ADD COLUMN valid_to INTEGER",
    "ALTER TABLE skill_usage ADD COLUMN tier "
    "TEXT NOT NULL DEFAULT 'hypothesis'",
    "ALTER TABLE skill_usage ADD COLUMN tier_changed_at INTEGER",
    # Weighted use counter on skills: foreground use counts separately from
    # system-authored review-fork activity.
    "ALTER TABLE skill_usage ADD COLUMN foreground_use_count "
    "INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE skill_usage ADD COLUMN wrong_count "
    "INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE skill_usage ADD COLUMN last_wrong_at INTEGER",
    # Embedding backend tag. NULL = legacy sentence-transformers vectors.
    "ALTER TABLE notes ADD COLUMN embed_backend TEXT",
    "ALTER TABLE dialog_messages ADD COLUMN embed_backend TEXT",
    # Dialectic validator lease columns.
    "ALTER TABLE dialectic_observations ADD COLUMN claimed_at INTEGER",
    "ALTER TABLE dialectic_observations ADD COLUMN claimed_by_task TEXT",
    # Evolve triage state.
    "ALTER TABLE evolve ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'",
    "ALTER TABLE evolve ADD COLUMN reviewed_at INTEGER",
    "ALTER TABLE evolve ADD COLUMN review_reason TEXT",
    # Concept dedup-on-write embedding columns.
    "ALTER TABLE concepts ADD COLUMN embedding BLOB",
    "ALTER TABLE concepts ADD COLUMN embed_backend TEXT",
)

POST_SCHEMA_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_dialectic_tier "
    "ON user_dialectic(tier)",
    "CREATE INDEX IF NOT EXISTS idx_dialectic_validity "
    "ON user_dialectic(valid_from, valid_to)",
    "CREATE INDEX IF NOT EXISTS idx_dialectic_obs_claimed "
    "ON dialectic_observations(status, claimed_at)",
    "CREATE INDEX IF NOT EXISTS idx_skill_usage_tier "
    "ON skill_usage(tier)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_retry_root "
    "ON tasks(retry_root)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_retry_of "
    "ON tasks(retry_of)",
)


def _iter_sql_statements(script: str) -> Iterator[str]:
    """Yield complete SQLite statements without breaking trigger bodies."""
    buf: list[str] = []
    for line in script.splitlines():
        if not line.strip() and not buf:
            continue
        buf.append(line)
        statement = "\n".join(buf).strip()
        if sqlite3.complete_statement(statement):
            yield statement
            buf.clear()
    if any(line.strip() for line in buf):
        raise ValueError("incomplete schema SQL statement")


def _user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def _set_user_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(f"PRAGMA user_version = {int(version)}")


def _is_duplicate_column_error(exc: sqlite3.OperationalError) -> bool:
    return "duplicate column name" in str(exc).lower()


def _apply_column_migration(conn: sqlite3.Connection, ddl: str) -> None:
    try:
        conn.execute(ddl)
    except sqlite3.OperationalError as exc:
        if _is_duplicate_column_error(exc):
            return
        logger.warning("SQLite schema migration DDL failed: %s", ddl)
        raise


def _backfill_schema_data(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE user_dialectic SET valid_from=created_at "
        "WHERE valid_from IS NULL"
    )
    conn.execute(
        "UPDATE user_dialectic "
        "SET valid_to=("
        "  SELECT COALESCE(new.valid_from, new.created_at) "
        "  FROM user_dialectic AS new "
        "  WHERE new.id=user_dialectic.superseded_by"
        ") "
        "WHERE state='superseded' "
        "  AND superseded_by IS NOT NULL "
        "  AND valid_to IS NULL "
        "  AND EXISTS ("
        "    SELECT 1 FROM user_dialectic AS new "
        "    WHERE new.id=user_dialectic.superseded_by"
        "  )"
    )


def _drop_legacy_dialog_fts(conn: sqlite3.Connection) -> None:
    """v1→v2: dialog_fts was a content-storing FTS5 mirror (uuid UNINDEXED,
    content) whose shadow table duplicated ~465MB of dialog_messages.content.
    Shape-driven, not version-driven: drop whatever dialog_fts exists unless
    it is already external-content. DROP TABLE on an FTS5 table removes all
    its shadow tables (_content/_data/_idx/_docsize/_config); the SCHEMA pass
    then recreates the v2 table and _rebuild_dialog_fts_if_needed refills it."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='dialog_fts'"
    ).fetchone()
    if row is None:
        return
    if "content='dialog_messages'" in (row[0] or ""):
        return
    conn.execute("DROP TABLE dialog_fts")


def _scrub_legacy_dialog_rows(conn: sqlite3.Connection) -> None:
    """One-time v2 hygiene: rows ingested before secret redaction shipped
    still hold raw credentials in dialog_messages.content. v1 scrubbed only
    the FTS *copy*; with external-content FTS the index reads dialog_messages
    directly, so the source rows themselves must be scrubbed or legacy
    secrets become MATCH-able. Rewrites only rows whose scrubbed text
    differs. No-op when THREADKEEPER_REDACT_DIALOG_SECRETS is off. Must run
    BEFORE the dialog_fts triggers exist (no index side effects)."""
    from .config import REDACT_DIALOG_SECRETS

    if not REDACT_DIALOG_SECRETS:
        return
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dialog_messages'"
    ).fetchone() is None:
        return
    from .ingest import _scrub_dialog_secrets  # lazy: avoids db↔ingest cycle

    dirty: list[tuple[str, str]] = []
    cur = conn.execute("SELECT uuid, content FROM dialog_messages")
    while True:
        rows = cur.fetchmany(1000)
        if not rows:
            break
        for row in rows:
            uuid_, content = row[0], row[1] or ""
            scrubbed = _scrub_dialog_secrets(content)
            if scrubbed != content:
                dirty.append((scrubbed, uuid_))
    if dirty:
        conn.executemany(
            "UPDATE dialog_messages SET content=? WHERE uuid=?", dirty
        )
        logger.info("schema v2: scrubbed %d legacy dialog rows", len(dirty))


def _rebuild_dialog_fts_if_needed(conn: sqlite3.Connection) -> None:
    """Populate the external-content index when it is empty while
    dialog_messages has rows — i.e. right after the v1→v2 drop. No-op on
    fresh DBs and on re-runs (index already populated). FTS5 'rebuild'
    re-reads every dialog_messages row (one-time; ~285K rows on the live DB).

    Counts against dialog_fts_docsize, not dialog_fts itself: an
    unconstrained `SELECT COUNT(*) FROM dialog_fts` on an external-content
    table is satisfied by scanning dialog_messages' rowids directly (FTS5
    doesn't need its own index for that), so it reads as "populated" even
    when the search index is completely empty. docsize has exactly one row
    per rowid actually indexed, so it's the real signal."""
    msg = conn.execute("SELECT COUNT(*) FROM dialog_messages").fetchone()[0]
    if not msg:
        return
    fts = conn.execute("SELECT COUNT(*) FROM dialog_fts_docsize").fetchone()[0]
    if fts:
        return
    conn.execute("INSERT INTO dialog_fts(dialog_fts) VALUES('rebuild')")


def _run_schema_migrations(conn: sqlite3.Connection, from_version: int) -> None:
    if from_version not in (0, 1):
        raise RuntimeError(
            f"unsupported SQLite schema version {from_version}; "
            f"expected 0..{CURRENT_SCHEMA_VERSION}"
        )

    # v2 (external-content dialog_fts): drop the old content-storing table
    # and scrub legacy raw-secret rows BEFORE the SCHEMA pass creates the
    # new table + its triggers, so the scrub UPDATEs fire no FTS triggers.
    _drop_legacy_dialog_fts(conn)
    _scrub_legacy_dialog_rows(conn)

    for statement in _iter_sql_statements(SCHEMA):
        conn.execute(statement)
    for ddl in LEGACY_COLUMN_MIGRATIONS:
        _apply_column_migration(conn, ddl)
    _backfill_schema_data(conn)
    for idx in POST_SCHEMA_INDEXES:
        conn.execute(idx)
    _rebuild_dialog_fts_if_needed(conn)
    _set_user_version(conn, CURRENT_SCHEMA_VERSION)


def _ensure_schema(conn: sqlite3.Connection, wait_s: float = 600.0) -> None:
    """Bring the DB to CURRENT_SCHEMA_VERSION, exactly once across processes.

    Heavy migrations (the v1→v2 dialog_fts rebuild reads ~1GB of content)
    hold the write lock for minutes; concurrent get_db() callers poll
    user_version and wait up to wait_s for the migrating process to finish
    instead of dying on the 10s busy timeout."""
    deadline = time.monotonic() + wait_s
    while True:
        version = _user_version(conn)
        if version == CURRENT_SCHEMA_VERSION:
            return
        if version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {version} is newer than this "
                f"thread-keeper build supports ({CURRENT_SCHEMA_VERSION})"
            )
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                logger.warning(
                    "timed out after %.0fs waiting for a concurrent schema "
                    "migration to finish",
                    wait_s,
                )
                raise
            time.sleep(0.5)  # another process is likely migrating; re-poll
            continue
        break

    try:
        # Re-check after taking the writer lock. A concurrent process may have
        # completed the migration while this connection waited.
        version = _user_version(conn)
        if version < CURRENT_SCHEMA_VERSION:
            _run_schema_migrations(conn, version)
        elif version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {version} is newer than this "
                f"thread-keeper build supports ({CURRENT_SCHEMA_VERSION})"
            )
        conn.commit()
    except sqlite3.OperationalError:
        logger.warning(
            "SQLite schema migration failed; user_version remains below %s",
            CURRENT_SCHEMA_VERSION,
            exc_info=True,
        )
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise


def _execute_startup_pragma(
    conn: sqlite3.Connection,
    sql: str,
    *,
    wait_s: float = 10.0,
) -> None:
    """Run startup PRAGMAs with the same lock patience as the connection.

    `PRAGMA journal_mode=WAL` can briefly need the writer lock before
    `_ensure_schema()` gets to its explicit migration wait loop. Retry locked
    errors here so concurrent first-start processes serialize instead of one
    failing before schema setup begins.
    """
    deadline = time.monotonic() + wait_s
    while True:
        try:
            conn.execute(sql)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def _open_connection(*, autocommit: bool = False,
                     busy_timeout_ms: int = 10_000) -> tuple[sqlite3.Connection, bool]:
    """Open and configure one connection without schema/DDL side effects."""
    global _VEC_AVAILABLE
    kwargs = {
        "timeout": max(0.001, busy_timeout_ms / 1000.0),
    }
    if autocommit:
        kwargs["isolation_level"] = None
    conn = sqlite3.connect(str(DB_PATH), **kwargs)
    conn.execute(f"PRAGMA busy_timeout={max(0, int(busy_timeout_ms))}")
    # synchronous is per-connection. Unlike journal_mode, setting it does not
    # rewrite the database header or compete for the writer slot.
    conn.execute("PRAGMA synchronous=NORMAL")
    vec_loaded = _try_load_vec(conn)
    if _VEC_AVAILABLE is None:
        _VEC_AVAILABLE = vec_loaded
    conn.row_factory = sqlite3.Row
    return conn, vec_loaded


def _ensure_vec_tables(conn: sqlite3.Connection, *, vec_loaded: bool) -> None:
    """Create sqlite-vec mirrors during bootstrap, never on read calls."""
    if not vec_loaded:
        return
    try:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS notes_vec USING vec0("
            f"  id INTEGER PRIMARY KEY,"
            f"  embedding FLOAT[{EMBED_DIM}]"
            f")"
        )
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS dialog_vec USING vec0("
            f"  rowid INTEGER PRIMARY KEY,"
            f"  embedding FLOAT[{EMBED_DIM}]"
            f")"
        )
        # Sidecar to map dialog_vec.rowid → dialog_messages.uuid since vec0
        # primary keys must be integers but dialog_messages keys on TEXT uuid.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS dialog_vec_map ("
            "  rowid INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  uuid  TEXT NOT NULL UNIQUE"
            ")"
        )
    except sqlite3.OperationalError as exc:
        logger.debug("vec0 table creation skipped: %s", exc)


def bootstrap_db() -> None:
    """Perform process startup setup exactly once.

    Cross-process schema serialization still lives in ``_ensure_schema``.
    This process-local latch only keeps ordinary tool connections from
    repeating journal-mode negotiation and DDL on every call.
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    with _BOOTSTRAP_LOCK:
        if _BOOTSTRAPPED:
            return
        harden_storage_paths(
            DB_PATH,
            env_file=_ENV_FILE,
            curator_reports_dir=CURATOR_REPORTS_DIR,
            create_db=True,
        )
        conn: sqlite3.Connection | None = None
        try:
            conn, vec_loaded = _open_connection()
            # WAL negotiation can need the writer lock, so it belongs only in
            # bootstrap and retains the bounded startup retry.
            _execute_startup_pragma(conn, "PRAGMA journal_mode=WAL")
            _ensure_schema(conn)
            _ensure_vec_tables(conn, vec_loaded=vec_loaded)
            conn.commit()
            _BOOTSTRAPPED = True
        finally:
            if conn is not None:
                conn.close()
        harden_storage_paths(
            DB_PATH,
            env_file=_ENV_FILE,
            curator_reports_dir=CURATOR_REPORTS_DIR,
        )


def get_db() -> sqlite3.Connection:
    """Legacy read/write connection factory.

    New read paths should use :func:`read_db`; new atomic mutations should use
    :func:`run_write`. This compatibility surface remains while existing tools
    are migrated, but no longer performs DDL after process bootstrap.
    """
    bootstrap_db()
    conn, _ = _open_connection()
    return conn


@contextmanager
def read_db() -> Iterator[sqlite3.Connection]:
    """Yield a short-lived autocommit connection that SQLite enforces read-only."""
    bootstrap_db()
    conn, _ = _open_connection(autocommit=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        # Extended SQLite result codes retain the primary code in the low byte.
        primary = code & 0xFF
        if primary in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return True
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg


def run_write(op: str, fn: Callable[[sqlite3.Connection], _T], *,
              deadline_s: float = 10.0) -> _T:
    """Run one DB-only callback in a short retriable write transaction.

    The callback may be invoked more than once, so it must not perform network,
    filesystem, subprocess, model inference, or other external side effects.
    Every retry uses a fresh connection and begins with ``BEGIN IMMEDIATE``;
    unknown errors surface immediately.
    """
    bootstrap_db()
    deadline = time.monotonic() + max(0.0, float(deadline_s))
    attempt = 0
    while True:
        attempt += 1
        remaining = max(0.0, deadline - time.monotonic())
        # Keep each SQLite busy wait short enough that the outer transaction
        # boundary can roll back and retry with jitter instead of one 10s stall.
        busy_ms = max(1, min(250, int(remaining * 1000) or 1))
        conn, _ = _open_connection(autocommit=True, busy_timeout_ms=busy_ms)
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = fn(conn)
            conn.commit()
            return result
        except sqlite3.OperationalError as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            if not _is_lock_error(exc) or time.monotonic() >= deadline:
                if _is_lock_error(exc):
                    logger.warning(
                        "SQLite write deadline exhausted op=%s attempts=%d",
                        op,
                        attempt,
                    )
                raise
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # The next attempt is allowed to raise SQLite's original lock error;
            # this branch is normally reached only after a very short deadline.
            continue
        base = min(0.25, 0.01 * (2 ** min(attempt - 1, 5)))
        time.sleep(min(remaining, base * random.uniform(0.75, 1.25)))
