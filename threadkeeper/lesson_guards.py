"""Owner-reviewed lesson guards enforced by the MCP server itself.

Registry proposals never install client hooks. Only the verified server adapter
can become active; native CLI hooks remain explicitly unsupported.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
import sqlite3
import time

from . import guard_rules


SCHEMA = """
CREATE TABLE IF NOT EXISTS lesson_guards (
    slug TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    adapter TEXT NOT NULL,
    event TEXT NOT NULL,
    trigger_json TEXT NOT NULL,
    contract_digest TEXT NOT NULL,
    lesson_digest TEXT NOT NULL,
    eligible_at INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('proposed','verified','active','failed','retired')),
    verified_at INTEGER,
    runtime_digest TEXT,
    verification_json TEXT,
    failure TEXT,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lesson_guards_active ON lesson_guards(state);
CREATE TABLE IF NOT EXISTS lesson_guard_history (
    slug TEXT NOT NULL,
    revision INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(slug,revision)
);
CREATE TABLE IF NOT EXISTS lesson_guard_exemptions (
    slug TEXT NOT NULL,
    revision INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY(slug,revision,session_id,payload_digest)
);
"""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _source_digest() -> str:
    root = Path(__file__).parent
    try:
        sources = b"".join((root / name).read_bytes() for name in (
            "lesson_guards.py", "guard_rules.py", "memory_access.py", "_mcp.py",
        ))
    except OSError:
        return "unavailable"
    return hashlib.sha256(sources).hexdigest()


RUNTIME_DIGEST = _source_digest()


def _runtime_current() -> bool:
    return RUNTIME_DIGEST != "unavailable" and _source_digest() == RUNTIME_DIGEST


def _binding_digest(tool_name: str) -> str | None:
    from ._mcp import mcp
    tool = mcp._tool_manager._tools.get(tool_name)
    if tool is None:
        return None
    return _hash(guard_rules.canonical({"runtime": RUNTIME_DIGEST, "signature": str(inspect.signature(tool.fn))}))


def _lesson_digest(slug: str) -> str | None:
    from .lessons import iter_lessons
    lesson = next((item for item in iter_lessons() if item["slug"] == slug), None)
    if lesson is None:
        return None
    provenance = {key: lesson.get(key) for key in ("slug", "body", "source", "ts", "summary")}
    return _hash(guard_rules.canonical(provenance))


def _owner() -> None:
    from . import config
    from .memory_access import current_access, _review_origin
    if current_access().child or _review_origin.get() or config.WRITE_ORIGIN != "foreground":
        raise ValueError("owner_required")


def effective_state(row: dict, *, lesson_digest=None, runtime_current=None) -> tuple[str, str]:
    if row["state"] not in {"active", "verified"}:
        return row["state"], row["failure"] or ""
    if row["adapter"] != "mcp" or row["event"] != "before_tool":
        return "stale", "unsupported_adapter_or_event"
    if _hash(row["trigger_json"]) != row["contract_digest"]:
        return "stale", "contract_changed"
    try:
        tool_name = json.loads(row["trigger_json"])["tool"]
    except (ValueError, TypeError, KeyError):
        return "stale", "contract_changed"
    current_lesson = _lesson_digest(row["slug"]) if lesson_digest is None else lesson_digest
    current_runtime = _runtime_current() if runtime_current is None else runtime_current
    if row["lesson_digest"] != current_lesson:
        return "stale", "lesson_changed_or_missing"
    if row["runtime_digest"] != _binding_digest(tool_name) or not current_runtime:
        return "stale", "runtime_changed_restart_and_verify"
    return row["state"], ""


def states(conn) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM lesson_guards ORDER BY slug").fetchall()
    out = {}
    for value in rows:
        row = dict(value)
        row["effective_state"], row["effective_failure"] = effective_state(row)
        out[row["slug"]] = row
    return out


def _save(conn, row: dict) -> dict:
    row["updated_at"] = int(time.time())
    columns = tuple(row)
    conn.execute(
        f"INSERT INTO lesson_guards({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) "
        "ON CONFLICT(slug) DO UPDATE SET " + ",".join(f"{key}=excluded.{key}" for key in columns if key != "slug"),
        tuple(row.values()),
    )
    conn.execute(
        "INSERT INTO lesson_guard_history VALUES(?,?,?,?)",
        (row["slug"], row["revision"], guard_rules.canonical(row), row["updated_at"]),
    )
    conn.execute("DELETE FROM lesson_guard_exemptions WHERE slug=?", (row["slug"],))
    return row


def _propose(conn, slug: str, existing: dict | None, expected: int, encoded: str,
             adapter: str, event: str, lesson_digest: str, owner_confirm: bool) -> dict:
    from .lesson_violations import memory_insufficient
    same_eligible_source = existing and existing["lesson_digest"] == lesson_digest
    if not same_eligible_source and slug not in memory_insufficient(conn):
        raise ValueError("lesson_not_memory_insufficient")
    if adapter != "mcp" or event != "before_tool":
        raise ValueError("unsupported_adapter_or_event")
    if existing and existing["trigger_json"] == encoded and existing["lesson_digest"] == lesson_digest:
        return existing
    if expected != (existing["revision"] if existing else 0):
        raise ValueError("stale_revision")
    if existing and existing["state"] == "active" and not owner_confirm:
        raise ValueError("owner_confirmation_required")
    return _save(conn, {
        "slug": slug, "revision": expected + 1, "adapter": adapter, "event": event,
        "trigger_json": encoded, "contract_digest": _hash(encoded), "lesson_digest": lesson_digest,
        "eligible_at": existing["eligible_at"] if existing else int(time.time()),
        "state": "proposed", "verified_at": None, "runtime_digest": None,
        "verification_json": None, "failure": None, "updated_at": 0,
    })


def _verify(row: dict, allow_payload: dict | None, deny_payload: dict | None, runtime_current: bool) -> dict:
    if allow_payload is None or deny_payload is None:
        raise ValueError("preflight_cases_required")
    rule = json.loads(row["trigger_json"])
    allow = guard_rules.tool_arguments(rule["tool"], allow_payload)
    deny = guard_rules.tool_arguments(rule["tool"], deny_payload)
    proof = {
        "allow": guard_rules.decision(rule, allow) == "allow",
        "deny": guard_rules.decision(rule, deny) == "deny",
        "escape": guard_rules.decision(rule, deny, exempt=True) == "allow",
        "server_binding": runtime_current and _binding_digest(rule["tool"]) is not None,
    }
    row.update(
        state="verified" if all(proof.values()) else "failed",
        verified_at=int(time.time()), runtime_digest=_binding_digest(rule["tool"]),
        verification_json=guard_rules.canonical(proof),
        failure=None if all(proof.values()) else "preflight_failed",
    )
    return row


def _escape(conn, row: dict, payload: dict | None, session_id: str, current_state: str) -> dict:
    if current_state != "active" or payload is None:
        raise ValueError("active_guard_and_payload_required")
    rule = json.loads(row["trigger_json"])
    normalized = guard_rules.tool_arguments(rule["tool"], payload)
    if not guard_rules.matches(rule, normalized):
        raise ValueError("escape_payload_does_not_match")
    conn.execute("DELETE FROM lesson_guard_exemptions WHERE expires_at<=?", (int(time.time()),))
    conn.execute(
        "INSERT OR REPLACE INTO lesson_guard_exemptions VALUES(?,?,?,?,?)",
        (row["slug"], row["revision"], session_id, _hash(guard_rules.canonical(normalized)), int(time.time()) + 60),
    )
    return row


def manage(*, action: str, slug: str, expected_revision: int = 0,
           trigger: dict | None = None, adapter: str = "mcp", event: str = "before_tool",
           allow_payload: dict | None = None, deny_payload: dict | None = None,
           owner_confirm: bool = False) -> str:
    from .db import run_write
    from . import identity
    try:
        _owner()
        if action not in {"propose", "verify", "activate", "escape", "retire", "rollback"}:
            raise ValueError("invalid_action")
        lesson_digest = _lesson_digest(slug)
        if lesson_digest is None and action not in {"retire", "rollback"}:
            raise ValueError("unknown_lesson")
        if action in {"activate", "escape", "retire", "rollback"} and not owner_confirm:
            raise ValueError("owner_confirmation_required")
        encoded = guard_rules.canonical(guard_rules.normalize(trigger or {})) if action == "propose" else ""
        runtime_current = _runtime_current()
        identity.ensure_session_started()
        def write(conn):
            found = conn.execute("SELECT * FROM lesson_guards WHERE slug=?", (slug,)).fetchone()
            row = dict(found) if found else None
            if action == "propose":
                return _propose(conn, slug, row, expected_revision, encoded, adapter, event, lesson_digest, owner_confirm)
            if row is None:
                raise ValueError("guard_not_found")
            if row["revision"] != expected_revision:
                raise ValueError("stale_revision")
            if action == "escape":
                state = effective_state(row, lesson_digest=lesson_digest, runtime_current=runtime_current)[0]
                return _escape(conn, row, deny_payload, identity._session_id or "", state)
            return _transition(conn, row, action, allow_payload, deny_payload, lesson_digest, runtime_current)
        row = run_write("lesson_guard_manage", write)
        return f"ok slug={slug} revision={row['revision']} state={row['state']}" + (" escape=one_call_60s" if action == "escape" else "")
    except (ValueError, PermissionError) as exc:
        return f"ERR {exc}"


def _transition(conn, row: dict, action: str, allow_payload, deny_payload, lesson_digest, runtime_current) -> dict:
    if action == "verify":
        if row["state"] == "active":
            raise ValueError("active_guard_requires_reproposal")
        if lesson_digest != row["lesson_digest"]:
            raise ValueError("lesson_changed_repropose")
        row = _verify(row, allow_payload, deny_payload, runtime_current)
    elif action == "activate":
        if effective_state(row, lesson_digest=lesson_digest, runtime_current=runtime_current)[0] != "verified":
            raise ValueError("verified_guard_required")
        row["state"] = "active"
    elif action == "retire":
        row["state"] = "retired"
    else:
        previous = conn.execute(
            "SELECT snapshot_json FROM lesson_guard_history WHERE slug=? AND revision<? "
            "ORDER BY revision DESC LIMIT 1", (row["slug"], row["revision"]),
        ).fetchone()
        if previous is None:
            raise ValueError("no_prior_revision")
        prior = json.loads(previous[0])
        for key in ("trigger_json", "contract_digest", "lesson_digest", "adapter", "event"):
            row[key] = prior[key]
        row.update(state="proposed", verified_at=None, runtime_digest=None, verification_json=None, failure=None)
    row["revision"] += 1
    return _save(conn, row)


def _consume_escapes(conn, rows: list[dict], payload: dict, session_id: str) -> bool:
    digest = _hash(guard_rules.canonical(payload))
    now = int(time.time())
    bindings = [(row["slug"], row["revision"], session_id, digest, now) for row in rows]
    predicate = "slug=? AND revision=? AND session_id=? AND payload_digest=? AND expires_at>?"
    if any(conn.execute(f"SELECT 1 FROM lesson_guard_exemptions WHERE {predicate}", values).fetchone() is None for values in bindings):
        return False
    for values in bindings:
        conn.execute(f"DELETE FROM lesson_guard_exemptions WHERE {predicate}", values)
    return True


def _check_call(fn, args, kwargs, tool_name: str | None = None) -> str:
    from .db import read_db, run_write
    from . import identity
    from .memory_access import current_access, _review_origin
    from . import config
    declared_name = tool_name or fn.__name__
    if declared_name in guard_rules.CONTROL_TOOLS or fn.__name__ in guard_rules.CONTROL_TOOLS:
        return ""
    bound = inspect.signature(fn).bind(*args, **kwargs)
    bound.apply_defaults()
    payload = {key: value for key, value in bound.arguments.items() if key != "ctx"}
    with read_db() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM lesson_guards WHERE state='active'")]
    blocked = []
    for row in rows:
        try:
            rule = json.loads(row["trigger_json"])
            tool_name = rule["tool"]
        except (ValueError, TypeError, KeyError):
            return f"ERR lesson_guard_stale slug={row['slug']}"
        if tool_name != declared_name:
            continue
        if effective_state(row)[0] != "active":
            return f"ERR lesson_guard_stale slug={row['slug']}"
        if guard_rules.decision(rule, payload) == "allow":
            continue
        blocked.append(row)
    if not blocked:
        return ""
    refusal = f"ERR lesson_guard_denied slug={blocked[0]['slug']}"
    if current_access().child or _review_origin.get() or config.WRITE_ORIGIN != "foreground":
        return refusal
    allowed = run_write("lesson_guard_escape", lambda conn: _consume_escapes(conn, blocked, payload, identity._session_id or ""))
    return "" if allowed else refusal


def check_call(fn, args, kwargs, tool_name: str | None = None) -> str:
    try:
        return _check_call(fn, args, kwargs, tool_name)
    except (sqlite3.DatabaseError, OSError, ValueError, KeyError):
        return "ERR lesson_guard_unavailable"
