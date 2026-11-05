from __future__ import annotations

from contextlib import nullcontext
import json
import sqlite3
import subprocess

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


CONTEXT = "The user needs to route imported items by status and handle incomplete inputs."
CASES = ("open", "closed", "draft", "missing", "blank")


def scenarios():
    return {
        "exclusive_axis": "status",
        "cases": [
            {"name": name, "prompt": f"Route the {name} item.",
             "input": f"item {name}", "expected": name,
             "context_excerpt": "route imported items by status",
             **({"exclusive_value": name} if index < 3 else {})}
            for index, name in enumerate(CASES)
        ],
    }


@pytest.mark.parametrize(("change", "message"), [
    (lambda suite: suite["cases"].pop(), "five to six"),
    (lambda suite: suite["cases"][2].pop("exclusive_value"), "mutually exclusive"),
    (lambda suite: suite["cases"][2].update(exclusive_value=" OPEN "), "mutually exclusive"),
    (lambda suite: suite["cases"][0].update(context_excerpt="unrelated reason"), "context"),
    (lambda suite: suite["cases"][0].update(assert_code="assert True"), "fields"),
])
def test_scenarios_need_grounding_and_mutually_exclusive_cases(change, message):
    suite = scenarios()
    change(suite)
    with pytest.raises(ValueError, match=message):
        evaluation.compile_scenarios(json.dumps(suite), CONTEXT)


def test_scenario_compiles_data_into_safe_deterministic_checks(tmp_path):
    suite = scenarios()
    fixture = "one \n'; touch injected; #"
    suite["cases"][0]["input"] = fixture
    task, *_ = evaluation.compile_scenarios(json.dumps(suite), CONTEXT)
    subprocess.run(task["setup"], shell=True, cwd=tmp_path, check=True)
    assert (tmp_path / "input.txt").read_text() == fixture
    assert json.loads((tmp_path / "condition.json").read_text()) == {"status": "open"}
    assert not (tmp_path / "injected").exists()
    assert "route imported items" not in str(task)
    result = tmp_path / "result.txt"
    result.write_text("open\n")
    command = ["python3", "-c", task["assert"]]
    subprocess.run(command, cwd=tmp_path, check=True)
    result.write_text("closed")
    assert subprocess.run(command, cwd=tmp_path, capture_output=True).returncode
    result.unlink()
    result.symlink_to("input.txt")
    assert subprocess.run(command, cwd=tmp_path, capture_output=True).returncode


def skill_context_fixture(tmp_path):
    skill = tmp_path / "sample"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        f"---\nname: sample\ndescription: Example\n"
        f"metadata:\n  threadkeeper-eval-context: {CONTEXT}\n---\nOriginal\n"
    )
    from threadkeeper.db import SCHEMA

    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    return conn, skill


def test_uses_live_creation_note_and_explicit_user_reason(tmp_path):
    conn, skill = skill_context_fixture(tmp_path)
    md = skill / "SKILL.md"
    assert evaluation.skill_context(conn, "sample", skill)["text"] == CONTEXT
    md.write_text("---\nname: sample\ndescription: Generic description\n---\nBody\n")
    assert evaluation.skill_context(conn, "sample", skill) is None
    conn.execute(
        "INSERT INTO notes(id, content, kind, created_at) VALUES (1, ?, 'insight', 0)",
        (CONTEXT,),
    )
    conn.execute("INSERT INTO memory_derivations VALUES ('note', '1', 'skill', 'sample', 0)")
    conn.execute(
        "INSERT INTO memory_authority VALUES ('note', '1', 'trusted', 'user', 'test', 0, NULL, NULL)"
    )
    assert evaluation.skill_context(conn, "sample", skill)["source"] == "note:1"
    conn.execute("UPDATE memory_authority SET invalidated_at=1")
    assert evaluation.skill_context(conn, "sample", skill) is None


@pytest.mark.parametrize(("candidate", "activated", "retry", "expected"), [
    ("corrected", True, False, "repair"),
    ("corrected", True, True, "repair"),
    ("", True, False, "remove"),
    ("", False, False, "inconclusive"),
])
def test_caliper_comparison_authorizes_only_supported_mutation(
    tmp_path, monkeypatch, candidate, activated, retry, expected,
):
    settings = tmp_path / "settings"
    auth = settings / "auth"
    auth.mkdir(mode=0o700, parents=True)
    (auth / "caliper").mkdir(mode=0o700)
    (auth / "caliper" / "chatgpt.json").write_text("{}")
    conn, skill = skill_context_fixture(tmp_path)
    original = (skill / "SKILL.md").read_text()
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
        assert (inputs / "original/sample/SKILL.md").read_text() == original
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
                for case in CASES
            ],
        }))

    monkeypatch.setattr(evaluation, "_docker_run", run)
    assert evaluation.evaluate(conn, "sample", skill)["status"] == "needs_scenarios"
    raw_scenarios = json.dumps(scenarios())
    if retry:
        assert evaluation.evaluate(conn, "sample", skill, scenarios=raw_scenarios)["decision"] == "remove"
        changed = scenarios()
        changed["cases"][0]["expected"] = "different"
        assert evaluation.evaluate(
            conn, "sample", skill, candidate, json.dumps(changed),
        )["reason"] == "reuse_original_scenarios"
        result = evaluation.evaluate(conn, "sample", skill, candidate)
    else:
        result = evaluation.evaluate(conn, "sample", skill, candidate, raw_scenarios)
    assert result["decision"] == expected
    expected_calls = ["control", "current"]
    if retry:
        expected_calls += ["control", "current"]
    if candidate:
        expected_calls.append("candidate")
    assert calls == expected_calls
    assert bool(evaluation.mutation_refusal(conn, "delete", "sample", skill)) == (expected != "remove")
    assert bool(evaluation.mutation_refusal(conn, "edit", "sample", skill, candidate)) == (expected != "repair")
    assert (skill / "SKILL.md").read_text() == original
    if expected == "repair":
        assert evaluation.mutation_refusal(conn, "edit", "sample", skill, "different repair")
    if expected != "inconclusive":
        (skill / "SKILL.md").write_text(original.replace("Original", "Edited outside Curator"))
        assert evaluation.mutation_refusal(conn, "delete", "sample", skill)
        assert evaluation.mutation_refusal(conn, "edit", "sample", skill, candidate)
    assert not evaluation.mutation_refusal(
        conn, "delete", "sample", skill, replacement="existing-skill",
    )


def test_caliper_schedule_prioritizes_skills_without_recent_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "snapshots_root", lambda: tmp_path)
    previous = tmp_path / "previous" / "caliper" / "batch-001"
    previous.mkdir(parents=True)
    (previous / "alpha.started").touch()
    (previous / "bravo:candidate.started").touch()

    assert evaluation.scheduled_suites(["alpha", "bravo", "charlie", "delta"]) == {
        "charlie", "delta",
    }


def test_rule_retirement_needs_current_evidence_of_no_benefit(tmp_path, monkeypatch):
    conn, source = skill_context_fixture(tmp_path)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    suite = snapshot / "sample.suite.json"
    suite.write_text("compiled tasks")
    monkeypatch.setattr(evaluation, "CURATOR_CALIPER_DIR", tmp_path)
    monkeypatch.setattr(evaluation, "evidence_dir", lambda: snapshot)
    assert evaluation.mutation_refusal(conn, "retire_rule", "sample", source)
    evidence = {
        "decision": "keep", "cases": list(CASES),
        "passes_by_case": {"control": [3] * 5, "current": [3] * 5},
        "activations_by_case": {"current": [3, 0, 3, 3, 3]},
    }
    context = evaluation.skill_context(conn, "sample", source)

    def save():
        evaluation.save_evidence(
            snapshot, "sample", evaluation._tree_hash(source),
            evaluation._digest(suite.read_bytes()), context["revision"], "", evidence,
        )

    save()
    assert evaluation.mutation_refusal(conn, "retire_rule", "sample", source)
    evidence["activations_by_case"]["current"] = [3] * 5
    save()
    assert not evaluation.mutation_refusal(conn, "retire_rule", "sample", source)
    assert evaluation.mutation_refusal(conn, "delete", "sample", source)
    suite.write_text("updated suite\n")
    assert evaluation.mutation_refusal(conn, "retire_rule", "sample", source)
