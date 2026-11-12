"""Skill-use counting for CLIs without a Skill tool call (Codex and others)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from threadkeeper.skill_signals import (
    SkillSignal,
    codex_rollout_signals,
    invocations_in,
    text_message_signals,
    without_bulk_reads,
)


def _ts(_value: str) -> int:
    return 1_800_000_000


def _no_spawn(_text: str) -> str:
    return ""


def _line(kind: str, payload: dict, ts: str = "2026-11-11T10:00:00Z") -> str:
    return json.dumps({"timestamp": ts, "type": kind, "payload": payload})


def _user(text: str) -> str:
    return _line("response_item", {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": text}],
    })


def _tool_call(arguments: str, kind: str = "function_call") -> str:
    key = "input" if kind == "custom_tool_call" else "arguments"
    return _line("response_item", {"type": kind, "name": "exec", key: arguments})


def _signals(lines: list[str]) -> list[SkillSignal]:
    return list(codex_rollout_signals(lines, _ts, _no_spawn))


def test_codex_tool_call_that_reads_skill_md_counts_as_open():
    lines = [
        _line("session_meta", {"id": "sess-1"}),
        _tool_call('{"cmd":"sed -n 1,80p ~/.codex/skills/naris-code-review/SKILL.md"}'),
        _tool_call("cat ~/.claude/skills/code-review/SKILL.md", "custom_tool_call"),
    ]
    found = _signals(lines)
    assert [(s.session_id, s.name, s.signal) for s in found] == [
        ("sess-1", "naris-code-review", "open"),
        ("sess-1", "code-review", "open"),
    ]


def test_codex_injected_skill_list_is_not_a_use():
    injected = (
        "<skills_instructions>\n- naris-code-review: review "
        "(file: /x/skills/naris-code-review/SKILL.md) $naris-code-review"
    )
    lines = [_line("session_meta", {"id": "s"}), _user(injected)]
    assert _signals(lines) == []


def test_codex_user_dollar_invocation_counts_as_invoke():
    lines = [_line("session_meta", {"id": "s"}), _user("please run $naris-code-review on this PR")]
    assert [(s.name, s.signal) for s in _signals(lines)] == [("naris-code-review", "invoke")]


def test_codex_session_spawned_by_thread_keeper_yields_nothing():
    lines = [
        _line("session_meta", {"id": "s"}),
        _tool_call("cat skills/code-review/SKILL.md"),
        _user("You were spawned in the background by parent conversation abc"),
    ]
    spawned = list(codex_rollout_signals(lines, _ts, lambda text: "cid" if "spawned" in text else ""))
    assert spawned == []


def test_bulk_reads_drop_opens_but_keep_user_invocations():
    opens = [SkillSignal("bulk", 1, f"skill-{i}", "open") for i in range(11)]
    invoke = SkillSignal("bulk", 2, "skill-0", "invoke")
    normal = SkillSignal("normal", 3, "skill-1", "open")
    assert without_bulk_reads([*opens, invoke, normal]) == [invoke, normal]


def test_text_store_signals_for_antigravity_and_copilot():
    messages = [
        SimpleNamespace(role="user", content="/code-review this branch", session_id="c", created_at=5),
        SimpleNamespace(role="assistant", content="Reading skills/code-review/SKILL.md now", session_id="c", created_at=6),
        SimpleNamespace(role="user", content="<environment> $code-review", session_id="c", created_at=7),
    ]
    assert [(s.name, s.signal) for s in text_message_signals(messages)] == [
        ("code-review", "invoke"), ("code-review", "open"),
    ]


def test_shell_variables_are_not_invocations():
    assert invocations_in("echo $HOME and $PATH") == []


def _write_rollout(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_ingest_counts_codex_skill_use_once_and_skips_unknown(fresh_mp, tmp_path):
    from threadkeeper import db, ingest
    from threadkeeper.adapters.codex import ADAPTER

    skills = tmp_path / "skills-root"
    (skills / "naris-code-review").mkdir(parents=True)
    (skills / "naris-code-review" / "SKILL.md").write_text("---\nname: x\n---\n")
    fp = tmp_path / "codex" / "rollout-1.jsonl"
    _write_rollout(fp, [
        _line("session_meta", {"id": "human-session"}),
        _tool_call("cat ~/.codex/skills/naris-code-review/SKILL.md"),
        _tool_call("cat ~/.codex/skills/not-a-skill/SKILL.md"),
    ])
    conn = db.get_db()
    ingest._skill_roots = lambda: [skills]
    assert ingest._record_cli_skill_signals(conn, ADAPTER, fp) == 1
    assert ingest._record_cli_skill_signals(conn, ADAPTER, fp) == 0
    row = conn.execute(
        "SELECT use_count, foreground_use_count FROM skill_usage WHERE name='naris-code-review'"
    ).fetchone()
    assert (row["use_count"], row["foreground_use_count"]) == (1, 1)
    assert conn.execute("SELECT COUNT(*) FROM skill_usage WHERE name='not-a-skill'").fetchone()[0] == 0
    ledger = conn.execute("SELECT source, session_id, signal FROM skill_use_events").fetchall()
    assert [tuple(r) for r in ledger] == [("codex", "human-session", "open")]


def test_ingest_skips_sessions_spawned_by_thread_keeper(fresh_mp, tmp_path):
    from threadkeeper import db, ingest
    from threadkeeper.adapters.codex import ADAPTER

    skills = tmp_path / "skills-root"
    (skills / "code-review").mkdir(parents=True)
    (skills / "code-review" / "SKILL.md").write_text("---\nname: x\n---\n")
    fp = tmp_path / "codex" / "rollout-2.jsonl"
    _write_rollout(fp, [
        _line("session_meta", {"id": "child-session"}),
        _tool_call("cat skills/code-review/SKILL.md"),
    ])
    conn = db.get_db()
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at, spawned_cid) "
        "VALUES ('t1', 1, '/', 'x', 1, 'child-session')"
    )
    ingest._skill_roots = lambda: [skills]
    assert ingest._record_cli_skill_signals(conn, ADAPTER, fp) == 0


def test_backfill_runs_once(fresh_mp, monkeypatch):
    from threadkeeper import db, ingest

    calls = []
    monkeypatch.setattr(ingest, "_record_cli_skill_signals", lambda conn, a, fp: calls.append(fp) or 1)
    fake = SimpleNamespace(transcript_files=lambda: ["a", "b"])
    import threadkeeper.adapters as adapters
    monkeypatch.setattr(adapters, "installed_adapters", lambda: [fake])
    conn = db.get_db()
    assert ingest.backfill_cli_skill_signals(conn) == 2
    assert ingest.backfill_cli_skill_signals(conn) == 0
    assert calls == ["a", "b"]
