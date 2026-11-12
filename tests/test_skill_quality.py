"""Pure SKILL.md quality checks: history noise, template, links, scope, staleness."""
from __future__ import annotations

from threadkeeper import skill_quality as q

GOOD_BODY = """# Review a pull request

## Steps
1. Run the failing check.

## Check
- The finding reproduces.

## Not for
- Writing new features.

## Why
A finding that does not reproduce wastes the author's time.
"""


def test_history_noise_flags_ids_dates_quotes_and_paths():
    text = (
        "In thread T5a3 for ECOM-42328 and AB#123 (PR #454) on 2026-11-11 the "
        "build failed 3 times. Fixed in v0.26.1. The user said «никогда так не делай». "
        "See /Users/someone/project/file."
    )
    found = q.history_noise(text)
    for marker in (
        "thread_id:T5a3", "ticket_id:ECOM-42328", "work_item:AB#123", "pr_number:PR #454",
        "date:2026-11-11", "incident_counter:failed 3 times", "version_narrative:v0.26.1", "user_quote",
        "user_path:/Users/someone/",
    ):
        assert marker in found


def test_history_noise_ignores_code_and_rules():
    text = (
        "Retry up to 3 times. Use UTF-8 and SHA-256.\n"
        "```bash\ngit log --since 2026-01-01  # T1a2\n```\n"
        "Match `ECOM-\\d+` keys."
    )
    assert q.history_noise(text) == []


def test_template_gaps_lists_every_missing_part():
    assert q.template_gaps("Use when reviewing a PR.", GOOD_BODY) == []
    gaps = q.template_gaps("Reviews PRs.", "# Title\n\nJust text.")
    assert gaps == [
        "description_must_start_with_use_when", "missing_section:not_for",
        "missing_section:steps", "missing_section:check", "missing_section:why",
    ]


def test_why_must_be_one_line():
    body = GOOD_BODY.replace(
        "A finding that does not reproduce wastes the author's time.",
        "Line one.\nLine two.",
    )
    assert q.template_gaps("Use when x.", body) == ["why_must_be_one_line"]


def test_wikilinks_are_found_outside_code():
    assert q.wikilinks("See [[some-lesson]] and `[[in-code]]`.") == ["some-lesson"]


def test_split_candidate_for_long_or_many_topic_bodies():
    assert q.split_candidate(GOOD_BODY) == []
    many = "\n".join(f"## Topic {i}\n" + "line\n" * 10 for i in range(11))
    assert q.split_candidate(many)[0].startswith("split_candidate:topic_sections=")
    assert q.split_candidate("x\n" * 200) == ["split_candidate:lines=200"]


def test_project_mentions_respect_word_boundaries():
    terms = ("naris", "masthead")
    assert q.project_mentions("Run the Naris review.", terms) == ["naris"]
    assert q.project_mentions("unarisen masthead-like", terms) == []


def test_stale_references(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".tool").mkdir()
    text = (
        "Read ~/.tool/missing.json and ~/.absent/x.\n"
        "Call mcp__thread-keeper__skill_manage and mcp__thread-keeper__gone_tool.\n"
        "```bash\ndefinitely-not-a-command-xyz --flag\nGET /api\n```\n"
        "Pin yarn@4.12.0."
    )
    found = q.stale_references(text, known_tools={"skill_manage"})
    assert "stale:missing_path:~/.tool/missing.json" in found
    assert not any("~/.absent" in item for item in found)
    assert "stale:unknown_tool:gone_tool" in found
    assert "stale:missing_command:definitely-not-a-command-xyz" in found
    assert not any("GET" in item for item in found)
    assert "stale:version_pin:yarn@4.12.0" in found


def test_added_text_keeps_only_new_lines():
    assert q.added_text("a\nb", "a\nb\nc T1a2") == "c T1a2"
