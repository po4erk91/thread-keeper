"""Child-session spawning and task management.

Provides the `spawn`, `tournament`, `tasks`, `task_kill`, `task_logs` MCP
tools, plus the supporting helpers (`_claude_bin`, `_resolve_spawned_cid`,
`_visible_task_status`, `_refresh_tasks`) and the `ROLE_PROMPTS` library
that defines cognitive stances a spawned child can adopt.
"""

import os
import shlex
import shutil
import subprocess
import signal as _sig
import sqlite3
import sys
import secrets
import time
import json as _json
from pathlib import Path
from typing import Optional

from .._mcp import mcp
from ..db import get_db
from ..config import TASK_LOG_DIR, CLAUDE_PROJECTS_DIR, DB_PATH
from ..helpers import fmt_age, q, alive
from .. import identity  # noqa: F401  (kept for future identity.* attr access)
from ..identity import _ensure_session, _detect_self_cid, _emit
from ..ingest import _parse_ts

# Path to the exit-code recorder that wraps spawned children so their real
# return_code reaches the DB regardless of which session reaps them. Run by
# file path (not `-m`) to avoid importing the package on every spawn.
_WRAP = Path(__file__).resolve().parent.parent / "_spawn_wrap.py"


def _claude_bin() -> Optional[str]:
    """Find claude CLI. Prefer CLAUDE_CODE_EXECPATH, then PATH, then known
    install locations. Returns None if not found."""
    p = os.environ.get("CLAUDE_CODE_EXECPATH")
    if p and Path(p).exists():
        return p
    found = shutil.which("claude")
    if found:
        return found
    for cand in (
        Path.home() / ".local/bin/claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ):
        if cand.exists():
            return str(cand)
    return None


def _resolve_spawned_cid(conn: sqlite3.Connection, task_id: str,
                        cwd: str, started_at: int) -> Optional[str]:
    """Find the jsonl created by this spawned child, if it has appeared.
    Heuristic: in the project dir for `cwd`, look for jsonl files whose
    earliest message timestamp is within [started_at-2, started_at+120]."""
    # cwd starts with '/'; replacing yields '-Users-…' (single leading dash).
    # Prior code added another dash, breaking the lookup.
    slug = cwd.replace("/", "-")
    project_dir = CLAUDE_PROJECTS_DIR / slug
    if not project_dir.exists():
        return None
    # exclude any cid already linked to another task in this batch
    used = set(
        r["spawned_cid"] for r in conn.execute(
            "SELECT spawned_cid FROM tasks WHERE spawned_cid IS NOT NULL"
        ).fetchall()
    )
    candidates: list[tuple[float, str]] = []
    for p in project_dir.glob("*.jsonl"):
        # subagent jsonl files (spawned by the child via Task tool) have
        # 'agent-' prefix; they're not the main session jsonl.
        if p.stem.startswith("agent-"):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        # use mtime as a coarse filter — child writes start within seconds
        # of spawn. ctime alone is unreliable across filesystems.
        if st.st_mtime < started_at - 2 or st.st_mtime > started_at + 600:
            continue
        cid = p.stem
        if cid in used:
            continue
        # peek first non-meta line for timestamp
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    ts = obj.get("timestamp")
                    if not ts:
                        continue
                    first_ts = _parse_ts(ts)
                    if started_at - 2 <= first_ts <= started_at + 600:
                        candidates.append((abs(first_ts - started_at), cid))
                    break
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


def _visible_task_status(cwd: str, cid: Optional[str],
                         started_at: int, idle_s: int = 30) -> tuple[str, Optional[int]]:
    """For visible (pid=0) tasks: infer status from the child's jsonl mtime.
    Returns (status, ended_at_guess). status ∈ {'running','idle','no_jsonl'}.
    `idle_s` controls how long since last jsonl write counts as 'done'."""
    if not cid:
        return ("no_cid", None)
    slug = cwd.replace("/", "-")
    jp = CLAUDE_PROJECTS_DIR / slug / f"{cid}.jsonl"
    if not jp.exists():
        return ("no_jsonl", None)
    try:
        m = int(jp.stat().st_mtime)
    except OSError:
        return ("no_jsonl", None)
    now_t = int(time.time())
    if now_t - m < idle_s:
        return ("running", None)
    return ("idle", m)


def _reap_finished_tasks(conn: sqlite3.Connection) -> None:
    """Reap exited headless children, recording their exit code.

    For each tracked task (pid>0) still marked running, do a non-blocking
    waitpid. If the child has exited, persist BOTH ended_at and the real
    return_code (os.waitstatus_to_exitcode → negative for signal-kills,
    e.g. -9 for SIGKILL). If the pid isn't our child (already reaped, or
    spawned by another process) we cannot learn the code: fall back to a
    liveness check and close the row out with return_code left NULL.

    This is the single place return_code gets written. Called by
    _refresh_tasks before any task read, so tasks() reflects reality.
    """
    now_t = int(time.time())
    rows = conn.execute(
        "SELECT id, pid FROM tasks "
        "WHERE ended_at IS NULL AND pid > 0 "
        "ORDER BY started_at DESC LIMIT 50"
    ).fetchall()
    changed = False
    for t in rows:
        pid = t["pid"]
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            # Not our child (or reaped elsewhere) — exit code is lost.
            # Close the row only if the process is genuinely gone.
            if not alive(pid):
                conn.execute(
                    "UPDATE tasks SET ended_at=? "
                    "WHERE id=? AND ended_at IS NULL",
                    (now_t, t["id"]),
                )
                changed = True
            continue
        except OSError:
            continue
        if wpid == 0:
            continue  # still running
        if wpid == pid:
            code = os.waitstatus_to_exitcode(status)
            conn.execute(
                "UPDATE tasks SET ended_at=?, return_code=? "
                "WHERE id=? AND ended_at IS NULL",
                (now_t, code, t["id"]),
            )
            changed = True
    if changed:
        conn.commit()


def _refresh_tasks(conn: sqlite3.Connection) -> None:
    """Update running tasks: detect process exit (or jsonl idle for visible
    tasks), link spawned_cid where possible. Cheap; safe to call before any
    task-listing read."""
    # Reap exited headless children first — this owns ended_at + return_code
    # for every pid>0 task. The loop below then only handles visible (pid<=0)
    # idle detection and spawned_cid linking.
    _reap_finished_tasks(conn)
    now_t = int(time.time())
    rows = conn.execute(
        "SELECT id, pid, cwd, started_at, spawned_cid, ended_at FROM tasks "
        "WHERE ended_at IS NULL OR spawned_cid IS NULL "
        "ORDER BY started_at DESC LIMIT 50"
    ).fetchall()
    for t in rows:
        updates: list[tuple[str, object]] = []
        if t["ended_at"] is None:
            if not (t["pid"] and t["pid"] > 0):
                # visible task — infer from jsonl idleness
                status, end_guess = _visible_task_status(
                    t["cwd"], t["spawned_cid"], t["started_at"]
                )
                if status == "idle" and end_guess:
                    updates.append(("ended_at", end_guess))
        if t["spawned_cid"] is None:
            cid = _resolve_spawned_cid(conn, t["id"], t["cwd"], t["started_at"])
            if cid:
                updates.append(("spawned_cid", cid))
        if updates:
            sets = ", ".join(f"{k}=?" for k, _ in updates)
            params = [v for _, v in updates] + [t["id"]]
            conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", params)
    if rows:
        conn.commit()


# Role library: predefined cognitive stances a spawned child can adopt.
# Each entry = a system-prompt addendum that nudges the child toward a
# specific mode of thinking. Used by spawn(role=...) and tournament().
ROLE_PROMPTS: dict[str, str] = {
    "skeptic":
        "Stance: skeptic. Find weak points, question assumptions, hunt for "
        "where the obvious answer fails. Don't propose solutions — only "
        "puncture. Output: 3-7 bullet criticisms, ranked by severity.",
    "generator":
        "Stance: generator. Produce as many distinct angles/options as you "
        "can, even half-baked or weird. Quantity over quality. Don't "
        "self-filter or critique. Output: numbered list of 5-15 ideas.",
    "critic":
        "Stance: critic. Read what others (parent, siblings via inbox/"
        "dialog_search) have proposed and rank by correctness, simplicity, "
        "risk. Output: top-3 with reasoning + 1 'avoid this' anti-pick.",
    "archivist":
        "Stance: archivist. Search the shared memory (search/dialog_search) "
        "for past similar problems and their outcomes. Don't invent — "
        "transplant. Output: 2-5 relevant precedents with citations to "
        "thread/note ids and the lesson each carries.",
    "synthesizer":
        "Stance: synthesizer. Pull diverse positions from peers (inbox/"
        "dialog_search) and fuse them into one coherent stance — shorter "
        "and crisper than the sum. Output: a single paragraph that "
        "supersedes the inputs.",
    "explorer":
        "Stance: explorer. Apply non-obvious analogies, port the problem "
        "to another domain, try the inverse direction. Heuristic: 'what if "
        "the opposite'. Output: 2-3 reframes that change the question, not "
        "just the answer.",
    "executor":
        "Stance: executor. Take the most concrete actionable step that "
        "advances the task. No analysis paralysis. Output: the single "
        "specific next action, in imperative form, ready to perform.",
}


def _build_slim_mcp_config(
    task_id: str,
    env_overrides: Optional[dict[str, str]] = None,
) -> Optional[Path]:
    """Write a minimal MCP config containing ONLY thread-keeper, so the
    spawned child doesn't load every other MCP server (context7, figma,
    stitch, etc.). Pair with --strict-mcp-config on the CLI.

    Resolution: prefer the user's ~/.claude.json `thread-keeper` entry
    (matches their actual install). Fall back to a synthesized config
    based on the running Python interpreter and package location.

    Returns the path to the slim config file, or None if neither path
    can produce a valid entry (caller should fall back to full config).
    """
    slim_dir = TASK_LOG_DIR
    slim_dir.mkdir(parents=True, exist_ok=True)
    slim_path = slim_dir / f"slim-mcp-{task_id}.json"
    mp_entry = None
    claude_json = Path.home() / ".claude.json"
    if claude_json.exists():
        try:
            data = _json.loads(claude_json.read_text(encoding="utf-8"))
            mp_entry = (data.get("mcpServers") or {}).get("thread-keeper")
        except (OSError, _json.JSONDecodeError):
            mp_entry = None
    if not mp_entry:
        # Synthesize from current runtime — same interpreter, same package.
        pkg_root = str(Path(__file__).resolve().parent.parent.parent)
        mp_entry = {
            "type": "stdio",
            "command": sys.executable,
            "args": ["-m", "threadkeeper.server"],
            "env": {
                "PYTHONPATH": pkg_root,
                "THREADKEEPER_TZ": os.environ.get(
                    "THREADKEEPER_TZ", "UTC"
                ),
            },
        }
    else:
        mp_entry = dict(mp_entry)
    env = dict(mp_entry.get("env") or {})
    if env_overrides:
        env.update(env_overrides)
    mp_entry["env"] = env
    try:
        slim_path.write_text(
            _json.dumps({"mcpServers": {"thread-keeper": mp_entry}},
                        indent=2),
            encoding="utf-8",
        )
    except OSError:
        return None
    return slim_path


@mcp.tool()
def spawn(prompt: str, cwd: str = "", append_system: str = "",
          model: str = "", effort: str = "",
          permission_mode: str = "auto",
          extra_allowed_tools: str = "",
          capture_output: bool = True,
          visible: bool = True,
          role: str = "",
          write_origin: str = "",
          slim: bool = True) -> str:
    """Launch a NEW claude session in parallel — your primary parallelism primitive.

    REACH FOR THIS WHEN:
    - you catch yourself about to do N independent things sequentially
      (give each to its own child; collect summaries via inbox/wait)
    - a task is long-running and you don't need to babysit
      (build, ingest, scrape, deep research) — spawn(visible=False), check task_logs later
    - multiple angles benefit from triangulation
      (3 children with different role= , then vote_distill / consolidate)
    - user signals decomposition via trigger phrases — see
      threadkeeper.i18n.SPAWN_TRIGGER_PHRASE_EXAMPLES for the bilingual list
    - a thread is stale and unblocks if someone just *does* it
      (pickup_candidates → spawn child with the plan)
    - you need a fresh context window without polluting your own
      (e.g. the user's question pulls in topics that would bloat this convo)

    DEFAULT TO SPAWNING when work decomposes. Sequential is the slow path —
    every minute the parent thinks step-by-step is a minute the children
    aren't doing anything. The only reason NOT to spawn is tight
    back-and-forth coupling (you need each step's result before the next).

    Mechanics:
    - visible=True (default): real Terminal.app window, you watch child stdout
      live. Window stays open after exit until Enter. Best for observation.
    - visible=False: silent background `claude -p`, stdout/stderr redirected
      to {TASK_LOG_DIR}/{task_id}.log (when capture_output=True).
      Read via task_logs(task_id).
    - permission_mode='auto' (default) — child runs in auto-mode and can call
      thread-keeper tools without approval prompts.
    - role= — apply a cognitive stance from ROLE_PROMPTS (problem_solver,
      skeptic, summarizer, …); custom roles are supported.
    - slim=True (DEFAULT): children are hands, not heads. Child loads ONLY
      thread-keeper MCP (no context7/figma/stitch/sentry/etc), no embeddings
      (no PyTorch/transformers), defers any semantic search to the parent
      via search_via_parent. Typical light-child RSS is 400-500MB vs
      1.3-1.7GB for a full child. Parent retains all heavy state. Use this
      for any execute-this-plan task where the parent already knows what
      needs doing.
    - slim=False (rare): pass when the child genuinely needs OTHER MCP
      servers from ~/.claude.json (e.g. context7 for library docs, figma
      for design lookups). Default-deny posture — only opt out when you
      have a concrete reason.
    - Children share THIS DB — talk via broadcast/whisper/ask/inbox/wait;
      child_cid is generated up-front and exposed via env so child self-knows.

    Returns: task_id, pid (0 for visible), child_cid, parent_cid."""
    prompt = prompt.strip()
    if not prompt:
        return "ERR empty_prompt"
    cwd = cwd.strip() or os.getcwd()
    if not Path(cwd).exists():
        return f"ERR cwd_not_found={cwd}"
    bin_ = _claude_bin()
    if not bin_:
        return "ERR claude_cli_not_found (set CLAUDE_CODE_EXECPATH or install claude)"

    # Admission control: refuse if running children + this one would
    # breach SPAWN_BUDGET_MB. Estimate based on slim vs full.
    from ..spawn_budget import estimate_child_rss_kb, check_budget
    _budget_conn = get_db()
    _ensure_session(_budget_conn)
    _new_kb = estimate_child_rss_kb(slim)
    _ok, _reason = check_budget(_budget_conn, _new_kb)
    if not _ok:
        return f"ERR {_reason}"

    parent_cid = _detect_self_cid()
    # child_cid is generated below; we craft sys_extra after that so it can
    # reference the exact ids. Build without it here, append after.
    sys_extra_template = (
        "You were spawned in the background by parent conversation "
        "{parent}. Your own cid is {child} (forced via --session-id and "
        "THREADKEEPER_FORCE_CID env). You share thread-keeper DB with "
        "the parent.\n\n"
        "Channels:\n"
        "  peers() — who's active\n"
        "  broadcast(content) — message to everyone\n"
        "  whisper(parent_cid, content) — directed message\n"
        "  inbox() — read pending signals\n"
        "  wait(timeout_s, kinds='question') — block until signal arrives\n"
        "  ask(cid, question) — synchronous q/a with peer\n"
        "  respond(qid, content) — answer a specific +question entry\n\n"
        "If your task expects realtime back-and-forth with the parent, sit "
        "in `wait(120, 'question')` loops between work units; otherwise just "
        "broadcast/whisper a summary at the end.\n\n"
        "When replying to the user: paraphrase in plain language. Do NOT "
        "quote internal IDs (cids, signal #ids, thread T-codes, qids, "
        "task tk_codes) — those are tool-call internals only."
    )
    # Generate the child's conversation_id up front. Pass it via --session-id
    # so claude uses it as the jsonl stem, AND via env so the child's MCP
    # server-process resolves itself to it via THREADKEEPER_FORCE_CID
    # (no ppid-walk needed for spawned children).
    import uuid as _uuid
    child_cid = str(_uuid.uuid4())
    sys_extra = sys_extra_template.format(
        parent=parent_cid or "(unknown)",
        child=child_cid,
    )
    role_clean = role.strip().lower()
    if role_clean:
        if role_clean in ROLE_PROMPTS:
            sys_extra += f"\n\nROLE: {role_clean}\n{ROLE_PROMPTS[role_clean]}"
        else:
            sys_extra += (
                f"\n\nROLE: {role_clean}\n"
                f"(custom role — apply your own interpretation; predefined "
                f"set: {', '.join(ROLE_PROMPTS.keys())})"
            )
    if append_system:
        sys_extra += "\n\n" + append_system
    child_env = {
        **os.environ,
        "THREADKEEPER_FORCE_CID": child_cid,
        "THREADKEEPER_SPAWNED_CHILD": "1",
        "THREADKEEPER_TZ": os.environ.get("THREADKEEPER_TZ", "UTC"),
    }
    if write_origin:
        child_env["THREADKEEPER_WRITE_ORIGIN"] = write_origin
    # slim spawn → child loads NO embeddings (delegates semantic search to
    # the parent via search_via_parent). Override only if user didn't set
    # the env explicitly already (allow opt-out by setting =0 explicitly).
    if slim and "THREADKEEPER_NO_EMBEDDINGS" not in child_env:
        child_env["THREADKEEPER_NO_EMBEDDINGS"] = "1"
    mcp_env_overrides = {
        k: child_env[k]
        for k in (
            "THREADKEEPER_FORCE_CID",
            "THREADKEEPER_SPAWNED_CHILD",
            "THREADKEEPER_TZ",
            "THREADKEEPER_WRITE_ORIGIN",
            "THREADKEEPER_NO_EMBEDDINGS",
        )
        if k in child_env
    }
    # Resolve which CLI agent should run this child. Claude is the
    # historical default and the only path with full MCP-config
    # injection + session-id + append-system-prompt translation; for
    # codex/gemini/copilot we take a simpler path via the adapter's
    # spawn_argv that builds basic argv only.
    from .. import spawn_config as _sc, identity as _id
    chosen_cli = _sc.resolve_agent(role or "", _id.active_cli())
    chosen_model = model or _sc.resolve_model(chosen_cli)
    if chosen_cli != "claude":
        from ..adapters import get_adapter
        _ad = get_adapter(chosen_cli)
        if _ad is None or not _ad.supports_spawn():
            return f"ERR spawn_unsupported cli={chosen_cli}"
        # Compress system-prompt extras + main prompt into one string —
        # non-Claude CLIs have no per-invocation --append-system-prompt.
        full_prompt = (sys_extra + "\n\n---\n\n" + prompt
                       if sys_extra else prompt)
        cmd = _ad.spawn_argv(
            full_prompt,
            model=chosen_model,
            permission_mode=permission_mode,
            extra_allowed_tools=extra_allowed_tools,
        )
        if not cmd:
            return f"ERR spawn_failed cli={chosen_cli} reason=binary_not_found"
    else:
        cmd = [
            bin_, "-p", prompt,
            "--session-id", child_cid,
            "--append-system-prompt", sys_extra,
        ]
    # Everything below is Claude-flavour argv flags. Non-claude
    # adapters built their full argv via spawn_argv() above and skip
    # all of this. (When chosen_cli != "claude", `cmd` already
    # contains the full argv list ready for subprocess.Popen.)
    if chosen_cli != "claude":
        task_id = "tk_" + secrets.token_hex(3)
        slim_cfg = None  # non-claude CLIs read MCP from their global config
    else:
        if permission_mode:
            cmd += ["--permission-mode", permission_mode]
        # Default allowlist: thread-keeper tools so the child can actually
        # report back via broadcast/whisper without auto-mode classifier
        # blocking. Users extend via extra_allowed_tools.
        _claude_default_allow = [
        "mcp__thread-keeper__broadcast",
        "mcp__thread-keeper__whisper",
        "mcp__thread-keeper__inbox",
        "mcp__thread-keeper__wait",
        "mcp__thread-keeper__ask",
        "mcp__thread-keeper__respond",
        "mcp__thread-keeper__peers",
        "mcp__thread-keeper__whoami",
        "mcp__thread-keeper__note",
        "mcp__thread-keeper__open_thread",
        "mcp__thread-keeper__close_thread",
        "mcp__thread-keeper__search",
        "mcp__thread-keeper__dialog_search",
        "mcp__thread-keeper__brief",
        "mcp__thread-keeper__context",
        "mcp__thread-keeper__verbatim_user",
        "mcp__thread-keeper__register_probe",
        "mcp__thread-keeper__run_probe",
        "mcp__thread-keeper__record_attempt",
        "mcp__thread-keeper__reliability_for",
        "mcp__thread-keeper__weak_spots",
        "mcp__thread-keeper__pickup_candidates",
        "mcp__thread-keeper__claim_pickup",
        "mcp__thread-keeper__release_pickup",
        "mcp__thread-keeper__register_concept",
        "mcp__thread-keeper__list_concepts",
        "mcp__thread-keeper__expand_concept",
        "mcp__thread-keeper__distill",
        "mcp__thread-keeper__vote_distill",
        "mcp__thread-keeper__pending_distillates",
        "mcp__thread-keeper__export_distillates",
        "mcp__thread-keeper__find_invariants",
        "mcp__thread-keeper__core_set",
        "mcp__thread-keeper__core_remove",
        "mcp__thread-keeper__core_list",
        "mcp__thread-keeper__core_get",
        "mcp__thread-keeper__link",
        "mcp__thread-keeper__unlink",
        "mcp__thread-keeper__neighbors",
        "mcp__thread-keeper__tag_signal",
        "mcp__thread-keeper__task_thread",
        "mcp__thread-keeper__extract_recent",
        "mcp__thread-keeper__review_candidates",
        "mcp__thread-keeper__accept_candidate",
        "mcp__thread-keeper__reject_candidate",
        "mcp__thread-keeper__consolidate",
        "mcp__thread-keeper__mark_skill_materialized",
        "mcp__thread-keeper__skill_record",
        "mcp__thread-keeper__skill_list",
        "mcp__thread-keeper__curator_run",
            "mcp__thread-keeper__search_via_parent",
        ]
        extra_list = [t.strip() for t in extra_allowed_tools.split(",") if t.strip()]
        allow = _claude_default_allow + extra_list
        cmd += ["--allowedTools"] + allow
        if chosen_model:
            cmd += ["--model", chosen_model]
        if effort:
            cmd += ["--effort", effort]
        task_id = "tk_" + secrets.token_hex(3)
        slim_cfg = None
        # slim=True: load ONLY thread-keeper MCP server. Skips context7,
        # figma, stitch and every other MCP from ~/.claude.json —
        # typically a 4-6× RAM reduction and a 10-30s faster cold start.
        # Use for review/curation children that only need thread-keeper
        # DB access (no Bash/Edit beyond claude built-ins, no external
        # API integrations).
        if slim:
            slim_cfg = _build_slim_mcp_config(task_id, mcp_env_overrides)
            if slim_cfg is not None:
                cmd += ["--mcp-config", str(slim_cfg),
                        "--strict-mcp-config"]
    log_path: Optional[Path] = None
    TASK_LOG_DIR.mkdir(parents=True, exist_ok=True)
    if capture_output and not visible:
        log_path = TASK_LOG_DIR / f"{task_id}.log"
    proc_pid = 0
    try:
        if visible:
            # Build a self-contained .command shell script that Terminal.app
            # will execute in a fresh window. We export env, cd, exec claude,
            # then `read` so the window stays open for inspection.
            script_path = TASK_LOG_DIR / f"{task_id}.command"
            cmd_line = " \\\n    ".join(shlex.quote(a) for a in cmd)
            # After the child exits in this shell, persist its real exit code
            # to the DB (the visible/pid=0 path is invisible to the parent
            # reaper's waitpid). Best-effort; never blocks window teardown.
            wrap_record = ""
            if _WRAP.exists():
                wrap_record = (
                    shlex.quote(sys.executable) + " "
                    + shlex.quote(str(_WRAP)) + " --record "
                    + shlex.quote(str(DB_PATH)) + " "
                    + shlex.quote(task_id) + ' "$rc" 2>/dev/null || true'
                )
            env_pairs = [
                ("THREADKEEPER_FORCE_CID", child_cid),
                ("THREADKEEPER_SPAWNED_CHILD", "1"),
                ("THREADKEEPER_TZ",
                 os.environ.get("THREADKEEPER_TZ", "UTC")),
            ]
            if write_origin:
                env_pairs.append(
                    ("THREADKEEPER_WRITE_ORIGIN", write_origin)
                )
            if slim and "THREADKEEPER_NO_EMBEDDINGS" not in os.environ:
                env_pairs.append(("THREADKEEPER_NO_EMBEDDINGS", "1"))
            env_lines = "\n".join(
                f"export {k}={shlex.quote(v)}" for k, v in env_pairs
            )
            # tag the terminal window with a unique title so the closer
            # AppleScript finds exactly this tab (front-window heuristics
            # break when the user switches focus during the run).
            tag = f"thread-keeper-{task_id}"
            close_apple = (
                f'tell application "Terminal"\n'
                f'  repeat with w in windows\n'
                f'    repeat with t in tabs of w\n'
                f'      try\n'
                f'        if (name of t) contains "{tag}" then\n'
                f'          close w saving no\n'
                f'          return\n'
                f'        end if\n'
                f'      end try\n'
                f'    end repeat\n'
                f'  end repeat\n'
                f'end tell'
            )
            script = f"""#!/bin/bash
set -u
{env_lines}
cd {shlex.quote(cwd)}
printf '\\033]0;{tag}\\007'
echo '── thread-keeper spawn ────────────────'
echo "  task_id : {task_id}"
echo "  cid     : {child_cid}"
echo "  parent  : {(parent_cid or '-')}"
echo "  perm    : {permission_mode}"
echo '────────────────────────────────────────'
echo
{cmd_line}
rc=$?
{wrap_record}
echo
echo "── done (exit=$rc) — closing in 2s ──"
sleep 2
( osascript <<'OSA' >/dev/null 2>&1 &
{close_apple}
OSA
)
exit $rc
"""
            script_path.write_text(script)
            script_path.chmod(0o755)
            try:
                subprocess.Popen(
                    ["open", "-a", "Terminal", str(script_path)],
                    env=child_env,
                )
            except (FileNotFoundError, OSError) as e:
                return f"ERR open_terminal_failed={e}"
            # pid for Terminal-launched claude isn't directly trackable from
            # here; tasks() relies on spawned_cid + jsonl mtime instead.
            proc_pid = 0
        else:
            # Run the child UNDER our exit-code recorder so return_code lands
            # in the DB from inside the child's own lifecycle — the parent
            # reaper's waitpid can't see cross-session children (the reason
            # return_code was always NULL). Degrade to the bare cmd if the
            # recorder file is somehow missing.
            launch_cmd = cmd
            if _WRAP.exists():
                launch_cmd = [
                    sys.executable, str(_WRAP), str(DB_PATH), task_id,
                    "--", *cmd,
                ]
            if log_path is not None:
                log_f = log_path.open("wb")
                proc = subprocess.Popen(
                    launch_cmd,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=child_env,
                )
                log_f.close()
            else:
                proc = subprocess.Popen(
                    launch_cmd,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    env=child_env,
                )
            proc_pid = proc.pid
    except (FileNotFoundError, OSError) as e:
        return f"ERR spawn_failed={e}"
    now_t = int(time.time())
    conn = get_db()
    _ensure_session(conn)
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, spawned_cid, cwd, prompt, "
        "started_at, rss_kb, rss_updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, proc_pid, parent_cid, child_cid, cwd, prompt, now_t,
         _new_kb, now_t),
    )
    _emit(conn, "spawn", target=task_id, summary=prompt[:140])
    conn.commit()
    mode = "visible" if visible else "headless"
    log_disp = log_path or ("Terminal.app" if visible else "devnull")
    return (
        f"ok task={task_id} pid={proc_pid} child_cid={child_cid[:8]} "
        f"parent_cid={(parent_cid or '-')[:8]} "
        f"perm={permission_mode or '-'} mode={mode} log={log_disp}"
    )


@mcp.tool()
def tournament(prompt: str,
               roles: str = "skeptic,generator,critic",
               cwd: str = "",
               timeout_s: int = 240,
               visible: bool = False,
               model: str = "",
               effort: str = "") -> str:
    """Spawn N children with different roles on the same prompt, then collect
    their answers via a tagged broadcast and return a comparison.

    `roles`: comma-separated role names. Predefined: skeptic, generator,
    critic, archivist, synthesizer, explorer, executor. Custom names allowed
    (child gets generic instruction). Each role gets a distinct system
    prompt addendum encoding its mindset.

    Each child is told to broadcast its final output as exactly:
        [<tournament_id>] [<role>] <answer>
    Parent polls signals every 2s for matching prefixes until all answered
    or timeout.

    Returns: a per-role digest. Children write everything to thread-keeper
    so you can also inspect via tasks()/dialog_search() afterward.

    `visible=False` (default for tournaments — opening 5 Terminal windows is
    obnoxious). Override per-need."""
    import re
    role_list = [r.strip().lower() for r in roles.split(",") if r.strip()]
    if not role_list:
        return "ERR no_roles"
    if len(role_list) > 8:
        return f"ERR too_many_roles={len(role_list)} (max 8)"
    self_cid = _detect_self_cid()
    if not self_cid:
        return "ERR cannot_detect_self_cid"
    tid = "trn_" + secrets.token_hex(3)
    cwd = cwd.strip() or os.getcwd()

    spawned: list[dict] = []
    aug_template = (
        "Tournament {tid}, role: {role}.\n\n"
        "Task:\n{task}\n\n"
        "When you're done, broadcast EXACTLY this single line (no markdown, "
        "no quotes, replace <answer> with your final output):\n"
        "  [{tid}] [{role}] <answer>\n"
        "Keep <answer> under 600 chars. That's the only required deliverable; "
        "the tournament organizer harvests broadcasts matching that prefix."
    )
    for role in role_list:
        aug = aug_template.format(tid=tid, role=role, task=prompt)
        # call spawn() — it's a regular Python function under @mcp.tool
        result = spawn(
            prompt=aug,
            cwd=cwd,
            visible=visible,
            model=model,
            effort=effort,
            permission_mode="auto",
            role=role,
        )
        m = re.search(r"task=(\S+)\s+.*child_cid=(\S+)", result)
        if m:
            spawned.append({
                "role": role, "task_id": m.group(1),
                "cid_short": m.group(2), "spawn_result": result,
            })
        else:
            spawned.append({"role": role, "error": result})

    started_at = int(time.time())
    deadline = started_at + max(15, min(int(timeout_s), 600))
    conn = get_db()
    collected: dict[str, dict] = {}
    line_re = re.compile(
        rf"^\[{re.escape(tid)}\]\s*\[([^\]]+)\]\s*(.*)$", re.DOTALL
    )
    while len(collected) < len(role_list) and time.time() < deadline:
        rows = conn.execute(
            "SELECT id, from_cid, content, created_at FROM signals "
            "WHERE kind='broadcast' AND created_at >= ? "
            "AND content LIKE ? ORDER BY created_at",
            (started_at - 2, f"[{tid}]%"),
        ).fetchall()
        for r in rows:
            m = line_re.match(r["content"])
            if not m:
                continue
            role_found = m.group(1).strip().lower()
            ans = m.group(2).strip()
            if role_found not in collected:
                collected[role_found] = {
                    "answer": ans,
                    "from": r["from_cid"][:8],
                    "at": r["created_at"],
                }
        if len(collected) >= len(role_list):
            break
        time.sleep(2)

    elapsed = int(time.time() - started_at)
    out = [
        f"tournament={tid} got={len(collected)}/{len(role_list)} "
        f"elapsed={elapsed}s"
    ]
    for s in spawned:
        if "error" in s:
            out.append(f"\n## {s['role']} — SPAWN_FAILED\n{s['error']}")
            continue
        role = s["role"]
        if role in collected:
            d = collected[role]
            out.append(
                f"\n## {role} (from {d['from']}, "
                f"+{fmt_age(int(time.time()) - d['at'])}_ago)"
            )
            out.append(d["answer"][:1200])
        else:
            out.append(
                f"\n## {role} — TIMEOUT (no broadcast within {elapsed}s; "
                f"task {s['task_id']} may still be running, check tasks())"
            )
    return "\n".join(out)


@mcp.tool()
def tasks(include_ended: bool = True, k: int = 15) -> str:
    """List spawned tasks: id, pid, status, elapsed, spawned_cid (if linked),
    prompt prefix. Refreshes liveness and resolves spawned_cid lazily."""
    conn = get_db()
    _ensure_session(conn)
    _refresh_tasks(conn)
    where = "" if include_ended else "WHERE ended_at IS NULL"
    rows = conn.execute(
        f"SELECT * FROM tasks {where} ORDER BY started_at DESC LIMIT ?", (k,)
    ).fetchall()
    if not rows:
        return "no_tasks"
    now_t = int(time.time())
    lines = []
    for t in rows:
        is_visible = not t["pid"] or t["pid"] <= 0
        if t["ended_at"]:
            rc = t["return_code"]
            rc_disp = "" if rc is None else f" rc={rc}"
            status = f"done@{fmt_age(now_t - t['ended_at'])}_ago{rc_disp}"
        elif is_visible:
            vstatus, _end = _visible_task_status(
                t["cwd"], t["spawned_cid"], t["started_at"]
            )
            status = vstatus
        elif alive(t["pid"]):
            status = "running"
        else:
            status = "dead?"
        elapsed = fmt_age(
            (t["ended_at"] or now_t) - t["started_at"]
        )
        snip = t["prompt"][:60].replace("\n", " ")
        if len(t["prompt"]) > 60:
            snip += "…"
        cid = (t["spawned_cid"] or "-")[:8]
        pid_disp = "vis" if is_visible else str(t["pid"])
        lines.append(
            f"{t['id']} pid={pid_disp} {status} elapsed={elapsed} "
            f"cid={cid} {q(snip)}"
        )
    return "\n".join(lines)


@mcp.tool()
def task_logs(task_id: str, tail_lines: int = 80) -> str:
    """Read tail of a spawned task's captured stdout/stderr log.

    Only works for tasks spawned with `capture_output=True` (default).
    Returns the last `tail_lines` lines or 'no_log' if the task ran with
    capture_output=False or the log file is missing."""
    log_path = TASK_LOG_DIR / f"{task_id}.log"
    if not log_path.exists():
        return f"no_log path={log_path}"
    try:
        with log_path.open("rb") as f:
            data = f.read()
    except OSError as e:
        return f"ERR read_failed={e}"
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if tail_lines and len(lines) > tail_lines:
        lines = lines[-tail_lines:]
    return "\n".join(lines) if lines else "(empty)"


@mcp.tool()
def spawn_budget_status() -> str:
    """Report current spawn-budget usage: cap, used, free, plus per-running-task
    RSS. Used to decide whether another spawn() will be admitted.

    Values come from the budget daemon (refreshes every SPAWN_BUDGET_POLL_S
    seconds via `ps`). Just-spawned tasks show their initial estimate until
    the daemon catches up. Tasks with pid=0 (visible Terminal-launched
    spawns) aren't tracked from here — their RSS column stays as estimate."""
    from ..config import SPAWN_BUDGET_MB, SPAWN_BUDGET_POLL_S
    conn = get_db()
    _ensure_session(conn)
    _refresh_tasks(conn)
    rows = conn.execute(
        "SELECT id, pid, spawned_cid, prompt, rss_kb, rss_updated_at, "
        "started_at FROM tasks WHERE ended_at IS NULL "
        "ORDER BY started_at DESC LIMIT 20"
    ).fetchall()
    now_t = int(time.time())
    used_kb = sum(
        (r["rss_kb"] or 0) for r in rows
    )
    if SPAWN_BUDGET_MB <= 0:
        header = (
            f"budget=disabled used={used_kb // 1024}MB "
            f"running={len(rows)}"
        )
    else:
        free_kb = max(0, SPAWN_BUDGET_MB * 1024 - used_kb)
        header = (
            f"budget={SPAWN_BUDGET_MB}MB used={used_kb // 1024}MB "
            f"free={free_kb // 1024}MB running={len(rows)} "
            f"poll={SPAWN_BUDGET_POLL_S}s"
        )
    if not rows:
        return header
    lines = [header]
    for r in rows:
        rss_mb = (r["rss_kb"] or 0) // 1024
        age_at = r["rss_updated_at"] or r["started_at"]
        age = fmt_age(now_t - age_at)
        snip = r["prompt"][:50].replace("\n", " ")
        if len(r["prompt"]) > 50:
            snip += "…"
        cid = (r["spawned_cid"] or "-")[:8]
        pid_disp = "vis" if not r["pid"] or r["pid"] <= 0 else str(r["pid"])
        lines.append(
            f"  {r['id']} pid={pid_disp} cid={cid} rss={rss_mb}MB "
            f"age={age} {q(snip)}"
        )
    return "\n".join(lines)


@mcp.tool()
def spawn_budget_set(limit_mb: int) -> str:
    """Override the spawn-budget cap for this process (in MB). Set 0 to
    disable enforcement. Does NOT persist across restarts — set
    THREADKEEPER_SPAWN_BUDGET_MB env for persistence.

    Useful when a heavy task needs a higher temporary ceiling, or to drop
    the cap mid-session if you notice the laptop struggling."""
    if limit_mb < 0:
        return "ERR limit_mb_must_be_non_negative"
    from .. import config
    config.SPAWN_BUDGET_MB = int(limit_mb)
    if limit_mb == 0:
        return "ok: budget enforcement DISABLED (existing children unaffected)"
    return f"ok: SPAWN_BUDGET_MB now {limit_mb}MB (was via env or previous override)"


@mcp.tool()
@mcp.tool()
def spawn_status() -> str:
    """Show which CLI thread-keeper detected as its host, and which CLI
    each spawn role resolves to (after env + file overrides). Use to
    sanity-check spawn config when you want loops to fire through a
    specific agent.

    Resolution priority (highest first):
      • THREADKEEPER_SPAWN_LOOP_<ROLE>=<cli>
      • ~/.threadkeeper/spawn.toml [loops] override
      • THREADKEEPER_SPAWN_DEFAULT=<cli>
      • [default] agent in the toml
      • active CLI detected at startup
      • final fallback: claude

    Manual model pinning:
      • THREADKEEPER_SPAWN_MODEL_<CLI>=<model>
      • [models].<cli> in the toml
    """
    from .. import spawn_config as _sc, identity as _id
    from ..adapters import get_adapter
    active = _id.active_cli()
    lines = [
        f"active_cli={active or '(none detected)'}",
        "",
        "per-role resolution:",
        _sc.summary_table(active),
        "",
        "spawn capability by CLI:",
    ]
    for cli in _sc.SUPPORTED_CLIS:
        adapter = get_adapter(cli)
        if not adapter:
            lines.append(f"  {cli:<8} no adapter")
            continue
        argv = adapter.spawn_argv("test", model="")
        if argv is None:
            lines.append(f"  {cli:<8} not on PATH")
        else:
            bin_short = argv[0].split("/")[-1]
            lines.append(f"  {cli:<8} ok bin={bin_short}")
    return "\n".join(lines)


def task_kill(task_id: str, force: bool = False) -> str:
    """Stop a spawned task. SIGTERM by default; force=True sends SIGKILL.

    Headless children launch detached (``start_new_session=True``) under the
    exit-code recorder, so the tracked pid is the session/group leader and
    the real CLI child shares its process group. We signal the whole group
    (``killpg``) rather than the bare pid: ``force`` sends SIGKILL, which the
    recorder cannot forward (SIGKILL is uncatchable), so a pid-only kill
    would drop the wrapper and orphan the live ``claude`` child. Killing the
    group reaps wrapper + child together (and any same-group MCP subprocesses
    the child started). Falls back to a single-pid ``kill`` if the group send
    is refused.
    """
    conn = get_db()
    _ensure_session(conn)
    row = conn.execute(
        "SELECT pid, ended_at FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if not row:
        return f"ERR task_not_found={task_id}"
    if row["ended_at"]:
        return f"already_ended task={task_id}"
    pid = row["pid"]
    if not pid or pid <= 0:
        # Visible/Terminal tasks aren't tracked by pid (pid=0). Signalling
        # pid 0 would hit the server's OWN process group — refuse instead.
        return (f"ERR not_killable_by_pid task={task_id} "
                f"(visible/terminal task — close its window)")

    def _mark_dead() -> str:
        conn.execute(
            "UPDATE tasks SET ended_at=? WHERE id=?",
            (int(time.time()), task_id),
        )
        conn.commit()
        return f"already_dead task={task_id}"

    sig_to_send = _sig.SIGKILL if force else _sig.SIGTERM
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return _mark_dead()
    try:
        os.killpg(pgid, sig_to_send)
    except ProcessLookupError:
        return _mark_dead()
    except PermissionError:
        # Group send refused (cross-owner group); fall back to the pid.
        try:
            os.kill(pid, sig_to_send)
        except ProcessLookupError:
            return _mark_dead()
        except PermissionError:
            return f"ERR permission_denied pid={pid}"
    return f"signal={sig_to_send.name} sent task={task_id} pid={pid}"
