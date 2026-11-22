"""Dead-letter signal on GitHub: the `blocked` label and the summary comment.

The label is the human-visible part of a dead-letter. A repository without
the label made every `gh issue edit --add-label` fail, while the comment still
said the label had been applied.
"""
from __future__ import annotations

from types import SimpleNamespace


def _gh(responses: list[int], calls: list[list[str]]):
    def run_gh(cmd, *, cwd, timeout=30):
        calls.append(list(cmd))
        code = responses.pop(0)
        return SimpleNamespace(
            returncode=code, stdout="",
            stderr="" if code == 0 else "could not add label: 'blocked' not found",
        )
    return run_gh


def test_missing_blocked_label_is_created_then_applied(fresh_mp, monkeypatch, tmp_path):
    from threadkeeper import evolve_applier as ea
    calls: list[list[str]] = []
    monkeypatch.setattr(ea, "_run_gh", _gh([1, 0, 0], calls))

    assert ea._apply_blocked_label(453, repo_root=tmp_path) == ""

    assert [c[:3] for c in calls] == [
        ["gh", "issue", "edit"],
        ["gh", "label", "create"],
        ["gh", "issue", "edit"],
    ]
    assert calls[1][3] == ea.ROADMAP_ISSUE_BLOCKED_LABEL


def test_existing_blocked_label_is_applied_without_creating_it(
    fresh_mp, monkeypatch, tmp_path,
):
    from threadkeeper import evolve_applier as ea
    calls: list[list[str]] = []
    monkeypatch.setattr(ea, "_run_gh", _gh([0], calls))

    assert ea._apply_blocked_label(453, repo_root=tmp_path) == ""
    assert [c[:3] for c in calls] == [["gh", "issue", "edit"]]


def test_label_failure_is_reported_when_the_retry_also_fails(
    fresh_mp, monkeypatch, tmp_path,
):
    from threadkeeper import evolve_applier as ea
    monkeypatch.setattr(ea, "_run_gh", _gh([1, 0, 1], []))

    assert ea._apply_blocked_label(453, repo_root=tmp_path).startswith(
        "gh_issue_edit_failed"
    )


def test_dead_letter_comment_states_the_label_outcome(fresh_mp):
    from threadkeeper import evolve_applier as ea
    issue = {"number": 453, "title": "Poison issue"}
    applied = f"applied the `{ea.ROADMAP_ISSUE_BLOCKED_LABEL}` label"

    assert applied in ea._roadmap_issue_dead_letter_body(issue, 3, label_applied=True)
    not_applied = ea._roadmap_issue_dead_letter_body(issue, 3, label_applied=False)
    assert applied not in not_applied
    assert "could not apply" in not_applied


def test_dead_letter_passes_the_label_outcome_to_the_comment(
    fresh_mp, monkeypatch, tmp_path,
):
    from threadkeeper import evolve_applier as ea
    seen: dict = {}
    monkeypatch.setattr(ea, "_apply_blocked_label",
                        lambda num, repo_root=None: "gh_issue_edit_failed: x")
    monkeypatch.setattr(
        ea, "_comment_dead_letter",
        lambda issue, attempts, repo_root=None, *, label_applied=True: (
            seen.update(label_applied=label_applied) or ("", "")
        ),
    )
    conn = fresh_mp["db"].get_db()
    fresh_mp["identity"]._ensure_session(conn)

    out = ea._dead_letter_issue(conn, {"number": 453, "title": "x"}, 3, tmp_path)

    assert "label_err=gh_issue_edit_failed" in out
    assert seen == {"label_applied": False}
