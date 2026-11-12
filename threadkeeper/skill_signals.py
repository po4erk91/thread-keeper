"""Skill-use signals for CLIs that have no native Skill tool call.

Claude Code records a skill run as a ``Skill`` tool_use block, which ingest
already counts. Codex, Gemini/Antigravity and Copilot open a skill by reading
its ``SKILL.md`` or by a user typing ``$name`` (or ``/name``). This module turns
those transcript events into :class:`SkillSignal` records.

Plain text matching is not enough: every Codex session lists all installed
skill paths in its injected instructions. Only tool-call arguments count as an
"open", and only real user turns count as an "invoke". Bulk reads (an audit
that opens many skills in one session) are dropped, because they say nothing
about whether a skill helped with a task.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Callable, Iterable, Iterator

SKILL_PATH_RE = re.compile(r"skills/([a-z0-9][a-z0-9._-]*)/SKILL\.md")
_DOLLAR_INVOKE_RE = re.compile(r"(?<![\w$])\$([a-z0-9][a-z0-9._-]{1,63})")
_SLASH_INVOKE_RE = re.compile(r"(?:^|\s)/([a-z0-9][a-z0-9._-]{1,63})(?=\s|$)")
BULK_SESSION_SKILL_LIMIT = 10
CODEX_TOOL_CALL_TYPES = frozenset({"function_call", "custom_tool_call", "local_shell_call"})
_INJECTED_CONTEXT_PREFIX = "<"


@dataclass(frozen=True)
class SkillSignal:
    """One observed skill use: ``open`` (tool read SKILL.md) or ``invoke``."""

    session_id: str
    created_at: int
    name: str
    signal: str


def skill_paths_in(text: str) -> list[str]:
    """Skill names whose SKILL.md path appears in tool-call arguments."""
    return list(dict.fromkeys(SKILL_PATH_RE.findall(text or "")))


def invocations_in(text: str, *, slash: bool = False) -> list[str]:
    """Skill names a user typed as ``$name`` (and ``/name`` when allowed)."""
    names = _DOLLAR_INVOKE_RE.findall(text or "")
    if slash:
        names += _SLASH_INVOKE_RE.findall(text or "")
    return list(dict.fromkeys(names))


def is_user_authored_text(text: str) -> bool:
    """False for CLI-injected context blocks (instructions, environment)."""
    return not (text or "").lstrip().startswith(_INJECTED_CONTEXT_PREFIX)


def without_bulk_reads(
    signals: Iterable[SkillSignal], limit: int = BULK_SESSION_SKILL_LIMIT,
) -> list[SkillSignal]:
    """Drop ``open`` signals of sessions that opened more than ``limit`` skills.

    Explicit user invocations stay: a person typing ``$name`` is a real use
    even inside a session that also bulk-read the store.
    """
    items = list(signals)
    opened: dict[str, set[str]] = {}
    for item in items:
        if item.signal == "open":
            opened.setdefault(item.session_id, set()).add(item.name)
    bulk = {session for session, names in opened.items() if len(names) > limit}
    return [
        item for item in items
        if not (item.signal == "open" and item.session_id in bulk)
    ]


def known_only(
    signals: Iterable[SkillSignal], is_known: Callable[[str], bool],
) -> list[SkillSignal]:
    """Keep signals for names that exist as skills."""
    cache: dict[str, bool] = {}
    kept: list[SkillSignal] = []
    for item in signals:
        if item.name not in cache:
            cache[item.name] = bool(is_known(item.name))
        if cache[item.name]:
            kept.append(item)
    return kept


def text_message_signals(messages: Iterable) -> list[SkillSignal]:
    """Signals from normalized text turns (Antigravity, Copilot).

    These stores keep no tool-call payloads, so an assistant turn that names a
    ``skills/<name>/SKILL.md`` path is the closest "open" signal, and user
    ``$name`` or ``/name`` is an invoke.
    """
    found: list[SkillSignal] = []
    for message in messages:
        text = getattr(message, "content", "") or ""
        session = getattr(message, "session_id", "") or ""
        created = int(getattr(message, "created_at", 0) or 0)
        if getattr(message, "role", "") == "user":
            if is_user_authored_text(text):
                found += [
                    SkillSignal(session, created, name, "invoke")
                    for name in invocations_in(text, slash=True)
                ]
            continue
        found += [
            SkillSignal(session, created, name, "open")
            for name in skill_paths_in(text)
        ]
    return found


def _codex_tool_call_text(payload: dict) -> str:
    """The part of a Codex tool call that carries its arguments."""
    for key in ("arguments", "input"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    action = payload.get("action")
    return json.dumps(action, ensure_ascii=False) if action else ""


def _codex_user_text(payload: dict) -> str:
    parts = payload.get("content") or []
    if not isinstance(parts, list):
        return ""
    return "\n".join(
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict) and part.get("type") in {"input_text", "text"}
    )


SPAWN_MARKER = "You were spawned in the background"


def _codex_line_may_signal(line: str) -> bool:
    """Cheap prefilter: skip JSON parsing for lines that cannot matter."""
    return (
        "SKILL.md" in line
        or '"session_meta"' in line
        or SPAWN_MARKER in line
        or ('"user"' in line and "$" in line)
    )


def codex_rollout_signals(
    lines: Iterable[str], parse_ts: Callable[[str], int],
    forced_cid: Callable[[str], str],
) -> Iterator[SkillSignal]:
    """Signals from one Codex ``rollout-*.jsonl`` stream.

    A rollout whose user turn carries a thread-keeper spawn marker belongs to
    a background child; it yields nothing.
    """
    session = ""
    pending: list[SkillSignal] = []
    for line in lines:
        if not _codex_line_may_signal(line):
            continue
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = envelope.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if envelope.get("type") == "session_meta":
            session = str(payload.get("id") or session)
            continue
        if envelope.get("type") != "response_item":
            continue
        created = parse_ts(str(envelope.get("timestamp") or ""))
        kind = payload.get("type")
        if kind in CODEX_TOOL_CALL_TYPES:
            pending += [
                SkillSignal(session, created, name, "open")
                for name in skill_paths_in(_codex_tool_call_text(payload))
            ]
            continue
        if kind != "message" or payload.get("role") != "user":
            continue
        text = _codex_user_text(payload)
        if forced_cid(text):
            return
        if is_user_authored_text(text):
            pending += [
                SkillSignal(session, created, name, "invoke")
                for name in invocations_in(text)
            ]
    for item in pending:
        yield SkillSignal(session or item.session_id, item.created_at, item.name, item.signal)
