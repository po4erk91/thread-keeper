"""Short-lived, caller-bound references to live, bounded memory evidence.

Only identifiers are retained in the process-local registry. Every read checks
the current store in one query-only snapshot; erased bodies are never cached.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import re
import secrets
import threading
import time

from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from mcp.types import CallToolResult, ResourceLink, TextContent

from . import authority, config
from .db import read_db
from .project_scope import current_project_id, recall_allowed
from .protocol import caller_principal
from .tool_schemas import SearchHit, SearchResults


EVIDENCE_PREFIX = "memory://evidence/"
EVIDENCE_TTL_SECONDS = 300
MAX_EVIDENCE_LINKS = 1024
MAX_RESOURCE_BYTES = 32768
MAX_THREAD_NOTES = 10
DIALOG_RADIUS = 2
_URI_PATTERN = re.compile(r"memory://evidence/[A-Za-z0-9_-]{43}")
_clock = time.monotonic


@dataclass(frozen=True)
class _Reference:
    kind: str
    ids: tuple[str, ...]
    caller: str
    project: str | None
    cross_project: bool
    expires: float
    thread_id: str | None = None


_references: OrderedDict[str, _Reference] = OrderedDict()
_lock = threading.Lock()


def _caller(context) -> str:
    try:
        request = getattr(context, "request_context", None)
    except ValueError:
        request = None
    return caller_principal(request) if request is not None else "local"


def _allowed(conn, kind: str, row, cross_project: bool) -> bool:
    if row is None:
        return False
    artifact_id = str(row["uuid"] if kind == "dialog" else row["id"])
    if not authority.is_visible(conn, kind, artifact_id):
        return False
    if not recall_allowed(row["memory_scope"], row["project_id"],
                          cross_project=cross_project):
        return False
    days = config.DIALOG_RETENTION_DAYS if kind == "dialog" else 0
    return not days or row["created_at"] >= int(time.time()) - int(days * 86400)


def _row(conn, kind: str, artifact_id: str):
    table, key = {"note": ("notes", "id"), "thread": ("threads", "id"),
                  "dialog": ("dialog_messages", "uuid")}[kind]
    content = f"substr(content, 1, {MAX_RESOURCE_BYTES}) AS content"
    fields = {
        "note": f"id, thread_id, kind, created_at, session_id, project_id, memory_scope, {content}",
        "dialog": f"uuid, source, session_id, role, created_at, project_id, memory_scope, {content}",
        "thread": f"id, state, project_id, memory_scope, "
                  f"substr(question, 1, {MAX_RESOURCE_BYTES}) AS question, "
                  f"substr(outcome, 1, {MAX_RESOURCE_BYTES}) AS outcome",
    }[kind]
    return conn.execute(f"SELECT {fields} FROM {table} WHERE {key}=?", (artifact_id,)).fetchone()


def _issue(kind: str, ids: tuple[str, ...], context, cross_project: bool,
           thread_id: str | None = None) -> str:
    uri = EVIDENCE_PREFIX + secrets.token_urlsafe(32)
    now = _clock()
    reference = _Reference(kind, ids, _caller(context), current_project_id(),
                           cross_project, now + EVIDENCE_TTL_SECONDS, thread_id)
    with _lock:
        expired = [key for key, value in _references.items() if value.expires <= now]
        for key in expired:
            del _references[key]
        while len(_references) >= MAX_EVIDENCE_LINKS:
            _references.popitem(last=False)
        _references[uri] = reference
    return uri


def _dialog_ids(conn, anchor, cross_project: bool) -> tuple[str, ...]:
    if not anchor["session_id"]:
        return (str(anchor["uuid"]),)
    params = (anchor["session_id"], anchor["source"], anchor["project_id"],
              anchor["memory_scope"], anchor["created_at"], anchor["created_at"],
              anchor["uuid"], DIALOG_RADIUS)
    neighbors = []
    for operator, order in (("<", "DESC"), (">", "ASC")):
        neighbors.extend(conn.execute(
            "SELECT uuid, created_at, project_id, memory_scope FROM dialog_messages "
            "WHERE session_id=? AND source=? "
            "AND project_id IS ? AND memory_scope=? "
            f"AND (created_at {operator} ? OR (created_at=? AND uuid {operator} ?)) "
            f"ORDER BY created_at {order}, uuid {order} LIMIT ?", params,
        ).fetchall())
    rows = [anchor, *[row for row in neighbors if _allowed(conn, "dialog", row, cross_project)]]
    return tuple(str(row["uuid"]) for row in sorted(rows, key=lambda row: (row["created_at"], row["uuid"])))


def _links_for_hit(conn, hit, context, cross_project: bool) -> tuple[str | None, str | None]:
    anchor = _row(conn, hit.source, hit.id)
    if not _allowed(conn, hit.source, anchor, cross_project):
        return None, None
    if hit.source == "dialog":
        ids = _dialog_ids(conn, anchor, cross_project)
        return _issue("dialog", ids, context, cross_project), None
    note_uri = _issue("note", (hit.id,), context, cross_project)
    thread = _row(conn, "thread", anchor["thread_id"]) if anchor["thread_id"] else None
    if not _allowed(conn, "thread", thread, cross_project):
        return note_uri, None
    rows = conn.execute(
        "SELECT id, project_id, memory_scope FROM notes WHERE thread_id=? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (thread["id"], MAX_THREAD_NOTES),
    ).fetchall()
    ids = tuple(str(row["id"]) for row in reversed(rows)
                if _allowed(conn, "note", row, cross_project))
    # The anchor keeps this thread excerpt tied to the returned search hit.
    ids = tuple(dict.fromkeys((hit.id, *ids)))[:MAX_THREAD_NOTES]
    return note_uri, _issue("thread", ids, context, cross_project, str(thread["id"]))


def search_result(conn, text: str, hits=(), *, context=None,
                  cross_project: bool = False) -> CallToolResult:
    metadata = []
    links = []
    for hit in hits:
        uri, thread_uri = _links_for_hit(conn, hit, context, cross_project)
        metadata.append(SearchHit(
            id=hit.id, source=hit.source, snippet=hit.content[:240],
            created_at=hit.created_at, thread_id=hit.thread_id,
            session_id=hit.session_id, role=hit.role, kind=hit.kind,
            project_id=hit.project_id, memory_scope=hit.memory_scope,
            score=hit.display_score, matched_by=sorted(hit.matched_by),
            resource_uri=uri, thread_uri=thread_uri,
        ))
        for resource_uri, kind in ((uri, hit.source), (thread_uri, "thread")):
            if resource_uri:
                links.append(ResourceLink(
                    type="resource_link", uri=resource_uri, name=f"{kind} evidence",
                    mime_type="application/json",
                    description="Bounded live evidence; expires in five minutes.",
                    meta={"cacheScope": "private", "ttl": 0},
                ))
    return CallToolResult(
        content=[TextContent(type="text", text=text), *links],
        structured_content=SearchResults(text=text, hits=metadata).model_dump(mode="json"),
    )


def _clip(value: str | None, limit: int) -> str:
    clipped = (value or "").encode("utf-8")[:limit].decode("utf-8", errors="ignore")
    while len(json.dumps(clipped, ensure_ascii=False).encode("utf-8")) > limit:
        clipped = clipped[:len(clipped) // 2]
    return clipped


def _evidence_row(conn, kind: str, row, limit: int) -> dict:
    content = _clip(row["content"], limit)
    artifact_id = str(row["uuid"] if kind == "dialog" else row["id"])
    stamp = conn.execute(
        "SELECT authority_class, source_channel FROM memory_authority "
        "WHERE artifact_kind=? AND artifact_id=?", (kind, artifact_id),
    ).fetchone()
    result = {
        "id": artifact_id, "content": content, "truncated": content != row["content"],
        "created_at": row["created_at"], "memory_scope": row["memory_scope"],
        "project_id": row["project_id"],
        "authority": dict(stamp) if stamp else {"authority_class": "unknown"},
    }
    fields = ("session_id", "role") if kind == "dialog" else ("thread_id", "kind")
    result.update({field: row[field] for field in fields})
    return result


def read_evidence(uri: str, context=None) -> str:
    """Resolve only issued references and never disclose why one is unavailable."""
    unavailable = ResourceNotFoundError("Evidence unavailable; search again")
    if not _URI_PATTERN.fullmatch(uri):
        raise unavailable
    with _lock:
        reference = _references.get(uri)
    if (reference is None or reference.expires <= _clock()
            or reference.caller != _caller(context)
            or reference.project != current_project_id()):
        raise unavailable
    with read_db() as conn:
        conn.execute("BEGIN")
        document = _read_document(conn, reference)
    if document is None:
        raise unavailable
    text = json.dumps(document, ensure_ascii=False)
    if len(text.encode("utf-8")) > MAX_RESOURCE_BYTES:
        raise unavailable
    return text


def _read_document(conn, reference: _Reference) -> dict | None:
    kind = "note" if reference.kind == "thread" else reference.kind
    rows = [_row(conn, kind, artifact_id) for artifact_id in reference.ids]
    if not all(_allowed(conn, kind, row, reference.cross_project) for row in rows):
        return None
    limit = 20000 if reference.kind == "note" else 2000
    document = {"kind": reference.kind, "items": [
        _evidence_row(conn, kind, row, limit) for row in rows
    ]}
    if reference.kind != "thread":
        return document
    thread = _row(conn, "thread", reference.thread_id)
    if not _allowed(conn, "thread", thread, reference.cross_project):
        return None
    if any(row["thread_id"] != reference.thread_id for row in rows):
        return None
    document["thread"] = {key: _clip(thread[key], 2000)
                          for key in ("id", "question", "state", "outcome")}
    document["bounded"] = True
    return document
