"""Deterministic quality checks for SKILL.md text.

Every check here is a pure function of the text (plus, for staleness, the
local filesystem). The write gates in ``tools/skills.py`` and the audit in
``skill_audit.py`` both call them, so a skill is judged the same way when it
is written and when it is audited.

Findings are short ``kind:detail`` strings. The checks target the defects a
skill audit found in loop-written skills:

* history instead of rules (thread or ticket IDs, dates, incident counters,
  version pins used as a story, user quotes, personal paths);
* missing parts of the fixed template (Use when, Not for, Steps, Check, Why);
* thread-keeper ``[[lesson]]`` wikilinks that no other CLI can resolve;
* project examples inside a skill meant to be shared;
* many unrelated topics in one skill (a split candidate);
* references to files, tools and commands that no longer exist.
"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
from typing import Iterable

_FENCE_BLOCK_RE = re.compile(r"(^|\n)\s*(```|~~~)[^\n]*\n.*?(\n\s*\2[^\n]*|$)", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

_THREAD_ID_RE = re.compile(r"\bT(?=[0-9a-f]{0,2}[0-9])[0-9a-f]{3}\b")
_TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d{2,6})\b")
_TICKET_PREFIX_ALLOW = frozenset({
    "AES", "CVE", "CWE", "ES", "GPT", "HS", "HTTP", "IEC", "ISO", "MD", "PS",
    "RFC", "RS", "SHA", "TLS", "SSL", "UTF", "WCAG", "X", "P",
})
_WORK_ITEM_RE = re.compile(r"\bAB#\d+\b")
_PR_NUMBER_RE = re.compile(r"(?<![\w&#])#\d{2,6}\b|\bPR\s*#?\d{2,6}\b|/pull/\d+\b", re.I)
_DATE_RE = re.compile(r"\b20\d{2}[-/](?:0[1-9]|1[0-2])[-/](?:0[1-9]|[12]\d|3[01])\b")
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_COUNTER_RES = (
    re.compile(
        r"\b(?:seen|happened|hit|occurred|failed|broke|repeated|recurred)\s+"
        r"(?:\w+\s+)?\d+\s*(?:x|times)\b", re.I,
    ),
    re.compile(r"\b\d+\s+(?:incidents|occurrences|regressions|recurrences)\b", re.I),
    re.compile(r"\bfor\s+the\s+(?:\d+(?:st|nd|rd|th)|second|third|fourth|fifth)\s+time\b", re.I),
)
_VERSION_NARRATIVE_RE = re.compile(
    r"\b(?:since|as of|in|after|before|until|fixed in|introduced in|added in|"
    r"upgraded to|upgrading to)\s+(v\d+\.\d+(?:\.\d+)?|\d+\.\d+\.\d+)\b"
    r"(?!\s*(?:s|ms|sec|seconds|min|minutes|h|hours|%|gb|mb|kb|x)\b)",
    re.I,
)
_QUOTE_RES = (
    re.compile(
        r"\b(?:the\s+)?(?:user|owner|пользователь)\s+(?:said|wrote|asked|stated|told|"
        r"сказал|написал|попросил)\b\s*[:,]?\s*[\"«“]",
        re.I,
    ),
    re.compile(r"«[^»\n]{15,}»"),
    re.compile(r"“[^”\n]{30,}”"),
    re.compile(r"\"[^\"\n]*[а-яё][^\"\n]{12,}\"", re.I),
)
_USER_PATH_RE = re.compile(r"(?:/Users|/home)/[A-Za-z0-9._-]+/")

_WIKILINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", re.M)
_TEMPLATE_HEADINGS = {
    "not_for": re.compile(r"^(?:not for|when not to use|do not use|out of scope)\b", re.I),
    "steps": re.compile(r"^(?:steps|procedure|how to|workflow)\b", re.I),
    "check": re.compile(r"^(?:check|checks|verify|verification|done when)\b", re.I),
    "why": re.compile(r"^why\b", re.I),
}
_GENERIC_SECTIONS = re.compile(
    r"^(?:overview|summary|references?|examples?|notes?|use when|when to use|"
    r"related|see also|contents|quick reference)\b",
    re.I,
)
WHY_MAX_CHARS = 300
SPLIT_SECTION_LIMIT = 10
SPLIT_SECTION_MIN_LINES = 110
SPLIT_LINE_LIMIT = 180
_TOP_HEADING_RE = re.compile(r"^##\s+(.+?)\s*#*\s*$", re.M)
_CLI_NAME_RE = re.compile(r"^[a-z][a-z0-9._+-]*$")

_MCP_TOOL_RE = re.compile(r"mcp__thread-keeper__([a-z_][a-z0-9_]*)")
_HOME_PATH_RE = re.compile(r"(?<![\w./-])(~/[\w./-]+|/(?:Users|home|opt|usr/local)/[\w./-]+)")
_SHELL_FENCE_RE = re.compile(r"```(?:bash|sh|zsh|shell|console)\n(.*?)```", re.S)
_VERSION_PIN_RE = re.compile(r"(?<![\w/])([@\w.-]+@\d+\.\d+\.\d+|[\w.-]+==\d+(?:\.\d+)+)\b")
_SHELL_WORDS = frozenset({
    ".", "[", "[[", "case", "cd", "do", "done", "echo", "elif", "else", "esac",
    "eval", "exec", "exit", "export", "false", "fi", "for", "function", "if",
    "local", "printf", "read", "return", "set", "shift", "source", "then",
    "trap", "true", "unset", "until", "wait", "while", "#",
})


def prose_only(text: str) -> str:
    """The text without fenced code blocks and inline code spans."""
    without_fences = _FENCE_BLOCK_RE.sub("\n", text or "")
    return _INLINE_CODE_RE.sub(" ", without_fences)


def _ticket_ids(prose: str) -> list[str]:
    return [
        f"{prefix}-{number}" for prefix, number in _TICKET_RE.findall(prose)
        if prefix not in _TICKET_PREFIX_ALLOW
    ]


def history_noise(text: str) -> list[str]:
    """Incident-history markers that should have been generalized away."""
    prose = prose_only(text)
    found: list[str] = []
    found += [f"thread_id:{m}" for m in _THREAD_ID_RE.findall(prose)]
    found += [f"ticket_id:{m}" for m in _ticket_ids(prose)]
    found += [f"work_item:{m}" for m in _WORK_ITEM_RE.findall(prose)]
    found += [f"pr_number:{m.strip()}" for m in _PR_NUMBER_RE.findall(prose)]
    found += [f"date:{m}" for m in _DATE_RE.findall(prose)]
    found += [f"session_id:{m[:8]}" for m in _UUID_RE.findall(prose)]
    for pattern in _COUNTER_RES:
        found += [f"incident_counter:{m}" for m in pattern.findall(prose)]
    found += [f"version_narrative:{m}" for m in _VERSION_NARRATIVE_RE.findall(prose)]
    if any(pattern.search(prose) for pattern in _QUOTE_RES):
        found.append("user_quote")
    found += [f"user_path:{m}" for m in sorted(set(_USER_PATH_RE.findall(text or "")))]
    return list(dict.fromkeys(found))


def wikilinks(text: str) -> list[str]:
    """``[[target]]`` links; only thread-keeper can resolve them."""
    return list(dict.fromkeys(_WIKILINK_RE.findall(prose_only(text))))


def _sections(body: str) -> list[tuple[str, str]]:
    """(heading, section text) for every markdown heading in the body."""
    matches = list(_HEADING_RE.finditer(body or ""))
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections.append((match.group(2).strip(), body[match.end():end]))
    return sections


def _why_is_one_line(section: str) -> bool:
    lines = [line for line in section.strip().splitlines() if line.strip()]
    return len(lines) == 1 and len(lines[0]) <= WHY_MAX_CHARS


def template_gaps(description: str, body: str) -> list[str]:
    """Missing parts of the fixed template: Use when, Not for, Steps, Check, Why."""
    gaps: list[str] = []
    if not re.match(r"^\s*use when\b", description or "", re.I):
        gaps.append("description_must_start_with_use_when")
    sections = _sections(body)
    for part, pattern in _TEMPLATE_HEADINGS.items():
        matching = [text for heading, text in sections if pattern.match(heading)]
        if not matching:
            gaps.append(f"missing_section:{part}")
        elif part == "why" and not _why_is_one_line(matching[0]):
            gaps.append("why_must_be_one_line")
    return gaps


def _topic_sections(body: str) -> int:
    headings = [match.strip() for match in _TOP_HEADING_RE.findall(body or "")]
    return len([
        heading for heading in headings
        if not _GENERIC_SECTIONS.match(heading)
        and not any(pattern.match(heading) for pattern in _TEMPLATE_HEADINGS.values())
    ])


def split_candidate(body: str) -> list[str]:
    """Signs that one skill holds several topics and should be split.

    A size heuristic only: very long bodies, or many top-level topic sections
    in a long body. Curator still judges the meaning.
    """
    lines = len((body or "").splitlines())
    if lines > SPLIT_LINE_LIMIT:
        return [f"split_candidate:lines={lines}"]
    sections = _topic_sections(body)
    if sections >= SPLIT_SECTION_LIMIT and lines >= SPLIT_SECTION_MIN_LINES:
        return [f"split_candidate:topic_sections={sections}"]
    return []


def project_mentions(text: str, terms: Iterable[str]) -> list[str]:
    """Configured project names that appear in the text."""
    found: list[str] = []
    for term in terms:
        clean = term.strip()
        if clean and re.search(rf"(?<![\w-]){re.escape(clean)}(?![\w-])", text or "", re.I):
            found.append(clean.lower())
    return list(dict.fromkeys(found))


def _is_concrete_missing_path(raw: str) -> bool:
    """A path whose first directory exists but whose target does not.

    Illustrative paths (``~/.app/runs``) and machine-specific roots that are
    absent here (``~/Android``) are not evidence of drift.
    """
    candidate = Path(raw).expanduser()
    if raw.endswith(("_", "-")) or candidate.exists():
        return False
    parts = candidate.parts
    anchor_depth = 3 if raw.startswith(("/Users/", "/home/")) else len(Path.home().parts) + 1
    if raw.startswith(("/opt/", "/usr/local/")):
        anchor_depth = 3
    if len(parts) <= anchor_depth:
        return False
    return Path(*parts[:anchor_depth]).exists()


def _missing_paths(text: str) -> list[str]:
    missing: list[str] = []
    for raw in sorted(set(_HOME_PATH_RE.findall(text or ""))):
        candidate = raw.rstrip(".,;:)")
        if "<" in candidate or "*" in candidate:
            continue
        if _is_concrete_missing_path(candidate):
            missing.append(f"stale:missing_path:{candidate}")
    return missing


def _unknown_tools(text: str, known_tools: set[str] | None) -> list[str]:
    if not known_tools:
        return []
    return [
        f"stale:unknown_tool:{name}"
        for name in sorted(set(_MCP_TOOL_RE.findall(text or "")))
        if name not in known_tools
    ]


def _command_of(line: str) -> str:
    words = [word for word in line.strip().split() if "=" not in word.split("/")[0]]
    if not words:
        return ""
    command = words[0].lstrip("(")
    return command if _CLI_NAME_RE.match(command) else ""


def _missing_commands(text: str) -> list[str]:
    missing: set[str] = set()
    for block in _SHELL_FENCE_RE.findall(text or ""):
        for line in block.splitlines():
            command = _command_of(line)
            if command and command not in _SHELL_WORDS and not shutil.which(command):
                missing.add(command)
    return [f"stale:missing_command:{name}" for name in sorted(missing)]


def stale_references(text: str, known_tools: set[str] | None = None) -> list[str]:
    """Files, thread-keeper tools and commands the skill names that are gone,
    plus version pins a reader should re-check against the current release."""
    findings = _missing_paths(text) + _unknown_tools(text, known_tools)
    findings += _missing_commands(text)
    findings += [
        f"stale:version_pin:{pin}" for pin in sorted(set(_VERSION_PIN_RE.findall(text or "")))
    ]
    return findings


def added_text(before: str, after: str) -> str:
    """Lines present in ``after`` but not in ``before``.

    Write gates judge only what a write adds, so an existing skill with old
    noise stays patchable while new noise is refused.
    """
    existing = set((before or "").splitlines())
    return "\n".join(line for line in (after or "").splitlines() if line not in existing)
