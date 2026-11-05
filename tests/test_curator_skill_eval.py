from __future__ import annotations

from contextlib import nullcontext
import json

import pytest

from threadkeeper import curator_skill_eval as evaluation


@pytest.mark.parametrize(
    ("control", "current", "candidate", "expected"),
    [
        ((3, 3), (0, 0), None, "remove"),
        ((3, 3), (0, 0), (0, 0), "remove"),
        ((0, 0), (0, 0), None, "inconclusive"),
        ((3, 3), (0, 1), None, "inconclusive"),
        ((3, 3), (0, 0), (3, 1), "inconclusive"),
        ((3, 3), (0, 3), (3, 3), "repair"),
        ((0, 0), (3, 3), None, "keep"),
    ],
)
def test_decision_requires_repeated_case_evidence(control, current, candidate, expected):
    assert evaluation.decide(control, current, candidate) == expected


def test_incomplete_or_failed_attempt_is_not_skill_evidence(tmp_path):
    path = tmp_path / "run.json"
    path.write_text(json.dumps({
        "run": {"k": 3, "backend": "codex", "user_customizations": False, "ablated": []},
        "task_results": [
            {"task_name": "case", "attempts": [
                {"outcome": "pass"}, {"outcome": "task_fail"}, {"outcome": "infra_error"},
            ]},
        ],
    }))
    with pytest.raises(ValueError, match="incomplete"):
        evaluation.read_arm(path, ("case",), "sample")


def test_suite_rejects_model_grading_and_agent_supplied_skill_sources(tmp_path):
    suite = tmp_path / "skill.eval.yaml"
    suite.write_text("skills: [./OTHER.md]\ntasks: []\n")
    with pytest.raises(ValueError, match="only a tasks mapping"):
        evaluation.read_suite(suite.read_bytes())
    suite.write_text("tasks: [{name: case, prompt: do something, expect: looks good}]\n")
    with pytest.raises(ValueError, match="deterministic"):
        evaluation.read_suite(suite.read_bytes())


def test_mutation_requires_fresh_evidence_for_exact_edit(tmp_path, monkeypatch):
    settings = tmp_path / "settings"
    suites = settings / "suites"
    suites.mkdir(parents=True)
    suite = suites / "sample.eval.yaml"
    suite.write_text("tasks: []\n")
    skill = tmp_path / "sample"
    skill.mkdir()
    (skill / "SKILL.md").write_text("original")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    monkeypatch.setattr(evaluation, "CURATOR_CALIPER_DIR", settings)
    monkeypatch.setattr(evaluation, "evidence_dir", lambda: snapshot)

    assert evaluation.mutation_refusal("edit", "sample", skill, "repair")
    evaluation.save_evidence(
        snapshot, "sample", evaluation._tree_hash(skill), evaluation._digest(suite.read_bytes()),
        "repair", {"decision": "repair"},
    )
    assert not evaluation.mutation_refusal("edit", "sample", skill, "repair")
    assert evaluation.mutation_refusal("edit", "sample", skill, "unverified edit")
    assert evaluation.mutation_refusal("delete", "sample", skill)
    assert not evaluation.mutation_refusal("delete", "sample", skill, replacement="existing-skill")

    (skill / "SKILL.md").write_text("changed elsewhere")
    assert evaluation.mutation_refusal("edit", "sample", skill, "repair")


@pytest.mark.parametrize(("candidate", "activated", "expected"), [
    ("corrected", True, "repair"),
    ("", True, "remove"),
    ("", False, "inconclusive"),
])
def test_caliper_comparison_authorizes_only_supported_mutation(
    tmp_path, monkeypatch, candidate, activated, expected,
):
    settings = tmp_path / "settings"
    suites = settings / "suites"
    suites.mkdir(parents=True)
    auth = settings / "auth"
    auth.mkdir(mode=0o700)
    (auth / "caliper").mkdir(mode=0o700)
    (auth / "caliper" / "chatgpt.json").write_text("{}")
    (suites / "sample.eval.yaml").write_text(
        "tasks:\n"
        "  - {name: create, prompt: Create sample, assert: assert True}\n"
        "  - {name: inspect, prompt: Inspect sample, assert: assert True}\n",
    )
    skill = tmp_path / "sample"
    skill.mkdir()
    (skill / "SKILL.md").write_text("original")
    snapshot = tmp_path / "snapshot" / "batch-001"
    monkeypatch.setattr(evaluation, "CURATOR_CALIPER_DIR", settings)
    monkeypatch.setattr(evaluation, "CURATOR_CALIPER_MODEL", "test-model")
    monkeypatch.setattr(evaluation, "evidence_dir", lambda: snapshot)
    monkeypatch.setattr(evaluation, "single_flight_lock", lambda name: nullcontext(True))
    calls = []

    def run(auth_path, inputs, output, arm, name, model):
        calls.append(arm)
        assert auth_path == auth / "caliper"
        assert model == "test-model"
        assert name == "sample"
        if arm == "candidate":
            assert (inputs / "repaired/sample/SKILL.md").read_text() == candidate
        assert (inputs / "original/sample/SKILL.md").read_text() == "original"
        passed = arm != "current"
        loaded = arm == "candidate" or (arm == "current" and activated)
        (output / "run.json").write_text(json.dumps({
            "run": {
                "k": 3, "backend": "codex", "model": model, "cli_version": "codex 1",
                "user_customizations": False, "ablated": [name] if arm == "control" else [],
            },
            "task_results": [
                {"task_name": case, "attempts": [
                    {"outcome": "pass" if passed else "task_fail",
                     "activated": [name] if loaded else []},
                ] * 3}
                for case in ("create", "inspect")
            ],
        }))

    monkeypatch.setattr(evaluation, "_docker_run", run)
    result = evaluation.evaluate("sample", skill, candidate)
    assert result["decision"] == expected
    assert calls == (["control", "current", "candidate"] if candidate else ["control", "current"])
    assert bool(evaluation.mutation_refusal("delete", "sample", skill)) == (expected != "remove")
    assert bool(evaluation.mutation_refusal("edit", "sample", skill, candidate)) == (expected != "repair")
    assert (skill / "SKILL.md").read_text() == "original"


def test_caliper_schedule_prioritizes_skills_without_recent_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "snapshots_root", lambda: tmp_path)
    previous = tmp_path / "previous" / "caliper" / "batch-001"
    previous.mkdir(parents=True)
    (previous / "alpha.started").touch()
    (previous / "bravo:candidate.started").touch()

    assert evaluation.scheduled_suites(["alpha", "bravo", "charlie", "delta"]) == {
        "charlie", "delta",
    }


def test_enrolled_rule_retirement_needs_current_evidence_of_no_benefit(tmp_path, monkeypatch):
    suites = tmp_path / "suites"
    suites.mkdir()
    suite = suites / "sample.eval.yaml"
    suite.write_text("tasks: []\n")
    source = tmp_path / "skill"
    source.mkdir()
    (source / "SKILL.md").write_text("A complete user rule.")
    snapshot = tmp_path / "snapshot"
    monkeypatch.setattr(evaluation, "CURATOR_CALIPER_DIR", tmp_path)
    monkeypatch.setattr(evaluation, "evidence_dir", lambda: snapshot)
    assert evaluation.mutation_refusal("retire_rule", "sample", source)
    evidence = {
        "decision": "keep", "cases": ["one", "two"],
        "passes_by_case": {"control": [3, 3], "current": [3, 3]},
        "activations_by_case": {"current": [3, 0]},
    }

    def save():
        evaluation.save_evidence(
            snapshot, "sample", evaluation._tree_hash(source),
            evaluation._digest(suite.read_bytes()), "", evidence,
        )

    save()
    assert evaluation.mutation_refusal("retire_rule", "sample", source)
    evidence["activations_by_case"]["current"] = [3, 3]
    save()
    assert not evaluation.mutation_refusal("retire_rule", "sample", source)
    assert evaluation.mutation_refusal("delete", "sample", source)
    suite.write_text("updated suite\n")
    assert evaluation.mutation_refusal("retire_rule", "sample", source)
