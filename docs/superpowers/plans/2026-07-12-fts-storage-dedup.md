# FTS Storage Dedup (external-content FTS5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert `dialog_fts` from content-storing to external-content FTS5 so the ~465 MB duplicate of `dialog_messages.content` disappears, with a safe v1→v2 migration and an opt-in `db_compact()` tool to reclaim the freed pages.

**Architecture:** `dialog_fts` becomes `fts5(content, content='dialog_messages', content_rowid='rowid')`, kept in sync by three triggers on `dialog_messages` (so ingest/retention stop writing FTS manually). A version-gated migration (`user_version` 1→2) drops the old table, scrubs legacy raw-secret rows in `dialog_messages` (the old scheme scrubbed only the FTS copy), recreates + rebuilds the index. Search maps results back via `dialog_fts.rowid == dialog_messages.rowid`. `VACUUM` renumbers `dialog_messages`' implicit rowids, so reclaim is only ever done by `db_compact()` = VACUUM + mandatory FTS rebuild.

**Tech Stack:** Python 3.11+, stdlib `sqlite3` (FTS5), FastMCP tool registration via `threadkeeper/_mcp.py`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-10-fts-storage-dedup-design.md`

## Global Constraints

- Python floor: `requires-python = ">=3.11"` (pyproject.toml) — no 3.12-only APIs.
- No new third-party dependencies.
- `CURRENT_SCHEMA_VERSION = 2`; migration is forward-only (no downgrade path).
- **Trigger-owned FTS sync**: after this plan, NO code writes `dialog_fts` rows manually. Only the triggers, the FTS5 special commands (`'rebuild'`, `'delete-all'`), and the migration touch it.
- Test command (worktree — PYTHONPATH pin is mandatory, the venv has an editable install of main):
  `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest <paths> -x -q`
- Full-suite runs use `--forked` (as CI does): `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest --forked -q`
- NEVER `git add` anything under `.claude/` (or `.superpowers/`) — commit only the files each task names.
- Scope guard: `notes_fts` and the embedding-BLOB duplicate are OUT of scope. Do not touch them.

## File Structure

| File | Change |
|---|---|
| `threadkeeper/db.py` | SCHEMA: external-content `dialog_fts` + 3 triggers; `CURRENT_SCHEMA_VERSION=2`; migration helpers `_drop_legacy_dialog_fts`, `_scrub_legacy_dialog_rows`, `_rebuild_dialog_fts_if_needed`; `_run_schema_migrations` accepts from_version 0 and 1; `_ensure_schema` waits out a concurrent migration (Task 2) |
| `threadkeeper/embeddings.py` | `_fts_search`: select `d.uuid` via `JOIN dialog_messages d ON d.rowid = f.rowid` |
| `threadkeeper/ingest.py` | delete manual `INSERT INTO dialog_fts` in `_ingest_file`; `_backfill_dialog_fts_if_empty` → guarded `'rebuild'` |
| `threadkeeper/retention.py` | `_delete_dialog_sidecars`: drop the FTS delete (AFTER DELETE trigger owns it); keep vec cleanup |
| `threadkeeper/tools/db_maintenance.py` | NEW — `db_compact()` MCP tool (VACUUM + rebuild, single-flight) |
| `threadkeeper/server.py` | import `tools.db_maintenance` |
| `tests/test_dialog_fts_external.py` | NEW — fresh-schema shape, trigger sync, backfill rebuild |
| `tests/test_db_migration_fts.py` | NEW — v1→v2 migration matrix, legacy scrub, re-run no-op; Task 2 adds the concurrent-waiter test |
| `tests/test_db_compact.py` | NEW — tool registration, post-VACUUM correctness, busy lock |
| `tests/test_retention.py` | `_insert_dialog` stops writing FTS manually; uuid-based FTS asserts → rowid-join helper |
| `tests/test_ingest_secret_redaction.py` | FTS asserts → MATCH-based; legacy-scrub test moves to migration file |
| `docs/ARCHITECTURE.md`, `README.md`, `CHANGELOG.md` | Task 4 docs sync |

### Interfaces produced (used across tasks)

- `db.CURRENT_SCHEMA_VERSION: int = 2`
- `db._rebuild_dialog_fts_if_needed(conn: sqlite3.Connection) -> None`
- `db._ensure_schema(conn: sqlite3.Connection, wait_s: float = 600.0) -> None` (Task 2 adds the param)
- FTS search SQL shape (any consumer): `SELECT d.<cols> FROM dialog_fts f JOIN dialog_messages d ON d.rowid = f.rowid WHERE dialog_fts MATCH ? ORDER BY rank`
- MCP tool `db_compact() -> str` in `threadkeeper/tools/db_maintenance.py`

---

### Task 1: The schema flip — external-content `dialog_fts`, migration, and all consumer sites

This task is deliberately atomic: the schema shape and every consumer of the old shape (`uuid` column in FTS) must move in lockstep or the suite goes red between commits. It covers spec sections 1–3 (schema+triggers, migration, code touchpoints) plus the legacy-secret scrub that keeps spec criterion 3 ("same search results") true — under v1 the FTS copy of pre-redaction rows was scrubbed, so raw secrets were NOT matchable; external-content FTS reads `dialog_messages` directly, so those source rows must be scrubbed once at migration or secrets become searchable.

**Files:**
- Modify: `threadkeeper/db.py` (SCHEMA at :396-402, `CURRENT_SCHEMA_VERSION` at :28, `_run_schema_migrations` at :672-686)
- Modify: `threadkeeper/embeddings.py:361-390` (`_fts_search`)
- Modify: `threadkeeper/ingest.py` (`_backfill_dialog_fts_if_empty` at :141-201, manual FTS insert at :441-447)
- Modify: `threadkeeper/retention.py:60-66` (`_delete_dialog_sidecars`)
- Test (new): `tests/test_dialog_fts_external.py`
- Test (new): `tests/test_db_migration_fts.py`
- Test (modify): `tests/test_retention.py`, `tests/test_ingest_secret_redaction.py`

**Interfaces:**
- Consumes: existing `db.SCHEMA`, `_iter_sql_statements`, `_set_user_version`, `ingest._scrub_dialog_secrets`, `config.REDACT_DIALOG_SECRETS`.
- Produces: v2 schema + triggers; `db._drop_legacy_dialog_fts(conn)`, `db._scrub_legacy_dialog_rows(conn)`, `db._rebuild_dialog_fts_if_needed(conn)`; the rowid-join search SQL shape (Task 3's test and Task 4's rehearsal rely on it).

- [ ] **Step 1: Write the failing schema/trigger/backfill tests**

Create `tests/test_dialog_fts_external.py`:

```python
"""dialog_fts v2: external-content FTS5 shape, trigger sync, rebuild backfill."""
from __future__ import annotations


def _insert_msg(conn, uuid: str, content: str) -> None:
    conn.execute(
        "INSERT INTO dialog_messages "
        "(uuid, source, project, session_id, role, content, model, created_at) "
        "VALUES (?, 'pytest', 'proj', 'sess', 'user', ?, NULL, 1800000000)",
        (uuid, content),
    )


def _match_uuids(conn, term: str) -> list[str]:
    rows = conn.execute(
        "SELECT d.uuid FROM dialog_fts f "
        "JOIN dialog_messages d ON d.rowid = f.rowid "
        "WHERE dialog_fts MATCH ? ORDER BY rank",
        (term,),
    ).fetchall()
    return [r["uuid"] if hasattr(r, "keys") else r[0] for r in rows]


def test_fresh_schema_is_external_content(fresh_mp):
    conn = fresh_mp["db"].get_db()
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='dialog_fts'"
    ).fetchone()[0]
    assert "content='dialog_messages'" in ddl
    assert "content_rowid='rowid'" in ddl
    # content-storing shadow copy must not exist on a fresh install
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='dialog_fts_content'"
    ).fetchone() is None
    triggers = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'dialog_fts_%'"
        ).fetchall()
    }
    assert triggers == {"dialog_fts_ai", "dialog_fts_ad", "dialog_fts_au"}


def test_fresh_db_starts_at_v2(fresh_mp):
    conn = fresh_mp["db"].get_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_trigger_insert_makes_row_searchable(fresh_mp):
    conn = fresh_mp["db"].get_db()
    _insert_msg(conn, "m-ins", "zebra crossing procedure")
    conn.commit()
    assert _match_uuids(conn, "zebra") == ["m-ins"]


def test_trigger_update_reindexes(fresh_mp):
    conn = fresh_mp["db"].get_db()
    _insert_msg(conn, "m-upd", "original giraffe text")
    conn.execute(
        "UPDATE dialog_messages SET content='replacement kangaroo text' "
        "WHERE uuid='m-upd'"
    )
    conn.commit()
    assert _match_uuids(conn, "giraffe") == []
    assert _match_uuids(conn, "kangaroo") == ["m-upd"]


def test_trigger_delete_removes_from_index(fresh_mp):
    conn = fresh_mp["db"].get_db()
    _insert_msg(conn, "m-del", "ephemeral walrus entry")
    conn.execute("DELETE FROM dialog_messages WHERE uuid='m-del'")
    conn.commit()
    assert _match_uuids(conn, "walrus") == []
    assert conn.execute("SELECT COUNT(*) FROM dialog_fts").fetchone()[0] == 0


def test_backfill_rebuilds_empty_index(fresh_mp):
    from threadkeeper import ingest

    conn = fresh_mp["db"].get_db()
    for i in range(8):
        _insert_msg(conn, f"m-bf-{i}", f"backfill payload number{i} common")
    # wipe the index (rows stay in dialog_messages) — simulates a restored
    # DB / failed rebuild; counts now diverge by > 5
    conn.execute("INSERT INTO dialog_fts(dialog_fts) VALUES('delete-all')")
    conn.commit()
    assert _match_uuids(conn, "common") == []

    ingest._backfill_dialog_fts_if_empty(conn)

    assert len(_match_uuids(conn, "common")) == 8
    row = conn.execute(
        "SELECT value FROM style WHERE key='fts_backfilled'"
    ).fetchone()
    assert row is not None and row[0] == "8"


def test_backfill_noop_when_in_sync(fresh_mp):
    from threadkeeper import ingest

    conn = fresh_mp["db"].get_db()
    _insert_msg(conn, "m-sync", "already indexed muskox")
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM dialog_fts").fetchone()[0]
    ingest._backfill_dialog_fts_if_empty(conn)
    after = conn.execute("SELECT COUNT(*) FROM dialog_fts").fetchone()[0]
    assert before == after == 1
```

Create `tests/test_db_migration_fts.py`:

```python
"""v1→v2 migration: content-storing dialog_fts → external-content."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Frozen v1 DDL (historical fact — do not "fix" to match current SCHEMA).
_V1_DIALOG_MESSAGES = """
CREATE TABLE dialog_messages (
    uuid         TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    project      TEXT,
    session_id   TEXT,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    model        TEXT,
    created_at   INTEGER NOT NULL,
    embedding    BLOB,
    embed_backend TEXT
);
"""
_V1_DIALOG_FTS = """
CREATE VIRTUAL TABLE dialog_fts USING fts5(
    uuid UNINDEXED,
    content
);
"""

_RAW_TOKEN = "c" * 24  # body of a fake GitHub token, a single FTS token
_RAW_SECRET = "ghp_" + _RAW_TOKEN


def _seed_v1(db_path: Path, *, scrubbed_fts: bool = True) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_V1_DIALOG_MESSAGES + _V1_DIALOG_FTS)
    rows = [
        ("v1-plain-1", "ordinary aardvark message"),
        ("v1-plain-2", "another aardvark note about retention"),
        ("v1-secret", f"my token is {_RAW_SECRET} keep it safe"),
    ]
    for uuid, content in rows:
        conn.execute(
            "INSERT INTO dialog_messages "
            "(uuid, source, project, session_id, role, content, model, created_at) "
            "VALUES (?, 'pytest', 'proj', 'sess', 'user', ?, NULL, 1800000000)",
            (uuid, content),
        )
        fts_content = content
        if scrubbed_fts and uuid == "v1-secret":
            # v1 behavior: the FTS copy was scrubbed, dialog_messages kept raw
            fts_content = "my token is [REDACTED:GITHUB_TOKEN] keep it safe"
        conn.execute(
            "INSERT INTO dialog_fts (uuid, content) VALUES (?, ?)",
            (uuid, fts_content),
        )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()


def _boot_db(monkeypatch, db_path: Path):
    """Import threadkeeper fresh against a pre-seeded DB file."""
    monkeypatch.setenv("THREADKEEPER_DB", str(db_path))
    # isolate from the user's real ~/.threadkeeper/.env (pydantic env_file
    # is resolved from THREADKEEPER_ENV_FILE at config import time)
    monkeypatch.setenv("THREADKEEPER_ENV_FILE", str(db_path.parent / "empty.env"))
    monkeypatch.setenv("THREADKEEPER_DISABLE_BG_DAEMONS", "1")
    monkeypatch.setenv("THREADKEEPER_INGEST_INTERVAL_S", "0")
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    from threadkeeper import db as tk_db

    return tk_db


def _match_uuids(conn, term: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT d.uuid FROM dialog_fts f "
            "JOIN dialog_messages d ON d.rowid = f.rowid "
            "WHERE dialog_fts MATCH ? ORDER BY rank",
            (term,),
        ).fetchall()
    ]


def test_migrates_v1_to_external_content(tmp_path, monkeypatch):
    db_path = tmp_path / "v1.sqlite"
    _seed_v1(db_path)
    tk_db = _boot_db(monkeypatch, db_path)
    conn = tk_db.get_db()

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='dialog_fts'"
    ).fetchone()[0]
    assert "content='dialog_messages'" in ddl
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='dialog_fts_content'"
    ).fetchone() is None
    # search parity: same rows still match
    assert set(_match_uuids(conn, "aardvark")) == {"v1-plain-1", "v1-plain-2"}
    # all three rows are indexed
    assert conn.execute("SELECT COUNT(*) FROM dialog_fts").fetchone()[0] == 3
    conn.close()


def test_migration_scrubs_legacy_secret_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "v1.sqlite"
    _seed_v1(db_path)
    monkeypatch.setenv("THREADKEEPER_REDACT_DIALOG_SECRETS", "1")
    tk_db = _boot_db(monkeypatch, db_path)
    conn = tk_db.get_db()

    content = conn.execute(
        "SELECT content FROM dialog_messages WHERE uuid='v1-secret'"
    ).fetchone()[0]
    assert _RAW_SECRET not in content
    assert "[REDACTED:GITHUB_TOKEN]" in content
    # v1 kept raw secrets out of the FTS index; v2 must preserve that
    assert _match_uuids(conn, _RAW_TOKEN) == []
    assert _match_uuids(conn, "REDACTED") == ["v1-secret"]
    conn.close()


def test_migration_scrub_respects_disable_knob(tmp_path, monkeypatch):
    db_path = tmp_path / "v1.sqlite"
    _seed_v1(db_path, scrubbed_fts=False)
    monkeypatch.setenv("THREADKEEPER_REDACT_DIALOG_SECRETS", "0")
    tk_db = _boot_db(monkeypatch, db_path)
    conn = tk_db.get_db()

    content = conn.execute(
        "SELECT content FROM dialog_messages WHERE uuid='v1-secret'"
    ).fetchone()[0]
    assert _RAW_SECRET in content  # untouched when redaction is off
    conn.close()


def test_migration_rerun_is_noop(tmp_path, monkeypatch):
    db_path = tmp_path / "v1.sqlite"
    _seed_v1(db_path)
    tk_db = _boot_db(monkeypatch, db_path)
    conn = tk_db.get_db()
    counts1 = (
        conn.execute("SELECT COUNT(*) FROM dialog_messages").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM dialog_fts").fetchone()[0],
    )
    conn.close()

    conn2 = tk_db.get_db()  # second connect: version already 2 → fast path
    assert conn2.execute("PRAGMA user_version").fetchone()[0] == 2
    counts2 = (
        conn2.execute("SELECT COUNT(*) FROM dialog_messages").fetchone()[0],
        conn2.execute("SELECT COUNT(*) FROM dialog_fts").fetchone()[0],
    )
    assert counts1 == counts2 == (3, 3)
    conn2.close()


def test_pre_versioning_db_with_old_fts_migrates(tmp_path, monkeypatch):
    """user_version=0 DB that already HAS tables (pre-versioning install):
    the shape-driven drop must still convert dialog_fts."""
    db_path = tmp_path / "v0.sqlite"
    _seed_v1(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()

    tk_db = _boot_db(monkeypatch, db_path)
    conn = tk_db.get_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='dialog_fts'"
    ).fetchone()[0]
    assert "content='dialog_messages'" in ddl
    assert set(_match_uuids(conn, "aardvark")) == {"v1-plain-1", "v1-plain-2"}
    conn.close()
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_dialog_fts_external.py tests/test_db_migration_fts.py -x -q`
Expected: FAIL — first failure in `test_fresh_schema_is_external_content` (`content='dialog_messages'` not in DDL).

- [ ] **Step 3: Change the schema in `threadkeeper/db.py`**

3a. Line 28: `CURRENT_SCHEMA_VERSION = 1` → `CURRENT_SCHEMA_VERSION = 2`

3b. Replace the `dialog_fts` block (lines 396-402):

```sql
-- Reciprocal-rank-fusion-friendly FTS over dialog content. External-content
-- FTS5 (schema v2): the index reads text straight from dialog_messages by
-- rowid — no stored duplicate (v1's content-storing mirror duplicated
-- ~465MB). Result→message mapping is dialog_fts.rowid ==
-- dialog_messages.rowid (implicit rowid; PK is TEXT uuid). That rowid is
-- NOT stable across VACUUM — any VACUUM must be followed by an FTS rebuild
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
```

(`_iter_sql_statements` uses `sqlite3.complete_statement`, which understands `CREATE TRIGGER ... BEGIN ... END;` bodies — mid-trigger semicolons do not split statements.)

3c. Add three helpers directly above `_run_schema_migrations` (after `_backfill_schema_data`, ~line 670):

```python
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
    re-reads every dialog_messages row (one-time; ~285K rows on the live DB)."""
    msg = conn.execute("SELECT COUNT(*) FROM dialog_messages").fetchone()[0]
    if not msg:
        return
    fts = conn.execute("SELECT COUNT(*) FROM dialog_fts").fetchone()[0]
    if fts:
        return
    conn.execute("INSERT INTO dialog_fts(dialog_fts) VALUES('rebuild')")
```

3d. Rewrite `_run_schema_migrations` (line 672):

```python
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
```

- [ ] **Step 4: Update the consumer sites**

4a. `threadkeeper/embeddings.py` — replace the query inside `_fts_search` (lines 371-377) and touch the docstring:

```python
def _fts_search(conn: sqlite3.Connection, query: str,
                k: int) -> list[dict]:
    """FTS5 search over dialog_fts joined to dialog_messages. FTS5 ranks
    by BM25 (lower = better); we keep insertion order from the result for
    RRF (already ranked best-first by FTS5). dialog_fts is external-content
    (schema v2): rows map back via dialog_fts.rowid == dialog_messages.rowid."""
    from .helpers import _fts_query
    fq = _fts_query(query)
    if not fq:
        return []
    try:
        rows = conn.execute(
            "SELECT d.uuid, d.role, d.session_id, d.content, d.created_at "
            "FROM dialog_fts f "
            "JOIN dialog_messages d ON d.rowid = f.rowid "
            "WHERE dialog_fts MATCH ? ORDER BY rank LIMIT ?",
            (fq, max(1, int(k))),
        ).fetchall()
    except sqlite3.OperationalError:
        # FTS reserved-char syntax error or table missing
        return []
    return [
        {
            "uuid": r["uuid"],
            "role": r["role"],
            "session_id": r["session_id"],
            "content": r["content"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
```

4b. `threadkeeper/ingest.py` — delete the manual FTS insert in `_ingest_file` (lines 441-447). The block to remove, right after the `INSERT INTO dialog_messages` execute:

```python
            try:
                conn.execute(
                    "INSERT INTO dialog_fts (uuid, content) VALUES (?, ?)",
                    (nm.uuid, text),
                )
            except sqlite3.OperationalError:
                pass
```

(The `AFTER INSERT` trigger indexes the row when `dialog_messages` is written.)

4c. `threadkeeper/ingest.py` — replace the whole `_backfill_dialog_fts_if_empty` body (lines 141-201):

```python
def _backfill_dialog_fts_if_empty(conn: sqlite3.Connection) -> None:
    """Safety net: repopulate the external-content dialog_fts index when it
    is meaningfully behind dialog_messages (a restored DB, a wiped index).
    Day-to-day sync is owned by the dialog_fts_* triggers; the v1→v2
    migration does the initial rebuild. FTS5 'rebuild' re-reads every
    dialog_messages.content row. Always refreshes the fts_backfilled style
    key that brief() surfaces."""
    try:
        msg_cnt = conn.execute(
            "SELECT COUNT(*) c FROM dialog_messages"
        ).fetchone()["c"]
        fts_cnt = conn.execute(
            "SELECT COUNT(*) c FROM dialog_fts"
        ).fetchone()["c"]
    except sqlite3.OperationalError:
        return
    if fts_cnt < msg_cnt - 5:
        try:
            conn.execute("INSERT INTO dialog_fts(dialog_fts) VALUES('rebuild')")
        except sqlite3.OperationalError:
            return
        fts_cnt = conn.execute(
            "SELECT COUNT(*) c FROM dialog_fts"
        ).fetchone()["c"]
    conn.execute(
        "INSERT INTO style (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        ("fts_backfilled", str(fts_cnt), int(time.time())),
    )
    conn.commit()
```

(If `_scrub_dialog_secrets` becomes unused in ingest.py after this — it does NOT: `_ingest_file` still calls it at line 429. Leave it.)

4d. `threadkeeper/retention.py` — in `_delete_dialog_sidecars` (line 60), remove:

```python
    ph = _placeholders(len(uuids))
    try:
        conn.execute(f"DELETE FROM dialog_fts WHERE uuid IN ({ph})", uuids)
    except sqlite3.OperationalError:
        pass
```

and replace with:

```python
    ph = _placeholders(len(uuids))
    # dialog_fts is external-content and trigger-synced (schema v2): the
    # AFTER DELETE trigger on dialog_messages removes the FTS entry when
    # _prune_dialog deletes the row itself. Only vec sidecars need manual
    # cleanup here (vectors have no trigger).
```

(`ph` is still used by the vec_map queries below — keep it.)

- [ ] **Step 5: Update the two existing test files that assume the v1 shape**

5a. `tests/test_retention.py` — `_insert_dialog` (line 24) loses the manual FTS insert:

```python
def _insert_dialog(conn, uuid: str, created_at: int) -> None:
    conn.execute(
        "INSERT INTO dialog_messages "
        "(uuid, source, project, session_id, role, content, model, created_at) "
        "VALUES (?, 'pytest', 'proj', 'sess', 'user', ?, NULL, ?)",
        (uuid, f"content {uuid}", created_at),
    )
    # dialog_fts is populated by the AFTER INSERT trigger (schema v2)
```

Add a helper next to `_count`:

```python
def _fts_has(conn, uuid: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM dialog_fts f "
        "JOIN dialog_messages d ON d.rowid = f.rowid WHERE d.uuid=?",
        (uuid,),
    ).fetchone()
    return row is not None
```

Replace the uuid-column asserts:
- line 72: `assert _count(conn, "dialog_fts", "uuid='old-default'") == 1` → `assert _fts_has(conn, "old-default")`
- line 95: `assert _count(conn, "dialog_fts", "uuid='new-a'") == 1` → `assert _fts_has(conn, "new-a")`
- lines 94 and 109 (`_count(conn, "dialog_fts") == 1`) stay as they are — plain `COUNT(*)` works on external-content tables.

5b. `tests/test_ingest_secret_redaction.py`:

`_ingest_one` is called by both the scrubbed test and `test_ingest_secret_redaction_can_be_disabled` (content NOT scrubbed), so the FTS assertions take an `expect_scrubbed` flag. Replace the whole `_ingest_one` (lines 64-95) with:

```python
def _ingest_one(pkg: dict, tmp_path: Path, content: str,
                expect_scrubbed: bool = True) -> str:
    from threadkeeper import ingest

    ingest.SEMANTIC_AVAILABLE = False
    conn = pkg["db"].get_db()
    fp = _touch_transcript(tmp_path)
    adapter = _FakeAdapter(
        [
            NormalizedMessage(
                uuid="msg-secret-1",
                session_id="sess-redact",
                role="user",
                content=content,
                model="",
                created_at=1_800_000_000,
                raw={"role": "user", "content": content},
            )
        ]
    )
    added = ingest._ingest_file(conn, fp, max_msgs=100, adapter=adapter)
    conn.commit()
    assert added == 1
    row = conn.execute(
        "SELECT content FROM dialog_messages WHERE uuid='msg-secret-1'"
    ).fetchone()
    assert row is not None
    # external-content FTS (schema v2): the index reads dialog_messages
    # directly. Scrubbed ⇒ raw token not MATCH-able; unscrubbed ⇒ it is.
    raw_token_hit = conn.execute(
        "SELECT 1 FROM dialog_fts WHERE dialog_fts MATCH ?",
        ('"' + "c" * 24 + '"',),   # ghp_ token body from _secret_values()
    ).fetchone()
    if expect_scrubbed:
        assert raw_token_hit is None
        assert conn.execute(
            "SELECT 1 FROM dialog_fts WHERE dialog_fts MATCH 'REDACTED'"
        ).fetchone() is not None
    else:
        assert raw_token_hit is not None
    return row["content"]
```

and in `test_ingest_secret_redaction_can_be_disabled` change the call to
`persisted = _ingest_one(fresh_mp, tmp_path, content, expect_scrubbed=False)`.

Delete `test_fts_backfill_scrubs_legacy_dialog_rows` entirely (lines 130-153): its property — "backfill writes a scrubbed FTS copy that diverges from dialog_messages" — is unimplementable by construction under external-content FTS. Its intent (legacy raw secrets are not searchable) now lives in `tests/test_db_migration_fts.py::test_migration_scrubs_legacy_secret_rows`, and the backfill-rebuild path is covered by `tests/test_dialog_fts_external.py::test_backfill_rebuilds_empty_index`.

- [ ] **Step 6: Run the new tests, then every FTS-touching test**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_dialog_fts_external.py tests/test_db_migration_fts.py tests/test_retention.py tests/test_ingest_secret_redaction.py -q`
Expected: ALL PASS.

Run the search/dashboard neighbors (these pin the spec's "existing FTS tests stay green" criterion):
`PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_delegated_search.py tests/test_search_fts_punctuation.py tests/test_vec_search.py tests/test_dashboard.py -q`
Expected: ALL PASS.

- [ ] **Step 7: Full suite**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest --forked -q`
Expected: PASS (same count as main, plus the new tests; zero failures). If `-q --forked` prints no summary line, re-run the failing subset without `--forked` to read it.

- [ ] **Step 8: Commit**

```bash
git add threadkeeper/db.py threadkeeper/embeddings.py threadkeeper/ingest.py threadkeeper/retention.py tests/test_dialog_fts_external.py tests/test_db_migration_fts.py tests/test_retention.py tests/test_ingest_secret_redaction.py
git commit -m "feat: dialog_fts → external-content FTS5 (schema v2, −465MB duplicate)"
```

---

### Task 2: `_ensure_schema` waits out a concurrent migration

The v1→v2 rebuild on the live 2.76 GB DB holds the write lock for minutes. Every other process calls `get_db()` → `_ensure_schema` → `BEGIN IMMEDIATE`, which today dies with `OperationalError` after the 10 s busy timeout — a crash-loop window for every other CLI session during the one-time migration. Make waiters poll patiently until the migrating process commits `user_version=2`.

**Files:**
- Modify: `threadkeeper/db.py` (`_ensure_schema`, line 689)
- Test: `tests/test_db_migration_fts.py` (append)

**Interfaces:**
- Consumes: `_user_version`, `_run_schema_migrations` from Task 1.
- Produces: `_ensure_schema(conn, wait_s: float = 600.0)` — signature used by the test; `get_db()` keeps calling it with defaults.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_db_migration_fts.py`:

```python
def test_ensure_schema_waits_for_concurrent_migration(tmp_path, monkeypatch):
    """While another process holds the write lock mid-migration, get_db()'s
    schema check must wait and succeed once user_version reaches CURRENT —
    not die on the busy timeout."""
    import threading
    import time as _time

    db_path = tmp_path / "wait.sqlite"
    _seed_v1(db_path)
    tk_db = _boot_db(monkeypatch, db_path)

    # simulate the migrating process: hold BEGIN IMMEDIATE
    holder = sqlite3.connect(str(db_path), timeout=1.0)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")

    waiter_conn = sqlite3.connect(str(db_path), timeout=1.0)
    waiter_conn.execute("PRAGMA busy_timeout=200")
    waiter_conn.row_factory = sqlite3.Row
    errors: list[BaseException] = []

    def _wait():
        try:
            tk_db._ensure_schema(waiter_conn, wait_s=30.0)
        except BaseException as e:  # noqa: BLE001 — surface into the test
            errors.append(e)

    t = threading.Thread(target=_wait, daemon=True)
    t.start()
    _time.sleep(1.0)
    assert t.is_alive()  # still waiting, not crashed

    # the "migrating" process finishes: version → CURRENT, lock released
    holder.execute(f"PRAGMA user_version = {tk_db.CURRENT_SCHEMA_VERSION}")
    holder.commit()
    holder.close()

    t.join(timeout=15.0)
    assert not t.is_alive()
    assert errors == []
    waiter_conn.close()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_db_migration_fts.py::test_ensure_schema_waits_for_concurrent_migration -x -q`
Expected: FAIL — either `TypeError: _ensure_schema() got an unexpected keyword argument 'wait_s'` or `errors` contains an `OperationalError`.

- [ ] **Step 3: Implement the waiter**

In `threadkeeper/db.py`, add `import time` to the imports (top of file, stdlib group), then replace `_ensure_schema`:

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_db_migration_fts.py -q`
Expected: ALL PASS (whole file, including Task 1's tests).

- [ ] **Step 5: Quick regression on db-touching suites**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_dialog_fts_external.py tests/test_retention.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add threadkeeper/db.py tests/test_db_migration_fts.py
git commit -m "feat: get_db waits out a concurrent schema migration instead of dying"
```

---

### Task 3: `db_compact()` — opt-in VACUUM + mandatory FTS rebuild

Dropping the shadow table frees pages inside the file; only `VACUUM` shrinks the file on disk. `VACUUM` renumbers `dialog_messages`' implicit rowids, which silently desyncs the external-content index — so the tool always rebuilds right after, on the same connection, under a single-flight lock.

**Files:**
- Create: `threadkeeper/tools/db_maintenance.py`
- Modify: `threadkeeper/server.py` (one import line, after `from .tools import config_watch`)
- Test: `tests/test_db_compact.py`

**Interfaces:**
- Consumes: `get_db`, `_ensure_session`, `single_flight_lock(name)` (locks in `DB_PATH.parent` by default → test-isolated under `fresh_mp`), `config.DB_PATH`, `@write_tool` from `.._mcp`.
- Produces: MCP tool `db_compact() -> str` (registered name: `db_compact`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_db_compact.py`:

```python
"""db_compact(): VACUUM + mandatory dialog_fts rebuild (opt-in reclaim)."""
from __future__ import annotations


def _tool(pkg, name: str):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _insert_msg(conn, uuid: str, content: str) -> None:
    conn.execute(
        "INSERT INTO dialog_messages "
        "(uuid, source, project, session_id, role, content, model, created_at) "
        "VALUES (?, 'pytest', 'proj', 'sess', 'user', ?, NULL, 1800000000)",
        (uuid, content),
    )


def _match_uuids(conn, term: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT d.uuid FROM dialog_fts f "
            "JOIN dialog_messages d ON d.rowid = f.rowid "
            "WHERE dialog_fts MATCH ? ORDER BY rank",
            (term,),
        ).fetchall()
    ]


def test_db_compact_registered(fresh_mp):
    assert "db_compact" in fresh_mp["mcp"]._tool_manager._tools


def test_db_compact_survives_rowid_renumbering(fresh_mp):
    """THE regression guard for the external-content trap: VACUUM renumbers
    dialog_messages' implicit rowids; without the rebuild the index maps
    MATCHes to wrong/missing rows. After db_compact, every row must still
    resolve to its correct uuid."""
    conn = fresh_mp["db"].get_db()
    _insert_msg(conn, "m-1", "first pelican entry")
    _insert_msg(conn, "m-2", "second toucan entry")
    _insert_msg(conn, "m-3", "third condor entry")
    # deleting the lowest rowid creates the gap VACUUM will compact away,
    # shifting m-2/m-3 onto new rowids
    conn.execute("DELETE FROM dialog_messages WHERE uuid='m-1'")
    conn.commit()
    conn.close()

    out = _tool(fresh_mp, "db_compact")()
    assert out.startswith("ok"), out

    conn = fresh_mp["db"].get_db()
    assert _match_uuids(conn, "toucan") == ["m-2"]
    assert _match_uuids(conn, "condor") == ["m-3"]
    assert _match_uuids(conn, "pelican") == []
    assert conn.execute("SELECT COUNT(*) FROM dialog_fts").fetchone()[0] == 2
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_db_compact_single_flight(fresh_mp):
    from threadkeeper.helpers import single_flight_lock

    with single_flight_lock("db-compact") as locked:
        assert locked
        out = _tool(fresh_mp, "db_compact")()
    assert "already running" in out
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_db_compact.py -x -q`
Expected: FAIL — `KeyError: 'db_compact'` in `test_db_compact_registered`.

- [ ] **Step 3: Implement the tool**

Create `threadkeeper/tools/db_maintenance.py`:

```python
"""One-shot DB maintenance: reclaim disk after the schema-v2 FTS dedup.

Dropping v1's dialog_fts shadow copy freed ~465MB of pages INSIDE the file;
the file itself shrinks only on VACUUM. VACUUM takes an exclusive lock and
renumbers dialog_messages' implicit rowids — which desyncs the
external-content dialog_fts index — so this is deliberately an explicit,
operator-run tool (never an automatic pass) and the FTS rebuild after
VACUUM is mandatory, not optional."""
from __future__ import annotations

import sqlite3
import time

from .._mcp import write_tool
from ..config import DB_PATH
from ..db import get_db
from ..helpers import single_flight_lock
from ..identity import _ensure_session


@write_tool(idempotent=True)
def db_compact() -> str:
    """Shrink the DB file: VACUUM + mandatory dialog_fts rebuild.

    Run in a quiet window — VACUUM needs an exclusive lock and copies the
    whole file (minutes on a multi-GB DB); concurrent FTS searches during
    the vacuum→rebuild gap may map to wrong rows until the rebuild commits.
    Fails soft (with a retry hint) when the DB is busy."""
    with single_flight_lock("db-compact") as locked:
        if not locked:
            return "db_compact already running (single-flight lock held)"
        conn = get_db()
        try:
            _ensure_session(conn)
            before = DB_PATH.stat().st_size
            t0 = time.time()
            conn.commit()  # VACUUM cannot run inside a transaction
            try:
                conn.execute("VACUUM")
            except sqlite3.OperationalError as e:
                return (
                    f"vacuum skipped: {e} — DB busy; retry in a quiet window "
                    f"(no rowids were changed, index is still consistent)"
                )
            # MANDATORY: VACUUM renumbered dialog_messages' implicit rowids;
            # the external-content index maps by rowid and is now stale.
            conn.execute("INSERT INTO dialog_fts(dialog_fts) VALUES('rebuild')")
            conn.commit()
            after = DB_PATH.stat().st_size
            return (
                f"ok vacuum+fts_rebuild {time.time() - t0:.1f}s "
                f"size {before / 1e6:.0f}MB -> {after / 1e6:.0f}MB "
                f"(freed {(before - after) / 1e6:.0f}MB)"
            )
        finally:
            conn.close()
```

Add to `threadkeeper/server.py` after `from .tools import config_watch  # noqa: F401`:

```python
from .tools import db_maintenance  # noqa: F401
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_db_compact.py -q`
Expected: ALL PASS.

- [ ] **Step 5: Tool-surface regression**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_tools_smoke.py tests/test_dashboard.py -q`
Expected: PASS. Note: `test_tools_smoke.py` auto-invokes EVERY registered tool with dummy args — `db_compact` is intentionally NOT added to its `_NO_INVOKE` set. It must survive a bare call on the tiny fixture DB: VACUUM succeeds (or returns the soft `vacuum skipped: ...` string on a busy DB) and never raises. No tool-inventory list pins exact names/counts (`test_all_tools_registered` checks a must-have subset only), so no inventory edit is needed.

- [ ] **Step 6: Commit**

```bash
git add threadkeeper/tools/db_maintenance.py threadkeeper/server.py tests/test_db_compact.py
git commit -m "feat: db_compact() — opt-in VACUUM + mandatory dialog_fts rebuild"
```

---

### Task 4: Docs sync, full suite, live-DB migration rehearsal

Docs move in the same branch as the behavior change (docs_sync_with_features). The rehearsal runs the real v1→v2 migration against a throwaway COPY of the live 2.76 GB DB — timing it and proving search parity + the compaction win before this ever touches the real file.

**Files:**
- Modify: `docs/ARCHITECTURE.md` (lines ~168-178, ~1461), `README.md` (lines ~930, ~990, ~1113 area, ~1184-1191), `CHANGELOG.md` (Unreleased → Added)
- No code changes.

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces: rehearsal numbers (migration wall-time, db_compact wall-time, size before/after) — paste into the task report; they go into the PR body.

- [ ] **Step 1: ARCHITECTURE.md — storage tier description (lines 168-178)**

Replace the sentence `The retention pass can prune aged dialog rows and deletes their FTS/vec mirrors in the same pass.` with:

```
The retention pass can prune aged dialog rows; `dialog_fts` follows
automatically (external-content FTS5, trigger-synced — schema v2), the vec
sidecars are deleted in the same pass.
```

Replace `Before new transcript content is written to `dialog_messages`, mirrored into `dialog_fts`, embedded, or backfilled into FTS, ingest masks common credential-shaped values` with:

```
Before new transcript content is written to `dialog_messages` (which the
external-content `dialog_fts` indexes directly — no stored copy), embedded,
or rebuilt into FTS, ingest masks common credential-shaped values
```

At line ~1461 (`THREADKEEPER_DIALOG_RETENTION_DAYS` row), change `prune old dialog rows plus `dialog_fts` / `dialog_vec` mirrors; 0 keeps forever` to `prune old dialog rows (FTS entries follow via trigger) plus `dialog_vec` sidecars; 0 keeps forever`.

- [ ] **Step 2: README.md**

Line ~930 (`THREADKEEPER_DIALOG_RETENTION_DAYS`): `prune aged `dialog_messages` plus `dialog_fts` / `dialog_vec` mirrors; 0 keeps forever` → `prune aged `dialog_messages` (their FTS entries follow via trigger) plus `dialog_vec` sidecars; 0 keeps forever`.

Line ~990 (`THREADKEEPER_REDACT_DIALOG_SECRETS`): append to the purpose cell: `; the v2 schema migration also scrubs legacy pre-redaction rows in place`.

Line ~1184-1191 (retention/dashboard paragraph): after the `mp_dashboard()` sentence, add a new paragraph:

```
`db_compact()` is the opt-in disk-reclaim tool: `VACUUM` + a mandatory
`dialog_fts` rebuild (schema v2 keys the FTS index on `dialog_messages`
rowids, which `VACUUM` renumbers — the rebuild is what keeps search
correct). Run it once in a quiet window after upgrading to the v2 schema
to shrink the DB file by roughly the old FTS shadow copy (~465 MB on a
2.7 GB DB); day-to-day it is never required.
```

In the tools reference near line ~1113 (next to the `mp_dashboard(window_days=7)` bullet), add a bullet:

```
- **`db_compact()`** — one-shot maintenance: `VACUUM` the SQLite file and
  rebuild `dialog_fts` (mandatory after VACUUM — rowid renumbering).
  Single-flight; fails soft with a retry hint when the DB is busy.
```

- [ ] **Step 3: CHANGELOG.md — Unreleased → Added (top of the Added list)**

```markdown
- **`dialog_fts` deduplicated: external-content FTS5 (schema v2).** The FTS
  index now reads text straight from `dialog_messages` instead of storing
  its own ~465 MB copy; three triggers own the sync (ingest and retention
  no longer write FTS rows manually). One-time startup migration drops the
  old table, scrubs legacy pre-redaction secret rows in place, and rebuilds
  the index; concurrent sessions wait for the migration instead of dying on
  the busy timeout. New opt-in `db_compact()` tool reclaims the freed pages
  (`VACUUM` + mandatory FTS rebuild — `VACUUM` renumbers the implicit
  rowids the index is keyed on). Search results and ranking are unchanged.
```

- [ ] **Step 4: Full suite**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest --forked -q`
Expected: PASS, zero failures.

- [ ] **Step 5: Live-DB migration rehearsal (throwaway copy — NEVER the real file)**

```bash
SCRATCH="${TMPDIR:-/tmp}/tk-mig-rehearsal"; mkdir -p "$SCRATCH"
df -h "$SCRATCH"   # need ~6 GB free (2.8 GB copy + WAL + VACUUM temp)
cp ~/.threadkeeper/db.sqlite "$SCRATCH/copy.sqlite"
# pre-migration baseline on the COPY (v1 query shape):
sqlite3 -readonly "$SCRATCH/copy.sqlite" \
  "SELECT COUNT(*) FROM dialog_messages; SELECT COUNT(*) FROM dialog_fts;
   SELECT f.uuid FROM dialog_fts f WHERE dialog_fts MATCH 'retention' ORDER BY rank LIMIT 5;"
# run the migration (times the whole get_db incl. rebuild):
cd /Users/dmytro/ai-memory/.claude/worktrees/loving-bassi-76343e
THREADKEEPER_DB="$SCRATCH/copy.sqlite" THREADKEEPER_DISABLE_BG_DAEMONS=1 \
THREADKEEPER_INGEST_CAP=0 PYTHONPATH="$PWD" \
/usr/bin/time -h /Users/dmytro/ai-memory/.venv/bin/python -c "
from threadkeeper import db
conn = db.get_db()
print('user_version', conn.execute('PRAGMA user_version').fetchone()[0])
print('shadow gone', conn.execute(\"SELECT COUNT(*) FROM sqlite_master WHERE name='dialog_fts_content'\").fetchone()[0] == 0)
print('msgs', conn.execute('SELECT COUNT(*) FROM dialog_messages').fetchone()[0])
print('fts ', conn.execute('SELECT COUNT(*) FROM dialog_fts').fetchone()[0])
print('match', [r[0] for r in conn.execute(\"SELECT d.uuid FROM dialog_fts f JOIN dialog_messages d ON d.rowid=f.rowid WHERE dialog_fts MATCH 'retention' ORDER BY rank LIMIT 5\")])
"
```

Expected: `user_version 2`, `shadow gone True`, `fts` == `msgs`, and `match` returns the SAME uuids as the pre-migration baseline query. Record the wall time (estimate: 1-5 min; the rebuild reads ~1 GB of text).

Then rehearse compaction on the same copy:

```bash
ls -l "$SCRATCH/copy.sqlite"    # size BEFORE (expect ~2.8 GB)
THREADKEEPER_DB="$SCRATCH/copy.sqlite" THREADKEEPER_DISABLE_BG_DAEMONS=1 \
THREADKEEPER_INGEST_CAP=0 PYTHONPATH="$PWD" \
/Users/dmytro/ai-memory/.venv/bin/python -c "
from threadkeeper.tools.db_maintenance import db_compact
print(db_compact())
"
ls -l "$SCRATCH/copy.sqlite"    # size AFTER (expect roughly −450-650 MB)
# post-compact search still correct:
sqlite3 -readonly "$SCRATCH/copy.sqlite" \
  "SELECT d.uuid FROM dialog_fts f JOIN dialog_messages d ON d.rowid=f.rowid WHERE dialog_fts MATCH 'retention' ORDER BY rank LIMIT 5;"
rm -rf "$SCRATCH"
```

Expected: `ok vacuum+fts_rebuild ...` with a real freed-MB number; post-compact MATCH uuids identical to the two earlier runs. Record all numbers in the task report. If `df` shows < 6 GB free, STOP and report instead of filling the disk.

(Note: `db_compact()` calls `_ensure_session`, which writes a presence row into the copy — harmless, it's a throwaway.)

- [ ] **Step 6: Commit**

```bash
git add docs/ARCHITECTURE.md README.md CHANGELOG.md
git commit -m "docs: external-content dialog_fts (schema v2) + db_compact reclaim"
```

---

## Final verification (before PR)

1. `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest --forked -q` — green.
2. Rehearsal numbers from Task 4 recorded (migration time, compact time, MB freed, search parity) — they go into the PR body.
3. `git log --oneline origin/main..HEAD` — spec + plan + 4 task commits, nothing under `.claude/`.
4. Hand off to superpowers:finishing-a-development-branch (PR to main; migration runs automatically at first post-merge startup; operator runs `db_compact()` once in a quiet window).
