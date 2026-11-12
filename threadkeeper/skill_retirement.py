"""Validate rule-only retirements and record their deletion history."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3

from .lessons import _slugify, iter_lessons
from .skill_audit import _frontmatter


RULE_RETIREMENT_EVENT = "curator_skill_rule_retired"
_RULE_SKILL_FILES = frozenset({"SKILL.md", "LICENSE", "LICENSE.md", "LICENSE.txt"})


@dataclass(frozen=True)
class RuleRetirement:
    score: int
    reason: str
    lesson_slug: str
    excerpt: str

    def log_details(self, recovery: str) -> dict:
        return {
            "usefulness_score": self.score,
            "reason": self.reason.strip(),
            "retained_lesson": self.lesson_slug,
            "recovery": recovery,
        }


def _rule_text(markdown: str) -> str:
    lines = (
        re.sub(r"^\s*(?:[-*+]\s+|>\s*)", "", line)
        for line in markdown.splitlines()
        if not re.match(r"^\s*#{1,6}\s", line)
    )
    return " ".join(" ".join(lines).split())


def validate_rule_retirement(
    retirement: RuleRetirement, skill_dir: Path, lessons_path: Path,
) -> str:
    if type(retirement.score) is not int or retirement.score not in (1, 2):
        return "ERR rule_retirement_requires_low_score"
    if not 1 <= len(retirement.reason.strip()) <= 500:
        return "ERR rule_retirement_requires_reason"
    slug = retirement.lesson_slug
    if not slug or slug != _slugify(slug):
        return "ERR rule_retirement_requires_lesson"
    excerpt = _rule_text(retirement.excerpt)
    if not excerpt:
        return "ERR rule_retirement_requires_rule_excerpt"
    try:
        if skill_dir.is_symlink() or any(
            child.name not in _RULE_SKILL_FILES or child.is_symlink() or not child.is_file()
            for child in skill_dir.iterdir()
        ):
            return "ERR rule_retirement_has_support_files"
        _, body, invalid = _frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
        if invalid or excerpt != _rule_text(body) or re.search(r"(?m)^\s*(?:`{3,}|~{3,})", body):
            return "ERR rule_retirement_rule_mismatch"
        lesson = next((item for item in iter_lessons(lessons_path) if item["slug"] == slug), None)
    except (OSError, UnicodeError):
        return "ERR rule_retirement_evidence_unavailable"
    if lesson is None:
        return "ERR rule_retirement_lesson_missing"
    if f" {excerpt} " not in f" {_rule_text(lesson['body'])} ":
        return "ERR rule_retirement_rule_not_preserved"
    return ""


def record_rule_retirement(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    name: str,
    details: dict,
) -> None:
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES (?, ?, ?, ?, strftime('%s','now'))",
        (session_id, RULE_RETIREMENT_EVENT, name, json.dumps(details, ensure_ascii=False)),
    )


def rule_retirement_log(
    conn: sqlite3.Connection, *, name: str, limit: int,
) -> list[dict]:
    rows = conn.execute(
        "SELECT target, summary, created_at FROM events "
        "WHERE kind=? AND (?='' OR target=?) ORDER BY id DESC LIMIT ?",
        (RULE_RETIREMENT_EVENT, name, name, limit),
    )
    return [
        {"name": row["target"], "deleted_at": row["created_at"], **json.loads(row["summary"])}
        for row in rows
    ]
