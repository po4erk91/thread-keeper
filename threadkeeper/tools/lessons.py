"""MCP tools that expose the CLI-agnostic lessons store.

  lesson_append(title, body, summary, source)
    Materialize a class-level lesson into ~/.threadkeeper/lessons.md.
    Idempotent on slug — re-calling with the same title overwrites the
    existing section.

  lesson_list(k=20)
    Compact listing for inspection / diagnostics.

  lesson_get(slug)
    Return the full body of a single lesson by slug.

The learning loop (review_thread + shadow_review) writes here instead
of (or in addition to) ~/.claude/skills/*/SKILL.md so non-Claude CLIs
share the procedural-knowledge surface. Each CLI's per-user
instructions file references this path via the managed thread-keeper
block written by `_setup.py`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .._mcp import mcp
from ..identity import _ensure_session
from ..db import get_db
from ..lessons import (
    append_lesson,
    iter_lessons,
    count_lessons,
    get_path,
)


@mcp.tool()
def lesson_append(
    title: str,
    body: str,
    summary: str = "",
    source: str = "",
) -> str:
    """Materialize a class-level lesson into ~/.threadkeeper/lessons.md.

    `title` is sluggified to a stable key — repeated calls with the same
    title overwrite the existing section (idempotent).

    `body` is markdown; goes verbatim into the section body.

    `summary` is an optional one-liner rendered as a blockquote right
    after the header. Use when the body is long and a TL;DR helps the
    next agent decide whether to read further.

    `source` is a provenance tag — typically a thread id (\"Tabc123\")
    when written by review_thread, or \"shadow\" when written by the
    shadow_review observer. Empty is fine.
    """
    conn = get_db()
    _ensure_session(conn)
    if not title.strip():
        return "ERR empty_title"
    if not body.strip():
        return "ERR empty_body"
    slug = append_lesson(
        title=title, body=body, summary=summary, source=source,
    )
    return f"ok slug={slug} path={get_path()}"


@mcp.tool()
def lesson_list(k: int = 20) -> str:
    """Compact listing of materialized lessons, newest first.

    Format per line: `<age>  <slug>  source=<src>  <first 60 chars of body>`
    """
    conn = get_db()
    _ensure_session(conn)
    items = list(iter_lessons())
    if not items:
        return "no_lessons"
    items.sort(key=lambda x: x["ts"], reverse=True)
    now = int(datetime.now().timestamp())
    out: list[str] = [f"lessons total={len(items)} path={get_path()}"]
    for it in items[:max(1, k)]:
        age_s = max(0, now - it["ts"])
        age = (
            f"{age_s}s"
            if age_s < 60
            else f"{age_s // 60}m"
            if age_s < 3600
            else f"{age_s // 3600}h"
            if age_s < 86400
            else f"{age_s // 86400}d"
        )
        snippet = " ".join(it["body"].split())[:60]
        src = it["source"] or "?"
        out.append(f"  {age:>5s}  {it['slug']:30s}  src={src:8s}  {snippet}")
    return "\n".join(out)


@mcp.tool()
def lesson_get(slug: str) -> str:
    """Return the full body of one lesson by slug. Useful when
    `lesson_list` surfaced something you want to read in full."""
    conn = get_db()
    _ensure_session(conn)
    for it in iter_lessons():
        if it["slug"] == slug:
            return it["body"]
    return f"ERR not_found slug={slug}"
