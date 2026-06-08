"""recompute_all_tiers() heals claims seeded before the tier machinery
existed: tier frozen at 'hypothesis', tier_changed_at NULL, never recomputed
because _recompute_tier only fires on new evidence."""
from __future__ import annotations

import sys
import time
from pathlib import Path

_FAKE_CID = "aaaaaaaa-bbbb-cccc-dddd-eeeeffff0000"


def _bootstrap(tmp_path, monkeypatch):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_INGEST_CAP": "0",
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_CURATOR_INTERVAL_S": "0",
        "THREADKEEPER_EXTRACT_INTERVAL_S": "0",
        "THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
        "THREADKEEPER_CLIENT": "pytest",
        "THREADKEEPER_FORCE_CID": _FAKE_CID,
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import db
    from threadkeeper.tools import dialectic
    return {"db": db, "dialectic": dialectic}


def _seed_frozen_claim(conn, claim_id, n_support):
    """Insert a claim frozen at hypothesis with N weight-1.0 supports,
    WITHOUT triggering _recompute_tier — simulates the pre-migration state."""
    now = int(time.time())
    old = now - 30 * 86400  # quiet: no contradicts, created long ago
    conn.execute(
        "INSERT INTO user_dialectic (id, claim, domain, support_count, "
        "contradict_count, confidence, state, created_at, last_evidence_at, "
        "tier, tier_changed_at) VALUES (?,?,?,?,0,'high','active',?,?,"
        "'hypothesis', NULL)",
        (claim_id, "frozen claim", "style", n_support, old, old),
    )
    for _ in range(n_support):
        conn.execute(
            "INSERT INTO dialectic_evidence (claim_id, kind, source, quote, "
            "weight, created_by_cid, created_at) "
            "VALUES (?, 'support', 'manual', 'q', 1.0, 'cid', ?)",
            (claim_id, old),
        )
    conn.commit()


def test_recompute_promotes_frozen_strong_claim_to_validated(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _seed_frozen_claim(conn, "UCfff", n_support=5)
    assert conn.execute(
        "SELECT tier FROM user_dialectic WHERE id='UCfff'"
    ).fetchone()["tier"] == "hypothesis"

    changed = pkg["dialectic"].recompute_all_tiers()

    assert changed == 1
    assert conn.execute(
        "SELECT tier FROM user_dialectic WHERE id='UCfff'"
    ).fetchone()["tier"] == "validated"
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='tier_promoted' AND target='UCfff'"
    ).fetchone()[0]
    assert n >= 1


def test_recompute_is_idempotent(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _seed_frozen_claim(conn, "UCfff", n_support=5)
    pkg["dialectic"].recompute_all_tiers()
    assert pkg["dialectic"].recompute_all_tiers() == 0


def test_recompute_counts_only_changed_claims(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    # Claim A: frozen at hypothesis with strong support → should promote.
    _seed_frozen_claim(conn, "UCaaa", n_support=5)
    # Claim B: already settled at validated → should NOT change.
    now = int(time.time())
    old = now - 30 * 86400
    conn.execute(
        "INSERT INTO user_dialectic (id, claim, domain, support_count, "
        "contradict_count, confidence, state, created_at, last_evidence_at, "
        "tier, tier_changed_at) VALUES ('UCbbb','settled','style',5,0,'high',"
        "'active',?,?,'validated',?)",
        (old, old, old),
    )
    for _ in range(5):
        conn.execute(
            "INSERT INTO dialectic_evidence (claim_id, kind, source, quote, "
            "weight, created_by_cid, created_at) "
            "VALUES ('UCbbb','support','manual','q',1.0,'cid',?)",
            (old,),
        )
    conn.commit()

    changed = pkg["dialectic"].recompute_all_tiers()

    assert changed == 1  # only UCaaa changed; UCbbb was already validated
    assert conn.execute(
        "SELECT tier FROM user_dialectic WHERE id='UCaaa'"
    ).fetchone()["tier"] == "validated"
    assert conn.execute(
        "SELECT tier FROM user_dialectic WHERE id='UCbbb'"
    ).fetchone()["tier"] == "validated"
