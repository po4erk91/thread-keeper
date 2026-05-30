"""search()/brief/dialog FTS path must tolerate FTS5 operator chars.

Regression (found via E2E flow run, 2026-05-30): a query containing an
FTS5 operator char ('-', '?', '/', '(', ':', '*') raised 'fts_error' from
search() and silently returned nothing from the brief() / dialog_search
FTS fallbacks, because FTS5 MATCH parses those as query syntax. The fix
quotes each whitespace term as a phrase via helpers._fts_query, so
operators become literal while phrase adjacency is preserved.

These tests force the FTS path (SEMANTIC_AVAILABLE off) so they run
deterministically whether or not the semantic extra is installed.
"""
from __future__ import annotations

import pytest


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _force_fts(monkeypatch):
    import threadkeeper.tools.threads as th
    monkeypatch.setattr(th, "SEMANTIC_AVAILABLE", False)


def test_fts_query_helper():
    from threadkeeper.helpers import _fts_query
    assert _fts_query("zebra-quux") == '"zebra-quux"'
    assert _fts_query("a b") == '"a" "b"'
    assert _fts_query("?? --") == ""      # pure-punctuation → empty
    assert _fts_query("") == ""
    assert _fts_query("   ") == ""


def test_search_hyphen_query_no_error(fresh_mp, monkeypatch):
    _force_fts(monkeypatch)
    tid = _tool(fresh_mp, "open_thread")(question="host")
    _tool(fresh_mp, "note")(thread_id=tid,
                            content="zebra-quux distinctive token", kind="insight")
    r = _tool(fresh_mp, "search")(query="zebra-quux", k=5)
    assert "fts_error" not in r, r
    assert ("zebra-quux" in r) or ("distinctive" in r), r


@pytest.mark.parametrize("qy", [
    "what about X?", "a/b test", "foo (bar)", "cost-aware", "ratio:high", "wild*",
])
def test_search_operator_chars_no_error(fresh_mp, monkeypatch, qy):
    _force_fts(monkeypatch)
    tid = _tool(fresh_mp, "open_thread")(question="host")
    _tool(fresh_mp, "note")(
        thread_id=tid,
        content="notes about cost-aware a/b foo bar X high wild ratio testing",
        kind="insight",
    )
    r = _tool(fresh_mp, "search")(query=qy, k=5)
    assert "fts_error" not in r, (qy, r)


def test_search_pure_punctuation_returns_no_matches(fresh_mp, monkeypatch):
    _force_fts(monkeypatch)
    tid = _tool(fresh_mp, "open_thread")(question="host")
    _tool(fresh_mp, "note")(thread_id=tid, content="real content", kind="insight")
    r = _tool(fresh_mp, "search")(query="?? --", k=5)
    assert "fts_error" not in r, r
    assert "no_matches" in r, r
