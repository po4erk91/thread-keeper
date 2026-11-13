"""Bounded deterministic context for search, separate from displayed evidence.

The SQL projection is shared by live index triggers and staged embeddings.
Older vector generations keep their original text contract until activation.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature
import os
import sqlite3
import time

VERSION = "context-v1"
SHORT_LIMIT = 280
TOPIC_LIMIT = 160
_cross_project: ContextVar[bool] = ContextVar("retrieval_cross_project", default=False)


def contextual(generation: str) -> bool:
    return f":text={VERSION}" in generation


@contextmanager
def recall_scope(cross_project: bool):
    token = _cross_project.set(cross_project)
    try:
        yield
    finally:
        _cross_project.reset(token)


def scoped_recall(fn):
    parameters = signature(fn)
    @wraps(fn)
    def run(*args, **kwargs):
        with recall_scope(parameters.bind(*args, **kwargs).arguments.get("cross_project", False)):
            return fn(*args, **kwargs)
    return run


def candidate_where(conn: sqlite3.Connection, kind: str, alias: str):
    """Apply namespace, deletion, authority and expiry before ranking/limits."""
    from . import config
    from .project_scope import recall_where
    where, params = recall_where(alias, current=current_recall_project(), cross_project=_cross_project.get())
    key = "id" if kind == "note" else "uuid"
    where += (
        f" AND {alias}.deleted=0 AND NOT EXISTS ("
        "SELECT 1 FROM memory_authority a WHERE a.artifact_kind=? "
        f"AND a.artifact_id=CAST({alias}.{key} AS TEXT) "
        "AND a.invalidated_at IS NOT NULL)"
    )
    params = (*params, kind)
    if kind == "dialog" and config.DIALOG_RETENTION_DAYS:
        where += f" AND {alias}.created_at>=?"
        params = (*params, int(time.time() - config.DIALOG_RETENTION_DAYS * 86400))
    if kind == "dialog":
        cutoff = int(time.time() - config.DIALOG_RETENTION_DAYS * 86400) if config.DIALOG_RETENTION_DAYS else 0
        where += (
            " AND NOT EXISTS (SELECT 1 FROM dialog_messages subject WHERE "
            f"subject.uuid=(SELECT d.uuid FROM dialog_messages d WHERE d.session_id={alias}.session_id "
            f"AND d.source={alias}.source AND d.project_id IS {alias}.project_id "
            f"AND d.memory_scope={alias}.memory_scope AND d.role='user' AND d.deleted=0 "
            "ORDER BY d.created_at,d.uuid LIMIT 1) AND (subject.created_at<? OR "
            "EXISTS (SELECT 1 FROM memory_authority a WHERE a.artifact_kind='dialog' "
            "AND a.artifact_id=subject.uuid AND a.invalidated_at IS NOT NULL)))"
        )
        params = (*params, cutoff)
    return where, params


def current_recall_project() -> str | None:
    from .memory_access import current_access
    from .project_scope import current_project_id
    policy = current_access()
    if policy.child and policy.namespace and policy.namespace != "*":
        return policy.namespace
    return current_project_id()


def dialogs_allowed() -> bool:
    from .egress import personal_allowed
    from . import identity
    consumer = os.environ.get("THREADKEEPER_EGRESS_CONSUMER") or identity.active_cli()
    return personal_allowed(consumer)


def document_sql(kind: str, alias: str = "s") -> str:
    body = f"{alias}.content"
    if kind == "note":
        topic = (
            "SELECT t.question FROM threads t "
            f"WHERE t.id={alias}.thread_id AND t.project_id IS {alias}.project_id "
            f"AND t.memory_scope={alias}.memory_scope AND t.deleted=0"
        )
        metadata = f"' kind=' || substr({alias}.kind,1,32)"
    else:
        topic = (
            "SELECT d.content FROM dialog_messages d "
            f"WHERE {alias}.session_id IS NOT NULL AND d.session_id={alias}.session_id "
            f"AND d.source={alias}.source AND d.project_id IS {alias}.project_id "
            f"AND d.memory_scope={alias}.memory_scope AND d.role='user' AND d.deleted=0 "
            "ORDER BY d.created_at,d.uuid LIMIT 1"
        )
        metadata = f"' source=' || substr({alias}.source,1,64) || ' role=' || substr({alias}.role,1,16)"
    subject = f"replace(replace(substr(COALESCE(({topic}),''),1,{TOPIC_LIMIT}),char(10),' '),char(13),' ')"
    return (
        f"CASE WHEN length({alias}.content)>{SHORT_LIMIT} THEN {body} ELSE "
        f"'[{VERSION} topic=' || {subject} || {metadata} || "
        f"' scope=' || {alias}.memory_scope || ' date=' || "
        f"COALESCE(strftime('%Y-%m-%d',{alias}.created_at,'unixepoch'),'') || "
        f"']' || char(10) || {body} END"
    )


def _first_subject(ref: str) -> str:
    return (
        f"{ref}.role='user' AND NOT EXISTS (SELECT 1 FROM dialog_messages d WHERE "
        f"d.session_id={ref}.session_id AND d.source={ref}.source "
        f"AND d.project_id IS {ref}.project_id AND d.memory_scope={ref}.memory_scope "
        "AND d.role='user' AND d.deleted=0 "
        f"AND (d.created_at,d.uuid)<({ref}.created_at,{ref}.uuid))"
    )


def schema() -> str:
    statements = ["""
CREATE TABLE IF NOT EXISTS retrieval_documents (
    rowid INTEGER PRIMARY KEY,
    memory_kind TEXT NOT NULL CHECK(memory_kind IN ('note','dialog')),
    memory_id TEXT NOT NULL,
    content TEXT NOT NULL,
    dirty INTEGER NOT NULL DEFAULT 1,
    UNIQUE(memory_kind,memory_id)
);
CREATE INDEX IF NOT EXISTS idx_retrieval_dirty ON retrieval_documents(dirty,rowid);
CREATE VIRTUAL TABLE IF NOT EXISTS retrieval_fts USING fts5(
    content, content='retrieval_documents', content_rowid='rowid'
);
CREATE INDEX IF NOT EXISTS idx_dialog_retrieval_subject ON dialog_messages
    (session_id,source,project_id,memory_scope,role,created_at,uuid);
CREATE TRIGGER IF NOT EXISTS retrieval_fts_ai AFTER INSERT ON retrieval_documents BEGIN
    INSERT INTO retrieval_fts(rowid,content) VALUES(new.rowid,new.content);
END;
CREATE TRIGGER IF NOT EXISTS retrieval_fts_ad AFTER DELETE ON retrieval_documents BEGIN
    INSERT INTO retrieval_fts(retrieval_fts,rowid,content) VALUES('delete',old.rowid,old.content);
END;
CREATE TRIGGER IF NOT EXISTS retrieval_fts_au AFTER UPDATE OF content ON retrieval_documents BEGIN
    INSERT INTO retrieval_fts(retrieval_fts,rowid,content) VALUES('delete',old.rowid,old.content);
    INSERT INTO retrieval_fts(rowid,content) VALUES(new.rowid,new.content);
    UPDATE retrieval_documents SET dirty=1 WHERE rowid=new.rowid;
END;
"""]
    for kind, table, key in (("note", "notes", "id"), ("dialog", "dialog_messages", "uuid")):
        projection = document_sql(kind)
        statements.append(
            f"CREATE VIEW IF NOT EXISTS {kind}_retrieval_sources AS "
            f"SELECT CAST(s.{key} AS TEXT) AS memory_id,{projection} AS content "
            f"FROM {table} s;"
        )
        upsert = (
            "INSERT INTO retrieval_documents(memory_kind,memory_id,content) "
            f"SELECT '{kind}',memory_id,content FROM {kind}_retrieval_sources "
            f"WHERE memory_id=CAST(new.{key} AS TEXT) "
            "ON CONFLICT(memory_kind,memory_id) DO UPDATE SET content=excluded.content "
            "WHERE content<>excluded.content;"
        )
        columns = "content,thread_id,kind,created_at,project_id,memory_scope,deleted" if kind == "note" else "content,session_id,source,role,created_at,project_id,memory_scope,deleted"
        for event, clause in (("ai", "INSERT"), ("au", f"UPDATE OF {columns}")):
            statements.append(f"CREATE TRIGGER IF NOT EXISTS retrieval_{kind}_{event} AFTER {clause} ON {table} BEGIN {upsert} END;")
        statements.append(
            f"CREATE TRIGGER IF NOT EXISTS retrieval_{kind}_ad AFTER DELETE ON {table} BEGIN "
            f"DELETE FROM retrieval_documents WHERE memory_kind='{kind}' AND memory_id=CAST(old.{key} AS TEXT); END;"
        )
    statements.append("""
CREATE TRIGGER IF NOT EXISTS retrieval_thread_au AFTER UPDATE OF question,project_id,memory_scope,deleted ON threads BEGIN
    UPDATE retrieval_documents SET content=(SELECT v.content FROM note_retrieval_sources v WHERE v.memory_id=retrieval_documents.memory_id)
    WHERE memory_kind='note' AND memory_id IN (SELECT CAST(id AS TEXT) FROM notes WHERE thread_id=new.id);
END;
""")
    # A changed or erased conversation subject must not survive in sibling prefixes.
    for event, clause in (("ai", "INSERT"), ("au", "UPDATE OF content,session_id,source,role,created_at,project_id,memory_scope,deleted"), ("ad", "DELETE")):
        references = ("old", "new") if event == "au" else (("old",) if event == "ad" else ("new",))
        when = " OR ".join(f"({_first_subject(ref)})" for ref in references)
        refreshes = []
        for ref in references:
            refreshes.append(
                "UPDATE retrieval_documents SET content=(SELECT v.content FROM dialog_retrieval_sources v WHERE v.memory_id=retrieval_documents.memory_id) "
                "WHERE memory_kind='dialog' AND memory_id IN (SELECT uuid FROM dialog_messages "
                f"WHERE session_id={ref}.session_id AND source={ref}.source "
                f"AND project_id IS {ref}.project_id AND memory_scope={ref}.memory_scope) "
                "AND content<>(SELECT v.content FROM dialog_retrieval_sources v WHERE v.memory_id=retrieval_documents.memory_id);"
            )
        statements.append(
            f"CREATE TRIGGER IF NOT EXISTS retrieval_subject_{event} AFTER {clause} ON dialog_messages "
            f"WHEN {when} BEGIN " + " ".join(refreshes) + " END;"
        )
    return "\n".join(statements)


def use_context_index(conn: sqlite3.Connection) -> bool:
    from .embeddings import embedding_fingerprint
    from .memory_compat import active_generation
    return contextual(active_generation(conn, embedding_fingerprint(), ensure=False))


def index_documents(conn, kind: str, rows) -> None:
    for memory_id, content in rows:
        # Do not replace a newer live projection with an encoding snapshot.
        conn.execute(
            "INSERT INTO retrieval_documents(memory_kind,memory_id,content) "
            f"SELECT ?,memory_id,content FROM {kind}_retrieval_sources "
            f"WHERE memory_id=? AND {'substr(content,1,2000)' if kind == 'dialog' else 'content'}=? "
            "ON CONFLICT(memory_kind,memory_id) DO UPDATE SET content=excluded.content "
            "WHERE content<>excluded.content",
            (kind, str(memory_id), content),
        )


def note_text(conn, thread_id: str, content: str, kind: str, created_at: int) -> str:
    projection = document_sql("note")
    row = conn.execute(
        f"SELECT {projection} FROM (SELECT ? AS content,? AS kind,? AS created_at,"
        "? AS thread_id,project_id,memory_scope FROM threads WHERE id=?) s",
        (content, kind, created_at, thread_id, thread_id),
    ).fetchone()
    return row[0] if row else content


def drop_source_schema(conn) -> None:
    """Temporarily remove dependants while sync replaces source tables."""
    triggers = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND "
        "(name LIKE 'retrieval_note_%' OR name LIKE 'retrieval_dialog_%' "
        "OR name LIKE 'retrieval_subject_%' OR name LIKE 'retrieval_thread_%')"
    ).fetchall()
    for row in triggers:
        conn.execute(f'DROP TRIGGER "{row[0]}"')
    for kind in ("note", "dialog"):
        conn.execute(f"DROP VIEW IF EXISTS {kind}_retrieval_sources")


def rebuild_documents(conn) -> None:
    for kind in ("note", "dialog"):
        conn.execute(
            "DELETE FROM retrieval_documents WHERE memory_kind=? AND memory_id "
            f"NOT IN (SELECT memory_id FROM {kind}_retrieval_sources)", (kind,),
        )
        conn.execute(
            "INSERT INTO retrieval_documents(memory_kind,memory_id,content) "
            f"SELECT ?,memory_id,content FROM {kind}_retrieval_sources WHERE 1 "
            "ON CONFLICT(memory_kind,memory_id) DO UPDATE SET content=excluded.content "
            "WHERE content<>excluded.content", (kind,),
        )
