"""Dialectic miner — mechanical capture of user replies into the
dialectic_observations buffer. No LLM, no spawn: deterministic and lossless.

For each user-role dialog_message since the last pass it stores the verbatim
quote plus the most-recent preceding assistant turn as context. The
dialectic_validator child later turns this buffer into claims. Session
filtering mirrors extract_recent so only the REAL user's turns are captured
(internal-prompt sessions + spawned-child sessions are excluded)."""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time

from .config import DIALECTIC_MINE_INTERVAL_S
from .db import get_db
from .helpers import daemon_sleep, resolve_ingest_watermark
from . import identity
from .identity import _ensure_session, _emit

logger = logging.getLogger(__name__)

_started = False
_CONTEXT_MAX = 600
_NOISE_CONTENT_PREFIXES: tuple[str, ...] = (
    "# AGENTS.md instructions",
    "# Context from my IDE setup:",
    "<command-name>",
    "<command-message>",
    "<environment_context>",
    "<goal_context>",
    "<ide_opened_file>",
    "<ide_selection>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<subagent_notification>",
    "<system-reminder>",
    "<task-notification>",
    "<turn_aborted>",
    "Base directory for this skill:",
    "Context: This summary will be shown in a list",
    "Reply with exactly",
    "This session is being continued from a previous conversation",
    "You were spawned in the background by parent conversation",
    "Your task is to create a detailed summary of the conversation so far",
    "[Request interrupted by user",
    "[SUGGESTION MODE:",
    "[tool_result]",
    "[tool_call]",
    "[tool_use]",
)
_NOISE_CONTENT_MARKERS: tuple[str, ...] = (
    "<!-- THREADKEEPER:BEGIN",
    "<!-- THREADKEEPER:END",
)
_ARTIFACT_CONTENT_PREFIXES: tuple[str, ...] = (
    "+- ",
    "[adb]",
    "[CDP]",
    "[SegmentPlugin]",
    "[Layout children]:",
    "Briefing audio error:",
    "DEBUG  ",
    "ERROR  ",
    "Error: Agent CLI ",
    "Google: ",
    "INFO  ",
    "Path to P8 file:",
    "Revenue Cat API:",
    "Run command:",
    "LOG  ",
    "VM",
    "WARN  ",
    "iOS Bundled ",
    "document.cookie.match(",
    "Your tool call was malformed",
    "-----BEGIN ",
    "✓ ",
    "✖ ",
    "⏺ ",
)
_SENSITIVE_CONTENT_MARKERS: tuple[str, ...] = (
    "-----BEGIN",
    "PRIVATE KEY",
    "Secret:",
    "apps.googleusercontent.com",
)
_TERMINAL_PROMPT_RE = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+ .{0,120} % ")
_ISO_LOG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\s")
_JS_ARRAY_DUMP_RE = re.compile(r"^(?:\(\d+\)\s*)?\[.+\]\s*$")
_TASK_PROMPT_MARKERS: tuple[str, ...] = (
    "working directory",
    "package manager",
    "task description",
    "read these files",
    "you have three jobs",
    "focus on:",
    "read-only review",
    "do not make any edits",
    "based on the exploration results",
    "## context",
    "## task",
    "files:",
    "validate the following",
    "reviewing code quality",
)
_LOW_VALUE_EXACT: tuple[str, ...] = (
    "ок",
    "окей",
    "да",
    "нет",
    "ну что там?",
    "что там?",
    "что не так?",
    "задеплоил?",
    "перезапустил",
    "рестартанул",
    "коммит и пуш",
    "коммит и пуш всех чейнжей",
    "коммит и пуш всех изменений",
    "деплой, коммит и пуш",
)
_LOW_VALUE_PREFIXES: tuple[str, ...] = (
    "а статус",
    "статус?",
)
_HIGH_SIGNAL_MARKERS: tuple[str, ...] = (
    "always",
    "do not",
    "don't",
    "i prefer",
    "i want",
    "must",
    "never",
    "prefer",
    "should",
    "важно",
    "всегда",
    "должен",
    "должна",
    "должно",
    "должны",
    "запомни",
    "никогда",
    "нужно",
    "надо",
    "нельзя",
    "не делай",
    "не должен",
    "не должно",
    "не используй",
    "предпочитаю",
    "хочу",
)
_MEDIUM_SIGNAL_MARKERS: tuple[str, ...] = (
    "better",
    "instead",
    "wrong",
    "лучше",
    "не так",
    "ошиб",
    "почему",
    "убери",
    "убирать",
)
_STOP_HOOK_RE = re.compile(r"^Stop hook feedback:\s*\[(.*?)\]", re.DOTALL)


def _is_agent_task_prompt(text: str) -> bool:
    """Detect role/task prompts accidentally ingested as user dialog."""
    sample = text[:1200].lower()
    if not sample.startswith("you are "):
        return False
    if any(marker in sample for marker in _TASK_PROMPT_MARKERS):
        return True
    first_line = sample.splitlines()[0]
    return any(
        marker in first_line
        for marker in (
            " expert",
            "developer",
            "reviewer",
            "reviewing",
            "curator",
            "implementing",
            "designing",
            "conducting",
            "completing",
            "extending",
            "renaming",
            "fixing",
            "validating",
            "auditing",
            "analyzing",
            "running",
            "security engineer",
            "doing",
            "isolating",
            "fact-checker",
            "lead",
            "native",
        )
    )


def _is_cli_artifact(text: str) -> bool:
    """Detect terminal/IDE/browser dumps that adapters record as user text."""
    if any(text.startswith(prefix) for prefix in _ARTIFACT_CONTENT_PREFIXES):
        return True
    if _TERMINAL_PROMPT_RE.match(text):
        return True
    if any(marker in text[:1000] for marker in _SENSITIVE_CONTENT_MARKERS):
        return True
    if _ISO_LOG_RE.match(text):
        return True
    if _JS_ARRAY_DUMP_RE.match(text[:500]):
        return True
    sample = text[:2000]
    markers = (
        "[CDP]",
        "Command failed:",
        "Error Command exited",
        "Exit code ",
        "Traceback (most recent call last)",
        "runFixture:",
        "✓ ",
        "✖ ",
    )
    return sum(sample.count(marker) for marker in markers) >= 3


def _is_noise_user_message(content: str) -> bool:
    text = (content or "").lstrip()
    if not text:
        return True
    if any(text.startswith(prefix) for prefix in _NOISE_CONTENT_PREFIXES):
        return True
    if _is_cli_artifact(text):
        return True
    if _is_agent_task_prompt(text):
        return True
    return any(marker in text for marker in _NOISE_CONTENT_MARKERS)


def _normalize_observation_quote(content: str) -> str:
    """Stable normalization for exact/repeated candidate compaction."""
    return re.sub(r"\s+", " ", (content or "").strip().lower())


def _observation_compaction_key(content: str) -> str:
    """Return the signal key used to collapse repeated observations.

    Stop-hook feedback often repeats the same human correction with different
    trailing runner diagnostics. The bracketed human feedback is the durable
    signal; the trailing test count/log fragment is incidental.
    """
    norm = _normalize_observation_quote(content)
    match = _STOP_HOOK_RE.match((content or "").strip())
    if match:
        return "stop_hook:" + _normalize_observation_quote(match.group(1))
    return norm


def _dialectic_signal_score(content: str) -> int:
    """Cheap deterministic score for whether a user turn can change the
    compact Dialectic user model.

    Scores <= 0 are terminal noise for Dialectic. Positive scores remain
    eligible; the validator consumes higher scores first.
    """
    norm = _normalize_observation_quote(content)
    if not norm:
        return 0
    if norm in _LOW_VALUE_EXACT:
        return 0
    if any(norm.startswith(prefix) for prefix in _LOW_VALUE_PREFIXES):
        return 0
    score = 1
    if norm.startswith("stop hook feedback:"):
        score += 3
    if "?" in norm:
        score -= 1
    score += 4 * sum(1 for marker in _HIGH_SIGNAL_MARKERS if marker in norm)
    score += 2 * sum(1 for marker in _MEDIUM_SIGNAL_MARKERS if marker in norm)
    if len(norm) >= 1200 and score < 6:
        score -= 2
    if re.fullmatch(r"[\W_0-9]+", norm):
        return 0
    return max(0, score)


def _is_low_value_observation(content: str) -> bool:
    return _dialectic_signal_score(content) <= 0


def _last_mine_rowid(conn: sqlite3.Connection) -> int:
    """Ingest-order rowid high-water mark for the miner (issue #69).

    The watermark in the latest `events.kind='dialectic_mine_pass'.target` is
    a dialog_messages rowid (ingest order), not a transcript timestamp, so a
    late/out-of-order ingested user turn can't fall below it. A pre-#69
    watermark held a created_at timestamp; it is translated to the matching
    rowid once. Returns 0 when no prior pass exists."""
    try:
        row = conn.execute(
            "SELECT target FROM events WHERE kind='dialectic_mine_pass' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row or not row["target"]:
        return 0
    try:
        stored = int(row["target"])
    except (ValueError, TypeError):
        return 0
    return resolve_ingest_watermark(conn, stored)


def _record_pass(conn: sqlite3.Connection, ts: int, outcome: str) -> None:
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, 'dialectic_mine_pass', ?, ?, ?)",
            (identity._session_id or "", str(ts), outcome[:300], int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("dialectic_miner: record_pass failed", exc_info=True)


def _preceding_context(conn: sqlite3.Connection, session_id: str,
                       before_ts: int) -> str:
    """Most recent assistant turn in this session before before_ts."""
    row = conn.execute(
        "SELECT content FROM dialog_messages WHERE session_id=? "
        "AND role='assistant' AND created_at <= ? "
        "ORDER BY created_at DESC LIMIT 1",
        (session_id, before_ts),
    ).fetchone()
    if not row or not row["content"]:
        return ""
    return row["content"][:_CONTEXT_MAX]


def run_mine_pass(force: bool = False) -> str:
    """Capture new user replies since the cursor. Returns
    'ok captured=N skipped=M' / 'no_user_dialog' / 'disabled'."""
    if DIALECTIC_MINE_INTERVAL_S <= 0 and not force:
        return "disabled"
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    cursor = _last_mine_rowid(conn)

    from .harvest import harvest_exclusion_cte

    exclusion_cte, exclusion_params = harvest_exclusion_cte()

    rows = conn.execute(
        exclusion_cte +
        "SELECT rowid, uuid, session_id, content, created_at FROM dialog_messages "
        "WHERE role='user' AND rowid > ? "
        "AND coalesce(project, '') != 'subagents' "
        "AND content NOT LIKE '[tool_result]%' AND content NOT LIKE '[Image%' "
        "AND length(content) >= 1 "
        "AND session_id NOT IN ("
        "  SELECT session_id FROM harvest_excluded_sessions"
        ") "
        "ORDER BY rowid ASC",
        (*exclusion_params, cursor),
    ).fetchall()

    if not rows:
        # Nothing new above the ingest-order cursor — keep it where it is.
        # (Pre-#69 this recorded `now`, a transcript timestamp, which pushed
        # the created_at cursor into the future and dropped late arrivals.)
        _record_pass(conn, cursor, "no_user_dialog")
        return "no_user_dialog"

    captured = skipped = 0
    seen_keys = {
        _observation_compaction_key(r["user_quote"] or "")
        for r in conn.execute(
            "SELECT user_quote FROM dialectic_observations "
            "WHERE status='pending'"
        ).fetchall()
    }
    max_rowid = cursor
    for r in rows:
        max_rowid = max(max_rowid, r["rowid"])
        if _is_noise_user_message(r["content"] or ""):
            skipped += 1
            continue
        if _is_low_value_observation(r["content"] or ""):
            skipped += 1
            continue
        key = _observation_compaction_key(r["content"] or "")
        if key in seen_keys:
            skipped += 1
            continue
        ctx = _preceding_context(conn, r["session_id"] or "", r["created_at"])
        cur = conn.execute(
            "INSERT OR IGNORE INTO dialectic_observations "
            "(dialog_uuid, user_quote, context, source_cid, status, created_at) "
            "VALUES (?,?,?,?, 'pending', ?)",
            (r["uuid"], r["content"], ctx, r["session_id"], now),
        )
        if cur.rowcount:
            captured += 1
            seen_keys.add(key)
        else:
            skipped += 1
    _emit(conn, "dialectic_mine_capture", summary=f"captured={captured}")
    conn.commit()
    _record_pass(conn, max_rowid, f"ok captured={captured} skipped={skipped}")
    return f"ok captured={captured} skipped={skipped}"


def _serve_loop() -> None:
    while True:
        try:
            run_mine_pass()
        except Exception:
            logger.debug("dialectic_miner tick failed", exc_info=True)
        daemon_sleep(DIALECTIC_MINE_INTERVAL_S)


def start_dialectic_miner_daemon() -> None:
    """Idempotent. Mechanical capture needs no embeddings, so it is gated only
    by BACKGROUND_DAEMONS_ALLOWED (not SEMANTIC_AVAILABLE)."""
    global _started
    if _started:
        return
    if DIALECTIC_MINE_INTERVAL_S <= 0:
        return
    from .config import BACKGROUND_DAEMONS_ALLOWED
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    t = threading.Thread(target=_serve_loop, name="dialectic_miner", daemon=True)
    t.start()
    _started = True
