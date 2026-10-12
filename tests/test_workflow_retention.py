"""Retention for durable Curator and Evolve workflow artifacts."""
from __future__ import annotations

import os
import time
from pathlib import Path


def _configure(monkeypatch, artifacts, *, days: float = 0.01, count: int = 0) -> None:
    monkeypatch.setattr(artifacts, "WORKFLOW_RETENTION_DAYS", days)
    monkeypatch.setattr(artifacts, "WORKFLOW_RETENTION_COUNT", count)


def _terminal_pass(artifacts, conn, reports: Path, pass_id: str, now: int):
    report = reports / f"REPORT-{pass_id}.md"
    report.write_text("complete\nCURATOR_PASS_COMPLETE\n", encoding="utf-8")
    artifacts.register_curator_pass(
        conn, pass_id=pass_id, batch_count=1, created_at=now - 10_000,
    )
    artifacts.register_curator_batch(
        conn,
        pass_id=pass_id,
        batch_index=1,
        batch_total=1,
        batch_text="frozen lesson body for retention",
        report_path=report,
        created_at=now - 10_000,
    )
    artifacts.record_curator_report(
        conn,
        pass_id=pass_id,
        batch_index=1,
        batch_total=1,
        report_path=report,
        report_text="complete\nCURATOR_PASS_COMPLETE\n",
        terminal=True,
        now=now - 10_000,
    )
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES ('test', 'curator_report_applied', ?, '', ?)",
        (str(report), now - 9_000),
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
    report = _terminal_pass(artifacts, conn, reports, "old-pass", now)

    preview = artifacts.workflow_retention_preview(conn)
    assert "dry_run workflow_retention rows=1" in preview
    assert "files=1" in preview

    first = artifacts.run_workflow_retention(conn, now=now)
    assert "compacted=1" in first
    assert not report.exists()
    row = conn.execute(
        "SELECT batch_text, batch_sha256, batch_summary FROM curator_batches "
        "WHERE pass_id='old-pass'"
    ).fetchone()
    assert row["batch_text"] == ""
    assert len(row["batch_sha256"]) == 64
    assert row["batch_summary"]

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
    artifacts.register_curator_pass(
        conn, pass_id="active", batch_count=1, created_at=now - 10_000,
    )
    artifacts.register_curator_batch(
        conn,
        pass_id="active",
        batch_index=1,
        batch_total=1,
        batch_text="active frozen body",
        report_path=reports / "REPORT-active.md",
        created_at=now - 10_000,
    )
    report = reports / "REPORT-unapplied.md"
    report.write_text("CURATOR_PASS_COMPLETE\n", encoding="utf-8")
    artifacts.register_curator_pass(
        conn, pass_id="unapplied", batch_count=1, created_at=now - 10_000,
    )
    artifacts.register_curator_batch(
        conn,
        pass_id="unapplied",
        batch_index=1,
        batch_total=1,
        batch_text="unapplied frozen body",
        report_path=report,
        created_at=now - 10_000,
    )
    artifacts.record_curator_report(
        conn,
        pass_id="unapplied",
        batch_index=1,
        batch_total=1,
        report_path=report,
        report_text="CURATOR_PASS_COMPLETE\n",
        terminal=True,
        now=now - 10_000,
    )

    out = artifacts.run_workflow_retention(conn, now=now)
    assert "rows=0" in out
    assert report.exists()
    assert conn.execute(
        "SELECT batch_text FROM curator_batches WHERE pass_id='active'"
    ).fetchone()[0] == "active frozen body"
    assert conn.execute(
        "SELECT batch_text FROM curator_batches WHERE pass_id='unapplied'"
    ).fetchone()[0] == "unapplied frozen body"


def test_file_failure_is_journaled_and_retry_is_idempotent(fresh_mp, monkeypatch):
    from threadkeeper import workflow_artifacts as artifacts

    conn = fresh_mp["db"].get_db()
    reports = fresh_mp["config"].CURATOR_REPORTS_DIR
    reports.mkdir(parents=True)
    _configure(monkeypatch, artifacts)
    now = int(time.time())
    report = _terminal_pass(artifacts, conn, reports, "retry", now)
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
    report = _terminal_pass(artifacts, conn, reports, "link", now)
    report.unlink()
    os.symlink(outside, report)

    out = artifacts.run_workflow_retention(conn, now=now)
    assert "file_errors=1" in out
    assert outside.read_text(encoding="utf-8") == "must survive"
    stored = conn.execute(
        "SELECT report_path FROM curator_batches WHERE pass_id='link'"
    ).fetchone()[0]
    assert stored == str(report)


def test_terminal_research_digest_compacts_and_then_removes(fresh_mp, monkeypatch):
    from threadkeeper import workflow_artifacts as artifacts

    conn = fresh_mp["db"].get_db()
    _configure(monkeypatch, artifacts)
    now = int(time.time())
    root = fresh_mp["config"].DB_PATH.parent / "evolve-research"
    root.mkdir()
    digest = root / "RESEARCH-old.md"
    digest.write_text("research body with derived memory", encoding="utf-8")
    artifacts.register_research_started(
        conn, research_path=digest, created_at=now - 10_000,
    )
    artifacts.complete_research_handoff(
        conn,
        research_path=digest,
        research_body=digest.read_text(encoding="utf-8"),
        now=now - 10_000,
    )

    artifacts.run_workflow_retention(conn, now=now)
    row = conn.execute(
        "SELECT research_body, research_sha256, research_summary "
        "FROM evolve_research_handoffs"
    ).fetchone()
    assert row["research_body"] == ""
    assert len(row["research_sha256"]) == 64
    assert row["research_summary"]
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
    artifacts.register_curator_pass(
        conn, pass_id="erase", batch_count=1, created_at=now,
    )
    artifacts.register_curator_batch(
        conn,
        pass_id="erase",
        batch_index=1,
        batch_total=1,
        batch_text="frozen source uuid-to-forget body",
        report_path=reports / "REPORT-erase.md",
        created_at=now,
    )
    conn.commit()

    dry = forget_selector("uuid-to-forget", dry_run=True, conn=conn)
    assert "workflow_artifacts=1" in dry
    applied = forget_selector("uuid-to-forget", dry_run=False, conn=conn)
    assert "workflow_artifacts=1" in applied
    row = conn.execute(
        "SELECT batch_text, batch_summary FROM curator_batches WHERE pass_id='erase'"
    ).fetchone()
    assert row["batch_text"] == ""
    assert row["batch_summary"] == "[selectively erased]"
