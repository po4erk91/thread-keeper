from __future__ import annotations

import os
import time


_PARENT_CID = "11112222-3333-4444-5555-666677778888"
_CHILD_CID = "99990000-aaaa-bbbb-cccc-ddddeeeeffff"
_OTHER_CID = "aaaaaaaa-bbbb-cccc-dddd-eeeeffffffff"


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _insert_thread(pkg, tid: str, *, last_touched_at: int,
                   claimed_at: int | None = None,
                   claimed_by_cid: str | None = None) -> None:
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO threads (id, question, state, opened_at, "
        "last_touched_at, claimed_at, claimed_by_cid) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            tid,
            f"pickup question {tid}",
            "idle",
            last_touched_at,
            last_touched_at,
            claimed_at,
            claimed_by_cid,
        ),
    )
    conn.commit()


def _claim_row(pkg, tid: str):
    conn = pkg["db"].get_db()
    return conn.execute(
        "SELECT claimed_at, claimed_by_cid FROM threads WHERE id=?",
        (tid,),
    ).fetchone()


def _insert_spawn_task(pkg, *, parent_cid: str, child_cid: str) -> None:
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, spawned_cid, cwd, prompt, "
        "started_at) VALUES (?,?,?,?,?,?,?)",
        (
            "tk_pickup_child",
            os.getpid(),
            parent_cid,
            child_cid,
            "/tmp",
            "pickup child",
            int(time.time()),
        ),
    )
    conn.commit()


def test_stale_claim_reappears_and_is_reaped_on_claim(mp_with_cid):
    pkg = mp_with_cid(_PARENT_CID)
    from threadkeeper.config import PICKUP_CLAIM_TTL_S

    now = int(time.time())
    tid = "Tpickup_stale"
    _insert_thread(
        pkg,
        tid,
        last_touched_at=now - 4 * 86400,
        claimed_at=now - PICKUP_CLAIM_TTL_S - 10,
        claimed_by_cid=_OTHER_CID,
    )

    candidates = _tool(pkg, "pickup_candidates")(min_idle_days=3, max_n=5)
    assert tid in candidates

    out = _tool(pkg, "claim_pickup")(thread_id=tid)
    assert out == f"ok claimed thread={tid}"
    row = _claim_row(pkg, tid)
    assert row["claimed_by_cid"] == _PARENT_CID
    assert row["claimed_at"] >= now


def test_fresh_claim_stays_hidden_and_blocks_other_claimant(mp_with_cid):
    pkg = mp_with_cid(_PARENT_CID)
    from threadkeeper.config import PICKUP_CLAIM_TTL_S

    now = int(time.time())
    tid = "Tpickup_fresh"
    claimed_at = now - PICKUP_CLAIM_TTL_S + 10
    _insert_thread(
        pkg,
        tid,
        last_touched_at=now - 4 * 86400,
        claimed_at=claimed_at,
        claimed_by_cid=_OTHER_CID,
    )

    candidates = _tool(pkg, "pickup_candidates")(min_idle_days=3, max_n=5)
    assert tid not in candidates

    out = _tool(pkg, "claim_pickup")(thread_id=tid)
    assert out.startswith("ERR already_claimed")
    row = _claim_row(pkg, tid)
    assert row["claimed_by_cid"] == _OTHER_CID
    assert row["claimed_at"] == claimed_at


def test_auto_spawn_child_can_release_parent_pickup_claim(
    mp_with_cid, monkeypatch
):
    pkg = mp_with_cid(_PARENT_CID)
    from threadkeeper.tools import pickup

    now = int(time.time())
    tid = "Tpickup_child_release"
    _insert_thread(pkg, tid, last_touched_at=now - 4 * 86400)

    captured = {}

    def fake_spawn(**kwargs):
        captured.update(kwargs)
        return (
            "ok task=tk_pickup_child pid=123 "
            f"child_cid={_CHILD_CID[:8]} parent_cid={_PARENT_CID[:8]} "
            "perm=auto mode=headless log=/tmp/tk_pickup_child.log"
        )

    monkeypatch.setattr(pickup, "spawn", fake_spawn)
    out = _tool(pkg, "claim_pickup")(thread_id=tid, auto_spawn=True)
    assert out.startswith(f"ok claimed thread={tid} | spawn: ok task=")
    assert "mcp__thread-keeper__release_pickup" in captured["prompt"]
    assert f"thread_id={tid}" in captured["prompt"]

    monkeypatch.setenv("THREADKEEPER_FORCE_CID", _CHILD_CID)
    blocked = _tool(pkg, "release_pickup")(thread_id=tid)
    assert blocked.startswith("ERR not_my_claim")

    _insert_spawn_task(pkg, parent_cid=_PARENT_CID, child_cid=_CHILD_CID)
    released = _tool(pkg, "release_pickup")(thread_id=tid)
    assert released == f"ok released thread={tid}"
    row = _claim_row(pkg, tid)
    assert row["claimed_at"] is None
    assert row["claimed_by_cid"] is None
