"""Tests for the cross-CLI production verification harness (issue #1).

The verdict logic is pure, so most of this exercises ``evaluate_coverage``
and ``evaluate_verdict`` directly. One test drives the read-only SQL layer
against an in-memory sqlite so the live-DB query path is covered without a
real ~/.threadkeeper store.
"""
from __future__ import annotations

import sqlite3

from threadkeeper.verify_ingest import (
    CANONICAL_SLOTS,
    collect_live_signals,
    evaluate_coverage,
    evaluate_verdict,
    format_report,
    slot_for_source,
)


def test_slot_mapping_excludes_non_ingestible_and_removed_adapters():
    assert slot_for_source("gemini") is None
    assert slot_for_source("antigravity") is None
    assert slot_for_source("claude-code") == "claude-code"
    assert slot_for_source("vscode") is None  # not a canonical slot


def test_coverage_status_verified_thin_absent():
    cov = evaluate_coverage(
        {"claude-code": 200, "codex": 50, "copilot": 2},
        thin_threshold=5,
    )
    assert cov["claude-code"]["status"] == "verified"
    assert cov["codex"]["status"] == "verified"
    assert cov["copilot"]["status"] == "thin"   # 2 rows, below threshold
    # every canonical slot is represented even when no source mapped to it
    assert set(cov) == set(CANONICAL_SLOTS)


def test_verdict_pass_when_all_criteria_met():
    rep = evaluate_verdict(
        source_counts={
            "claude-code": 100, "codex": 100, "copilot": 100, "antigravity": 100,
        },
        window_sources=["claude-code", "codex", "antigravity"],
        shadow_passes=10,
    )
    assert rep["verdict"] == "PASS"
    assert rep["criteria"]["all_sources_present"]["pass"] is True
    assert rep["criteria"]["cross_adapter_window"]["pass"] is True
    assert rep["criteria"]["learning_loop_non_claude"]["pass"] is True


def test_verdict_pass_for_all_ingestible_supported_slots():
    rep = evaluate_verdict(
        source_counts={"claude-code": 200000, "codex": 11000, "copilot": 10},
        window_sources=["claude-code", "codex"],
        shadow_passes=2567,
    )
    assert rep["verdict"] == "PASS"
    assert rep["criteria"]["all_sources_present"]["pass"] is True
    assert rep["criteria"]["all_sources_present"]["verified_slots"] == [
        "claude-code", "codex", "copilot",
    ]
    assert rep["criteria"]["cross_adapter_window"]["pass"] is True
    assert rep["criteria"]["learning_loop_non_claude"]["pass"] is True
    assert "codex" in rep["criteria"]["learning_loop_non_claude"]["sources"]


def test_verdict_fail_single_adapter_only():
    # Only Claude Code has data and the window — not a cross-CLI demonstration.
    rep = evaluate_verdict(
        source_counts={"claude-code": 5000},
        window_sources=["claude-code"],
        shadow_passes=100,
    )
    assert rep["verdict"] == "FAIL"
    assert rep["criteria"]["cross_adapter_window"]["pass"] is False
    assert rep["criteria"]["learning_loop_non_claude"]["pass"] is False


def test_verdict_fail_when_loop_never_ran():
    rep = evaluate_verdict(
        source_counts={"claude-code": 100, "codex": 100},
        window_sources=["claude-code", "codex"],
        shadow_passes=0,  # learning loop has never fired
    )
    # cross-adapter window passes, but loop criterion fails and only 2 slots
    # verified → PARTIAL (loop is one signal, window is the other).
    assert rep["verdict"] == "PARTIAL"
    assert rep["criteria"]["learning_loop_non_claude"]["pass"] is False


def _seed_live_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE dialog_messages (source TEXT, created_at INTEGER)"
    )
    conn.execute("CREATE TABLE events (kind TEXT)")
    rows = [
        ("claude-code", 1_000_000),
        ("claude-code", 1_000_500),
        ("codex", 1_000_600),   # interleaved with claude in the window
        ("copilot", 100),       # ancient — outside the recent window
    ]
    conn.executemany(
        "INSERT INTO dialog_messages (source, created_at) VALUES (?, ?)", rows
    )
    conn.executemany(
        "INSERT INTO events (kind) VALUES (?)",
        [("shadow_review_pass",)] * 3 + [("ingest_pass",)],
    )
    conn.commit()


def test_collect_live_signals_reads_window_and_passes():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_live_db(conn)

    sig = collect_live_signals(conn, window_hours=24)
    assert sig["source_counts"] == {
        "claude-code": 2, "codex": 1, "copilot": 1,
    }
    # newest is 1_000_600; copilot@100 is far outside a 24h window of it.
    assert set(sig["window_sources"]) == {"claude-code", "codex"}
    assert sig["shadow_passes"] == 3
    assert sig["newest_ts"] == 1_000_600


def test_collect_live_signals_tolerates_missing_events_table():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE dialog_messages (source TEXT, created_at INTEGER)")
    conn.execute("INSERT INTO dialog_messages VALUES ('codex', 5)")
    conn.commit()
    sig = collect_live_signals(conn)
    assert sig["shadow_passes"] == 0  # no events table → graceful 0


def test_format_report_renders_verdict_and_slots():
    rep = evaluate_verdict(
        source_counts={"claude-code": 200000, "codex": 11000, "copilot": 10},
        window_sources=["claude-code", "codex"],
        shadow_passes=2567,
    )
    rep["db_path"] = "/tmp/x.sqlite"
    rep["signals"] = {"window_hours": 24}
    text = format_report(rep)
    assert "VERDICT: PASS" in text
    assert "claude-code" in text
    assert "learning_loop_non_claude" in text
