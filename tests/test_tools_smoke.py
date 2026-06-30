"""Smoke tests: every registered @mcp.tool() callable without Python exception.

A tool returning an error string ("ERR thread_not_found=...") still passes —
the contract is that errors are surface text, not unhandled exceptions.

Skipped: tools that block (ask/wait/respond) or open OS resources (spawn).
"""
from __future__ import annotations

import asyncio
import inspect

import pytest


# Tools that intentionally block, open Terminal windows, or hit external state.
# We verify they're registered and have a callable .fn — but don't invoke.
_NO_INVOKE = {
    "spawn",          # opens Terminal.app
    "wait",           # blocks up to 60s
    "ask",            # round-trip to a peer
    "respond",        # needs valid qid from ask()
    "open_dialog_window",  # spawns subprocess
    "tournament",     # multi-step, expensive
}


def _dummy_for(param: inspect.Parameter):
    if param.default is not inspect.Parameter.empty:
        return None  # use default
    ann = param.annotation
    if ann is str or ann == "str":
        return "dummy"
    if ann is int or ann == "int":
        return 1
    if ann is bool or ann == "bool":
        return False
    if ann is float or ann == "float":
        return 0.0
    return "dummy"


def _build_kwargs(fn) -> dict:
    sig = inspect.signature(fn)
    out: dict = {}
    for name, p in sig.parameters.items():
        if p.default is inspect.Parameter.empty:
            out[name] = _dummy_for(p)
    return out


def test_all_tools_registered(fresh_mp):
    mcp = fresh_mp["mcp"]
    names = sorted(mcp._tool_manager._tools.keys())
    # Sanity: at least the foundational tools exist
    for must in ["brief", "context", "open_thread", "note", "close_thread",
                 "search", "session_end", "core_set", "whoami"]:
        assert must in names, f"missing tool: {must}"


def test_tool_smoke(fresh_mp, tool_name):
    mcp = fresh_mp["mcp"]
    tool = mcp._tool_manager._tools[tool_name]
    fn = tool.fn
    kwargs = _build_kwargs(fn)
    try:
        out = fn(**kwargs)
        if inspect.isawaitable(out):
            out = asyncio.run(out)
    except Exception as e:
        pytest.fail(f"{tool_name}({kwargs}) raised {type(e).__name__}: {e}")
    assert out is not None or fn.__name__ in {"core_remove"}, \
        f"{tool_name} returned None"


def pytest_generate_tests(metafunc):
    """Parametrize test_tool_smoke over all registered tool names except _NO_INVOKE.

    We have to import the package once to enumerate; this happens in a clean env
    via the same env knobs as fresh_mp (so it doesn't ingest user data).
    """
    if "tool_name" not in metafunc.fixturenames:
        return
    import os, sys, tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp(prefix="mp_collect_"))
    os.environ.setdefault("THREADKEEPER_DB", str(tmp / "db.sqlite"))
    os.environ.setdefault("CLAUDE_PROJECTS_DIR", str(tmp / "no_such"))
    os.environ.setdefault("THREADKEEPER_INGEST_INTERVAL_S", "0")
    os.environ.setdefault("THREADKEEPER_INGEST_CAP", "0")
    Path(os.environ["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper._mcp import mcp
    names = sorted(mcp._tool_manager._tools.keys())
    invokable = [n for n in names if n not in _NO_INVOKE]
    metafunc.parametrize("tool_name", invokable, ids=invokable)
