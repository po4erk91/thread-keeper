"""brief() spawn_hint nudge — fires only when conditions suggest the agent
should reach for spawn() but currently isn't.

Pins the trigger logic so we don't accidentally regress to "tool exists,
nobody uses it" again.
"""
from __future__ import annotations

import os
import time


_FAKE_CID = "11111111-2222-3333-4444-555555555555"


def _brief_text(pkg):
    return pkg["mcp"]._tool_manager._tools["brief"].fn()


def _open_threads(pkg, n: int):
    open_t = pkg["mcp"]._tool_manager._tools["open_thread"].fn
    return [open_t(question=f"piling up #{i}") for i in range(n)]


def test_no_hint_when_no_active_threads(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    txt = _brief_text(pkg)
    assert "spawn_hint" not in txt


def test_hint_fires_when_active_threads_pile_up(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    _open_threads(pkg, 3)
    txt = _brief_text(pkg)
    assert "spawn_hint" in txt
    assert "active=3" in txt
    assert "children=0" in txt
    assert "spawn(" in txt  # imperative nudge present


def test_hint_marks_never_spawned_on_first_convo(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    _open_threads(pkg, 3)
    txt = _brief_text(pkg)
    assert "never_spawned=1" in txt


def test_hint_disappears_when_child_running(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    _open_threads(pkg, 4)
    # Insert a fake running task as if we'd already spawned a child.
    conn = pkg["db"].get_db()
    now = int(time.time())
    # Use the test process's own pid — alive() returns True so _refresh_tasks
    # leaves it in the running set instead of marking ended.
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, cwd, prompt, started_at) "
        "VALUES (?,?,?,?,?,?)",
        ("tk_x", os.getpid(), _FAKE_CID, "/tmp", "fake child", now),
    )
    conn.commit()
    txt = _brief_text(pkg)
    # children=1, no hint
    assert "spawn_hint" not in txt


def test_hint_fires_on_user_parallelism_cue(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    # Only 1 active thread — wouldn't fire on its own, but cue should kick it.
    _open_threads(pkg, 1)
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO dialog_messages (uuid, source, project, session_id, "
        "role, content, model, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("u_cue_1", "claude-code", "x", _FAKE_CID, "user",
         "while you do that, also check the build logs in parallel", "?", now),
    )
    conn.commit()
    txt = _brief_text(pkg)
    assert "spawn_hint" in txt
    assert "user_cue=" in txt


# Russian fixtures below are intentional: they validate the RU branches
# of threadkeeper.i18n.SPAWN_CUE_RE. The codebase is English-only outside
# i18n.py + these test fixtures.

def test_cue_detects_russian_parallel_word(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    _open_threads(pkg, 1)
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO dialog_messages (uuid, source, project, session_id, "
        "role, content, model, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("u_cue_ru", "claude-code", "x", _FAKE_CID, "user",
         "сделай параллельно ещё одну штуку и заодно проверь тесты", "?", now),
    )
    conn.commit()
    txt = _brief_text(pkg)
    assert "spawn_hint" in txt
    # cue word should be embedded in the hint
    assert "параллельно" in txt or "user_cue=" in txt


def _insert_user_msg(pkg, content: str, uuid: str = "u_cue_x"):
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO dialog_messages (uuid, source, project, session_id, "
        "role, content, model, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (uuid, "claude-code", "x", _FAKE_CID, "user", content, "?", now),
    )
    conn.commit()


def test_cue_detects_count_plus_plural_noun_russian(mp_with_cid):
    """'2 вопроса' / 'три задачи' implies decomposable work even without
    explicit parallelism word."""
    pkg = mp_with_cid(_FAKE_CID)
    _open_threads(pkg, 1)
    _insert_user_msg(pkg, "2 вопроса, почему оно так и почему сяк", "u_cue_n_ru")
    txt = _brief_text(pkg)
    assert "spawn_hint" in txt
    assert "user_cue=" in txt


def test_cue_detects_count_plus_plural_noun_english(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    _open_threads(pkg, 1)
    _insert_user_msg(pkg, "I have three questions about the design", "u_cue_n_en")
    txt = _brief_text(pkg)
    assert "spawn_hint" in txt
    assert "user_cue=" in txt


def test_cue_detects_numbered_enumeration(mp_with_cid):
    """A second-or-later numbered item implies an enumerated list."""
    pkg = mp_with_cid(_FAKE_CID)
    _open_threads(pkg, 1)
    _insert_user_msg(pkg, "do these:\n1) first\n2) second\n3) third", "u_cue_list")
    txt = _brief_text(pkg)
    assert "spawn_hint" in txt
    assert "user_cue=" in txt


def test_cue_does_not_fire_on_single_noun(mp_with_cid):
    """Plain 'I have a question' should not trigger decomposition cue."""
    pkg = mp_with_cid(_FAKE_CID)
    _open_threads(pkg, 1)  # below the active>=3 threshold
    _insert_user_msg(pkg, "I have one question about the design", "u_cue_single")
    txt = _brief_text(pkg)
    # Only 1 active thread + no cue → no hint.
    assert "spawn_hint" not in txt


def test_hint_text_is_imperative(mp_with_cid):
    """Hint phrasing should be directive ('BEFORE answering', 'DECOMPOSE',
    'DO NOT answer linearly'), not descriptive."""
    pkg = mp_with_cid(_FAKE_CID)
    _open_threads(pkg, 3)
    txt = _brief_text(pkg)
    # At least one imperative marker must be present.
    assert any(marker in txt for marker in (
        "BEFORE answering", "DECOMPOSE", "DO NOT answer linearly",
    ))


def test_hint_escalates_after_repeated_ignores(mp_with_cid):
    """3+ shows without an intervening spawn() should escalate the hint:
    add ⚠️ marker, ignored=Nx counter, and reflex-failure footer."""
    pkg = mp_with_cid(_FAKE_CID)
    _open_threads(pkg, 3)
    # First two shows — no escalation yet.
    _brief_text(pkg)
    _brief_text(pkg)
    txt2 = _brief_text(pkg)
    # Third show: counter inside brief sees 2 prior shows logged.
    # The hint text is built BEFORE the current show is logged, so on the
    # third call consecutive_ignored == 2 (still below threshold).
    assert "ignored=" not in txt2
    # Fourth call: now 3 prior shows logged → escalation.
    txt3 = _brief_text(pkg)
    assert "ignored=3x" in txt3 or "ignored=" in txt3
    assert "⚠️" in txt3
    assert "FAILING" in txt3


def test_escalation_resets_after_spawn(mp_with_cid):
    """A new task row (= spawn() happened) should reset the consecutive
    ignore counter — hint stops escalating."""
    pkg = mp_with_cid(_FAKE_CID)
    _open_threads(pkg, 3)
    # Drive counter past threshold.
    for _ in range(5):
        _brief_text(pkg)
    txt_pre = _brief_text(pkg)
    assert "⚠️" in txt_pre  # escalated

    # Simulate a spawn by inserting a task row with started_at = now.
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, cwd, prompt, started_at, "
        "ended_at) VALUES (?,?,?,?,?,?,?)",
        ("tk_reset", 1, _FAKE_CID, "/tmp", "p", now, now),
    )
    conn.commit()

    txt_post = _brief_text(pkg)
    # Counter window is "events after last task started_at", so it should
    # be 0 (or 1 if the just-now show squeezes in past now).
    assert "ignored=" not in txt_post
    assert "FAILING" not in txt_post
