"""Footprint controls for render_brief(): `scope` and `lean`.

Goal: cut the context cost of the SessionStart injection and of repeated
mid-session brief() calls WITHOUT losing any data.

Invariants under test:
  - default (scope='full', lean off) renders everything — data + nudge/meta +
    footer (existing suites cover exact section contents; here we assert
    presence + the deltas).
  - lean=True drops the nudge/meta sections + footer but KEEPS the static data
    sections (verbatim, user_model, …) and the live working set.
  - scope='query' drops the static data AND nudges, keeping only the live
    working set (ctx, threads, query hits).
  - lean skips a nudge's *_shown INSERT together with its render, so escalation
    counters don't advance for nudges the agent never saw.

Deterministic sections only (no env-interval-gated nudges) so the assertions
don't depend on fixture module-reload timing.
"""
from __future__ import annotations

from threadkeeper.brief import render_brief


_FAKE_CID = "dddddddd-eeee-ffff-0000-111122223333"
_THREAD_Q = "footprint working set thread"


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _seed(pkg):
    """One static section (verbatim + user_model), one nudge/meta section
    (evolve_pending — deterministic, no counter), one working-set section
    (an open thread)."""
    open_t = _tool(pkg, "open_thread")
    note = _tool(pkg, "note")
    verbatim_user = _tool(pkg, "verbatim_user")
    claim = _tool(pkg, "dialectic_claim")
    evidence = _tool(pkg, "dialectic_evidence")
    evolve_format = _tool(pkg, "evolve_format")

    tid = open_t(question=_THREAD_Q)
    for i in range(2):
        note(thread_id=tid, content=f"footprint note {i}", kind="move")

    verbatim_user(content="keep prose lean please")  # → verbatim (static)

    res = claim(claim="prefers lean structural prose", domain="style")
    claim_id = next(p[3:] for p in res.split() if p.startswith("id="))
    for _ in range(5):
        evidence(claim_id=claim_id, kind="support", quote="lean")  # → user_model

    evolve_format(suggestion="footprint test pending suggestion")  # → evolve_pending
    return tid


def test_default_full_renders_data_meta_and_footer(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    _seed(pkg)
    conn = pkg["db"].get_db()

    txt = render_brief(conn)  # default scope='full', lean off

    assert "verbatim" in txt           # static data
    assert "user_model" in txt         # static data
    assert "evolve_pending" in txt     # nudge/meta
    assert "user-facing: paraphrase plain" in txt   # footer
    assert _THREAD_Q in txt            # working set


def test_lean_drops_meta_and_footer_keeps_data(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    _seed(pkg)
    conn = pkg["db"].get_db()

    full = render_brief(conn)
    lean = render_brief(conn, lean=True)

    # nudge/meta + footer gone under lean
    assert "evolve_pending" in full and "evolve_pending" not in lean
    assert "user-facing: paraphrase plain" in full
    assert "user-facing: paraphrase plain" not in lean
    # static data preserved
    assert "verbatim" in lean
    assert "user_model" in lean
    # working set preserved
    assert _THREAD_Q in lean
    # net smaller
    assert len(lean) < len(full)


def test_query_scope_drops_static_keeps_workingset(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    _seed(pkg)
    conn = pkg["db"].get_db()

    full = render_brief(conn, query="footprint")
    scoped = render_brief(conn, query="footprint", scope="query")

    # static memory dropped on a mid-session query call
    assert "verbatim" in full and "verbatim" not in scoped
    assert "user_model" in full and "user_model" not in scoped
    # nudge/meta dropped too (non-full scope is implicitly lean)
    assert "evolve_pending" not in scoped
    # working set kept
    assert _THREAD_Q in scoped
    assert len(scoped) < len(full)


def test_lean_skips_spawn_hint_shown_insert(mp_with_cid):
    """spawn_hint fires on ≥3 active threads and logs a spawn_hint_shown
    event. Lean must skip BOTH the render and that INSERT, so the ignore-
    escalation counter doesn't advance for a hint never shown."""
    pkg = mp_with_cid(_FAKE_CID)
    open_t = _tool(pkg, "open_thread")
    for i in range(3):
        open_t(question=f"active thread {i}")
    conn = pkg["db"].get_db()

    def _shown():
        return conn.execute(
            "SELECT COUNT(*) c FROM events WHERE kind='spawn_hint_shown'"
        ).fetchone()["c"]

    before = _shown()
    full = render_brief(conn)
    assert "spawn_hint" in full
    after_full = _shown()
    assert after_full == before + 1   # full render logs the show

    lean = render_brief(conn, lean=True)
    assert "spawn_hint" not in lean
    assert _shown() == after_full      # lean render logs nothing
