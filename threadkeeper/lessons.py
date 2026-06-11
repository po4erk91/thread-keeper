"""CLI-agnostic procedural-knowledge store at ~/.threadkeeper/lessons.md.

The learning loop (auto-review on close_thread + shadow_review daemon)
materializes lessons here. Every supported CLI's per-user instructions
file references this path so the lessons take effect in any of them.

Format on disk:

    # thread-keeper lessons

    Procedural knowledge accumulated across sessions. Auto-managed by
    the learning loop — do not edit by hand; new entries are appended.

    <!-- LESSON:BEGIN slug=<slug> ts=<unix> source=<thread_id|shadow> -->
    ## <slug>
    > <one-line summary>

    <body of the lesson>
    <!-- LESSON:END slug=<slug> -->

    <!-- LESSON:BEGIN ... -->
    ...

The sentinel-bracketed sections make per-entry diffs cheap and let us
update or de-duplicate without rewriting the whole file. New entries
land at the bottom (chronological).
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Iterator, Optional


_LESSONS_PATH = Path(
    os.environ.get("THREADKEEPER_LESSONS", "~/.threadkeeper/lessons.md")
).expanduser()


_HEADER = """\
# thread-keeper lessons

Procedural knowledge accumulated across sessions. Auto-managed by the
learning loop — do not edit by hand; new entries are appended.

"""


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slugify(title: str) -> str:
    """Produce a safe filesystem/url slug from a lesson title."""
    s = title.strip().lower().replace(" ", "-")
    s = _SLUG_RE.sub("-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "untitled"


def _ensure_file(path: Path) -> None:
    """Create the lessons file with the standard header if absent."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_HEADER)


def _format_section(slug: str, summary: str, body: str,
                    source: str, ts: int) -> str:
    """One LESSON-BEGIN…LESSON-END block with the sentinel markers."""
    summary_line = f"> {summary.strip()}" if summary.strip() else ""
    body_text = body.strip()
    return (
        f"<!-- LESSON:BEGIN slug={slug} ts={ts} source={source} -->\n"
        f"## {slug}\n"
        + (f"{summary_line}\n\n" if summary_line else "\n")
        + body_text + "\n"
        f"<!-- LESSON:END slug={slug} -->\n\n"
    )


_BLOCK_RE = re.compile(
    r"<!-- LESSON:BEGIN slug=(?P<slug>[^\s]+)[^>]*-->"
    r"(?P<body>.*?)"
    r"<!-- LESSON:END slug=(?P=slug) -->",
    re.DOTALL,
)


def append_lesson(
    title: str,
    body: str,
    summary: str = "",
    source: str = "",
    path: Optional[Path] = None,
) -> str:
    """Append a new lesson section, or replace an existing one with the
    same slug. Returns the slug.

    `title` becomes the section header (sluggified for the sentinel).
    `body` is markdown; `summary` is a one-liner shown right after the
    header. `source` is a free-text provenance tag — typically a thread
    id ("Tabc123") or "shadow" for shadow_review writes.
    """
    fp = path or _LESSONS_PATH
    _ensure_file(fp)
    slug = _slugify(title)
    ts = int(time.time())
    new_section = _format_section(slug, summary, body, source or "", ts)

    body_existing = fp.read_text()
    # If a section with this slug already exists, replace it in-place
    # (idempotent re-materialization of the same lesson).
    target_begin = f"<!-- LESSON:BEGIN slug={slug} "
    target_end = f"<!-- LESSON:END slug={slug} -->"
    if target_begin in body_existing and target_end in body_existing:
        head, _, rest = body_existing.partition(target_begin)
        # Find the matching END after the BEGIN.
        end_marker = target_end
        end_idx = rest.find(end_marker)
        if end_idx >= 0:
            tail = rest[end_idx + len(end_marker):]
            body_existing = head + new_section.rstrip() + "\n" + tail.lstrip("\n")
        else:
            # Malformed file (BEGIN without END) — just append at end.
            body_existing = body_existing.rstrip() + "\n\n" + new_section
    else:
        body_existing = body_existing.rstrip() + "\n\n" + new_section
    fp.write_text(body_existing)
    return slug


def remove_lesson(slug: str, path: Optional[Path] = None) -> bool:
    """Remove one lesson section by exact slug. Returns True when removed."""
    fp = path or _LESSONS_PATH
    if not fp.exists():
        return False
    body_existing = fp.read_text()
    target_begin = f"<!-- LESSON:BEGIN slug={slug} "
    target_end = f"<!-- LESSON:END slug={slug} -->"
    if target_begin not in body_existing or target_end not in body_existing:
        return False
    head, _, rest = body_existing.partition(target_begin)
    end_idx = rest.find(target_end)
    if end_idx < 0:
        return False
    tail = rest[end_idx + len(target_end):]
    new_body = head.rstrip() + "\n\n" + tail.lstrip("\n")
    fp.write_text(new_body.rstrip() + "\n")
    return True


def iter_lessons(path: Optional[Path] = None) -> Iterator[dict]:
    """Yield every lesson section as a dict with keys:
       slug, body (raw markdown between BEGIN/END), ts, source.

    Order is file-order (chronological if writes are append-only)."""
    fp = path or _LESSONS_PATH
    if not fp.exists():
        return
    body = fp.read_text()
    for m in _BLOCK_RE.finditer(body):
        slug = m.group("slug")
        block_body = m.group("body").strip()
        # Parse ts and source out of the BEGIN line we already matched.
        begin_line = body[m.start():m.start() + 200].split("\n", 1)[0]
        ts_match = re.search(r"ts=(\d+)", begin_line)
        source_match = re.search(r"source=([^\s>]+)", begin_line)
        yield {
            "slug": slug,
            "body": block_body,
            "ts": int(ts_match.group(1)) if ts_match else 0,
            "source": source_match.group(1) if source_match else "",
        }


def count_lessons(path: Optional[Path] = None) -> int:
    """Cheap count for diagnostic surfaces (brief, shadow_review_status)."""
    fp = path or _LESSONS_PATH
    if not fp.exists():
        return 0
    return len(_BLOCK_RE.findall(fp.read_text()))


def get_path() -> Path:
    """Public accessor — used by _setup to reference the file in the
    managed-instructions block."""
    return _LESSONS_PATH
