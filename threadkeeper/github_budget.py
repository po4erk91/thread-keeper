"""Shared GitHub API budget and cooldown coordination.

Roadmap automation talks to GitHub from several processes: foreground status
commands, daemon loops, and privileged spawned children running the PATH
``gh`` wrapper. GitHub quota is account-scoped, so this module stores the
current account budget/cooldown in SQLite and makes every caller honor it
before launching another ``gh`` request.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import os
import re
import sqlite3
import subprocess
import time
from typing import Any, Callable

from . import identity
from .db import get_db


GITHUB_RATE_BACKOFF_BASE_S = 60
GITHUB_RATE_BACKOFF_CAP_S = 60 * 60
GITHUB_RATE_COOLDOWN_FLOOR_S = 5
GITHUB_RATE_COOLDOWN_EXIT = 75

GITHUB_RATE_BUDGET_TABLE = """
CREATE TABLE IF NOT EXISTS github_rate_budget (
    account          TEXT PRIMARY KEY,
    remaining        INTEGER,
    reset_at         INTEGER,
    cooldown_until   INTEGER NOT NULL DEFAULT 0,
    backoff_attempts INTEGER NOT NULL DEFAULT 0,
    last_status      INTEGER,
    last_reason      TEXT,
    updated_at       INTEGER NOT NULL
)
"""

GITHUB_RATE_BUDGET_INDEX = """
CREATE INDEX IF NOT EXISTS idx_github_rate_budget_cooldown
ON github_rate_budget(cooldown_until)
"""

_HTTP_HEADER_RE = re.compile(r"(?m)^[\[,]*HTTP/\S+\s+\d{3}.*$")
_HTTP_STATUS_RE = re.compile(r"HTTP/\S+\s+(\d{3})")
_RATE_LIMIT_STATUS_RE = re.compile(r"\bHTTP\s+([45]\d\d)\b", re.I)


@dataclass(frozen=True)
class GithubRateObservation:
    status_code: int = 0
    remaining: int | None = None
    reset_at: int | None = None
    limit: int | None = None
    resource: str = ""
    retry_after_s: int | None = None
    cooldown_until: int = 0
    reason: str = ""
    backoff_attempts: int = 0


def _now() -> int:
    return int(time.time())


def _ensure_schema(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(GITHUB_RATE_BUDGET_TABLE)
        conn.execute(GITHUB_RATE_BUDGET_INDEX)
        conn.commit()
    except sqlite3.OperationalError:
        pass


def github_account_key() -> str:
    """Stable, non-secret account key for the local gh auth context."""
    host = os.environ.get("GH_HOST", "").strip() or "github.com"
    token = (
        os.environ.get("GH_TOKEN", "").strip()
        or os.environ.get("GITHUB_TOKEN", "").strip()
    )
    if token:
        fp = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        return f"{host}:token-{fp}"
    return f"{host}:gh-default"


def _int_header(headers: Mapping[str, str], name: str) -> int | None:
    raw = headers.get(name.lower())
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _retry_after_s(headers: Mapping[str, str], now_t: int) -> int | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    text = str(raw).strip()
    try:
        return max(0, int(float(text)))
    except (TypeError, ValueError):
        pass
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, int(dt.timestamp()) - int(now_t))


def exponential_backoff_s(
    attempts: int,
    *,
    base_s: int = GITHUB_RATE_BACKOFF_BASE_S,
    cap_s: int = GITHUB_RATE_BACKOFF_CAP_S,
) -> int:
    if attempts <= 0:
        return 0
    return int(min(float(cap_s), float(base_s) * (2 ** (attempts - 1))))


def _bounded_delay(delay_s: int) -> int:
    if delay_s <= 0:
        return 0
    return max(
        GITHUB_RATE_COOLDOWN_FLOOR_S,
        min(int(delay_s), GITHUB_RATE_BACKOFF_CAP_S),
    )


def _looks_secondary_limited(status_code: int, body: str, headers: Mapping[str, str]) -> bool:
    if status_code not in (403, 429):
        return False
    if "retry-after" in headers:
        return True
    lower = (body or "").lower()
    return (
        "secondary rate limit" in lower
        or "abuse detection" in lower
        or "too many requests" in lower
    )


def _looks_rate_limited(status_code: int, body: str) -> bool:
    if status_code not in (403, 429):
        return False
    lower = (body or "").lower()
    return "rate limit" in lower or "too many requests" in lower


def observe_rate_headers(
    headers: Mapping[str, str] | None,
    *,
    status_code: int = 0,
    body: str = "",
    now_t: int | None = None,
    previous_attempts: int = 0,
) -> GithubRateObservation:
    """Convert GitHub response headers/body into a ledger update."""
    now_i = int(now_t if now_t is not None else _now())
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    remaining = _int_header(h, "x-ratelimit-remaining")
    reset_at = _int_header(h, "x-ratelimit-reset")
    limit = _int_header(h, "x-ratelimit-limit")
    resource = h.get("x-ratelimit-resource", "")
    retry_after = _retry_after_s(h, now_i)

    cooldown_until = 0
    reason = ""
    attempts = 0

    if status_code in (403, 429) and retry_after is not None:
        cooldown_until = now_i + _bounded_delay(retry_after)
        reason = "retry_after"
    elif remaining == 0 and reset_at and reset_at > now_i:
        cooldown_until = min(reset_at, now_i + GITHUB_RATE_BACKOFF_CAP_S)
        reason = "primary_rate_limit"
    elif _looks_secondary_limited(status_code, body, h):
        attempts = max(0, int(previous_attempts)) + 1
        cooldown_until = now_i + exponential_backoff_s(attempts)
        reason = "secondary_rate_limit"
    elif _looks_rate_limited(status_code, body):
        attempts = max(0, int(previous_attempts)) + 1
        cooldown_until = now_i + exponential_backoff_s(attempts)
        reason = "rate_limit"

    if reason and attempts <= 0:
        attempts = max(1, int(previous_attempts) + 1)

    return GithubRateObservation(
        status_code=int(status_code or 0),
        remaining=remaining,
        reset_at=reset_at,
        limit=limit,
        resource=resource,
        retry_after_s=retry_after,
        cooldown_until=int(cooldown_until or 0),
        reason=reason,
        backoff_attempts=attempts,
    )


def _parse_header_block(block: str) -> tuple[int, dict[str, str]]:
    lines = [ln.rstrip("\r") for ln in block.splitlines() if ln.strip()]
    if not lines:
        return 0, {}
    first = lines[0].lstrip("[,")
    status = 0
    m = _HTTP_STATUS_RE.search(first)
    if m:
        status = int(m.group(1))
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return status, headers


def _clean_body_fragment(text: str) -> str:
    s = (text or "").strip()
    if s.startswith(","):
        s = s[1:].lstrip()
    if s.endswith(","):
        s = s[:-1].rstrip()
    return s


def split_gh_api_output(text: str) -> tuple[list[tuple[int, dict[str, str]]], list[str]]:
    """Split `gh api --include` output into response headers and JSON bodies."""
    raw = text or ""
    matches = list(_HTTP_HEADER_RE.finditer(raw))
    if not matches:
        return [], [raw]
    responses: list[tuple[int, dict[str, str]]] = []
    bodies: list[str] = []
    for idx, match in enumerate(matches):
        block_start = match.start()
        header_start = block_start
        while header_start < len(raw) and raw[header_start] in "[,\n\r":
            header_start += 1
        header_end = raw.find("\n\n", match.end())
        if header_end < 0:
            header_end = len(raw)
        status, headers = _parse_header_block(raw[header_start:header_end])
        responses.append((status, headers))
        body_start = min(len(raw), header_end + 2)
        body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw)
        body = _clean_body_fragment(raw[body_start:body_end])
        if body:
            bodies.append(body)
    return responses, bodies


def strip_gh_api_headers(text: str) -> str:
    """Return a JSON body string from included gh output when possible."""
    _responses, bodies = split_gh_api_output(text)
    if not bodies:
        return ""
    if len(bodies) == 1:
        return bodies[0]
    return "\n".join(bodies)


def _infer_status_code(text: str) -> int:
    m = _RATE_LIMIT_STATUS_RE.search(text or "")
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return 0
    lower = (text or "").lower()
    if "too many requests" in lower:
        return 429
    if "rate limit" in lower or "abuse detection" in lower:
        return 403
    return 0


def github_budget_state(
    conn: sqlite3.Connection | None = None,
    *,
    account: str | None = None,
    now_t: int | None = None,
) -> dict[str, Any]:
    own_conn = conn is None
    db = conn or get_db()
    _ensure_schema(db)
    acct = account or github_account_key()
    now_i = int(now_t if now_t is not None else _now())
    try:
        row = db.execute(
            "SELECT account, remaining, reset_at, cooldown_until, "
            "backoff_attempts, last_status, last_reason, updated_at "
            "FROM github_rate_budget WHERE account=?",
            (acct,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if own_conn:
        try:
            db.close()
        except sqlite3.Error:
            pass
    if not row:
        return {
            "account": acct,
            "remaining": None,
            "reset_at": None,
            "cooldown_until": 0,
            "cooldown_left_s": 0,
            "cooldown_active": False,
            "backoff_attempts": 0,
            "last_status": None,
            "last_reason": "",
            "updated_at": None,
        }
    cooldown_until = int(row["cooldown_until"] or 0)
    left = max(0, cooldown_until - now_i)
    return {
        "account": row["account"],
        "remaining": row["remaining"],
        "reset_at": row["reset_at"],
        "cooldown_until": cooldown_until,
        "cooldown_left_s": left,
        "cooldown_active": left > 0,
        "backoff_attempts": int(row["backoff_attempts"] or 0),
        "last_status": row["last_status"],
        "last_reason": row["last_reason"] or "",
        "updated_at": row["updated_at"],
    }


def github_budget_preflight(
    *,
    account: str | None = None,
    now_t: int | None = None,
) -> str:
    state = github_budget_state(account=account, now_t=now_t)
    if not state.get("cooldown_active"):
        return ""
    return (
        "thread-keeper gh budget cooldown active: "
        f"account={state['account']} "
        f"retry_after_s={state['cooldown_left_s']} "
        f"reason={state.get('last_reason') or 'rate_limit'}"
    )


def _previous_attempts(conn: sqlite3.Connection, account: str) -> int:
    try:
        row = conn.execute(
            "SELECT backoff_attempts FROM github_rate_budget WHERE account=?",
            (account,),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row:
        return 0
    try:
        return int(row["backoff_attempts"] or 0)
    except (TypeError, ValueError):
        return 0


def record_github_response(
    *,
    status_code: int = 0,
    headers: Mapping[str, str] | None = None,
    body: str = "",
    account: str | None = None,
    now_t: int | None = None,
) -> GithubRateObservation:
    acct = account or github_account_key()
    now_i = int(now_t if now_t is not None else _now())
    conn = get_db()
    _ensure_schema(conn)
    previous = _previous_attempts(conn, acct)
    obs = observe_rate_headers(
        headers,
        status_code=status_code,
        body=body,
        now_t=now_i,
        previous_attempts=previous,
    )
    has_budget_data = (
        obs.remaining is not None
        or obs.reset_at is not None
        or bool(obs.reason)
        or bool(obs.status_code)
    )
    if not has_budget_data:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        return obs

    cooldown = int(obs.cooldown_until or 0)
    attempts = int(obs.backoff_attempts or 0)
    reason = obs.reason
    if not reason and int(status_code or 0) < 400:
        attempts = 0
        if obs.remaining == 0 and obs.reset_at and obs.reset_at > now_i:
            cooldown = min(obs.reset_at, now_i + GITHUB_RATE_BACKOFF_CAP_S)
            reason = "primary_rate_limit"
            attempts = max(1, previous)
        else:
            cooldown = 0
            reason = "ok"
    elif not reason:
        attempts = previous

    try:
        conn.execute(
            "INSERT INTO github_rate_budget "
            "(account, remaining, reset_at, cooldown_until, backoff_attempts, "
            "last_status, last_reason, updated_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account) DO UPDATE SET "
            "remaining=excluded.remaining, "
            "reset_at=excluded.reset_at, "
            "cooldown_until=excluded.cooldown_until, "
            "backoff_attempts=excluded.backoff_attempts, "
            "last_status=excluded.last_status, "
            "last_reason=excluded.last_reason, "
            "updated_at=excluded.updated_at",
            (
                acct,
                obs.remaining,
                obs.reset_at,
                cooldown,
                attempts,
                int(status_code or obs.status_code or 0) or None,
                reason,
                now_i,
            ),
        )
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, 'github_rate_budget', ?, ?, ?)",
            (
                identity._session_id or "",
                acct,
                (
                    f"remaining={obs.remaining} reset_at={obs.reset_at} "
                    f"cooldown_until={cooldown} attempts={attempts} "
                    f"reason={reason}"
                )[:300],
                now_i,
            ),
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return obs


def record_gh_result(
    *,
    returncode: int,
    stdout: str = "",
    stderr: str = "",
    account: str | None = None,
    now_t: int | None = None,
) -> GithubRateObservation | None:
    responses, bodies = split_gh_api_output(stdout or "")
    if not responses and stderr:
        responses, bodies = split_gh_api_output(stderr or "")
    body = "\n".join(bodies) if bodies else "\n".join(
        part for part in (stdout, stderr) if part
    )
    if responses:
        status, headers = responses[-1]
        return record_github_response(
            status_code=status,
            headers=headers,
            body=body,
            account=account,
            now_t=now_t,
        )
    if int(returncode or 0) == 0:
        return None
    status = _infer_status_code(body)
    if not status:
        return None
    return record_github_response(
        status_code=status,
        headers={},
        body=body,
        account=account,
        now_t=now_t,
    )


def run_gh(
    cmd: Sequence[str],
    *,
    runner: Callable[..., Any] = subprocess.run,
    **kwargs: Any,
) -> Any:
    """Run gh after consulting the shared cooldown ledger."""
    blocked = github_budget_preflight()
    if blocked:
        return subprocess.CompletedProcess(
            list(cmd),
            GITHUB_RATE_COOLDOWN_EXIT,
            "",
            blocked,
        )
    proc = runner(list(cmd), **kwargs)
    record_gh_result(
        returncode=int(getattr(proc, "returncode", 0) or 0),
        stdout=str(getattr(proc, "stdout", "") or ""),
        stderr=str(getattr(proc, "stderr", "") or ""),
    )
    return proc


def format_github_budget(state: Mapping[str, Any]) -> str:
    if state.get("cooldown_active"):
        return (
            f"github_budget=cooldown left={state.get('cooldown_left_s', 0)}s "
            f"reason={state.get('last_reason') or 'rate_limit'}"
        )
    remaining = state.get("remaining")
    if remaining is None:
        return "github_budget=unknown"
    reset_at = state.get("reset_at")
    reset = ""
    if reset_at:
        dt = datetime.fromtimestamp(int(reset_at), timezone.utc)
        reset = " reset=" + dt.replace(microsecond=0).isoformat()
    return f"github_budget=ok remaining={remaining}{reset}"
