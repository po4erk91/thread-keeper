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
    m = re.search(r"shadow\s+(\d+) / \d+", out)
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
