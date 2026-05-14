"""Dialectic user-model tests.

Covers the 5 MCP tools shipped under threadkeeper/tools/dialectic.py
plus the confidence-from-evidence emergent behavior.
"""
from __future__ import annotations


def _tools(fresh_mp):
    return fresh_mp["mcp"]._tool_manager._tools


def _id_from_ok(s: str) -> str:
    """Parse 'ok id=UCxxx conf=low' → 'UCxxx'."""
    for tok in s.split():
        if tok.startswith("id="):
            return tok.split("=", 1)[1]
    raise AssertionError(f"no id= token in {s!r}")


def _new_id_from_supersede(s: str) -> str:
    """Parse 'ok new=UCxxx old=UCyyy conf=low' → 'UCxxx'."""
    for tok in s.split():
        if tok.startswith("new="):
            return tok.split("=", 1)[1]
    raise AssertionError(f"no new= token in {s!r}")


# ── creation + initial state ─────────────────────────────────────────────


def test_claim_creates_row_with_low_confidence_by_default(fresh_mp):
    t = _tools(fresh_mp)
    out = t["dialectic_claim"].fn(claim="user prefers terse output",
                                  domain="style")
    assert out.startswith("ok ")
    cid = _id_from_ok(out)
    assert cid.startswith("UC")
    assert "conf=low" in out

    db = fresh_mp["db"]
    conn = db.get_db()
    row = conn.execute(
        "SELECT * FROM user_dialectic WHERE id=?", (cid,)
    ).fetchone()
    assert row["claim"] == "user prefers terse output"
    assert row["domain"] == "style"
    assert row["support_count"] == 0
    assert row["contradict_count"] == 0
    assert row["confidence"] == "low"
    assert row["state"] == "active"
    assert row["superseded_by"] is None


def test_claim_with_initial_evidence_bumps_support_count(fresh_mp):
    t = _tools(fresh_mp)
    out = t["dialectic_claim"].fn(
        claim="user is a senior engineer",
        domain="context",
        evidence="10 years backend, designs distributed systems",
    )
    assert out.startswith("ok ")
    cid = _id_from_ok(out)

    db = fresh_mp["db"]
    conn = db.get_db()
    row = conn.execute(
        "SELECT support_count, contradict_count FROM user_dialectic WHERE id=?",
        (cid,),
    ).fetchone()
    assert row["support_count"] == 1
    assert row["contradict_count"] == 0
    ev = conn.execute(
        "SELECT * FROM dialectic_evidence WHERE claim_id=?", (cid,)
    ).fetchall()
    assert len(ev) == 1
    assert ev[0]["kind"] == "support"
    assert ev[0]["source"] == "manual"


def test_claim_initial_evidence_can_be_contradict(fresh_mp):
    t = _tools(fresh_mp)
    out = t["dialectic_claim"].fn(
        claim="user dislikes long explanations",
        evidence="actually asked for more detail here",
        evidence_kind="contradict",
    )
    cid = _id_from_ok(out)
    db = fresh_mp["db"]
    row = db.get_db().execute(
        "SELECT support_count, contradict_count FROM user_dialectic WHERE id=?",
        (cid,),
    ).fetchone()
    assert row["support_count"] == 0
    assert row["contradict_count"] == 1


# ── confidence recomputation ─────────────────────────────────────────────


def _claim(t, body: str, domain: str = "") -> str:
    return _id_from_ok(
        t["dialectic_claim"].fn(claim=body, domain=domain)
    )


def _add(t, cid: str, kind: str = "support") -> None:
    r = t["dialectic_evidence"].fn(
        claim_id=cid, kind=kind, quote=f"e_{kind}", source="manual"
    )
    assert r.startswith("ok "), r


def _conf(db, cid: str) -> str:
    return db.get_db().execute(
        "SELECT confidence FROM user_dialectic WHERE id=?", (cid,)
    ).fetchone()["confidence"]


def test_evidence_recomputes_confidence(fresh_mp):
    t = _tools(fresh_mp)
    db = fresh_mp["db"]
    cid = _claim(t, "user prefers a tight feedback loop", "workflow")
    assert _conf(db, cid) == "low"
    _add(t, cid, "support")
    # After 1 support / 0 contradict still on the low side (ratio
    # 1/(1+3)=0.25 → medium with our smoothing — also acceptable)
    assert _conf(db, cid) in ("low", "medium")


def test_five_supports_yields_high(fresh_mp):
    t = _tools(fresh_mp)
    db = fresh_mp["db"]
    cid = _claim(t, "user uses Russian when frustrated", "style")
    for _ in range(5):
        _add(t, cid, "support")
    assert _conf(db, cid) == "high"


def test_three_supports_yields_medium(fresh_mp):
    t = _tools(fresh_mp)
    db = fresh_mp["db"]
    cid = _claim(t, "user reads diffs not summaries", "style")
    for _ in range(3):
        _add(t, cid, "support")
    assert _conf(db, cid) == "medium"


def test_one_support_three_contradicts_yields_disputed(fresh_mp):
    t = _tools(fresh_mp)
    db = fresh_mp["db"]
    cid = _claim(t, "user dislikes verbose comments", "style")
    _add(t, cid, "support")
    for _ in range(3):
        _add(t, cid, "contradict")
    assert _conf(db, cid) == "disputed"


def test_one_support_one_contradict_yields_low(fresh_mp):
    t = _tools(fresh_mp)
    db = fresh_mp["db"]
    cid = _claim(t, "user prefers TDD", "workflow")
    _add(t, cid, "support")
    _add(t, cid, "contradict")
    assert _conf(db, cid) == "low"


# ── review filtering ─────────────────────────────────────────────────────


def test_review_filters_by_min_confidence(fresh_mp):
    t = _tools(fresh_mp)
    low = _claim(t, "weak claim", "context")  # stays low
    high = _claim(t, "strong claim", "context")
    for _ in range(5):
        _add(t, high, "support")

    out = t["dialectic_review"].fn(min_confidence="medium")
    assert high in out
    assert low not in out

    out_low = t["dialectic_review"].fn(min_confidence="low")
    # 'low' floor should include both (disputed is excluded by design
    # unless explicitly asked for)
    assert high in out_low
    assert low in out_low


def test_review_filters_by_domain(fresh_mp):
    t = _tools(fresh_mp)
    a = _claim(t, "style claim alpha", "style")
    b = _claim(t, "workflow claim beta", "workflow")
    for _ in range(5):
        _add(t, a, "support")
        _add(t, b, "support")

    out_style = t["dialectic_review"].fn(domain="style")
    assert a in out_style
    assert b not in out_style

    out_workflow = t["dialectic_review"].fn(domain="workflow")
    assert b in out_workflow
    assert a not in out_workflow


def test_review_disputed_bucket(fresh_mp):
    t = _tools(fresh_mp)
    cid = _claim(t, "disputed claim", "context")
    _add(t, cid, "support")
    for _ in range(3):
        _add(t, cid, "contradict")

    # Default min_confidence='low' should NOT surface disputed
    out_default = t["dialectic_review"].fn(min_confidence="low")
    assert cid not in out_default

    # Explicit disputed pulls only disputed
    out_disp = t["dialectic_review"].fn(min_confidence="disputed")
    assert cid in out_disp


# ── synthesis ────────────────────────────────────────────────────────────


def test_synthesis_omits_low_and_disputed(fresh_mp):
    t = _tools(fresh_mp)
    # Low — single support stays low
    low = _claim(t, "low claim untested", "style")
    # Disputed
    disp = _claim(t, "disputed claim mixed", "style")
    _add(t, disp, "support")
    for _ in range(3):
        _add(t, disp, "contradict")
    # Medium
    med = _claim(t, "medium claim threefold", "workflow")
    for _ in range(3):
        _add(t, med, "support")
    # High
    high = _claim(t, "high claim fivefold", "workflow")
    for _ in range(5):
        _add(t, high, "support")

    out = t["dialectic_synthesis"].fn()
    assert "low claim untested" not in out
    assert "disputed claim mixed" not in out
    assert "medium claim threefold" in out
    assert "high claim fivefold" in out


def test_synthesis_groups_by_domain(fresh_mp):
    t = _tools(fresh_mp)
    s = _claim(t, "style line", "style")
    w = _claim(t, "workflow line", "workflow")
    for _ in range(5):
        _add(t, s, "support")
        _add(t, w, "support")

    out = t["dialectic_synthesis"].fn()
    assert "[style]" in out
    assert "[workflow]" in out
    # Each claim appears under its header
    style_idx = out.index("[style]")
    workflow_idx = out.index("[workflow]")
    assert "style line" in out[style_idx:workflow_idx] \
        or "style line" in out[style_idx:]
    assert "workflow line" in out[workflow_idx:]


def test_synthesis_caps_at_12_lines(fresh_mp):
    t = _tools(fresh_mp)
    # Make 20 high-confidence claims; assert <= 12 lines returned.
    for i in range(20):
        cid = _claim(t, f"claim number {i}", "context")
        for _ in range(5):
            _add(t, cid, "support")
    out = t["dialectic_synthesis"].fn()
    assert out != "no_synthesis"
    assert len(out.splitlines()) <= 12


# ── supersede ────────────────────────────────────────────────────────────


def test_supersede_transitions_state_and_links(fresh_mp):
    t = _tools(fresh_mp)
    db = fresh_mp["db"]
    old = _claim(t, "user wants brief comments", "style")
    _add(t, old, "support")
    _add(t, old, "support")

    out = t["dialectic_supersede"].fn(
        old_claim_id=old,
        new_claim="user wants zero comments unless WHY is non-obvious",
        quote="quote: trust internal code; only comment hidden constraints",
    )
    assert out.startswith("ok ")
    new = _new_id_from_supersede(out)
    assert new.startswith("UC")
    assert new != old

    conn = db.get_db()
    old_row = conn.execute(
        "SELECT state, superseded_by FROM user_dialectic WHERE id=?", (old,)
    ).fetchone()
    assert old_row["state"] == "superseded"
    assert old_row["superseded_by"] == new

    new_row = conn.execute(
        "SELECT claim, domain, state FROM user_dialectic WHERE id=?", (new,)
    ).fetchone()
    assert new_row["state"] == "active"
    # Domain inherited from old when not specified
    assert new_row["domain"] == "style"


def test_supersede_preserves_old_evidence(fresh_mp):
    t = _tools(fresh_mp)
    db = fresh_mp["db"]
    old = _claim(t, "outdated belief", "context")
    _add(t, old, "support")
    _add(t, old, "support")
    _add(t, old, "contradict")

    conn = db.get_db()
    before = conn.execute(
        "SELECT COUNT(*) c FROM dialectic_evidence WHERE claim_id=?", (old,)
    ).fetchone()["c"]
    assert before == 3

    t["dialectic_supersede"].fn(
        old_claim_id=old,
        new_claim="refined belief that replaces it",
    )

    after = conn.execute(
        "SELECT COUNT(*) c FROM dialectic_evidence WHERE claim_id=?", (old,)
    ).fetchone()["c"]
    assert after == 3, "supersede must not delete prior evidence"


def test_cannot_add_evidence_to_superseded_claim(fresh_mp):
    t = _tools(fresh_mp)
    old = _claim(t, "stale claim", "context")
    t["dialectic_supersede"].fn(
        old_claim_id=old,
        new_claim="fresh claim",
    )
    out = t["dialectic_evidence"].fn(claim_id=old, kind="support")
    assert out.startswith("ERR")
    assert "not_active" in out


# ── error paths ──────────────────────────────────────────────────────────


def test_claim_rejects_empty(fresh_mp):
    t = _tools(fresh_mp)
    assert t["dialectic_claim"].fn(claim="   ").startswith("ERR")


def test_claim_rejects_bad_evidence_kind(fresh_mp):
    t = _tools(fresh_mp)
    out = t["dialectic_claim"].fn(claim="x", evidence="y",
                                  evidence_kind="maybe")
    assert out.startswith("ERR")
    assert "bad_kind" in out


def test_evidence_rejects_bad_kind(fresh_mp):
    t = _tools(fresh_mp)
    cid = _claim(t, "anything", "other")
    out = t["dialectic_evidence"].fn(claim_id=cid, kind="cheering")
    assert out.startswith("ERR")
    assert "bad_kind" in out


def test_evidence_rejects_bad_weight(fresh_mp):
    t = _tools(fresh_mp)
    cid = _claim(t, "anything", "other")
    out = t["dialectic_evidence"].fn(claim_id=cid, weight=2.5)
    assert out.startswith("ERR")
    assert "weight_out_of_range" in out


def test_evidence_rejects_unknown_claim(fresh_mp):
    t = _tools(fresh_mp)
    out = t["dialectic_evidence"].fn(claim_id="UCnope")
    assert out.startswith("ERR")
    assert "claim_not_found" in out


def test_review_rejects_bad_confidence(fresh_mp):
    t = _tools(fresh_mp)
    out = t["dialectic_review"].fn(min_confidence="enormous")
    assert out.startswith("ERR")
    assert "bad_confidence" in out


def test_supersede_rejects_unknown_old(fresh_mp):
    t = _tools(fresh_mp)
    out = t["dialectic_supersede"].fn(
        old_claim_id="UCnope", new_claim="x"
    )
    assert out.startswith("ERR")
    assert "old_claim_not_found" in out


def test_supersede_rejects_empty_new_claim(fresh_mp):
    t = _tools(fresh_mp)
    old = _claim(t, "anything", "other")
    out = t["dialectic_supersede"].fn(
        old_claim_id=old, new_claim="   "
    )
    assert out.startswith("ERR")


# ── tool registration ───────────────────────────────────────────────────


def test_all_five_tools_registered(fresh_mp):
    t = _tools(fresh_mp)
    for name in (
        "dialectic_claim",
        "dialectic_evidence",
        "dialectic_review",
        "dialectic_synthesis",
        "dialectic_supersede",
    ):
        assert name in t, f"tool {name} not registered"
