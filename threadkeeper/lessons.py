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

from contextlib import contextmanager
import math
import os
import re
import sqlite3
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


@contextmanager
def _lessons_file_lock(path: Path) -> Iterator[None]:
    """Serialize read-modify-write access to one lessons.md file."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - thread-keeper runs on Unix CLIs.
        yield
        return

    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


LESSON_DECAY_TAU_DAYS = 45
LESSON_STALE_AFTER_DAYS = 30
LESSON_STALE_MAX_PULLS = 1
LESSON_STALE_REPORT_LIMIT = 20


def _row_dict(row) -> dict:
    if row is None:
        return {}
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return dict(row)


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
    slug = _slugify(title)
    ts = int(time.time())
    new_section = _format_section(slug, summary, body, source or "", ts)

    with _lessons_file_lock(fp):
        _ensure_file(fp)
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
                body_existing = (
                    head + new_section.rstrip() + "\n" + tail.lstrip("\n")
                )
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
    with _lessons_file_lock(fp):
        found = lesson_section(slug, path=fp)
        if not found:
            return False
        body_existing = found["file_body"]
        new_body = body_existing[:found["start"]] + body_existing[found["end"]:]
        fp.write_text(new_body)
    return True


def lesson_section(slug: str, path: Optional[Path] = None) -> Optional[dict]:
    """Return the exact sentinel section and character offsets for one lesson.

    The returned ``section`` includes the BEGIN/END markers and any immediate
    trailing newlines. This is the recovery pre-image used by destructive
    removal and restore.
    """
    fp = path or _LESSONS_PATH
    if not fp.exists():
        return None
    body_existing = fp.read_text()
    target_begin = f"<!-- LESSON:BEGIN slug={slug} "
    target_end = f"<!-- LESSON:END slug={slug} -->"
    start = body_existing.find(target_begin)
    if start < 0:
        return None
    end_idx = body_existing.find(target_end, start)
    if end_idx < 0:
        return None
    marker_end = end_idx + len(target_end)
    end = marker_end
    while end < len(body_existing) and body_existing[end] == "\n":
        end += 1
    begin_line = body_existing[start:marker_end].split("\n", 1)[0]
    ts_match = re.search(r"ts=(\d+)", begin_line)
    source_match = re.search(r"source=([^\s>]+)", begin_line)
    return {
        "slug": slug,
        "section": body_existing[start:end],
        "start": start,
        "end": end,
        "ts": int(ts_match.group(1)) if ts_match else 0,
        "source": source_match.group(1) if source_match else "",
        "file_body": body_existing,
    }


def restore_lesson_section(
    slug: str,
    section: str,
    *,
    char_start: Optional[int] = None,
    path: Optional[Path] = None,
) -> bool:
    """Restore an exact lesson section. Refuses to overwrite an existing slug."""
    fp = path or _LESSONS_PATH
    with _lessons_file_lock(fp):
        _ensure_file(fp)
        if lesson_section(slug, path=fp):
            return False
        body_existing = fp.read_text()
        insert_at = len(body_existing)
        if char_start is not None and 0 <= char_start <= len(body_existing):
            insert_at = char_start
        new_body = body_existing[:insert_at] + section + body_existing[insert_at:]
        fp.write_text(new_body)
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


def ensure_lesson_usage(
    conn: sqlite3.Connection,
    item: dict,
    *,
    now: Optional[int] = None,
) -> None:
    """Ensure a lesson_usage row exists for a lessons.md item.

    The filesystem store remains canonical for lesson body/source/ts. This
    side table is telemetry only: access counters, optional pin/tier fields,
    and timestamps used by decay scoring.
    """
    slug = item.get("slug") or ""
    if not slug:
        return
    now_t = int(now or time.time())
    created = int(item.get("ts") or now_t)
    source = item.get("source") or ""
    conn.execute(
        "INSERT INTO lesson_usage (slug, created_at, source) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(slug) DO UPDATE SET "
        "source=CASE "
        "  WHEN excluded.source IS NOT NULL AND excluded.source != '' "
        "  THEN excluded.source ELSE lesson_usage.source END",
        (slug, created, source),
    )


def record_lesson_access(
    conn: sqlite3.Connection,
    item: dict,
    *,
    kind: str,
    now: Optional[int] = None,
) -> None:
    """Bump access telemetry for a lesson.

    kind='view' records that the lesson appeared in lesson_list output.
    kind='use' records that lesson_get returned the lesson body.
    """
    if kind not in {"view", "use"}:
        raise ValueError(f"unknown lesson access kind: {kind}")
    slug = item.get("slug") or ""
    if not slug:
        return
    now_t = int(now or time.time())
    ensure_lesson_usage(conn, item, now=now_t)
    if kind == "view":
        conn.execute(
            "UPDATE lesson_usage "
            "SET last_viewed_at=?, view_count=view_count + 1 "
            "WHERE slug=?",
            (now_t, slug),
        )
    else:
        conn.execute(
            "UPDATE lesson_usage "
            "SET last_used_at=?, use_count=use_count + 1 "
            "WHERE slug=?",
            (now_t, slug),
        )


def lesson_usage_map(conn: sqlite3.Connection) -> dict[str, dict]:
    """Return lesson_usage rows keyed by slug, or {} when the table is absent."""
    try:
        rows = conn.execute("SELECT * FROM lesson_usage").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r["slug"]: _row_dict(r) for r in rows}


def lesson_protection(item: dict, usage: Optional[dict] = None) -> tuple[bool, str]:
    """Return (protected, reason) for curator decay handling."""
    usage = usage or {}
    source = (usage.get("source") or item.get("source") or "").strip().lower()
    if source in {"foreground", "user"}:
        return True, source
    if int(usage.get("pinned") or 0):
        return True, "pinned"
    if (usage.get("tier") or "hypothesis") == "validated":
        return True, "validated"
    return False, ""


def lesson_retention_score(
    item: dict,
    usage: Optional[dict] = None,
    *,
    now: Optional[int] = None,
    tau_days: float = LESSON_DECAY_TAU_DAYS,
) -> dict:
    """Compute recency/frequency retention metadata for one lesson.

    score = access_frequency * exp(-days_since_access / tau)
    where access_frequency is pull_count per day since lesson creation.
    """
    usage = usage or {}
    now_t = int(now or time.time())
    created = int(usage.get("created_at") or item.get("ts") or now_t)
    lesson_ts = int(item.get("ts") or created)
    last_used = int(usage.get("last_used_at") or 0)
    last_viewed = int(usage.get("last_viewed_at") or 0)
    # A freshly written/replaced lesson should not be considered stale before
    # anyone has had a chance to read it, so the file timestamp is a freshness
    # baseline when no access timestamp exists.
    last_access = max(last_used, last_viewed, lesson_ts, created)
    use_count = int(usage.get("use_count") or 0)
    view_count = int(usage.get("view_count") or 0)
    pull_count = use_count + view_count
    age_days = max(0.0, (now_t - last_access) / 86400.0)
    lifetime_days = max(1.0, (now_t - min(created, lesson_ts)) / 86400.0)
    access_frequency = pull_count / lifetime_days
    tau = max(1.0, float(tau_days))
    score = access_frequency * math.exp(-age_days / tau)
    protected, reason = lesson_protection(item, usage)
    return {
        "slug": item.get("slug") or usage.get("slug") or "",
        "source": usage.get("source") or item.get("source") or "",
        "created_at": created,
        "lesson_ts": lesson_ts,
        "last_used_at": last_used or None,
        "last_viewed_at": last_viewed or None,
        "last_access_at": last_access,
        "use_count": use_count,
        "view_count": view_count,
        "pull_count": pull_count,
        "pinned": int(usage.get("pinned") or 0),
        "tier": usage.get("tier") or "hypothesis",
        "protected": protected,
        "protected_reason": reason,
        "age_days": age_days,
        "access_frequency": access_frequency,
        "decay_score": score,
    }


def rank_stale_lessons(
    conn: sqlite3.Connection,
    *,
    now: Optional[int] = None,
    stale_after_days: int = LESSON_STALE_AFTER_DAYS,
    low_pull_count: int = LESSON_STALE_MAX_PULLS,
    tau_days: float = LESSON_DECAY_TAU_DAYS,
    limit: int = LESSON_STALE_REPORT_LIMIT,
) -> list[dict]:
    """Rank advisory stale-lesson candidates by increasing retention score.

    This function never mutates lessons.md and never returns protected lessons.
    It is the dry-run signal the curator can surface before any human decides
    whether an old lesson should be composted.
    """
    now_t = int(now or time.time())
    usage = lesson_usage_map(conn)
    ranked: list[dict] = []
    for item in iter_lessons():
        score = lesson_retention_score(
            item, usage.get(item["slug"]), now=now_t, tau_days=tau_days,
        )
        if score["protected"]:
            continue
        if score["age_days"] < stale_after_days:
            continue
        if score["pull_count"] > low_pull_count:
            continue
        ranked.append(score)
    ranked.sort(
        key=lambda s: (
            s["decay_score"],
            s["pull_count"],
            -s["age_days"],
            s["slug"],
        )
    )
    return ranked[:max(1, int(limit))]


def get_path() -> Path:
    """Public accessor — used by _setup to reference the file in the
    managed-instructions block."""
    return _LESSONS_PATH
