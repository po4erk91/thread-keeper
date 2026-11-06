"""Versioned Curator evidence without copying lesson text or source paths."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import heapq
import json
import re
import unicodedata

from . import lessons
from .lesson_freshness import lesson_revision
from .skill_audit import build_batch_skill_audit_slice


CURATOR_AUDIT_SCHEMA_VERSION = 3
WIKILINK_RE = re.compile(r"\[\[([A-Za-z0-9][A-Za-z0-9_.:-]*)(?:\|[^\]]+)?\]\]")
REPORT_AUDIT_PREFIX = "<!-- CURATOR_AUDIT "
_KNOWN_ORIGINS = lessons.CURATABLE_LESSON_ORIGINS | lessons.FOREGROUND_LESSON_ORIGINS
_KNOWN_SOURCES = {"shadow", "curator", "evolve", "foreground", "user"}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalized_body(body: str) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", body).casefold()))


def _number(value) -> int:
    return int(value or 0)


def _lesson_record(item: dict) -> dict:
    body = item.get("body") or ""
    usage = item.get("usage") or {}
    source = item.get("source") or ""
    usage_source = usage.get("source") or ""
    origin = item.get("origin") or ""
    protected, reason = lessons.lesson_protection(item, usage)
    return {
        "slug": item["slug"],
        "body_sha256": _digest(body),
        "normalized_body_sha256": _digest(_normalized_body(body)),
        "revision": lesson_revision(item),
        "timestamp": _number(item.get("ts")),
        "provenance": {
            "origin": origin if origin in _KNOWN_ORIGINS else "unknown",
            "origin_sha256": _digest(origin),
            "source_kind": source if source in _KNOWN_SOURCES else "redacted",
            "source_sha256": _digest(source),
            "usage_source_sha256": _digest(usage_source),
        },
        "protected": protected,
        "protection_reason": "unknown_origin" if reason.startswith("unknown_origin") else reason,
        "tier": usage.get("tier") if usage.get("tier") in {"observed", "validated"} else "hypothesis",
        "usage": {
            key: _number(usage.get(key))
            for key in ("created_at", "last_used_at", "last_viewed_at", "use_count", "view_count", "pinned")
        },
        "outgoing_links": sorted(set(WIKILINK_RE.findall(body))),
    }


def _duplicate_groups(records: list[dict], key: str) -> list[dict]:
    groups: dict[str, list[str]] = defaultdict(list)
    for record in records:
        groups[record[key]].append(record["slug"])
    return [
        {key: digest, "lessons": sorted(slugs)}
        for digest, slugs in sorted(groups.items()) if len(slugs) > 1
    ]


def _near_duplicate_pairs(items: list[dict]):
    tokens = {item["slug"]: set(_normalized_body(item["body"]).split()) for item in items}
    slugs = sorted(tokens)
    for index, left in enumerate(slugs):
        for right in slugs[index + 1:]:
            common = tokens[left] & tokens[right]
            union = tokens[left] | tokens[right]
            score = len(common) / len(union) if union else 0
            if len(common) < 3 or score < 0.8:
                continue
            yield {
                "left": left, "right": right, "token_jaccard": round(score, 4),
                "status": "candidate_only_requires_semantic_review",
            }


def _near_duplicates(items: list[dict], limit: int = 200) -> list[dict]:
    return heapq.nsmallest(
        limit, _near_duplicate_pairs(items),
        key=lambda pair: (-pair["token_jaccard"], pair["left"], pair["right"]),
    )


def build_curator_audit(skill_audit: dict, items: list[dict]) -> dict:
    """Extend skill evidence using exactly the lessons in the frozen inventory."""
    records = sorted((_lesson_record(item) for item in items), key=lambda row: row["slug"])
    slugs = {record["slug"] for record in records}
    resolved = slugs | {record["name"] for record in skill_audit["skills"]}
    adjacency: dict[str, set[str]] = {slug: set() for slug in slugs}
    for record in records:
        slug = record["slug"]
        for target in set(record["outgoing_links"]) & (slugs - {slug}):
            adjacency[slug].add(target)
            adjacency[target].add(slug)
        record["unresolved_links"] = sorted(set(record["outgoing_links"]) - resolved)
    for record in records:
        record["adjacent_lessons"] = sorted(adjacency[record["slug"]])
    return {
        **skill_audit,
        "schema_version": CURATOR_AUDIT_SCHEMA_VERSION,
        "lesson_audit": {
            "normalization": "unicode_nfkc_casefold_word_tokens_v1",
            "near_duplicate_policy": {"metric": "token_jaccard", "threshold": 0.8, "limit": 200},
            "lessons": records,
            "exact_duplicate_groups": _duplicate_groups(records, "body_sha256"),
            "normalized_duplicate_groups": _duplicate_groups(records, "normalized_body_sha256"),
            "near_duplicate_candidates": _near_duplicates(items),
        },
    }


def validate_manifest_version(manifest: dict) -> None:
    version = manifest.get("schema_version")
    if type(version) is not int or version not in {1, 2, CURATOR_AUDIT_SCHEMA_VERSION}:
        raise ValueError(f"unsupported_curator_audit_version={version!r}; supported=1,2,3")
    if version == CURATOR_AUDIT_SCHEMA_VERSION and not isinstance(manifest.get("lesson_audit"), dict):
        raise ValueError("lesson_audit_missing")


def batch_manifest_binding(schema_version: int, digest: str) -> str:
    return (
        f"\nAUDIT_MANIFEST_SCHEMA_VERSION = {schema_version}\n"
        f"AUDIT_MANIFEST_SHA256 = {digest}\n"
    )


def batch_lesson_slugs(batch_text: str) -> list[str]:
    """Recover lesson scope from frozen lesson lines and promotion sources."""
    slugs = set(re.findall(r"^- LESSON ([A-Za-z0-9_.:-]+)", batch_text, re.MULTILINE))
    for group in re.findall(r"^    lessons: ([^\n]+)", batch_text, re.MULTILINE):
        slugs.update(slug.strip() for slug in group.split(",") if slug.strip())
    return sorted(slugs)


def build_batch_audit_slice(manifest: dict, *, assigned_lesson_slugs=(), **kwargs) -> dict:
    validate_manifest_version(manifest)
    result = build_batch_skill_audit_slice(manifest, **kwargs)
    if manifest["schema_version"] < CURATOR_AUDIT_SCHEMA_VERSION:
        return result
    slugs = sorted(set(assigned_lesson_slugs))
    scope = set(slugs)
    audit = manifest["lesson_audit"]
    result.update({"schema_version": 2, "assigned_lesson_slugs": slugs})
    result["lesson_audit"] = {
        "normalization": audit["normalization"],
        "near_duplicate_policy": audit["near_duplicate_policy"],
        "lessons": [record for record in audit["lessons"] if record["slug"] in scope],
        **{
            key: [group for group in audit[key] if scope.intersection(group["lessons"])]
            for key in ("exact_duplicate_groups", "normalized_duplicate_groups")
        },
        "near_duplicate_candidates": [
            pair for pair in audit["near_duplicate_candidates"]
            if {pair["left"], pair["right"]} & scope
        ],
    }
    if {record["slug"] for record in result["lesson_audit"]["lessons"]} != scope:
        raise ValueError("lesson_audit_scope_missing")
    return result


def audit_receipt(slice_manifest: dict, current_items: list[dict]) -> dict:
    """Bind reviewed revisions and observed changes to the verified manifest."""
    current = {item["slug"]: lesson_revision(item) for item in current_items}
    records = slice_manifest["lesson_audit"]["lessons"]
    reviewed = {record["slug"]: record["revision"] for record in records}
    return {
        **slice_manifest["canonical_manifest"],
        "reviewed_revisions": reviewed,
        "lesson_revision_changes": [
            {"slug": slug, "reviewed_revision": revision, "current_revision": current.get(slug)}
            for slug, revision in sorted(reviewed.items()) if current.get(slug) != revision
        ],
    }


def report_audit_header(receipt: dict) -> str:
    return REPORT_AUDIT_PREFIX + json.dumps(receipt, sort_keys=True, separators=(",", ":")) + " -->\n"


def report_matches_audit(report: str, slice_manifest: dict) -> bool:
    if slice_manifest["canonical_manifest"]["schema_version"] < CURATOR_AUDIT_SCHEMA_VERSION:
        return True
    first = report.splitlines()[0] if report else ""
    if not first.startswith(REPORT_AUDIT_PREFIX) or not first.endswith(" -->"):
        return False
    try:
        receipt = json.loads(first[len(REPORT_AUDIT_PREFIX):-4])
    except ValueError:
        return False
    if not isinstance(receipt, dict):
        return False
    reviewed = {record["slug"]: record["revision"] for record in slice_manifest["lesson_audit"]["lessons"]}
    return (
        all(receipt.get(key) == value for key, value in slice_manifest["canonical_manifest"].items())
        and receipt.get("reviewed_revisions") == reviewed
    )
