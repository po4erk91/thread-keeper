"""Evolve applier — closes the format-evolution self-improvement loop, PR-gated.

`evolve_format()` files a suggestion to improve thread-keeper's brief format (the
session-start memory snapshot rendered by `render_brief` in `brief.py`). The
`evolve_reviewer` daemon PROMOTES the good ones — and the brief surfaces them
with a ★. Until now that's where it stopped: a human had to hand-edit `brief.py`.

This module automates the IMPLEMENTATION, with a pull request as the human gate:

  apply_evolve(evolve_id)  — look up a PROMOTED + not-applied suggestion, build
  an implementer prompt, and spawn() an `evolve_applier` child that EDITS
  `brief.py` (`render_brief`), adds/extends a GOLDEN render_brief test, runs the
  FULL suite, and opens a PR on a feature branch. The child NEVER pushes or
  commits to main (main has branch protection); a human reviews + merges.

On a successful `gh pr create`, the child calls
`evolve_mark_applied(evolve_id, pr_url)` → `applied=1`, so the suggestion stops
resurfacing in the brief / `evolve_review`.

The optional daemon (`THREADKEEPER_EVOLVE_APPLY_INTERVAL_S > 0`, default 0 = off)
periodically picks the oldest promoted+unapplied suggestion and fires
apply_evolve for it. Mirror of evolve_daemon / candidate_reviewer: foreground-
only, machine-wide single-flight via the prompt prefix, child runs with
`write_origin='evolve_apply'` so it can't recursively start daemons.

The objective gate the loop has lacked: the child must make a golden
render_brief test pass AND keep the full suite green before it may open the PR.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .config import CURATOR_REPORTS_DIR, DB_PATH, EVOLVE_APPLY_INTERVAL_S
from .db import get_db
from . import identity

logger = logging.getLogger(__name__)

_started = False

# First line of the implementer child's prompt. Added to
# shadow_review._INTERNAL_PROMPT_PREFIXES so the child's transcript (it edits
# code + runs gh) doesn't pollute extract/shadow learning windows on re-ingest.
EVOLVE_APPLY_PROMPT_PREFIX = "You are an EVOLVE APPLIER"

EVOLVE_APPLY_PROMPT = """\
You are an EVOLVE APPLIER for thread-keeper. A past session filed a suggestion
to improve thread-keeper's OWN brief format — the session-start memory snapshot
rendered by render_brief() in threadkeeper/brief.py. The evolve reviewer
PROMOTED it. Your job: IMPLEMENT it in code, VALIDATE with the test suite, and
open a PULL REQUEST. A human reviews and merges — you NEVER merge or touch main.

SUGGESTION #{evolve_id}
=======================
{suggestion}
{rationale_block}
REPO: {repo}   (run everything from here; the project venv is .venv/)

DO, strictly in order:

1. READ threadkeeper/brief.py — specifically render_brief() — and understand the
   sections it emits (core_memory, style, verbatim, open/idle/closed threads,
   evolve_pending, the trailing user-facing reminder, ...). Find exactly where
   the suggestion applies.

2. IMPLEMENT the suggestion by editing render_brief() (and only the helper(s) it
   needs). Keep the change surgical and in the existing house style — match the
   surrounding section idiom. Do NOT reformat or touch unrelated code.

3. ADD or EXTEND A GOLDEN TEST under tests/ (extend tests/test_brief_sections.py
   or add tests/test_evolve_apply_{evolve_id}.py). It MUST assert BOTH:
     (a) the NEW behavior/field the suggestion asks for actually appears in the
         rendered brief for a seeded fixture, AND
     (b) the EXISTING brief still renders — assert a couple of pre-existing
         sections (e.g. a seeded open thread, the evolve_pending ★, the
         user-facing reminder) are STILL present. A format change must not
         silently break the brief.
   Mirror the bootstrap/fixture style of tests/test_brief_sections.py and
   tests/test_evolve_daemon.py (env setup, module reload, render_brief(conn)).

4. RUN THE FULL SUITE from the repo root and read the FINAL summary line.
   IMPORTANT: your spawned environment sets THREADKEEPER_NO_EMBEDDINGS=1 (the
   slim-child default). That makes the embedding/vector tests (test_vec_search,
   test_delegated_search, test_onnx_embeddings) fail SPURIOUSLY — they are NOT
   related to your change. Run the suite with that var unset so embeddings are
   available:
       env -u THREADKEEPER_NO_EMBEDDINGS .venv/bin/python -m pytest -q
   It MUST report 0 failed (e.g. "=== N passed in Xs ==="). If a test fails that
   IS related to your brief change, FIX it (your change or your test) and re-run
   until green. Do NOT proceed while red.

5. OPEN A PR — only after the suite is GREEN:
     • Create a NEW feature branch (NEVER commit on main):
         git checkout -b {branch}
     • Choose a Conventional Commits type allowed by CONTRIBUTING.md and
       .github/workflows/pr-title.yml. Use `feat` for new/changed brief output
       and `fix` for a broken existing brief behavior. Do NOT use `evolve:`:
       the PR title gate rejects it.
     • Stage ONLY the files you changed, commit with a clear message, push:
         git add threadkeeper/brief.py tests/<your_test_file>
         git commit -m "<type>: <short imperative summary>"
         git push -u origin {branch}
     • Open the PR; the body MUST quote the suggestion text + rationale and note
       it was generated by evolve_applier:
         gh pr create --title "<type>: <short>" --body "<body incl. suggestion + rationale>"
     • Capture the PR URL that gh prints.

6. RECORD COMPLETION — ONLY after gh pr create printed a real PR URL — call the
   thread-keeper MCP tool:
       evolve_mark_applied(evolve_id={evolve_id}, pr_url="<the PR url>")
   This sets applied=1 so the suggestion stops resurfacing.

HARD CONSTRAINTS (non-negotiable):
  • NEVER commit, push, or force-push to main. main has branch protection.
  • NEVER merge the PR — a human does that.
  • If you CANNOT make the full suite green, do NOT open a PR and do NOT call
    evolve_mark_applied. Broadcast a one-line failure summary and stop.
  • When you report, paraphrase in plain language; don't cite internal IDs.

When finished, output exactly ONE final line:
  EVOLVE_APPLY_COMPLETE pr=<url>      (success), or
  EVOLVE_APPLY_ABORTED reason=<why>   (could not complete safely).
"""

CURATOR_REPORT_MARKER = "CURATOR_PASS_COMPLETE"
CURATOR_REPORT_APPLIED_KIND = "curator_report_applied"

CURATOR_REPORT_APPLY_PROMPT = """\
You are an EVOLVE APPLIER for thread-keeper. This work item is NOT a brief-code
evolve suggestion. It is a Curator REPORT: an advisory audit of existing
lessons, skills, and concepts. Your job is to apply ONLY the safe, still-valid
memory maintenance recommendations, then mark the report applied.

AUTHORIZATION AND SESSION DISCIPLINE
====================================
The human user explicitly authorized Evolve applier to process Curator reports
automatically. This is routine memory maintenance, not a new user-facing thread.
Do NOT call brief(), context(), open_thread(), close_thread(), note(),
session_end(), search(), or dialog_search(). The report plus the cross-check
tools below are the complete task context.

REPORT_PATH
===========
{report_path}

REPORT_TEXT
===========
{report_text}

REPO: {repo}

DO, strictly in order:

1. Cross-check current state before mutating anything:
   - Call lesson_list(k=500).
   - Call skill_list(include_archived=True).
   - For every lesson you might change/remove, call lesson_get(slug).
   - For every skill you might patch/delete, read its current SKILL.md first.
     Canonical skills live under the configured skills root; if a Read fails,
     skip that skill rather than guessing.

2. Apply only LOW-RISK, report-backed operations:
   - PATCH a skill only when the report gives a concrete fix and you can make
     an exact, validated skill_manage(action='patch'|'edit'|'write_file') call.
   - CONSOLIDATE lessons by writing the merged lesson with lesson_append(
     source='curator'), then lesson_remove only the superseded non-protected
     lessons whose content you already carried over.
   - PRUNE only clear background/system false positives, stale duplicate
     lessons, or superseded non-protected skills. Skip anything ambiguous.

3. Hard safety gates:
   - NEVER touch entries marked [PROTECTED] in the report.
   - NEVER touch lessons whose source is foreground or user.
   - NEVER delete or patch skills with origin=foreground, pinned=1, or
     tier=validated in skill_list output.
   - NEVER mutate concepts for now; report concept recommendations as skipped.
   - Do not use Bash, git, gh, Edit, or raw file writes. Use only the MCP memory
     tools plus Read. This is live memory maintenance, not a code PR.

4. After applying safe changes, or after deciding there are no safe changes,
   call:
     evolve_mark_curator_report_applied(
       report_path="{report_path}",
       summary="<one-line summary of applied/skipped counts>"
     )
   Do not call it before the checks and mutations above.

5. If you cannot complete safely, do NOT mark the report applied. Broadcast a
   concise failure summary and stop.

When finished, output exactly ONE final line:
  CURATOR_REPORT_APPLY_COMPLETE report={report_name}
or:
  CURATOR_REPORT_APPLY_ABORTED reason=<why>
"""


def _repo_root() -> Path:
    """Repository root = the package's parent dir (threadkeeper/.. )."""
    return Path(__file__).resolve().parent.parent


def _slug(text: str, maxlen: int = 32) -> str:
    """kebab-case slug for a branch name; alnum+dash only, lowercased."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = s[:maxlen].rstrip("-")
    return s or "change"


def branch_name(evolve_id: int, suggestion: str) -> str:
    return f"evolve/apply-{int(evolve_id)}-{_slug(suggestion)}"


def _report_key(path: Path) -> str:
    return str(path.expanduser().resolve())


def _resolve_curator_report_path(report_path: str) -> Optional[Path]:
    raw = (report_path or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = CURATOR_REPORTS_DIR / p
    try:
        resolved = p.resolve()
        root = CURATOR_REPORTS_DIR.expanduser().resolve()
    except OSError:
        return None
    if root not in resolved.parents and resolved != root:
        return None
    if resolved.name.startswith("REPORT-") and resolved.suffix == ".md":
        return resolved
    return None


def _read_curator_report(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _is_complete_curator_report(path: Path) -> bool:
    return CURATOR_REPORT_MARKER in _read_curator_report(path)


def _curator_report_applied(conn: sqlite3.Connection, path: Path) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM events WHERE kind=? AND target=? LIMIT 1",
            (CURATOR_REPORT_APPLIED_KIND, _report_key(path)),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _latest_complete_curator_report(
    conn: sqlite3.Connection,
) -> Optional[Path]:
    """Latest complete Curator report if it has not already been applied.

    Older unapplied reports are treated as superseded by the newest complete
    inventory snapshot. This keeps the applier from replaying stale advice.
    """
    try:
        reports = sorted(
            CURATOR_REPORTS_DIR.glob("REPORT-*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for path in reports:
        if not _is_complete_curator_report(path):
            return None
        return None if _curator_report_applied(conn, path) else path
    return None


def _pending_curator_reports(conn: sqlite3.Connection) -> list[Path]:
    latest = _latest_complete_curator_report(conn)
    return [latest] if latest else []


def build_apply_prompt(evolve_id: int, suggestion: str,
                       rationale: Optional[str],
                       repo_root: Optional[Path] = None) -> str:
    """Render the implementer child's prompt for one suggestion."""
    repo = str(repo_root or _repo_root())
    rationale_block = (
        f"RATIONALE: {rationale}\n" if (rationale or "").strip() else ""
    )
    return EVOLVE_APPLY_PROMPT.format(
        evolve_id=int(evolve_id),
        suggestion=suggestion,
        rationale_block=rationale_block,
        repo=repo,
        branch=branch_name(evolve_id, suggestion),
    )


def build_curator_report_apply_prompt(
    report_path: Path,
    report_text: str,
    repo_root: Optional[Path] = None,
) -> str:
    repo = str(repo_root or _repo_root())
    key = _report_key(report_path)
    return CURATOR_REPORT_APPLY_PROMPT.format(
        report_path=key,
        report_name=report_path.name,
        report_text=report_text,
        repo=repo,
    )


def _get_promoted_unapplied(conn: sqlite3.Connection,
                            evolve_id: int) -> Optional[sqlite3.Row]:
    """Fetch a single evolve row IF it is promoted and not yet applied."""
    try:
        return conn.execute(
            "SELECT id, suggestion, rationale FROM evolve "
            "WHERE id=? AND applied=0 "
            "AND COALESCE(status,'pending')='promoted'",
            (int(evolve_id),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def _promoted_unapplied(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All promoted+unapplied suggestions, oldest first (FIFO drain order)."""
    try:
        return conn.execute(
            "SELECT id, suggestion, rationale FROM evolve "
            "WHERE applied=0 AND COALESCE(status,'pending')='promoted' "
            "ORDER BY created_at ASC, id ASC"
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _row_exists(conn: sqlite3.Connection, evolve_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM evolve WHERE id=?", (int(evolve_id),)
    ).fetchone() is not None


def _running_applier_children(conn: sqlite3.Connection) -> list[str]:
    """Running applier task ids, reaping dead rows. Machine-wide single-flight:
    one applier child at a time across all servers (two would both edit
    brief.py and collide on the branch)."""
    from .helpers import alive
    try:
        rows = conn.execute(
            "SELECT id, pid FROM tasks WHERE ended_at IS NULL "
            "AND prompt LIKE ?",
            (EVOLVE_APPLY_PROMPT_PREFIX + "%",),
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


@contextmanager
def _apply_spawn_lock():
    """Cross-process guard for the check-running-then-spawn critical section.

    The tasks-table single-flight check is necessary but not sufficient when a
    manual trigger and a daemon tick arrive in the same second: both processes
    can observe no running task before either spawn() inserts its row. This
    short file lock closes that race without holding a lock for the child run.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - thread-keeper runs on Unix CLIs.
        yield True
        return
    lock_path = DB_PATH.parent / "evolve-applier.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def mark_applied(conn: sqlite3.Connection, evolve_id: int,
                 pr_url: str) -> str:
    """Set applied=1 for an evolve row and record the PR. The PR-gate: this is
    only ever reached once a real PR exists (the child calls it after
    `gh pr create`). Returns a status string."""
    if not _row_exists(conn, evolve_id):
        return f"ERR evolve_not_found={evolve_id}"
    conn.execute(
        "UPDATE evolve SET applied=1 WHERE id=?", (int(evolve_id),)
    )
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, 'evolve_applied', ?, ?, ?)",
            (identity._session_id or "", str(int(evolve_id)),
             (pr_url or "")[:300], int(time.time())),
        )
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return f"ok id={evolve_id} applied=1 pr={pr_url}"


def mark_curator_report_applied(
    conn: sqlite3.Connection,
    report_path: str,
    summary: str,
) -> str:
    """Record that an evolve_applier child processed a Curator report."""
    path = _resolve_curator_report_path(report_path)
    if path is None:
        return "ERR invalid_report_path"
    if not path.exists():
        return f"ERR report_not_found={path}"
    if not _is_complete_curator_report(path):
        return f"ERR report_incomplete={path.name}"
    key = _report_key(path)
    if _curator_report_applied(conn, path):
        return f"ok report={path.name} already_applied=1"
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            identity._session_id or "",
            CURATOR_REPORT_APPLIED_KIND,
            key,
            (summary or "")[:300],
            int(time.time()),
        ),
    )
    conn.commit()
    return f"ok report={path.name} applied=1"


def apply_curator_report(report_path: str = "") -> str:
    """Spawn an `evolve_applier` child to apply the latest complete Curator
    report, or a specific report path when provided.

    This shares the same single-flight guard as format evolve apply, so report
    cleanup cannot race code-evolve implementation.
    """
    with _apply_spawn_lock() as locked:
        if not locked:
            return "applier_running n=1 (single-flight lock)"

        conn = get_db()
        if report_path.strip():
            path = _resolve_curator_report_path(report_path)
            if path is None:
                return "ERR invalid_report_path"
            if not path.exists():
                return f"ERR report_not_found={path}"
            if not _is_complete_curator_report(path):
                return f"ERR report_incomplete={path.name}"
            if _curator_report_applied(conn, path):
                return f"ERR report_already_applied={path.name}"
        else:
            pending = _pending_curator_reports(conn)
            if not pending:
                return "no_curator_report"
            path = pending[0]

        running = _running_applier_children(conn)
        if running:
            return f"applier_running n={len(running)} (single-flight)"

        report_text = _read_curator_report(path)
        if not report_text:
            return f"ERR report_empty={path.name}"
        repo_root = _repo_root()
        prompt = build_curator_report_apply_prompt(path, report_text,
                                                   repo_root)

        from .tools.spawn import spawn  # late import — avoids import cycle
        try:
            result = spawn(
                prompt=prompt,
                cwd=str(repo_root),
                visible=False,
                capture_output=True,
                permission_mode="auto",
                append_system=(
                    "Curator-report apply is a pre-authorized Evolve applier "
                    "maintenance task. Do not open a thread or call "
                    "brief/context/note/session_end/search tools; use only "
                    "the explicit report cross-check and mutation tools named "
                    "in the user prompt."
                ),
                role="evolve_applier",
                write_origin="evolve_apply",
                slim=True,
                extra_allowed_tools=(
                    "Read,"
                    "mcp__thread-keeper__lesson_list,"
                    "mcp__thread-keeper__lesson_get,"
                    "mcp__thread-keeper__lesson_append,"
                    "mcp__thread-keeper__lesson_remove,"
                    "mcp__thread-keeper__skill_list,"
                    "mcp__thread-keeper__skill_manage,"
                    "mcp__thread-keeper__evolve_mark_curator_report_applied,"
                    "mcp__thread-keeper__broadcast"
                ),
            )
        except Exception as e:  # noqa: BLE001 — never crash the daemon/tool
            return f"spawn_error: {e}"
        return f"spawned curator_report={path.name} {str(result)[:140]}"


def apply_evolve(evolve_id: int) -> str:
    """Spawn an `evolve_applier` child to IMPLEMENT a promoted suggestion and
    open a PR. The manual entry point (always available — does NOT depend on the
    daemon interval). Does NOT set applied=1; that happens only when the child
    reports a real PR via evolve_mark_applied → mark_applied().

    Status strings:
      'ERR evolve_not_found=<id>'      — no such row
      'ERR not_actionable=<id> …'      — exists but not promoted+unapplied
      'applier_running n=<k>'          — an applier child is already in flight
      'spawned evolve_id=<id> …'       — launched the implementer child
      'spawn_error: …'                 — spawn rejected
    """
    with _apply_spawn_lock() as locked:
        if not locked:
            return "applier_running n=1 (single-flight lock)"

        conn = get_db()
        if not _row_exists(conn, evolve_id):
            return f"ERR evolve_not_found={evolve_id}"
        row = _get_promoted_unapplied(conn, evolve_id)
        if not row:
            cur = conn.execute(
                "SELECT applied, COALESCE(status,'pending') AS st FROM evolve "
                "WHERE id=?", (int(evolve_id),),
            ).fetchone()
            return (
                f"ERR not_actionable={evolve_id} applied={cur['applied']} "
                f"status={cur['st']} (need promoted + applied=0)"
            )

        running = _running_applier_children(conn)
        if running:
            return f"applier_running n={len(running)} (single-flight)"

        repo_root = _repo_root()
        prompt = build_apply_prompt(
            row["id"], row["suggestion"], row["rationale"], repo_root
        )

        from .tools.spawn import spawn  # late import — avoids import cycle
        try:
            result = spawn(
                prompt=prompt,
                cwd=str(repo_root),
                visible=False,
                capture_output=True,
                permission_mode="bypassPermissions",
                role="evolve_applier",
                write_origin="evolve_apply",
                slim=True,
                extra_allowed_tools=(
                    "Bash,Edit,Write,Read,Glob,Grep,"
                    "mcp__thread-keeper__evolve_mark_applied,"
                    "mcp__thread-keeper__evolve_review,"
                    "mcp__thread-keeper__broadcast"
                ),
            )
        except Exception as e:  # noqa: BLE001 — never crash the daemon/tool
            return f"spawn_error: {e}"
        return f"spawned evolve_id={evolve_id} {str(result)[:140]}"


# ── Optional daemon ───────────────────────────────────────────────────────────


def _last_apply_ts(conn: sqlite3.Connection) -> int:
    """High-water timestamp of the most recent apply pass, or 0."""
    try:
        row = conn.execute(
            "SELECT target FROM events WHERE kind='evolve_apply_pass' "
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


def _record_apply_pass(conn: sqlite3.Connection, ts: int,
                       outcome: str) -> None:
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, "
            "created_at) VALUES (?, 'evolve_apply_pass', ?, ?, ?)",
            (identity._session_id or "", str(ts), outcome[:300],
             int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("evolve_applier: failed to record pass", exc_info=True)


def run_evolve_apply_pass(force: bool = False) -> str:
    """One apply pass: apply the latest complete Curator report first, then
    pick the oldest promoted+unapplied suggestion.

    Status strings:
      'disabled'                  — knob off and not forced
      'no_apply_work'             — no Curator report or promoted suggestion
      'applier_running n=<k>'     — an applier child is already in flight
      'spawned curator_report=…'  — launched the memory-maintenance child
      'spawned evolve_id=<id> …'  — launched the implementer child
      (or any apply_evolve error string)
    """
    if EVOLVE_APPLY_INTERVAL_S <= 0 and not force:
        return "disabled"
    conn = get_db()
    now_t = int(time.time())

    running = _running_applier_children(conn)
    if running:
        out = f"applier_running n={len(running)}"
        _record_apply_pass(conn, now_t, out)
        return out

    reports = _pending_curator_reports(conn)
    if reports:
        report = reports[0]
        out = apply_curator_report(str(report))
        _record_apply_pass(conn, now_t, f"curator_report={report.name} {out}")
        return out

    pending = _promoted_unapplied(conn)
    if not pending:
        _record_apply_pass(conn, now_t, "no_apply_work")
        return "no_apply_work"

    top = pending[0]
    out = apply_evolve(top["id"])
    _record_apply_pass(conn, now_t, f"id={top['id']} {out}")
    return out


def _serve_loop() -> None:
    while True:
        try:
            run_evolve_apply_pass()
        except Exception:
            logger.debug("evolve_applier tick failed", exc_info=True)
        time.sleep(EVOLVE_APPLY_INTERVAL_S)


def start_evolve_applier_daemon() -> None:
    """Idempotent starter. No-op when EVOLVE_APPLY_INTERVAL_S<=0. Spawned
    children / non-foreground origins refuse to start it so spawn() can't
    recurse (same cascade prevention as the other daemons)."""
    global _started
    if _started:
        return
    if EVOLVE_APPLY_INTERVAL_S <= 0:
        return
    from .config import BACKGROUND_DAEMONS_ALLOWED
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    t = threading.Thread(
        target=_serve_loop, name="evolve_applier_daemon", daemon=True,
    )
    t.start()
    _started = True
