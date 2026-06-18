"""Learning-loop decision-quality eval harness (threadkeeper/eval, issue #72).

Three layers, mirroring test_memory_eval.py:
  • pure-function units — metrics (precision/recall/F1), Cohen's kappa, the
    deterministic rubric classifier, and the skill-quality heuristic. These
    import nothing heavy, so they run in-process.
  • rubric-sensitivity — the acceptance criterion "editing a daemon rubric and
    re-running moves the metric", proven offline by perturbing the live prompt
    and asserting the F1 drops (no API key needed).
  • end-to-end smoke — runs `python -m threadkeeper.eval --json` as a
    subprocess against the bundled golden fixtures and asserts it computes the
    headline metrics (precision/recall/F1 + judge↔human agreement) and a PASS
    verdict, NOT a fixed quality threshold.
Plus a fixtures-have-no-secrets guard for the "fixtures contain no secrets/
private paths" acceptance criterion.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from threadkeeper.eval import harness as h

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "threadkeeper" / "eval" / "fixtures"


# ── pure functions: metrics ────────────────────────────────────────────────
def test_binary_metrics_precision_recall_f1():
    # 2 TP, 1 FP, 1 FN, 1 TN
    pairs = [("pos", "pos"), ("pos", "pos"), ("pos", "neg"),
             ("neg", "pos"), ("neg", "neg")]
    m = h.binary_metrics(pairs, "pos")
    assert (m["tp"], m["fp"], m["fn"], m["tn"]) == (2, 1, 1, 1)
    assert m["precision"] == round(2 / 3, 4)
    assert m["recall"] == round(2 / 3, 4)
    assert m["f1"] == round(2 / 3, 4)


def test_binary_metrics_undefined_when_no_positives_predicted():
    m = h.binary_metrics([("neg", "neg"), ("neg", "neg")], "pos")
    assert m["precision"] is None and m["recall"] is None and m["f1"] is None


def test_cohen_kappa_perfect_and_chance():
    perfect = [("a", "a"), ("b", "b"), ("a", "a"), ("b", "b")]
    assert h.cohen_kappa(perfect) == 1.0
    # one disagreement out of four → kappa strictly below 1
    mixed = [("a", "a"), ("b", "b"), ("a", "b"), ("b", "b")]
    k = h.cohen_kappa(mixed)
    assert k is not None and k < 1.0


def test_agreement_shape():
    agr = h.agreement([("high", "high"), ("low", "high")])
    assert agr["n"] == 2
    assert agr["accuracy"] == 0.5
    assert agr["kappa"] is not None


# ── pure functions: rubric classifier ───────────────────────────────────────
def test_rubric_predict_votes_via_present_anchor():
    prompt = "CLASS-LEVEL signals (materialize):\n- a workflow rule\nNOT class-level (skip):\n- one-off task\nPROCEDURE"
    reg = {
        "stated_policy": {"label": "materialize", "anchor": "workflow rule"},
        "one_off_task": {"label": "skip", "anchor": "one-off task"},
    }
    sections = h.SHADOW_SECTIONS
    pred, _ = h.rubric_predict(prompt, ["stated_policy"], reg, "skip", sections)
    assert pred == "materialize"
    pred, _ = h.rubric_predict(prompt, ["one_off_task"], reg, "skip", sections)
    assert pred == "skip"


def test_rubric_predict_deactivates_when_section_loses_anchor():
    """A signal stops voting once its anchor leaves its rubric section."""
    reg = {"stated_policy": {"label": "materialize", "anchor": "workflow rule"}}
    with_anchor = "CLASS-LEVEL signals (materialize):\n- a workflow rule\nNOT class-level (skip):\n- x\nPROCEDURE"
    without = "CLASS-LEVEL signals (materialize):\n- a debugging insight\nNOT class-level (skip):\n- x\nPROCEDURE"
    assert h.rubric_predict(with_anchor, ["stated_policy"], reg, "skip",
                            h.SHADOW_SECTIONS)[0] == "materialize"
    # anchor gone from the materialize section → falls back to default
    assert h.rubric_predict(without, ["stated_policy"], reg, "skip",
                            h.SHADOW_SECTIONS)[0] == "skip"


def test_rubric_predict_tie_falls_back_to_default():
    prompt = "CLASS-LEVEL signals (materialize):\n- a workflow rule\nNOT class-level (skip):\n- one-off task\nPROCEDURE"
    reg = {
        "stated_policy": {"label": "materialize", "anchor": "workflow rule"},
        "one_off_task": {"label": "skip", "anchor": "one-off task"},
    }
    pred, reason = h.rubric_predict(prompt, ["stated_policy", "one_off_task"],
                                    reg, "skip", h.SHADOW_SECTIONS)
    assert pred == "skip" and "tie" in reason


# ── pure functions: quality heuristic ───────────────────────────────────────
def test_quality_heuristic_high_and_low_cases():
    high, _ = h.quality_heuristic(
        "verify-state-transition",
        "Use when asserting a result after navigating between screens.",
        "Assert that the destination state actually changed, not merely that "
        "a label exists on the screen.")
    assert high == "high"
    # negative-claim constraint
    assert h.quality_heuristic("x", "notes", "the browser tools do not work")[0] == "low"
    # incident-scoped name
    assert h.quality_heuristic("fix-login-pr-1234", "how we fixed it",
                               "edited the handler and it passed now")[0] == "low"
    # missing description
    assert h.quality_heuristic("x", "", "a body with several words here ok")[0] == "low"
    # too thin
    assert h.quality_heuristic("x", "use it", "too short")[0] == "low"
    # bloated
    assert h.quality_heuristic("x", "use it", "word " * 700)[0] == "low"


# ── rubric sensitivity: editing the rubric moves the metric ─────────────────
def test_editing_shadow_rubric_moves_the_metric():
    """Acceptance criterion: a daemon-rubric edit changes the offline F1."""
    fx = h.load_fixtures(FIXTURES)["shadow"]
    prompt = h.get_shadow_prompt()

    def f1(p):
        pairs = [(h.rubric_predict(p, it.get("signals", []), h.SHADOW_SIGNALS,
                                   "skip", h.SHADOW_SECTIONS)[0], it["label"])
                 for it in fx]
        return h.binary_metrics(pairs, "materialize")["f1"]

    base = f1(prompt)
    assert base == 1.0, "golden fixtures should classify cleanly on the live rubric"
    # remove the stated-policy materialize criterion from the rubric section
    edited = prompt.replace("a workflow rule the user stated as policy", "")
    assert f1(edited) < base


def test_editing_candidate_rubric_moves_the_metric():
    fx = h.load_fixtures(FIXTURES)["candidate"]
    prompt = h.get_candidate_prompt()

    def f1(p):
        pairs = [(h.rubric_predict(p, it.get("signals", []),
                                   h.CANDIDATE_SIGNALS, "reject",
                                   h.CANDIDATE_SECTIONS)[0], it["label"])
                 for it in fx]
        return h.binary_metrics(pairs, "accept")["f1"]

    base = f1(prompt)
    assert base == 1.0
    edited = prompt.replace("class-level rule worth a durable SKILL.md",
                            "worth a durable SKILL.md")
    assert f1(edited) < base


# ── end-to-end smoke ────────────────────────────────────────────────────────
def _run_cli(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "threadkeeper.eval", *args],
        cwd=str(REPO), capture_output=True, text=True, timeout=180, env=env)


def test_cli_computes_metrics_and_passes():
    proc = _run_cli("--json")
    assert proc.returncode == 0, f"runner failed: {proc.stderr}\n{proc.stdout}"
    r = json.loads(proc.stdout)
    # the metrics the acceptance criteria require, all present and computed
    assert r["shadow"]["precision"] is not None
    assert r["shadow"]["recall"] is not None
    assert r["shadow"]["f1"] is not None
    assert r["candidate"]["f1"] is not None
    assert r["quality"]["accuracy"] is not None  # judge↔human agreement
    assert r["quality"]["kappa"] is not None
    assert r["judge"] == "rubric"


def test_cli_golden_baseline_is_clean():
    """The bundled fixtures are a golden set: the offline rubric judge agrees
    with the human labels, so any future rubric edit that drops a criterion
    shows up as a metric regression here."""
    r = json.loads(_run_cli("--json").stdout)
    assert r["shadow"]["f1"] == 1.0, r["shadow"]["rows"]
    assert r["candidate"]["f1"] == 1.0, r["candidate"]["rows"]
    assert r["quality"]["accuracy"] == 1.0, r["quality"]["rows"]
    assert r["verdict"] == "PASS"
    # both decision axes carry enough labels with both classes present
    assert r["shadow"]["ready"] and r["candidate"]["ready"] and r["quality"]["ready"]


def test_cli_llm_judge_without_key_exits_cleanly():
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    proc = _run_cli("--judge", "llm", env=env)
    assert proc.returncode == 3
    assert "ANTHROPIC_API_KEY" in proc.stderr


# ── fixtures contain no secrets / private paths ─────────────────────────────
_SECRET_PATTERNS = [
    re.compile(r"/Users/[A-Za-z0-9]"),       # macOS home paths
    re.compile(r"/home/[A-Za-z0-9]"),        # linux home paths
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),    # API-key-ish tokens
    re.compile(r"AKIA[0-9A-Z]{12,}"),        # AWS access key id
    re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),  # PEM private keys
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{8,}"),  # slack tokens
    re.compile(r"password\s*[=:]\s*\S", re.I),
]


def test_fixtures_have_no_secrets_or_private_paths():
    for name in ("shadow.json", "candidates.json", "skill_quality.json"):
        text = (FIXTURES / name).read_text()
        # valid JSON
        json.loads(text)
        for pat in _SECRET_PATTERNS:
            assert not pat.search(text), f"{name} matched secret pattern {pat.pattern!r}"
