"""Retention for durable Curator and Evolve workflow artifacts."""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path


def _configure(monkeypatch, artifacts, *, days: float = 0.01, count: int = 0) -> None:
    monkeypatch.setattr(artifacts, "WORKFLOW_RETENTION_DAYS", days)
    monkeypatch.setattr(artifacts, "WORKFLOW_RETENTION_COUNT", count)


def _seed_pass(
    conn,
    reports: Path,
    pass_id: str,
    now: int,
    *,
    state: str = "complete",
    applied: str = "applied",
    endorsed: bool = True,
) -> Path:
    report = reports / f"REPORT-{pass_id}.md"
    report.write_text("complete\nCURATOR_PASS_COMPLETE\n", encoding="utf-8")
    terminal_at = now - 10_000
    conn.execute(
        "INSERT INTO curator_passes (pass_id, inventory_fingerprint, "
        "expected_batches, mode, audit_manifest_path, snapshot_path, "
        "created_at, updated_at, completed_at, endorsed_at) "
        "VALUES (?, 'fingerprint', 1, 'advisory', '', NULL, ?, ?, ?, ?)",
        (
            pass_id,
            terminal_at,
            terminal_at,
            terminal_at if endorsed else None,
            terminal_at if endorsed else None,
        ),
    )
    conn.execute(
        "INSERT INTO curator_batches (pass_id, batch_index, report_name, "
        "batch_text, research_state, state, provenance_sha256, apply_state) "
        "VALUES (?, 1, ?, ?, 'pending', ?, 'digest', ?)",
        (
            pass_id,
            report.name,
            "frozen lesson body for retention",
            state,
            applied,
        ),
    )
    conn.commit()
    return report


def test_terminal_artifacts_compact_then_remove_on_retry(fresh_mp, monkeypatch):
    from threadkeeper import workflow_artifacts as artifacts

    conn = fresh_mp["db"].get_db()
    reports = fresh_mp["config"].CURATOR_REPORTS_DIR
    reports.mkdir(parents=True)
    _configure(monkeypatch, artifacts)
    now = int(time.time())
    report = _seed_pass(conn, reports, "old-pass", now)

    preview = artifacts.workflow_retention_preview(conn)
    assert "dry_run workflow_retention rows=1" in preview
    assert "files=1" in preview

    first = artifacts.run_workflow_retention(conn, now=now)
    assert "compacted=1" in first
    assert not report.exists()
    row = conn.execute(
        "SELECT batch_text, workflow_batch_sha256, workflow_batch_summary "
        "FROM curator_batches WHERE pass_id='old-pass'"
    ).fetchone()
    assert row["batch_text"] == ""
    assert len(row["workflow_batch_sha256"]) == 64
    assert row["workflow_batch_summary"]

    second = artifacts.run_workflow_retention(conn, now=now + 1)
    assert "deleted_rows=1" in second
    assert conn.execute("SELECT COUNT(*) FROM curator_passes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM curator_batches").fetchone()[0] == 0


def test_active_and_unapplied_reports_are_never_candidates(fresh_mp, monkeypatch):
    from threadkeeper import workflow_artifacts as artifacts

    conn = fresh_mp["db"].get_db()
    reports = fresh_mp["config"].CURATOR_REPORTS_DIR
    reports.mkdir(parents=True)
    _configure(monkeypatch, artifacts)
    now = int(time.time())
    _seed_pass(
        conn,
        reports,
        "active",
        now,
        state="pending",
        applied="unapplied",
        endorsed=False,
    )
    report = _seed_pass(conn, reports, "unapplied", now, applied="unapplied")

    out = artifacts.run_workflow_retention(conn, now=now)
    assert "rows=0" in out
    assert report.exists()
    assert conn.execute(
        "SELECT batch_text FROM curator_batches WHERE pass_id='active'"
    ).fetchone()[0] == "frozen lesson body for retention"
    assert conn.execute(
        "SELECT batch_text FROM curator_batches WHERE pass_id='unapplied'"
    ).fetchone()[0] == "frozen lesson body for retention"


def test_file_failure_is_journaled_and_retry_is_idempotent(fresh_mp, monkeypatch):
    from threadkeeper import workflow_artifacts as artifacts

    conn = fresh_mp["db"].get_db()
    reports = fresh_mp["config"].CURATOR_REPORTS_DIR
    reports.mkdir(parents=True)
    _configure(monkeypatch, artifacts)
    now = int(time.time())
    report = _seed_pass(conn, reports, "retry", now)
    real_unlink = artifacts.os.unlink

    def fail_report_unlink(path):
        if Path(path) == report:
            raise OSError("test deletion failure")
        return real_unlink(path)

    monkeypatch.setattr(artifacts.os, "unlink", fail_report_unlink)
    first = artifacts.run_workflow_retention(conn, now=now)
    assert "file_errors=1" in first
    assert report.exists()
    journal = conn.execute(
        "SELECT state FROM workflow_artifact_deletions WHERE path=?",
        (str(report),),
    ).fetchone()
    assert journal["state"] == "failed"

    monkeypatch.setattr(artifacts.os, "unlink", real_unlink)
    second = artifacts.run_workflow_retention(conn, now=now + 1)
    assert "deleted_rows=1" in second
    assert not report.exists()
    assert conn.execute("SELECT COUNT(*) FROM curator_passes").fetchone()[0] == 0


def test_refuses_symlinked_workflow_file(fresh_mp, monkeypatch, tmp_path):
    from threadkeeper import workflow_artifacts as artifacts

    conn = fresh_mp["db"].get_db()
    reports = fresh_mp["config"].CURATOR_REPORTS_DIR
    reports.mkdir(parents=True)
    _configure(monkeypatch, artifacts)
    now = int(time.time())
    outside = tmp_path / "outside-report.md"
    outside.write_text("must survive", encoding="utf-8")
    report = _seed_pass(conn, reports, "link", now)
    report.unlink()
    os.symlink(outside, report)

    out = artifacts.run_workflow_retention(conn, now=now)
    assert "file_errors=1" in out
    assert outside.read_text(encoding="utf-8") == "must survive"
    assert conn.execute(
        "SELECT report_name FROM curator_batches WHERE pass_id='link'"
    ).fetchone()[0] == report.name


def test_terminal_research_digest_compacts_and_then_removes(fresh_mp, monkeypatch):
    from threadkeeper import workflow_artifacts as artifacts

    conn = fresh_mp["db"].get_db()
    _configure(monkeypatch, artifacts)
    now = int(time.time())
    root = fresh_mp["config"].DB_PATH.parent / "evolve-research"
    root.mkdir()
    digest = root / "RESEARCH-old.md"
    body = "research body with derived memory"
    digest.write_text(body, encoding="utf-8")
    target = str(digest.resolve())
    conn.execute(
        "INSERT INTO evolve_research_handoffs "
        "(pass_id, target_path, owner_cid, authorized_at, expires_at, status, "
        "content_sha256, content_chars, completed_at) "
        "VALUES ('old-research', ?, 'owner', ?, ?, 'accepted', ?, ?, ?)",
        (
            target,
            now - 10_000,
            now + 10_000,
            hashlib.sha256(body.encode("utf-8")).hexdigest(),
            len(body),
            now - 10_000,
        ),
    )
    conn.commit()
    artifacts.record_research_consumed(
        conn,
        research_path=digest,
        research_body=body,
        now=now - 10_000,
    )

    artifacts.run_workflow_retention(conn, now=now)
    row = conn.execute(
        "SELECT retention_body, content_sha256, retention_summary "
        "FROM evolve_research_handoffs"
    ).fetchone()
    assert row["retention_body"] == ""
    assert len(row["content_sha256"]) == 64
    assert row["retention_summary"]
    assert not digest.exists()

    artifacts.run_workflow_retention(conn, now=now + 1)
    assert conn.execute(
        "SELECT COUNT(*) FROM evolve_research_handoffs"
    ).fetchone()[0] == 0


def test_selective_erasure_compacts_matching_frozen_workflow_body(fresh_mp):
    from threadkeeper import workflow_artifacts as artifacts
    from threadkeeper.forget import forget_selector

    conn = fresh_mp["db"].get_db()
    reports = fresh_mp["config"].CURATOR_REPORTS_DIR
    reports.mkdir(parents=True)
    now = int(time.time())
    _seed_pass(
        conn,
        reports,
        "erase",
        now,
        state="pending",
        applied="unapplied",
        endorsed=False,
    )
    conn.execute(
        "UPDATE curator_batches SET batch_text=? WHERE pass_id='erase'",
        ("frozen source uuid-to-forget body",),
    )
    conn.commit()

    dry = forget_selector("uuid-to-forget", dry_run=True, conn=conn)
    assert "workflow_artifacts=1" in dry
    applied = forget_selector("uuid-to-forget", dry_run=False, conn=conn)
    assert "workflow_artifacts=1" in applied
    row = conn.execute(
        "SELECT batch_text, workflow_batch_summary FROM curator_batches "
        "WHERE pass_id='erase'"
    ).fetchone()
    assert row["batch_text"] == ""
    assert row["workflow_batch_summary"] == "[selectively erased]"
