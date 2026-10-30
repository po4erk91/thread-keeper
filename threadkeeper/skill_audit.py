"""Deterministic input layer for Curator's deep skill audit.

The LLM curator makes semantic and freshness judgements.  This module gives it
an exhaustive, reproducible inventory first: one logical record per skill,
consumer-format checks, mirror integrity, local-link checks, exact duplicates,
and lexical near-duplicate candidates.  It deliberately does not make delete
decisions; those require reading the complete skill and, where relevant,
current external research.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Iterable

import yaml
from .consequential_state import skill_review_categories

from .config import (
    CLAUDE_SKILLS_DIR,
    CURATOR_MANAGE_FOREGROUND_SKILLS,
    DB_PATH,
)


AUDIT_SCHEMA_VERSION = 2
_CODEX_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_URI_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_ATX_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(?:\s|$)")
_SETEXT_HEADING_RE = re.compile(r"^\s*(?:=+|-+)\s*$")
_LIST_OR_QUOTE_RE = re.compile(r"^\s*(?:[-+*]\s+|\d+[.)]\s+|>)")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[a-z]:[\\/]", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-zа-яё0-9][a-zа-яё0-9_-]{2,}", re.IGNORECASE)
_RESOURCE_DIR_NAMES = {"assets", "references", "scripts", "templates"}
MIN_REPEATED_PARAGRAPH_CHARS = 160
MIN_REPEATED_PARAGRAPH_OCCURRENCES = 3
_STOPWORDS = {
    "about", "after", "also", "and", "are", "before", "but", "can",
    "does", "for", "from", "how", "into", "its", "not", "only", "or",
    "should", "skill", "skills", "that", "the", "then", "this", "use",
    "using", "when", "where", "with", "you", "your", "для", "его", "или",
    "как", "когда", "навык", "навыки", "при", "что", "это",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tree_hash(root: Path) -> str | None:
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    files = sorted(p for p in root.rglob("*") if p.is_file())
    for path in files:
        rel = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(rel).to_bytes(4, "big"))
        digest.update(rel)
        try:
            body = path.read_bytes()
        except OSError:
            return None
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()


def _frontmatter(content: str) -> tuple[dict, str, str | None]:
    if not content.startswith("---"):
        return {}, content, "frontmatter must start at byte 0"
    match = re.search(r"\n---\s*\n", content[3:])
    if not match:
        return {}, content, "frontmatter closing delimiter is missing"
    raw = content[3:match.start() + 3]
    body = content[3 + match.end():]
    try:
        fields = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return {}, body, f"frontmatter invalid YAML: {exc}"
    if not isinstance(fields, dict):
        return {}, body, "frontmatter must be a YAML mapping"
    return fields, body, None


def _normalized_body(body: str) -> str:
    # Formatting-only changes should not hide an exact logical duplicate.
    return " ".join(_TOKEN_RE.findall(body.lower()))


def _tokens(record: dict) -> Counter[str]:
    text = f"{record.get('name', '')} {record.get('description', '')} "
    source = record.get("source_path")
    if source:
        try:
            content = Path(source).read_text(encoding="utf-8")
            _fields, body, _error = _frontmatter(content)
            text += body
        except OSError:
            pass
    return Counter(
        token for token in _TOKEN_RE.findall(text.lower())
        if token not in _STOPWORDS
    )


def _candidate_pairs(records: list[dict], limit: int = 200) -> list[dict]:
    usable = [r for r in records if r.get("source_path")]
    counters = {r["name"]: _tokens(r) for r in usable}
    document_frequency: Counter[str] = Counter()
    for counter in counters.values():
        document_frequency.update(counter.keys())
    n_docs = max(1, len(counters))
    vectors: dict[str, dict[str, float]] = {}
    norms: dict[str, float] = {}
    for name, counter in counters.items():
        vector = {
            token: (1.0 + math.log(count))
            * (math.log((1.0 + n_docs) / (1.0 + document_frequency[token])) + 1.0)
            for token, count in counter.items()
        }
        vectors[name] = vector
        norms[name] = math.sqrt(sum(value * value for value in vector.values()))

    pairs: list[dict] = []
    names = sorted(vectors)
    for index, left in enumerate(names):
        left_vec = vectors[left]
        if not left_vec or not norms[left]:
            continue
        for right in names[index + 1:]:
            right_vec = vectors[right]
            if not right_vec or not norms[right]:
                continue
            common = left_vec.keys() & right_vec.keys()
            if len(common) < 2:
                continue
            score = sum(left_vec[t] * right_vec[t] for t in common)
            score /= norms[left] * norms[right]
            if score < 0.22:
                continue
            pairs.append({
                "left": left,
                "right": right,
                "lexical_cosine": round(score, 4),
                "shared_terms": sorted(
                    common,
                    key=lambda token: left_vec[token] * right_vec[token],
                    reverse=True,
                )[:8],
                "status": "candidate_only_requires_semantic_review",
            })
    pairs.sort(key=lambda item: (-item["lexical_cosine"], item["left"], item["right"]))
    return pairs[:limit]


def _scanable_markdown(body: str) -> str:
    """Exclude non-prose Markdown regions from resource and repeat checks."""
    lines = body.splitlines()
    result: list[str] = []
    fence: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        marker = _FENCE_RE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            result.append("")
            index += 1
            continue
        if fence is not None:
            result.append("")
            index += 1
            continue
        if _ATX_HEADING_RE.match(line):
            result.append("")
            index += 1
            continue
        if (
            line.strip()
            and index + 1 < len(lines)
            and _SETEXT_HEADING_RE.match(lines[index + 1])
        ):
            result.extend(("", ""))
            index += 2
            continue
        result.append(line)
        index += 1
    return "\n".join(result)


def _safe_relative_resource(target: str) -> tuple[str, str] | None:
    """Return display and filesystem forms for a non-escaping relative path."""
    display = target.strip().strip("<>").split(maxsplit=1)[0]
    display = display.split("#", 1)[0].split("?", 1)[0]
    if (
        not display
        or display.startswith(("#", "/", "\\", "~"))
        or _URI_SCHEME_RE.match(display)
        or _WINDOWS_ABSOLUTE_RE.match(display)
    ):
        return None
    decoded = display.replace("%20", " ")
    path = Path(decoded)
    if path.is_absolute() or ".." in path.parts:
        return None
    return display, decoded


def _is_path_like_inline_code(target: str) -> bool:
    """Keep inline-code detection conservative enough to avoid commands."""
    if any(char.isspace() for char in target):
        return False
    if not re.fullmatch(r"[A-Za-z0-9._/%+-]+", target):
        return False
    path = _safe_relative_resource(target)
    if path is None:
        return False
    _display, decoded = path
    normalized = decoded.removeprefix("./")
    if "/" not in decoded or normalized.endswith("/"):
        return False
    parts = normalized.split("/")
    return parts[0] in _RESOURCE_DIR_NAMES or "." in parts[-1]


def _resource_exists_within_skill(skill_dir: Path, decoded: str) -> bool | None:
    """Return None when a symlink would lead the audit outside the skill."""
    candidate = skill_dir / decoded
    try:
        candidate.resolve().relative_to(skill_dir.resolve())
    except ValueError:
        return None
    return candidate.exists()


def _relative_resource_findings(skill_dir: Path, body: str) -> list[str]:
    """Find missing local Markdown links and unambiguous inline resources."""
    findings: list[str] = []
    scanable = _scanable_markdown(body)
    markdown = _INLINE_CODE_RE.sub("", scanable)
    for raw_target in _MARKDOWN_LINK_RE.findall(markdown):
        resource = _safe_relative_resource(raw_target)
        if resource is None:
            continue
        display, decoded = resource
        exists = _resource_exists_within_skill(skill_dir, decoded)
        if exists is False:
            findings.append(f"dangling_relative_link:{display}")
    for raw_target in _INLINE_CODE_RE.findall(scanable):
        if not _is_path_like_inline_code(raw_target):
            continue
        resource = _safe_relative_resource(raw_target)
        if resource is None:
            continue
        display, decoded = resource
        exists = _resource_exists_within_skill(skill_dir, decoded)
        if exists is False:
            findings.append(f"dangling_inline_resource:{display}")
    return sorted(set(findings))


def _repeated_paragraph_findings(body: str) -> list[str]:
    """Report substantial prose repeated at least three times in one skill."""
    counts: Counter[str] = Counter()
    for paragraph in re.split(r"\n\s*\n", _scanable_markdown(body)):
        stripped = paragraph.strip()
        if not stripped or _LIST_OR_QUOTE_RE.match(stripped):
            continue
        normalized = " ".join(_TOKEN_RE.findall(stripped.lower()))
        if len(normalized) >= MIN_REPEATED_PARAGRAPH_CHARS:
            counts[normalized] += 1
    return [
        "repeated_normalized_paragraph:"
        f"occurrences={count}:sha256={_sha256(paragraph.encode('utf-8'))[:12]}"
        for paragraph, count in sorted(counts.items())
        if count >= MIN_REPEATED_PARAGRAPH_OCCURRENCES
    ]


def _root_label(root: Path) -> str:
    home = Path.home()
    try:
        return "~/" + root.relative_to(home).as_posix()
    except ValueError:
        return str(root)


def _configured_roots() -> list[Path]:
    # Late import avoids pulling MCP decorators into users of the pure module.
    from .tools.skills import _skill_roots

    return _skill_roots()


def _external_skill_index() -> dict[str, list[Path]]:
    """Index installed system/plugin skills without treating them as mirrors.

    ThreadKeeper telemetry can contain namespaced skills surfaced by a host
    plugin (for example ``superpowers:systematic-debugging``). They are part of
    the audit scope because ThreadKeeper tracks their use, but their source is
    owned by the plugin manager and must remain read-only.
    """
    bases = (
        Path("~/.codex/skills/.system").expanduser(),
        Path("~/.codex/plugins/cache").expanduser(),
        Path("~/.claude/plugins/cache").expanduser(),
        Path("~/.claude/plugins/marketplaces").expanduser(),
    )
    index: dict[str, list[Path]] = defaultdict(list)
    seen: set[Path] = set()
    for base in bases:
        if not base.is_dir():
            continue
        try:
            paths = base.rglob("SKILL.md")
            for path in paths:
                key = path.resolve()
                if key in seen or not path.is_file():
                    continue
                seen.add(key)
                aliases = {path.parent.name}
                try:
                    content = path.read_text(encoding="utf-8")
                    fields, _body, _error = _frontmatter(content)
                    declared = str(fields.get("name") or "").strip()
                    if declared:
                        aliases.add(declared)
                except (OSError, UnicodeError):
                    pass
                for alias in aliases:
                    index[alias].append(path)
        except OSError:
            continue
    return index


def _preferred_external(paths: Iterable[Path]) -> Path | None:
    candidates = list(paths)
    if not candidates:
        return None

    def rank(path: Path) -> tuple[int, float, str]:
        in_cache = 1 if "/cache/" in path.as_posix() else 0
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0.0
        return in_cache, modified, path.as_posix()

    return max(candidates, key=rank)


def _telemetry_rows(conn: sqlite3.Connection, include_archived: bool) -> dict[str, dict]:
    where = "" if include_archived else " WHERE state IN ('active', 'stale')"
    try:
        rows = conn.execute(
            "SELECT name, created_at, created_by_origin, last_used_at, "
            "last_viewed_at, last_patched_at, use_count, view_count, "
            "patch_count, foreground_use_count, pinned, state, tier, "
            "availability, unavailable_since, unavailable_prior_state "
            "FROM skill_usage" + where
            + " ORDER BY name"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row["name"]: {key: row[key] for key in row.keys()} for row in rows}


def _managed_names(telemetry: dict[str, dict]) -> list[str]:
    names = set(telemetry)
    # Historical ThreadKeeper versions could materialize a skill before the
    # telemetry transaction completed.  Include untracked primary/fallback
    # directories, but mark them protected rather than guessing provenance.
    for root in (CLAUDE_SKILLS_DIR, DB_PATH.parent / "skills"):
        if not root.is_dir():
            continue
        for path in root.iterdir():
            if path.is_dir() and not path.name.startswith(".") and (path / "SKILL.md").is_file():
                names.add(path.name)
    return sorted(names)


def _source_for(
    name: str,
    roots: Iterable[Path],
    state: str,
    external_index: dict[str, list[Path]],
) -> tuple[Path | None, str]:
    candidates = [CLAUDE_SKILLS_DIR / name / "SKILL.md"]
    candidates.extend(root / name / "SKILL.md" for root in roots)
    if state == "archived":
        candidates.append(CLAUDE_SKILLS_DIR / ".archive" / name / "SKILL.md")
    seen: set[Path] = set()
    for candidate in candidates:
        key = candidate.expanduser()
        if key in seen:
            continue
        seen.add(key)
        if key.is_file():
            return key, "managed"
    aliases = (name, name.rsplit(":", 1)[-1])
    external_paths: list[Path] = []
    for alias in aliases:
        external_paths.extend(external_index.get(alias, []))
    external = _preferred_external(external_paths)
    if external is not None:
        return external, "external_plugin"
    return None, "missing"


def find_any_skill_source(name: str) -> tuple[Path | None, str]:
    """Find any source path before a registry-only cleanup.

    An active row normally ignores ``.archive`` because it is not its current
    source. Cleanup is stricter: it must preserve the registry whenever any
    managed, archived, or external source still exists.
    """
    return _source_for(
        name,
        _configured_roots(),
        "archived",
        _external_skill_index(),
    )


def source_requires_recovery(record: dict) -> bool:
    """Whether this record has no source a consumer can read."""
    return (
        record.get("source_kind") == "missing"
        or any(
            str(finding).startswith("source_unreadable:")
            for finding in record.get("findings", [])
        )
    )


def _apply_availability_contract(record: dict) -> None:
    """Attach the source-availability and owner-recovery contract."""
    missing_source = source_requires_recovery(record)
    stored = str(record.get("availability") or "available")
    availability = "unavailable" if missing_source else stored
    record["availability"] = availability
    record["compatible"] = bool(
        availability == "available"
        and record.get("validators")
        and all(value == "PASS" for value in record["validators"].values())
    )
    if missing_source:
        record["owner_action"] = "restore_source"
    elif availability == "unavailable":
        # The source is back, but validation must explicitly restore the
        # registry's saved lifecycle state before it is compatible again.
        record["owner_action"] = "validate_restore"
    else:
        record["owner_action"] = None


def reconcile_source_availability(
    conn: sqlite3.Connection,
    records: Iterable[dict],
) -> list[str]:
    """Persist availability transitions and return only changed skill names.

    Availability is separate from active/stale/archive. Remembering the prior
    lifecycle state lets a later validation restore the record without creating
    another repair item every time an unavailable source is checked.
    """
    changed: list[str] = []
    now = int(time.time())
    for record in records:
        name = str(record.get("name") or "")
        if not name:
            continue
        row = conn.execute(
            "SELECT state, availability FROM skill_usage WHERE name=?", (name,),
        ).fetchone()
        if row is None:
            continue
        desired = "unavailable" if source_requires_recovery(record) else "available"
        current = str(row["availability"] or "available")
        if current == desired:
            continue
        if desired == "unavailable":
            conn.execute(
                "UPDATE skill_usage SET availability='unavailable', "
                "unavailable_since=?, unavailable_prior_state=? WHERE name=?",
                (now, row["state"], name),
            )
        else:
            conn.execute(
                "UPDATE skill_usage SET state=CASE "
                "WHEN unavailable_prior_state IN ('active','stale','archived') "
                "THEN unavailable_prior_state ELSE state END, "
                "availability='available', unavailable_since=NULL, "
                "unavailable_prior_state=NULL WHERE name=?",
                (name,),
            )
        changed.append(name)
    if changed:
        conn.commit()
    return changed


def _record_for(
    name: str,
    telemetry: dict | None,
    roots: list[Path],
    external_index: dict[str, list[Path]],
) -> dict:
    from .tools.skills import _validate_skill_md

    telemetry = telemetry or {}
    state = str(telemetry.get("state") or "untracked")
    origin = str(telemetry.get("created_by_origin") or "untracked")
    source, source_kind = _source_for(name, roots, state, external_index)
    protected = bool(
        telemetry.get("pinned")
        or source_kind == "external_plugin"
        or origin in {"untracked", "unknown", ""}
        or (
            origin == "foreground"
            and not CURATOR_MANAGE_FOREGROUND_SKILLS
        )
    )
    record: dict = {
        "name": name,
        "origin": origin,
        "state": state,
        "availability": telemetry.get("availability") or "available",
        "unavailable_since": telemetry.get("unavailable_since"),
        "unavailable_prior_state": telemetry.get("unavailable_prior_state"),
        "tier": telemetry.get("tier") or "unknown",
        "protected": protected,
        "telemetry": {
            key: telemetry.get(key)
            for key in (
                "created_at", "last_used_at", "last_viewed_at",
                "last_patched_at", "use_count", "view_count", "patch_count",
                "foreground_use_count", "pinned",
            )
        },
        "source_path": str(source) if source else None,
        "source_kind": source_kind,
        "description": "",
        "frontmatter_keys": [],
        "content_sha256": None,
        "normalized_body_sha256": None,
        "chars": 0,
        "lines": 0,
        "support_files": [],
        "validators": {},
        "mirrors": [],
        "findings": [],
    }
    if source is None:
        record["findings"].append("source_missing")
        record["validators"] = {
            "threadkeeper": "FAIL:source_missing",
            "claude_code": "FAIL:source_missing",
            "codex": "FAIL:source_missing",
            "agentskills": "FAIL:source_missing",
        }
        _apply_availability_contract(record)
        return record
    try:
        content = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        record["findings"].append(f"source_unreadable:{type(exc).__name__}")
        record["validators"] = {
            "threadkeeper": "FAIL:source_unreadable",
            "claude_code": "FAIL:source_unreadable",
            "codex": "FAIL:source_unreadable",
            "agentskills": "FAIL:source_unreadable",
        }
        _apply_availability_contract(record)
        return record

    fields, body, front_error = _frontmatter(content)
    description = str(fields.get("description") or "")
    fm_name = str(fields.get("name") or "")
    record.update({
        "description": description,
        "frontmatter_keys": sorted(str(key) for key in fields),
        "content_sha256": _sha256(content.encode("utf-8")),
        "normalized_body_sha256": _sha256(_normalized_body(body).encode("utf-8")),
        "chars": len(content),
        "lines": len(content.splitlines()),
    })
    skill_dir = source.parent
    record["findings"].extend(
        f"owner_review_required:{category}"
        for category in skill_review_categories(skill_dir)
    )
    record["support_files"] = sorted(
        path.relative_to(skill_dir).as_posix()
        for path in skill_dir.rglob("*")
        if path.is_file() and path.name != "SKILL.md"
    )
    record["findings"].extend(_relative_resource_findings(skill_dir, body))
    record["findings"].extend(_repeated_paragraph_findings(body))

    expected_name = fm_name or source.parent.name
    tk_error = _validate_skill_md(content, expected_name)
    record["validators"]["threadkeeper"] = "PASS" if not tk_error else f"FAIL:{tk_error}"
    generic_errors: list[str] = []
    if front_error:
        generic_errors.append(front_error)
    if fm_name != source.parent.name:
        generic_errors.append(
            f"frontmatter_name={fm_name!r} directory_name={source.parent.name!r}"
        )
    if not description.strip():
        generic_errors.append("description_missing")
    if not body.strip():
        generic_errors.append("body_empty")
    generic = "PASS" if not generic_errors else "FAIL:" + ";".join(generic_errors)
    record["validators"]["claude_code"] = generic

    agents_errors = list(generic_errors)
    if not _CODEX_NAME_RE.fullmatch(expected_name):
        agents_errors.append("name_requires_lowercase_hyphen_form")
    record["validators"]["agentskills"] = (
        "PASS" if not agents_errors else "FAIL:" + ";".join(agents_errors)
    )
    codex_errors = list(agents_errors)
    if "<" in description or ">" in description:
        codex_errors.append("description_contains_angle_brackets")
    record["validators"]["codex"] = (
        "PASS" if not codex_errors else "FAIL:" + ";".join(codex_errors)
    )
    if any(str(value).startswith("FAIL:") for value in record["validators"].values()):
        record["findings"].append("consumer_validation_failed")

    if source_kind == "external_plugin":
        record["mirrors"] = [{
            "root": _root_label(skill_dir),
            "status": "external_read_only",
        }]
        record["findings"] = sorted(set(record["findings"]))
        _apply_availability_contract(record)
        return record

    canonical_tree_hash = _tree_hash(skill_dir)
    for root in roots:
        mirror_dir = root / name
        if mirror_dir.resolve() == skill_dir.resolve():
            status = "canonical"
        elif not mirror_dir.exists():
            status = "missing"
        else:
            status = "in_sync" if _tree_hash(mirror_dir) == canonical_tree_hash else "drift"
        record["mirrors"].append({"root": _root_label(root), "status": status})
    mirror_states = {item["status"] for item in record["mirrors"]}
    if "drift" in mirror_states:
        record["findings"].append("mirror_drift")
    if state in {"active", "stale", "untracked"}:
        if "missing" in mirror_states:
            record["findings"].append("mirror_missing")
    record["findings"] = sorted(set(record["findings"]))
    _apply_availability_contract(record)
    return record


def build_skill_audit(
    conn: sqlite3.Connection,
    *,
    include_archived: bool = True,
) -> dict:
    """Return the complete deterministic audit manifest as JSON data."""
    telemetry = _telemetry_rows(conn, include_archived)
    roots = _configured_roots()
    external_index = _external_skill_index()
    records = [
        _record_for(name, telemetry.get(name), roots, external_index)
        for name in _managed_names(telemetry)
    ]
    exact_groups: dict[str, list[str]] = defaultdict(list)
    for record in records:
        digest = record.get("normalized_body_sha256")
        if digest:
            exact_groups[digest].append(record["name"])
    exact_duplicates = [
        {"normalized_body_sha256": digest, "skills": sorted(names)}
        for digest, names in sorted(exact_groups.items())
        if len(names) > 1
    ]
    findings = Counter(
        finding for record in records for finding in record.get("findings", [])
    )
    active = sum(r["state"] in {"active", "stale", "untracked"} for r in records)
    manifest = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "scope": "all skills tracked or materialized by thread-keeper",
        "roots": [_root_label(root) for root in roots],
        "summary": {
            "total": len(records),
            "active_or_stale": active,
            "archived": sum(r["state"] == "archived" for r in records),
            "protected": sum(bool(r["protected"]) for r in records),
            "unavailable": sum(r["availability"] == "unavailable" for r in records),
            "compatible": sum(bool(r["compatible"]) for r in records),
            "external_plugin": sum(
                r["source_kind"] == "external_plugin" for r in records
            ),
            "with_findings": sum(bool(r["findings"]) for r in records),
            "finding_counts": dict(sorted(findings.items())),
        },
        "skills": records,
        "exact_duplicate_groups": exact_duplicates,
        "semantic_candidates": _candidate_pairs(records),
        "semantic_warning": (
            "Candidates are lexical pre-screening only. Curator must read every "
            "complete skill, compare intent/outcomes, and use current web research."
        ),
    }
    return manifest


def audit_fingerprint_payload(conn: sqlite3.Connection) -> list[dict]:
    """Small stable projection used by Curator's inventory fingerprint."""
    manifest = build_skill_audit(conn, include_archived=True)
    return [
        {
            "name": record["name"],
            "state": record["state"],
            "availability": record["availability"],
            "compatible": record["compatible"],
            "owner_action": record["owner_action"],
            "source_path": record["source_path"],
            "source_kind": record["source_kind"],
            "content_sha256": record["content_sha256"],
            "mirrors": record["mirrors"],
            "findings": record["findings"],
        }
        for record in manifest["skills"]
    ]


def write_skill_audit_manifest(manifest: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)
    return path


def format_skill_checklist(manifest: dict) -> str:
    """Compact numbered prompt index; the full detail stays in JSON."""
    lines = [
        f"## SKILLS (n={manifest['summary']['total']}) — DEEP AUDIT CHECKLIST",
        "Full deterministic manifest is at AUDIT_MANIFEST_PATH below.",
    ]
    for index, record in enumerate(manifest["skills"], start=1):
        flags = ",".join(record["findings"]) or "clean"
        telemetry = record["telemetry"]
        protected = (
            " [PINNED]" if telemetry.get("pinned")
            else " [PROTECTED]" if record["protected"] else ""
        )
        created_at = int(telemetry.get("created_at") or 0)
        created_age = (
            max(0, int(time.time()) - created_at) // 86400
            if created_at else "?"
        )
        lines.append(
            f"{index}. SKILL {record['name']}{protected} "
            f"state={record['state']} availability={record['availability']} "
            f"owner_action={record['owner_action'] or '-'} "
            f"origin={record['origin']} "
            f"fg_uses={telemetry.get('foreground_use_count') or 0} "
            f"uses={telemetry.get('use_count') or 0} "
            f"views={telemetry.get('view_count') or 0} "
            f"maintenance_patches={telemetry.get('patch_count') or 0} "
            f"created={created_age}d_ago "
            f"source_kind={record['source_kind']} "
            f"source={record['source_path'] or '-'} findings={flags}"
        )
    return "\n".join(lines)
