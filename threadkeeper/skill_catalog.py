"""The complete skill catalog: every local and plugin skill, by meaning.

Loop writers used to see only ~15 recently active skills, so they kept
creating skills that already existed under another name. This module gives
them the name and description of every installed skill and finds duplicates
by embedding similarity instead of shared words.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from pathlib import Path

import yaml

DESCRIPTION_CHARS = 160
_vector_cache: dict[str, object] = {}


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    description: str
    kind: str
    protected: bool
    uses: int


def _frontmatter_description(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    if not content.startswith("---"):
        return ""
    end = content.find("\n---", 3)
    if end < 0:
        return ""
    try:
        fields = yaml.safe_load(content[3:end]) or {}
    except yaml.YAMLError:
        return ""
    return " ".join(str(fields.get("description") or "").split()) if isinstance(fields, dict) else ""


def _usage_rows(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    try:
        rows = conn.execute(
            "SELECT name, pinned, created_by_origin, use_count, state, classification "
            "FROM skill_usage"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row["name"]: row for row in rows}


def _is_protected(row: sqlite3.Row | None) -> bool:
    if row is None:
        return True
    from .tools.skills import _skill_is_protected

    return bool(row["pinned"]) or _skill_is_protected(row)


def _local_sources() -> dict[str, Path]:
    from .tools.skills import _skill_roots

    sources: dict[str, Path] = {}
    for root in _skill_roots():
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            md = path / "SKILL.md"
            if path.name.startswith(".") or path.name in sources or not md.is_file():
                continue
            sources[path.name] = md
    return sources


def _plugin_sources(skip: set[str]) -> dict[str, Path]:
    from .skill_audit import _external_skill_index, _preferred_external

    sources: dict[str, Path] = {}
    for name, paths in sorted(_external_skill_index().items()):
        if name in skip:
            continue
        preferred = _preferred_external(paths)
        if preferred is not None:
            sources[name] = preferred
    return sources


def load_catalog(conn: sqlite3.Connection) -> list[CatalogEntry]:
    """Every installed skill, local first, then read-only plugin skills."""
    usage = _usage_rows(conn)
    local = _local_sources()
    entries = [
        CatalogEntry(
            name, _frontmatter_description(path), "local",
            _is_protected(usage.get(name)), int(usage[name]["use_count"]) if name in usage else 0,
        )
        for name, path in local.items()
        if not (name in usage and usage[name]["state"] == "archived")
        and not (name in usage and usage[name]["classification"] == "fixture")
    ]
    entries += [
        CatalogEntry(name, _frontmatter_description(path), "plugin", True, 0)
        for name, path in _plugin_sources(set(local)).items()
    ]
    return entries


def _entry_line(entry: CatalogEntry) -> str:
    tag = " [PLUGIN]" if entry.kind == "plugin" else (" [PROTECTED]" if entry.protected else "")
    description = entry.description[:DESCRIPTION_CHARS]
    if len(entry.description) > DESCRIPTION_CHARS:
        description += "…"
    return f"  - {entry.name}{tag} (uses={entry.uses}): {description}"


def catalog_prompt_block(conn: sqlite3.Connection) -> str:
    """The catalog as a prompt block for every learning-loop writer."""
    entries = load_catalog(conn)
    if not entries:
        return ""
    local = sum(entry.kind == "local" for entry in entries)
    lines = [
        f"SKILL CATALOG — all {len(entries)} installed skills ({local} local, "
        f"{len(entries) - local} plugin). Before any create, check this list: a "
        "new skill must not cover the same task class as one of these. "
        "[PROTECTED] = never patch; [PLUGIN] = read-only, never duplicate.",
    ]
    lines += [_entry_line(entry) for entry in entries]
    return "\n".join(lines) + "\n\n"


def semantic_text(name: str, description: str) -> str:
    """The text compared for duplicates: what a router reads."""
    return f"{name.replace('-', ' ')}: {' '.join((description or '').split())}"


def _vectors(texts: list[str]):
    from .embeddings import _encode

    missing = [text for text in texts if text not in _vector_cache]
    if missing:
        encoded = _encode(missing)
        if encoded is None:
            return None
        _vector_cache.update(zip(missing, encoded))
    return [_vector_cache[text] for text in texts]


def semantic_neighbors(
    entries: list[CatalogEntry], name: str, description: str, *, limit: int = 3,
) -> list[tuple[str, float]] | None:
    """Closest catalog skills by meaning; None when embeddings are off."""
    others = [entry for entry in entries if entry.name != name]
    if not others:
        return []
    texts = [semantic_text(name, description)] + [
        semantic_text(entry.name, entry.description) for entry in others
    ]
    vectors = _vectors(texts)
    if vectors is None:
        return None
    import numpy as np

    scores = np.vstack(vectors[1:]) @ vectors[0]
    scored = [(entry.name, float(score)) for entry, score in zip(others, scores)]
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored[:limit]


def duplicate_pairs(
    entries: list[CatalogEntry], threshold: float,
) -> list[dict] | None:
    """All catalog pairs at or above ``threshold``; None without embeddings."""
    texts = [semantic_text(entry.name, entry.description) for entry in entries]
    vectors = _vectors(texts) if texts else []
    if vectors is None:
        return None
    if len(vectors) < 2:
        return []
    import numpy as np

    matrix = np.vstack(vectors)
    similarity = matrix @ matrix.T
    pairs: list[dict] = []
    for left, right in zip(*np.triu_indices(len(entries), k=1)):
        score = float(similarity[left, right])
        if score >= threshold:
            pairs.append({
                "left": entries[left].name,
                "right": entries[right].name,
                "semantic_cosine": round(score, 4),
            })
    pairs.sort(key=lambda item: (-item["semantic_cosine"], item["left"], item["right"]))
    return pairs
