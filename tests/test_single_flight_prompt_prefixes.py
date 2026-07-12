from __future__ import annotations

from dataclasses import dataclass
import importlib
import sqlite3

import pytest


@dataclass(frozen=True)
class PromptCase:
    module_name: str
    prefix_name: str
    prompt_names: tuple[str, ...]


PROMPT_CASES = (
    PromptCase(
        "threadkeeper.curator",
        "CURATOR_PROMPT_PREFIX",
        ("CURATOR_PROMPT",),
    ),
    PromptCase(
        "threadkeeper.shadow_review",
        "SHADOW_REVIEW_PROMPT_PREFIX",
        ("SHADOW_REVIEW_PROMPT",),
    ),
    PromptCase(
        "threadkeeper.dialectic_validator",
        "DIALECTIC_VALIDATOR_PROMPT_PREFIX",
        ("DIALECTIC_VALIDATOR_PROMPT",),
    ),
    PromptCase(
        "threadkeeper.evolve_daemon",
        "EVOLVE_PROMPT_PREFIX",
        ("EVOLVE_RESEARCH_PROMPT", "EVOLVE_AUDIT_PROMPT"),
    ),
    PromptCase(
        "threadkeeper.evolve_daemon",
        "EVOLVE_RESEARCH_PROMPT_PREFIX",
        ("EVOLVE_RESEARCH_PROMPT",),
    ),
    PromptCase(
        "threadkeeper.evolve_daemon",
        "EVOLVE_AUDIT_PROMPT_PREFIX",
        ("EVOLVE_AUDIT_PROMPT",),
    ),
    PromptCase(
        "threadkeeper.evolve_applier",
        "EVOLVE_APPLY_PROMPT_PREFIX",
        (
            "EVOLVE_APPLY_PROMPT",
            "CURATOR_REPORT_APPLY_PROMPT",
            "ROADMAP_ISSUE_APPLY_PROMPT",
            "PR_CONFLICT_REPAIR_PROMPT",
        ),
    ),
    PromptCase(
        "threadkeeper.candidate_reviewer",
        "CANDIDATE_REVIEW_PROMPT_PREFIX",
        ("CANDIDATE_REVIEW_PROMPT",),
    ),
)


@pytest.mark.parametrize(
    "case",
    PROMPT_CASES,
    ids=lambda c: f"{c.module_name}.{c.prefix_name}",
)
def test_single_flight_prompt_prefix_matches_prompt_opening(
    case: PromptCase,
) -> None:
    module = importlib.import_module(case.module_name)
    prefix = getattr(module, case.prefix_name)

    for prompt_name in case.prompt_names:
        prompt = getattr(module, prompt_name)
        assert prompt.startswith(prefix), prompt_name


def test_evolve_audit_prefix_matches_git_writer_guard() -> None:
    from threadkeeper import evolve_applier, evolve_daemon

    assert (
        evolve_applier.EVOLVE_REVIEW_AUDIT_PROMPT_PREFIX
        == evolve_daemon.EVOLVE_AUDIT_PROMPT_PREFIX
    )
    assert evolve_daemon.EVOLVE_AUDIT_PROMPT.startswith(
        evolve_applier.EVOLVE_REVIEW_AUDIT_PROMPT_PREFIX
    )


@dataclass(frozen=True)
class DetectorCase:
    module_name: str
    prefix_name: str
    detector_name: str


DETECTOR_CASES = (
    DetectorCase(
        "threadkeeper.curator",
        "CURATOR_PROMPT_PREFIX",
        "_running_curator_children",
    ),
    DetectorCase(
        "threadkeeper.shadow_review",
        "SHADOW_REVIEW_PROMPT_PREFIX",
        "_running_shadow_children",
    ),
    DetectorCase(
        "threadkeeper.dialectic_validator",
        "DIALECTIC_VALIDATOR_PROMPT_PREFIX",
        "_running_validator_children",
    ),
    DetectorCase(
        "threadkeeper.evolve_daemon",
        "EVOLVE_PROMPT_PREFIX",
        "_running_evolve_children",
    ),
    DetectorCase(
        "threadkeeper.evolve_applier",
        "EVOLVE_APPLY_PROMPT_PREFIX",
        "_running_applier_children",
    ),
    DetectorCase(
        "threadkeeper.evolve_applier",
        "EVOLVE_APPLY_PROMPT_PREFIX",
        "_running_git_writer_children",
    ),
    DetectorCase(
        "threadkeeper.evolve_applier",
        "EVOLVE_REVIEW_AUDIT_PROMPT_PREFIX",
        "_running_git_writer_children",
    ),
    DetectorCase(
        "threadkeeper.candidate_reviewer",
        "CANDIDATE_REVIEW_PROMPT_PREFIX",
        "_running_reviewer_children",
    ),
)


def _task_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks ("
        "id TEXT PRIMARY KEY, "
        "pid INTEGER, "
        "ended_at INTEGER, "
        "prompt TEXT"
        ")"
    )
    return conn


@pytest.mark.parametrize(
    "case",
    DETECTOR_CASES,
    ids=lambda c: f"{c.module_name}.{c.detector_name}.{c.prefix_name}",
)
def test_running_child_detectors_use_prompt_prefix_constant(
    monkeypatch: pytest.MonkeyPatch,
    case: DetectorCase,
) -> None:
    module = importlib.import_module(case.module_name)
    detector = getattr(module, case.detector_name)
    prefix = f"TEST {case.prefix_name} "
    monkeypatch.setattr(module, case.prefix_name, prefix)

    conn = _task_conn()
    conn.execute(
        "INSERT INTO tasks (id, pid, ended_at, prompt) VALUES (?, ?, NULL, ?)",
        ("matching", 0, prefix + "sentinel"),
    )
    conn.execute(
        "INSERT INTO tasks (id, pid, ended_at, prompt) VALUES (?, ?, NULL, ?)",
        ("wrong-prefix", 0, "not " + prefix),
    )

    assert detector(conn) == ["matching"]
