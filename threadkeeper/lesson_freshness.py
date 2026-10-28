"""Source-linked factual checks, separate from protected lesson content."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from urllib.parse import urlsplit


VERIFICATION_STATES = {"verified", "contradicted", "unsubstantiated"}


def lesson_revision(item: dict) -> str:
    """Content-address the lesson and its creator metadata, not its usage."""
    content = {key: item[key] for key in ("slug", "body", "ts", "source", "origin")}
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def claim_fingerprint(claim: str) -> str:
    return hashlib.sha256(" ".join(claim.split()).encode("utf-8")).hexdigest()


def checked_source_revision(source_revision: str, source_text: str) -> str:
    """Accept an official revision token or hash the supplied source text."""
    if bool(source_revision.strip()) == bool(source_text.strip()):
        raise ValueError("pass exactly one of source_revision or source_text")
    if source_text.strip():
        return hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    return source_revision.strip()


def _validate_source(source_url: str, source_revision: str) -> None:
    try:
        parsed = urlsplit(source_url)
        valid = (
            parsed.scheme == "https" and parsed.hostname
            and not parsed.username and not parsed.password
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid or any(char.isspace() for char in source_url):
        raise ValueError("source_url must be an official HTTPS page without credentials or fragment")
    if not source_revision.strip():
        raise ValueError("source_revision must identify the checked official source")


def record_verification(
    conn: sqlite3.Connection,
    item: dict,
    expected_revision: str,
    claim: str,
    status: str,
    source_url: str,
    source_revision: str,
) -> dict:
    """Upsert one claim's audit; observing a changed source expires its peers."""
    revision = lesson_revision(item)
    if expected_revision != revision:
        raise ValueError("lesson_revision_changed; read lesson_verifications and recheck the claim")
    claim = claim.strip()
    if not claim or claim not in item["body"]:
        raise ValueError("claim must be an exact nonempty excerpt from the current lesson")
    if status not in VERIFICATION_STATES:
        raise ValueError("status must be verified, contradicted, or unsubstantiated")
    _validate_source(source_url, source_revision)
    checked_at = int(time.time())
    fingerprint = claim_fingerprint(claim)
    conn.execute(
        "INSERT INTO lesson_verification_sources (source_url, revision, generation) "
        "VALUES (?, ?, 1) ON CONFLICT(source_url) DO UPDATE SET "
        "generation=generation + (revision != excluded.revision), revision=excluded.revision",
        (source_url, source_revision),
    )
    generation = conn.execute(
        "SELECT generation FROM lesson_verification_sources WHERE source_url=?",
        (source_url,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO lesson_claim_verifications "
        "(slug, lesson_revision, claim_fingerprint, claim, status, source_url, "
        "source_revision, source_generation, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(slug, claim_fingerprint) DO UPDATE SET "
        "lesson_revision=excluded.lesson_revision, claim=excluded.claim, "
        "status=excluded.status, source_url=excluded.source_url, "
        "source_revision=excluded.source_revision, source_generation=excluded.source_generation, "
        "checked_at=excluded.checked_at",
        (item["slug"], revision, fingerprint, claim, status, source_url,
         source_revision, generation, checked_at),
    )
    return {"claim_fingerprint": fingerprint, "checked_at": checked_at}


def verifications_for_lesson(conn: sqlite3.Connection, item: dict) -> list[dict]:
    revision = lesson_revision(item)
    rows = conn.execute(
        "SELECT v.*, s.revision AS current_source_revision, "
        "s.generation AS current_source_generation FROM lesson_claim_verifications v "
        "LEFT JOIN lesson_verification_sources s ON s.source_url=v.source_url "
        "WHERE v.slug=? ORDER BY v.claim_fingerprint", (item["slug"],),
    ).fetchall()
    results = []
    for row in rows:
        result = dict(row)
        source_revision = result.pop("current_source_revision")
        source_generation = result.pop("current_source_generation")
        invalidated = ""
        if result["lesson_revision"] != revision:
            invalidated = "lesson_revision_changed"
        elif (result["source_revision"] != source_revision
              or result["source_generation"] != source_generation):
            invalidated = "source_revision_changed"
        result["invalidated"] = invalidated
        results.append(result)
    return results


def verification_warnings(conn: sqlite3.Connection, item: dict) -> list[str]:
    warnings = []
    current_text = " ".join(item["body"].split())
    for result in verifications_for_lesson(conn, item):
        status = result["status"]
        invalidated = result["invalidated"]
        claim_text = " ".join(result["claim"].split())
        if invalidated == "lesson_revision_changed" and claim_text not in current_text:
            continue
        if status == "verified" and not invalidated:
            continue
        state = f"needs_recheck ({invalidated}; previous_status={status})" if invalidated else status
        warnings.append(
            f"WARNING factual freshness: {state}; claim={json.dumps(result['claim'], ensure_ascii=False)}; "
            f"source={result['source_url']}; checked_at={result['checked_at']}; "
            f"claim_fingerprint={result['claim_fingerprint']}"
        )
    return warnings
