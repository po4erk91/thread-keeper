"""skill_manage quality gates for learning-loop and session-agent writes."""
from __future__ import annotations

import asyncio
import inspect
import time

import pytest

from tests.test_skills import _bootstrap_skills, _tool

DESCRIPTION = "Use when reviewing a pull request before merge."
BODY = """# Review a pull request

## Steps
1. Run the failing check yourself.

## Check
- Each finding reproduces.

## Not for
- Writing new features.

## Why
A finding that does not reproduce wastes the author's time.
"""


@pytest.fixture()
def gated(tmp_path, monkeypatch):
    pkg = _bootstrap_skills(tmp_path, monkeypatch)
    import threadkeeper.skill_catalog as catalog
    import threadkeeper.tools.skills as skills_mod

    monkeypatch.setattr(skills_mod, "SKILL_QUALITY_GATES", True)
    monkeypatch.setattr(catalog, "semantic_neighbors", lambda *a, **k: [])
    pkg["skills_mod"] = skills_mod
    pkg["catalog"] = catalog
    return pkg


def _as_loop(pkg, monkeypatch, origin="candidate_review"):
    monkeypatch.setattr(pkg["skills_mod"], "WRITE_ORIGIN", origin)


def _two_sessions(pkg) -> str:
    conn = pkg["db"].get_db()
    now = int(time.time())
    for index, session in enumerate(("sess-aaaa1111", "sess-bbbb2222")):
        conn.execute(
            "INSERT INTO dialog_messages (uuid, source, session_id, role, content, created_at) "
            "VALUES (?, 'claude_code', ?, 'user', 'review this PR please', ?)",
            (f"u{index}", session, now - index * 86400),
        )
    conn.commit()
    return "sess-aaaa1111,sess-bbbb2222"


def _create(pkg, **kwargs):
    args = {"action": "create", "name": "pr-review", "description": DESCRIPTION, "content": BODY}
    args.update(kwargs)
    return _tool(pkg, "skill_manage")(**args)


def test_loop_create_needs_template(gated, monkeypatch):
    _as_loop(gated, monkeypatch)
    result = _create(gated, content="# Title\n\nJust text.", evidence_sessions=_two_sessions(gated))
    assert result.startswith("ERR skill_template")
    assert "missing_section:not_for" in result


def test_loop_create_refuses_history_noise_but_session_only_warns(gated, monkeypatch):
    sessions = _two_sessions(gated)
    noisy = BODY.replace("Run the failing check", "As in thread T5a3, run the failing check")
    _as_loop(gated, monkeypatch)
    assert _create(gated, content=noisy, evidence_sessions=sessions).startswith(
        "ERR skill_history_noise"
    )
    _as_loop(gated, monkeypatch, "foreground")
    result = _create(gated, content=noisy)
    assert result.startswith("ok path=SKILL.md")
    assert "warn skill_history_noise" in result


def test_wikilinks_are_refused_for_every_writer(gated):
    result = _create(gated, content=BODY + "\nSee [[some-lesson]].\n")
    assert result.startswith("ERR skill_wikilink")


def test_duplicate_by_meaning_is_refused_unless_new_despite(gated, monkeypatch):
    monkeypatch.setattr(gated["catalog"], "semantic_neighbors", lambda *a, **k: [("code-review", 0.81)])
    result = _create(gated)
    assert result.startswith("ERR likely_duplicate_skill neighbor=code-review score=0.81")
    assert _create(gated, new_despite="code-review").startswith("ok path=SKILL.md")


def test_loop_create_needs_two_sessions(gated, monkeypatch):
    _as_loop(gated, monkeypatch)
    assert _create(gated).startswith("ERR skill_needs_repetition sessions=0")
    assert _create(gated, evidence_sessions="sess-aaaa1111").startswith("ERR skill_needs_repetition")
    _two_sessions(gated)
    assert _create(gated, evidence_sessions="sess-aaaa1111,sess-bbbb2222").startswith("ok path=SKILL.md")


def test_project_examples_need_a_project_scope(gated, monkeypatch):
    monkeypatch.setattr(gated["skills_mod"], "SKILL_PROJECT_TERMS", ("naris",))
    naris_body = BODY.replace("Run the failing check", "Run the Naris check")
    assert _create(gated, content=naris_body).startswith("ERR skill_project_examples")
    assert _create(gated, content=naris_body, project="naris").startswith(
        "ERR skill_project_not_in_description"
    )
    result = _create(
        gated, content=naris_body, project="naris",
        description="Use when reviewing a Naris pull request before merge.",
    )
    assert result.startswith("ok path=SKILL.md")
    text = (gated["skills_root"] / "pr-review" / "SKILL.md").read_text()
    assert 'threadkeeper-project: "naris"' in text


def test_template_exception_needs_force_from_session(gated):
    assert _create(gated, content="# Title\n\nShort.").startswith("ERR skill_template")
    result = _create(gated, content="# Title\n\nShort.", force=True)
    assert result.startswith("ok path=SKILL.md")
    assert "warn" in result


def _record_use(pkg, name, ago_s=0):
    conn = pkg["db"].get_db()
    conn.execute(
        "UPDATE skill_usage SET last_used_at=?, use_count=use_count+1 WHERE name=?",
        (int(time.time()) - ago_s, name),
    )
    conn.commit()


def test_loop_patch_needs_reason_use_and_evidence(gated, monkeypatch):
    assert _create(gated).startswith("ok")
    _as_loop(gated, monkeypatch)
    patch = {"action": "patch", "name": "pr-review",
             "old_string": "- Each finding reproduces.", "new_string": "- Each finding reproduces twice."}
    sm = _tool(gated, "skill_manage")
    assert sm(**patch).startswith("ERR skill_patch_reason_required")
    assert sm(**patch, patch_reason="used_failed", evidence="the review missed a flaky test").startswith(
        "ERR skill_patch_without_use"
    )
    _record_use(gated, "pr-review")
    assert sm(**patch, patch_reason="used_failed").startswith("ERR skill_patch_evidence_required")
    result = sm(**patch, patch_reason="used_failed", evidence="the review missed a flaky test")
    assert result.startswith("ok")


def test_loop_patches_without_use_are_capped(gated, monkeypatch):
    assert _create(gated).startswith("ok")
    _record_use(gated, "pr-review", ago_s=60)
    _as_loop(gated, monkeypatch)
    sm = _tool(gated, "skill_manage")
    lines = ["- Each finding reproduces.", "- Line one.", "- Line two.", "- Line three."]
    for before, after in zip(lines, lines[1:3]):
        result = sm(action="patch", name="pr-review", old_string=before, new_string=after,
                    patch_reason="contradicted", evidence="a newer release changed this check")
        assert result.startswith("ok"), result
    result = sm(action="patch", name="pr-review", old_string=lines[2], new_string=lines[3],
                patch_reason="contradicted", evidence="a newer release changed this check")
    assert result.startswith("ERR skill_patch_cap_without_use patches=2")


def _decide(pkg, proposal_id, accept=True):
    value = _tool(pkg, "memory_proposal_decide")(proposal_id, accept)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def test_session_writes_are_queued_as_proposals(gated, monkeypatch):
    monkeypatch.setattr(gated["skills_mod"], "SESSION_SKILL_WRITES", "propose")
    result = _create(gated)
    assert result.startswith("ok proposal=1 status=pending action=create name=pr-review")
    assert not (gated["skills_root"] / "pr-review").exists()
    assert _create(gated, content="# Title\n\nShort.").startswith("ERR skill_template")
    decided = _decide(gated, 1)
    assert "status=accepted" in decided
    assert (gated["skills_root"] / "pr-review" / "SKILL.md").exists()
    origin = gated["db"].get_db().execute(
        "SELECT created_by_origin FROM skill_usage WHERE name='pr-review'"
    ).fetchone()[0]
    assert origin == "foreground"


def test_accepted_child_proposal_keeps_loop_origin_and_gates(gated, monkeypatch):
    import json

    sessions = _two_sessions(gated)
    conn = gated["db"].get_db()
    conn.execute(
        "INSERT INTO child_memory_grants VALUES ('child-1','tk','x','propose','','candidate_review',1)"
    )
    arguments = {"action": "create", "name": "pr-review", "description": DESCRIPTION, "content": BODY}
    for proposal in (arguments, {**arguments, "evidence_sessions": sessions}):
        conn.execute(
            "INSERT INTO memory_proposals (child_cid, tool_name, arguments, memory_scope, created_at) "
            "VALUES ('child-1', 'skill_manage', ?, 'unresolved', 1)",
            (json.dumps(proposal),),
        )
    conn.commit()
    assert "ERR skill_needs_repetition" in _decide(gated, 1)
    assert "status=accepted" in _decide(gated, 2)
    origin = conn.execute("SELECT created_by_origin FROM skill_usage WHERE name='pr-review'").fetchone()[0]
    assert origin == "candidate_review"


def test_child_proposal_text_gates_run_at_queue_time(gated):
    from threadkeeper.tools.skills import proposal_quality_refusal

    arguments = {"action": "create", "name": "pr-review", "description": DESCRIPTION,
                 "content": BODY + "\nSee [[a-lesson]].\n"}
    assert proposal_quality_refusal(arguments, "shadow_review").startswith("ERR skill_wikilink")
    arguments["content"] = BODY
    assert proposal_quality_refusal(arguments, "shadow_review") == ""


def test_gates_off_restores_legacy_behaviour(gated, monkeypatch):
    monkeypatch.setattr(gated["skills_mod"], "SKILL_QUALITY_GATES", False)
    assert _create(gated, content="# Title\n\nSee [[a]].").startswith("ok path=SKILL.md")
