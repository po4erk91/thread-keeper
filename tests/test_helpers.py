"""Stateless helpers: daemon-sleep wake-up jitter (issue #86)."""
from __future__ import annotations

import time

from threadkeeper import helpers


def test_jittered_within_bounds():
    """_jittered never leaves the ±_JITTER_FRAC band."""
    frac = helpers._JITTER_FRAC
    lo, hi = 100.0 * (1 - frac), 100.0 * (1 + frac)
    for _ in range(2000):
        v = helpers._jittered(100.0)
        assert lo <= v <= hi


def test_jittered_actually_varies():
    """Jitter must de-synchronize: many draws are not all identical."""
    seen = {helpers._jittered(100.0) for _ in range(200)}
    assert len(seen) > 50  # overwhelmingly not the same value


def test_jittered_mean_near_input():
    """Jitter is symmetric, so the average stays close to the nominal."""
    n = 5000
    avg = sum(helpers._jittered(100.0) for _ in range(n)) / n
    assert 97.0 <= avg <= 103.0


def test_jittered_passthrough_for_non_positive():
    """0 / negative are returned unchanged (idle path stays well-defined)."""
    assert helpers._jittered(0.0) == 0.0
    assert helpers._jittered(-5.0) == -5.0


def _capture_sleep(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: calls.append(s))
    return calls


def test_daemon_sleep_jitters_active_interval(monkeypatch):
    calls = _capture_sleep(monkeypatch)
    frac = helpers._JITTER_FRAC
    for _ in range(500):
        helpers.daemon_sleep(100.0)
    assert calls
    for s in calls:
        assert 100.0 * (1 - frac) <= s <= 100.0 * (1 + frac)
    assert len(set(calls)) > 10  # not lockstep


def test_daemon_sleep_idles_when_disabled(monkeypatch):
    """interval<=0 sleeps the (jittered) idle floor, never 0 (no busy-spin)."""
    calls = _capture_sleep(monkeypatch)
    frac = helpers._JITTER_FRAC
    helpers.daemon_sleep(0, idle_s=30.0)
    helpers.daemon_sleep(-1, idle_s=30.0)
    assert len(calls) == 2
    for s in calls:
        assert 30.0 * (1 - frac) <= s <= 30.0 * (1 + frac)
        assert s > 0


def test_daemon_sleep_non_numeric_idles(monkeypatch):
    calls = _capture_sleep(monkeypatch)
    helpers.daemon_sleep("not-a-number", idle_s=30.0)
    assert calls and calls[0] > 0


def test_single_flight_lock_is_non_blocking(tmp_path):
    with helpers.single_flight_lock("daemon-test", lock_dir=tmp_path) as first:
        assert first is True
        with helpers.single_flight_lock("daemon-test", lock_dir=tmp_path) as second:
            assert second is False

    with helpers.single_flight_lock("daemon-test", lock_dir=tmp_path) as after:
        assert after is True


def test_alive_returns_false_for_zombie_state(monkeypatch):
    """A pid that exists but reports ps state Z is not a live parent."""
    monkeypatch.setattr(helpers.os, "waitpid", lambda pid, flags: (0, 0))
    monkeypatch.setattr(helpers.os, "kill", lambda pid, sig: None)

    class Result:
        stdout = "Z\n"

    def fake_run(args, **kwargs):
        assert args == ["ps", "-p", "4242", "-o", "state="]
        return Result()

    monkeypatch.setattr(helpers.subprocess, "run", fake_run)

    assert helpers.alive(4242) is False
