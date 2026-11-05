"""Explicit managed-skill classification and hidden fixture storage."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def skill_classification(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(
        "SELECT classification FROM skill_usage WHERE name=?", (name,),
    ).fetchone()
    return row[0] if row else "production"


def skill_directory(root: Path, name: str, classification: str) -> Path:
    if classification == "fixture":
        return root.parent / ".threadkeeper-fixtures" / root.name / name
    return root / name
