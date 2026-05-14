"""find_missed_spawns() — flags decomposable assistant responses that
weren't accompanied by a spawn() call.

The tool reads dialog_messages (assistant rows) and tasks (spawn record)
and reports patterns where the agent answered linearly when the response
shape suggests it could have parallelized.
"""
from __future__ import annotations

import os
import time


_FAKE_CID = "11111111-2222-3333-4444-555555555555"
_OTHER_CID = "99999999-8888-7777-6666-555555555555"


def _tool(pkg):
    return pkg["mcp"]._tool_manager._tools["find_missed_spawns"].fn


def _insert_assistant(pkg, content: str, session_id: str = _FAKE_CID,
                      uuid: str = "u_a_1", offset_s: int = 0):
    conn = pkg["db"].get_db()
    now = int(time.time()) - offset_s
    conn.execute(
        "INSERT INTO dialog_messages (uuid, source, project, session_id, "
        "role, content, model, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (uuid, "claude-code", "default", session_id, "assistant",
         content, "?", now),
    )
    conn.commit()
    return now


def _insert_task(pkg, parent_cid: str, started_at: int, task_id: str = "tk_1"):
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, cwd, prompt, started_at) "
        "VALUES (?,?,?,?,?,?)",
        (task_id, os.getpid(), parent_cid, "/tmp", "test spawn", started_at),
    )
    conn.commit()


_NUMBERED_RESPONSE = (
    "Here are several points to consider:\n\n"
    "1. **First reason** — the system handles this correctly under "
    "normal load conditions and stays stable.\n"
    "2. **Second reason** — but under burst traffic it drops messages "
    "due to a queue cap that wasn't tuned for spikes.\n"
    "3. **Third reason** — and finally, the retry path goes through "
    "the same queue, compounding the issue further.\n\n"
    "So the fix should address each of these in turn."
)

_HEADERS_RESPONSE = (
    "Let me cover the topics one by one.\n\n"
    "## Topic A\n\n"
    "First we look at A — it involves the lower layer of the stack "
    "and exposes a few interesting tradeoffs that deserve attention.\n\n"
    "## Topic B\n\n"
    "Then topic B — different concerns entirely, focused on user-facing "
    "ergonomics and the way state propagates upward.\n\n"
    "## Topic C\n\n"
    "Finally topic C, which ties the other two together via a shared "
    "dependency on the bus."
)

_LINEAR_RESPONSE = (
    "The answer is straightforward and does not require breaking the "
    "response into independent units. The bug is in the validator: it "
    "treats empty strings as valid when they should be rejected. The "
    "fix is to add a length check before the regex match — that should "
    "do it. No other changes needed elsewhere in the codebase as far "
    "as I can tell from reading it through carefully. The validator "
    "function is the only place where this check is missing; every "
    "other code path already guards against empty input via the "
    "upstream sanitizer, so a single edit there covers the entire "
    "surface without further coordination."
)


def test_no_data_returns_insufficient(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    out = _tool(pkg)()
    assert "insufficient_data" in out


def test_flags_numbered_response_without_spawn(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    _insert_assistant(pkg, _NUMBERED_RESPONSE, uuid="u_num")
    out = _tool(pkg)()
    assert "missed=1" in out or "missed_spawn" in out
    assert "nbr=3" in out


def test_flags_headers_response_without_spawn(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    _insert_assistant(pkg, _HEADERS_RESPONSE, uuid="u_hdr")
    out = _tool(pkg)()
    assert "missed=1" in out
    assert "hdr=3" in out


def test_does_not_flag_linear_response(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    _insert_assistant(pkg, _LINEAR_RESPONSE, uuid="u_lin")
    out = _tool(pkg)()
    assert "missed=0" in out or "decomposable=0" in out


def test_does_not_flag_when_spawn_nearby(mp_with_cid):
    """A tasks row within ±10 min should clear the missed_spawn flag."""
    pkg = mp_with_cid(_FAKE_CID)
    msg_at = _insert_assistant(pkg, _NUMBERED_RESPONSE, uuid="u_num_ok")
    _insert_task(pkg, _FAKE_CID, started_at=msg_at - 60, task_id="tk_near")
    out = _tool(pkg)()
    # Decomposable response found, but spawn was nearby → not missed.
    assert "missed=0" in out


def test_does_not_credit_spawn_from_other_conversation(mp_with_cid):
    """A spawn under a different parent_cid must not clear the flag."""
    pkg = mp_with_cid(_FAKE_CID)
    msg_at = _insert_assistant(pkg, _NUMBERED_RESPONSE,
                                session_id=_FAKE_CID, uuid="u_num_x")
    _insert_task(pkg, _OTHER_CID, started_at=msg_at, task_id="tk_other")
    out = _tool(pkg)()
    assert "missed=1" in out


def test_skips_subagent_messages(mp_with_cid):
    """Messages from project='subagents' jsonls should be excluded."""
    pkg = mp_with_cid(_FAKE_CID)
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO dialog_messages (uuid, source, project, session_id, "
        "role, content, model, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("u_sub", "claude-code", "subagents", _FAKE_CID, "assistant",
         _NUMBERED_RESPONSE, "?", now),
    )
    conn.commit()
    out = _tool(pkg)()
    # Only subagent message exists → scanned=0 since project='subagents' filtered.
    assert "insufficient_data" in out or "scanned=0" in out


def test_top_n_limit_respected(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    for i in range(5):
        _insert_assistant(pkg, _NUMBERED_RESPONSE,
                          uuid=f"u_n_{i}", offset_s=i * 60)
    out = _tool(pkg)(top_n=2)
    # 5 missed, but only 2 rendered in output.
    body_lines = [l for l in out.split("\n") if l.startswith("  ")]
    assert len(body_lines) == 2
    assert "missed=5" in out
