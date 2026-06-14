"""Evolve reviewer daemon — autonomous roadmap audit for thread-keeper.

The reviewer is the upstream half of thread-keeper's self-improvement loop. It
does not implement code. On each interval it spawns a research/audit child that
reviews thread-keeper itself for security, innovation, memory leaks, cost/
performance, reliability, and integration opportunities; researches current
external ideas when useful; updates docs/ROADMAP.md through a PR if the roadmap
needs edits; and creates or updates GitHub issues for actionable work.

Legacy `evolve_format()` suggestions are still included as audit input. The
child can promote/dismiss them for brief visibility, but durable implementation
work should be represented as GitHub issues. The evolve_applier drains those
issues one at a time.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

from .config import EVOLVE_REVIEW_INTERVAL_S, EVOLVE_REVIEW_MIN
from .db import get_db
from . import identity

logger = logging.getLogger(__name__)

_started = False

# First line of the prompt injected into the reviewer child. Added to
# shadow_review._INTERNAL_PROMPT_PREFIXES so the child's transcript doesn't
# pollute extract/shadow windows when ingested back.
EVOLVE_PROMPT_PREFIX = "You are an EVOLVE REVIEWER"

EVOLVE_PROMPT = """\
You are an EVOLVE REVIEWER for thread-keeper. Your job is product/engineering
roadmap audit, not implementation.

MISSION
-------
Audit thread-keeper itself for:
  - security and privacy risks;
  - memory leaks, runaway daemons, spawn/cost waste, and telemetry blind spots;
  - reliability gaps in learning loops, issue flow, tests, and adapters;
  - optimizations and simplifications;
  - integration of new ideas from current agent/MCP/memory tooling research;
  - stale or missing roadmap items.

You may do web research. Use it only when it can produce a concrete improvement
for this repo. Prefer primary/current sources when researching APIs or platform
behavior. Any issue based on web research must cite its source URLs in the body.

REPO AND BACKLOG CHECKS
-----------------------
Run from the repo root:
  - Read README.md, docs/ARCHITECTURE.md, docs/ROADMAP.md, CHANGELOG.md.
  - Inspect threadkeeper/evolve_daemon.py, threadkeeper/evolve_applier.py,
    threadkeeper/agent_status.py, threadkeeper/curator.py, and relevant tests.
  - Run `gh issue list --state open --limit 50` and avoid duplicate issues.
  - Review pending legacy evolve suggestions below. For each clear suggestion,
    create or link a GitHub issue; then call evolve_decide(promote|dismiss) only
    when that helps keep the legacy queue honest.

OUTPUTS
-------
1. Create/update GitHub issues for actionable roadmap work:
     gh issue create --title "..." --label enhancement --label roadmap --body "..."
   Use existing labels when possible. If the item is docs-only or i18n/adapter,
   use fitting labels too. The issue body must contain: Problem, Proposed
   direction, Acceptance criteria, Test/docs impact, and Sources if researched.

2. If docs/ROADMAP.md is stale, update it on a branch and open a PR. Do not
   commit to main. Do not implement product/code fixes in reviewer; only roadmap
   documentation changes are allowed.

3. If no new issue/doc change is warranted, say so and explain the audit signal
   briefly.

HARD CONSTRAINTS
----------------
  - Do not implement roadmap issues. That is evolve_applier's job.
  - Do not close issues unless they are exact duplicates and you state which
    issue supersedes them.
  - Do not create duplicate issues. Search open and closed issues first.
  - Never write secrets, tokens, local private paths, or transcript content into
    GitHub issues.
  - If credentials/network block GitHub writes, output a concise blocked summary
    and do not pretend issues were created.

When done, output exactly:
  EVOLVE_REVIEW_COMPLETE created=<n> updated=<n> roadmap_pr=<url-or-none>
or:
  EVOLVE_REVIEW_ABORTED reason=<why>

PENDING LEGACY EVOLVE SUGGESTIONS
---------------------------------
{queue}
"""


def _last_evolve_ts(conn: sqlite3.Connection) -> int:
    """High-water timestamp of the most recent evolve-review pass, or 0."""
    try:
        row = conn.execute(
            "SELECT target FROM events WHERE kind='evolve_review_pass' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row or not row["target"]:
        return 0
    try:
        return int(row["target"])
    except (ValueError, TypeError):
        return 0


def _record_evolve_pass(conn: sqlite3.Connection, ts: int,
                        outcome: str) -> None:
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, "
            "created_at) VALUES (?, 'evolve_review_pass', ?, ?, ?)",
            (identity._session_id or "", str(ts), outcome[:300],
             int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("evolve_daemon: failed to record pass", exc_info=True)


def _pass_due(conn: sqlite3.Connection, now_t: int) -> bool:
    last = _last_evolve_ts(conn)
    return last <= 0 or now_t >= last + int(EVOLVE_REVIEW_INTERVAL_S)


def _pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Pending suggestions: not applied, not yet triaged."""
    try:
        return conn.execute(
            "SELECT id, suggestion, rationale FROM evolve "
            "WHERE applied=0 AND COALESCE(status,'pending')='pending' "
            "ORDER BY created_at ASC"
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _running_evolve_children(conn: sqlite3.Connection) -> list[str]:
    """Running reviewer task ids, reaping dead rows. Machine-wide
    single-flight: one evolve reviewer at a time across all servers."""
    from .helpers import alive
    try:
        rows = conn.execute(
            "SELECT id, pid FROM tasks WHERE ended_at IS NULL "
            "AND prompt LIKE ?",
            (EVOLVE_PROMPT_PREFIX + "%",),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    now = int(time.time())
    running: list[str] = []
    touched = False
    for r in rows:
        pid = int(r["pid"] or 0)
        if pid > 0 and not alive(pid):
            conn.execute(
                "UPDATE tasks SET ended_at=? WHERE id=? AND ended_at IS NULL",
                (now, r["id"]),
            )
            touched = True
            continue
        running.append(r["id"])
    if touched:
        conn.commit()
    return running


def run_evolve_pass(force: bool = False) -> str:
    """One evolve-review/audit pass.

    Status strings:
      'disabled'                  — knob off and not forced
      'not_due'                   — audit checked recently and no legacy input
      'reviewer_running n=<k>'    — a reviewer child is already in flight
      'spawned audit pending=<k>' — launched the audit/research child
      'spawn_error: …'            — spawn rejected
    """
    if EVOLVE_REVIEW_INTERVAL_S <= 0 and not force:
        return "disabled"
    conn = get_db()
    now_t = int(time.time())
    pending = _pending(conn)
    if not force and not _pass_due(conn, now_t):
        return "not_due"

    running = _running_evolve_children(conn)
    if running:
        out = f"reviewer_running n={len(running)}"
        _record_evolve_pass(conn, now_t, out)
        return out

    queue = (
        "\n".join(
            f"#{r['id']}: {r['suggestion']}"
            + (f"\n    rationale: {r['rationale']}" if r["rationale"] else "")
            for r in pending
        )
        if pending else "(none)"
    )
    prompt = EVOLVE_PROMPT.format(queue=queue)

    from .tools.spawn import spawn  # late import — avoids import cycle
    try:
        result = spawn(
            prompt=prompt,
            visible=False,
            capture_output=True,
            permission_mode="bypassPermissions",
            role="evolve_reviewer",
            write_origin="evolve",
            slim=True,
            extra_allowed_tools=(
                "Bash,Edit,Write,Read,Glob,Grep,WebSearch,WebFetch,"
                "mcp__thread-keeper__evolve_review,"
                "mcp__thread-keeper__evolve_decide,"
                "mcp__thread-keeper__broadcast"
            ),
        )
    except Exception as e:  # noqa: BLE001 — never crash the daemon
        out = f"spawn_error: {e}"
        _record_evolve_pass(conn, now_t, out)
        return out
    out = f"spawned audit pending={len(pending)} {str(result)[:120]}"
    _record_evolve_pass(conn, now_t, out)
    return out


def _serve_loop() -> None:
    while True:
        try:
            run_evolve_pass()
        except Exception:
            logger.debug("evolve_daemon tick failed", exc_info=True)
        time.sleep(EVOLVE_REVIEW_INTERVAL_S)


def start_evolve_daemon() -> None:
    """Idempotent starter. No-op when EVOLVE_REVIEW_INTERVAL_S<=0. Same
    cascade prevention as the other daemons: spawned children / non-
    foreground origins refuse to start it so spawn() can't recurse."""
    global _started
    if _started:
        return
    if EVOLVE_REVIEW_INTERVAL_S <= 0:
        return
    from .config import BACKGROUND_DAEMONS_ALLOWED
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    t = threading.Thread(
        target=_serve_loop, name="evolve_daemon", daemon=True,
    )
    t.start()
    _started = True
