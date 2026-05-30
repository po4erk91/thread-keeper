"""Judge panel — spawned independent voters fill the distill/dialectic
promotion quorum, with an adversarial guard so a rubber-stamp panel can't
self-promote (the panel_vote origin is granted by the spawner only when a
skeptic is present).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path


_FAKE_CID = "cccc3333-4444-5555-6666-777788889999"


def _bootstrap(tmp_path, monkeypatch, **env_extra):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_INGEST_CAP": "0",
        "THREADKEEPER_SKILL_WATCH_INTERVAL_S": "0",
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_SEARCH_PROXY_POLL_S": "0",
        "THREADKEEPER_MEMORY_GUARD_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_CURATOR_INTERVAL_S": "0",
        "THREADKEEPER_EXTRACT_INTERVAL_S": "0",
        "THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_PROBE_INTERVAL_S": "0",
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
        "THREADKEEPER_CLIENT": "pytest",
        "THREADKEEPER_FORCE_CID": _FAKE_CID,
        "THREADKEEPER_NO_EMBEDDINGS": "1",
    }
    env.update(env_extra)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import _mcp, db
    return {"mcp": _mcp.mcp, "db": db}


def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


# ── role / adversarial composition logic ───────────────────────────────

def test_roles_default_from_config(tmp_path, monkeypatch):
    _bootstrap(tmp_path, monkeypatch)
    from threadkeeper.tools import panel
    assert panel._roles() == ["skeptic", "critic", "generator"]


def test_roles_override_and_size(tmp_path, monkeypatch):
    _bootstrap(tmp_path, monkeypatch)
    from threadkeeper.tools import panel
    assert panel._roles(size=2, override="a,b,c") == ["a", "b"]
    # size larger than role set cycles
    assert panel._roles(size=4, override="x,y") == ["x", "y", "x", "y"]


def test_panel_with_skeptic_is_adversarial_full_weight(tmp_path, monkeypatch):
    _bootstrap(tmp_path, monkeypatch)
    from threadkeeper.tools import panel
    assert panel._panel_origin(["skeptic", "critic"]) == "panel_vote"


def test_panel_without_skeptic_is_discounted(tmp_path, monkeypatch):
    _bootstrap(tmp_path, monkeypatch)  # PANEL_REQUIRE_SKEPTIC defaults on
    from threadkeeper.tools import panel
    assert panel._panel_origin(["critic", "generator"]) == "background_review"


def test_require_skeptic_off_accepts_diverse_panel(tmp_path, monkeypatch):
    _bootstrap(tmp_path, monkeypatch,
               THREADKEEPER_PANEL_REQUIRE_SKEPTIC="0")
    from threadkeeper.tools import panel
    assert panel._panel_origin(["critic", "generator"]) == "panel_vote"
    assert panel._panel_origin(["critic", "critic"]) == "background_review"


# ── panel_vote origin lifts dialectic evidence to full weight ──────────

def test_panel_vote_origin_full_weight(tmp_path, monkeypatch):
    _bootstrap(tmp_path, monkeypatch)
    from threadkeeper.tools.dialectic import _evidence_weight
    assert _evidence_weight("panel_vote", 1.0) == 1.0
    assert _evidence_weight("background_review", 1.0) == 0.5


def test_panel_vote_weight_configurable(tmp_path, monkeypatch):
    _bootstrap(tmp_path, monkeypatch, THREADKEEPER_PANEL_VOTE_WEIGHT="0.75")
    from threadkeeper.tools.dialectic import _evidence_weight
    assert _evidence_weight("panel_vote", 1.0) == 0.75


# ── convene_panel dispatch (spawn monkeypatched) ───────────────────────

def _seed_distill(pkg, did="Dtest", content="reusable insight X"):
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO threads (id, question, state, opened_at, last_touched_at) "
        "VALUES ('Tsrc','q','active',1,1)"
    )
    conn.execute(
        "INSERT INTO distill (id, content, kind, confidence, source_thread, "
        "created_at) VALUES (?,?,?,?,?,?)",
        (did, content, "pattern", "high", "Tsrc", int(time.time())),
    )
    conn.commit()


def test_convene_panel_distill_spawns_voters(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    _seed_distill(pkg, "Dabc", "always reset network before WDA start")
    calls = []
    import threadkeeper.tools.spawn as spawn_mod

    def _fake_spawn(**kw):
        calls.append(kw)
        return f"ok task=tk_{len(calls)} pid={len(calls)} child_cid=c{len(calls)}"
    monkeypatch.setattr(spawn_mod, "spawn", _fake_spawn)

    out = _tool(pkg, "convene_panel")(target_kind="distill", target_id="Dabc")
    assert "adversarial" in out and "origin=panel_vote" in out
    assert len(calls) == 3
    for c in calls:
        assert c["write_origin"] == "panel_vote"
        assert "mcp__thread-keeper__vote_distill" in c["extra_allowed_tools"]
        # the distillate content reached the child
        assert "always reset network" in c["prompt"]
        # explicit permission to dissent
        assert "may vote against" in c["prompt"].lower()


def test_convene_panel_claim_uses_dialectic_tool(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    conn.execute(
        "INSERT INTO user_dialectic (id, claim, domain, confidence, state, "
        "tier, created_at) VALUES ('UCx','user prefers spawn','workflow',"
        "'medium','active','hypothesis',?)",
        (int(time.time()),),
    )
    conn.commit()
    calls = []
    import threadkeeper.tools.spawn as spawn_mod
    monkeypatch.setattr(spawn_mod, "spawn",
                        lambda **kw: calls.append(kw) or "ok task=tk1")

    out = _tool(pkg, "convene_panel")(target_kind="claim", target_id="UCx")
    assert "origin=panel_vote" in out
    assert all(
        "mcp__thread-keeper__dialectic_evidence" in c["extra_allowed_tools"]
        for c in calls
    )
    assert all("user prefers spawn" in c["prompt"] for c in calls)


def test_convene_panel_unknown_target(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    out = _tool(pkg, "convene_panel")(target_kind="distill", target_id="Dnope")
    assert out.startswith("ERR target_not_found")


def test_convene_panel_bad_kind(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    out = _tool(pkg, "convene_panel")(target_kind="banana", target_id="x")
    assert out.startswith("ERR bad_target_kind")


def test_convene_panel_no_skeptic_is_discounted(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    _seed_distill(pkg, "Dq", "some insight")
    import threadkeeper.tools.spawn as spawn_mod
    seen = []
    monkeypatch.setattr(spawn_mod, "spawn",
                        lambda **kw: seen.append(kw) or "ok task=t")
    out = _tool(pkg, "convene_panel")(
        target_kind="distill", target_id="Dq", roles="critic,generator"
    )
    assert "discounted" in out and "origin=background_review" in out
    assert all(c["write_origin"] == "background_review" for c in seen)
