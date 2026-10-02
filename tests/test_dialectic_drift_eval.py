"""Tests for the offline durable preference-drift pilot (issue #339)."""
from __future__ import annotations

import json
import re

from threadkeeper import dialectic_drift_eval as drift


def _rows_by_id(report):
    return {row["id"]: row for row in report["rows"]}


def test_fixed_corpus_covers_each_required_replay_category():
    categories = {case["category"] for case in drift.load_cases()}
    assert {
        "durable_change",
        "task_local_exception",
        "ambiguous_flip",
        "poisoning",
    } <= categories


def test_fixed_corpus_is_anonymized_and_contains_no_private_paths_or_tokens():
    text = drift.FIXTURES_PATH.read_text()

    assert json.loads(text)["cases"]
    for pattern in (
        r"/Users/[A-Za-z0-9]",
        r"/home/[A-Za-z0-9]",
        r"\bsk-[A-Za-z0-9]{16,}",
        r"AKIA[0-9A-Z]{12,}",
        r"BEGIN [A-Z ]*PRIVATE KEY",
    ):
        assert not re.search(pattern, text), pattern


def test_pilot_reports_required_metrics_and_documented_thresholds():
    report = drift.run_pilot()

    assert report["supersession"]["precision"] == 1.0
    assert report["supersession"]["recall"] == 1.0
    assert report["false_durable_flip_rate"]["rate"] == 0.0
    assert report["clarification_rate"]["rate"] == 0.5
    assert report["poisoning_containment"]["rate"] == 1.0
    assert report["thresholds_met"] is True


def test_each_proposed_supersession_has_replayable_audit_citations():
    report = drift.run_pilot()
    proposed = [row for row in report["rows"] if row["proposed_supersession"]]

    assert report["audit_complete"] is True
    assert proposed
    for row in proposed:
        assert row["supporting_memory_ids"]
        assert row["contradicting_memory_ids"]
        assert row["counterfactual"]
        assert row["evidence_windows"]["recent_supporting_memory_ids"]
        assert row["evidence_windows"]["older_supporting_memory_ids"]


def test_validated_preference_survives_one_explicit_task_exception():
    row = _rows_by_id(drift.run_pilot())["local-style-exception"]

    assert row["decision"] == "preserve"
    assert row["proposed_supersession"] is False
    assert row["non_destructive"] is True
    assert "task-scoped" in row["counterfactual"]


def test_ambiguous_high_impact_flips_clarify_or_preserve_by_host_support():
    rows = _rows_by_id(drift.run_pilot())

    supported = rows["ambiguous-high-impact-supported"]
    assert supported["decision"] == "clarify"
    assert supported["requires_confirmation"] is True

    unsupported = rows["ambiguous-high-impact-unsupported"]
    assert unsupported["decision"] == "preserve"
    assert unsupported["requires_confirmation"] is False
    assert unsupported["non_destructive"] is True


def test_poisoning_is_contained_without_a_proposed_supersession():
    row = _rows_by_id(drift.run_pilot())["poisoned-preference-flip"]

    assert row["decision"] == "contain"
    assert row["non_destructive"] is True
    assert row["proposed_supersession"] is False


def test_cli_json_is_machine_readable_and_never_mutates_a_live_model(capsys):
    assert drift.main(["--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["audit_complete"] is True
    assert report["thresholds_met"] is True
