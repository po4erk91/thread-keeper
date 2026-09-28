"""Read-only integrity checks for links between lessons and skills."""

from __future__ import annotations

from pathlib import Path
import re
import sqlite3

from .lessons import iter_lessons
from .skill_audit import _frontmatter, build_skill_audit


WIKILINK_HEALTH_SCHEMA_VERSION = 1
_WIKILINK_RE = re.compile(r"\[\[([a-z0-9]+(?:-[a-z0-9]+)*)\]\]")


def _targets(body: str) -> list[str]:
    """Return every canonical wikilink target in one entry body."""
    return _WIKILINK_RE.findall(body)


def scan_wikilink_health(
    conn: sqlite3.Connection,
    *,
    include_archived: bool = True,
) -> dict:
    """Report unresolved lesson/skill wikilinks without changing either store."""
    lessons = list(iter_lessons())
    audit = build_skill_audit(conn, include_archived=include_archived)
    skills = audit["skills"]
    resolved = {
        item["slug"] for item in lessons
    } | {
        record["name"] for record in skills
    }
    skill_bodies: list[tuple[str, str]] = []
    for record in skills:
        source = record.get("source_path")
        if not source:
            continue
        try:
            content = Path(source).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        _fields, body, _error = _frontmatter(content)
        skill_bodies.append((record["name"], body))

    findings: list[dict[str, str]] = []
    for item in lessons:
        for target in _targets(item.get("body") or ""):
            if target not in resolved:
                findings.append({
                    "source_kind": "lesson",
                    "source": item["slug"],
                    "target": target,
                })
    for source, body in skill_bodies:
        for target in _targets(body):
            if target not in resolved:
                findings.append({
                    "source_kind": "skill",
                    "source": source,
                    "target": target,
                })

    findings.sort(key=lambda item: (
        item["source_kind"], item["source"], item["target"],
    ))
    return {
        "schema_version": WIKILINK_HEALTH_SCHEMA_VERSION,
        "scope": "all materialized lesson and skill bodies",
        "summary": {
            "lessons_scanned": len(lessons),
            "skills_scanned": len(skill_bodies),
            "references_scanned": sum(
                len(_targets(item.get("body") or "")) for item in lessons
            ) + sum(len(_targets(body)) for _source, body in skill_bodies),
            "dangling_references": len(findings),
        },
        "dangling_references": findings,
    }
