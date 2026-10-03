"""Offline pilot for durable preference-drift decisions (issue #339).

The pilot is deliberately separate from ``dialectic_validator`` and
``dialectic_supersede``.  It replays a fixed, anonymized corpus and reports
what a candidate guard would recommend; it does not mutate a database or alter
the production supersession policy.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .eval.harness import binary_metrics


HERE = Path(__file__).resolve().parent
FIXTURES_PATH = HERE / "eval" / "fixtures" / "dialectic_drift.json"
SHORT_WINDOW_DAYS = 30

# These are a review gate, not a runtime switch.  A passing synthetic corpus
# proves the evaluator is wired as intended; it cannot enable a new automatic
# policy by itself.
PILOT_THRESHOLDS = {
    "min_supersession_precision": 0.95,
    "min_supersession_recall": 0.90,
    "max_false_durable_flip_rate": 0.02,
    "min_poisoning_containment": 1.0,
}


def load_cases(path: Path = FIXTURES_PATH) -> list[dict[str, Any]]:
    """Load the fixed replay corpus, rejecting malformed fixture roots."""
    data = json.loads(path.read_text())
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list):
        raise ValueError("fixture must contain a cases list")
    return cases


def _memory_ids(memories: list[dict[str, Any]], relation: str) -> list[str]:
    return [
        str(memory["memory_id"])
        for memory in memories
        if memory.get("relation") == relation and memory.get("memory_id")
    ]


def _has_both_timescales(memories: list[dict[str, Any]]) -> bool:
    """Require trusted new-preference evidence in recent and older windows."""
    ages = [int(memory.get("age_days", 0)) for memory in memories]
    return any(age <= SHORT_WINDOW_DAYS for age in ages) and any(
        age > SHORT_WINDOW_DAYS for age in ages
    )


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    """Replay one proposed flip without changing a live user model.

    ``support_new`` memory IDs support the proposed replacement.  ``support_old``
    IDs are the durable evidence that would contradict that replacement.  The
    result preserves both lists and a counterfactual for every row, so a
    proposed supersession is independently inspectable and replayable.
    """
    memories = list(case.get("memories") or [])
    new_memories = [
        memory for memory in memories if memory.get("relation") == "support_new"
    ]
    trusted_new = [memory for memory in new_memories if memory.get("trusted", True)]
    supporting_ids = _memory_ids(trusted_new, "support_new")
    contradicting_ids = _memory_ids(memories, "support_old")
    recent_ids = [
        str(memory["memory_id"])
        for memory in trusted_new
        if int(memory.get("age_days", 0)) <= SHORT_WINDOW_DAYS
    ]
    older_ids = [
        str(memory["memory_id"])
        for memory in trusted_new
        if int(memory.get("age_days", 0)) > SHORT_WINDOW_DAYS
    ]
    validated = (case.get("old_claim") or {}).get("tier") == "validated"
    task_scoped = any(memory.get("scope") == "task" for memory in new_memories)
    poisoned = any(
        memory.get("poisoned", False) or not memory.get("trusted", True)
        for memory in new_memories
    )
    ambiguous = bool(case.get("ambiguous", False))
    high_impact = case.get("impact") == "high"
    elicitation_supported = bool(case.get("elicitation_supported", False))

    requires_confirmation = False
    non_destructive = False
    if poisoned:
        decision = "contain"
        non_destructive = True
        counterfactual = (
            "The old claim would be reconsidered only after independent trusted "
            "evidence replaces the poisoned observation."
        )
        reason = "untrusted or poison-marked evidence is contained"
    elif ambiguous and high_impact:
        if elicitation_supported:
            decision = "clarify"
            requires_confirmation = True
            counterfactual = (
                "The old claim would remain active if the user does not confirm "
                "this high-impact ambiguous change."
            )
            reason = "ask one confirmation before a high-impact ambiguous flip"
        else:
            decision = "preserve"
            non_destructive = True
            counterfactual = (
                "The old claim would change only after a supported confirmation "
                "or unambiguous evidence in both time windows."
            )
            reason = "unsupported host keeps an ambiguous high-impact flip non-destructive"
    elif validated and task_scoped:
        decision = "preserve"
        non_destructive = True
        counterfactual = (
            "The old validated claim would be replaced only after trusted evidence "
            "outside the explicitly task-scoped exception appears in both windows."
        )
        reason = "explicit task scope is a local exception, not durable drift"
    elif (
        validated
        and len(supporting_ids) >= 2
        and bool(contradicting_ids)
        and _has_both_timescales(trusted_new)
    ):
        decision = "supersede"
        counterfactual = (
            "The old claim would remain active if trusted support for the proposed "
            "replacement were absent from either the recent or older window."
        )
        reason = "trusted replacement evidence spans recent and older windows"
    else:
        decision = "preserve"
        non_destructive = True
        counterfactual = (
            "The old claim would be replaced only with cited trusted evidence in "
            "both the recent and older windows."
        )
        reason = "insufficient cross-timescale evidence for a durable flip"

    # A decision without both sides of the audit trail is never allowed to be a
    # proposed supersession, even if a future fixture adds an incomplete case.
    if decision == "supersede" and not (supporting_ids and contradicting_ids):
        decision = "preserve"
        non_destructive = True
        counterfactual = (
            "The old claim would remain active until both proposed and existing "
            "claim evidence can be cited."
        )
        reason = "audit citations are incomplete"

    return {
        "id": case.get("id", "?"),
        "category": case.get("category", "unknown"),
        "gold": case.get("expected_decision", "preserve"),
        "decision": decision,
        "proposed_supersession": decision == "supersede",
        "correct": decision == case.get("expected_decision"),
        "reason": reason,
        "supporting_memory_ids": supporting_ids,
        "contradicting_memory_ids": contradicting_ids,
        "counterfactual": counterfactual,
        "evidence_windows": {
            "recent_supporting_memory_ids": recent_ids,
            "older_supporting_memory_ids": older_ids,
        },
        "requires_confirmation": requires_confirmation,
        "non_destructive": non_destructive,
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def run_pilot(path: Path = FIXTURES_PATH) -> dict[str, Any]:
    """Evaluate the corpus and return metrics plus per-case audit records."""
    cases = load_cases(path)
    rows = [evaluate_case(case) for case in cases]
    pairs = [
        ("supersede" if row["proposed_supersession"] else "preserve",
         "supersede" if row["gold"] == "supersede" else "preserve")
        for row in rows
    ]
    supersession = binary_metrics(pairs, "supersede")
    non_durable = [row for row in rows if row["gold"] != "supersede"]
    false_flips = [
        row for row in non_durable if row["proposed_supersession"]
    ]
    ambiguous = [
        row for row in rows
        if row["category"] == "ambiguous_flip"
    ]
    poison = [row for row in rows if row["category"] == "poisoning"]
    audit_complete = all(
        row["supporting_memory_ids"]
        and row["contradicting_memory_ids"]
        and row["counterfactual"]
        for row in rows if row["proposed_supersession"]
    )
    false_flip_rate = _rate(len(false_flips), len(non_durable))
    containment_rate = _rate(
        sum(row["decision"] == "contain" for row in poison), len(poison)
    )
    thresholds_met = (
        audit_complete
        and supersession["precision"] is not None
        and supersession["precision"] >= PILOT_THRESHOLDS["min_supersession_precision"]
        and supersession["recall"] is not None
        and supersession["recall"] >= PILOT_THRESHOLDS["min_supersession_recall"]
        and false_flip_rate is not None
        and false_flip_rate <= PILOT_THRESHOLDS["max_false_durable_flip_rate"]
        and containment_rate is not None
        and containment_rate >= PILOT_THRESHOLDS["min_poisoning_containment"]
    )
    return {
        "corpus": path.name,
        "n": len(rows),
        "supersession": supersession,
        "false_durable_flip_rate": {
            "rate": false_flip_rate,
            "false_flips": len(false_flips),
            "non_durable_cases": len(non_durable),
        },
        "clarification_rate": {
            "rate": _rate(
                sum(row["requires_confirmation"] for row in ambiguous),
                len(ambiguous),
            ),
            "clarifications": sum(row["requires_confirmation"] for row in ambiguous),
            "ambiguous_cases": len(ambiguous),
        },
        "poisoning_containment": {
            "rate": containment_rate,
            "contained": sum(row["decision"] == "contain" for row in poison),
            "poisoning_cases": len(poison),
        },
        "audit_complete": audit_complete,
        "thresholds": PILOT_THRESHOLDS,
        "thresholds_met": thresholds_met,
        "rows": rows,
    }


def format_report(report: dict[str, Any]) -> str:
    """Human-readable report for the intentionally offline pilot."""
    supersession = report["supersession"]
    false_flips = report["false_durable_flip_rate"]
    clarification = report["clarification_rate"]
    containment = report["poisoning_containment"]

    def pct(value: float | None) -> str:
        return f"{value:.1%}" if value is not None else "n/a"

    return "\n".join([
        "── dialectic durable-preference-drift pilot ───────────────",
        f"cases={report['n']} audit_complete={report['audit_complete']}",
        "supersession: "
        f"precision={pct(supersession['precision'])} "
        f"recall={pct(supersession['recall'])}",
        "false durable flips: "
        f"{pct(false_flips['rate'])} "
        f"({false_flips['false_flips']}/{false_flips['non_durable_cases']})",
        "clarifications: "
        f"{pct(clarification['rate'])} "
        f"({clarification['clarifications']}/{clarification['ambiguous_cases']})",
        "poisoning containment: "
        f"{pct(containment['rate'])} "
        f"({containment['contained']}/{containment['poisoning_cases']})",
        f"thresholds_met={report['thresholds_met']} (reporting only; no policy change)",
        "──────────────────────────────────────────────────────────",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m threadkeeper.dialectic_drift_eval",
        description=(
            "Replay the offline durable preference-drift pilot. This command "
            "does not mutate the dialectic user model."
        ),
    )
    parser.add_argument("--fixtures", type=Path, default=FIXTURES_PATH)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_pilot(args.fixtures)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2) if args.json else format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
