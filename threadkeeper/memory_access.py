"""Task-bound durable-memory capabilities, independent of provenance labels."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import hashlib
import inspect
import json
import os
import secrets
import sqlite3
import time


MEMORY_ENV = frozenset({
    'THREADKEEPER_MEMORY_ACCESS', 'THREADKEEPER_MEMORY_TOKEN',
    'THREADKEEPER_MEMORY_NAMESPACE',
})
TRUSTED_LAUNCH = object()
_grant_issuance: ContextVar[bool] = ContextVar('memory_grant_issuance', default=False)
_review_origin: ContextVar[str] = ContextVar('memory_proposal_origin', default='')

# Coordination remains available without granting any durable learning writes.
_OPERATIONAL_TOOLS = frozenset({
    'broadcast', 'whisper', 'wait', 'ask', 'respond', 'inbox', 'live_status',
    'presence', 'search_via_parent', 'spawn', 'tournament',
    'model_route_write', 'evolve_research_handoff', 'curator_research_write',
    'curator_report_write',
    'tag_signal',
})
_OBSERVATIONAL_TOOLS = frozenset({'lesson_get', 'lesson_list', 'skill_list'})
PROPOSABLE_TOOLS = frozenset({
    'open_thread', 'note', 'close_thread', 'verbatim_user', 'core_set',
    'style_set', 'register_concept', 'distill', 'link', 'lesson_append',
    'lesson_patch', 'skill_manage', 'dialectic_claim', 'dialectic_evidence',
    'claim_pickup', 'release_pickup',
})
_SCOPED_TABLES = frozenset({
    'threads', 'notes', 'verbatim', 'dialog_messages', 'core_memory', 'concepts',
    'distill', 'user_dialectic', 'dialectic_evidence', 'dialectic_observations',
    'extract_candidates', 'edges', 'skill_usage',
})
# Everything outside this small operational set is durable state. New tables
# fail closed until their intended child access has been considered.
_OPERATIONAL_TABLES = frozenset({
    'sessions', 'events', 'signals', 'tasks',
    'mcp_replay_ledger', 'mcp_task_handles',
    'resource_controls', 'cursors', 'presence',
    'model_router_passes', 'model_routes', 'evolve_research_handoffs',
})


@dataclass(frozen=True)
class MemoryAccess:
    access: str = 'read_only'
    namespace: str = ''
    origin: str = 'spawned'
    child_cid: str = ''
    child: bool = True


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def resolve_access(conn: sqlite3.Connection) -> MemoryAccess:
    """Only a persisted launch grant can elevate a spawned process."""
    from .config import SPAWNED_CHILD

    cid = os.environ.get('THREADKEEPER_FORCE_CID', '')
    token = os.environ.get('THREADKEEPER_MEMORY_TOKEN', '')
    row = conn.execute(
        'SELECT * FROM child_memory_grants WHERE child_cid=?', (cid,),
    ).fetchone()
    if row is not None and token and secrets.compare_digest(row['token_hash'], token_digest(token)):
        return MemoryAccess(row['access'], row['namespace'], row['origin'], cid)
    if SPAWNED_CHILD or token or row is not None:
        return MemoryAccess(child_cid=cid)
    return MemoryAccess('write', '*', '', '', child=False)


def current_access() -> MemoryAccess:
    from .db import bootstrap_db, _open_connection

    bootstrap_db()
    conn, _ = _open_connection(autocommit=True)
    conn.execute('PRAGMA query_only=ON')
    try:
        return resolve_access(conn)
    finally:
        conn.close()


def effective_origin(origin: str) -> str:
    reviewed = _review_origin.get()
    if reviewed:
        return 'spawned'
    policy = current_access()
    return policy.origin if policy.child else origin


def require_write() -> None:
    if current_access().access != 'write':
        raise PermissionError('memory_access_denied')


def scope_predicate(alias: str, policy: MemoryAccess) -> tuple[str, tuple[str, ...]]:
    if policy.namespace == '*':
        return '1', ()
    if not policy.namespace:
        return f"{alias}.memory_scope IN ('global','shared')", ()
    return (
        f"({alias}.memory_scope IN ('global','shared') OR "
        f"({alias}.memory_scope='project' AND {alias}.project_id=?))",
        (policy.namespace,),
    )


def protect_connection(conn: sqlite3.Connection) -> None:
    """Enforce read/propose below tools, including direct storage callers.

    Scoped temporary views filter even exact-ID reads before rows leave SQL.
    Main-table reads outside those views cannot bypass the scope restriction.
    """
    policy = resolve_access(conn)
    if not policy.child:
        return
    views = set()
    if policy.namespace != '*':
        for table in sorted(_SCOPED_TABLES):
            where, params = scope_predicate(table, policy)
            # SQLite views cannot contain bound parameters. The only literal
            # comes from our persisted namespace; quote it as SQL data.
            if params:
                where = where.replace('?', "'" + params[0].replace("'", "''") + "'")
            conn.execute(
                f'CREATE TEMP VIEW "{table}" AS '
                f'SELECT rowid AS rowid, * FROM main."{table}" WHERE {where}'
            )
            views.add(table)

    def authorize(action, table, column, database, source):
        if action == sqlite3.SQLITE_READ and table == 'memory_proposals':
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ and database == 'main' and table in views:
            return sqlite3.SQLITE_OK if source == table else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_UPDATE and table == 'curator_batches' and column in {'report_written_at', 'provenance_sha256'}:
            return sqlite3.SQLITE_OK
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            if table == 'child_memory_grants':
                return sqlite3.SQLITE_OK if action == sqlite3.SQLITE_INSERT and _grant_issuance.get() else sqlite3.SQLITE_DENY
            if policy.access != 'write' and table not in _OPERATIONAL_TABLES:
                return sqlite3.SQLITE_DENY
        if action in (
            sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_DROP_TEMP_VIEW, sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorize)


def propose(conn: sqlite3.Connection, policy: MemoryAccess, name: str, arguments: dict) -> str:
    """Queue data only; no accepted artifact or authority stamp is created."""
    if policy.access != 'propose' or name not in PROPOSABLE_TOOLS:
        return 'ERR memory_access_denied'
    if name == 'skill_manage' and arguments.get('action') not in {'create', 'edit', 'patch', 'write_file'}:
        return 'ERR proposal_action_not_supported'
    payload = json.dumps(arguments, ensure_ascii=False)
    if len(payload.encode()) > 128_000:
        return 'ERR proposal_too_large'
    scope = 'project' if policy.namespace else 'unresolved'
    cursor = conn.execute(
        'INSERT INTO memory_proposals '
        '(child_cid,tool_name,arguments,project_id,memory_scope,created_at) '
        'VALUES (?,?,?,?,?,?)',
        (policy.child_cid, name, payload, policy.namespace or None, scope, int(time.time())),
    )
    conn.commit()
    return f'ok proposal={cursor.lastrowid} status=pending'


@contextmanager
def reviewing_proposal(child_cid: str):
    token = _review_origin.set(child_cid)
    try:
        yield
    finally:
        _review_origin.reset(token)


def guard_tool(fn):
    """Deny durable writers regardless of discovery profile or direct calls."""
    def preflight(args, kwargs):
        policy = current_access()
        if policy.access == 'write' or fn.__name__ in _OPERATIONAL_TOOLS:
            return None
        if fn.__name__ in _OBSERVATIONAL_TOOLS:
            return None
        if policy.access == 'propose' and fn.__name__ in PROPOSABLE_TOOLS:
            bound = inspect.signature(fn).bind(*args, **kwargs)
            bound.apply_defaults()
            # A dedicated short connection permits only this queue INSERT.
            from .db import proposal_db
            with proposal_db() as conn:
                return propose(conn, policy, fn.__name__, bound.arguments)
        return 'ERR memory_access_denied'

    if inspect.iscoroutinefunction(fn):
        @wraps(fn)
        async def async_guard(*args, **kwargs):
            refusal = preflight(args, kwargs)
            return refusal if refusal is not None else await fn(*args, **kwargs)
        return async_guard

    @wraps(fn)
    def sync_guard(*args, **kwargs):
        refusal = preflight(args, kwargs)
        return refusal if refusal is not None else fn(*args, **kwargs)
    return sync_guard


@contextmanager
def issuing_grant():
    token = _grant_issuance.set(True)
    try:
        yield
    finally:
        _grant_issuance.reset(token)


def candidate_scope(conn: sqlite3.Connection, alias: str) -> tuple[str, tuple[str, ...]]:
    return scope_predicate(alias, resolve_access(conn))


def _proposal_references(proposal: dict, arguments: dict) -> list[tuple[str, str, str]]:
    references = [
        ('thread_id', 'threads', 'id'), ('parent_id', 'threads', 'id'),
        ('claim_id', 'user_dialectic', 'id'), ('source_thread', 'threads', 'id'),
    ]
    if proposal['tool_name'] == 'link':
        from .tools.graph import _entity_table
        for direction in ('from', 'to'):
            table = _entity_table(arguments.get(f'{direction}_kind', ''))
            if table in _SCOPED_TABLES or table == 'tasks':
                references.append((f'{direction}_id', table, 'id'))
    return references


def proposal_scope_allowed(conn: sqlite3.Connection, proposal: dict, arguments: dict) -> bool:
    policy = MemoryAccess(namespace=proposal['project_id'] or '')
    for argument, table, key in _proposal_references(proposal, arguments):
        value = arguments.get(argument)
        if not value:
            continue
        where, params = scope_predicate(table, policy)
        if not conn.execute(
            f'SELECT 1 FROM {table} WHERE {key}=? AND ({where})', (value, *params),
        ).fetchone():
            return False
    if proposal['tool_name'] == 'skill_manage':
        name = arguments.get('name', '')
        where, params = scope_predicate('skill_usage', policy)
        outside = conn.execute(
            f'SELECT 1 FROM skill_usage WHERE name=? AND NOT ({where})',
            (name, *params),
        ).fetchone()
        if outside:
            return False
    return True
