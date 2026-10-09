"""Stable MCP Skills extension backed by the canonical skill store.

The filesystem mirrors remain the compatibility path for hosts without MCP
Skills support.  This module is a read-only transport view of the canonical
``CLAUDE_SKILLS_DIR``: it never records skill usage and never activates a
skill.  Hosts must still verify a manifest and apply their own approval flow.
"""
from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml
from mcp.server.extension import Extension, MethodBinding
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, RequestParams, Resource

from .config import CLAUDE_SKILLS_DIR


SKILLS_EXTENSION_ID = "io.modelcontextprotocol/skills"
SKILL_URI_ORIGIN = "thread-keeper"
SKILL_LIST_PAGE_SIZE = 50
MAX_SKILL_RESOURCE_BYTES = 1_048_576
MAX_SKILL_RESOURCES = 512
MAX_SKILL_TOTAL_BYTES = 16_777_216
ALLOWED_SKILL_SUBDIRS = frozenset({"references", "templates", "scripts", "assets"})
VALID_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


@dataclass(frozen=True)
class SkillFile:
    """One safe, declared file in a canonical skill."""

    path: Path
    relative_path: str
    uri: str
    size: int
    digest: str
    mime_type: str


@dataclass(frozen=True)
class SkillEntry:
    """The point-in-time manifest returned by the Skills extension."""

    name: str
    uri: str
    frontmatter: dict[str, Any]
    files: tuple[SkillFile, ...]

    def as_protocol_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "frontmatter": self.frontmatter,
            "resources": [
                {"uri": item.uri, "digest": item.digest, "size": item.size}
                for item in self.files
            ],
        }


class ListSkillsParams(RequestParams):
    """Parameters for the stable ``skills/list`` extension method."""

    cursor: str | None = None


class GetSkillParams(RequestParams):
    """Parameters for the stable ``skills/get`` extension method."""

    uri: str


def _invalid_params(message: str) -> MCPError:
    return MCPError(code=INVALID_PARAMS, message=message)


def _skill_uri(name: str, relative_path: str = "SKILL.md") -> str:
    return f"skill://{SKILL_URI_ORIGIN}/{name}/{relative_path}"


def _mime_type(relative_path: str) -> str:
    if relative_path.endswith(".md"):
        return "text/markdown"
    return mimetypes.guess_type(relative_path)[0] or "application/octet-stream"


def _safe_relative_path(value: str) -> tuple[str, str] | None:
    """Return the skill name and permitted relative file path, if exact.

    URI parsing is intentionally strict: no query/fragment, escaping, dot
    segments, or alternate authority can make a filesystem path reachable.
    """
    parsed = urlsplit(value)
    if (
        parsed.scheme != "skill"
        or parsed.netloc != SKILL_URI_ORIGIN
        or parsed.query
        or parsed.fragment
    ):
        return None
    raw_parts = parsed.path.split("/")
    if raw_parts[:1] != [""] or len(raw_parts) < 3:
        return None
    parts = [unquote(part) for part in raw_parts[1:]]
    if any(not part or part in {".", ".."} or "/" in part or "\\" in part for part in parts):
        return None
    name, *path_parts = parts
    if not VALID_SKILL_NAME_RE.fullmatch(name) or len(name) > 64:
        return None
    if not path_parts:
        return None
    relative_path = "/".join(path_parts)
    if relative_path != "SKILL.md" and (
        path_parts[0] not in ALLOWED_SKILL_SUBDIRS or len(path_parts) < 2
    ):
        return None
    if _skill_uri(name, relative_path) != value:
        return None
    return name, relative_path


def _parse_skill_frontmatter(skill_md: Path, name: str) -> dict[str, Any] | None:
    """Return complete valid frontmatter, or leave malformed skills unpublished."""
    try:
        body = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if not VALID_SKILL_NAME_RE.fullmatch(name) or len(name) > 64 or not body.startswith("---"):
        return None
    closing = re.search(r"\n---\s*\n", body[3:])
    if closing is None:
        return None
    try:
        parsed = yaml.safe_load(body[3:closing.start() + 3])
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("name") != name or not isinstance(parsed.get("description"), str):
        return None
    if not body[3 + closing.end():].strip():
        return None
    return parsed


def _is_safe_file(path: Path, root: Path) -> bool:
    """Reject symlinks and anything escaping the canonical skill directory."""
    try:
        if path.is_symlink() or not path.is_file():
            return False
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _skill_files(skill_dir: Path, name: str) -> tuple[SkillFile, ...] | None:
    """Build the complete bounded manifest for one permitted skill tree."""
    candidates = [skill_dir / "SKILL.md"]
    for subdir in sorted(ALLOWED_SKILL_SUBDIRS):
        directory = skill_dir / subdir
        if not directory.is_dir() or directory.is_symlink():
            continue
        candidates.extend(sorted(directory.rglob("*")))

    files: list[SkillFile] = []
    total_size = 0
    for path in candidates:
        if not _is_safe_file(path, skill_dir):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            return None
        size = len(data)
        if size > MAX_SKILL_RESOURCE_BYTES:
            return None
        relative_path = path.relative_to(skill_dir).as_posix()
        files.append(
            SkillFile(
                path=path,
                relative_path=relative_path,
                uri=_skill_uri(name, relative_path),
                size=size,
                digest=f"sha256:{hashlib.sha256(data).hexdigest()}",
                mime_type=_mime_type(relative_path),
            )
        )
        total_size += size
        if len(files) > MAX_SKILL_RESOURCES or total_size > MAX_SKILL_TOTAL_BYTES:
            return None

    files.sort(key=lambda item: item.relative_path)
    if not files or files[0].relative_path != "SKILL.md":
        return None
    return tuple(files)


def skill_catalog() -> tuple[SkillEntry, ...]:
    """List valid canonical skills in deterministic URI order.

    Only the canonical root is published.  Mirrored copies are delivery
    fallbacks for non-supporting hosts and must never become duplicate origins.
    """
    root = CLAUDE_SKILLS_DIR
    try:
        skill_dirs = sorted(path for path in root.iterdir() if path.is_dir() and not path.is_symlink())
    except OSError:
        return ()

    entries: list[SkillEntry] = []
    for skill_dir in skill_dirs:
        name = skill_dir.name
        skill_md = skill_dir / "SKILL.md"
        frontmatter = _parse_skill_frontmatter(skill_md, name)
        if frontmatter is None:
            continue
        files = _skill_files(skill_dir, name)
        if files is None:
            continue
        entries.append(
            SkillEntry(
                name=name,
                uri=_skill_uri(name),
                frontmatter=frontmatter,
                files=files,
            )
        )
    return tuple(sorted(entries, key=lambda entry: entry.uri))


def resource_catalog() -> tuple[Resource, ...]:
    """Render published skill files as ordinary MCP resources."""
    resources: list[Resource] = []
    for entry in skill_catalog():
        for item in entry.files:
            kwargs: dict[str, Any] = {
                "uri": item.uri,
                "name": entry.name if item.relative_path == "SKILL.md" else item.relative_path,
                "mime_type": item.mime_type,
                "size": item.size,
            }
            if item.relative_path == "SKILL.md":
                kwargs.update(
                    title=entry.name,
                    description=entry.frontmatter["description"],
                )
            resources.append(Resource(**kwargs))
    return tuple(resources)


def read_skill_resource(uri: str) -> SkillFile:
    """Resolve only a current, declared, bounded resource from the catalog."""
    parsed = _safe_relative_path(uri)
    if parsed is None:
        raise ResourceNotFoundError(f"Unknown resource: {uri}")
    for entry in skill_catalog():
        for item in entry.files:
            if item.uri == uri:
                return item
    raise ResourceNotFoundError(f"Unknown resource: {uri}")


def _cursor_offset(cursor: str | None, count: int) -> int:
    if cursor is None:
        return 0
    if not cursor.startswith("offset:"):
        raise _invalid_params("Invalid skills/list cursor")
    try:
        offset = int(cursor.removeprefix("offset:"))
    except ValueError as exc:
        raise _invalid_params("Invalid skills/list cursor") from exc
    if offset < 0 or offset >= count:
        raise _invalid_params("Invalid skills/list cursor")
    return offset


async def _list_skills(_ctx: object, params: ListSkillsParams) -> dict[str, Any]:
    catalog = skill_catalog()
    offset = _cursor_offset(params.cursor, len(catalog))
    page = catalog[offset:offset + SKILL_LIST_PAGE_SIZE]
    next_offset = offset + len(page)
    result: dict[str, Any] = {
        "resultType": "complete",
        "skills": [entry.as_protocol_dict() for entry in page],
        "ttlMs": 300_000,
        "cacheScope": "public",
    }
    if next_offset < len(catalog):
        result["nextCursor"] = f"offset:{next_offset}"
    return result


async def _get_skill(_ctx: object, params: GetSkillParams) -> dict[str, Any]:
    parsed = _safe_relative_path(params.uri)
    if parsed is None or parsed[1] != "SKILL.md":
        raise _invalid_params("uri must identify a served skill SKILL.md")
    for entry in skill_catalog():
        if entry.uri == params.uri:
            return {
                "resultType": "complete",
                "skill": entry.as_protocol_dict(),
                "ttlMs": 300_000,
                "cacheScope": "public",
            }
    raise _invalid_params("Unknown skill")


class SkillsExtension(Extension):
    """Stable Skills methods and discovery declaration for MCP 2026-07-28."""

    identifier = SKILLS_EXTENSION_ID

    def methods(self) -> tuple[MethodBinding, ...]:
        stable = frozenset({"2026-07-28"})
        return (
            MethodBinding("skills/list", ListSkillsParams, _list_skills, protocol_versions=stable),
            MethodBinding("skills/get", GetSkillParams, _get_skill, protocol_versions=stable),
        )
