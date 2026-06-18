"""SQLite schema and connection factory.
Imported by every tool module that needs DB access."""
from __future__ import annotations

import logging
import sqlite3

from .config import DB_PATH

logger = logging.getLogger(__name__)

# Embedding dimension for paraphrase-multilingual-MiniLM-L12-v2.
# When swapping models, change here AND drop & recreate the *_vec tables.
EMBED_DIM = 384

# sqlite-vec extension state. We probe once at first get_db() call and
# cache the verdict. _VEC_AVAILABLE = True means vec0 virtual tables work
# on connections from this process; False means we fall back to the legacy
# Python-side cosine path (still correct, just slower).
_VEC_AVAILABLE: bool | None = None


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
    embed_backend TEXT           -- backend that produced `embedding`; NULL = legacy
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
    embed_backend TEXT           -- backend that produced `embedding`; NULL = legacy
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

-- Reciprocal-rank-fusion-friendly FTS over dialog content. Mirror table
-- (not content='dialog_messages') because dialog_messages PK is TEXT and
-- FTS5 content tables expect INTEGER rowid alignment.
CREATE VIRTUAL TABLE IF NOT EXISTS dialog_fts USING fts5(
    uuid UNINDEXED,
    content
);

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
    return_code   INTEGER
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
CREATE INDEX IF NOT EXISTS idx_tasks_running  ON tasks(ended_at) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_resource_controls_pending
    ON resource_controls(target_pid, action, handled_at, expires_at);
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

def get_db() -> sqlite3.Connection:
    global _VEC_AVAILABLE
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    # WAL = concurrent readers + one writer without blocking. Required for
    # running Desktop + CLI + VS Code against the same DB simultaneously.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    # Load sqlite-vec extension if available. Must happen BEFORE schema
    # so the vec0 virtual tables can be created in this connection.
    vec_loaded = _try_load_vec(conn)
    if _VEC_AVAILABLE is None:
        _VEC_AVAILABLE = vec_loaded
    conn.executescript(SCHEMA)
    if vec_loaded:
        # Create vec0 virtual tables side-by-side with the BLOB-embedding
        # ones. Existing data in notes.embedding / dialog_messages.embedding
        # is migrated lazily by a backfill job (see ingest.py).
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
            # Sidecar to map dialog_vec.rowid → dialog_messages.uuid since
            # vec0 PKs must be integers but dialog_messages keys on TEXT uuid.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS dialog_vec_map ("
                "  rowid INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  uuid  TEXT NOT NULL UNIQUE"
                ")"
            )
        except sqlite3.OperationalError as e:
            logger.debug("vec0 table creation skipped: %s", e)
    # Lightweight column migrations. ALTER TABLE ADD COLUMN is idempotent-safe
    # if we swallow OperationalError ("duplicate column name").
    for ddl in (
        "ALTER TABLE threads ADD COLUMN claimed_at INTEGER",
        "ALTER TABLE threads ADD COLUMN claimed_by_cid TEXT",
        "ALTER TABLE signals ADD COLUMN task_id TEXT",
        "ALTER TABLE sessions ADD COLUMN write_origin "
        "TEXT NOT NULL DEFAULT 'foreground'",
        "ALTER TABLE tasks ADD COLUMN rss_kb INTEGER",
        "ALTER TABLE tasks ADD COLUMN rss_updated_at INTEGER",
        # Tier promotion machinery — discrete state machine over claims
        # and skills. Independent of the continuous confidence/state
        # columns; tier is what gates downstream behavior (brief framing,
        # curator archival, auto-action thresholds).
        "ALTER TABLE user_dialectic ADD COLUMN tier "
        "TEXT NOT NULL DEFAULT 'hypothesis'",
        "ALTER TABLE user_dialectic ADD COLUMN tier_changed_at INTEGER",
        "ALTER TABLE skill_usage ADD COLUMN tier "
        "TEXT NOT NULL DEFAULT 'hypothesis'",
        "ALTER TABLE skill_usage ADD COLUMN tier_changed_at INTEGER",
        # Weighted use counter on skills: a foreground 'use' counts 1.0,
        # background_review/shadow_review/curator 'use' counts 0.5. Lets
        # tier promotion ignore self-generated activity (skills the
        # system-itself used through review-forks).
        "ALTER TABLE skill_usage ADD COLUMN foreground_use_count "
        "INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE skill_usage ADD COLUMN wrong_count "
        "INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE skill_usage ADD COLUMN last_wrong_at INTEGER",
        # Embedding backend tag. NULL = legacy (sentence-transformers, pre-ONNX
        # migration). New/recomputed rows carry 'onnx' or 'sentence-transformers'
        # so `tk-migrate-embeddings` can find stale vectors and skip done ones.
        "ALTER TABLE notes ADD COLUMN embed_backend TEXT",
        "ALTER TABLE dialog_messages ADD COLUMN embed_backend TEXT",
        # Dialectic validator lease: rows are still status='pending' until the
        # child resolves them, but claimed rows are no longer visible backlog.
        # If the child dies, the validator requeues stale claims by clearing
        # these columns.
        "ALTER TABLE dialectic_observations ADD COLUMN claimed_at INTEGER",
        "ALTER TABLE dialectic_observations ADD COLUMN claimed_by_task TEXT",
        # Evolve triage: the autonomous evolve reviewer moves a suggestion
        # from 'pending' to 'promoted' (still relevant, surface it sharply
        # for the foreground agent / human to APPLY) or 'dismissed' (dup /
        # superseded / stale). The legacy `applied` flag stays for the human
        # "I implemented this" mark. status default 'pending' so existing
        # rows enter the queue.
        "ALTER TABLE evolve ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'",
        "ALTER TABLE evolve ADD COLUMN reviewed_at INTEGER",
        "ALTER TABLE evolve ADD COLUMN review_reason TEXT",
        # Concept dedup-on-write: store the description embedding so the
        # register/extract path can re-corroborate an equivalent invariant
        # (cosine over `description`) and bump last_evidence_at instead of
        # inserting a near-duplicate row. embed_backend tags the vector the
        # same way notes/dialog_messages are tagged (NULL = no embedding).
        "ALTER TABLE concepts ADD COLUMN embedding BLOB",
        "ALTER TABLE concepts ADD COLUMN embed_backend TEXT",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass

    # Indexes for tier-aware queries. Safe to repeat (IF NOT EXISTS).
    for idx in (
        "CREATE INDEX IF NOT EXISTS idx_dialectic_tier "
        "ON user_dialectic(tier)",
        "CREATE INDEX IF NOT EXISTS idx_dialectic_obs_claimed "
        "ON dialectic_observations(status, claimed_at)",
        "CREATE INDEX IF NOT EXISTS idx_skill_usage_tier "
        "ON skill_usage(tier)",
    ):
        try:
            conn.execute(idx)
        except sqlite3.OperationalError:
            pass
    conn.row_factory = sqlite3.Row
    return conn
