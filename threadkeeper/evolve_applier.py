"""Evolve applier — PR-gated implementation worker for roadmap issues.

Primary path:

  apply_roadmap_issue(issue_number=0) — pick the first startable open GitHub
  issue (`roadmap` label first, then FIFO; skip/advance past issues that cannot
  be claimed), build an implementer prompt, spawn an `evolve_applier` child that
  edits code/docs, runs the full suite, opens a PR with `Closes #N`, then calls
  `evolve_mark_roadmap_issue_applied(issue_number, pr_url)`.

  Poison-issue guard: each spawn records a `roadmap_issue_attempt` event. An
  escalating backoff (base * 2^(attempts-1), default base 2 days) defers
  re-selection of an issue whose child keeps aborting without a PR, and after
  `ROADMAP_ISSUE_MAX_ATTEMPTS` the issue is dead-lettered — a `blocked` label
  plus a one-time summary comment — and dropped from the auto-drain until a
  human intervenes. A successful child writes `roadmap_issue_applied` (checked
  first everywhere), so only genuinely-failing issues accrue backoff.

Fallback paths:

  apply_curator_report(report_path="") — apply safe Curator memory-maintenance
  recommendations using memory MCP tools only; no code PR.

  apply_evolve(evolve_id) — implement a legacy promoted `evolve_format`
  suggestion behind a PR and call `evolve_mark_applied(evolve_id, pr_url)`.

All code-changing paths are PR-gated. The child never commits to main or marks
work applied without a real PR URL. The file lock plus prompt-prefix running
task check enforce one applier child at a time across foreground servers.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from .config import (
    CURATOR_REPORTS_DIR,
    DB_PATH,
    EVOLVE_APPLY_INTERVAL_S,
    EVOLVE_AUTO_CLONE,
    EVOLVE_REPO_BRANCH,
    EVOLVE_REPO_ROOT,
    EVOLVE_REPO_URL,
    EVOLVE_TRUST_LABELS,
    EVOLVE_TRUSTED_AUTHOR_ASSOCIATIONS,
    ROADMAP_CLAIM_RACE_WINDOW_S,
    ROADMAP_ISSUE_BACKOFF_BASE_S,
    ROADMAP_ISSUE_MAX_ATTEMPTS,
)
from .db import get_db
from .helpers import daemon_sleep
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
-----------------------
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
ROADMAP_ISSUE_APPLIED_KIND = "roadmap_issue_applied"
# Local-only event recording the full host identity behind a posted claim. The
# public claim comment carries just the opaque _host_branch_slug() token; this
# event keeps hostname/PID/git-rev in the local DB for multi-host triage (#63).
ROADMAP_ISSUE_CLAIM_HOST_KIND = "roadmap_issue_claim_host"
# Poison-issue failure ledger. One `roadmap_issue_attempt` row is recorded per
# spawned implementer child; `roadmap_issue_dead_letter` is the one-time marker
# written when an issue crosses the attempt cap and is flagged for a human.
ROADMAP_ISSUE_ATTEMPT_KIND = "roadmap_issue_attempt"
ROADMAP_ISSUE_DEAD_LETTER_KIND = "roadmap_issue_dead_letter"
# Label applied to a dead-lettered issue so it is visibly excluded from the
# auto-drain and surfaced for a human (composes with the #50 skip-label gate).
ROADMAP_ISSUE_BLOCKED_LABEL = "blocked"
# Upper bound on the escalating backoff window regardless of attempt count.
ROADMAP_ISSUE_BACKOFF_CAP_S = 30 * 24 * 60 * 60
ROADMAP_ISSUE_FETCH_LIMIT = 1000
ROADMAP_ISSUE_FETCH_PAGE_SIZE = 100
ROADMAP_ISSUE_CLAIM_MARKER = "<!-- thread-keeper:evolve-applier-claim -->"
ROADMAP_ISSUE_CLAIM_TTL_S = 24 * 60 * 60

CURATOR_REPORT_APPLY_PROMPT = """\
You are an EVOLVE APPLIER for thread-keeper. This work item is NOT a brief-code
evolve suggestion. It is a Curator REPORT: an advisory audit of existing
lessons, skills, and concepts. Your job is to apply ONLY the safe, still-valid
memory maintenance recommendations, then mark the report applied.

AUTHORIZATION AND SESSION DISCIPLINE
------------------------------------
The human user explicitly authorized Evolve applier to process Curator reports
automatically. This is routine memory maintenance, not a new user-facing thread.
Do NOT call brief(), context(), open_thread(), close_thread(), note(),
session_end(), search(), or dialog_search(). The report plus the cross-check
tools below are the complete task context.

REPORT_PATH
-----------
{report_path}

REPORT_TEXT
-----------
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
   - If the report has concept recommendations, call list_concepts(k=200) and
     expand_concept(<id>) for any concept you might consolidate/prune so you act
     on the current store, not the snapshot baked into the report text.

2. Apply only LOW-RISK, report-backed operations:
   - PATCH a skill only when the report gives a concrete fix and you can make
     an exact, validated skill_manage(action='patch'|'edit'|'write_file') call.
   - CONSOLIDATE lessons by writing the merged lesson with lesson_append(
     source='curator'), then lesson_remove only the superseded non-protected
     lessons whose content you already carried over.
   - PRUNE only clear background/system false positives, stale duplicate
     lessons, or superseded non-protected skills. Skip anything ambiguous.
   - CONSOLIDATE_CONCEPT / PRUNE_CONCEPT: apply clear concept recommendations
     via concept_manage. CONSOLIDATE_CONCEPT → concept_manage(
     action='consolidate', concept_id=<kept-id>, merge_ids='<id-a>,<id-b>').
     PRUNE_CONCEPT → concept_manage(action='remove', concept_id=<id>). Use
     expand_concept first if you need the full description to judge a merge.

3. Hard safety gates:
   - NEVER touch entries marked [PROTECTED] in the report.
   - NEVER touch lessons whose source is foreground or user.
   - NEVER delete or patch skills with origin=foreground, pinned=1, or
     tier=validated in skill_list output.
   - Concepts are ALL system-generated (no foreground/pinned concept class), so
     you MAY apply clear CONSOLIDATE_CONCEPT / PRUNE_CONCEPT recommendations via
     concept_manage. Skip ambiguous merges and any confidence change you are not
     confident about.
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

ROADMAP_ISSUE_APPLY_PROMPT = """\
You are an EVOLVE APPLIER for thread-keeper. This work item is a GitHub issue
from the project roadmap/backlog. Your job is to implement exactly ONE issue,
validate it, open a pull request, then mark the issue work as handed off.

ISSUE #{issue_number}: {issue_title}
---------------------
URL: {issue_url}
LABELS: {issue_labels}

ISSUE BODY
----------
{issue_body}

REPO: {repo}   (run everything from here; the project venv is .venv/)

DO, strictly in order:

1. Re-check current issue state:
     gh issue view {issue_number} --json number,title,state,labels,body,url
   Abort if it is no longer open, or if it is clearly already implemented by
   current code/docs. If it is already implemented, comment on the issue with
   the evidence and stop without marking it applied.

CLAIM
-----
Before spawning you, the parent Evolve applier already posted a GitHub issue
comment containing `{claim_marker}`. Treat that as your active work claim so
other agents do not start the same issue in parallel. Do not remove it. If you
abort after doing meaningful investigation, leave a normal issue comment with
the blocker/status so a later agent has enough context.

2. Read the relevant code and docs before editing. Also read docs/ROADMAP.md
   and the issue body so the implementation matches the tracked roadmap item.

3. Implement only this issue. Keep the change surgical, update README /
   docs/ARCHITECTURE.md / docs/ROADMAP.md / CHANGELOG.md when behavior or
   documented state changes, and add focused tests proportional to risk.

4. Run the full suite from the repo root and read the FINAL summary line:
     env -u THREADKEEPER_NO_EMBEDDINGS .venv/bin/python -m pytest -q
   It MUST report 0 failed. Fix related failures and re-run until green.

5. Open a PR on a new branch; never commit on main:
     git checkout -b {branch}
     git add <only files you changed>
     git commit -m "<type>: <short imperative summary>"
     git push -u origin {branch}
     gh pr create --title "<type>: <short>" --body "<body incl. Closes #{issue_number}>"
   Use an allowed Conventional Commit type (`feat`, `fix`, `docs`, `test`,
   etc.). The PR body MUST include `Closes #{issue_number}` so GitHub closes
   the issue after human merge.

6. ONLY after gh prints a real PR URL, call:
     evolve_mark_roadmap_issue_applied(
       issue_number={issue_number},
       pr_url="<the PR url>"
     )

HARD CONSTRAINTS:
  • Implement one issue only; do not batch multiple roadmap items.
  • Never push or commit to main, never merge the PR.
  • Do not mark the issue applied without a real PR URL.
  • If blocked by credentials/network/permissions, comment on the issue with
    the blocker and stop without marking it applied.

When finished, output exactly ONE final line:
  ROADMAP_ISSUE_APPLY_COMPLETE issue=#{issue_number} pr=<url>
or:
  ROADMAP_ISSUE_APPLY_ABORTED issue=#{issue_number} reason=<why>
"""


EVOLVE_CLONE_TIMEOUT_S = 600
EVOLVE_VENV_TIMEOUT_S = 1800


def _managed_repo_dir() -> Path:
    """Managed checkout used when thread-keeper is installed without a source
    tree (PyPI/site-packages). Co-located with the DB so it has a stable home
    across restarts."""
    return (DB_PATH.parent / "evolve-repo").expanduser()


def _resolve_repo_root() -> Path:
    """Pure repo-root resolution — no network, no git subprocess.

    Order:
      1. explicit EVOLVE_REPO_ROOT override;
      2. the package's parent dir when it is itself a checkout (editable
         install.sh), detected cheaply by a `.git` entry;
      3. the managed checkout under the DB dir (PyPI/site-packages installs),
         which `_ensure_repo_ready()` auto-provisions on first use."""
    override = (EVOLVE_REPO_ROOT or "").strip()
    if override:
        return Path(override).expanduser()
    pkg_parent = Path(__file__).resolve().parent.parent
    if (pkg_parent / ".git").exists():
        return pkg_parent
    return _managed_repo_dir()


def _repo_root() -> Path:
    """Git checkout the evolve loops operate on (see `_resolve_repo_root`)."""
    return _resolve_repo_root()


def _is_git_repo(path: Path) -> bool:
    """True if `path` is inside a git work tree. Best-effort: any probe failure
    (git missing, path absent, not a repo) yields False rather than raising."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            text=True, capture_output=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and (proc.stdout or "").strip() == "true"


def _override_not_git_error(root: Path) -> str:
    """The explicit EVOLVE_REPO_ROOT is not a checkout — never auto-clone into a
    user-chosen path; tell them to fix it."""
    return (
        f"ERR repo_root_not_git={root} (the path in THREADKEEPER_EVOLVE_REPO_ROOT"
        " is not a git checkout; point it at a real thread-keeper clone)"
    )


def _autoclone_disabled_error(root: Path) -> str:
    """No checkout and auto-clone is turned off — the only way the evolve loops
    don't work by default."""
    return (
        f"ERR evolve_repo_unavailable={root} (no git checkout and auto-clone is "
        "disabled via THREADKEEPER_EVOLVE_AUTO_CLONE=0; set "
        "THREADKEEPER_EVOLVE_REPO_ROOT to a checkout or re-enable auto-clone)"
    )


@contextmanager
def _repo_provision_lock():
    """Serialize clone/venv provisioning across foreground servers so two ticks
    don't race a half-finished clone into the same managed dir. Blocking: the
    loser waits, then re-checks under the lock and reuses the finished clone."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - thread-keeper runs on Unix CLIs.
        yield
        return
    lock_path = DB_PATH.parent / "evolve-repo-provision.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _run(cmd: list[str], timeout: int, cwd: Optional[Path] = None) -> str:
    """Run a provisioning subprocess. Returns '' on success or a short error."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True, capture_output=True, timeout=timeout, check=False,
        )
    except FileNotFoundError:
        return f"{cmd[0]}_not_found"
    except subprocess.TimeoutExpired:
        return f"{cmd[0]}_timeout"
    except OSError as e:
        return f"{cmd[0]}_error: {e}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        return (err[-1] if err else f"exit={proc.returncode}")[:180]
    return ""


def _ensure_managed_venv(dest: Path) -> str:
    """Create dest/.venv and editable-install thread-keeper with semantic+test
    extras so the evolve children can run `.venv/bin/python -m pytest`. Idempotent
    — a present venv python is treated as ready. Returns '' or an ERR string."""
    import sys
    venv_py = dest / ".venv" / "bin" / "python"
    if venv_py.exists():
        return ""
    err = _run([sys.executable, "-m", "venv", str(dest / ".venv")],
               EVOLVE_VENV_TIMEOUT_S)
    if err:
        return f"ERR evolve_venv_create_failed={dest}: {err}"
    pip = dest / ".venv" / "bin" / "pip"
    err = _run([str(pip), "install", "-q", "-e", f"{dest}[semantic,dev]"],
               EVOLVE_VENV_TIMEOUT_S)
    if err:
        return f"ERR evolve_venv_install_failed={dest}: {err}"
    return ""


def _provision_managed_repo(dest: Path) -> str:
    """Clone the canonical repo into `dest` and provision its venv. Serialized
    and idempotent: re-checks under the lock so a concurrent winner's clone is
    reused. Returns '' on success or an ERR string."""
    with _repo_provision_lock():
        if _is_git_repo(dest):
            return _ensure_managed_venv(dest)
        if dest.exists() and any(dest.iterdir()):
            return (
                f"ERR evolve_repo_dir_not_empty={dest} (cannot auto-clone into a "
                "non-empty non-git directory; clear it or set "
                "THREADKEEPER_EVOLVE_REPO_ROOT)"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        err = _run(
            ["git", "clone", "--quiet", "--branch", str(EVOLVE_REPO_BRANCH),
             str(EVOLVE_REPO_URL), str(dest)],
            EVOLVE_CLONE_TIMEOUT_S,
        )
        if err:
            return f"ERR evolve_repo_clone_failed={dest}: {err}"
        return _ensure_managed_venv(dest)


def _ensure_repo_ready() -> tuple[Path, str]:
    """Resolve the repo root and make sure it is a usable git checkout, cloning
    a managed one on first use when needed. Returns (root, error); error is ''
    on success. This is the gate every code/PR path and the reviewer call."""
    root = _resolve_repo_root()
    if _is_git_repo(root):
        return root, ""
    if (EVOLVE_REPO_ROOT or "").strip():
        # Explicit override that isn't a checkout — never auto-clone there.
        return root, _override_not_git_error(root)
    if not EVOLVE_AUTO_CLONE:
        return root, _autoclone_disabled_error(root)
    err = _provision_managed_repo(root)
    if err:
        return root, err
    return root, ""


def _slug(text: str, maxlen: int = 32) -> str:
    """kebab-case slug for a branch name; alnum+dash only, lowercased."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = s[:maxlen].rstrip("-")
    return s or "change"


def branch_name(evolve_id: int, suggestion: str) -> str:
    return f"evolve/apply-{int(evolve_id)}-{_slug(suggestion)}"


def roadmap_issue_branch_name(issue_number: int, title: str) -> str:
    """Branch name for a roadmap-issue PR. Includes a 6-char hostname hash so
    two hosts racing on the same issue do not collide on `git push -u origin`."""
    return (
        f"roadmap/issue-{int(issue_number)}-{_slug(title)}-{_host_branch_slug()}"
    )


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


def _issue_labels(issue: dict) -> list[str]:
    labels = issue.get("labels") or []
    out: list[str] = []
    for label in labels:
        if isinstance(label, dict):
            name = label.get("name")
        else:
            name = str(label)
        if name:
            out.append(str(name))
    return out


def _issue_author_association(issue: dict) -> str:
    """Normalized GitHub author association for an issue (e.g. 'OWNER',
    'MEMBER', 'NONE'). Missing/blank → 'NONE' so the trust gate fails closed."""
    raw = issue.get("authorAssociation")
    return str(raw or "").strip().upper() or "NONE"


def _issue_author_trusted(issue: dict) -> bool:
    """Whether an open issue is eligible for AUTONOMOUS pickup by the applier.

    This repo is public, so any GitHub account can open an issue whose body is
    then injected into a permission-bypassing implementer child. Trust comes
    from EITHER a maintainer-level author association
    (EVOLVE_TRUSTED_AUTHOR_ASSOCIATIONS — OWNER/MEMBER/COLLABORATOR by default)
    OR a maintainer-applied trust label (EVOLVE_TRUST_LABELS; empty by
    default). On a public repo only collaborators can apply labels, so a trust
    label is itself a maintainer endorsement. Exact-issue invocation (a human
    naming the number) bypasses this gate upstream as explicit promotion (#63).
    """
    trusted = {str(a).strip().upper() for a in EVOLVE_TRUSTED_AUTHOR_ASSOCIATIONS}
    if _issue_author_association(issue) in trusted:
        return True
    label_set = {str(name).strip().lower() for name in EVOLVE_TRUST_LABELS
                 if str(name).strip()}
    if label_set and {name.lower() for name in _issue_labels(issue)} & label_set:
        return True
    return False


def _fetch_open_issues(repo_root: Optional[Path] = None) -> tuple[list[dict], str]:
    """Fetch open GitHub issues via the REST API. Returns (issues, error).

    Uses `gh api` rather than `gh issue list --json` because the autonomous-
    pickup author-trust gate (#63) needs each issue's `author_association`
    (OWNER/MEMBER/COLLABORATOR/…), which the `gh issue list --json` field set
    does not expose. The REST `/issues` endpoint also returns pull requests (a
    PR is an issue in GitHub's data model); those are filtered out so the
    applier never treats a PR as backlog.

    Pagination is explicit and oldest-first (`sort=created&direction=asc`) so
    the downstream roadmap/FIFO drain sees the oldest open issues even when the
    backlog grows past one GitHub page. A generous local window is retained only
    as a runaway guard; if it truncates, a warning records the exact overflow so
    an operator does not mistake a cap for an empty/startable-free queue.
    Returned dicts keep the prior internal shape (`number/title/labels/body/url`)
    plus the `authorAssociation`/`authorLogin` fields the gate reads.
    """
    repo = str(repo_root or _repo_root())
    cmd = [
        "gh", "api", "--paginate", "--slurp",
        "-H", "Accept: application/vnd.github+json",
        (
            "repos/{owner}/{repo}/issues"
            "?state=open&sort=created&direction=asc"
            f"&per_page={ROADMAP_ISSUE_FETCH_PAGE_SIZE}"
        ),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return [], "gh_not_found"
    except subprocess.TimeoutExpired:
        return [], "gh_issue_list_timeout"
    except OSError as e:
        return [], f"gh_issue_list_error: {e}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        msg = err[-1] if err else f"exit={proc.returncode}"
        return [], f"gh_issue_list_failed: {msg[:180]}"
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as e:
        return [], f"gh_issue_list_bad_json: {e}"
    if not isinstance(data, list):
        return [], "gh_issue_list_bad_shape"
    if data and all(isinstance(page, list) for page in data):
        items = [
            item
            for page in data
            for item in page
            if isinstance(item, dict)
        ]
    else:
        # Defensive fallback for tests/older gh behavior without --slurp.
        items = [item for item in data if isinstance(item, dict)]
    open_issues = [
        item for item in items
        if not item.get("pull_request")
    ]
    total_open = len(open_issues)
    if total_open > ROADMAP_ISSUE_FETCH_LIMIT:
        skipped = total_open - ROADMAP_ISSUE_FETCH_LIMIT
        logger.warning(
            "evolve_applier: %d open GitHub issues exceeds roadmap issue fetch "
            "window %d; %d newest issue(s) not considered",
            total_open,
            ROADMAP_ISSUE_FETCH_LIMIT,
            skipped,
        )
        open_issues = open_issues[:ROADMAP_ISSUE_FETCH_LIMIT]
    out: list[dict] = []
    for item in open_issues:
        user = item.get("user")
        login = user.get("login") if isinstance(user, dict) else ""
        out.append({
            "number": item.get("number"),
            "title": item.get("title") or "",
            "labels": item.get("labels") or [],
            "body": item.get("body") or "",
            "url": item.get("html_url") or item.get("url") or "",
            "authorAssociation": item.get("author_association") or "",
            "authorLogin": login or "",
        })
    return out, ""


def _fetch_issue_comments(
    issue_number: int,
    repo_root: Optional[Path] = None,
) -> tuple[list[dict], str]:
    """Fetch issue comments via gh. Returns (comments, error)."""
    repo = str(repo_root or _repo_root())
    cmd = [
        "gh", "issue", "view", str(int(issue_number)),
        "--json", "comments",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return [], "gh_not_found"
    except subprocess.TimeoutExpired:
        return [], "gh_issue_comments_timeout"
    except OSError as e:
        return [], f"gh_issue_comments_error: {e}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        msg = err[-1] if err else f"exit={proc.returncode}"
        return [], f"gh_issue_comments_failed: {msg[:180]}"
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as e:
        return [], f"gh_issue_comments_bad_json: {e}"
    comments = data.get("comments") if isinstance(data, dict) else None
    if not isinstance(comments, list):
        return [], "gh_issue_comments_bad_shape"
    return comments, ""


def _parse_gh_timestamp(value: object) -> Optional[float]:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _issue_comment_is_active_claim(comment: dict, now_t: float) -> bool:
    body = str(comment.get("body") or "")
    if ROADMAP_ISSUE_CLAIM_MARKER not in body:
        return False
    created_at = _parse_gh_timestamp(comment.get("createdAt"))
    if created_at is None:
        return True
    return now_t < created_at + ROADMAP_ISSUE_CLAIM_TTL_S


def _issue_has_active_claim(
    issue_number: int,
    repo_root: Optional[Path] = None,
    now_t: Optional[float] = None,
) -> tuple[bool, str]:
    comments, err = _fetch_issue_comments(issue_number, repo_root)
    if err:
        return False, err
    now = float(now_t if now_t is not None else time.time())
    return any(_issue_comment_is_active_claim(c, now) for c in comments), ""


def _host_identity() -> dict:
    """Hostname, PID, git-rev block that identifies which machine/process owns
    a claim. Bare-best-effort: any field may be empty if probing fails."""
    import socket
    import os as _os
    git_rev = ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(_repo_root()),
             "rev-parse", "--short", "HEAD"],
            text=True, capture_output=True, timeout=5, check=False,
        )
        if proc.returncode == 0:
            git_rev = (proc.stdout or "").strip()[:12]
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    return {
        "hostname": (hostname or "")[:60],
        "pid": _os.getpid(),
        "git_rev": git_rev,
    }


def _host_branch_slug() -> str:
    """Short, stable hostname-derived slug for branch names so two hosts
    racing on the same issue do not collide on `git push -u origin <branch>`.

    Six hex chars from sha1(hostname) — visible enough for the human reviewer
    to tell two PRs apart, short enough not to bloat the branch name."""
    import hashlib
    import socket
    try:
        name = socket.gethostname() or "unknown"
    except OSError:
        name = "unknown"
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]


def _roadmap_issue_claim_body(
    issue: dict,
    now_t: Optional[float] = None,
) -> str:
    num = int(issue["number"])
    title = str(issue.get("title") or f"issue {num}")
    ts = datetime.fromtimestamp(
        float(now_t if now_t is not None else time.time()),
        timezone.utc,
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    ttl_h = int(ROADMAP_ISSUE_CLAIM_TTL_S // 3600)
    # Public tracker — carry only the opaque per-host token, never the raw
    # hostname/PID/git-rev. The token is enough to tell two racing hosts apart
    # for human triage; the full identity is kept in the local event log (#63).
    host_token = _host_branch_slug()
    return (
        f"{ROADMAP_ISSUE_CLAIM_MARKER}\n"
        "Evolve applier is starting work on this issue.\n\n"
        f"- Issue: #{num} {title}\n"
        "- Agent role: evolve_applier\n"
        f"- Host token: {host_token}\n"
        f"- Started: {ts}\n"
        f"- Claim TTL: {ttl_h}h\n\n"
        "Other agents should not start parallel implementation while this "
        "claim is active. If no PR or status update appears after the TTL, "
        "treat the claim as stale."
    )


def _record_claim_host_identity(
    issue_number: int,
    comment_url: str,
) -> None:
    """Persist the full host identity (hostname/PID/git-rev) behind a posted
    claim to the LOCAL event log only — it never egresses to the public
    tracker, which gets the opaque `_host_branch_slug()` token. Best-effort
    debugging aid for which machine owns a claim (#63)."""
    ident = _host_identity()
    summary = (
        f"host={ident.get('hostname') or '?'} pid={ident.get('pid')} "
        f"git_rev={ident.get('git_rev') or '?'} slug={_host_branch_slug()} "
        f"comment={comment_url or '?'}"
    )[:300]
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (identity._session_id or "", ROADMAP_ISSUE_CLAIM_HOST_KIND,
             str(int(issue_number)), summary, int(time.time())),
        )
        conn.commit()
    except (sqlite3.OperationalError, sqlite3.ProgrammingError,
            ValueError, TypeError):
        pass


def _comment_issue_claim(
    issue: dict,
    repo_root: Optional[Path] = None,
) -> tuple[str, str]:
    """Post the GitHub issue claim comment. Returns (comment_url, error).
    Empty error means success; comment_url is the URL of the posted comment
    used by race-detection and spawn-failure retraction."""
    repo = str(repo_root or _repo_root())
    num = int(issue["number"])
    cmd = [
        "gh", "issue", "comment", str(num),
        "--body", _roadmap_issue_claim_body(issue),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return "", "gh_not_found"
    except subprocess.TimeoutExpired:
        return "", "gh_issue_comment_timeout"
    except OSError as e:
        return "", f"gh_issue_comment_error: {e}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        msg = err[-1] if err else f"exit={proc.returncode}"
        return "", f"gh_issue_comment_failed: {msg[:180]}"
    url_lines = [
        ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()
    ]
    url = url_lines[0] if url_lines else ""
    # Keep the full host identity locally (the public comment is redacted).
    _record_claim_host_identity(num, url)
    return url, ""


_COMMENT_URL_ID_RE = re.compile(r"issuecomment[-_](\d+)")


def _comment_url_to_id(url: str) -> str:
    """Extract the numeric comment id from a GitHub issue-comment URL."""
    m = _COMMENT_URL_ID_RE.search(url or "")
    return m.group(1) if m else ""


def _delete_issue_comment(
    comment_url: str,
    repo_root: Optional[Path] = None,
) -> str:
    """Delete an issue comment by URL via the GitHub REST API. Empty string
    means success."""
    cid = _comment_url_to_id(comment_url)
    if not cid:
        return "gh_delete_bad_url"
    repo = str(repo_root or _repo_root())
    cmd = [
        "gh", "api",
        "-X", "DELETE",
        f"repos/{{owner}}/{{repo}}/issues/comments/{cid}",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return "gh_not_found"
    except subprocess.TimeoutExpired:
        return "gh_delete_timeout"
    except OSError as e:
        return f"gh_delete_error: {e}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        msg = err[-1] if err else f"exit={proc.returncode}"
        return f"gh_delete_failed: {msg[:180]}"
    return ""


def _open_prs_for_issue(
    issue_number: int,
    repo_root: Optional[Path] = None,
) -> tuple[list[dict], str]:
    """Open PRs that close this issue (via `Closes #N`/`Fixes #N` in body or
    a GitHub-linked PR). Returns (prs, error)."""
    repo = str(repo_root or _repo_root())
    num = int(issue_number)
    cmd = [
        "gh", "pr", "list",
        "--state", "open",
        "--search", f"in:body Closes #{num}",
        "--json", "number,url,headRefName,title,author",
        "--limit", "5",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return [], "gh_not_found"
    except subprocess.TimeoutExpired:
        return [], "gh_pr_list_timeout"
    except OSError as e:
        return [], f"gh_pr_list_error: {e}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        msg = err[-1] if err else f"exit={proc.returncode}"
        return [], f"gh_pr_list_failed: {msg[:180]}"
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as e:
        return [], f"gh_pr_list_bad_json: {e}"
    if not isinstance(data, list):
        return [], "gh_pr_list_bad_shape"
    return data, ""


def _resolve_claim_race(
    issue_number: int,
    my_comment_url: str,
    repo_root: Optional[Path] = None,
) -> tuple[bool, str]:
    """Cross-host TOCTOU resolver.

    After posting our own claim, wait briefly and re-fetch comments to detect a
    parallel claim from another host. Tie-break by earliest createdAt: if ours
    is not first, retract our own claim (best-effort delete) and report the loss
    so the queue advances.

    Returns (won, error). won=True → we own the issue and can spawn.
    won=False → another host raced ahead; our claim was retracted (or attempted
    to be retracted) so they are unblocked. error non-empty → could not decide
    the race; caller should treat that as a transient failure.
    """
    if not my_comment_url:
        return False, "missing_comment_url"
    wait_s = float(ROADMAP_CLAIM_RACE_WINDOW_S)
    if wait_s > 0:
        try:
            time.sleep(wait_s)
        except Exception:
            pass
    comments, err = _fetch_issue_comments(issue_number, repo_root)
    if err:
        return False, err
    now_t = time.time()
    active = [c for c in comments if _issue_comment_is_active_claim(c, now_t)]
    if len(active) <= 1:
        return True, ""
    my_id = _comment_url_to_id(my_comment_url)

    def cid(c: dict) -> str:
        return _comment_url_to_id(str(c.get("url") or ""))

    def ts(c: dict) -> float:
        v = _parse_gh_timestamp(c.get("createdAt"))
        return v if v is not None else 0.0

    mine_present = my_id and any(cid(c) == my_id for c in active)
    if not mine_present:
        # Our claim is no longer visible. Treat as already retracted; abort.
        return False, ""
    earliest = min(active, key=ts)
    if cid(earliest) == my_id:
        return True, ""
    # We lost — best-effort delete our own claim so the winner is unblocked.
    _delete_issue_comment(my_comment_url, repo_root)
    return False, ""


def _roadmap_issue_applied(conn: sqlite3.Connection, issue_number: int) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM events WHERE kind=? AND target=? LIMIT 1",
            (ROADMAP_ISSUE_APPLIED_KIND, str(int(issue_number))),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


# ── Poison-issue failure backoff + dead-letter ──────────────────────────────
#
# A roadmap issue whose implementer child repeatedly aborts without opening a
# PR used to be re-selected every ~24h (once its claim TTL lapsed), burning a
# fresh bypassPermissions Opus child each pass with no escalation. The applier
# now records one `roadmap_issue_attempt` event per spawned child and gates
# re-selection on an escalating backoff; after a cap it dead-letters the issue
# (a `blocked` label + a one-time human-facing comment) and excludes it from the
# auto-drain. A successful child writes `roadmap_issue_applied`, which every
# selection path checks first — so any non-applied issue with attempts>0 has
# genuinely failed those spawns.


def _roadmap_issue_attempt_state(
    conn: sqlite3.Connection, issue_number: int
) -> tuple[int, int]:
    """Return (attempt_count, last_attempt_ts) for a roadmap issue."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(created_at), 0) AS last "
            "FROM events WHERE kind=? AND target=?",
            (ROADMAP_ISSUE_ATTEMPT_KIND, str(int(issue_number))),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0, 0
    if not row:
        return 0, 0
    return int(row["c"] or 0), int(row["last"] or 0)


def _roadmap_issue_backoff_window_s(attempts: int) -> int:
    """Escalating cooldown after `attempts` failed spawns: base * 2^(n-1),
    capped at ROADMAP_ISSUE_BACKOFF_CAP_S. 0 when no attempts yet."""
    if attempts <= 0:
        return 0
    window = float(ROADMAP_ISSUE_BACKOFF_BASE_S) * (2 ** (attempts - 1))
    return int(min(window, float(ROADMAP_ISSUE_BACKOFF_CAP_S)))


def _roadmap_issue_dead_lettered(
    conn: sqlite3.Connection, issue_number: int
) -> bool:
    """True once an issue has been flagged dead-letter (its marker exists)."""
    try:
        row = conn.execute(
            "SELECT 1 FROM events WHERE kind=? AND target=? LIMIT 1",
            (ROADMAP_ISSUE_DEAD_LETTER_KIND, str(int(issue_number))),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _classify_roadmap_issue(
    conn: sqlite3.Connection, issue_number: int, now_t: float
) -> tuple[str, int, int]:
    """Map a (non-applied) issue to ('ready'|'backoff'|'dead_letter',
    attempts, last_ts).

    'dead_letter' once attempts reach ROADMAP_ISSUE_MAX_ATTEMPTS (cap>0);
    'backoff' while the escalating cooldown since the last attempt has not
    elapsed; else 'ready'."""
    attempts, last_ts = _roadmap_issue_attempt_state(conn, issue_number)
    cap = int(ROADMAP_ISSUE_MAX_ATTEMPTS)
    if cap > 0 and attempts >= cap:
        return "dead_letter", attempts, last_ts
    if attempts > 0:
        window = _roadmap_issue_backoff_window_s(attempts)
        if float(now_t) < last_ts + window:
            return "backoff", attempts, last_ts
    return "ready", attempts, last_ts


def _record_roadmap_issue_attempt(
    conn: sqlite3.Connection, issue_number: int, summary: str = ""
) -> None:
    """Append one attempt row for a roadmap issue (best-effort)."""
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (identity._session_id or "", ROADMAP_ISSUE_ATTEMPT_KIND,
             str(int(issue_number)), (summary or "")[:300], int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("evolve_applier: failed to record attempt", exc_info=True)


def _apply_blocked_label(
    issue_number: int, repo_root: Optional[Path] = None
) -> str:
    """Add the `blocked` label to a dead-lettered issue. Empty string =
    success; best-effort (a label failure never blocks the marker write)."""
    repo = str(repo_root or _repo_root())
    cmd = [
        "gh", "issue", "edit", str(int(issue_number)),
        "--add-label", ROADMAP_ISSUE_BLOCKED_LABEL,
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=repo, text=True, capture_output=True, timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return "gh_not_found"
    except subprocess.TimeoutExpired:
        return "gh_issue_edit_timeout"
    except OSError as e:
        return f"gh_issue_edit_error: {e}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        msg = err[-1] if err else f"exit={proc.returncode}"
        return f"gh_issue_edit_failed: {msg[:180]}"
    return ""


def _roadmap_issue_dead_letter_body(issue: dict, attempts: int) -> str:
    num = int(issue["number"])
    title = str(issue.get("title") or f"issue {num}")
    return (
        "Evolve applier: dead-lettered after repeated failed attempts.\n\n"
        f"- Issue: #{num} {title}\n"
        f"- Attempts: {attempts} implementer child(ren) spawned, no PR opened\n"
        f"- Action: applied the `{ROADMAP_ISSUE_BLOCKED_LABEL}` label and "
        "stopped auto-attempting this issue.\n\n"
        "This issue is excluded from the automatic drain to stop burning a "
        "bypassPermissions implementer child every cycle with no progress. A "
        f"human should unblock it (remove the `{ROADMAP_ISSUE_BLOCKED_LABEL}` "
        "label, then split/clarify the issue) before it is retried. A manual "
        f"`evolve_apply_roadmap_issue(issue_number={num})` still force-retries "
        "it regardless of this dead-letter state."
    )


def _comment_dead_letter(
    issue: dict, attempts: int, repo_root: Optional[Path] = None
) -> tuple[str, str]:
    """Post the one-time dead-letter summary comment. Returns (url, error)."""
    repo = str(repo_root or _repo_root())
    num = int(issue["number"])
    cmd = [
        "gh", "issue", "comment", str(num),
        "--body", _roadmap_issue_dead_letter_body(issue, attempts),
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=repo, text=True, capture_output=True, timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return "", "gh_not_found"
    except subprocess.TimeoutExpired:
        return "", "gh_issue_comment_timeout"
    except OSError as e:
        return "", f"gh_issue_comment_error: {e}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        msg = err[-1] if err else f"exit={proc.returncode}"
        return "", f"gh_issue_comment_failed: {msg[:180]}"
    url_lines = [
        ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()
    ]
    return (url_lines[0] if url_lines else ""), ""


def _dead_letter_issue(
    conn: sqlite3.Connection,
    issue: dict,
    attempts: int,
    repo_root: Optional[Path] = None,
) -> str:
    """Flag a poison issue once: apply the `blocked` label, post a one-time
    summary comment, and write the dead-letter marker. The marker is
    authoritative for exclusion; the label/comment are best-effort signals."""
    num = int(issue["number"])
    if _roadmap_issue_dead_lettered(conn, num):
        return f"already_dead_letter=#{num}"
    label_err = _apply_blocked_label(num, repo_root)
    _, comment_err = _comment_dead_letter(issue, attempts, repo_root)
    note = f"attempts={attempts}"
    if label_err:
        note += f" label_err={label_err[:80]}"
    if comment_err:
        note += f" comment_err={comment_err[:80]}"
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (identity._session_id or "", ROADMAP_ISSUE_DEAD_LETTER_KIND,
             str(num), note[:300], int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("evolve_applier: failed to record dead-letter",
                     exc_info=True)
    return f"dead_letter=#{num} {note}"


def roadmap_attempt_ledger(conn: sqlite3.Connection) -> list[dict]:
    """Per-issue attempt summary for non-applied roadmap issues, newest
    activity first. Pure read of the event ledger (no `gh`): each entry is
    {number, attempts, last_ts, state, backoff_left_s} where state is
    'dead_letter' | 'backoff' | 'ready'. Applied issues are omitted — they are
    done, not stuck. Drives the status view + dashboard counters."""
    try:
        rows = conn.execute(
            "SELECT target, COUNT(*) AS attempts, "
            "COALESCE(MAX(created_at), 0) AS last "
            "FROM events WHERE kind=? GROUP BY target "
            "ORDER BY last DESC",
            (ROADMAP_ISSUE_ATTEMPT_KIND,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    now_t = time.time()
    out: list[dict] = []
    for r in rows:
        try:
            num = int(r["target"])
        except (TypeError, ValueError):
            continue
        if _roadmap_issue_applied(conn, num):
            continue
        state, attempts, last_ts = _classify_roadmap_issue(conn, num, now_t)
        backoff_left = 0
        if state == "backoff":
            backoff_left = max(
                0,
                int(last_ts + _roadmap_issue_backoff_window_s(attempts) - now_t),
            )
        out.append({
            "number": num,
            "attempts": attempts,
            "last_ts": last_ts,
            "state": state,
            "backoff_left_s": backoff_left,
        })
    return out


def _open_roadmap_issues(
    conn: sqlite3.Connection,
    repo_root: Optional[Path] = None,
    *,
    skip_claimed: bool = True,
    enforce_author_trust: bool = True,
    skip_backoff: bool = True,
    flag_dead_letter: bool = False,
) -> tuple[list[dict], str]:
    """Open GitHub issues not already handed off by evolve_applier.

    The user treats all open issues as roadmap backlog; issues with the
    explicit `roadmap` label are prioritized first, then FIFO by number. Active
    issue-claim comments are skipped so multiple appliers do not start the same
    item in parallel. The upstream issue fetch is paginated oldest-first before
    this sort, so FIFO applies across the whole visible backlog rather than a
    newest-first GitHub CLI window.

    When `enforce_author_trust` is set (the autonomous-drain default), issues
    whose author is not trusted (`_issue_author_trusted`) are skipped — this
    repo is public, so an untrusted issue body must not reach the permission-
    bypassing implementer child without explicit human promotion (#63). Exact-
    issue invocation passes `enforce_author_trust=False`: naming the number is
    itself the human promotion.
    `skip_backoff` (default True) additionally excludes issues in failure
    backoff and dead-lettered poison issues from auto-selection; pass False for
    a manual exact-issue override that should ignore the cooldown/cap. When
    `flag_dead_letter` is set, a dead-lettered issue is flagged once (a
    `blocked` label + one summary comment) as it is excluded — only the real
    drain paths pass this so the read-only status view never writes to GitHub.
    """
    issues, err = _fetch_open_issues(repo_root)
    if err:
        return [], err
    out: list[dict] = []
    claim_errors: list[str] = []
    untrusted: list[int] = []
    now_t = time.time()
    for issue in issues:
        try:
            num = int(issue.get("number"))
        except (TypeError, ValueError):
            continue
        if _roadmap_issue_applied(conn, num):
            continue
        if enforce_author_trust and not _issue_author_trusted(issue):
            untrusted.append(num)
            continue
        if skip_backoff:
            state, attempts, _ = _classify_roadmap_issue(conn, num, now_t)
            if state == "dead_letter":
                if flag_dead_letter:
                    _dead_letter_issue(conn, issue, attempts, repo_root)
                continue
            if state == "backoff":
                continue
        if skip_claimed:
            claimed, claim_err = _issue_has_active_claim(
                num, repo_root, now_t
            )
            if claim_err:
                claim_errors.append(f"#{num}: {claim_err}")
                continue
            if claimed:
                continue
        out.append(issue)
    out.sort(
        key=lambda issue: (
            0 if "roadmap" in _issue_labels(issue) else 1,
            int(issue.get("number") or 0),
        )
    )
    if untrusted:
        logger.info(
            "evolve_applier: skipped %d untrusted-author issue(s) on autonomous "
            "pickup (no trusted authorAssociation/label): %s",
            len(untrusted),
            ", ".join(f"#{n}" for n in untrusted[:20]),
        )
    if not out and claim_errors:
        return [], "roadmap_issue_claim_check_failed: " + "; ".join(
            claim_errors
        )[:240]
    return out, ""


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


def build_roadmap_issue_apply_prompt(
    issue: dict,
    repo_root: Optional[Path] = None,
) -> str:
    repo = str(repo_root or _repo_root())
    number = int(issue["number"])
    title = str(issue.get("title") or f"issue {number}")
    labels = ", ".join(_issue_labels(issue)) or "(none)"
    return ROADMAP_ISSUE_APPLY_PROMPT.format(
        issue_number=number,
        issue_title=title,
        issue_url=str(issue.get("url") or ""),
        issue_labels=labels,
        issue_body=str(issue.get("body") or "").strip() or "(no body)",
        claim_marker=ROADMAP_ISSUE_CLAIM_MARKER,
        repo=repo,
        branch=roadmap_issue_branch_name(number, title),
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


def mark_roadmap_issue_applied(
    conn: sqlite3.Connection,
    issue_number: int,
    pr_url: str,
) -> str:
    """Record that an evolve_applier child opened a PR for a roadmap issue."""
    num = int(issue_number)
    if _roadmap_issue_applied(conn, num):
        return f"ok issue=#{num} already_applied=1"
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            identity._session_id or "",
            ROADMAP_ISSUE_APPLIED_KIND,
            str(num),
            (pr_url or "")[:300],
            int(time.time()),
        ),
    )
    conn.commit()
    return f"ok issue=#{num} applied=1 pr={pr_url}"


def _start_roadmap_issue_child(
    conn: sqlite3.Connection, issue: dict, repo_root: Path
) -> tuple[bool, str]:
    """Try to claim and spawn one roadmap issue.

    Multi-host safe. Order of checks:
      1. Is there already an active claim comment? (cheap, single gh call)
      2. Is there already an open PR closing this issue? (cross-host duplicate
         guard; also handles the case where a previous applier crashed after
         opening the PR but before marking it applied)
      3. Post our claim, capture comment URL.
      4. TOCTOU race: re-fetch claims, retract own if we lost.
      5. Spawn the child.
      6. On spawn failure, retract our claim so the next pass can retry
         immediately instead of waiting for the 24h TTL.

    Returns (started, status). Claim/PR/race failures are issue-local and let
    automatic queue drains advance to the next issue. Spawn errors retract the
    just-posted claim before returning.
    """
    num = int(issue["number"])
    claimed, claim_err = _issue_has_active_claim(num, repo_root)
    if claim_err:
        return False, f"ERR roadmap_issue_claim_check_failed=#{num}: {claim_err}"
    if claimed:
        return False, f"ERR roadmap_issue_claimed={num}"

    open_prs, pr_err = _open_prs_for_issue(num, repo_root)
    if pr_err:
        return False, f"ERR roadmap_issue_pr_check_failed=#{num}: {pr_err}"
    if open_prs:
        urls = ", ".join(
            str(p.get("url") or "") for p in open_prs[:3] if p.get("url")
        )
        return False, f"ERR roadmap_issue_open_pr=#{num}: {urls[:160]}"

    comment_url, claim_err = _comment_issue_claim(issue, repo_root)
    if claim_err:
        return False, f"ERR roadmap_issue_claim_failed=#{num}: {claim_err}"

    won, race_err = _resolve_claim_race(num, comment_url, repo_root)
    if race_err:
        # Couldn't decide the race (e.g. transient gh failure on re-fetch).
        # Best-effort retract our own claim so we don't block other hosts
        # for 24h on a single transient blip.
        _delete_issue_comment(comment_url, repo_root)
        return False, (
            f"ERR roadmap_issue_race_check_failed=#{num}: {race_err}"
        )
    if not won:
        # Another host got there first. Our claim was retracted by
        # _resolve_claim_race. Advance to the next candidate.
        return False, f"ERR roadmap_issue_lost_race=#{num}"

    prompt = build_roadmap_issue_apply_prompt(issue, repo_root)

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
                "mcp__thread-keeper__evolve_mark_roadmap_issue_applied,"
                "mcp__thread-keeper__broadcast"
            ),
        )
    except Exception as e:  # noqa: BLE001 — never crash the daemon/tool
        # Retract the claim we just posted so the next pass can retry the
        # issue immediately (no 24h-TTL hold on a transient spawn failure).
        _delete_issue_comment(comment_url, repo_root)
        return False, f"spawn_error issue=#{num}: {e}"
    # Record the spawn as an attempt: the failure ledger that drives backoff +
    # dead-letter. A child that completes the PR writes roadmap_issue_applied
    # (checked first everywhere), so this only accrues on issues that fail.
    _record_roadmap_issue_attempt(
        conn, num,
        "spawned branch="
        f"{roadmap_issue_branch_name(num, str(issue.get('title') or ''))}",
    )
    return True, f"spawned roadmap_issue=#{num} {str(result)[:140]}"


def _roadmap_dispatch_can_try_next(status: str) -> bool:
    """Whether an automatic issue drain should advance after this failure."""
    return status.startswith((
        "ERR roadmap_issue_claim_check_failed",
        "ERR roadmap_issue_claimed=",
        "ERR roadmap_issue_claim_failed",
        "ERR roadmap_issue_pr_check_failed",
        "ERR roadmap_issue_open_pr=",
        "ERR roadmap_issue_race_check_failed",
        "ERR roadmap_issue_lost_race=",
    ))


def apply_roadmap_issue(issue_number: int = 0) -> str:
    """Spawn an evolve_applier child to implement one open GitHub issue.

    With issue_number=0, this is queue mode: try candidates in roadmap/FIFO
    order and advance past issue-local dispatch failures. With a specific
    issue_number, this is exact mode and returns that issue's failure directly.

    Queue mode enforces the author-trust gate (#63); exact mode bypasses it —
    naming an issue number is itself the explicit human promotion the gate
    requires for an untrusted author.
    """
    with _apply_spawn_lock() as locked:
        if not locked:
            return "applier_running n=1 (single-flight lock)"

        conn = get_db()
        repo_root, repo_err = _ensure_repo_ready()
        if repo_err:
            return repo_err
        exact = bool(issue_number)
        # Queue mode honours the backoff/dead-letter gate and flags poison
        # issues; exact mode is a deliberate human override that ignores it.
        issues, err = _open_roadmap_issues(
            conn, repo_root,
            skip_claimed=not exact,
            enforce_author_trust=not exact,
            skip_backoff=not exact,
            flag_dead_letter=not exact,
        )
        if err:
            return f"ERR roadmap_issue_fetch_failed: {err}"
        if exact:
            if _roadmap_issue_applied(conn, int(issue_number)):
                return f"ERR roadmap_issue_already_applied={int(issue_number)}"
            issue = next(
                (i for i in issues if int(i.get("number") or 0) == int(issue_number)),
                None,
            )
            if issue is None:
                return f"ERR roadmap_issue_not_open={int(issue_number)}"
            candidates = [issue]
        else:
            if not issues:
                return "no_roadmap_issue"
            candidates = issues

        running = _running_applier_children(conn)
        if running:
            return f"applier_running n={len(running)} (single-flight)"

        failures: list[str] = []
        for issue in candidates:
            started, status = _start_roadmap_issue_child(
                conn, issue, repo_root
            )
            if started:
                if failures:
                    return f"{status} after_skipping={len(failures)}"
                return status
            failures.append(status)
            if exact or not _roadmap_dispatch_can_try_next(status):
                return status

        return "no_roadmap_issue_startable " + " | ".join(failures)[:240]


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
        # Curator apply is memory-only — no git tree required. Use the resolved
        # repo root when it exists, else fall back to the DB dir so the child's
        # cwd is always valid even before a managed checkout is cloned.
        repo_root = _repo_root()
        if not repo_root.exists():
            repo_root = DB_PATH.parent
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
                    "mcp__thread-keeper__list_concepts,"
                    "mcp__thread-keeper__expand_concept,"
                    "mcp__thread-keeper__concept_manage,"
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
        repo_root, repo_err = _ensure_repo_ready()
        if repo_err:
            return repo_err
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


def _pass_due(conn: sqlite3.Connection, now_t: int) -> bool:
    last = _last_apply_ts(conn)
    return last <= 0 or now_t >= last + int(EVOLVE_APPLY_INTERVAL_S)


def run_evolve_apply_pass(force: bool = False) -> str:
    """One apply pass: pick one open roadmap issue first, then fall back to
    Curator reports and finally promoted+unapplied evolve suggestions.

    Status strings:
      'disabled'                  — knob off and not forced
      'not_due'                   — automatic apply pass checked recently
      'no_apply_work'             — no Curator report or promoted suggestion
      'spawned roadmap_issue=<id>' — launched a GitHub issue implementer child
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
    if not force and not _pass_due(conn, now_t):
        return "not_due"

    # Provision the checkout before the gh-dependent roadmap peek so the managed
    # clone exists on PyPI installs. A repo error is non-fatal here: the Curator
    # fallback below is memory-only and needs no checkout.
    _, repo_err = _ensure_repo_ready()
    # flag_dead_letter=True so a poison issue is flagged (label + one comment)
    # and dropped even on a pass where it is the only open issue left.
    issues, issue_err = (
        ([], repo_err) if repo_err
        else _open_roadmap_issues(conn, flag_dead_letter=True)
    )
    if issues:
        out = apply_roadmap_issue()
        if out.startswith("spawned roadmap_issue=") or out.startswith(
            "applier_running"
        ):
            _record_apply_pass(conn, now_t, f"roadmap_issue {out}")
            return out
        issue_err = out

    reports = _pending_curator_reports(conn)
    if reports:
        report = reports[0]
        out = apply_curator_report(str(report))
        _record_apply_pass(conn, now_t, f"curator_report={report.name} {out}")
        return out

    pending = _promoted_unapplied(conn)
    if not pending:
        if issue_err:
            out = f"roadmap_issue_fetch_error: {issue_err}"
            _record_apply_pass(conn, now_t, out)
            return out
        if not force and not _pass_due(conn, now_t):
            return "not_due"
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
        daemon_sleep(EVOLVE_APPLY_INTERVAL_S)


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
