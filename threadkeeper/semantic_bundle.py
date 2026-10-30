"""Portable, privacy-preserving semantic memory bundles.

The bundle deliberately models useful memory rather than the local SQLite
implementation.  It is an interchange and audit format, not a replacement for
``tk-backup`` or live instance sync.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Any, Iterator


BUNDLE_FORMAT = "threadkeeper.semantic-memory-bundle"
BUNDLE_SCHEMA_VERSION = 1
RECORD_ENCODING = "canonical-jsonl-v1"
RECORDS_FILE = "records.jsonl"
MANIFEST_FILE = "manifest.json"


class SemanticBundleError(RuntimeError):
    """Raised when a semantic bundle cannot be safely exported or imported."""


@dataclass(frozen=True)
class SemanticBundleResult:
    destination: Path
    record_count: int
    record_types: dict[str, int]


@dataclass(frozen=True)
class SemanticImportReport:
    additions: tuple[dict[str, str], ...]
    conflicts: tuple[dict[str, str], ...]
    invalid_records: tuple[dict[str, str], ...]
    existing: tuple[dict[str, str], ...]
    namespace_decisions: tuple[dict[str, str], ...]
    dry_run: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_TABLE_RECORDS = (
    ("thread", "threads", "id", "semantic"),
    ("note", "notes", "id", "semantic"),
    ("concept", "concepts", "id", "semantic"),
    ("distill", "distill", "id", "semantic"),
)
_PERSONAL_TABLE_RECORDS = (
    ("core_memory", "core_memory", "key", "personal_memory"),
    ("claim", "user_dialectic", "id", "personal_memory"),
    ("evidence", "dialectic_evidence", "id", "personal_memory"),
)
_DIALOG_TABLE_RECORDS = (
    ("dialog", "dialog_messages", "uuid", "raw_dialog"),
    ("verbatim", "verbatim", "id", "raw_dialog"),
)
_TYPE_ORDER = {
    "thread": 10,
    "note": 20,
    "concept": 30,
    "distill": 40,
    "core_memory": 50,
    "claim": 60,
    "evidence": 70,
    "edge": 80,
    "lesson": 90,
    "skill": 100,
    "dialog": 110,
    "verbatim": 120,
    "task_log": 130,
}
_SUPPORTED_TYPES = frozenset(_TYPE_ORDER)
_ID_FIELD_BY_TYPE = {
    "thread": "id",
    "concept": "id",
    "distill": "id",
    "core_memory": "key",
    "claim": "id",
    "dialog": "uuid",
}
_TABLE_BY_TYPE = {
    "thread": "threads",
    "note": "notes",
    "concept": "concepts",
    "distill": "distill",
    "core_memory": "core_memory",
    "claim": "user_dialectic",
    "evidence": "dialectic_evidence",
    "edge": "edges",
    "dialog": "dialog_messages",
    "verbatim": "verbatim",
    "skill": "skill_usage",
    "lesson": "lesson_usage",
}
_RELATION_TYPE_BY_KIND = {
    "thread": "thread",
    "note": "note",
    "concept": "concept",
    "distill": "distill",
    "claim": "claim",
    "dialectic": "claim",
    "core_memory": "core_memory",
    "dialog": "dialog",
    "verbatim": "verbatim",
}
_DERIVED_COLUMNS = {"embedding", "embed_backend", "hlc", "origin_node", "deleted"}
_SAFE_SKILL_PART = re.compile(r"[^a-z0-9._-]+")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def _record_hash(record: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in record.items() if key != "content_sha256"}
    return _sha256(_canonical_json(unsigned))


def _row_dict(row: Any, columns: list[str] | None = None) -> dict[str, Any]:
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    if columns is None:
        raise TypeError("columns are required for tuple rows")
    return dict(zip(columns, row))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _select_rows(conn: sqlite3.Connection, table: str, order_by: str) -> list[dict[str, Any]]:
    if not _table_exists(conn, table):
        return []
    cursor = conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
    columns = [item[0] for item in cursor.description]
    return [_row_dict(row, columns) for row in cursor.fetchall()]


@contextmanager
def _source_connection(
    source: sqlite3.Connection | str | os.PathLike[str] | None,
) -> Iterator[sqlite3.Connection]:
    if isinstance(source, sqlite3.Connection):
        yield source
        return
    if source is None:
        from .db import read_db

        with read_db() as conn:
            conn.execute("BEGIN")
            try:
                yield conn
            finally:
                conn.rollback()
        return
    source_path = Path(source).expanduser()
    if not source_path.is_file():
        raise SemanticBundleError(f"source database does not exist: {source_path}")
    conn = sqlite3.connect(f"file:{source_path.resolve()}?mode=ro", uri=True)
    try:
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.rollback()
    finally:
        conn.close()


@contextmanager
def _destination_connection(
    destination: sqlite3.Connection | str | os.PathLike[str] | None,
) -> Iterator[sqlite3.Connection]:
    if isinstance(destination, sqlite3.Connection):
        yield destination
        return
    if destination is None:
        from .db import get_db

        conn = get_db()
        try:
            yield conn
        finally:
            conn.close()
        return
    destination_path = Path(destination).expanduser()
    if not destination_path.is_file():
        raise SemanticBundleError(
            f"destination database does not exist: {destination_path}; initialize it first"
        )
    conn = sqlite3.connect(str(destination_path))
    try:
        yield conn
    finally:
        conn.close()


def _authority_index(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    if not _table_exists(conn, "memory_authority"):
        return {}
    roots: dict[tuple[str, str], list[dict[str, Any]]] = {}
    if _table_exists(conn, "memory_authority_roots"):
        for row in _select_rows(conn, "memory_authority_roots", "artifact_kind, artifact_id"):
            key = (str(row["artifact_kind"]), str(row["artifact_id"]))
            roots.setdefault(key, []).append(row)
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _select_rows(conn, "memory_authority", "artifact_kind, artifact_id"):
        key = (str(row["artifact_kind"]), str(row["artifact_id"]))
        item = dict(row)
        item["roots"] = roots.get(key, [])
        result[key] = item
    return result


def _provenance_index(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    if not _table_exists(conn, "memory_provenance"):
        return {}
    rows = _select_rows(conn, "memory_provenance", "memory_kind, memory_id")
    return {(str(row["memory_kind"]), str(row["memory_id"])): row for row in rows}


def _artifact_metadata(
    record_type: str,
    source_id: str,
    authorities: dict[tuple[str, str], dict[str, Any]],
    provenance: dict[tuple[str, str], dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    aliases = (record_type, "dialectic" if record_type == "claim" else record_type)
    authority = next((authorities.get((kind, source_id)) for kind in aliases), None)
    source_provenance = next((provenance.get((kind, source_id)) for kind in aliases), None)
    return authority, source_provenance


def _record_from_row(
    record_type: str,
    source_id: str,
    row: dict[str, Any],
    *,
    egress_class: str,
    namespace: str,
    authorities: dict[tuple[str, str], dict[str, Any]],
    provenance: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    data = {
        key: value
        for key, value in row.items()
        if key not in _DERIVED_COLUMNS and key not in {"id", "key", "uuid"}
    }
    authority, source_provenance = _artifact_metadata(
        record_type, source_id, authorities, provenance
    )
    temporal = {
        key: value
        for key, value in data.items()
        if key.endswith("_at") or key in {"valid_from", "valid_to"}
    }
    state = {"deleted": bool(row.get("deleted", 0))}
    if authority and authority.get("invalidated_at") is not None:
        state["invalidated"] = True
        state["invalidated_at"] = authority["invalidated_at"]
    record: dict[str, Any] = {
        "type": record_type,
        "source": {"namespace": namespace, "id": source_id},
        "data": data,
        "provenance": source_provenance,
        "authority": authority,
        "temporal": temporal,
        "state": state,
        "egress_class": egress_class,
    }
    record["content_sha256"] = _record_hash(record)
    return record


def _table_records(
    conn: sqlite3.Connection,
    definitions: tuple[tuple[str, str, str, str], ...],
    *,
    namespace: str,
    authorities: dict[tuple[str, str], dict[str, Any]],
    provenance: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for record_type, table, id_column, egress_class in definitions:
        for row in _select_rows(conn, table, id_column):
            source_id = str(row[id_column])
            records.append(
                _record_from_row(
                    record_type,
                    source_id,
                    row,
                    egress_class=egress_class,
                    namespace=namespace,
                    authorities=authorities,
                    provenance=provenance,
                )
            )
    return records


def _lesson_records(
    conn: sqlite3.Connection,
    *,
    namespace: str,
    authorities: dict[tuple[str, str], dict[str, Any]],
    provenance: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    from .lessons import iter_lessons

    usage = {row["slug"]: row for row in _select_rows(conn, "lesson_usage", "slug")}
    records: list[dict[str, Any]] = []
    for lesson in iter_lessons() or ():
        source_id = str(lesson["slug"])
        row = {"slug": source_id, **lesson, "usage": usage.get(source_id)}
        records.append(
            _record_from_row(
                "lesson",
                source_id,
                row,
                egress_class="semantic",
                namespace=namespace,
                authorities=authorities,
                provenance=provenance,
            )
        )
    return records


def _safe_skill_files(skill_dir: Path) -> list[dict[str, str]]:
    if not skill_dir.is_dir() or skill_dir.is_symlink():
        return []
    candidates = [skill_dir / "SKILL.md"]
    for folder in ("assets", "references", "scripts", "templates"):
        directory = skill_dir / folder
        if directory.is_dir() and not directory.is_symlink():
            candidates.extend(sorted(path for path in directory.rglob("*") if path.is_file()))
    files: list[dict[str, str]] = []
    for path in candidates:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            files.append(
                {
                    "path": path.relative_to(skill_dir).as_posix(),
                    "content": path.read_text(encoding="utf-8"),
                }
            )
        except (OSError, UnicodeError):
            continue
    return files


def _skill_records(
    conn: sqlite3.Connection,
    *,
    namespace: str,
    authorities: dict[tuple[str, str], dict[str, Any]],
    provenance: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    from .config import CLAUDE_SKILLS_DIR

    usage = {row["name"]: row for row in _select_rows(conn, "skill_usage", "name")}
    names = set(usage)
    if CLAUDE_SKILLS_DIR.is_dir():
        names.update(path.name for path in CLAUDE_SKILLS_DIR.iterdir() if path.is_dir())
    records: list[dict[str, Any]] = []
    for name in sorted(names):
        if name.startswith("."):
            continue
        row = {
            "name": name,
            "usage": usage.get(name),
            "files": _safe_skill_files(CLAUDE_SKILLS_DIR / name),
        }
        records.append(
            _record_from_row(
                "skill",
                name,
                row,
                egress_class="semantic",
                namespace=namespace,
                authorities=authorities,
                provenance=provenance,
            )
        )
    return records


def _edge_records(
    conn: sqlite3.Connection,
    *,
    namespace: str,
    authorities: dict[tuple[str, str], dict[str, Any]],
    provenance: dict[tuple[str, str], dict[str, Any]],
    known_records: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in _select_rows(conn, "edges", "id"):
        from_type = _RELATION_TYPE_BY_KIND.get(str(row["from_kind"]))
        to_type = _RELATION_TYPE_BY_KIND.get(str(row["to_kind"]))
        if (from_type, str(row["from_id"])) not in known_records:
            continue
        if (to_type, str(row["to_id"])) not in known_records:
            continue
        records.append(
            _record_from_row(
                "edge",
                str(row["id"]),
                row,
                egress_class="semantic",
                namespace=namespace,
                authorities=authorities,
                provenance=provenance,
            )
        )
    return records


def _task_log_records(namespace: str) -> list[dict[str, Any]]:
    from .config import TASK_LOG_DIR

    records: list[dict[str, Any]] = []
    if not TASK_LOG_DIR.is_dir():
        return records
    for path in sorted(TASK_LOG_DIR.glob("*.log")):
        try:
            row = {"name": path.name, "content": path.read_text(encoding="utf-8")}
        except (OSError, UnicodeError):
            continue
        record: dict[str, Any] = {
            "type": "task_log",
            "source": {"namespace": namespace, "id": path.name},
            "data": row,
            "provenance": None,
            "authority": None,
            "temporal": {},
            "state": {"deleted": False},
            "egress_class": "task_log",
        }
        record["content_sha256"] = _record_hash(record)
        records.append(record)
    return records


def _record_sort_key(record: dict[str, Any]) -> tuple[int, str]:
    return _TYPE_ORDER[record["type"]], str(record["source"]["id"])


def _bundle_records(
    conn: sqlite3.Connection,
    *,
    namespace: str,
    include_dialog: bool,
    include_task_logs: bool,
    include_personal_memory: bool,
) -> list[dict[str, Any]]:
    authorities = _authority_index(conn)
    provenance = _provenance_index(conn)
    records = _table_records(
        conn,
        _TABLE_RECORDS,
        namespace=namespace,
        authorities=authorities,
        provenance=provenance,
    )
    if include_personal_memory:
        records.extend(
            _table_records(
                conn,
                _PERSONAL_TABLE_RECORDS,
                namespace=namespace,
                authorities=authorities,
                provenance=provenance,
            )
        )
    if include_dialog:
        records.extend(
            _table_records(
                conn,
                _DIALOG_TABLE_RECORDS,
                namespace=namespace,
                authorities=authorities,
                provenance=provenance,
            )
        )
    records.extend(
        _lesson_records(
            conn,
            namespace=namespace,
            authorities=authorities,
            provenance=provenance,
        )
    )
    records.extend(
        _skill_records(
            conn,
            namespace=namespace,
            authorities=authorities,
            provenance=provenance,
        )
    )
    known_records = {(record["type"], str(record["source"]["id"])) for record in records}
    records.extend(
        _edge_records(
            conn,
            namespace=namespace,
            authorities=authorities,
            provenance=provenance,
            known_records=known_records,
        )
    )
    if include_task_logs:
        records.extend(_task_log_records(namespace))
    records.sort(key=_record_sort_key)
    return records


def export_semantic_bundle(
    destination: str | os.PathLike[str],
    *,
    source: sqlite3.Connection | str | os.PathLike[str] | None = None,
    source_namespace: str = "local",
    include_dialog: bool = False,
    include_task_logs: bool = False,
    include_personal_memory: bool = False,
) -> SemanticBundleResult:
    """Write a deterministic, inspectable semantic bundle directory.

    Sensitive classes are opt-in independently.  Derived embeddings and local
    sync clocks are intentionally absent because they are neither portable nor
    semantic source material.
    """
    namespace = source_namespace.strip()
    if not namespace:
        raise SemanticBundleError("source namespace must not be empty")
    target = Path(destination).expanduser()
    if target.exists():
        raise SemanticBundleError(f"destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with _source_connection(source) as conn:
        records = _bundle_records(
            conn,
            namespace=namespace,
            include_dialog=include_dialog,
            include_task_logs=include_task_logs,
            include_personal_memory=include_personal_memory,
        )
    records_bytes = "".join(_canonical_json(record) + "\n" for record in records).encode("utf-8")
    included = sorted({record["egress_class"] for record in records})
    omitted = []
    if not include_dialog:
        omitted.append("raw_dialog")
    if not include_task_logs:
        omitted.append("task_log")
    if not include_personal_memory:
        omitted.append("personal_memory")
    manifest = {
        "format": BUNDLE_FORMAT,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "record_encoding": RECORD_ENCODING,
        "records_file": RECORDS_FILE,
        "records_sha256": _sha256(records_bytes),
        "record_count": len(records),
        "source_namespace": namespace,
        "privacy": {"included_classes": included, "omitted_classes": omitted},
    }
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    try:
        os.chmod(temporary, 0o700)
        records_path = temporary / RECORDS_FILE
        manifest_path = temporary / MANIFEST_FILE
        records_path.write_bytes(records_bytes)
        manifest_path.write_text(_canonical_json(manifest) + "\n", encoding="utf-8")
        os.chmod(records_path, 0o600)
        os.chmod(manifest_path, 0o600)
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    counts = dict(sorted(Counter(record["type"] for record in records).items()))
    return SemanticBundleResult(target, len(records), counts)


def _bundle_path(path: str | os.PathLike[str]) -> Path:
    bundle = Path(path).expanduser()
    if not bundle.is_dir():
        raise SemanticBundleError(f"bundle directory does not exist: {bundle}")
    return bundle


def _load_bundle(path: str | os.PathLike[str]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, str]]]:
    bundle = _bundle_path(path)
    manifest_path = bundle / MANIFEST_FILE
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SemanticBundleError(f"invalid bundle manifest: {exc}") from exc
    if manifest.get("format") != BUNDLE_FORMAT:
        raise SemanticBundleError("unsupported semantic bundle format")
    if manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise SemanticBundleError(
            f"unsupported semantic bundle schema version: {manifest.get('schema_version')!r}"
        )
    if manifest.get("record_encoding") != RECORD_ENCODING:
        raise SemanticBundleError("unsupported semantic bundle record encoding")
    if manifest.get("records_file") != RECORDS_FILE:
        raise SemanticBundleError("unsupported semantic bundle records file")
    records_path = bundle / RECORDS_FILE
    try:
        payload = records_path.read_bytes()
    except OSError as exc:
        raise SemanticBundleError(f"bundle records are unavailable: {exc}") from exc
    invalid: list[dict[str, str]] = []
    if _sha256(payload) != manifest.get("records_sha256"):
        invalid.append({"record": "bundle", "reason": "records_sha256_mismatch"})
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(payload.decode("utf-8", errors="replace").splitlines(), start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            invalid.append({"record": str(line_number), "reason": "invalid_json"})
            continue
        if not isinstance(record, dict):
            invalid.append({"record": str(line_number), "reason": "record_is_not_an_object"})
            continue
        error = _validate_record(record)
        if error:
            invalid.append({"record": str(line_number), "reason": error})
            continue
        records.append(record)
    if manifest.get("record_count") != len(records):
        invalid.append({"record": "bundle", "reason": "record_count_mismatch"})
    return manifest, records, invalid


def _validate_record(record: dict[str, Any]) -> str:
    record_type = record.get("type")
    source = record.get("source")
    if record_type not in _SUPPORTED_TYPES:
        return "unsupported_record_type"
    if not isinstance(source, dict) or not source.get("namespace") or source.get("id") is None:
        return "missing_source_identity"
    if not isinstance(record.get("data"), dict):
        return "missing_record_data"
    if not isinstance(record.get("state"), dict):
        return "missing_record_state"
    if record.get("content_sha256") != _record_hash(record):
        return "content_sha256_mismatch"
    return ""


def _mapping_table_exists(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "semantic_bundle_import_records")


def _ensure_mapping_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS semantic_bundle_import_records ("
        "source_namespace TEXT NOT NULL, record_type TEXT NOT NULL, source_id TEXT NOT NULL, "
        "content_sha256 TEXT NOT NULL, local_kind TEXT NOT NULL, local_id TEXT NOT NULL, "
        "imported_at INTEGER NOT NULL, "
        "PRIMARY KEY (source_namespace, record_type, source_id))"
    )


def _mapping_row(
    conn: sqlite3.Connection, namespace: str, record_type: str, source_id: str
) -> Any:
    if not _mapping_table_exists(conn):
        return None
    return conn.execute(
        "SELECT content_sha256, local_kind, local_id FROM semantic_bundle_import_records "
        "WHERE source_namespace=? AND record_type=? AND source_id=?",
        (namespace, record_type, source_id),
    ).fetchone()


def _summary(record: dict[str, Any]) -> dict[str, str]:
    return {"type": str(record["type"]), "source_id": str(record["source"]["id"])}


def _namespaced_id(namespace: str, record_type: str, source_id: str) -> str:
    return f"bundle:{namespace}:{record_type}:{source_id}"


def _namespaced_label(namespace: str, record_type: str, source_id: str) -> str:
    cleaned = _SAFE_SKILL_PART.sub("-", source_id.lower()).strip("-.") or record_type
    digest = _sha256(f"{namespace}\0{record_type}\0{source_id}")[:12]
    return f"imported-{digest}-{cleaned}"[:64].rstrip("-.")


def _planned_local_id(record: dict[str, Any], namespace: str) -> str:
    record_type = str(record["type"])
    source_id = str(record["source"]["id"])
    if record_type in {"skill", "lesson", "task_log"}:
        return _namespaced_label(namespace, record_type, source_id)
    return _namespaced_id(namespace, record_type, source_id)


def _path_conflict(record: dict[str, Any], namespace: str) -> bool:
    record_type = record["type"]
    if record_type == "skill":
        from .config import CLAUDE_SKILLS_DIR

        return (CLAUDE_SKILLS_DIR / _planned_local_id(record, namespace)).exists()
    if record_type == "lesson":
        from .lessons import lesson_section

        return lesson_section(_planned_local_id(record, namespace)) is not None
    return False


def _build_report(
    conn: sqlite3.Connection,
    records: list[dict[str, Any]],
    invalid: list[dict[str, str]],
    namespace: str,
    *,
    dry_run: bool,
) -> SemanticImportReport:
    additions: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []
    existing: list[dict[str, str]] = []
    for record in sorted(records, key=_record_sort_key):
        record_type = str(record["type"])
        source_id = str(record["source"]["id"])
        mapped = _mapping_row(conn, namespace, record_type, source_id)
        if mapped is not None:
            old_hash = mapped[0] if not hasattr(mapped, "keys") else mapped["content_sha256"]
            if old_hash == record["content_sha256"]:
                existing.append(_summary(record))
            else:
                conflicts.append({**_summary(record), "reason": "source_record_changed"})
            continue
        if _path_conflict(record, namespace):
            conflicts.append({**_summary(record), "reason": "target_name_occupied"})
            continue
        additions.append(_summary(record))
    decisions = (
        {
            "source_namespace": str(records[0]["source"]["namespace"]) if records else namespace,
            "destination_namespace": namespace,
            "decision": "source_ids_are_retained_in_semantic_bundle_import_records",
        },
    )
    return SemanticImportReport(
        tuple(additions), tuple(conflicts), tuple(invalid), tuple(existing), decisions, dry_run
    )


def _lookup_local_id(
    conn: sqlite3.Connection, namespace: str, record_type: str, source_id: str
) -> str:
    row = _mapping_row(conn, namespace, record_type, source_id)
    if row is not None:
        return str(row[2] if not hasattr(row, "keys") else row["local_id"])
    return _namespaced_id(namespace, record_type, source_id)


def _insert_dict(conn: sqlite3.Connection, table: str, values: dict[str, Any]) -> None:
    available = _table_columns(conn, table)
    selected = {key: value for key, value in values.items() if key in available}
    if not selected:
        return
    names = list(selected)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
        [selected[name] for name in names],
    )


def _primary_is_text(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    for row in rows:
        name = row[1] if not hasattr(row, "keys") else row["name"]
        if name == column:
            declared = row[2] if not hasattr(row, "keys") else row["type"]
            return "TEXT" in str(declared).upper()
    return False


def _apply_authority_and_provenance(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    local_id: str,
) -> None:
    authority = record.get("authority")
    if authority and _table_exists(conn, "memory_authority"):
        kind = str(authority.get("artifact_kind") or record["type"])
        conn.execute(
            "INSERT OR IGNORE INTO memory_authority "
            "(artifact_kind, artifact_id, authority_class, source_principal, source_channel, "
            "created_at, invalidated_at, invalidation_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                local_id,
                authority["authority_class"],
                authority["source_principal"],
                authority["source_channel"],
                authority["created_at"],
                authority.get("invalidated_at"),
                authority.get("invalidation_reason"),
            ),
        )
        for root in authority.get("roots") or ():
            conn.execute(
                "INSERT OR IGNORE INTO memory_authority_roots "
                "(artifact_kind, artifact_id, principal, channel, authority_class, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    local_id,
                    root["principal"],
                    root["channel"],
                    root["authority_class"],
                    root["created_at"],
                ),
            )
    provenance = record.get("provenance")
    if provenance and _table_exists(conn, "memory_provenance"):
        kind = str(provenance.get("memory_kind") or record["type"])
        values = dict(provenance)
        values["memory_kind"] = kind
        values["memory_id"] = local_id
        _insert_dict(conn, "memory_provenance", values)


def _materialize_skill(record: dict[str, Any], namespace: str) -> None:
    if record.get("state", {}).get("deleted"):
        return
    from .config import CLAUDE_SKILLS_DIR
    from .tools.skills import _mirror_skill_dir

    name = _planned_local_id(record, namespace)
    target = CLAUDE_SKILLS_DIR / name
    files = record["data"].get("files") or []
    if not files:
        return
    CLAUDE_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{name}.tmp-", dir=CLAUDE_SKILLS_DIR.parent))
    try:
        for entry in files:
            relative = Path(str(entry.get("path", "")))
            if relative.is_absolute() or ".." in relative.parts:
                raise SemanticBundleError("skill file path escapes the skill directory")
            if relative.as_posix() != "SKILL.md" and relative.parts[:1] not in {
                ("assets",), ("references",), ("scripts",), ("templates",)
            }:
                raise SemanticBundleError("unsupported skill file path")
            content = str(entry.get("content", ""))
            if relative.as_posix() == "SKILL.md":
                content = re.sub(r"(?m)^name:\s*.*$", f"name: {name}", content, count=1)
            output = temporary / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(content, encoding="utf-8")
        if not (temporary / "SKILL.md").is_file():
            raise SemanticBundleError("skill record has no SKILL.md")
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    _mirror_skill_dir(name)


def _materialize_lesson(record: dict[str, Any], namespace: str) -> None:
    if record.get("state", {}).get("deleted"):
        return
    from .lessons import append_lesson

    data = record["data"]
    source_id = str(record["source"]["id"])
    append_lesson(
        _planned_local_id(record, namespace),
        str(data.get("body", "")),
        source=f"bundle:{namespace}:{source_id}",
    )


def _materialize_task_log(record: dict[str, Any], namespace: str) -> None:
    if record.get("state", {}).get("deleted"):
        return
    from .config import TASK_LOG_DIR

    TASK_LOG_DIR.mkdir(parents=True, exist_ok=True)
    output = TASK_LOG_DIR / f"{_planned_local_id(record, namespace)}.log"
    output.write_text(str(record["data"].get("content", "")), encoding="utf-8")


def _apply_record(conn: sqlite3.Connection, record: dict[str, Any], namespace: str) -> str:
    record_type = str(record["type"])
    source_id = str(record["source"]["id"])
    data = dict(record["data"])
    state = record.get("state") or {}
    table = _TABLE_BY_TYPE.get(record_type)
    local_id = _planned_local_id(record, namespace)
    if record_type in {"note", "evidence", "verbatim"}:
        column = "id"
        assert table is not None
        if _primary_is_text(conn, table, column):
            data[column] = local_id
        else:
            local_id = ""
    elif record_type == "edge":
        if _primary_is_text(conn, "edges", "id"):
            data["id"] = local_id
        else:
            local_id = ""
    elif record_type == "skill":
        usage = data.pop("usage", None) or {}
        data = dict(usage)
        data["name"] = local_id
        data.setdefault("created_at", int(time.time()))
        data.setdefault("created_by_origin", "foreground")
    elif record_type == "lesson":
        usage = data.pop("usage", None) or {}
        data = dict(usage)
        data["slug"] = local_id
        data.setdefault("created_at", int(record["data"].get("ts") or time.time()))
    elif record_type == "task_log":
        return local_id
    else:
        id_field = _ID_FIELD_BY_TYPE.get(record_type)
        if id_field:
            data[id_field] = local_id
    if record_type == "thread" and data.get("parent_id"):
        data["parent_id"] = _lookup_local_id(conn, namespace, "thread", str(data["parent_id"]))
    if record_type in {"note", "concept", "distill"} and data.get("thread_id"):
        data["thread_id"] = _lookup_local_id(conn, namespace, "thread", str(data["thread_id"]))
    if record_type in {"concept", "distill"} and data.get("source_thread"):
        data["source_thread"] = _lookup_local_id(conn, namespace, "thread", str(data["source_thread"]))
    if record_type == "claim" and data.get("superseded_by"):
        data["superseded_by"] = _lookup_local_id(conn, namespace, "claim", str(data["superseded_by"]))
    if record_type == "evidence":
        data["claim_id"] = _lookup_local_id(conn, namespace, "claim", str(data["claim_id"]))
    if record_type == "edge":
        for side in ("from", "to"):
            kind = str(data[f"{side}_kind"])
            reference_type = _RELATION_TYPE_BY_KIND.get(kind)
            if reference_type:
                data[f"{side}_id"] = _lookup_local_id(
                    conn, namespace, reference_type, str(data[f"{side}_id"])
                )
    if record_type == "dialog":
        data["uuid"] = local_id
    if "deleted" in _table_columns(conn, table or ""):
        data["deleted"] = int(bool(state.get("deleted")))
    assert table is not None
    _insert_dict(conn, table, data)
    if not local_id:
        local_id = str(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    _apply_authority_and_provenance(conn, record, local_id)
    conn.execute(
        "INSERT INTO semantic_bundle_import_records "
        "(source_namespace, record_type, source_id, content_sha256, local_kind, local_id, imported_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (namespace, record_type, source_id, record["content_sha256"], record_type, local_id, int(time.time())),
    )
    return local_id


def import_semantic_bundle(
    source: str | os.PathLike[str],
    *,
    destination: sqlite3.Connection | str | os.PathLike[str] | None = None,
    namespace: str | None = None,
    dry_run: bool = True,
) -> SemanticImportReport:
    """Plan or apply a semantic bundle import.

    Imports are dry-run by default.  An apply validates every hash and schema
    field before opening a write transaction; conflicts and invalid records
    therefore never leave a partial database import behind.
    """
    manifest, records, invalid = _load_bundle(source)
    source_namespace = str(manifest.get("source_namespace") or "")
    destination_namespace = (namespace or source_namespace).strip()
    if not destination_namespace:
        raise SemanticBundleError("bundle has no source namespace")
    for record in records:
        if record["source"]["namespace"] != source_namespace:
            invalid.append({"record": str(record["source"]["id"]), "reason": "namespace_mismatch"})
    with _destination_connection(destination) as conn:
        report = _build_report(
            conn,
            records,
            invalid,
            destination_namespace,
            dry_run=dry_run,
        )
        if dry_run:
            return report
        if report.invalid_records:
            raise SemanticBundleError("bundle contains invalid records; run a dry import for details")
        if report.conflicts:
            raise SemanticBundleError("bundle import has conflicts; run a dry import for details")
        accepted = {
            (entry["type"], entry["source_id"])
            for entry in report.additions
        }
        accepted_records = [
            record
            for record in records
            if (str(record["type"]), str(record["source"]["id"])) in accepted
        ]
        try:
            conn.execute("BEGIN IMMEDIATE")
            _ensure_mapping_table(conn)
            for record in sorted(accepted_records, key=_record_sort_key):
                _apply_record(conn, record, destination_namespace)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    for record in accepted_records:
        if record["type"] == "skill":
            _materialize_skill(record, destination_namespace)
        elif record["type"] == "lesson":
            _materialize_lesson(record, destination_namespace)
        elif record["type"] == "task_log":
            _materialize_task_log(record, destination_namespace)
    return SemanticImportReport(
        report.additions,
        report.conflicts,
        report.invalid_records,
        report.existing,
        report.namespace_decisions,
        False,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tk-semantic-bundle",
        description="Export or import privacy-preserving semantic memory bundles.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="write a versioned semantic bundle")
    export.add_argument("destination", help="new bundle directory")
    export.add_argument("--db", help="source SQLite database")
    export.add_argument("--namespace", default="local", help="stable source namespace")
    export.add_argument("--include-dialog", action="store_true", help="include raw dialog and verbatim records")
    export.add_argument("--include-task-logs", action="store_true", help="include raw task logs")
    export.add_argument("--include-personal-memory", action="store_true", help="include core memory, claims, and evidence")
    importer = sub.add_parser("import", help="dry-run or apply a semantic bundle")
    importer.add_argument("source", help="bundle directory")
    importer.add_argument("--db", help="initialized destination SQLite database")
    importer.add_argument("--namespace", help="destination namespace for source IDs")
    importer.add_argument("--apply", action="store_true", help="write after the default dry-run validation")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "export":
            result = export_semantic_bundle(
                args.destination,
                source=args.db,
                source_namespace=args.namespace,
                include_dialog=args.include_dialog,
                include_task_logs=args.include_task_logs,
                include_personal_memory=args.include_personal_memory,
            )
            print(
                _canonical_json(
                    {
                        "destination": str(result.destination),
                        "record_count": result.record_count,
                        "record_types": result.record_types,
                    }
                )
            )
            return 0
        report = import_semantic_bundle(
            args.source,
            destination=args.db,
            namespace=args.namespace,
            dry_run=not args.apply,
        )
        print(_canonical_json(report.as_dict()))
        return 0
    except SemanticBundleError as exc:
        print(f"ERR {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
