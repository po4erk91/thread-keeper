"""mp_dashboard — aggregate rollup tool.

Verifies the tool renders all sections, counts seeded stores, reflects
loop-pass + outcome events, and degrades without crashing on an empty DB.

NOTE on isolation: assertions are DELTA-based, never absolute counts. The
suite's `test_tools_smoke.py` does a `del sys.modules` + package re-import
+ every-tool invocation at COLLECTION time in the parent process, which
`os.environ.setdefault`-pins a DB path and seeds rows. So "exactly N
threads" is not guaranteed across the full suite even with `fresh_mp`'s
tmp DB — we assert that the dashboard reflects the rows THIS test adds
(before/after delta), which is the real contract anyway.
"""
from __future__ import annotations

import re
import time


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _count(out: str, key: str) -> int:
    """Pull `<key>=N` from the dashboard text. Absence means zero: when a
    store is empty the grouped `threads:` line collapses to `threads: 0`
    (no `active=` token), and that genuinely means 0 active threads — so a
    missing key reads as 0, which keeps before/after deltas correct."""
    m = re.search(rf"\b{re.escape(key)}=(\d+)", out)
    return int(m.group(1)) if m else 0


def _active_count(out: str) -> int:
    return _count(out, "active")


def _notes_count(out: str) -> int:
    return _count(out, "notes")


def _concepts_count(out: str) -> int:
    return _count(out, "concepts")


def test_dashboard_registered(fresh_mp):
    assert "mp_dashboard" in fresh_mp["mcp"]._tool_manager._tools


def test_dashboard_empty_db_no_crash(fresh_mp):
    out = _tool(fresh_mp, "mp_dashboard")()
    for section in ("dashboard", "stores", "loops", "outcomes", "reliability"):
        assert section in out, (section, out)


def test_dashboard_counts_stores_delta(fresh_mp):
    dash = _tool(fresh_mp, "mp_dashboard")
    before = dash()
    a0, n0, c0 = (_active_count(before), _notes_count(before),
                  _concepts_count(before))

    open_thread = _tool(fresh_mp, "open_thread")
    note = _tool(fresh_mp, "note")
    t1 = open_thread(question="alpha")
    open_thread(question="beta")
    note(thread_id=t1, content="a note here", kind="insight")
    note(thread_id=t1, content="another move", kind="move")
    _tool(fresh_mp, "register_concept")(description="a concept by example",
                                        confidence="low")

    after = dash()
    assert _active_count(after) - a0 == 2, (a0, after)
    assert _notes_count(after) - n0 == 2, (n0, after)
    assert _concepts_count(after) - c0 == 1, (c0, after)


def _shadow_win(out: str) -> int:
    # Loop labels are now the agent_status loop ids (shadow_review, not shadow).
    m = re.search(r"shadow_review\s+(\d+) / \d+", out)
    return int(m.group(1)) if m else 0


def test_dashboard_reflects_loop_and_outcome_events(fresh_mp):
    # Delta measured THROUGH the tool itself (before vs after), so both reads
    # go through the identical DB-resolution path — immune to whatever DB a
    # contaminated parent env pinned. Insert the loop/outcome events the
    # daemons would write, then confirm the dashboard's own count rises by 3.
    conn = fresh_mp["db"].get_db()
    now = int(time.time())
    before = _shadow_win(_tool(fresh_mp, "mp_dashboard")(window_days=7))
    for _ in range(3):
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES ('s', 'shadow_review_pass', ?, '', ?)", (str(now), now))
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES ('s', 'skill_materialized', 'Tx', 'path', ?)", (now,))
    conn.commit()
    after_out = _tool(fresh_mp, "mp_dashboard")(window_days=7)
    assert _shadow_win(after_out) - before == 3, (before, after_out)
    assert "skill_materialized" in after_out, after_out


def _loop_win(out: str, label: str) -> int:
    m = re.search(rf"^\s+{re.escape(label)}\s+(\d+) / \d+", out, re.M)
    return int(m.group(1)) if m else 0


def _outcome_all(out: str, label: str) -> int:
    m = re.search(rf"^\s+{re.escape(label)}\s+\d+ / \d+ / (\d+)", out, re.M)
    return int(m.group(1)) if m else 0


def _net_field(out: str, field: str) -> int:
    m = re.search(rf"curator_net_change [^\n]*\b{field}=(\d+)", out)
    return int(m.group(1)) if m else 0


def _net_removed(out: str) -> int:
    return _net_field(out, "removed")


def test_dashboard_loop_list_matches_agent_status(fresh_mp):
    # The two telemetry surfaces must agree on which loops exist: the dashboard
    # derives its loop kinds from the same _LOOP_DEFS the menu-bar status reads.
    from threadkeeper.tools import dashboard
    from threadkeeper import agent_status

    dash_events = {event for _, event in dashboard._LOOP_KINDS}
    status_events = {d["event"] for d in agent_status._LOOP_DEFS}
    assert dash_events == status_events, (dash_events, status_events)
    # The previously-omitted loops (two spawn PAID children) must now be listed.
    for kind in ("dialectic_mine_pass", "dialectic_validate_pass",
                 "evolve_apply_pass", "janitor_pass"):
        assert kind in dash_events, (kind, dash_events)


def test_dashboard_reflects_previously_unlisted_loops(fresh_mp):
    # Acceptance: fire counts show up for dialectic_mine, dialectic_validate,
    # evolve_apply, and thread_janitor — loops the old hand-list omitted.
    conn = fresh_mp["db"].get_db()
    now = int(time.time())
    dash = _tool(fresh_mp, "mp_dashboard")
    # (loop id label, *_pass event kind)
    seeded = [
        ("dialectic_miner", "dialectic_mine_pass"),
        ("dialectic_validator", "dialectic_validate_pass"),
        ("evolve_apply", "evolve_apply_pass"),
        ("thread_janitor", "janitor_pass"),
    ]
    before = {label: _loop_win(dash(window_days=7), label) for label, _ in seeded}
    for _, kind in seeded:
        for _ in range(2):
            conn.execute(
                "INSERT INTO events (session_id, kind, target, summary, created_at) "
                "VALUES ('s', ?, '', '', ?)", (kind, now))
    conn.commit()
    after = dash(window_days=7)
    for label, _ in seeded:
        assert _loop_win(after, label) - before[label] == 2, (label, after)


def test_dashboard_curator_removal_outcome(fresh_mp):
    # Acceptance: a destructive curator pass that prunes 3 lessons shows a
    # non-zero removal outcome AND a non-zero curator_net_change removed count.
    conn = fresh_mp["db"].get_db()
    now = int(time.time())
    dash = _tool(fresh_mp, "mp_dashboard")
    before_out = dash(window_days=7)
    rem0 = _outcome_all(before_out, "lesson_remove")
    net0 = _net_removed(before_out)
    for i in range(3):
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES ('s', 'lesson_remove', ?, 'source=curator', ?)",
            (f"stale-lesson-{i}", now))
    conn.commit()
    after = dash(window_days=7)
    assert _outcome_all(after, "lesson_remove") - rem0 == 3, after
    assert _net_removed(after) - net0 == 3, after


def test_dashboard_lesson_append_emits_countable_outcome(fresh_mp):
    # lesson_append now records an event so additions are visible as a number
    # and split create-vs-patch in the curator_net_change line.
    dash = _tool(fresh_mp, "mp_dashboard")
    la = _tool(fresh_mp, "lesson_append")
    before = dash(window_days=7)
    add0 = _outcome_all(before, "lesson_append")
    net_add0 = _net_field(before, "added")
    net_patch0 = _net_field(before, "patched")

    la(title="Dashboard test lesson", body="a durable rule worth keeping",
       summary="tldr", source="curator")
    # Re-append the same title → in-place patch, not a new addition.
    la(title="Dashboard test lesson", body="the same rule, reworded",
       summary="tldr", source="curator")

    after = dash(window_days=7)
    assert _outcome_all(after, "lesson_append") - add0 == 2, after
    # One create, one in-place patch.
    assert _net_field(after, "added") - net_add0 == 1, after
    assert _net_field(after, "patched") - net_patch0 == 1, after


def test_dashboard_accept_rate(fresh_mp):
    conn = fresh_mp["db"].get_db()
    now = int(time.time())
    # Snapshot existing decisions so the ratio assertion is exact regardless
    # of pre-seeded rows.
    acc0 = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind LIKE 'accept_candidate%'"
    ).fetchone()[0]
    rej0 = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='reject_candidate'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO events (session_id, kind, target, created_at) "
        "VALUES ('s','accept_candidate:note','1',?)", (now,))
    for _ in range(3):
        conn.execute(
            "INSERT INTO events (session_id, kind, target, created_at) "
            "VALUES ('s','reject_candidate','x',?)", (now,))
    conn.commit()
    out = _tool(fresh_mp, "mp_dashboard")()
    acc, dec = acc0 + 1, acc0 + 1 + rej0 + 3
    assert f"candidate_accept_rate {acc}/{dec}" in out, (acc0, rej0, out)
