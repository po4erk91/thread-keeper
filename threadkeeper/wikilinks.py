"""Find and repair wiki-style links between lessons and skills.

Lessons and skills are independently stored, but both can reference a stable
slug with ``[[slug]]``. Destructive lifecycle operations use this module so a
consolidation can redirect references to its umbrella entry, and a plain prune
can report the complete set of references it would otherwise strand.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .lessons import _BLOCK_RE, _lessons_file_lock


def _link_pattern(target: str) -> re.Pattern[str]:
    """Match a target at the start of a wiki link, preserving labels/anchors."""
    return re.compile(r"\[\[" + re.escape(target) + r"(?=\]\]|[|#])")


def _unique_roots(roots: Iterable[Path]) -> list[Path]:
    """Deduplicate configured mirror roots without requiring them to exist."""
    out: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        expanded = root.expanduser()
        try:
            key = expanded.resolve()
        except OSError:
            key = expanded.absolute()
        if key not in seen:
            seen.add(key)
            out.append(expanded)
    return out


def rewrite_inbound_wikilinks(
    target: str,
    replacement: str = "",
    *,
    lessons_path: Path,
    skill_roots: Iterable[Path],
) -> dict[str, list[str]]:
    """Find references to ``target`` and optionally redirect them.

    A link target is replaced only at the beginning of a wiki link, so labels
    (``[[old|label]]``) and fragments (``[[old#section]]``) remain intact.
    The entry being removed is excluded from the result: self-links are not
    inbound links and its content will be removed immediately afterward.
    """
    pattern = _link_pattern(target)
    lesson_refs: set[str] = set()
    skill_refs: set[str] = set()

    if lessons_path.exists():
        with _lessons_file_lock(lessons_path):
            current = lessons_path.read_text(encoding="utf-8")

            def replace_lesson(match: re.Match[str]) -> str:
                section = match.group(0)
                slug = match.group("slug")
                if slug == target or not pattern.search(section):
                    return section
                lesson_refs.add(slug)
                return pattern.sub("[[" + replacement, section) if replacement else section

            updated = _BLOCK_RE.sub(replace_lesson, current)
            if replacement and updated != current:
                lessons_path.write_text(updated, encoding="utf-8")

    seen_files: set[Path] = set()
    for root in _unique_roots(skill_roots):
        if not root.is_dir():
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            try:
                key = skill_md.resolve()
            except OSError:
                key = skill_md.absolute()
            if key in seen_files or skill_md.parent.name == target:
                continue
            seen_files.add(key)
            current = skill_md.read_text(encoding="utf-8")
            if not pattern.search(current):
                continue
            skill_refs.add(skill_md.parent.name)
            if replacement:
                skill_md.write_text(
                    pattern.sub("[[" + replacement, current), encoding="utf-8"
                )

    return {
        "lessons": sorted(lesson_refs),
        "skills": sorted(skill_refs),
    }


def format_inbound_wikilinks(refs: dict[str, list[str]]) -> str:
    """Render a compact, complete and stable source list for tool output."""
    items = [
        *(f"lesson:{slug}" for slug in refs["lessons"]),
        *(f"skill:{name}" for name in refs["skills"]),
    ]
    return ",".join(items)
