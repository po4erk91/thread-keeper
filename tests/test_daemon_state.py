"""Cross-process daemon cadence gate (`daemon_state.claim_pass`).

Single-flight locks stop *concurrent* passes; this table stops *frequent
sequential* ones — without it, every freshly started MCP server considers
every interval daemon overdue and refires it (observed: an "every 3 days"
curator running 51 times in one day, one pass per new CLI session).
"""
from __future__ import annotations

import time


def test_first_scheduled_claim_wins_second_skips(fresh_mp):
    from threadkeeper import daemon_state

    now = time.time()
    assert daemon_state.claim_pass("loop_a", 3600, scheduled=True, now=now)
    # A second server starting a moment later must NOT refire the loop.
    assert not daemon_state.claim_pass(
        "loop_a", 3600, scheduled=True, now=now + 5
    )


def test_scheduled_due_again_after_interval(fresh_mp):
    from threadkeeper import daemon_state

    now = time.time()
    assert daemon_state.claim_pass("loop_b", 3600, scheduled=True, now=now)
    # daemon_sleep jitters ±15%, so an early wake at 0.85×interval must still
    # count as due (the gate's due-fraction is 0.8)...
    assert daemon_state.claim_pass(
        "loop_b", 3600, scheduled=True, now=now + 0.85 * 3600
    )
    # ...and the freshly recorded run gates the next tick again.
    assert not daemon_state.claim_pass(
        "loop_b", 3600, scheduled=True, now=now + 0.85 * 3600 + 5
    )


def test_manual_claim_bypasses_gate_and_resets_clock(fresh_mp):
    from threadkeeper import daemon_state

    now = time.time()
    assert daemon_state.claim_pass("loop_c", 3600, scheduled=True, now=now)
    # Manual / tool-invoked pass always runs...
    assert daemon_state.claim_pass(
        "loop_c", 3600, scheduled=False, now=now + 10
    )
    # ...and pushes the next scheduled fire out from itself: at +2000s the
    # elapsed 1990s is under the 0.8×3600 due-gap, so the tick skips.
    assert not daemon_state.claim_pass(
        "loop_c", 3600, scheduled=True, now=now + 2000
    )
    assert daemon_state.claim_pass(
        "loop_c", 3600, scheduled=True, now=now + 10 + 3600
    )


def test_cross_connection_claim_is_atomic(fresh_mp):
    """Two servers waking at the same instant: exactly one wins the slot.
    The claim is a single upsert with a WHERE gate, serialized by SQLite's
    write lock — no flock needed for the frequency decision."""
    from threadkeeper import daemon_state
    from threadkeeper.db import get_db

    c1, c2 = get_db(), get_db()
    now = time.time()
    r1 = daemon_state.claim_pass(
        "loop_d", 600, scheduled=True, conn=c1, now=now
    )
    r2 = daemon_state.claim_pass(
        "loop_d", 600, scheduled=True, conn=c2, now=now
    )
    assert (r1, r2) == (True, False)


def test_last_run_at_roundtrip(fresh_mp):
    from threadkeeper import daemon_state

    assert daemon_state.last_run_at("loop_e") is None
    daemon_state.claim_pass("loop_e", 60, scheduled=False, now=1_000_000)
    assert daemon_state.last_run_at("loop_e") == 1_000_000


def test_retention_pass_wired_to_gate(fresh_mp, monkeypatch):
    """Integration: a daemon pass honors the gate only on scheduled ticks."""
    from threadkeeper import retention

    monkeypatch.setattr(retention, "RETENTION_INTERVAL_S", 3600.0)
    first = retention.run_retention_pass(scheduled=True)
    assert first.startswith("deleted")
    assert retention.run_retention_pass(scheduled=True) == "not_due"
    # Manual/forced invocation keeps working regardless of the gate.
    assert retention.run_retention_pass(force=True).startswith("deleted")
