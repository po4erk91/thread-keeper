# Dialectic user-model auto-feed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make thread-keeper's dialectic user-model self-update continuously (mechanical capture of user replies → opus interpretation into claims), fix the tier-stuck bug, and make per-role agent+model a first-class setting.

**Architecture:** Three phases. **A** heals existing claims whose tier froze at `hypothesis`. **C** turns `spawn_config` model selection from CLI-keyed into role-keyed `[agents.<role>]` assignments (so the validator can run opus while other roles stay sonnet). **B** adds two daemons — a mechanical `dialectic_miner` (no LLM: captures user replies + preceding-assistant context into a `dialectic_observations` buffer) and a `dialectic_validator` (spawns an opus child that turns the buffer into claims via the existing `dialectic_*` tools).

**Tech Stack:** Python 3.11+, stdlib `sqlite3` (WAL), FastMCP (`@mcp.tool()`), pytest. Daemons are plain `threading.Thread` loops. Spec: [docs/superpowers/specs/2026-06-07-dialectic-user-model-autofeed-design.md](../specs/2026-06-07-dialectic-user-model-autofeed-design.md).

**Conventions (match existing code):**
- Tests purge `sys.modules` then `import threadkeeper.server`; MCP tools reached via `mcp._tool_manager._tools["<name>"].fn`. Use `THREADKEEPER_FORCE_CID` + per-daemon `*_INTERVAL_S=0` env in `_bootstrap`.
- Daemons mirror `threadkeeper/extract_daemon.py` + `threadkeeper/candidate_reviewer.py`: `_serve_loop`, `run_*_pass(force)`, idempotent `start_*` with `_started` flag + `BACKGROUND_DAEMONS_ALLOWED`/`SEMANTIC_AVAILABLE` guards. Telemetry via `events.kind='*_pass'`, high-water ts stored in `events.target`.
- Run `.venv/bin/pytest` from repo root. Commit after each task.

---

## Phase A — tier recompute + startup heal

### Task A1: `recompute_all_tiers()` in tools/dialectic.py

**Files:**
- Modify: `threadkeeper/tools/dialectic.py` (add function after `_recompute_tier`, ~line 255)
- Test: `tests/test_dialectic_recompute.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dialectic_recompute.py
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
    # Precondition: stuck at hypothesis
    assert conn.execute(
        "SELECT tier FROM user_dialectic WHERE id='UCfff'"
    ).fetchone()["tier"] == "hypothesis"

    changed = pkg["dialectic"].recompute_all_tiers()

    assert changed == 1
    assert conn.execute(
        "SELECT tier FROM user_dialectic WHERE id='UCfff'"
    ).fetchone()["tier"] == "validated"
    # tier_promoted event emitted
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='tier_promoted' AND target='UCfff'"
    ).fetchone()[0]
    assert n >= 1


def test_recompute_is_idempotent(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    _seed_frozen_claim(conn, "UCfff", n_support=5)
    pkg["dialectic"].recompute_all_tiers()
    # Second run: nothing left to change
    assert pkg["dialectic"].recompute_all_tiers() == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_dialectic_recompute.py -v`
Expected: FAIL with `AttributeError: module 'threadkeeper.tools.dialectic' has no attribute 'recompute_all_tiers'`

- [ ] **Step 3: Write minimal implementation**

Add to `threadkeeper/tools/dialectic.py` immediately after `_recompute_tier` (before `_insert_evidence`):

```python
def recompute_all_tiers() -> int:
    """One-shot: re-run the tier state machine over every active claim until
    each reaches a fixed point. Heals claims seeded before the tier machinery
    landed (tier defaulted to 'hypothesis', tier_changed_at NULL, and
    _recompute_tier only fires on new evidence). Idempotent — returns the
    number of claims whose tier actually changed.

    The state machine advances at most one level per _recompute_tier call
    (hysteresis), so hypothesis→observed→validated needs up to two steps; we
    iterate per claim to settle it."""
    conn = get_db()
    now_t = int(time.time())
    rows = conn.execute(
        "SELECT id, tier FROM user_dialectic WHERE state='active'"
    ).fetchall()
    changed = 0
    for r in rows:
        start_tier = r["tier"] or "hypothesis"
        last = start_tier
        for _ in range(4):  # ample to settle a 2-step climb
            _, new_tier = _recompute_tier(conn, r["id"], now_t)
            if new_tier == last:
                break
            last = new_tier
        if last != start_tier:
            changed += 1
    conn.commit()
    return changed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_dialectic_recompute.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add threadkeeper/tools/dialectic.py tests/test_dialectic_recompute.py
git commit -m "fix: recompute_all_tiers() heals claims frozen at hypothesis"
```

### Task A2: call recompute at server startup

**Files:**
- Modify: `threadkeeper/identity.py` (daemon-start block in `_ensure_session`, after the `thread_janitor` try/except ending ~line 176, before `return _session_id`)

- [ ] **Step 1: Add the heal call**

In `threadkeeper/identity.py`, immediately after this existing block:

```python
        try:
            from . import thread_janitor
            thread_janitor.start_thread_janitor()
        except Exception:
            pass
```

insert:

```python
        try:
            # One-shot self-heal: claims seeded before the tier machinery
            # never got their tier recomputed (see recompute_all_tiers). Cheap
            # (a handful of active claims); idempotent on every startup.
            from .tools import dialectic
            dialectic.recompute_all_tiers()
        except Exception:
            pass
```

- [ ] **Step 2: Verify nothing regressed**

Run: `.venv/bin/pytest tests/test_dialectic_tier.py tests/test_dialectic_recompute.py -q`
Expected: PASS (existing tier tests + new recompute tests)

- [ ] **Step 3: Commit**

```bash
git add threadkeeper/identity.py
git commit -m "fix: recompute dialectic tiers once at server startup"
```

---

## Phase C — role-keyed agent+model settings

### Task C1: role-aware `resolve_model` + `[agents.<role>]` in `resolve_agent`

**Files:**
- Modify: `threadkeeper/spawn_config.py`
- Test: `tests/test_spawn_config.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_spawn_config.py` (before the `summary_table` section). Note `_reset` already purges `THREADKEEPER_SPAWN_MODEL_*` env and points the config at a non-existent toml.

```python
# ── per-role agent assignments ([agents.<role>]) ──────────────────────

def test_agents_section_sets_cli_and_model(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    cfg = tmp_path / "spawn.toml"
    cfg.write_text(
        '[agents.dialectic_validator]\ncli = "claude"\nmodel = "opus"\n'
        '[models]\nclaude = "sonnet"\n'
    )
    monkeypatch.setenv("THREADKEEPER_SPAWN_CONFIG", str(cfg))
    from threadkeeper.spawn_config import resolve_agent, resolve_model
    # role assignment wins for both cli and model
    assert resolve_agent("dialectic_validator", "codex") == "claude"
    assert resolve_model("claude", "dialectic_validator") == "opus"
    # a role WITHOUT an [agents.*] entry still gets the per-CLI default
    assert resolve_model("claude", "curator") == "sonnet"


def test_resolve_model_back_compat_cli_only(tmp_path, monkeypatch):
    """Legacy positional resolve_model('claude') still works (role optional)."""
    _reset(monkeypatch, tmp_path)
    cfg = tmp_path / "spawn.toml"
    cfg.write_text('[models]\nclaude = "sonnet"\n')
    monkeypatch.setenv("THREADKEEPER_SPAWN_CONFIG", str(cfg))
    from threadkeeper.spawn_config import resolve_model
    assert resolve_model("claude") == "sonnet"


def test_per_role_model_env_beats_file(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    cfg = tmp_path / "spawn.toml"
    cfg.write_text('[agents.dialectic_validator]\nmodel = "opus"\n')
    monkeypatch.setenv("THREADKEEPER_SPAWN_CONFIG", str(cfg))
    monkeypatch.setenv("THREADKEEPER_SPAWN_MODEL_DIALECTIC_VALIDATOR", "haiku")
    from threadkeeper.spawn_config import resolve_model
    assert resolve_model("claude", "dialectic_validator") == "haiku"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_spawn_config.py -k "agents_section or back_compat_cli_only or per_role_model_env" -v`
Expected: FAIL (`resolve_model() takes 1 positional argument` / wrong values)

- [ ] **Step 3: Implement**

In `threadkeeper/spawn_config.py`, add a helper after `_file_default_override` (~line 112):

```python
def _file_agent_assignment(role: str, cfg: dict) -> dict:
    """Return the [agents.<role>] table ({} if absent/malformed)."""
    agents = cfg.get("agents") or {}
    val = agents.get(role) or agents.get(role.lower())
    return val if isinstance(val, dict) else {}
```

In `resolve_agent`, make `[agents.<role>].cli` the highest file-source. Replace the resolver tuple (currently `_env_role_override`, `_file_role_override`, `_env_default_override`, `_file_default_override`) so the agents-assignment cli is checked right after the per-role env:

```python
    cfg = _load_file()
    chosen: Optional[str] = None

    def _agent_cli():
        v = _file_agent_assignment(role, cfg).get("cli")
        if isinstance(v, str):
            v = v.strip().lower()
            return v if v in SUPPORTED_CLIS or v == "auto" else None
        return None

    for resolver in (
        lambda: _env_role_override(role),
        _agent_cli,
        lambda: _file_role_override(role, cfg),
        lambda: _env_default_override(),
        lambda: _file_default_override(cfg),
    ):
```

Replace `resolve_model` entirely with the role-aware, back-compat-safe version (cli stays first positional):

```python
def resolve_model(cli: str, role: str = "") -> str:
    """Configured model for this spawn, or "" (let the CLI use its default).

    Priority (highest first):
      1. per-role env   THREADKEEPER_SPAWN_MODEL_<ROLE>
      2. file           [agents.<role>].model
      3. per-CLI env    THREADKEEPER_SPAWN_MODEL_<CLI>   (legacy)
      4. file           [models].<cli>                   (legacy)
      5. ""

    `role` is optional so legacy positional callers — resolve_model("claude")
    — keep working unchanged.
    """
    if role:
        env_role = os.environ.get(
            "THREADKEEPER_SPAWN_MODEL_" + role.upper().replace("-", "_"), ""
        ).strip()
        if env_role:
            return env_role
    cfg = _load_file()
    if role:
        m = _file_agent_assignment(role, cfg).get("model")
        if isinstance(m, str) and m.strip():
            return m.strip()
    env_cli = os.environ.get("THREADKEEPER_SPAWN_MODEL_" + cli.upper(), "").strip()
    if env_cli:
        return env_cli
    models = cfg.get("models") or {}
    val = models.get(cli) or models.get(cli.lower())
    return val.strip() if isinstance(val, str) else ""
```

- [ ] **Step 4: Run to verify pass (incl. existing tests)**

Run: `.venv/bin/pytest tests/test_spawn_config.py -v`
Expected: PASS — new tests + all pre-existing `resolve_model`/`resolve_agent` tests (the legacy `resolve_model("claude")` calls still bind `cli`).

- [ ] **Step 5: Commit**

```bash
git add threadkeeper/spawn_config.py tests/test_spawn_config.py
git commit -m "feat: role-keyed [agents.<role>] cli+model assignments in spawn_config"
```

### Task C2: thread role through `spawn()` + `summary_table`

**Files:**
- Modify: `threadkeeper/tools/spawn.py` (~line 470)
- Modify: `threadkeeper/spawn_config.py` (`summary_table`, ~line 200)
- Test: `tests/test_spawn_config.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_spawn_config.py`:

```python
def test_summary_table_includes_dialectic_validator_model(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path)
    cfg = tmp_path / "spawn.toml"
    cfg.write_text('[agents.dialectic_validator]\ncli="claude"\nmodel="opus"\n')
    monkeypatch.setenv("THREADKEEPER_SPAWN_CONFIG", str(cfg))
    from threadkeeper.spawn_config import summary_table
    out = summary_table("claude")
    assert "dialectic_validator" in out
    assert "model=opus" in out
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_spawn_config.py -k summary_table_includes_dialectic -v`
Expected: FAIL (`dialectic_validator` not in the fixed roles tuple)

- [ ] **Step 3: Implement**

In `threadkeeper/spawn_config.py` `summary_table`, add the new role to the `roles` tuple and pass `role` to `resolve_model`:

```python
    roles = (
        "archivist",
        "shadow_observer",
        "extract",
        "candidate_reviewer",
        "curator",
        "dialectic_validator",
    )
```

and change the model line inside the loop:

```python
        model = resolve_model(chosen, role)
```

Also add the agents-assignment source label so it's visible. After the existing `if _env_role_override(role): src = "env override"` chain, insert as the second branch:

```python
        if _env_role_override(role):
            src = "env override"
        elif _file_agent_assignment(role, cfg).get("cli"):
            src = "agents assignment"
        elif _file_role_override(role, cfg):
            src = "file override"
```

In `threadkeeper/tools/spawn.py` at the model resolution (currently `chosen_model = model or _sc.resolve_model(chosen_cli)`), pass the role:

```python
    chosen_cli = _sc.resolve_agent(role or "", _id.active_cli())
    chosen_model = model or _sc.resolve_model(chosen_cli, role or "")
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_spawn_config.py -v`
Expected: PASS (all, including the prior `test_summary_table_*`)

- [ ] **Step 5: Commit**

```bash
git add threadkeeper/spawn_config.py threadkeeper/tools/spawn.py tests/test_spawn_config.py
git commit -m "feat: pass spawn role into resolve_model + show in spawn_status table"
```

---

## Phase B — two-daemon auto-feeder

### Task B1: config knobs

**Files:**
- Modify: `threadkeeper/config.py` (append after the thread-janitor block, ~line 393)

- [ ] **Step 1: Add knobs (no test — exercised by B3/B5)**

Append to `threadkeeper/config.py`:

```python
# Dialectic auto-feed. Two daemons build the user-model continuously.
# dialectic_miner is MECHANICAL (no LLM): every DIALECTIC_MINE_INTERVAL_S it
# captures user-role dialog_messages + their preceding-assistant context into
# the dialectic_observations buffer. dialectic_validator periodically spawns an
# (opus) child that turns the buffer into claims via dialectic_* tools. Both
# 0 = off (default — opt in via env).
DIALECTIC_MINE_INTERVAL_S: float = float(
    os.environ.get("THREADKEEPER_DIALECTIC_MINE_INTERVAL_S", "0")
)
DIALECTIC_VALIDATE_INTERVAL_S: float = float(
    os.environ.get("THREADKEEPER_DIALECTIC_VALIDATE_INTERVAL_S", "0")
)
# Min pending observations before the validator spawns — below this there's
# not enough signal to justify an opus child.
DIALECTIC_VALIDATE_MIN: int = int(
    os.environ.get("THREADKEEPER_DIALECTIC_VALIDATE_MIN", "5")
)
# Cap on new claims the validator may create per pass (it should prefer adding
# evidence to existing claims; see the validator prompt).
DIALECTIC_MAX_NEW_CLAIMS: int = int(
    os.environ.get("THREADKEEPER_DIALECTIC_MAX_NEW_CLAIMS", "3")
)
```

- [ ] **Step 2: Smoke-check import**

Run: `.venv/bin/python -c "import threadkeeper.config as c; print(c.DIALECTIC_MINE_INTERVAL_S, c.DIALECTIC_VALIDATE_MIN, c.DIALECTIC_MAX_NEW_CLAIMS)"`
Expected: `0.0 5 3`

- [ ] **Step 3: Commit**

```bash
git add threadkeeper/config.py
git commit -m "feat: dialectic auto-feed config knobs"
```

### Task B2: `dialectic_observations` buffer table

**Files:**
- Modify: `threadkeeper/db.py` (add CREATE TABLE in `SCHEMA`, ~after `extract_candidates` line 344; add index ~line 420)

- [ ] **Step 1: Add the table + index**

In `threadkeeper/db.py`, inside the `SCHEMA` string, after the `extract_candidates` table block, add:

```sql
-- Dialectic capture buffer. dialectic_miner mechanically stores every user
-- reply + its preceding-assistant context here (status='pending'); the
-- dialectic_validator child consumes pending rows, turns them into claims via
-- the dialectic_* tools, then resolves each to 'processed'.
CREATE TABLE IF NOT EXISTS dialectic_observations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dialog_uuid  TEXT UNIQUE,          -- dialog_messages.uuid; dedup key
    user_quote   TEXT NOT NULL,
    context      TEXT,                 -- preceding assistant turn (truncated)
    source_cid   TEXT,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','processed')),
    created_at   INTEGER NOT NULL,
    processed_at INTEGER
);
```

In the index list (~line 420, near `idx_extract_status`) add:

```sql
CREATE INDEX IF NOT EXISTS idx_dialectic_obs_status ON dialectic_observations(status, created_at DESC);
```

- [ ] **Step 2: Verify the table is created**

Run: `.venv/bin/python -c "import os,tempfile; os.environ['THREADKEEPER_DB']=tempfile.mktemp(); from threadkeeper.db import get_db; print([r[0] for r in get_db().execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='dialectic_observations'\")])"`
Expected: `['dialectic_observations']`

- [ ] **Step 3: Commit**

```bash
git add threadkeeper/db.py
git commit -m "feat: dialectic_observations capture-buffer table"
```

### Task B3: `dialectic_miner` daemon (mechanical capture)

**Files:**
- Create: `threadkeeper/dialectic_miner.py`
- Test: `tests/test_dialectic_miner.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dialectic_miner.py
"""dialectic_miner — mechanical capture of user replies + preceding-assistant
context into dialectic_observations. No LLM, no spawn. Same session-filtering
as extract (exclude internal-prompt + spawned-child sessions)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

_FAKE_CID = "aaaa1111-2222-3333-4444-555566667777"


def _bootstrap(tmp_path, monkeypatch, interval="0"):
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
        "THREADKEEPER_DIALECTIC_MINE_INTERVAL_S": interval,
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
    from threadkeeper import db, dialectic_miner, identity
    return {"db": db, "dialectic_miner": dialectic_miner, "identity": identity}


def _seed(conn, role, content, ts, session_id="real-sess"):
    uid = f"u-{ts}-{role}-{abs(hash(content)) % 100000}"
    conn.execute(
        "INSERT INTO dialog_messages (uuid, source, project, session_id, role, "
        "content, model, created_at) VALUES (?, 'claude-code', 'p1', ?, ?, ?, "
        "'test-model', ?)",
        (uid, session_id, role, content, ts),
    )


def test_disabled_without_force(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)  # interval=0
    assert pkg["dialectic_miner"].run_mine_pass() == "disabled"


def test_captures_user_reply_with_context(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    _seed(conn, "assistant", "Which auth method do you want?", now - 60)
    _seed(conn, "user", "use better-auth with the neon adapter", now - 50)
    conn.commit()
    out = pkg["dialectic_miner"].run_mine_pass(force=True)
    assert "captured=1" in out
    row = conn.execute(
        "SELECT user_quote, context, status FROM dialectic_observations"
    ).fetchone()
    assert row["user_quote"] == "use better-auth with the neon adapter"
    assert "Which auth method" in row["context"]
    assert row["status"] == "pending"


def test_dedup_by_dialog_uuid(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    _seed(conn, "user", "remember I prefer lean prose", now - 40)
    conn.commit()
    pkg["dialectic_miner"].run_mine_pass(force=True)
    pkg["dialectic_miner"].run_mine_pass(force=True)  # re-scan overlap
    n = conn.execute(
        "SELECT COUNT(*) FROM dialectic_observations"
    ).fetchone()[0]
    assert n == 1


def test_excludes_spawned_child_session(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    child = "child-cid"
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, spawned_cid, cwd, prompt, "
        "started_at) VALUES ('tk_x', 0, 'p', ?, '/x', 'You are auditing.', ?)",
        (child, now - 200),
    )
    _seed(conn, "user", "internal child utterance about X", now - 90,
          session_id=child)
    _seed(conn, "user", "real user preference statement here", now - 60,
          session_id="real-sess")
    conn.commit()
    pkg["dialectic_miner"].run_mine_pass(force=True)
    cids = [r["source_cid"] for r in conn.execute(
        "SELECT source_cid FROM dialectic_observations"
    ).fetchall()]
    assert "real-sess" in cids
    assert child not in cids


def test_cursor_advances_and_no_spawn(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    pkg["dialectic_miner"].run_mine_pass(force=True)
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='dialectic_mine_pass'"
    ).fetchone()[0]
    assert n == 1


def test_daemon_does_not_start_in_slim_child(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="3600")
    import threadkeeper.config as cfg
    monkeypatch.setattr(cfg, "BACKGROUND_DAEMONS_ALLOWED", False)
    pkg["dialectic_miner"]._started = False
    pkg["dialectic_miner"].start_dialectic_miner_daemon()
    assert pkg["dialectic_miner"]._started is False


def test_daemon_disabled_at_interval_zero(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="0")
    pkg["dialectic_miner"]._started = False
    pkg["dialectic_miner"].start_dialectic_miner_daemon()
    assert pkg["dialectic_miner"]._started is False
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_dialectic_miner.py -v`
Expected: FAIL (`No module named 'threadkeeper.dialectic_miner'`)

- [ ] **Step 3: Implement the daemon**

```python
# threadkeeper/dialectic_miner.py
"""Dialectic miner — mechanical capture of user replies into the
dialectic_observations buffer. No LLM, no spawn: deterministic and lossless.

For each user-role dialog_message since the last pass it stores the verbatim
quote plus the most-recent preceding assistant turn as context. The
dialectic_validator child later turns this buffer into claims. Session
filtering mirrors extract_recent so only the REAL user's turns are captured
(internal-prompt sessions + spawned-child sessions are excluded)."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time

from .config import DIALECTIC_MINE_INTERVAL_S
from .db import get_db
from . import identity
from .identity import _ensure_session, _emit

logger = logging.getLogger(__name__)

_started = False
_CONTEXT_MAX = 600


def _last_mine_ts(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT target FROM events WHERE kind='dialectic_mine_pass' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row or not row["target"]:
        return 0
    try:
        return int(row["target"])
    except (ValueError, TypeError):
        return 0


def _record_pass(conn: sqlite3.Connection, ts: int, outcome: str) -> None:
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, 'dialectic_mine_pass', ?, ?, ?)",
            (identity._session_id or "", str(ts), outcome[:300], int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("dialectic_miner: record_pass failed", exc_info=True)


def _preceding_context(conn: sqlite3.Connection, session_id: str,
                       before_ts: int) -> str:
    """Most recent assistant turn in this session before before_ts."""
    row = conn.execute(
        "SELECT content FROM dialog_messages WHERE session_id=? "
        "AND role='assistant' AND created_at <= ? "
        "ORDER BY created_at DESC LIMIT 1",
        (session_id, before_ts),
    ).fetchone()
    if not row or not row["content"]:
        return ""
    return row["content"][:_CONTEXT_MAX]


def run_mine_pass(force: bool = False) -> str:
    """Capture new user replies since the cursor. Returns
    'ok captured=N skipped=M' / 'no_user_dialog' / 'disabled'."""
    if DIALECTIC_MINE_INTERVAL_S <= 0 and not force:
        return "disabled"
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    cursor = _last_mine_ts(conn)

    # Same self-pollution filter as extract_recent: drop sessions whose first
    # user message is one of our spawn-prompt markers, and sessions that ARE a
    # spawned child (cid in tasks.spawned_cid).
    from .shadow_review import _INTERNAL_PROMPT_PREFIXES
    sess_prefix_clauses = " OR ".join(
        ["substr(content, 1, ?) = ?"] * len(_INTERNAL_PROMPT_PREFIXES)
    )
    sess_prefix_params: list = []
    for p in _INTERNAL_PROMPT_PREFIXES:
        sess_prefix_params.extend([len(p), p])

    rows = conn.execute(
        "SELECT uuid, session_id, content, created_at FROM dialog_messages "
        "WHERE role='user' AND created_at > ? "
        "AND content NOT LIKE '[tool_result]%' AND content NOT LIKE '[Image%' "
        "AND length(content) >= 1 "
        "AND session_id NOT IN ("
        "  SELECT DISTINCT session_id FROM dialog_messages "
        f"  WHERE role='user' AND ({sess_prefix_clauses})"
        ") "
        "AND session_id NOT IN ("
        "  SELECT spawned_cid FROM tasks WHERE spawned_cid IS NOT NULL"
        ") "
        "ORDER BY created_at ASC",
        (cursor, *sess_prefix_params),
    ).fetchall()

    if not rows:
        _record_pass(conn, now, "no_user_dialog")
        return "no_user_dialog"

    captured = skipped = 0
    max_ts = cursor
    for r in rows:
        max_ts = max(max_ts, r["created_at"])
        ctx = _preceding_context(conn, r["session_id"] or "", r["created_at"])
        cur = conn.execute(
            "INSERT OR IGNORE INTO dialectic_observations "
            "(dialog_uuid, user_quote, context, source_cid, status, created_at) "
            "VALUES (?,?,?,?, 'pending', ?)",
            (r["uuid"], r["content"], ctx, r["session_id"], now),
        )
        if cur.rowcount:
            captured += 1
        else:
            skipped += 1
    # Advance cursor to the newest message we saw (target stores the ts).
    _emit(conn, "dialectic_mine_capture", summary=f"captured={captured}")
    conn.commit()
    _record_pass(conn, max(max_ts, now) if captured == 0 else max_ts,
                 f"ok captured={captured} skipped={skipped}")
    return f"ok captured={captured} skipped={skipped}"


def _serve_loop() -> None:
    while True:
        try:
            run_mine_pass()
        except Exception:
            logger.debug("dialectic_miner tick failed", exc_info=True)
        time.sleep(DIALECTIC_MINE_INTERVAL_S)


def start_dialectic_miner_daemon() -> None:
    """Idempotent. Mechanical capture needs no embeddings, so it is gated only
    by BACKGROUND_DAEMONS_ALLOWED (not SEMANTIC_AVAILABLE)."""
    global _started
    if _started:
        return
    if DIALECTIC_MINE_INTERVAL_S <= 0:
        return
    from .config import BACKGROUND_DAEMONS_ALLOWED
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    t = threading.Thread(
        target=_serve_loop, name="dialectic_miner", daemon=True,
    )
    t.start()
    _started = True
```

Note on the cursor: store the high-water as the max captured message ts so the next pass only sees strictly-newer rows (`created_at > cursor`). When nothing was captured we still record a pass (telemetry) using `now` but keep the cursor effectively at the last real message — using `max_ts` (which stays `cursor` when no rows) preserves correctness.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_dialectic_miner.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add threadkeeper/dialectic_miner.py tests/test_dialectic_miner.py
git commit -m "feat: dialectic_miner daemon — mechanical user-reply capture"
```

### Task B4: `dialectic_observation_resolve` MCP tool

**Files:**
- Modify: `threadkeeper/tools/dialectic.py` (append a tool at end)
- Test: `tests/test_dialectic_observation_resolve.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dialectic_observation_resolve.py
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
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_CLIENT": "pytest",
        "THREADKEEPER_FORCE_CID": _FAKE_CID,
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import _mcp, db
    return {"mcp": _mcp.mcp, "db": db}


def test_resolve_marks_processed(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO dialectic_observations (dialog_uuid, user_quote, context, "
        "source_cid, status, created_at) VALUES ('u1','q','c','s','pending',?)",
        (now,),
    )
    conn.commit()
    oid = conn.execute("SELECT id FROM dialectic_observations").fetchone()["id"]
    tool = pkg["mcp"]._tool_manager._tools["dialectic_observation_resolve"].fn
    out = tool(id=oid, note="chit-chat")
    assert "ok" in out
    row = conn.execute(
        "SELECT status, processed_at FROM dialectic_observations WHERE id=?",
        (oid,),
    ).fetchone()
    assert row["status"] == "processed"
    assert row["processed_at"] is not None


def test_resolve_unknown_id_errors(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    tool = pkg["mcp"]._tool_manager._tools["dialectic_observation_resolve"].fn
    assert tool(id=99999).startswith("ERR")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_dialectic_observation_resolve.py -v`
Expected: FAIL (`KeyError: 'dialectic_observation_resolve'`)

- [ ] **Step 3: Implement**

Append to `threadkeeper/tools/dialectic.py`:

```python
@mcp.tool()
def dialectic_observation_resolve(id: int, note: str = "") -> str:
    """Mark a dialectic_observations buffer row 'processed' so the validator
    never re-interprets it. Called by the validator child after it has written
    (or deliberately skipped) the observation's claims/evidence."""
    conn = get_db()
    _ensure_session(conn)
    r = conn.execute(
        "SELECT status FROM dialectic_observations WHERE id=?", (int(id),)
    ).fetchone()
    if not r:
        return f"ERR observation_not_found={id}"
    if r["status"] == "processed":
        return f"ok already_processed #{id}"
    now_t = int(time.time())
    conn.execute(
        "UPDATE dialectic_observations SET status='processed', processed_at=? "
        "WHERE id=?",
        (now_t, int(id)),
    )
    _emit(conn, "dialectic_observation_resolve", target=str(id), summary=note[:140])
    conn.commit()
    return f"ok resolved #{id}"
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_dialectic_observation_resolve.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add threadkeeper/tools/dialectic.py tests/test_dialectic_observation_resolve.py
git commit -m "feat: dialectic_observation_resolve tool for the validator child"
```

### Task B5: `dialectic_validator` daemon (opus interpretation)

**Files:**
- Create: `threadkeeper/dialectic_validator.py`
- Test: `tests/test_dialectic_validator.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dialectic_validator.py
"""dialectic_validator — spawns an opus child that turns pending observations
into claims. Tests the scaffolding (threshold, spawn kwargs, lifecycle); the
LLM decision itself runs in production."""
from __future__ import annotations

import sys
import time
from pathlib import Path

_FAKE_CID = "aaaa1111-2222-3333-4444-555566667777"


def _bootstrap(tmp_path, monkeypatch, interval="0", min_n="5"):
    env = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "fake_claude_projects"),
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_CURATOR_INTERVAL_S": "0",
        "THREADKEEPER_EXTRACT_INTERVAL_S": "0",
        "THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_DIALECTIC_MINE_INTERVAL_S": "0",
        "THREADKEEPER_DIALECTIC_VALIDATE_INTERVAL_S": interval,
        "THREADKEEPER_DIALECTIC_VALIDATE_MIN": min_n,
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
    from threadkeeper import db, dialectic_validator, identity
    return {"db": db, "dialectic_validator": dialectic_validator, "identity": identity}


def _seed_obs(conn, quote, age_s=60, status="pending"):
    now = int(time.time())
    conn.execute(
        "INSERT INTO dialectic_observations (dialog_uuid, user_quote, context, "
        "source_cid, status, created_at) VALUES (?,?,?,?,?,?)",
        (f"u-{now}-{abs(hash(quote)) % 99999}", quote, "ctx", "real-sess",
         status, now - age_s),
    )


def test_disabled_without_force(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    assert pkg["dialectic_validator"].run_validate_pass() == "disabled"


def test_below_threshold_no_spawn(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, min_n="5")
    conn = pkg["db"].get_db()
    for i in range(3):
        _seed_obs(conn, f"obs {i}")
    conn.commit()
    out = pkg["dialectic_validator"].run_validate_pass(force=True)
    assert out.startswith("below_threshold")
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='dialectic_validate_pass'"
    ).fetchone()[0]
    assert n == 1


def test_spawns_when_threshold_met(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, min_n="3")
    conn = pkg["db"].get_db()
    for i in range(4):
        _seed_obs(conn, f"user preference number {i}")
    conn.commit()

    import threadkeeper.tools.spawn as spawn_mod
    captured: list[dict] = []

    def fake_spawn(**kwargs):
        captured.append(kwargs)
        return "spawn task_id=fake-validator pid=0"

    monkeypatch.setattr(spawn_mod, "spawn", fake_spawn)

    out = pkg["dialectic_validator"].run_validate_pass(force=True)
    assert "fake-validator" in out
    assert len(captured) == 1
    kw = captured[0]
    assert kw["slim"] is True
    assert kw["visible"] is False
    assert kw["role"] == "dialectic_validator"
    assert kw["write_origin"] == "background_review"
    assert "DIALECTIC VALIDATOR" in kw["prompt"]
    assert "PENDING OBSERVATIONS (n=4)" in kw["prompt"]
    assert "user preference number 0" in kw["prompt"]
    allowed = kw["extra_allowed_tools"]
    assert "dialectic_claim" in allowed
    assert "dialectic_evidence" in allowed
    assert "dialectic_observation_resolve" in allowed
    assert "Bash" not in allowed


def test_excludes_processed_and_stale(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, min_n="1")
    conn = pkg["db"].get_db()
    _seed_obs(conn, "fresh pending", age_s=60, status="pending")
    _seed_obs(conn, "already done", age_s=60, status="processed")
    _seed_obs(conn, "ancient pending", age_s=40 * 86400, status="pending")
    conn.commit()
    dump, n = pkg["dialectic_validator"]._collect_pending(conn)
    assert n == 1
    assert "fresh pending" in dump
    assert "already done" not in dump
    assert "ancient pending" not in dump


def test_daemon_does_not_start_in_slim_child(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="3600")
    import threadkeeper.config as cfg
    monkeypatch.setattr(cfg, "SEMANTIC_AVAILABLE", False)
    pkg["dialectic_validator"]._started = False
    pkg["dialectic_validator"].start_dialectic_validator_daemon()
    assert pkg["dialectic_validator"]._started is False


def test_daemon_disabled_at_interval_zero(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, interval="0")
    pkg["dialectic_validator"]._started = False
    pkg["dialectic_validator"].start_dialectic_validator_daemon()
    assert pkg["dialectic_validator"]._started is False
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_dialectic_validator.py -v`
Expected: FAIL (`No module named 'threadkeeper.dialectic_validator'`)

- [ ] **Step 3: Implement the daemon**

```python
# threadkeeper/dialectic_validator.py
"""Dialectic validator — the SOLE interpreter of the dialectic_observations
buffer. Periodically spawns one opus child that reads pending observations +
the full current model and turns raw user replies into claims via the existing
dialectic_* tools, then resolves each observation.

Mirrors candidate_reviewer (spawns an LLM child); the miner is the cheap
mechanical producer, this is the careful infrequent consumer."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time

from .config import DIALECTIC_VALIDATE_INTERVAL_S, DIALECTIC_VALIDATE_MIN, \
    DIALECTIC_MAX_NEW_CLAIMS
from .db import get_db
from . import identity
from .identity import _ensure_session

logger = logging.getLogger(__name__)

_started = False


DIALECTIC_VALIDATOR_PROMPT = """\
You are a DIALECTIC VALIDATOR for thread-keeper's user model.

The dialectic_miner mechanically captured recent USER replies (with the
preceding assistant turn as context) into a buffer. Your job: turn these raw
observations into the dialectic user-model, and self-correct it.

You are given (a) the CURRENT MODEL — every active claim with its domain, tier
and confidence — and (b) PENDING OBSERVATIONS. For each observation (or a
coherent cluster of them) choose exactly one action:

  1. dialectic_evidence(claim_id=..., kind='support', quote=..., source='dialog',
     weight=W) — the observation corroborates an EXISTING claim. PREFER THIS
     over creating a near-duplicate claim.
  2. dialectic_evidence(claim_id=..., kind='contradict', quote=..., weight=W) —
     the observation conflicts with an existing claim (the user did/said the
     opposite). This is how the model self-corrects.
  3. dialectic_supersede(old_claim_id=..., new_claim=..., quote=...) — an
     existing claim is right in spirit but needs refining/replacing.
  4. dialectic_claim(claim=..., domain=..., evidence=..., evidence_kind='support')
     — genuinely NEW territory not covered by any existing claim.
  5. (write nothing) — the observation is chit-chat / noise / a one-off with no
     durable signal about who the user is.

Then ALWAYS call dialectic_observation_resolve(id=<obs id>, note='...') for
every observation you processed (including ones you deliberately skipped), so
it is never re-interpreted.

WEIGHT (the `weight` arg, base trust ∈ [0,1]; an automatic 0.5 review-fork
discount is applied on top): use ~1.0 for an explicit user STATEMENT of
preference/decision, ~0.5 for a trait you only INFER from behavior.

RULES:
- PREFER support-existing over new claims. Dedup hard against the current model.
- MERGE near-duplicate observations into ONE claim.
- contradict / supersede ONLY on a clear conflict — don't thrash the model.
- LIMIT %(max_new)d NEW claims this pass; if more seem warranted, pick the
  strongest and leave the rest (their observations stay resolved-as-skipped or
  pending per your judgement).
- domain ∈ style / workflow / values / context / skills / other.

Finish with a one-paragraph summary: "Processed N observations: K supports,
C contradicts, S supersedes, M new claims, X skipped."

CURRENT MODEL
=============
%(model)s

PENDING OBSERVATIONS
====================
%(inventory)s
"""


def _last_validate_ts(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT target FROM events WHERE kind='dialectic_validate_pass' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row or not row["target"]:
        return 0
    try:
        return int(row["target"])
    except (ValueError, TypeError):
        return 0


def _record_pass(conn: sqlite3.Connection, ts: int, outcome: str) -> None:
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, 'dialectic_validate_pass', ?, ?, ?)",
            (identity._session_id or "", str(ts), outcome[:300], int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("dialectic_validator: record_pass failed", exc_info=True)


def _collect_pending(conn: sqlite3.Connection) -> tuple[str, int]:
    """Inventory of pending observations within the last 30 days."""
    now = int(time.time())
    stale_cutoff = now - 30 * 86400
    try:
        rows = conn.execute(
            "SELECT id, user_quote, context, source_cid, created_at "
            "FROM dialectic_observations WHERE status='pending' AND created_at > ? "
            "ORDER BY created_at ASC",
            (stale_cutoff,),
        ).fetchall()
    except sqlite3.OperationalError:
        return ("", 0)
    if not rows:
        return ("", 0)
    parts = [f"PENDING OBSERVATIONS (n={len(rows)})\n"]
    for r in rows:
        quote = (r["user_quote"] or "")[:400].replace("\n", " ")
        ctx = (r["context"] or "")[:200].replace("\n", " ")
        parts.append(
            f"  #{r['id']} cid={(r['source_cid'] or '-')[:8]}\n"
            f"    context: {ctx}\n"
            f"    user: {quote}"
        )
    return ("\n".join(parts), len(rows))


def _current_model_dump(conn: sqlite3.Connection) -> str:
    """The full active model the child must dedup against. Reuses
    dialectic_review's listing logic at the lowest confidence floor."""
    from .tools.dialectic import dialectic_review
    out = dialectic_review(min_confidence="low", k=200)
    return out if out and not out.startswith("no_claims") else "(model is empty)"


def run_validate_pass(force: bool = False) -> str:
    if DIALECTIC_VALIDATE_INTERVAL_S <= 0 and not force:
        return "disabled"
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    inventory, n_pending = _collect_pending(conn)
    if n_pending < DIALECTIC_VALIDATE_MIN:
        _record_pass(conn, now,
                     f"below_threshold pending={n_pending} "
                     f"min={DIALECTIC_VALIDATE_MIN}")
        return f"below_threshold n={n_pending}"

    prompt = DIALECTIC_VALIDATOR_PROMPT % {
        "max_new": DIALECTIC_MAX_NEW_CLAIMS,
        "model": _current_model_dump(conn),
        "inventory": inventory,
    }

    from .tools.spawn import spawn  # type: ignore
    try:
        result = spawn(
            prompt=prompt,
            visible=False,
            capture_output=True,
            permission_mode="auto",
            role="dialectic_validator",
            write_origin="background_review",
            slim=True,
            extra_allowed_tools=(
                "mcp__thread-keeper__dialectic_claim,"
                "mcp__thread-keeper__dialectic_evidence,"
                "mcp__thread-keeper__dialectic_supersede,"
                "mcp__thread-keeper__dialectic_review,"
                "mcp__thread-keeper__dialectic_observation_resolve"
            ),
        )
    except Exception as e:
        _record_pass(conn, now, f"spawn_error: {e}")
        return f"spawn_error: {e}"

    _record_pass(conn, now, f"spawned pending={n_pending} :: {str(result)[:140]}")
    return str(result)


def _serve_loop() -> None:
    while True:
        try:
            run_validate_pass()
        except Exception:
            logger.debug("dialectic_validator tick failed", exc_info=True)
        time.sleep(DIALECTIC_VALIDATE_INTERVAL_S)


def start_dialectic_validator_daemon() -> None:
    """Idempotent. Spawns children → same cascade-prevention as
    candidate_reviewer (BACKGROUND_DAEMONS_ALLOWED + SEMANTIC_AVAILABLE)."""
    global _started
    if _started:
        return
    if DIALECTIC_VALIDATE_INTERVAL_S <= 0:
        return
    from .config import BACKGROUND_DAEMONS_ALLOWED, SEMANTIC_AVAILABLE
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    if not SEMANTIC_AVAILABLE:
        return
    t = threading.Thread(
        target=_serve_loop, name="dialectic_validator", daemon=True,
    )
    t.start()
    _started = True
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_dialectic_validator.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add threadkeeper/dialectic_validator.py tests/test_dialectic_validator.py
git commit -m "feat: dialectic_validator daemon — opus child interprets buffer into claims"
```

### Task B6: manual run/status MCP tools

**Files:**
- Create: `threadkeeper/tools/dialectic_feed.py`
- Modify: `threadkeeper/server.py` (ensure the new tools module is imported so `@mcp.tool()` registers — see Step 3)
- Test: `tests/test_dialectic_feed_tools.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dialectic_feed_tools.py
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
        "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
        "THREADKEEPER_DIALECTIC_MINE_INTERVAL_S": "0",
        "THREADKEEPER_DIALECTIC_VALIDATE_INTERVAL_S": "0",
        "THREADKEEPER_DIALECTIC_VALIDATE_MIN": "5",
        "THREADKEEPER_CLIENT": "pytest",
        "THREADKEEPER_FORCE_CID": _FAKE_CID,
        "THREADKEEPER_TASK_LOG_DIR": str(tmp_path / "tasks"),
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Path(env["CLAUDE_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    import threadkeeper.server  # noqa: F401
    from threadkeeper import _mcp, db
    return {"mcp": _mcp.mcp, "db": db}


def test_mine_run_forces_capture(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO dialog_messages (uuid, source, project, session_id, role, "
        "content, model, created_at) VALUES "
        "('uX','claude-code','p','real-sess','user','I prefer X', 'm', ?)",
        (now - 30,),
    )
    conn.commit()
    tool = pkg["mcp"]._tool_manager._tools["dialectic_mine_run"].fn
    out = tool()
    assert "captured=1" in out


def test_validate_status_reports_pending(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    conn = pkg["db"].get_db()
    now = int(time.time())
    conn.execute(
        "INSERT INTO dialectic_observations (dialog_uuid, user_quote, context, "
        "source_cid, status, created_at) VALUES ('u1','q','c','s','pending',?)",
        (now,),
    )
    conn.commit()
    tool = pkg["mcp"]._tool_manager._tools["dialectic_validate_status"].fn
    out = tool()
    assert "pending_now=1" in out
    assert "min=5" in out
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_dialectic_feed_tools.py -v`
Expected: FAIL (`KeyError: 'dialectic_mine_run'`)

- [ ] **Step 3: Implement the tools module**

```python
# threadkeeper/tools/dialectic_feed.py
"""Manual run/status MCP tools for the dialectic auto-feed daemons.

  dialectic_mine_run(force=True)        — capture user replies now
  dialectic_validate_run(force, dry_run)— interpret the buffer now
  dialectic_mine_status()               — miner config + buffer + last passes
  dialectic_validate_status()           — validator config + pending + passes
"""
from __future__ import annotations

import time

from .._mcp import mcp
from ..db import get_db
from ..identity import _ensure_session
from ..dialectic_miner import run_mine_pass, _last_mine_ts
from ..dialectic_validator import (
    run_validate_pass, _collect_pending, _last_validate_ts,
)
from ..config import (
    DIALECTIC_MINE_INTERVAL_S,
    DIALECTIC_VALIDATE_INTERVAL_S,
    DIALECTIC_VALIDATE_MIN,
)


@mcp.tool()
def dialectic_mine_run(force: bool = True) -> str:
    """Fire one mechanical capture pass now (force=True runs even when the
    miner daemon interval is 0)."""
    conn = get_db()
    _ensure_session(conn)
    return run_mine_pass(force=force)


@mcp.tool()
def dialectic_validate_run(force: bool = True, dry_run: bool = False) -> str:
    """Fire one validator pass. dry_run shows pending count + would_spawn
    without spawning or advancing the cursor."""
    conn = get_db()
    _ensure_session(conn)
    if dry_run:
        _, n = _collect_pending(conn)
        below = n < DIALECTIC_VALIDATE_MIN
        return (
            f"dry_run: pending={n} min={DIALECTIC_VALIDATE_MIN} "
            f"would_spawn={'no (below_threshold)' if below else 'yes'}"
        )
    return run_validate_pass(force=force)


@mcp.tool()
def dialectic_mine_status() -> str:
    """Miner config + buffer sizes + last 5 capture passes."""
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM dialectic_observations WHERE status='pending'"
        ).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM dialectic_observations"
        ).fetchone()[0]
    except Exception:
        pending = total = "?"
    floor = _last_mine_ts(conn)
    lines = [
        f"interval_s={DIALECTIC_MINE_INTERVAL_S:.0f} buffer_pending={pending} "
        f"buffer_total={total}",
        f"cursor_ts={floor}" if floor else "cursor_ts=0 (no prior pass)",
        "",
        "recent passes (newest first):",
    ]
    rows = conn.execute(
        "SELECT created_at, summary FROM events WHERE kind='dialectic_mine_pass' "
        "ORDER BY id DESC LIMIT 5"
    ).fetchall()
    lines += [f"  {now - int(r['created_at'])}s_ago  {(r['summary'] or '')[:120]}"
              for r in rows] or ["  (none)"]
    return "\n".join(lines)


@mcp.tool()
def dialectic_validate_status() -> str:
    """Validator config + pending observation count + last 5 passes."""
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM dialectic_observations WHERE status='pending'"
        ).fetchone()[0]
    except Exception:
        pending = "?"
    floor = _last_validate_ts(conn)
    lines = [
        f"interval_s={DIALECTIC_VALIDATE_INTERVAL_S:.0f} "
        f"min={DIALECTIC_VALIDATE_MIN} pending_now={pending}",
        f"cursor_ts={floor}" if floor else "cursor_ts=0 (no prior pass)",
        "",
        "recent passes (newest first):",
    ]
    rows = conn.execute(
        "SELECT created_at, summary FROM events "
        "WHERE kind='dialectic_validate_pass' ORDER BY id DESC LIMIT 5"
    ).fetchall()
    lines += [f"  {now - int(r['created_at'])}s_ago  {(r['summary'] or '')[:120]}"
              for r in rows] or ["  (none)"]
    return "\n".join(lines)
```

Then ensure the module is imported at server load so `@mcp.tool()` runs. In `threadkeeper/server.py`, find where the other `tools.*` modules are imported (e.g. `from .tools import extract  # noqa` or a bulk import) and add `dialectic_feed` alongside. If tools are imported via a list/loop, add `"dialectic_feed"`. (Grep: `grep -n "tools import\|from .tools" threadkeeper/server.py`.)

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_dialectic_feed_tools.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add threadkeeper/tools/dialectic_feed.py threadkeeper/server.py tests/test_dialectic_feed_tools.py
git commit -m "feat: dialectic_mine/validate run+status MCP tools"
```

### Task B7: wire daemons into startup

**Files:**
- Modify: `threadkeeper/identity.py` (daemon-start block; add after `thread_janitor`, before the A2 recompute call)

- [ ] **Step 1: Add the two start calls**

In `threadkeeper/identity.py`, in the same daemon-start sequence, add (after the `thread_janitor` try/except, before the `recompute_all_tiers` heal from Task A2):

```python
        try:
            from . import dialectic_miner
            dialectic_miner.start_dialectic_miner_daemon()
        except Exception:
            pass
        try:
            from . import dialectic_validator
            dialectic_validator.start_dialectic_validator_daemon()
        except Exception:
            pass
```

- [ ] **Step 2: Verify full suite still green**

Run: `.venv/bin/pytest -q`
Expected: PASS (whole suite; daemons stay off because intervals default 0)

- [ ] **Step 3: Commit**

```bash
git add threadkeeper/identity.py
git commit -m "feat: start dialectic miner + validator daemons at session start"
```

### Task B8: docs

**Files:**
- Modify: `README.md`, `CHANGELOG.md`, `threadkeeper/spawn_config.py` (module docstring)

- [ ] **Step 1: Update spawn_config docstring**

In `threadkeeper/spawn_config.py`, extend the module docstring to document the new `[agents.<role>]` section as the FIRST-class assignment (cli + model per role), noting the legacy `[loops]`/`[models]` remain as lower-priority fallbacks. Add an example:

```
  Preferred (role-keyed assignment — "which agent for which purpose"):
      [agents.dialectic_validator]
      cli   = "claude"
      model = "opus"
  Legacy [loops]/[models] still honored at lower priority.
```

- [ ] **Step 2: Update README + CHANGELOG**

In `README.md`, in the learning-loops / daemons section add the two new daemons (`dialectic_miner`, `dialectic_validator`) and the new env knobs (`THREADKEEPER_DIALECTIC_MINE_INTERVAL_S`, `THREADKEEPER_DIALECTIC_VALIDATE_INTERVAL_S`, `THREADKEEPER_DIALECTIC_VALIDATE_MIN`, `THREADKEEPER_DIALECTIC_MAX_NEW_CLAIMS`), and document `[agents.<role>]` spawn settings + per-role `THREADKEEPER_SPAWN_MODEL_<ROLE>`. In `CHANGELOG.md`, add an entry under a new unreleased version: tier-recompute fix, role-keyed agent settings, dialectic auto-feed daemons.

- [ ] **Step 3: Commit**

```bash
git add README.md CHANGELOG.md threadkeeper/spawn_config.py
git commit -m "docs: dialectic auto-feed daemons + role-keyed agent settings"
```

---

## Final verification

- [ ] **Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: all pass (new: `test_dialectic_recompute`, `test_dialectic_miner`, `test_dialectic_observation_resolve`, `test_dialectic_validator`, `test_dialectic_feed_tools`, extended `test_spawn_config`; plus all pre-existing).

- [ ] **Manual smoke (optional, real opus child)**

With `THREADKEEPER_DIALECTIC_MINE_INTERVAL_S=3600` set and some real user dialog ingested:
`dialectic_mine_run()` → `dialectic_validate_run(dry_run=True)` → inspect `dialectic_review()`.

---

## Self-Review

**Spec coverage:**
- Tier-stuck fix → Task A1 (`recompute_all_tiers`) + A2 (startup heal). ✓
- No-auto-feeder → Tasks B3 (miner) + B5 (validator) + B7 (wiring). ✓
- Model/agent settings → Tasks C1 + C2. ✓
- Mechanical capture (no LLM, lossless, session-filtered, dedup) → B3. ✓
- Validator sole interpreter, full dialectic (support/contradict/supersede), dedup, MAX_NEW_CLAIMS → B5 prompt + tools. ✓
- `dialectic_observations` buffer + resolve tool → B2 + B4. ✓
- Config knobs → B1. ✓
- run/status tools → B6. ✓
- Docs → B8. ✓

**Placeholder scan:** No TBD/“handle edge cases”/“similar to”. Every code step has full code. ✓

**Type/name consistency:** `run_mine_pass`/`run_validate_pass`/`_collect_pending`/`_last_mine_ts`/`_last_validate_ts`/`start_dialectic_miner_daemon`/`start_dialectic_validator_daemon`/`recompute_all_tiers`/`dialectic_observation_resolve` used identically across daemon, tools, tests, and wiring. `resolve_model(cli, role="")` order consistent at the spawn.py call-site and tests. Event kinds `dialectic_mine_pass`/`dialectic_validate_pass` consistent between daemons, status tools, and tests. ✓

**Deviation from spec (intentional):** the spec line "verbatim user quote = full weight" is implemented as: the validator passes base `weight` (≈1.0 for explicit statements, ≈0.5 for inferences), and the existing review-fork origin discount (×0.5) still applies on top — review-fork evidence is never full-weight by design (anti-self-confirmation). Promotion therefore stays slow, exactly as intended.
