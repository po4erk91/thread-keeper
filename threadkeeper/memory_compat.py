"""Durable compatibility gates for model-dependent memory.

Embedding upgrades write an isolated generation here before readers move to it.
The provenance side records pointers and model identity for synthesized memory;
it intentionally never stores a second copy of transcript or artifact text.
"""
from __future__ import annotations

from hashlib import sha256
import json
import sqlite3
import time
from typing import Iterable


_KINDS = (("note", "notes", "id", None),
          ("dialog", "dialog_messages", "uuid", 2000))


def source_text(kind: str, content: str | None) -> str:
    """Return exactly the text contract used for an embedding source."""
    text = content or ""
    return text[:2000] if kind == "dialog" else text


def source_hash(kind: str, content: str | None) -> str:
    """Fingerprint a source without retaining a duplicate of private text."""
    return sha256(source_text(kind, content).encode("utf-8")).hexdigest()


def _legacy_generation(conn: sqlite3.Connection, configured: str) -> str:
    """Pick the most represented existing tagged generation on upgrade.

    Older installations did not have a state pointer. Choosing their dominant
    persisted tag before consulting the new configuration prevents the first
    post-upgrade process from silently declaring a half-built new space active.
    """
    try:
        row = conn.execute(
            "SELECT embed_backend, COUNT(*) AS n FROM ("
            " SELECT embed_backend FROM notes WHERE embed_backend IS NOT NULL "
            "AND instr(embed_backend, ':revision=') > 0"
            " UNION ALL"
            " SELECT embed_backend FROM dialog_messages "
            " WHERE embed_backend IS NOT NULL "
            "AND instr(embed_backend, ':revision=') > 0"
            ") GROUP BY embed_backend ORDER BY n DESC, embed_backend LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    return str(row[0]) if row and row[0] else configured


def generation_state(
    conn: sqlite3.Connection,
    configured: str,
    *,
    ensure: bool = True,
) -> dict[str, object]:
    """Return the durable generation pointer, creating its legacy bridge once."""
    try:
        row = conn.execute(
            "SELECT active_generation, staging_generation, previous_generation, "
            "state, started_at, validated_at, activated_at "
            "FROM embedding_generation_state WHERE id=1"
        ).fetchone()
        if row is None and ensure:
            # Do not use INSERT OR IGNORE on every read: even an ignored INSERT
            # asks SQLite for the writer slot and would make dashboard/search
            # calls contend with a long staged migration.
            conn.execute(
                "INSERT OR IGNORE INTO embedding_generation_state(id) VALUES (1)"
            )
            row = conn.execute(
                "SELECT active_generation, staging_generation, previous_generation, "
                "state, started_at, validated_at, activated_at "
                "FROM embedding_generation_state WHERE id=1"
            ).fetchone()
    except sqlite3.OperationalError:
        # A caller on a deliberately pre-schema test database keeps the old
        # behavior instead of turning a diagnostic into a schema failure.
        return {
            "active_generation": configured, "staging_generation": None,
            "previous_generation": None, "state": "legacy", "started_at": None,
            "validated_at": None, "activated_at": None,
        }
    if row is not None and row[0]:
        return _state_row(row)
    active = _legacy_generation(conn, configured)
    if not ensure:
        return {
            "active_generation": active, "staging_generation": None,
            "previous_generation": None, "state": "uninitialized",
            "started_at": None, "validated_at": None, "activated_at": None,
        }
    conn.execute(
        "UPDATE embedding_generation_state SET active_generation=?, state='ready' "
        "WHERE id=1 AND active_generation IS NULL",
        (active,),
    )
    row = conn.execute(
        "SELECT active_generation, staging_generation, previous_generation, "
        "state, started_at, validated_at, activated_at "
        "FROM embedding_generation_state WHERE id=1"
    ).fetchone()
    return _state_row(row)


def _state_row(row) -> dict[str, object]:
    keys = ("active_generation", "staging_generation", "previous_generation",
            "state", "started_at", "validated_at", "activated_at")
    return {key: row[i] for i, key in enumerate(keys)}


def active_generation(
    conn: sqlite3.Connection, configured: str, *, ensure: bool = True,
) -> str:
    return str(generation_state(conn, configured, ensure=ensure)["active_generation"])


def start_staging(conn: sqlite3.Connection, target: str, configured: str) -> dict[str, object]:
    """Create or resume one target generation without changing the active one."""
    state = generation_state(conn, configured)
    active = str(state["active_generation"])
    staging = state["staging_generation"]
    if staging and staging != target:
        raise RuntimeError(
            "another embedding generation is already staging; finish, roll back, "
            "or discard it before selecting a different target"
        )
    if target == active:
        # This is a same-generation backfill (typically legacy NULL-tag rows),
        # not an upgrade. It is safe to fill the active generation directly.
        return generation_state(conn, configured)
    if not staging:
        now = int(time.time())
        conn.execute(
            "UPDATE embedding_generation_state SET staging_generation=?, "
            "state='staging', started_at=?, validated_at=NULL WHERE id=1",
            (target, now),
        )
    return generation_state(conn, configured)


def discard_staging(conn: sqlite3.Connection, configured: str) -> bool:
    """Forget only an unactivated target; the active generation is untouched."""
    state = generation_state(conn, configured)
    target = state["staging_generation"]
    if not target:
        return False
    conn.execute("DELETE FROM embedding_generation_vectors WHERE generation=?", (target,))
    conn.execute(
        "UPDATE embedding_generation_state SET staging_generation=NULL, "
        "state='ready', started_at=NULL, validated_at=NULL WHERE id=1"
    )
    return True


def stage_vectors(
    conn: sqlite3.Connection,
    generation: str,
    kind: str,
    rows: Iterable[tuple[object, str]],
    blobs: Iterable[bytes],
) -> int:
    """Persist one encoded batch. Each batch is independently resumable."""
    now = int(time.time())
    staged = 0
    for (memory_id, content), blob in zip(rows, blobs):
        conn.execute(
            "INSERT INTO embedding_generation_vectors("
            "generation,memory_kind,memory_id,source_hash,embedding,created_at"
            ") VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(generation,memory_kind,memory_id) DO UPDATE SET "
            "source_hash=excluded.source_hash, embedding=excluded.embedding, "
            "created_at=excluded.created_at",
            (generation, kind, str(memory_id), source_hash(kind, content), blob, now),
        )
        staged += 1
    return staged


def pending_sources(
    conn: sqlite3.Connection,
    generation: str,
    kind: str,
    *,
    chunk: int,
) -> Iterable[list[tuple[object, str]]]:
    """Yield source rows whose staged vector is absent or predates its text."""
    spec = next((item for item in _KINDS if item[0] == kind), None)
    if spec is None:
        raise ValueError(f"unknown embedding memory kind: {kind}")
    _, table, id_col, _ = spec
    cursor = conn.execute(
        f"SELECT s.{id_col}, s.content, g.source_hash FROM {table} s "
        "LEFT JOIN embedding_generation_vectors g ON "
        "g.generation=? AND g.memory_kind=? AND g.memory_id=CAST(s."
        f"{id_col} AS TEXT) ORDER BY s.rowid",
        (generation, kind),
    )
    while True:
        source_rows = cursor.fetchmany(max(1, chunk))
        if not source_rows:
            return
        missing = [
            (row[0], source_text(kind, row[1])) for row in source_rows
            if row[2] != source_hash(kind, row[1])
        ]
        if missing:
            yield missing


def coverage(conn: sqlite3.Connection, generation: str) -> dict[str, int | bool]:
    """Count exact source/vector matches for an operator-visible report."""
    out: dict[str, int | bool] = {}
    complete = True
    for kind, table, id_col, _limit in _KINDS:
        total = staged = valid = 0
        cursor = conn.execute(
            f"SELECT s.{id_col}, s.content, g.source_hash, g.embedding "
            f"FROM {table} s LEFT JOIN embedding_generation_vectors g ON "
            "g.generation=? AND g.memory_kind=? AND g.memory_id=CAST(s."
            f"{id_col} AS TEXT)",
            (generation, kind),
        )
        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                total += 1
                if row[2] is not None:
                    staged += 1
                if row[2] == source_hash(kind, row[1]) and row[3]:
                    valid += 1
        out[f"{kind}s_total"] = total
        out[f"{kind}s_staged"] = staged
        out[f"{kind}s_valid"] = valid
        complete = complete and total == valid
    out["complete"] = complete
    return out


def activate_staging(conn: sqlite3.Connection, target: str, configured: str) -> tuple[bool, dict[str, int | bool]]:
    """Validate and move the only reader pointer in one writer transaction."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        state = generation_state(conn, configured)
        if state["staging_generation"] != target:
            raise RuntimeError("target generation is not the durable staging generation")
        report = coverage(conn, target)
        if not report["complete"]:
            conn.rollback()
            return False, report
        now = int(time.time())
        conn.execute(
            "UPDATE embedding_generation_state SET previous_generation=active_generation, "
            "active_generation=staging_generation, staging_generation=NULL, "
            "state='ready', validated_at=?, activated_at=? WHERE id=1",
            (now, now),
        )
        conn.commit()
        return True, report
    except Exception:
        conn.rollback()
        raise


def rollback_active(conn: sqlite3.Connection, configured: str) -> bool:
    """Atomically restore the prior durable generation pointer."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        state = generation_state(conn, configured)
        previous = state["previous_generation"]
        if not previous:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE embedding_generation_state SET active_generation=?, "
            "previous_generation=?, staging_generation=NULL, state='ready', "
            "activated_at=? WHERE id=1",
            (previous, state["active_generation"], int(time.time())),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def active_vectors_present(conn: sqlite3.Connection, generation: str) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM embedding_generation_vectors WHERE generation=? LIMIT 1",
            (generation,),
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def memory_provenance(
    conn: sqlite3.Connection,
    memory_kind: str,
    memory_id: object,
    *,
    source_event_kind: str = "",
    source_event_id: str = "",
    source_thread_id: str = "",
) -> None:
    """Record writer identity and source pointers for a newly derived memory."""
    from . import config
    from .spawn_config import resolve_model

    provider = (getattr(config, "WRITER_PROVIDER", "") or "").strip()
    provider = provider or (getattr(config, "CLIENT_LABEL", "") or "unknown")
    model = (getattr(config, "WRITER_MODEL", "") or "").strip()
    if not model:
        try:
            model = resolve_model(provider)
        except Exception:
            model = ""
    revision = (getattr(config, "WRITER_REVISION", "") or "").strip()
    conn.execute(
        "INSERT INTO memory_provenance("
        "memory_kind,memory_id,writer_provider,writer_model,writer_revision,"
        "source_event_kind,source_event_id,source_thread_id,recorded_at"
        ") VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(memory_kind,memory_id) "
        "DO NOTHING",
        (memory_kind, str(memory_id), provider, model or "unknown",
         revision or "unknown", source_event_kind or None,
         source_event_id or None, source_thread_id or None, int(time.time())),
    )


def record_upgrade_replay(
    conn: sqlite3.Connection,
    *,
    direction: str,
    writer_generation: str,
    embedding_generation: str,
    passed: bool,
    report: dict,
) -> None:
    """Persist metrics only, keeping the fixed corpus outside user memory."""
    conn.execute(
        "INSERT INTO memory_upgrade_replays("
        "direction,writer_generation,embedding_generation,passed,report_json,created_at"
        ") VALUES (?,?,?,?,?,?)",
        (direction, writer_generation, embedding_generation, int(passed),
         json.dumps(report, sort_keys=True), int(time.time())),
    )
