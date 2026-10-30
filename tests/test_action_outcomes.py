"""Offline action/outcome memory stays bounded, deletable, and non-live."""
from __future__ import annotations

import json
import time

import pytest


def _record_pair(conn, action_outcomes, *, objective="objective-42", source_id="dialog-42"):
    failed = action_outcomes.record_evaluation_event(
        conn,
        event_id="ao-failed",
        objective_ref=objective,
        action_name="github.pr.create",
        outcome="failure",
        error_fingerprint="http.422.validation",
        sequence=1,
        source_kind="dialog",
        source_id=source_id,
        authority="observed",
        principal="fixture-runner",
        channel="offline-evaluation",
        created_at=100,
    )
    recovered = action_outcomes.record_evaluation_event(
        conn,
        event_id="ao-recovered",
        objective_ref=objective,
        action_name="github.pr.create",
        outcome="success",
        sequence=2,
        recovered_from=failed,
        source_kind="dialog",
        source_id=source_id,
        authority="observed",
        principal="fixture-runner",
        channel="offline-evaluation",
        created_at=101,
    )
    return failed, recovered


def test_schema_and_validation_exclude_raw_payloads(fresh_mp):
    from threadkeeper import action_outcomes

    conn = fresh_mp["db"].get_db()
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(action_outcome_events)")
    }
    assert columns == {
        "id", "objective_ref", "action_name", "outcome", "error_fingerprint",
        "recovered_from", "sequence", "source_kind", "source_id",
        "authority_class", "provenance_version", "created_at", "expires_at",
    }

    with pytest.raises(ValueError, match="bounded identifier"):
        action_outcomes.record_evaluation_event(
            conn,
            event_id="ao-secret",
            objective_ref="objective-1",
            action_name="github.pr.create --body raw-output",
            outcome="success",
            sequence=1,
            source_kind="dialog",
            source_id="dialog-1",
            authority="observed",
            principal="fixture-runner",
            channel="offline-evaluation",
        )
    with pytest.raises(RuntimeError, match="offline evaluation"):
        action_outcomes.record_live_event()


def test_recovery_order_and_authorized_candidate_are_bounded(fresh_mp):
    from threadkeeper import action_outcomes

    conn = fresh_mp["db"].get_db()
    failed, recovered = _record_pair(conn, action_outcomes)
    candidate_id = action_outcomes.derive_recovery_candidate(conn, recovered)

    row = conn.execute(
        "SELECT content, rationale, source_uuid FROM extract_candidates WHERE id=?",
        (candidate_id,),
    ).fetchone()
    assert row["rationale"] == "operational_recovery"
    assert row["source_uuid"] == f"action-outcome:{recovered}"
    assert row["content"] == (
        "Operational recovery: retry github.pr.create after "
        "http.422.validation."
    )
    assert action_outcomes.candidate_source(conn, candidate_id) == (
        "action_outcome_candidate", str(candidate_id)
    )
    exported = action_outcomes.export_events(conn)
    assert [row["id"] for row in exported] == [failed, recovered]
    assert set(exported[0]) == {
        "id", "objective_ref", "action_name", "outcome", "error_fingerprint",
        "recovered_from", "sequence", "source_kind", "source_id",
        "authority_class", "provenance_version", "created_at", "expires_at",
    }

    with pytest.raises(ValueError, match="earlier failure"):
        action_outcomes.record_evaluation_event(
            conn,
            event_id="ao-out-of-order",
            objective_ref="objective-42",
            action_name="github.pr.create",
            outcome="success",
            sequence=1,
            recovered_from=failed,
            source_kind="dialog",
            source_id="dialog-42",
            authority="observed",
            principal="fixture-runner",
            channel="offline-evaluation",
        )
    with pytest.raises(ValueError, match="same objective"):
        action_outcomes.record_evaluation_event(
            conn,
            event_id="ao-cross-objective",
            objective_ref="objective-43",
            action_name="github.pr.create",
            outcome="success",
            sequence=2,
            recovered_from=failed,
            source_kind="dialog",
            source_id="dialog-43",
            authority="observed",
            principal="fixture-runner",
            channel="offline-evaluation",
        )


def test_expiry_and_source_or_objective_forgetting_remove_candidates_and_invalidate_lessons(
    fresh_mp,
):
    from threadkeeper import action_outcomes
    from threadkeeper.authority import source_is_live
    from threadkeeper.tools.lessons import lesson_append

    conn = fresh_mp["db"].get_db()
    _failed, recovered = _record_pair(conn, action_outcomes)
    candidate_id = action_outcomes.derive_recovery_candidate(conn, recovered)
    conn.commit()

    result = lesson_append(
        title="Recover GitHub validation errors",
        summary="Use the linked recovery evidence.",
        body="Retry the normalized action only after the stable validation error.",
        source=f"action_outcome_candidate:{candidate_id}",
    )
    assert result.startswith("ok slug=recover-github-validation-errors")
    assert source_is_live(conn, ("lesson", "recover-github-validation-errors"))

    deleted = action_outcomes.forget_objective(conn, "objective-42")
    assert deleted == {"events": 2, "candidates": 1}
    assert not source_is_live(conn, ("lesson", "recover-github-validation-errors"))
    assert conn.execute("SELECT COUNT(*) FROM action_outcome_events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM extract_candidates").fetchone()[0] == 0

    old = action_outcomes.record_evaluation_event(
        conn,
        event_id="ao-expired",
        objective_ref="objective-44",
        action_name="github.pr.create",
        outcome="failure",
        error_fingerprint="http.429.rate-limit",
        sequence=1,
        source_kind="dialog",
        source_id="dialog-44",
        authority="observed",
        principal="fixture-runner",
        channel="offline-evaluation",
        created_at=int(time.time()) - 3 * 86400,
        retention_days=1,
    )
    assert old == "ao-expired"
    assert action_outcomes.prune_expired(conn, int(time.time())) == {"events": 1, "candidates": 0}

    action_outcomes.record_evaluation_event(
        conn,
        event_id="ao-source-forget",
        objective_ref="objective-45",
        action_name="github.pr.create",
        outcome="failure",
        error_fingerprint="http.429.rate-limit",
        sequence=1,
        source_kind="dialog",
        source_id="dialog-45",
        authority="observed",
        principal="fixture-runner",
        channel="offline-evaluation",
    )
    assert action_outcomes.forget_sources(conn, [("dialog", "dialog-45")]) == {
        "events": 1,
        "candidates": 0,
    }

    conn.execute(
        "INSERT INTO dialog_messages "
        "(uuid, source, role, content, created_at) VALUES "
        "('dialog-46', 'pytest', 'user', 'erasable source', 100)",
    )
    action_outcomes.record_evaluation_event(
        conn,
        event_id="ao-forget-selector",
        objective_ref="objective-46",
        action_name="github.pr.create",
        outcome="failure",
        error_fingerprint="http.429.rate-limit",
        sequence=1,
        source_kind="dialog",
        source_id="dialog-46",
        authority="observed",
        principal="fixture-runner",
        channel="offline-evaluation",
    )
    conn.commit()
    from threadkeeper.forget import forget_selector

    assert "action_outcomes=1" in forget_selector(
        "dialog-46", selector_type="uuid", conn=conn,
    )
    forget_selector("dialog-46", selector_type="uuid", dry_run=False, conn=conn)
    assert conn.execute("SELECT COUNT(*) FROM action_outcome_events").fetchone()[0] == 0


def test_offline_comparison_sets_thresholds_without_enabling_live_ingest():
    from threadkeeper import action_outcomes

    fixture = {
        "dialogue_only": [
            "tool arguments very-sensitive-arguments tool output unbounded-output " * 20,
        ],
        "events": [
            {
                "id": "failure", "objective_ref": "objective-1",
                "action_name": "github.pr.create", "outcome": "failure",
                "error_fingerprint": "http.422.validation", "sequence": 1,
            },
            {
                "id": "recovery", "objective_ref": "objective-1",
                "action_name": "github.pr.create", "outcome": "success",
                "sequence": 2, "recovered_from": "failure",
            },
        ],
        "expected_recoveries": [["failure", "recovery"]],
        "forbidden_substrings": ["very-sensitive-arguments", "unbounded-output"],
        "thresholds": {
            "min_lesson_usefulness": 1.0,
            "max_privacy_leakage": 0,
            "max_storage_ratio": 0.5,
            "max_false_causal_links": 0,
        },
    }
    report = action_outcomes.evaluate_offline_fixture(fixture)

    assert report["thresholds_pass"] is True
    assert report["bounded_action_outcomes"]["privacy_leakage"] == 0
    assert report["dialogue_only"]["privacy_leakage"] == 2
    assert report["bounded_action_outcomes"]["lesson_usefulness"] == 1.0
    assert report["live_ingestion_allowed"] is False
    assert action_outcomes.live_ingestion_allowed(True, report) is True
    assert json.dumps(report)
