"""Live ingestion of Claude Code jsonl transcripts into dialog_messages/_fts.
Background daemon ticks every INGEST_INTERVAL_S; brief() can also call _ingest_recent_only directly."""
from __future__ import annotations

import json as _json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime as _dt
from pathlib import Path
from typing import Optional

from .config import (
    BACKGROUND_DAEMONS_ALLOWED,
    INGEST_CAP_PER_CALL,
    INGEST_INTERVAL_S,
    INGEST_RECENT_WINDOW_S,
    REDACT_DIALOG_SECRETS,
    SEMANTIC_AVAILABLE,
)
from .db import get_db
from .embeddings import _embed, embed_tag

_ingest_thread: Optional[threading.Thread] = None
_initial_ingest_thread: Optional[threading.Thread] = None
_ingest_lock = threading.Lock()
_ingest_interval_s = INGEST_INTERVAL_S
_ingest_recent_window_s = INGEST_RECENT_WINDOW_S
_last_ingest_event_at = 0
_INGEST_EVENT_IDLE_THROTTLE_S = 60

_AUTH_HEADER_RE = re.compile(
    r"(?i)(\b(?:Authorization|Proxy-Authorization)\s*:\s*"
    r"(?:Bearer|Token|Basic|OAuth)\s+)"
    r"([A-Za-z0-9._~+/=-]{8,})"
)
_BEARER_RE = re.compile(
    r"(?i)\b((?:Bearer|OAuth)\s+)([A-Za-z0-9._~+/=-]{16,})"
)
_AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_GITHUB_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
)
_OPENAI_KEY_RE = re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9][A-Za-z0-9_-]{18,}\b")
_ANTHROPIC_KEY_RE = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{18,}\b")
_SLACK_TOKEN_RE = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{18,}\b")
_NPM_TOKEN_RE = re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b")
_SENSITIVE_KEY = (
    r"[A-Za-z0-9_.-]*(?:TOKEN|SECRET|API[_-]?KEY|ACCESS[_-]?KEY|"
    r"PRIVATE[_-]?KEY|CLIENT[_-]?SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL)"
    r"[A-Za-z0-9_.-]*"
)
_SECRET_ASSIGN_QUOTED_RE = re.compile(
    rf"(?i)(\b{_SENSITIVE_KEY}\s*[:=]\s*)(?P<quote>[\"'])"
    rf"(?P<value>[^\"'\n]{{4,}})(?P=quote)"
)
_SECRET_ASSIGN_RE = re.compile(
    rf"(?i)(\b{_SENSITIVE_KEY}\s*[:=]\s*)([^\s`'\"<>\[]{{4,}})"
)
_NPMRC_CREDENTIAL_RE = re.compile(
    r"(?im)(^|[\s])"
    r"((?://[^\s:=]+(?::\d+)?/?:)?"
    r"(?:_authToken|_auth|password|username)\s*=\s*)"
    r"([^\s`'\"<>]{4,})"
)
_NETRC_LOGIN_PASSWORD_RE = re.compile(
    r"(?i)(\bmachine\s+\S+\s+login\s+)(\S+)(\s+password\s+)(\S+)"
)


def _scrub_dialog_secrets(text: str) -> str:
    """Mask credential-shaped values before dialog text reaches durable stores."""
    if not REDACT_DIALOG_SECRETS:
        return text
    scrubbed = str(text or "")
    scrubbed = _AUTH_HEADER_RE.sub(
        r"\1[REDACTED:AUTHORIZATION]", scrubbed
    )
    scrubbed = _BEARER_RE.sub(r"\1[REDACTED:BEARER_TOKEN]", scrubbed)
    scrubbed = _NETRC_LOGIN_PASSWORD_RE.sub(
        r"\1[REDACTED:NETRC_LOGIN]\3[REDACTED:NETRC_PASSWORD]",
        scrubbed,
    )
    scrubbed = _NPMRC_CREDENTIAL_RE.sub(
        r"\1\2[REDACTED:NPMRC_CREDENTIAL]", scrubbed
    )
    scrubbed = _SECRET_ASSIGN_QUOTED_RE.sub(
        r"\1\g<quote>[REDACTED:SECRET]\g<quote>", scrubbed
    )
    scrubbed = _SECRET_ASSIGN_RE.sub(
        r"\1[REDACTED:SECRET]", scrubbed
    )
    scrubbed = _ANTHROPIC_KEY_RE.sub("[REDACTED:ANTHROPIC_API_KEY]", scrubbed)
    scrubbed = _OPENAI_KEY_RE.sub("[REDACTED:OPENAI_API_KEY]", scrubbed)
    scrubbed = _GITHUB_TOKEN_RE.sub("[REDACTED:GITHUB_TOKEN]", scrubbed)
    scrubbed = _SLACK_TOKEN_RE.sub("[REDACTED:SLACK_TOKEN]", scrubbed)
    scrubbed = _NPM_TOKEN_RE.sub("[REDACTED:NPM_TOKEN]", scrubbed)
    scrubbed = _AWS_ACCESS_KEY_RE.sub(
        "[REDACTED:AWS_ACCESS_KEY_ID]", scrubbed
    )
    return scrubbed


def _record_ingest_pass(
    conn: sqlite3.Connection,
    *,
    mode: str,
    new_msgs: int,
    files_seen: int,
) -> None:
    """Emit a lightweight telemetry event for status/UI clients.

    The live ingester can tick every few seconds, so empty passes are throttled
    while non-empty passes are always recorded.
    """
    global _last_ingest_event_at
    now = int(time.time())
    if new_msgs <= 0 and now < _last_ingest_event_at + _INGEST_EVENT_IDLE_THROTTLE_S:
        return
    try:
        from . import identity

        session_id = identity._session_id or ""
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, 'ingest_pass', ?, ?, ?)",
            (
                session_id,
                str(now),
                f"ok mode={mode} new={int(new_msgs)} files={int(files_seen)}",
                now,
            ),
        )
        conn.commit()
        _last_ingest_event_at = now
    except sqlite3.OperationalError:
        return


def _backfill_dialog_fts_if_empty(conn: sqlite3.Connection) -> None:
    """Safety net: repopulate the external-content dialog_fts index when it
    is meaningfully behind dialog_messages (a restored DB, a wiped index).
    Day-to-day sync is owned by the dialog_fts_* triggers; the v1→v2
    migration does the initial rebuild. FTS5 'rebuild' re-reads every
    dialog_messages.content row. Always refreshes the fts_backfilled style
    key that brief() surfaces.

    Counts against dialog_fts_docsize, not dialog_fts itself: an
    unconstrained `SELECT COUNT(*) FROM dialog_fts` on an external-content
    table is satisfied by scanning dialog_messages' rowids directly, so it
    reads as "populated" even when the search index is empty. docsize has
    exactly one row per rowid actually indexed, so it's the real signal."""
    try:
        msg_cnt = conn.execute(
            "SELECT COUNT(*) c FROM dialog_messages"
        ).fetchone()["c"]
        fts_cnt = conn.execute(
            "SELECT COUNT(*) c FROM dialog_fts_docsize"
        ).fetchone()["c"]
    except sqlite3.OperationalError:
        return
    if fts_cnt < msg_cnt - 5:
        try:
            conn.execute("INSERT INTO dialog_fts(dialog_fts) VALUES('rebuild')")
        except sqlite3.OperationalError:
            return
        fts_cnt = conn.execute(
            "SELECT COUNT(*) c FROM dialog_fts_docsize"
        ).fetchone()["c"]
    conn.execute(
        "INSERT INTO style (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        ("fts_backfilled", str(fts_cnt), int(time.time())),
    )
    conn.commit()


def _parse_ts(ts: str) -> int:
    try:
        return int(_dt.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    except Exception:
        return int(time.time())


def _scan_message_for_skill_use(msg: dict) -> list[str]:
    """Return Skill tool_use invocations found in a single message dict.
    Handles both flat and nested content arrays; accepts either 'skill' or
    'name' key inside the tool_use input payload. Returns [] for non-
    matching messages.
    """
    found: list[str] = []

    def _walk(node) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("type") == "tool_use" and node.get("name") == "Skill":
            inp = node.get("input") or {}
            if isinstance(inp, dict):
                val = inp.get("skill") or inp.get("name")
                if isinstance(val, str) and val:
                    found.append(val)
        # Recurse into anything that might wrap further content blocks.
        for v in node.values():
            if isinstance(v, (list, dict)):
                _walk(v)

    _walk(msg)
    return found


def _is_spawned_child_session(conn: sqlite3.Connection,
                             session_id: Optional[str]) -> bool:
    """True when `session_id` belongs to autonomous child lineage.

    Skill use inside review-fork/native child descendants must NOT count toward
    tier promotion — the system observing its own behavior can't promote a
    skill, mirroring the dialectic evidence discount.

    Best-effort: task lineage is linked lazily, so a just-started child may
    briefly read as foreground. Acceptable — the backfill pass corrects it
    once the link resolves."""
    from .harvest import is_harvest_excluded_session

    return is_harvest_excluded_session(conn, session_id)


def _normalize_codex_spawned_session_ids(conn: sqlite3.Connection) -> int:
    """Backfill Codex spawned transcripts from rollout UUID to forced cid.

    New ingest gets this directly from CodexAdapter.iter_messages(). This keeps
    older rows consistent so tasks.spawned_cid joins work without depending on
    the preamble-content fallback forever.
    """
    try:
        rows = conn.execute(
            "SELECT DISTINCT session_id, content FROM dialog_messages "
            "WHERE source='codex' AND role='user' "
            "AND instr(content, 'You were spawned in the background') > 0"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    if not rows:
        return 0

    from .adapters.codex import _forced_cid_from_text

    mappings: dict[str, str] = {}
    for row in rows:
        old = row["session_id"] or ""
        forced = _forced_cid_from_text(row["content"] or "")
        if old and forced and old != forced:
            mappings[old] = forced
    if not mappings:
        return 0

    changed = 0
    for old, forced in mappings.items():
        cur = conn.execute(
            "UPDATE dialog_messages SET session_id=? "
            "WHERE source='codex' AND session_id=?",
            (forced, old),
        )
        changed += cur.rowcount if cur.rowcount else 0
        try:
            conn.execute(
                "UPDATE dialectic_observations SET source_cid=? "
                "WHERE source_cid=?",
                (forced, old),
            )
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "UPDATE extract_candidates SET source_cid=? "
                "WHERE source_cid=?",
                (forced, old),
            )
        except sqlite3.OperationalError:
            pass
    return changed


def _record_skill_use(conn: sqlite3.Connection, skill_name: str,
                     created_at: int,
                     session_id: Optional[str] = None) -> None:
    """Record one observed Skill invocation against skill_usage.

    `use_count` (raw) always increments. `foreground_use_count` — the
    counter that GATES tier promotion — increments only for genuine
    foreground sessions, not spawned review-fork children. On a real
    foreground bump, recompute the skill's tier so the hypothesis →
    observed → validated ladder actually advances from passive usage.

    Idempotent via the last_used_at guard (re-ingesting the same message
    won't double-count). Single combined UPDATE so the guard sees the
    pre-update last_used_at for both counters."""
    fg = 0 if _is_spawned_child_session(conn, session_id) else 1
    try:
        conn.execute(
            "INSERT INTO skill_usage "
            "(name, created_at, created_by_origin) "
            "VALUES (?, ?, 'foreground') "
            "ON CONFLICT(name) DO NOTHING",
            (skill_name, created_at),
        )
        cur = conn.execute(
            "UPDATE skill_usage "
            "SET last_used_at=?, use_count=use_count+1, "
            "    foreground_use_count=foreground_use_count+? "
            "WHERE name=? AND (last_used_at IS NULL "
            "OR last_used_at < ?)",
            (created_at, fg, skill_name, created_at),
        )
    except sqlite3.OperationalError:
        return  # skill_usage missing on this conn
    if fg and cur.rowcount:
        try:
            from .tools.skills import _recompute_skill_tier
            _recompute_skill_tier(conn, skill_name, created_at)
        except (ImportError, sqlite3.OperationalError):
            pass


def _extract_text(msg: dict) -> str:
    """Pull searchable text from a message; skip tool_use args, cap tool_results."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text":
            parts.append(block.get("text", ""))
        elif t == "thinking":
            parts.append(f"[thinking] {block.get('thinking', '')}")
        elif t == "tool_result":
            tr = block.get("content", "")
            if isinstance(tr, list):
                tr = " ".join(b.get("text", "") for b in tr if isinstance(b, dict))
            if isinstance(tr, str) and tr:
                parts.append(f"[tool_result] {tr[:800]}")
        # tool_use blocks deliberately skipped (noisy for semantic search)
    return "\n".join(p for p in parts if p)


def _ingest_file(conn: sqlite3.Connection, fp: Path, max_msgs: int,
                 adapter=None) -> int:
    """Incrementally ingest one transcript file from the given adapter.
    Returns number of new messages added.

    When `adapter` is None (legacy callers), the Claude Code adapter is
    used so the function's old contract still holds.

    Strategy: skip the file entirely if neither its mtime nor size has
    advanced past ingest_state. Otherwise use `adapter.iter_messages(fp)`
    to enumerate normalized messages and dedup via dialog_messages.uuid.
    If max_msgs is hit, preserve the prior cursor so the next pass rereads
    the file and drains more messages instead of losing the tail.
    """
    if adapter is None:
        from .adapters import _CLAUDE_CODE as _claude_default  # type: ignore
        adapter = _claude_default
    if not fp.exists():
        return 0
    stat = fp.stat()
    mtime = int(stat.st_mtime)
    state = conn.execute(
        "SELECT last_size, last_mtime FROM ingest_state WHERE file_path=?",
        (str(fp),)
    ).fetchone()
    last_mtime = state["last_mtime"] if state else 0
    last_size = state["last_size"] if state else 0
    if mtime <= last_mtime and stat.st_size <= last_size:
        return 0
    # Phase 1/2: collect, scrub, and embed before the first DML statement. A
    # SELECT does not start Python sqlite3's implicit write transaction; this
    # keeps model inference entirely outside the single-writer critical path.
    prepared: list[dict] = []
    text_candidates = 0
    hit_cap = False
    try:
        for nm in adapter.iter_messages(fp):
            if text_candidates >= max_msgs:
                hit_cap = True
                break
            if not nm.uuid:
                continue
            if conn.execute(
                "SELECT 1 FROM dialog_messages WHERE uuid=?", (nm.uuid,)
            ).fetchone():
                continue
            # Skill scan still includes tool-only assistant turns, but the
            # resulting counter writes happen in phase 3 below.
            skills = (
                _scan_message_for_skill_use(nm.raw)
                if nm.role == "assistant" else []
            )
            text = _scrub_dialog_secrets(nm.content)
            if not text or len(text) < 10:
                if skills:
                    prepared.append({
                        "message": nm,
                        "skills": skills,
                        "text": "",
                        "embedding": None,
                    })
                continue
            emb = _embed(text[:2000]) if SEMANTIC_AVAILABLE else None
            prepared.append({
                "message": nm,
                "skills": skills,
                "text": text,
                "embedding": emb,
            })
            text_candidates += 1
    except OSError:
        # Preserve the cursor so the next pass rereads the file, while still
        # committing any complete messages prepared before the read failed.
        hit_cap = True

    # Phase 3: only short SQLite mutations remain.
    added = 0
    for item in prepared:
        nm = item["message"]
        text = item["text"]
        emb = item["embedding"]
        inserted = False
        if text:
            cur = conn.execute(
                "INSERT INTO dialog_messages (uuid, source, project, session_id, "
                "role, content, model, created_at, embedding, embed_backend) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(uuid) DO NOTHING",
                (nm.uuid, adapter.name, adapter.project_label(fp),
                 nm.session_id, nm.role, text,
                 nm.model, nm.created_at, emb, embed_tag(emb))
            )
            inserted = cur.rowcount > 0
            if inserted and emb is not None:
                try:
                    from .embeddings import _vec_upsert_dialog
                    _vec_upsert_dialog(conn, nm.uuid, emb)
                except Exception:
                    pass
            if inserted:
                added += 1
        # For text messages, only the process that won INSERT owns passive
        # skill accounting. Tool-only turns have no dialog row, so their
        # timestamp guard inside _record_skill_use provides idempotency.
        if inserted or not text:
            for skill_name in item["skills"]:
                _record_skill_use(conn, skill_name, nm.created_at, nm.session_id)
    next_size = last_size if hit_cap else stat.st_size
    next_mtime = last_mtime if hit_cap else mtime
    conn.execute(
        "INSERT INTO ingest_state (file_path, last_size, last_mtime, ingested_at, msg_count) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(file_path) DO UPDATE SET "
        "  last_size=excluded.last_size, last_mtime=excluded.last_mtime, "
        "  ingested_at=excluded.ingested_at, msg_count=ingest_state.msg_count+excluded.msg_count",
        (str(fp), next_size, next_mtime, int(time.time()), added)
    )
    return added


def _ingest_all(conn: sqlite3.Connection, max_msgs: int = 1_000_000) -> tuple[int, int]:
    """Iterate every installed CLI adapter, incrementally ingest each
    transcript file. Returns (new_msgs, files_seen) across ALL adapters."""
    from .adapters import installed_adapters
    total = 0
    files_seen = 0
    for adapter in installed_adapters():
        files = adapter.transcript_files()
        files_seen += len(files)
        files = sorted(
            files,
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )
        for fp in files:
            if total >= max_msgs:
                break
            added = _ingest_file(conn, fp, max_msgs - total, adapter=adapter)
            total += added
            # Bound lock duration to one transcript file. Embedding for that
            # file already completed before its first DML statement.
            if conn.in_transaction:
                conn.commit()
    _normalize_codex_spawned_session_ids(conn)
    conn.commit()
    _record_ingest_pass(conn, mode="all", new_msgs=total, files_seen=files_seen)
    return (total, files_seen)


def _ingest_recent_only(conn: sqlite3.Connection,
                        max_msgs: int = 200,
                        max_age_s: int = 600) -> tuple[int, int]:
    """Live-mode ingest: only transcript files modified within `max_age_s`,
    across ALL installed CLI adapters.

    Commits after EACH file so the background tick doesn't hold a long
    write lock — multi-writer contention (parent + children + ingester)
    deadlocks fast otherwise."""
    from .adapters import installed_adapters
    cutoff = time.time() - max_age_s
    fresh: list[tuple[float, Path, object]] = []
    for adapter in installed_adapters():
        for p in adapter.transcript_files():
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m > cutoff:
                fresh.append((m, p, adapter))
    fresh.sort(key=lambda x: x[0], reverse=True)
    total = 0
    for _, fp, adapter in fresh:
        if total >= max_msgs:
            break
        added = _ingest_file(conn, fp, max_msgs - total, adapter=adapter)
        total += added
        if conn.in_transaction:
            try:
                conn.commit()
            except sqlite3.OperationalError:
                conn.rollback()
                raise
    normalized = _normalize_codex_spawned_session_ids(conn)
    if normalized:
        try:
            conn.commit()
        except sqlite3.OperationalError:
            conn.rollback()
            raise
    _record_ingest_pass(conn, mode="recent", new_msgs=total, files_seen=len(fresh))
    return (total, len(fresh))


def _backfill_skill_usage_from_jsonls(conn: sqlite3.Connection) -> int:
    """One-shot historical scan across every installed adapter. Finds
    assistant messages with tool_use(name='Skill') blocks and bumps
    skill_usage counters. Idempotent — the UPDATE guard on last_used_at
    prevents double-counting.

    Skill-tool semantics are Claude-specific in practice (other CLIs
    don't emit `tool_use name='Skill'` blocks), but the scanner is
    defensive and silently returns [] on unmatched payload shapes —
    so iterating all adapters is safe.

    Returns the number of (skill_name, message) pairs processed.
    """
    from .adapters import installed_adapters
    processed = 0
    for adapter in installed_adapters():
        for fp in adapter.transcript_files():
            try:
                for nm in adapter.iter_messages(fp):
                    if nm.role != "assistant":
                        continue
                    skills = _scan_message_for_skill_use(nm.raw)
                    if not skills:
                        continue
                    for skill_name in skills:
                        _record_skill_use(
                            conn, skill_name, nm.created_at, nm.session_id
                        )
                        processed += 1
            except OSError:
                continue
    try:
        conn.commit()
    except sqlite3.OperationalError:
        pass
    return processed


def _backfill_note_embeddings(conn: sqlite3.Connection, max_n: int = 20) -> int:
    """Embed up to `max_n` notes whose embedding column is NULL, and mirror
    every newly-embedded blob into notes_vec.

    Light spawned children (NO_EMBEDDINGS=1) write notes with embedding=NULL
    because they don't carry the model. A parent process with embeddings
    available catches them up here so semantic search isn't permanently
    blind to those notes. No-op when this process doesn't have embeddings.
    Returns the number of rows updated.
    """
    from .config import SEMANTIC_AVAILABLE
    if not SEMANTIC_AVAILABLE:
        return 0
    try:
        rows = conn.execute(
            "SELECT id, content FROM notes "
            "WHERE embedding IS NULL "
            "ORDER BY id DESC LIMIT ?",
            (max_n,),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    if not rows:
        return 0
    from .embeddings import encode_many, _vec_upsert_note, embed_tag
    try:
        vectors = encode_many([r["content"] for r in rows])
    except Exception:
        return 0
    if vectors is None:
        return 0
    updated = 0
    # All inference above completed before the first UPDATE below.
    for idx, r in enumerate(rows):
        emb = vectors[idx].astype("float32").tobytes()
        try:
            conn.execute(
                "UPDATE notes SET embedding=?, embed_backend=? WHERE id=?",
                (emb, embed_tag(emb), r["id"]),
            )
            _vec_upsert_note(conn, r["id"], emb)
            updated += 1
        except sqlite3.OperationalError:
            continue
    if updated:
        try:
            conn.commit()
        except sqlite3.OperationalError:
            pass
    return updated


# Replicated embedding-bearing tables: (table, text col, pk col, vec kind,
# truncate). Embeddings/embed_backend are never shipped over sync, so rows that
# arrive from a peer carry NULL and must be re-embedded locally from content.
_SYNC_EMBED_TABLES = (
    ("notes", "content", "id", "note", None),
    ("dialog_messages", "content", "uuid", "dialog", 2000),
    ("concepts", "description", "id", None, None),
)


def _backfill_sync_embeddings(
    conn: sqlite3.Connection,
    batch: int = 100,
    max_rows: int | None = None,
) -> int:
    """Re-embed replicated rows that arrived over sync without an embedding.

    The wire payload omits `embedding`/`embed_backend` (they are local derived
    state), so synced notes/dialog_messages/concepts land with NULL embeddings
    and are invisible to semantic search until re-embedded here. Notes/dialog
    are also mirrored into their vec tables. MUST run under `applying_guard`
    (rebuild_derived does) so the embedding UPDATEs are not captured as fresh
    sync writes. Returns the total re-embedded. No-op without embeddings.

    `max_rows` caps the work per call so a request-path caller (e.g. /sync/push)
    stays bounded and can't time out on a large initial corpus; NULL embeddings
    are a valid eventual state, so the centralized daemon finishes the remainder
    in later bounded ticks. `None` = process everything (background use).
    """
    from .config import SEMANTIC_AVAILABLE
    if not SEMANTIC_AVAILABLE:
        return 0
    from .embeddings import (
        encode_many, embed_tag, _vec_upsert_note, _vec_upsert_dialog,
    )
    total = 0
    for table, text_col, pk_col, vec_kind, trunc in _SYNC_EMBED_TABLES:
        while max_rows is None or total < max_rows:
            lim = batch if max_rows is None else min(batch, max_rows - total)
            try:
                rows = conn.execute(
                    f"SELECT {pk_col} AS k, {text_col} AS t FROM {table} "
                    f"WHERE embedding IS NULL AND {text_col} IS NOT NULL "
                    f"LIMIT ?",
                    (lim,),
                ).fetchall()
            except sqlite3.OperationalError:
                break
            if not rows:
                break
            texts = [(r["t"][:trunc] if trunc else r["t"]) for r in rows]
            try:
                vectors = encode_many(texts)
            except Exception:
                break
            if vectors is None:
                break
            n = 0
            for i, r in enumerate(rows):
                emb = vectors[i].astype("float32").tobytes()
                try:
                    conn.execute(
                        f"UPDATE {table} SET embedding=?, embed_backend=? "
                        f"WHERE {pk_col}=?",
                        (emb, embed_tag(emb), r["k"]),
                    )
                    if vec_kind == "note":
                        _vec_upsert_note(conn, r["k"], emb)
                    elif vec_kind == "dialog":
                        _vec_upsert_dialog(conn, r["k"], emb)
                    n += 1
                except sqlite3.OperationalError:
                    continue
            total += n
            if n == 0 or len(rows) < lim:
                break  # no progress (avoid a hot loop) or last partial batch
        if max_rows is not None and total >= max_rows:
            break
    if total:
        try:
            conn.commit()
        except sqlite3.OperationalError:
            pass
    return total


def _backfill_vec_tables(conn: sqlite3.Connection, batch: int = 500) -> tuple[int, int]:
    """One-shot migration: mirror existing notes.embedding and
    dialog_messages.embedding BLOBs into notes_vec / dialog_vec.

    Idempotent — `INSERT OR REPLACE` won't duplicate. Returns
    (notes_inserted, dialog_inserted). Called from the background ingester
    tick; bails fast when there's nothing to do.
    """
    from .config import SEMANTIC_AVAILABLE
    from .db import vec_available
    if not SEMANTIC_AVAILABLE or not vec_available():
        return (0, 0)
    from .embeddings import _vec_upsert_note, _vec_upsert_dialog, _notes_mapped
    n_notes = 0
    n_dialog = 0
    try:
        # Notes that have embedding but aren't yet mirrored into notes_vec.
        # Post-migration the presence check goes through notes_vec_map (notes_vec
        # is keyed by rowid, not the note's TEXT id); pre-migration it is direct.
        if _notes_mapped(conn):
            rows = conn.execute(
                "SELECT n.id, n.embedding FROM notes n "
                "LEFT JOIN notes_vec_map m ON m.gid = n.id "
                "WHERE n.embedding IS NOT NULL AND m.gid IS NULL "
                "LIMIT ?",
                (batch,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT n.id, n.embedding FROM notes n "
                "LEFT JOIN notes_vec v ON v.id = n.id "
                "WHERE n.embedding IS NOT NULL AND v.id IS NULL "
                "LIMIT ?",
                (batch,),
            ).fetchall()
        for r in rows:
            _vec_upsert_note(conn, r["id"], r["embedding"])
            n_notes += 1
    except sqlite3.OperationalError:
        pass
    try:
        # Dialog messages with embedding but no dialog_vec_map row → need
        # mirroring. (We check via the map because dialog_vec is keyed
        # by rowid, not uuid.)
        rows = conn.execute(
            "SELECT d.uuid, d.embedding FROM dialog_messages d "
            "LEFT JOIN dialog_vec_map m ON m.uuid = d.uuid "
            "WHERE d.embedding IS NOT NULL AND m.uuid IS NULL "
            "LIMIT ?",
            (batch,),
        ).fetchall()
        for r in rows:
            _vec_upsert_dialog(conn, r["uuid"], r["embedding"])
            n_dialog += 1
    except sqlite3.OperationalError:
        pass
    if n_notes or n_dialog:
        try:
            conn.commit()
        except sqlite3.OperationalError:
            pass
    return (n_notes, n_dialog)


def _start_background_ingester() -> None:
    """Start a daemon thread that incrementally ingests recently-modified jsonl
    files. Idempotent: subsequent calls are no-ops. Daemon=True so it dies with
    the process; no shutdown handshake needed."""
    global _ingest_thread
    if _ingest_thread is not None and _ingest_thread.is_alive():
        return
    if _ingest_interval_s <= 0:
        return  # disabled via env
    if not BACKGROUND_DAEMONS_ALLOWED:
        return

    def _loop() -> None:
        while True:
            time.sleep(_ingest_interval_s)
            try:
                if not _ingest_lock.acquire(blocking=False):
                    continue  # another tick still running, skip
                try:
                    bg_conn = get_db()
                    try:
                        _ingest_recent_only(
                            bg_conn,
                            max_msgs=200,
                            max_age_s=_ingest_recent_window_s,
                        )
                        # Embedding backfill: light children write notes
                        # with embedding=NULL (NO_EMBEDDINGS=1). Parent
                        # processes with SEMANTIC_AVAILABLE catch them up
                        # asynchronously so semantic search recovers
                        # without blocking the child.
                        _backfill_note_embeddings(bg_conn, max_n=20)
                        # vec0 backfill: mirror legacy BLOB embeddings
                        # into the vec0 virtual tables in batches so the
                        # sub-linear index gradually warms up.
                        _backfill_vec_tables(bg_conn, batch=500)
                        # Finish re-embedding rows that arrived over sync
                        # without an embedding (dialog/concepts especially).
                        # /sync/push only does a bounded slice; this bounded
                        # tick drains the rest. Under applying_guard so the
                        # embedding UPDATEs aren't captured as sync writes.
                        try:
                            from .sync.capture import is_migrated, applying_guard
                            if is_migrated(bg_conn):
                                with applying_guard(bg_conn):
                                    _backfill_sync_embeddings(
                                        bg_conn, max_rows=200)
                        except Exception:
                            pass
                    finally:
                        bg_conn.close()
                finally:
                    _ingest_lock.release()
            except Exception:
                pass  # never crash the daemon

    _ingest_thread = threading.Thread(
        target=_loop, name="thread-keeper-live-ingest", daemon=True
    )
    _ingest_thread.start()


def _start_initial_ingest() -> None:
    """Run the bounded startup catch-up asynchronously and single-flight.

    Under daemon-host mode only the host calls this function. In legacy mode
    each server may attempt it, but the machine-wide flock lets exactly one
    process scan/write while the rest continue serving MCP requests.
    """
    global _initial_ingest_thread
    if INGEST_CAP_PER_CALL <= 0 or not BACKGROUND_DAEMONS_ALLOWED:
        return
    if _initial_ingest_thread is not None and _initial_ingest_thread.is_alive():
        return

    def _run() -> None:
        from .helpers import single_flight_lock
        with single_flight_lock("initial-ingest") as locked:
            if not locked:
                return
            conn = get_db()
            try:
                _ingest_all(conn, max_msgs=INGEST_CAP_PER_CALL)
                _backfill_dialog_fts_if_empty(conn)
            finally:
                conn.close()

    _initial_ingest_thread = threading.Thread(
        target=_run,
        name="thread-keeper-initial-ingest",
        daemon=True,
    )
    _initial_ingest_thread.start()
