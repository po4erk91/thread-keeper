# Phase 2 — FTS storage dedup (external-content FTS5)

**Status:** design / approved shape, pending spec review
**Date:** 2026-07-10
**Roadmap:** audit Phase 2 ("устранение дублей хранения в базе"), scoped to the FTS half.

## Problem

`dialog_fts` is a **content-storing** FTS5 table:

```sql
CREATE VIRTUAL TABLE dialog_fts USING fts5(uuid UNINDEXED, content)
```

FTS5 in this (default) mode keeps its **own copy** of every indexed column in a shadow table `dialog_fts_content`. On the live DB that copy is **~465 MB** — a byte-for-byte duplicate of `dialog_messages.content`, which is already the source of truth (1027 MB, 285 298 rows). The FTS *index* (`dialog_fts_data`, ~172 MB) is not a duplicate — it's the inverted index and stays.

Measured (live `~/.threadkeeper/db.sqlite`, 2.76 GB):
- `dialog_messages` 1027 MB (SoT: uuid, content, embedding BLOB, …)
- `dialog_fts_content` **464.8 MB** ← the duplicate this spec removes
- `dialog_fts_data` 172.5 MB (index, kept)

## Goal / success criteria

1. `dialog_fts` becomes an **external-content** FTS5 table (`content='dialog_messages'`) — no `%_content` shadow copy. Frees ~465 MB of duplicated storage.
2. **Zero durability loss:** the FTS is 100 % derivable from `dialog_messages.content` (which stays), and can be rebuilt at any time.
3. Full-text search returns the same results (same rows, same ranking) via a rowid↔uuid mapping.
4. Migration is safe on the live 2.76 GB DB (transactional; readers see the old schema until commit).
5. Reclaiming the freed space to disk is an **opt-in** maintenance action (no forced exclusive `VACUUM` on the live DB).

**Scope:** only `dialog_fts` (the 465 MB win). NON-goals: the embedding-BLOB dedup (separate phase — it changes vector-store durability); `notes_fts` (also content-storing but the `notes` table is ~6 MB, negligible — same pattern applies later if wanted).

## Design

### 1. Schema — external-content FTS5 + sync triggers (`db.py`)

`dialog_messages` has an implicit integer `rowid` (its PK is `uuid TEXT`, not `INTEGER PRIMARY KEY`, and it is not `WITHOUT ROWID`), so it can back an external-content FTS.

```sql
CREATE VIRTUAL TABLE dialog_fts USING fts5(
    content,
    content='dialog_messages',
    content_rowid='rowid'
);
```

The `uuid UNINDEXED` column is gone; result→message mapping is now `dialog_fts.rowid == dialog_messages.rowid`. External-content FTS5 does **not** auto-sync, so three triggers keep the index current:

```sql
CREATE TRIGGER dialog_fts_ai AFTER INSERT ON dialog_messages BEGIN
    INSERT INTO dialog_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER dialog_fts_ad AFTER DELETE ON dialog_messages BEGIN
    INSERT INTO dialog_fts(dialog_fts, rowid, content) VALUES('delete', old.rowid, old.content);
END;
CREATE TRIGGER dialog_fts_au AFTER UPDATE ON dialog_messages BEGIN
    INSERT INTO dialog_fts(dialog_fts, rowid, content) VALUES('delete', old.rowid, old.content);
    INSERT INTO dialog_fts(rowid, content) VALUES (new.rowid, new.content);
END;
```

Triggers are chosen over manual sync because they (a) are the idiomatic external-content pattern, (b) *remove* the three hand-written `INSERT INTO dialog_fts` sites from ingest and the delete from retention, and (c) guarantee sync even from a code path that forgets. A fresh install creates this schema directly.

### 2. Migration v1 → v2 (`db.py`, `CURRENT_SCHEMA_VERSION` 1 → 2)

In the version-gated migration path, when `user_version < 2`, inside one transaction:

```sql
DROP TABLE dialog_fts;              -- drops old FTS + dialog_fts_content/_data shadow tables
-- CREATE the external-content dialog_fts + the 3 triggers (above)
INSERT INTO dialog_fts(dialog_fts) VALUES('rebuild');   -- reads all dialog_messages.content
```

The `rebuild` reads the 285 K rows and builds the index once. Under WAL, concurrent readers see the committed v1 state until the migration transaction commits; the write lock is held only for the migration's duration (one-time, at startup — now inside the daemon-host's DB init when the host owns init, else the first server). `_set_user_version(conn, 2)` on success; on failure the version stays < 2 and the migration retries next startup (idempotent: `DROP TABLE` + recreate).

### 3. Code touchpoints (~5 files)

- **`embeddings.py:357-359`** — the FTS search. Today selects the `uuid` column out of `dialog_fts`. New:
  ```sql
  SELECT d.uuid, ...
  FROM dialog_fts f JOIN dialog_messages d ON d.rowid = f.rowid
  WHERE dialog_fts MATCH ? ORDER BY rank LIMIT ?
  ```
- **`ingest.py:179,187,442`** — remove the three manual `INSERT INTO dialog_fts (uuid, content)` — the `AFTER INSERT` trigger now populates FTS when the `dialog_messages` row is written.
- **`ingest.py:140` `_backfill_dialog_fts_if_empty` + `:170` join** — keep the helper as a safety net but simplify it to the external-content idiom: when `COUNT(*) FROM dialog_fts == 0` while `dialog_messages` is non-empty, run `INSERT INTO dialog_fts(dialog_fts) VALUES('rebuild')` instead of the per-row `LEFT JOIN … ON f.uuid=d.uuid` + loop (which references the now-removed `uuid` column). `COUNT(*) FROM dialog_fts` (`:153,:192`) is unchanged (works on external-content).
- **`retention.py:65` (`_delete_dialog_sidecars`)** — this helper prunes the FTS + vec sidecars **by uuid** when dialog rows are pruned (off by default). Its `DELETE FROM dialog_fts WHERE uuid IN (...)` cannot survive the migration (external-content FTS has no `uuid` column). Fix: **drop the FTS delete** from this helper — the `AFTER DELETE` trigger removes the FTS entry when the `dialog_messages` row itself is deleted — and **keep the `dialog_vec_map`/vec cleanup** (vectors have no trigger). Requirement to verify in the plan: the dialog-prune caller must delete the `dialog_messages` row (so the trigger fires); if it deletes sidecars *before* the row, the trigger still fires on the row delete, so order is not load-bearing as long as the row is deleted.
- **`tools/dashboard.py:326`** — `COUNT(*) FROM dialog_fts` unchanged.

### 4. Opt-in reclaim (`VACUUM` + FTS rebuild)

Dropping `dialog_fts_content` frees ~465 MB of pages but does not shrink the 2.76 GB file; the file shrinks only on `VACUUM`, which takes a machine-exclusive lock the daemon-host + live servers can't tolerate on demand. So reclaim is an **explicit, opt-in maintenance action** the operator runs in a quiet window — a new MCP tool `db_compact()` (and/or a `THREADKEEPER_RETENTION_VACUUM` opt-in on the retention pass):

```sql
VACUUM;
INSERT INTO dialog_fts(dialog_fts) VALUES('rebuild');
```

**The FTS rebuild after `VACUUM` is mandatory, not optional.** `VACUUM` renumbers the *implicit* rowids of `dialog_messages` (its PK is `uuid TEXT`, so the rowid is not stable across `VACUUM`). An external-content FTS keyed on `content_rowid='rowid'` would silently desync — searches would map to the wrong or missing rows — until the index is rebuilt against the new rowids. `db_compact()` does both atomically (single connection, `VACUUM` then `rebuild`), guarded by a single-flight lock so two compactions can't overlap. This is precisely why a *forced* `VACUUM` is unsafe here and reclaim is opt-in.

### 5. Testing

- **Migration:** seed a v1 DB (content-storing `dialog_fts` with rows) → run migration → assert: `dialog_fts_content` shadow table is gone, `user_version==2`, the triggers exist, and a MATCH query returns the same uuids/ranking as before.
- **Search:** MATCH returns correct `uuid`s via the `d.rowid=f.rowid` join; ranking preserved.
- **Triggers:** insert a `dialog_messages` row → searchable; update its content → old terms gone, new terms found; delete it → not searchable. (Direct trigger behavior, no ingest.)
- **Fresh install:** a brand-new DB creates the external-content `dialog_fts` + triggers directly (schema v2), no `dialog_fts_content`.
- **Post-VACUUM correctness:** after `db_compact()` (VACUUM renumbers rowids + rebuild), a MATCH still returns the correct uuids — the regression guard for the rowid-desync trap.
- Existing FTS-dependent tests (`test_delegated_search`, `test_search_fts_punctuation`, `test_vec_search`) stay green.

## Risks

- **rowid stability across VACUUM** — the core subtlety; handled by mandatory rebuild-after-VACUUM in `db_compact()` and by never forcing VACUUM in the normal path.
- **Migration on a 2.76 GB live DB** — the rebuild is one-time and transactional; WAL keeps readers on v1 until commit. If a host is mid-write during the migration, SQLite serializes; acceptable for a one-time startup migration.
- **Trigger overhead on bulk ingest** — per-row trigger fire during ingest batches. FTS5 external-content is designed for this; negligible vs the embedding/HTTP costs already in the ingest path.

## Rollout

1. Land the schema + migration + code + `db_compact()` tool. Migration runs automatically at next startup (bumps `user_version` to 2); the dedup takes effect (no new content copy; old `dialog_fts_content` dropped).
2. Operator runs `db_compact()` once in a quiet window to shrink the file ~465 MB.
3. Not flag-gated — the migration is a straight schema improvement with no behavior change to search results (only storage). Reversible only by restoring a pre-migration DB backup; the migration is forward-only (standard for this project's schema migrations).
