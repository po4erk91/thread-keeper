"""Recovery artifacts for destructive curator operations.

The curator is destructive by default, so delete-class tools must persist a
pre-image before they remove lessons or skills. Artifacts live beside curator
reports under ``<db dir>/curator/trash`` and are pruned by a configurable TTL.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import CURATOR_TRASH_DIR, CURATOR_TRASH_TTL_DAYS


_SAFE_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _safe_name(value: str) -> str:
    cleaned = _SAFE_RE.sub("-", value.strip()).strip("-")
    return cleaned[:80] or "item"


def _artifact_base(kind: str, name: str, now: int) -> str:
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    return f"{stamp}-{kind}-{_safe_name(name)}"


def _new_artifact_dir(kind: str, name: str, *, now: Optional[int] = None) -> Path:
    now_t = int(now or time.time())
    CURATOR_TRASH_DIR.mkdir(parents=True, exist_ok=True)
    base = _artifact_base(kind, name, now_t)
    for i in range(1000):
        suffix = "" if i == 0 else f"-{i}"
        path = CURATOR_TRASH_DIR / f"{base}{suffix}"
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"could not allocate trash artifact for {kind}:{name}")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_meta(path: Path, meta: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(meta, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_meta(path: Path) -> dict[str, Any]:
    return json.loads((path / "meta.json").read_text(encoding="utf-8"))


def sweep_expired_trash(*, now: Optional[int] = None) -> int:
    """Delete recovery artifacts older than CURATOR_TRASH_TTL_DAYS.

    Returns the number of artifact directories removed. Malformed entries are
    left in place; only directories with readable metadata are swept.
    """
    root = CURATOR_TRASH_DIR
    if not root.exists():
        return 0
    ttl_days = max(0, int(CURATOR_TRASH_TTL_DAYS))
    cutoff = int(now or time.time()) - ttl_days * 86400
    removed = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            meta = _read_meta(child)
            created_at = int(meta.get("created_at") or 0)
        except Exception:
            continue
        if created_at <= cutoff:
            shutil.rmtree(child)
            removed += 1
    return removed


def capture_removed_lesson(
    *,
    slug: str,
    section: str,
    source: str,
    lessons_path: Path,
    char_start: int,
    char_end: int,
    post_remove_sha256: str,
    usage_row: Optional[dict[str, Any]] = None,
    now: Optional[int] = None,
) -> Path:
    """Persist the exact lesson section that is about to be removed."""
    now_t = int(now or time.time())
    sweep_expired_trash(now=now_t)
    artifact = _new_artifact_dir("lesson", slug, now=now_t)
    data = section.encode("utf-8")
    (artifact / "section.md").write_bytes(data)
    _write_meta(
        artifact / "meta.json",
        {
            "version": 1,
            "kind": "lesson",
            "slug": slug,
            "source": source,
            "created_at": now_t,
            "original_path": str(lessons_path),
            "char_start": char_start,
            "char_end": char_end,
            "post_remove_sha256": post_remove_sha256,
            "usage_row": usage_row or None,
            "sha256": _sha256_bytes(data),
        },
    )
    return artifact


def capture_removed_skill(
    *,
    name: str,
    skill_dir: Path,
    usage_row: Optional[dict[str, Any]] = None,
    now: Optional[int] = None,
) -> Path:
    """Copy a skill directory and usage row before deletion."""
    now_t = int(now or time.time())
    sweep_expired_trash(now=now_t)
    artifact = _new_artifact_dir("skill", name, now=now_t)
    dst = artifact / "skill"
    shutil.copytree(skill_dir, dst, copy_function=shutil.copy2)
    md = dst / "SKILL.md"
    sha = _sha256_bytes(md.read_bytes()) if md.exists() else ""
    _write_meta(
        artifact / "meta.json",
        {
            "version": 1,
            "kind": "skill",
            "name": name,
            "created_at": now_t,
            "original_path": str(skill_dir),
            "skill_md_sha256": sha,
            "usage_row": usage_row or None,
        },
    )
    return artifact


def _latest_artifact(kind: str, key: str, field: str) -> Optional[Path]:
    root = CURATOR_TRASH_DIR
    if not root.exists():
        return None
    matches: list[tuple[int, str, Path]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            meta = _read_meta(child)
        except Exception:
            continue
        if meta.get("kind") != kind or meta.get(field) != key:
            continue
        matches.append((int(meta.get("created_at") or 0), child.name, child))
    if not matches:
        return None
    matches.sort()
    return matches[-1][2]


def latest_lesson_artifact(slug: str) -> Optional[Path]:
    return _latest_artifact("lesson", slug, "slug")


def latest_skill_artifact(name: str) -> Optional[Path]:
    return _latest_artifact("skill", name, "name")


def read_lesson_artifact(artifact: Path) -> tuple[str, dict[str, Any]]:
    meta = _read_meta(artifact)
    if meta.get("kind") != "lesson":
        raise ValueError("trash artifact is not a lesson")
    data = (artifact / "section.md").read_bytes()
    if meta.get("sha256") != _sha256_bytes(data):
        raise ValueError("lesson trash artifact checksum mismatch")
    return data.decode("utf-8"), meta


def read_skill_artifact(artifact: Path) -> tuple[Path, dict[str, Any]]:
    meta = _read_meta(artifact)
    if meta.get("kind") != "skill":
        raise ValueError("trash artifact is not a skill")
    src = artifact / "skill"
    if not src.is_dir():
        raise ValueError("skill trash artifact missing skill directory")
    md = src / "SKILL.md"
    expected = meta.get("skill_md_sha256") or ""
    if expected and _sha256_bytes(md.read_bytes()) != expected:
        raise ValueError("skill trash artifact checksum mismatch")
    return src, meta
