"""Pin the error-handling contract: tools return human-readable ERR strings
on bad input rather than raising Python exceptions.

A future error-boundary refactor (a `@safe_tool` decorator, schema validation,
or a top-level FastMCP wrapper) must keep these tests green.
"""
from __future__ import annotations


def _t(fresh_mp, name):
    return fresh_mp["mcp"]._tool_manager._tools[name].fn


def test_note_to_unknown_thread(fresh_mp):
    out = _t(fresh_mp, "note")(thread_id="Tnope", content="x", kind="move")
    assert out.startswith("ERR")


def test_close_unknown_thread(fresh_mp):
    out = _t(fresh_mp, "close_thread")(thread_id="Tnope", outcome="x")
    assert out.startswith("ERR")


def test_open_thread_with_bad_parent(fresh_mp):
    out = _t(fresh_mp, "open_thread")(question="x", parent_id="Tnope")
    assert out.startswith("ERR")


def test_whisper_to_unknown_peer(fresh_mp):
    """If self-cid can't be detected (test env has no jsonl), or peer
    doesn't exist, the tool returns ERR not crashes."""
    out = _t(fresh_mp, "whisper")(to_cid="dead00", content="hi")
    assert out.startswith("ERR")


def test_record_attempt_unknown_probe(fresh_mp):
    """Probe with id 'Pxxx' doesn't exist → ERR."""
    out = _t(fresh_mp, "record_attempt")(
        category="ghost_category", success=True, probe_id="Pnope"
    )
    # Either errors out or accepts (category is freeform); must not crash
    assert isinstance(out, str)


def test_vote_distill_unknown_id(fresh_mp):
    out = _t(fresh_mp, "vote_distill")(distill_id="Dnope", weight=1.0)
    assert "ERR" in out or "not_found" in out.lower()


def test_core_remove_nonexistent_is_idempotent(fresh_mp):
    """Removing a key that isn't there is fine — no exception, no error."""
    out = _t(fresh_mp, "core_remove")(key="never_existed")
    # Either "ok" or "not_found" — both acceptable, just must not crash
    assert isinstance(out, str)
