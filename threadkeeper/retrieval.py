"""Unified candidate generation and rank fusion for notes and dialogs.

MCP tools format strings; this module owns retrieval semantics so ``search``,
``dialog_search`` and query-aware ``brief`` cannot drift into different
semantic-vs-lexical fallback behavior.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import sqlite3
from typing import Literal

from . import config
from .embeddings import _cosine_search, _dialog_cosine_search, _fts_search
from .helpers import _fts_query

# Normalized MiniLM cosine below this level is generally nearest-neighbour
# noise. Apply the threshold to raw dense evidence (never to RRF scores), so a
# query with no lexical match can abstain instead of returning an arbitrary row.
DENSE_MIN_SCORE = 0.45


@dataclass
class Candidate:
    id: str
    source: Literal["note", "dialog"]
    content: str
    created_at: int
    thread_id: str | None = None
    kind: str | None = None
    session_id: str | None = None
    role: str | None = None
    project: str | None = None
    dense_score: float | None = None
    lexical_score: float | None = None
    fused_score: float | None = None
    matched_by: set[str] = field(default_factory=set)

    @property
    def display_score(self) -> float | None:
        """Keep the familiar cosine score when present; otherwise show fusion."""
        return self.dense_score if self.dense_score is not None else self.fused_score


def _notes_fts_search(conn: sqlite3.Connection, query: str,
                      k: int) -> list[Candidate]:
    fq = _fts_query(query)
    if not fq:
        return []
    def _run(match_query: str):
        return conn.execute(
            "SELECT n.id, n.thread_id, n.kind, n.content, n.created_at, "
            "       bm25(notes_fts) AS lexical_score "
            # notes_fts is external-content keyed on notes.rowid (content_rowid);
            # join on rowid, NOT n.id — post-migration n.id is a TEXT ULID while
            # the FTS rowid stays the integer notes.rowid.
            "FROM notes_fts f JOIN notes n ON n.rowid=f.rowid "
            "WHERE notes_fts MATCH ? "
            "ORDER BY lexical_score, n.id DESC LIMIT ?",
            (match_query, max(1, int(k))),
        ).fetchall()

    try:
        rows = _run(fq)
        if not rows and " " in fq:
            rows = _run(fq.replace(" ", " OR "))
    except sqlite3.OperationalError:
        return []
    return [
        Candidate(
            id=str(row["id"]),
            source="note",
            content=row["content"],
            created_at=row["created_at"],
            thread_id=row["thread_id"],
            kind=row["kind"],
            lexical_score=float(row["lexical_score"]),
            matched_by={"fts"},
        )
        for row in rows
    ]


def _notes_dense_search(conn: sqlite3.Connection, query: str,
                        k: int) -> list[Candidate]:
    if not config.SEMANTIC_AVAILABLE:
        return []
    return [
        Candidate(
            id=str(row["id"]),
            source="note",
            content=row["content"],
            created_at=row["created_at"],
            thread_id=row["thread_id"],
            kind=row["kind"],
            dense_score=float(row["score"]),
            matched_by={"dense"},
        )
        for row in _cosine_search(conn, query, k)
        if float(row["score"]) >= DENSE_MIN_SCORE
    ]


def _dialogs_fts_search(conn: sqlite3.Connection, query: str, k: int,
                        role: str = "") -> list[Candidate]:
    rows = _fts_search(conn, query, k, role=role)
    return [
        Candidate(
            id=str(row["uuid"]),
            source="dialog",
            content=row["content"],
            created_at=row["created_at"],
            session_id=row.get("session_id"),
            role=row.get("role"),
            project=row.get("project"),
            lexical_score=(float(row["lexical_score"])
                           if row.get("lexical_score") is not None else None),
            matched_by={"fts"},
        )
        for row in rows
    ]


def _dialogs_dense_search(conn: sqlite3.Connection, query: str, k: int,
                          role: str = "") -> list[Candidate]:
    if not config.SEMANTIC_AVAILABLE:
        return []
    rows = _dialog_cosine_search(conn, query, k, role=role)
    return [
        Candidate(
            id=str(row["uuid"]),
            source="dialog",
            content=row["content"],
            created_at=row["created_at"],
            session_id=row.get("session_id"),
            role=row.get("role"),
            project=row.get("project"),
            dense_score=float(row["score"]),
            matched_by={"dense"},
        )
        for row in rows
        if float(row["score"]) >= DENSE_MIN_SCORE
    ]


def _fuse(lists: list[list[Candidate]], *, top_n: int,
          k_rrf: int = 60) -> list[Candidate]:
    """Reciprocal-rank fuse while preserving per-channel scores/provenance."""
    scores: dict[tuple[str, str], float] = {}
    merged: dict[tuple[str, str], Candidate] = {}
    for candidates in lists:
        for rank, candidate in enumerate(candidates, start=1):
            key = (candidate.source, candidate.id)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k_rrf + rank)
            existing = merged.get(key)
            if existing is None:
                merged[key] = candidate
                continue
            existing.matched_by.update(candidate.matched_by)
            if candidate.dense_score is not None:
                existing.dense_score = candidate.dense_score
            if candidate.lexical_score is not None:
                existing.lexical_score = candidate.lexical_score
    ranked = sorted(scores, key=lambda key: (-scores[key], key[1]))[:top_n]
    out: list[Candidate] = []
    for key in ranked:
        candidate = merged[key]
        candidate.fused_score = scores[key]
        out.append(candidate)
    return out


def retrieve_notes(conn: sqlite3.Connection, query: str, k: int = 5,
                   mode: str = "hybrid") -> list[Candidate]:
    mode = mode.strip().lower()
    if mode not in {"hybrid", "semantic", "fts"}:
        raise ValueError(f"unsupported retrieval mode: {mode}")
    over_fetch = max(max(1, int(k)) * 8, 40)
    dense = (
        _notes_dense_search(conn, query, over_fetch)
        if mode in {"hybrid", "semantic"} else []
    )
    lexical = (
        _notes_fts_search(conn, query, over_fetch)
        if mode in {"hybrid", "fts"} else []
    )
    if mode == "semantic":
        return dense[:k]
    if mode == "fts":
        return lexical[:k]
    return _fuse([dense, lexical], top_n=k)


def retrieve_dialogs(conn: sqlite3.Connection, query: str, k: int = 5,
                     role: str = "", mode: str = "hybrid") -> list[Candidate]:
    mode = mode.strip().lower()
    if mode not in {"hybrid", "semantic", "fts"}:
        raise ValueError(f"unsupported retrieval mode: {mode}")
    role = role.strip().lower()
    over_fetch = max(max(1, int(k)) * 8, 40)
    dense = (
        _dialogs_dense_search(conn, query, over_fetch, role=role)
        if mode in {"hybrid", "semantic"} else []
    )
    lexical = (
        _dialogs_fts_search(conn, query, over_fetch, role=role)
        if mode in {"hybrid", "fts"} else []
    )
    if mode == "semantic":
        return dense[:k]
    if mode == "fts":
        return lexical[:k]
    return _fuse([dense, lexical], top_n=k)
