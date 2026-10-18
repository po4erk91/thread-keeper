"""Memory-quality eval harness (scripts/memory_eval/run.py, issues #71/#347).

Two layers:
  • pure-function units — token estimate + deterministic evidence and replay
    judges (recall hit, abstention clean, protected-memory leak), imported in-process
    because run.py defers all threadkeeper imports into a helper.
  • an end-to-end smoke test — runs the actual command as a subprocess
    against the bundled demo corpus and asserts it emits the three headline
    numbers (accuracy / abstention rate / tokens-per-retrieval) over all five
    LongMemEval axes. Subprocess keeps the harness's import-time env setup off
    the shared in-process package state.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RUN_PY = REPO / "scripts" / "memory_eval" / "run.py"
GROUND_TRUTH = REPO / "scripts" / "memory_eval" / "ground_truth.json"


def _load_run_module():
    """Import run.py by path. Safe in-process: its top level imports no
    threadkeeper module (those are deferred into _import_threadkeeper)."""
    spec = importlib.util.spec_from_file_location("memory_eval_run", RUN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── pure functions ────────────────────────────────────────────────────────
def test_estimate_tokens_counts_words_and_punct():
    run = _load_run_module()
    assert run.estimate_tokens("") == 0
    # 3 words + 1 standalone punctuation token
    assert run.estimate_tokens("postgres and redis!") == 4


def test_judge_lexical_recall_hit_and_miss():
    run = _load_run_module()
    item = {"gold_any": ["PostgreSQL", "Postgres"]}
    ok, _ = run.judge_lexical(item, "we chose postgresql as the database")
    assert ok is True
    miss, _ = run.judge_lexical(item, "we chose mysql instead")
    assert miss is False


def test_judge_lexical_gold_all_requires_every_fact():
    run = _load_run_module()
    item = {"gold_all": ["Stripe", "eu-central-1"]}
    assert run.judge_lexical(item, "stripe in eu-central-1")[0] is True
    assert run.judge_lexical(item, "stripe only")[0] is False


def test_judge_lexical_abstention_clean_vs_leak():
    run = _load_run_module()
    item = {
        "abstain": True,
        "trap_substrings": ["migrated to kubernetes"],
        "abstain_evidence": ["stayed on ecs"],
    }
    # faithful context: no fabricated claim surfaced → correct refusal
    clean, _ = run.judge_lexical(item, "we considered kubernetes but stayed on ecs")
    assert clean is True
    # a hallucinated/leaked claim → abstention failure
    leak, reason = run.judge_lexical(item, "we migrated to kubernetes last week")
    assert leak is False
    assert "trap" in reason


def test_judge_lexical_abstention_rejects_unrelated_candidates():
    run = _load_run_module()
    item = {
        "abstain": True,
        "trap_substrings": ["soc2"],
        "abstain_evidence": [],
    }
    ok, reason = run.judge_lexical(item, "PostgreSQL billing configuration")
    assert ok is False
    assert "unrelated" in reason
    assert run.judge_lexical(item, "no_matches")[0] is True


def test_replay_keeps_evidence_and_final_answer_separate():
    run = _load_run_module()
    item = {"gold_all": ["Stripe", "Sentry"]}
    evidence_ok, _ = run.judge_evidence(
        item, "Stripe sends errors to Sentry", expected_abstention=False)
    answer = run.replay_final_answer(
        item, "Stripe sends errors to Sentry", expected_abstention=False)
    answer_ok, _ = run.judge_final_answer(
        item, answer, expected_abstention=False)
    assert evidence_ok is True
    assert answer_ok is True
    assert "Stripe" in answer and "Sentry" in answer


def test_protected_evidence_is_a_retrieval_failure_after_removal():
    run = _load_run_module()
    item = {"gold_all": ["Sundial"], "removed_in": ["retained"]}
    evidence_ok, _ = run.judge_evidence(
        item, "Sundial legacy deployment", expected_abstention=True)
    answer = run.replay_final_answer(
        item, "Sundial legacy deployment", expected_abstention=True)
    outcome = run.classify_outcome(
        item, "Sundial legacy deployment", answer,
        evidence_correct=evidence_ok, answer_correct=True,
        expected_abstention=True, egress_policy="all",
    )
    assert evidence_ok is False
    assert outcome == "removed_memory_use"


def test_stale_answer_is_classified_separately_from_retrieval_failure():
    run = _load_run_module()
    item = {"gold_any": ["eu-central-1"], "stale": ["us-east-1"]}
    outcome = run.classify_outcome(
        item, "eu-central-1", "Answer: us-east-1",
        evidence_correct=True, answer_correct=False,
        expected_abstention=False, egress_policy="all",
    )
    assert outcome == "stale_memory_use"


# ── end-to-end smoke ──────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def smoke_report():
    """Run the real command once (FTS backend, lexical judge) and parse JSON."""
    proc = subprocess.run(
        [sys.executable, str(RUN_PY), "--json"],
        cwd=str(REPO), capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"runner failed: {proc.stderr}\n{proc.stdout}"
    return json.loads(proc.stdout)


def test_smoke_emits_headline_metrics(smoke_report):
    r = smoke_report
    # the three numbers the acceptance criteria require
    assert "accuracy" in r
    assert r["abstention"]["rate"] is not None
    assert r["tokens_per_retrieval"]["total"] > 0
    assert r["tokens_per_retrieval"]["mean"] > 0
    assert r["tokens_per_final_answer"]["total"] > 0
    assert r["retrieval_latency_ms"]["p95"] > 0
    assert r["backend"] == "fts"  # default run is offline / no embeddings
    assert r["evidence_recall"]["rate"] == 1.0
    assert r["final_answer"]["rate"] == 1.0
    assert r["thresholds_pass"] is True


def test_smoke_covers_every_versioned_axis(smoke_report):
    axes = set(smoke_report["per_type"])
    assert axes == {
        "information_extraction", "multi_session_reasoning",
        "temporal_reasoning", "knowledge_update", "abstention",
        "dynamic_state", "workflow_recall", "premise_awareness",
        "implicit_composed_request",
    }


def test_smoke_golden_baseline_is_perfect(smoke_report):
    """The bundled demo corpus is a golden fixture: a faithful retrieval
    recalls every gold fact and never leaks a fabricated one. A regression in
    search()/dialog_search() would drop this below 1.0."""
    r = smoke_report
    assert r["accuracy"] == 1.0, r["rows"]
    assert r["final_answer"]["rate"] == 1.0, r["rows"]
    assert r["abstention"]["rate"] == 1.0
    assert r["abstention"]["n"] >= 10  # abstention is the high-payoff axis


def test_fts_matrix_covers_state_and_egress_safety_variants():
    """The fixed CI-safe matrix proves removed/disallowed memory stays absent.

    Hybrid uses the same path when the optional semantic extra is installed;
    this test pins the FTS half so it remains deterministic in a minimal venv.
    """
    proc = subprocess.run(
        [
            sys.executable, str(RUN_PY), "--matrix", "--matrix-backends", "fts",
            "--strict", "--json",
        ],
        cwd=str(REPO), capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"runner failed: {proc.stderr}\n{proc.stdout}"
    report = json.loads(proc.stdout)
    assert report["thresholds_pass"] is True
    assert len(report["matrix"]) == 9
    assert all(cell["status"] == "ok" for cell in report["matrix"])
    assert all(cell["safety_violations"] == 0 for cell in report["matrix"])


def test_private_holdout_suppresses_case_output():
    proc = subprocess.run(
        [sys.executable, str(RUN_PY), "--private-holdout", "--json"],
        cwd=str(REPO), capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"runner failed: {proc.stderr}\n{proc.stdout}"
    report = json.loads(proc.stdout)
    assert "rows" not in report
    assert "egress_personal_codex" not in proc.stdout


def test_semantic_smoke_uses_hybrid_and_keeps_golden_recall():
    """Semantic availability must add dense candidates, not disable lexical
    recall for notes that have not been embedded yet."""
    proc = subprocess.run(
        [sys.executable, str(RUN_PY), "--semantic", "--json"],
        cwd=str(REPO), capture_output=True, text=True, timeout=240,
    )
    assert proc.returncode == 0, f"runner failed: {proc.stderr}\n{proc.stdout}"
    report = json.loads(proc.stdout)
    assert report["backend"] == "hybrid"
    assert report["accuracy"] == 1.0, report["rows"]
    health = report["embedding_index"]
    assert health["notes_current"] == health["notes_total"] > 0
    assert health["dialog_current"] == health["dialog_total"] > 0
    note_rows = [r for r in report["rows"] if r["system"] == "search"]
    assert note_rows and all(r["correct"] for r in note_rows)


def test_llm_judge_without_key_exits_cleanly():
    """--judge llm with no ANTHROPIC_API_KEY must fail fast, not crash."""
    import os
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    proc = subprocess.run(
        [sys.executable, str(RUN_PY), "--judge", "llm"],
        cwd=str(REPO), capture_output=True, text=True, timeout=180, env=env,
    )
    assert proc.returncode == 3
    assert "ANTHROPIC_API_KEY" in proc.stderr
