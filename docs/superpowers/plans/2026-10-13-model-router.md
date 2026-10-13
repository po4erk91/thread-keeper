# Model Router Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a weekly `model_router` loop that picks CLI + model + effort for every role set to `THREADKEEPER_SPAWN__LOOP__<ROLE>=auto`, with a Sol fallback when the Opus run fails.

**Architecture:** Decisions and pass state live in two additive SQLite tables (`model_routes`, `model_router_passes`) behind `threadkeeper/model_routes.py`. `spawn_config.resolve_*` consults the latest valid route only for roles on `auto`. An hourly-ticking daemon (`threadkeeper/model_router.py`) builds a deterministic telemetry digest (`threadkeeper/model_router_digest.py`), spawns the router child, and the child records its decisions through one validated MCP tool (`threadkeeper/tools/model_router.py`).

**Tech Stack:** Python 3 (pydantic-settings, sqlite3, FastMCP), pytest, Swift/SwiftUI menu-bar app.

**Spec:** `docs/superpowers/specs/2026-10-12-model-router-design.md`

## Global Constraints

- Router primary defaults: CLI `claude`, model `opus`, effort `max` (code-level `ROLE_DEFAULTS`; the full `claude-opus-5-5` ID is reported `unrecognized_model` by the installed Claude Code 2.1.278).
- Fallback defaults: `THREADKEEPER_MODEL_ROUTER_FALLBACK_CLI=codex`, `…_FALLBACK_MODEL=gpt-6.1-sol`, `…_FALLBACK_EFFORT=xhigh`.
- `THREADKEEPER_MODEL_ROUTER_INTERVAL_S` default `604800`; `<= 0` disables the loop and `auto` keeps its pre-router meaning (follow the active CLI).
- Hourly tick `_TICK_S = 3600`; failed-pass backoff `_BACKOFF_S = 6 * 3600`.
- Digest: 30-day window, weekly buckets, 5 outcome samples capped at 240 characters each, whole digest capped at 60 000 characters.
- Decision `reason`: 1–500 characters.
- Schema changes go only into `db.ADDITIVE_RUNTIME_SCHEMA` as `CREATE ... IF NOT EXISTS`; no `CURRENT_SCHEMA_VERSION` bump. The new tables are not added to sync.
- Only roles whose raw `spawn.loop[role]` is `auto` are routed; `spawn.default=auto` keeps its meaning; `model_router` is never routed.
- README and `docs/ARCHITECTURE.md` must not gain hand-maintained tool or test totals (`tests/test_documentation_inventory.py`); every new MCP tool must appear in the ARCHITECTURE tool table in the same task that registers it.
- Code style (user rule): small single-purpose functions, descriptive names, no `if` nesting deeper than two levels, comments only for constraints or rationale.
- Commits: Conventional Commits, no AI attribution or co-author lines. Never `git add` anything under `.claude/` or the stray untracked `0` file at the repo root; add files by explicit path.
- Run tests with `.venv/bin/python -m pytest <paths> -q` from the repo root.

## Review Focus

1. **Mixed-case or padded `auto` in `.env`** (`" Auto "`) — a person expects the role to be routed exactly like `auto`. Pinned by `test_roles_on_auto_normalizes_case_and_space` (Task 1) and `test_padded_auto_is_routed` (Task 3).
2. **Model ID with different case** (`GPT-5.6-Luna`) in a router decision — a person expects a clear rejection naming the problem, not a silent write of a model the CLI will refuse. Pinned by `test_decision_model_ids_are_case_sensitive` (Task 2).
3. **Decisions passed as a JSON-encoded string** instead of an array (common from Codex tool calls) — a person expects the tool to accept it. Pinned by `test_write_accepts_json_string_decisions` (Task 5).
4. **Child records its decisions and then exits non-zero** — a person expects the week's decision to stand, not a fallback rerun. Pinned by `test_recorded_pass_does_not_fall_back_after_crash` (Task 6).
5. **A role switched to `auto` while a pass for other roles is still running** — a person expects that role to be picked up as soon as the running pass finishes, not a week later. Pinned by `test_role_added_mid_pass_starts_next_pass` (Task 6).

---

## File Structure

| File | Responsibility |
|---|---|
| `threadkeeper/agent_metadata.py` (modify) | `ROUTER_ROLE` constant + `model_router` role metadata |
| `threadkeeper/config.py` (modify) | Router interval and fallback settings + derived constants; pass-id env key allowlist |
| `threadkeeper/spawn_config.py` (modify) | `ROLE_DEFAULTS`, `roles_on_auto`, `route_for`, routed resolution, warnings |
| `threadkeeper/db.py` (modify) | `model_routes` and `model_router_passes` tables |
| `threadkeeper/model_catalog.py` (modify) | `read_cached_clis()`; `routed` / `routed_at` in the settings catalog |
| `threadkeeper/model_routes.py` (create) | Route + pass storage, catalog options, decision validation, `active_route` |
| `threadkeeper/model_router_digest.py` (create) | Deterministic telemetry digest for the router prompt |
| `threadkeeper/model_router.py` (create) | Tick state machine, prompt, child spawn, daemon start |
| `threadkeeper/tools/model_router.py` (create) | `model_route_write`, `model_router_run`, `model_router_status` |
| `threadkeeper/agent_status.py` (modify) | Public `extract_useful_result`; loop + role registration |
| `threadkeeper/adapters/base.py` (modify) | Pass-id env key reaches the child's MCP server |
| `threadkeeper/host.py`, `threadkeeper/notify.py`, `threadkeeper/tools/dashboard.py`, `threadkeeper/server.py` (modify) | Daemon, failure-notify, dashboard, and tool registration |
| `apps/macos-agent-status/ThreadKeeperAgentStatus.swift` (modify) | `Auto` choice, routed line, `config_write` badge |
| `tests/conftest.py`, `tests/test_spawn_config.py`, `tests/test_menubar_app.py` (modify) | Isolation + updated role taxonomy |
| `tests/test_model_routes.py`, `tests/test_model_router_digest.py`, `tests/test_model_router_tools.py`, `tests/test_model_router.py` (create) | New unit tests |
| `README.md`, `docs/ARCHITECTURE.md`, `.env.example`, `CHANGELOG.md`, `apps/macos-agent-status/README.md` (modify) | Documentation |

---

### Task 1: Router role, settings, and role defaults

**Files:**
- Modify: `threadkeeper/agent_metadata.py` (AGENT_ROLES tuple, new constant)
- Modify: `threadkeeper/config.py` (Settings fields near the dialectic block ~line 548; `_derive_constants` ~line 973)
- Modify: `threadkeeper/spawn_config.py` (`resolve_agent`, `resolve_model`, `resolve_effort`, `agent_cli_is_dynamic`, `resolution_details`)
- Modify: `tests/conftest.py` (`_force_clean_env`)
- Modify: `tests/test_menubar_app.py:317`
- Test: `tests/test_spawn_config.py`

**Interfaces:**
- Produces: `agent_metadata.ROUTER_ROLE = "model_router"`; `config.MODEL_ROUTER_INTERVAL_S: float`, `config.MODEL_ROUTER_FALLBACK_CLI: str`, `config.MODEL_ROUTER_FALLBACK_MODEL: str`, `config.MODEL_ROUTER_FALLBACK_EFFORT: str`; `spawn_config.ROLE_DEFAULTS: dict[str, dict[str, str]]`; `spawn_config.roles_on_auto() -> list[str]`; resolution source label `"role default"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_spawn_config.py`:

```python
# ──────────────────────────────────────────────────────────────────────
# model_router role defaults
# ──────────────────────────────────────────────────────────────────────

def test_model_router_settings_defaults():
    from threadkeeper.config import Settings

    fields = Settings.model_fields
    assert fields["model_router_interval_s"].default == 604800.0
    assert fields["model_router_fallback_cli"].default == "codex"
    assert fields["model_router_fallback_model"].default == "gpt-6.1-sol"
    assert fields["model_router_fallback_effort"].default == "xhigh"


def test_model_router_derived_constants(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_MODEL_ROUTER_INTERVAL_S": "3600",
        "THREADKEEPER_MODEL_ROUTER_FALLBACK_CLI": " Codex ",
        "THREADKEEPER_MODEL_ROUTER_FALLBACK_EFFORT": "HIGH",
    })
    import threadkeeper.config as c

    assert c.MODEL_ROUTER_INTERVAL_S == 3600.0
    assert c.MODEL_ROUTER_FALLBACK_CLI == "codex"
    assert c.MODEL_ROUTER_FALLBACK_MODEL == "gpt-6.1-sol"
    assert c.MODEL_ROUTER_FALLBACK_EFFORT == "high"


def test_model_router_defaults_to_claude_opus_max(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path)
    assert sc.resolve_agent("model_router", "codex") == "claude"
    assert sc.resolve_model("claude", "model_router") == "opus"
    assert sc.resolve_effort("claude", "model_router") == "max"
    details = sc.resolution_details("model_router", "codex")
    assert (
        details["cli_source"], details["model_source"], details["effort_source"]
    ) == ("role default", "role default", "role default")
    assert sc.agent_cli_is_dynamic("model_router", None) is False


def test_router_role_default_model_needs_matching_cli(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__LOOP__MODEL_ROUTER": "codex",
    })
    assert sc.resolve_agent("model_router", None) == "codex"
    assert sc.resolve_model("codex", "model_router") == ""
    assert sc.resolve_effort("codex", "model_router") == ""


def test_router_role_pins_beat_role_default(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__MODEL__MODEL_ROUTER": "sonnet",
        "THREADKEEPER_SPAWN__EFFORT__MODEL_ROUTER": "high",
    })
    assert sc.resolve_model("claude", "model_router") == "sonnet"
    assert sc.resolve_effort("claude", "model_router") == "high"


def test_router_role_on_auto_follows_active_cli(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__LOOP__MODEL_ROUTER": "auto",
    })
    assert sc.resolve_agent("model_router", "codex") == "codex"


def test_roles_on_auto_normalizes_case_and_space(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__LOOP__CURATOR": " Auto ",
        "THREADKEEPER_SPAWN__LOOP__MODEL_ROUTER": "auto",
        "THREADKEEPER_SPAWN__LOOP__SHADOW_OBSERVER": "codex",
    })
    assert sc.roles_on_auto() == ["curator"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_spawn_config.py -q -k "model_router or roles_on_auto or router_role"`
Expected: FAIL (`KeyError: 'model_router_interval_s'`, `AttributeError: ... roles_on_auto`).

- [ ] **Step 3: Add the role metadata**

In `threadkeeper/agent_metadata.py`, add above `AGENT_ROLES`:

```python
ROUTER_ROLE: Final[str] = "model_router"
```

Append this entry as the last item of `AGENT_ROLES` (after `evolve_applier`):

```python
    {
        "role": ROUTER_ROLE,
        "name": "Model router",
        "description": (
            "Weekly reviews which CLI, model and effort each auto role runs "
            "on, and re-assigns them."
        ),
        "reads": "Spawn telemetry, recent run outcomes, and the local model catalog",
        "writes": "Routing decisions for roles set to auto",
        "impact": "config_write",
        "interval_key": "THREADKEEPER_MODEL_ROUTER_INTERVAL_S",
    },
```

- [ ] **Step 4: Add the settings**

In `threadkeeper/config.py`, in `Settings` directly after the dialectic auto-feed block (after `dialectic_obs_max_requeues`), add:

```python
    # ── Model router daemon ──────────────────────────────────────────────────
    # Weekly CLI/model/effort routing for roles set to `auto`. 0 disables the
    # loop, and `auto` then keeps its pre-router meaning (follow active CLI).
    model_router_interval_s: float = 604800.0
    model_router_fallback_cli: str = "codex"
    model_router_fallback_model: str = "gpt-6.1-sol"
    model_router_fallback_effort: str = "xhigh"
```

In `_derive_constants`, after the `"DIALECTIC_OBS_MAX_REQUEUES"` entry, add:

```python
        "MODEL_ROUTER_INTERVAL_S": float(s.model_router_interval_s),
        "MODEL_ROUTER_FALLBACK_CLI": s.model_router_fallback_cli.strip().lower(),
        "MODEL_ROUTER_FALLBACK_MODEL": s.model_router_fallback_model.strip(),
        "MODEL_ROUTER_FALLBACK_EFFORT": (
            s.model_router_fallback_effort.strip().lower()
        ),
```

In `tests/conftest.py` `_force_clean_env`, next to `"THREADKEEPER_CURATOR_INTERVAL_S": "0",` add:

```python
        "THREADKEEPER_MODEL_ROUTER_INTERVAL_S": "0",  # disable model-router daemon
```

- [ ] **Step 5: Add role defaults to `spawn_config.py`**

Change the metadata import to:

```python
from .agent_metadata import AGENT_ROLE_NAMES, ROUTER_ROLE
```

Add after `EFFORT_OPTIONS`:

```python
# Code-level defaults for roles whose right model differs from the CLI default.
# Model and effort apply only while the role still resolves to `cli`, so
# pinning another CLI never inherits a model that CLI would reject.
ROLE_DEFAULTS: dict[str, dict[str, str]] = {
    ROUTER_ROLE: {"cli": "claude", "model": "opus", "effort": "max"},
}
```

Add after `_norm_cli`:

```python
def _role_cli_unset(role: str) -> bool:
    return not _norm(_spawn().loop.get(_norm(role)))


def _role_default(role: str, cli: str, field: str) -> str:
    default = ROLE_DEFAULTS.get(_norm(role))
    if default is None or default["cli"] != _norm_cli(cli):
        return ""
    return default[field]


def roles_on_auto() -> list[str]:
    """Agent roles whose CLI is set to `auto`, excluding the router itself."""
    loop = _spawn().loop
    return [
        role for role in SUMMARY_ROLES
        if role != ROUTER_ROLE and _norm(loop.get(role)) == "auto"
    ]
```

Replace `resolve_agent` with:

```python
def resolve_agent(role: str, active_cli: Optional[str] = None) -> str:
    """Which CLI runs the spawned child for this role. Priority:
    spawn.loop[role] -> role default -> spawn.default -> active CLI -> 'claude'.
    'auto' in loop/default defers to the active CLI."""
    sp = _spawn()
    role_key = _norm(role)
    cli = _norm_cli(sp.loop.get(role_key))
    if cli and cli != "auto" and cli in SUPPORTED_CLIS:
        return cli
    if _role_cli_unset(role_key) and role_key in ROLE_DEFAULTS:
        return ROLE_DEFAULTS[role_key]["cli"]
    default = _norm_cli(sp.default)
    if default and default != "auto" and default in SUPPORTED_CLIS:
        return default
    active = _norm_cli(active_cli)
    if active and active in SUPPORTED_CLIS:
        return active
    return "claude"
```

Replace `agent_cli_is_dynamic` with:

```python
def agent_cli_is_dynamic(role: str, active_cli: Optional[str] = None) -> bool:
    """Whether routing still depends on the host CLI selected at spawn time."""
    if _norm_cli(active_cli) in SUPPORTED_CLIS:
        return False
    sp = _spawn()
    role_key = _norm(role)
    if _norm_cli(sp.loop.get(role_key)) in SUPPORTED_CLIS:
        return False
    if _role_cli_unset(role_key) and role_key in ROLE_DEFAULTS:
        return False
    return _norm_cli(sp.default) not in SUPPORTED_CLIS
```

In `resolve_model`, replace the `if role:` block with:

```python
    if role:
        v = models.get(role.lower())
        if isinstance(v, str) and v.strip():
            return v.strip()
        default = _role_default(role, cli, "model")
        if default:
            return default
```

In `resolve_effort`, replace the `if role:` block with:

```python
    if role:
        value = efforts.get(role.lower())
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
        default = _role_default(role, cli, "effort")
        if default:
            return default
```

In `resolution_details`, add one branch to each source chain:

```python
    elif role_cli in SUPPORTED_CLIS:
        cli_source_key = ""
        cli_source = "role override"
    elif _role_cli_unset(role_key) and role_key in ROLE_DEFAULTS:
        cli_source_key = ""
        cli_source = "role default"
```

```python
    if role_key in sp.model and _norm(sp.model.get(role_key)):
        model_source_key = _process_override_key(role_model_key)
        model_source = "process environment" if model_source_key else "role override"
    elif _role_default(role_key, cli, "model"):
        model_source_key = ""
        model_source = "role default"
```

```python
    if role_key in sp.effort and _norm(sp.effort.get(role_key)):
        effort_source_key = _process_override_key(role_effort_key)
        effort_source = "process environment" if effort_source_key else "role override"
    elif _role_default(role_key, cli, "effort"):
        effort_source_key = ""
        effort_source = "role default"
```

Update the module docstring's resolution lines to:

```
Agent resolution per role (highest first):
    spawn.loop[role] -> ROLE_DEFAULTS[role] -> spawn.default -> active CLI -> "claude"
`"auto"` in loop/default explicitly defers to the active CLI.

Model and effort resolution: role override -> role default (same CLI only)
-> CLI default -> "".
```

- [ ] **Step 6: Update the role taxonomy test**

In `tests/test_menubar_app.py`, change the asserted tuple to:

```python
    assert AGENT_ROLE_NAMES == (
        "shadow_observer", "archivist", "candidate_reviewer", "curator",
        "curator_researcher", "dialectic_validator", "probe_runner", "evolve_researcher",
        "evolve_reviewer", "evolve_applier", "model_router",
    )
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_spawn_config.py tests/test_menubar_app.py tests/test_config_settings.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add threadkeeper/agent_metadata.py threadkeeper/config.py threadkeeper/spawn_config.py tests/conftest.py tests/test_spawn_config.py tests/test_menubar_app.py
git commit -m "feat: add model_router role, settings, and role defaults"
```

---

### Task 2: Route and pass storage with catalog validation

**Files:**
- Modify: `threadkeeper/db.py` (`ADDITIVE_RUNTIME_SCHEMA`, end of the string)
- Modify: `threadkeeper/model_catalog.py` (after `_read_cache`)
- Create: `threadkeeper/model_routes.py`
- Test: `tests/test_model_routes.py`

**Interfaces:**
- Consumes: `agent_metadata.ROUTER_ROLE`; `config.MODEL_ROUTER_INTERVAL_S`; `spawn_config.EFFORT_OPTIONS`.
- Produces (all in `threadkeeper.model_routes`):
  - `PASS_ID_ENV = "THREADKEEPER_MODEL_ROUTER_PASS_ID"`, `MAX_REASON_CHARS = 500`
  - `Route(role, cli, model, effort, reason, pass_id, decided_at)` frozen dataclass
  - `RouterPass(pass_id, roles: tuple[str, ...], attempt, task_id, status, started_at, ended_at)` frozen dataclass
  - `CliOptions(models, model_efforts, efforts, effort_mode)` with `efforts_for(model) -> tuple[str, ...]`
  - `options_from_catalog(clis: dict) -> dict[str, CliOptions]`, `cached_options() -> dict[str, CliOptions]`
  - `route_problem(cli, model, effort, options) -> str`, `route_is_valid(route, options) -> bool`
  - `decision_errors(decisions: list, expected_roles: set[str], options) -> list[str]`
  - `latest_route(conn, role) -> Route | None`, `recent_routes(conn, role, limit=3) -> list[Route]`, `insert_routes(conn, decisions, pass_id, now) -> list[Route]`
  - `active_route(role) -> Route | None`, `stale_route(role) -> Route | None`
  - `triple(cli, model, effort) -> str`, `routing_summary(routes, previous: dict[str, str]) -> str`
  - `open_pass(conn, roles, now) -> RouterPass`, `get_pass(conn, pass_id)`, `latest_pass(conn)`, `set_attempt(conn, pass_id, attempt, task_id)`, `finish_pass(conn, pass_id, status, now)`, `last_complete_at(conn) -> int | None`
  - `model_catalog.read_cached_clis() -> dict[str, Any]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_model_routes.py`:

```python
"""model_routes — route/pass storage, catalog options, decision validation."""
from __future__ import annotations

CATALOG = {
    "claude": {"models": ["opus", "sonnet"], "effort_options": None, "model_efforts": {}},
    "codex": {
        "models": ["gpt-6.1-sol", "gpt-5.6-luna"],
        "effort_options": ["low", "medium", "high", "xhigh"],
        "model_efforts": {"gpt-5.6-luna": ["low", "medium", "high", "xhigh", "max"]},
    },
    "antigravity": {
        "models": ["gemini-3.8-flash-high\tGemini 3.8 Flash (High)"],
        "effort_options": [],
        "model_efforts": {},
    },
}


def _decision(role="curator", cli="codex", model="gpt-5.6-luna", effort="high",
              reason="cheaper with equal outcomes"):
    return {"role": role, "cli": cli, "model": model, "effort": effort, "reason": reason}


def _options():
    from threadkeeper import model_routes
    return model_routes.options_from_catalog(CATALOG)


def test_options_strip_labels_and_fall_back_to_cli_efforts():
    options = _options()
    assert options["antigravity"].models == ("gemini-3.8-flash-high",)
    assert options["antigravity"].effort_mode == "model"
    assert options["claude"].efforts_for("opus") == ("low", "medium", "high", "xhigh", "max")
    assert options["codex"].efforts_for("gpt-5.6-luna")[-1] == "max"
    assert options["codex"].efforts_for("gpt-6.1-sol")[-1] == "xhigh"


def test_route_problem_cases():
    from threadkeeper.model_routes import route_problem
    options = _options()
    assert route_problem("codex", "gpt-6.1-sol", "xhigh", options) == ""
    assert "not in the model catalog" in route_problem("copilot", "x", "high", options)
    assert "not offered" in route_problem("codex", "gpt-9", "high", options)
    assert "not allowed" in route_problem("codex", "gpt-6.1-sol", "max", options)
    assert "must be empty" in route_problem("antigravity", "gemini-3.8-flash-high", "high", options)
    assert route_problem("antigravity", "gemini-3.8-flash-high", "", options) == ""


def test_valid_decision_set_has_no_errors():
    from threadkeeper.model_routes import decision_errors
    decisions = [_decision(), _decision(role="shadow_observer", cli="claude",
                                        model="sonnet", effort="high")]
    assert decision_errors(decisions, {"curator", "shadow_observer"}, _options()) == []


def test_role_set_must_match_pass():
    from threadkeeper.model_routes import decision_errors
    errors = decision_errors(
        [_decision(), _decision(), _decision(role="probe_runner")],
        {"curator", "shadow_observer"},
        _options(),
    )
    joined = "\n".join(errors)
    assert "duplicate roles: curator" in joined
    assert "missing roles: shadow_observer" in joined
    assert "roles not in this pass: probe_runner" in joined


def test_router_cannot_route_itself_and_reason_is_bounded():
    from threadkeeper.model_routes import MAX_REASON_CHARS, decision_errors
    errors = decision_errors(
        [_decision(role="model_router", reason="x" * (MAX_REASON_CHARS + 1))],
        {"model_router"},
        _options(),
    )
    joined = "\n".join(errors)
    assert "cannot route itself" in joined
    assert "reason must be 1-500 characters" in joined


def test_non_object_and_missing_fields_are_reported():
    from threadkeeper.model_routes import decision_errors
    errors = decision_errors(["nope", {"role": "curator"}], {"curator"}, _options())
    joined = "\n".join(errors)
    assert "decision 0: must be an object" in joined
    assert "decision 1: missing string fields: cli, model, effort, reason" in joined


def test_decision_model_ids_are_case_sensitive():
    from threadkeeper.model_routes import decision_errors
    errors = decision_errors([_decision(model="GPT-5.6-Luna")], {"curator"}, _options())
    assert errors == ["decision 0: model 'GPT-5.6-Luna' is not offered by codex"]


def test_missing_catalog_blocks_writes():
    from threadkeeper.model_routes import decision_errors
    assert decision_errors([_decision()], {"curator"}, {}) == [
        "model catalog is unavailable; cannot validate decisions"
    ]


def test_insert_and_latest_route(fresh_mp):
    from threadkeeper import model_routes
    conn = fresh_mp["db"].get_db()
    model_routes.insert_routes(conn, [_decision(model="gpt-6.1-sol", effort="xhigh")], "mr-1", 100)
    model_routes.insert_routes(conn, [_decision()], "mr-2", 200)
    conn.commit()
    route = model_routes.latest_route(conn, "curator")
    assert (route.model, route.effort, route.pass_id, route.decided_at) == (
        "gpt-5.6-luna", "high", "mr-2", 200)
    assert [r.pass_id for r in model_routes.recent_routes(conn, "curator")] == ["mr-2", "mr-1"]
    assert model_routes.latest_route(conn, "shadow_observer") is None


def test_active_route_requires_enabled_router_and_valid_catalog(fresh_mp, monkeypatch):
    from threadkeeper import model_routes
    conn = fresh_mp["db"].get_db()
    model_routes.insert_routes(conn, [_decision()], "mr-1", 100)
    conn.commit()
    monkeypatch.setattr(model_routes, "cached_options", _options)

    assert model_routes.active_route("curator") is None  # interval 0 in tests

    monkeypatch.setattr(fresh_mp["config"], "MODEL_ROUTER_INTERVAL_S", 604800.0)
    assert model_routes.active_route("curator").model == "gpt-5.6-luna"
    assert model_routes.active_route("model_router") is None
    assert model_routes.stale_route("curator") is None

    monkeypatch.setattr(model_routes, "cached_options",
                        lambda: model_routes.options_from_catalog({"codex": {"models": ["other"]}}))
    assert model_routes.active_route("curator") is None
    assert model_routes.stale_route("curator").model == "gpt-5.6-luna"

    monkeypatch.setattr(model_routes, "cached_options", lambda: {})
    assert model_routes.active_route("curator").model == "gpt-5.6-luna"


def test_pass_lifecycle(fresh_mp):
    from threadkeeper import model_routes
    conn = fresh_mp["db"].get_db()
    assert model_routes.latest_pass(conn) is None
    router_pass = model_routes.open_pass(conn, ["shadow_observer", "curator"], 1000)
    assert router_pass.roles == ("curator", "shadow_observer")
    assert (router_pass.attempt, router_pass.status, router_pass.task_id) == ("primary", "running", "")

    model_routes.set_attempt(conn, router_pass.pass_id, "fallback", "tk_abc")
    model_routes.finish_pass(conn, router_pass.pass_id, "complete", 2000)
    conn.commit()
    stored = model_routes.latest_pass(conn)
    assert (stored.attempt, stored.task_id, stored.status, stored.ended_at) == (
        "fallback", "tk_abc", "complete", 2000)
    assert model_routes.last_complete_at(conn) == 2000


def test_routing_summary_lists_only_changes():
    from threadkeeper.model_routes import Route, routing_summary
    routes = [
        Route("curator", "codex", "gpt-5.6-luna", "high", "r", "mr-1", 1),
        Route("shadow_observer", "codex", "gpt-5.6-terra", "xhigh", "r", "mr-1", 1),
    ]
    previous = {
        "curator": "codex gpt-5.6-terra xhigh",
        "shadow_observer": "codex gpt-5.6-terra xhigh",
    }
    assert routing_summary(routes, previous) == (
        "routed 1/2: curator: codex gpt-5.6-terra xhigh → codex gpt-5.6-luna high"
    )
    assert routing_summary(routes[1:], previous) == "routed 0/1 (all kept)"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_model_routes.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'threadkeeper.model_routes'`.

- [ ] **Step 3: Add the tables**

In `threadkeeper/db.py`, append inside `ADDITIVE_RUNTIME_SCHEMA` (before the closing `"""`):

```sql
-- Model router: one row per routing decision. The latest row per role is the
-- current route; older rows are the history the next pass reads. Local only:
-- catalogs differ per machine, so these tables are not synced.
CREATE TABLE IF NOT EXISTS model_routes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT NOT NULL,
    cli         TEXT NOT NULL,
    model       TEXT NOT NULL,
    effort      TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL,
    pass_id     TEXT NOT NULL,
    decided_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_routes_role
    ON model_routes(role, decided_at DESC);
CREATE TABLE IF NOT EXISTS model_router_passes (
    pass_id     TEXT PRIMARY KEY,
    roles_json  TEXT NOT NULL,
    attempt     TEXT NOT NULL CHECK(attempt IN ('primary','fallback')),
    task_id     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'running'
                CHECK(status IN ('running','complete','failed')),
    started_at  INTEGER NOT NULL,
    ended_at    INTEGER
);
```

- [ ] **Step 4: Expose the cached catalog**

In `threadkeeper/model_catalog.py`, add after `_read_cache`:

```python
def read_cached_clis() -> dict[str, Any]:
    """Per-CLI entries from the on-disk catalog cache, without discovery."""
    clis = _read_cache().get("clis")
    return dict(clis) if isinstance(clis, dict) else {}
```

- [ ] **Step 5: Create `threadkeeper/model_routes.py`**

```python
"""Model-router persistence and validation.

`model_routes` holds one row per routing decision (the latest row per role is
the current route); `model_router_passes` holds the state of each router pass.
Spawn resolution reads routes through `active_route`, which only returns a
route the local model catalog can still run.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from .agent_metadata import ROUTER_ROLE

PASS_ID_ENV = "THREADKEEPER_MODEL_ROUTER_PASS_ID"
MAX_REASON_CHARS = 500
_DECISION_FIELDS = ("role", "cli", "model", "effort", "reason")
_ROUTE_COLUMNS = "role, cli, model, effort, reason, pass_id, decided_at"
_PASS_COLUMNS = "pass_id, roles_json, attempt, task_id, status, started_at, ended_at"


@dataclass(frozen=True)
class Route:
    role: str
    cli: str
    model: str
    effort: str
    reason: str
    pass_id: str
    decided_at: int


@dataclass(frozen=True)
class RouterPass:
    pass_id: str
    roles: tuple[str, ...]
    attempt: str
    task_id: str
    status: str
    started_at: int
    ended_at: Optional[int]


@dataclass(frozen=True)
class CliOptions:
    models: tuple[str, ...]
    model_efforts: dict[str, tuple[str, ...]]
    efforts: tuple[str, ...]
    effort_mode: str

    def efforts_for(self, model: str) -> tuple[str, ...]:
        return self.model_efforts.get(model) or self.efforts


# ── catalog options ─────────────────────────────────────────────────────────

def _model_id(entry: Any) -> str:
    return str(entry).split("\t", 1)[0].strip()


def _cli_options(cli: str, data: dict[str, Any]) -> CliOptions:
    from .spawn_config import EFFORT_OPTIONS

    models = tuple(m for m in (_model_id(e) for e in data.get("models") or []) if m)
    model_efforts = {
        _model_id(model): tuple(efforts)
        for model, efforts in (data.get("model_efforts") or {}).items()
    }
    efforts = tuple(data.get("effort_options") or EFFORT_OPTIONS.get(cli, ()))
    effort_mode = "model" if cli == "antigravity" else "independent"
    return CliOptions(models, model_efforts, efforts, effort_mode)


def options_from_catalog(clis: dict[str, Any]) -> dict[str, CliOptions]:
    return {
        cli: _cli_options(cli, data)
        for cli, data in (clis or {}).items()
        if isinstance(data, dict)
    }


def cached_options() -> dict[str, CliOptions]:
    """Catalog options from the on-disk cache; {} when there is no cache."""
    from .model_catalog import read_cached_clis

    return options_from_catalog(read_cached_clis())


def route_problem(cli: str, model: str, effort: str,
                  options: dict[str, CliOptions]) -> str:
    """Why a CLI/model/effort triple cannot run here, or '' when it can."""
    cli_options = options.get(cli)
    if cli_options is None:
        return f"cli {cli!r} is not in the model catalog"
    if model not in cli_options.models:
        return f"model {model!r} is not offered by {cli}"
    if cli_options.effort_mode == "model":
        return "" if not effort else f"{cli} encodes effort in the model; effort must be empty"
    if effort not in cli_options.efforts_for(model):
        return f"effort {effort!r} is not allowed for {model}"
    return ""


def route_is_valid(route: Route, options: dict[str, CliOptions]) -> bool:
    """A missing catalog never invalidates a route; only a known gap does."""
    if not options:
        return True
    return route_problem(route.cli, route.model, route.effort, options) == ""


# ── decision validation ─────────────────────────────────────────────────────

def _clean(value: str) -> str:
    return value.strip().lower()


def _role_set_errors(decisions: list[Any], expected_roles: set[str]) -> list[str]:
    roles = [_clean(str(d.get("role", ""))) for d in decisions if isinstance(d, dict)]
    duplicates = sorted({role for role in roles if roles.count(role) > 1})
    missing = sorted(expected_roles - set(roles))
    extra = sorted(set(roles) - expected_roles)
    errors = []
    if duplicates:
        errors.append(f"duplicate roles: {', '.join(duplicates)}")
    if missing:
        errors.append(f"missing roles: {', '.join(missing)}")
    if extra:
        errors.append(f"roles not in this pass: {', '.join(extra)}")
    return errors


def _decision_problems(decision: Any, options: dict[str, CliOptions]) -> list[str]:
    if not isinstance(decision, dict):
        return ["must be an object"]
    missing = [f for f in _DECISION_FIELDS if not isinstance(decision.get(f), str)]
    if missing:
        return [f"missing string fields: {', '.join(missing)}"]
    problems = []
    if _clean(decision["role"]) == ROUTER_ROLE:
        problems.append("the model router cannot route itself")
    triple_problem = route_problem(
        _clean(decision["cli"]), decision["model"].strip(),
        _clean(decision["effort"]), options,
    )
    if triple_problem:
        problems.append(triple_problem)
    if not 1 <= len(decision["reason"].strip()) <= MAX_REASON_CHARS:
        problems.append(f"reason must be 1-{MAX_REASON_CHARS} characters")
    return problems


def decision_errors(decisions: list[Any], expected_roles: set[str],
                    options: dict[str, CliOptions]) -> list[str]:
    """Every reason the decision set cannot be recorded; [] when it can."""
    if not options:
        return ["model catalog is unavailable; cannot validate decisions"]
    errors = _role_set_errors(decisions, expected_roles)
    for index, decision in enumerate(decisions):
        errors.extend(
            f"decision {index}: {problem}"
            for problem in _decision_problems(decision, options)
        )
    return errors


# ── routes ──────────────────────────────────────────────────────────────────

def latest_route(conn: sqlite3.Connection, role: str) -> Optional[Route]:
    row = conn.execute(
        f"SELECT {_ROUTE_COLUMNS} FROM model_routes WHERE role=? "
        "ORDER BY decided_at DESC, id DESC LIMIT 1",
        (role,),
    ).fetchone()
    return Route(*row) if row else None


def recent_routes(conn: sqlite3.Connection, role: str, limit: int = 3) -> list[Route]:
    rows = conn.execute(
        f"SELECT {_ROUTE_COLUMNS} FROM model_routes WHERE role=? "
        "ORDER BY decided_at DESC, id DESC LIMIT ?",
        (role, limit),
    ).fetchall()
    return [Route(*row) for row in rows]


def _normalized(decision: dict[str, str]) -> dict[str, str]:
    return {
        "role": _clean(decision["role"]),
        "cli": _clean(decision["cli"]),
        "model": decision["model"].strip(),
        "effort": _clean(decision["effort"]),
        "reason": decision["reason"].strip(),
    }


def insert_routes(conn: sqlite3.Connection, decisions: list[dict[str, str]],
                  pass_id: str, now: int) -> list[Route]:
    """Insert validated decisions. The caller owns the commit."""
    routes = [
        Route(pass_id=pass_id, decided_at=now, **_normalized(d)) for d in decisions
    ]
    conn.executemany(
        f"INSERT INTO model_routes ({_ROUTE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(r.role, r.cli, r.model, r.effort, r.reason, r.pass_id, r.decided_at)
         for r in routes],
    )
    return routes


def _router_enabled() -> bool:
    from . import config

    return float(getattr(config, "MODEL_ROUTER_INTERVAL_S", 0) or 0) > 0


def _stored_route(role: str) -> Optional[Route]:
    if role == ROUTER_ROLE or not _router_enabled():
        return None
    try:
        from .db import get_db

        return latest_route(get_db(), role)
    except sqlite3.Error:
        return None


def active_route(role: str) -> Optional[Route]:
    """Current runnable route for `role`; None when the router is off, has not
    decided yet, or the catalog no longer offers the decided triple."""
    route = _stored_route(role)
    if route is None or not route_is_valid(route, cached_options()):
        return None
    return route


def stale_route(role: str) -> Optional[Route]:
    """The latest decision for `role` when the catalog no longer offers it."""
    route = _stored_route(role)
    if route is None or route_is_valid(route, cached_options()):
        return None
    return route


def triple(cli: str, model: str, effort: str) -> str:
    return f"{cli} {model or '(default)'} {effort or '(default)'}"


def routing_summary(routes: list[Route], previous: dict[str, str]) -> str:
    changes = [
        f"{route.role}: {previous.get(route.role, '?')} → "
        f"{triple(route.cli, route.model, route.effort)}"
        for route in routes
        if previous.get(route.role) != triple(route.cli, route.model, route.effort)
    ]
    head = f"routed {len(changes)}/{len(routes)}"
    return head + (": " + "; ".join(changes) if changes else " (all kept)")


# ── passes ──────────────────────────────────────────────────────────────────

def _pass_from_row(row: sqlite3.Row) -> RouterPass:
    return RouterPass(
        pass_id=row["pass_id"],
        roles=tuple(json.loads(row["roles_json"])),
        attempt=row["attempt"],
        task_id=row["task_id"],
        status=row["status"],
        started_at=int(row["started_at"]),
        ended_at=row["ended_at"],
    )


def get_pass(conn: sqlite3.Connection, pass_id: str) -> Optional[RouterPass]:
    row = conn.execute(
        f"SELECT {_PASS_COLUMNS} FROM model_router_passes WHERE pass_id=?",
        (pass_id,),
    ).fetchone()
    return _pass_from_row(row) if row else None


def latest_pass(conn: sqlite3.Connection) -> Optional[RouterPass]:
    row = conn.execute(
        f"SELECT {_PASS_COLUMNS} FROM model_router_passes "
        "ORDER BY started_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    return _pass_from_row(row) if row else None


def open_pass(conn: sqlite3.Connection, roles: list[str], now: int) -> RouterPass:
    pass_id = f"mr-{now}-{secrets.token_hex(3)}"
    conn.execute(
        "INSERT INTO model_router_passes (pass_id, roles_json, attempt, started_at) "
        "VALUES (?, ?, 'primary', ?)",
        (pass_id, json.dumps(sorted(roles)), now),
    )
    conn.commit()
    return get_pass(conn, pass_id)


def set_attempt(conn: sqlite3.Connection, pass_id: str, attempt: str,
                task_id: str) -> None:
    conn.execute(
        "UPDATE model_router_passes SET attempt=?, task_id=? WHERE pass_id=?",
        (attempt, task_id, pass_id),
    )


def finish_pass(conn: sqlite3.Connection, pass_id: str, status: str, now: int) -> None:
    conn.execute(
        "UPDATE model_router_passes SET status=?, ended_at=? "
        "WHERE pass_id=? AND status='running'",
        (status, now, pass_id),
    )


def last_complete_at(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute(
        "SELECT MAX(ended_at) FROM model_router_passes WHERE status='complete'"
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_model_routes.py tests/test_db_schema.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add threadkeeper/db.py threadkeeper/model_catalog.py threadkeeper/model_routes.py tests/test_model_routes.py
git commit -m "feat: store model-router routes and passes with catalog validation"
```

---

### Task 3: Route `auto` roles in spawn resolution

**Files:**
- Modify: `threadkeeper/spawn_config.py` (`route_for`, `resolve_agent`, `resolve_model`, `resolve_effort`, `agent_cli_is_dynamic`, `resolution_details`, `_spawn_warnings`)
- Test: `tests/test_spawn_config.py`

**Interfaces:**
- Consumes: `model_routes.active_route(role)`, `model_routes.stale_route(role)`, `model_routes.Route`; Task 1 helpers.
- Produces: `spawn_config.route_for(role) -> Route | None`; resolution source label `"model router"`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_spawn_config.py`, change `_reset` so the router is off unless a test turns it on (keeps the existing `auto` tests off the database):

```python
    monkeypatch.setenv("THREADKEEPER_ENV_FILE", env_file or str(tmp_path / "none.env"))
    monkeypatch.setenv("THREADKEEPER_MODEL_ROUTER_INTERVAL_S", "0")
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
```

Append:

```python
# ──────────────────────────────────────────────────────────────────────
# model-router routes for `auto` roles
# ──────────────────────────────────────────────────────────────────────

def _route(**overrides):
    from threadkeeper.model_routes import Route

    values = dict(role="curator", cli="codex", model="gpt-5.6-luna",
                  effort="high", reason="cheaper", pass_id="mr-1", decided_at=1)
    values.update(overrides)
    return Route(**values)


def _patch_routes(monkeypatch, routes):
    import threadkeeper.model_routes as mr

    monkeypatch.setattr(mr, "active_route", lambda role: routes.get(role))


def test_auto_role_uses_router_decision(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={"THREADKEEPER_SPAWN__LOOP__CURATOR": "auto"})
    _patch_routes(monkeypatch, {"curator": _route()})
    assert sc.resolve_agent("curator", "claude") == "codex"
    assert sc.resolve_model("codex", "curator") == "gpt-5.6-luna"
    assert sc.resolve_effort("codex", "curator") == "high"


def test_padded_auto_is_routed(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={"THREADKEEPER_SPAWN__LOOP__CURATOR": " AUTO "})
    _patch_routes(monkeypatch, {"curator": _route()})
    assert sc.resolve_agent("curator", "claude") == "codex"


def test_auto_role_without_decision_follows_active_cli(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={"THREADKEEPER_SPAWN__LOOP__CURATOR": "auto"})
    _patch_routes(monkeypatch, {})
    assert sc.resolve_agent("curator", "codex") == "codex"
    assert sc.resolve_agent("curator", "claude") == "claude"


def test_route_ignores_role_pins(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__LOOP__CURATOR": "auto",
        "THREADKEEPER_SPAWN__MODEL__CURATOR": "gpt-5.6-terra",
        "THREADKEEPER_SPAWN__EFFORT__CURATOR": "xhigh",
    })
    _patch_routes(monkeypatch, {"curator": _route()})
    assert sc.resolve_model("codex", "curator") == "gpt-5.6-luna"
    assert sc.resolve_effort("codex", "curator") == "high"


def test_explicit_cli_other_than_route_keeps_role_pins(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__LOOP__CURATOR": "auto",
        "THREADKEEPER_SPAWN__MODEL__CURATOR": "sonnet",
    })
    _patch_routes(monkeypatch, {"curator": _route()})
    assert sc.resolve_model("claude", "curator") == "sonnet"


def test_pinned_role_never_consults_router(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={"THREADKEEPER_SPAWN__LOOP__CURATOR": "claude"})
    import threadkeeper.model_routes as mr

    monkeypatch.setattr(mr, "active_route",
                        lambda role: pytest.fail("router consulted for a pinned role"))
    assert sc.resolve_agent("curator", None) == "claude"
    assert sc.resolve_model("claude", "curator") == ""


def test_router_role_is_never_routed(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={"THREADKEEPER_SPAWN__LOOP__MODEL_ROUTER": "auto"})
    import threadkeeper.model_routes as mr

    monkeypatch.setattr(mr, "active_route",
                        lambda role: pytest.fail("router consulted for itself"))
    assert sc.resolve_agent("model_router", "codex") == "codex"


def test_routed_details_and_not_dynamic(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={"THREADKEEPER_SPAWN__LOOP__CURATOR": "auto"})
    _patch_routes(monkeypatch, {"curator": _route()})
    details = sc.resolution_details("curator", None)
    assert (details["cli"], details["model"], details["effort"]) == (
        "codex", "gpt-5.6-luna", "high")
    assert {details["cli_source"], details["model_source"], details["effort_source"]} == {
        "model router"}
    assert sc.agent_cli_is_dynamic("curator", None) is False
    assert "(model router)" in sc.summary_table(None)


def test_warns_when_routed_role_has_pins(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={
        "THREADKEEPER_SPAWN__LOOP__CURATOR": "auto",
        "THREADKEEPER_SPAWN__MODEL__CURATOR": "opus",
        "THREADKEEPER_SPAWN__EFFORT__CURATOR": "max",
    })
    _patch_routes(monkeypatch, {"curator": _route()})
    warnings = "\n".join(sc._spawn_warnings(None))
    assert ("THREADKEEPER_SPAWN__MODEL__CURATOR is ignored while the role is on "
            "auto and routed by the model router") in warnings
    assert "THREADKEEPER_SPAWN__EFFORT__CURATOR is ignored" in warnings
    assert "is invalid for CLI" not in warnings
    assert "Claude-family model" not in warnings


def test_warns_on_stale_route(tmp_path, monkeypatch):
    sc = _reset(monkeypatch, tmp_path, env={"THREADKEEPER_SPAWN__LOOP__CURATOR": "auto"})
    import threadkeeper.model_routes as mr

    _patch_routes(monkeypatch, {})
    monkeypatch.setattr(mr, "stale_route",
                        lambda role: _route(model="gone") if role == "curator" else None)
    warnings = "\n".join(sc._spawn_warnings(None))
    assert ("model router decision for curator (codex gone high) is no longer in "
            "the model catalog") in warnings
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_spawn_config.py -q -k "route or routed or auto"`
Expected: the new tests FAIL (CLI stays on the active CLI; no `model router` source).

- [ ] **Step 3: Implement routing in `spawn_config.py`**

Add after `roles_on_auto`:

```python
def route_for(role: str):
    """The model router's decision for a role set to `auto`, or None."""
    role_key = _norm(role)
    if role_key == ROUTER_ROLE or _norm(_spawn().loop.get(role_key)) != "auto":
        return None
    from .model_routes import active_route

    return active_route(role_key)


def _routed_details(role_key: str, route) -> dict[str, str]:
    source = "model router"
    return {
        "role": role_key,
        "cli": route.cli, "cli_source": source, "cli_source_key": "",
        "model": route.model, "model_source": source, "model_source_key": "",
        "effort": route.effort, "effort_source": source, "effort_source_key": "",
    }
```

At the top of `resolve_agent` (before `sp = _spawn()`):

```python
    route = route_for(role)
    if route is not None:
        return route.cli
```

and update its docstring priority line to `model-router route (role on auto) -> spawn.loop[role] -> role default -> spawn.default -> active CLI -> 'claude'.`

In `resolve_model`, make the `if role:` block start with:

```python
    if role:
        route = route_for(role)
        if route is not None and route.cli == _norm_cli(cli):
            return route.model
```

In `resolve_effort`, make the `if role:` block start with:

```python
    if role:
        route = route_for(role)
        if route is not None and route.cli == _norm_cli(cli):
            return route.effort
```

In `agent_cli_is_dynamic`, after the active-CLI check add:

```python
    if route_for(role) is not None:
        return False
```

In `resolution_details`, replace the first two lines (`sp = _spawn()` / `role_key = _norm(role)`) with:

```python
    role_key = _norm(role)
    route = route_for(role_key)
    if route is not None:
        return _routed_details(role_key, route)
    sp = _spawn()
```

Add before `_spawn_warnings`:

```python
def _pinned_sections(sp, role: str) -> list[str]:
    return [
        section for section, values in (("MODEL", sp.model), ("EFFORT", sp.effort))
        if _norm(values.get(role))
    ]


def _stale_route_warning(role: str) -> list[str]:
    from .model_routes import stale_route

    route = stale_route(role)
    if route is None:
        return []
    return [
        "  warning: model router decision for "
        f"{role} ({route.cli} {route.model} {route.effort}) is no longer in the "
        "model catalog; the role follows the active CLI until the next pass"
    ]


def _routed_role_warnings(sp) -> tuple[list[str], set[str]]:
    """Warnings for pins a route overrides, plus the set of routed roles."""
    warnings: list[str] = []
    routed: set[str] = set()
    for role in roles_on_auto():
        if route_for(role) is None:
            warnings.extend(_stale_route_warning(role))
            continue
        routed.add(role)
        warnings.extend(
            "  warning: "
            f"{_env_suffix(f'THREADKEEPER_SPAWN__{section}__', role)} is ignored "
            "while the role is on auto and routed by the model router"
            for section in _pinned_sections(sp, role)
        )
    return warnings, routed
```

In `_spawn_warnings`, after `warnings = []` add:

```python
    routed_warnings, routed_roles = _routed_role_warnings(sp)
    warnings.extend(routed_warnings)
```

In the `for key, model in sorted(sp.model.items()):` loop and in the `for key, raw_effort in sorted(sp.effort.items()):` loop, insert right after `role_key = _norm(key)`:

```python
        if role_key in routed_roles:
            continue
```

Add to the module docstring after the resolution lines:

```
A role whose loop value is `auto` first uses the model router's latest valid
decision (`model_routes.active_route`); all three values then come from that
decision. Without one, `auto` keeps the active-CLI meaning.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_spawn_config.py tests/test_spawn_codex_stdin.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add threadkeeper/spawn_config.py tests/test_spawn_config.py
git commit -m "feat: route auto roles through model-router decisions"
```

---

### Task 4: Telemetry digest

**Files:**
- Modify: `threadkeeper/agent_status.py:883` (rename `_extract_useful_result` → `extract_useful_result`; update its one caller at ~line 956)
- Create: `threadkeeper/model_router_digest.py`
- Test: `tests/test_model_router_digest.py`

**Interfaces:**
- Consumes: `model_routes.CliOptions`, `model_routes.recent_routes`, `agent_metadata.role_metadata`.
- Produces: `model_router_digest.build_digest(conn, roles: list[str], options: dict[str, CliOptions], current: dict[str, dict[str, str]], now: int, failure_reason=None, outcome=None) -> str`; helpers `percentile(values, q) -> int | None`, `normalize_failure(line) -> str`; constants `WINDOW_S`, `WEEK_S`, `OUTCOME_SAMPLES = 5`, `MAX_DIGEST_CHARS = 60_000`; `agent_status.extract_useful_result(task_id) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_model_router_digest.py`:

```python
"""model_router_digest — deterministic telemetry digest for the router."""
from __future__ import annotations

NOW = 10_000_000
DAY = 86400
CURRENT = {
    "curator": {"cli": "codex", "model": "gpt-5.6-terra", "effort": "xhigh",
                "cli_source": "active CLI"},
}


def _task(conn, task_id, *, age_days, rc=0, ended=True, model="gpt-5.6-terra",
          effort="xhigh", duration=100, tokens=1000, role="curator"):
    started = NOW - int(age_days * DAY)
    conn.execute(
        "INSERT INTO tasks (id, pid, cwd, prompt, started_at, ended_at, return_code, "
        "duration_s, tokens_total, role, chosen_cli, model, effort) "
        "VALUES (?, 1, '/tmp', 'p', ?, ?, ?, ?, ?, ?, 'codex', ?, ?)",
        (task_id, started, started + duration if ended else None, rc if ended else None,
         duration if ended else None, tokens, role, model, effort),
    )
    conn.commit()


def _options():
    from threadkeeper import model_routes
    return model_routes.options_from_catalog({
        "codex": {"models": ["gpt-5.6-terra", "gpt-5.6-luna"],
                  "effort_options": ["low", "medium", "high", "xhigh"]},
    })


def _digest(conn, **kwargs):
    from threadkeeper.model_router_digest import build_digest
    return build_digest(
        conn, ["curator"], _options(), CURRENT, NOW,
        failure_reason=kwargs.get("failure_reason", lambda task_id: "ERROR: Selected model is at capacity. task tk_123"),
        outcome=kwargs.get("outcome", lambda task_id: f"Report written for {task_id}"),
    )


def test_percentile_nearest_rank():
    from threadkeeper.model_router_digest import percentile
    assert percentile([], 0.5) is None
    assert percentile([5, 1, 3], 0.5) == 3
    assert percentile(list(range(1, 11)), 0.9) == 9


def test_normalize_failure_groups_volatile_tokens():
    from threadkeeper.model_router_digest import normalize_failure
    assert normalize_failure("ERROR: capacity tk_0ec04c after 12 s") == (
        "ERROR: capacity <id> after <n> s")


def test_digest_buckets_weeks_and_counts_statuses(fresh_mp):
    conn = fresh_mp["db"].get_db()
    _task(conn, "tk_a", age_days=1, duration=100)
    _task(conn, "tk_b", age_days=2, duration=300, rc=1)
    _task(conn, "tk_c", age_days=9, duration=50)
    _task(conn, "tk_d", age_days=0.1, ended=False)
    _task(conn, "tk_old", age_days=40)
    text = _digest(conn)

    assert "## ROLE curator" in text
    assert "current: codex / gpt-5.6-terra / xhigh (active CLI)" in text
    assert "codex / gpt-5.6-terra / xhigh" in text
    assert ("this week: runs=3 ok=1 failed=1 unknown=0 running=1 "
            "p50_s=100 p90_s=300 p50_tokens=1000") in text
    assert "1 week(s) ago: runs=1 ok=1" in text
    assert "ERROR: Selected model is at capacity. task <id> ×1" in text
    assert "tk_old" not in text


def test_digest_shows_na_tokens_outcomes_and_options(fresh_mp):
    conn = fresh_mp["db"].get_db()
    _task(conn, "tk_a", age_days=1, tokens=None)
    text = _digest(conn)
    assert "p50_tokens=n/a" in text
    assert "Report written for tk_a" in text
    assert "codex / gpt-5.6-luna: low, medium, high, xhigh" in text


def test_digest_lists_recent_decisions(fresh_mp):
    from threadkeeper import model_routes
    conn = fresh_mp["db"].get_db()
    model_routes.insert_routes(conn, [{"role": "curator", "cli": "codex",
        "model": "gpt-5.6-luna", "effort": "high", "reason": "cheaper"}], "mr-1", NOW - DAY)
    conn.commit()
    text = _digest(conn)
    assert "recent decisions:" in text
    assert "codex gpt-5.6-luna high — cheaper" in text


def test_digest_role_without_runs(fresh_mp):
    conn = fresh_mp["db"].get_db()
    assert "(no runs in the last 30 days)" in _digest(conn)


def test_digest_trims_to_cap(fresh_mp, monkeypatch):
    from threadkeeper import model_router_digest
    conn = fresh_mp["db"].get_db()
    for index in range(6):
        _task(conn, f"tk_{index}", age_days=index * 7 + 1)
    monkeypatch.setattr(model_router_digest, "MAX_DIGEST_CHARS", 900)
    text = _digest(conn, outcome=lambda task_id: "x" * 240)
    assert len(text) <= 900
    assert "x" * 240 not in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_model_router_digest.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'threadkeeper.model_router_digest'`.

- [ ] **Step 3: Make the result extractor public**

In `threadkeeper/agent_status.py`, rename `def _extract_useful_result(task_id: str) -> str:` to `def extract_useful_result(task_id: str) -> str:` and change its caller in `_recent_results` to `summary = extract_useful_result(row["id"])`.

- [ ] **Step 4: Create `threadkeeper/model_router_digest.py`**

```python
"""Deterministic telemetry digest the model router reasons over.

Everything here is computed by the parent from the `tasks` table, task logs,
past decisions and the model catalog, so the router child sees exactly the
numbers a test can assert.
"""
from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Optional

from .agent_metadata import role_metadata
from .model_routes import CliOptions, Route, recent_routes, triple

WINDOW_S = 30 * 86400
WEEK_S = 7 * 86400
OUTCOME_SAMPLES = 5
OUTCOME_CHARS = 240
MAX_DIGEST_CHARS = 60_000
# Trim order when the digest is too long: outcome samples first, then the
# oldest weekly buckets.
_TRIM_STEPS = ((OUTCOME_SAMPLES, 5), (0, 5), (0, 3), (0, 1))
_ID_RE = re.compile(r"\b(?:tk_[0-9a-f]+|[0-9a-f]{8}-[0-9a-f-]{27,})\b")
_NUMBER_RE = re.compile(r"\d+")

TaskText = Callable[[str], str]


@dataclass
class ArmWeek:
    runs: int = 0
    ok: int = 0
    failed: int = 0
    unknown: int = 0
    running: int = 0
    durations: list[int] = field(default_factory=list)
    tokens: list[int] = field(default_factory=list)
    failures: dict[str, int] = field(default_factory=dict)

    def count(self, status: str) -> None:
        setattr(self, status, getattr(self, status) + 1)


def percentile(values: list[int], q: float) -> Optional[int]:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def normalize_failure(line: str) -> str:
    """Collapse task ids, UUIDs and numbers so equal failures group together."""
    text = _NUMBER_RE.sub("<n>", _ID_RE.sub("<id>", line))
    return text.strip()[:160]


def _status(row: sqlite3.Row) -> str:
    if row["ended_at"] is None:
        return "running"
    if row["return_code"] is None:
        return "unknown"
    return "ok" if row["return_code"] == 0 else "failed"


def _arm_key(row: sqlite3.Row) -> tuple[str, str, str]:
    return (
        row["chosen_cli"] or "?",
        row["model"] or "(cli default)",
        row["effort"] or "(cli default)",
    )


def _task_rows(conn: sqlite3.Connection, role: str, since: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, chosen_cli, model, effort, started_at, ended_at, return_code, "
        "duration_s, tokens_total FROM tasks WHERE role=? AND started_at>=? "
        "ORDER BY started_at DESC",
        (role, since),
    ).fetchall()


def _add_run(stats: ArmWeek, row: sqlite3.Row, failure_reason: TaskText) -> None:
    status = _status(row)
    stats.runs += 1
    stats.count(status)
    if row["duration_s"] is not None:
        stats.durations.append(int(row["duration_s"]))
    if row["tokens_total"] is not None:
        stats.tokens.append(int(row["tokens_total"]))
    if status == "failed":
        reason = normalize_failure(failure_reason(row["id"]))
        stats.failures[reason] = stats.failures.get(reason, 0) + 1


def collect_arms(rows: list[sqlite3.Row], now: int,
                 failure_reason: TaskText) -> dict[tuple[str, str, str], dict[int, ArmWeek]]:
    arms: dict[tuple[str, str, str], dict[int, ArmWeek]] = {}
    for row in rows:
        week = int((now - int(row["started_at"])) // WEEK_S)
        stats = arms.setdefault(_arm_key(row), {}).setdefault(week, ArmWeek())
        _add_run(stats, row, failure_reason)
    return arms


def _fmt(value: Optional[int]) -> str:
    return "n/a" if value is None else str(value)


def _top_failures(failures: dict[str, int]) -> str:
    ranked = sorted(failures.items(), key=lambda item: (-item[1], item[0]))[:3]
    return ", ".join(f"{reason} ×{count}" for reason, count in ranked)


def render_week(week: int, stats: ArmWeek) -> str:
    label = "this week" if week == 0 else f"{week} week(s) ago"
    line = (
        f"    {label}: runs={stats.runs} ok={stats.ok} failed={stats.failed} "
        f"unknown={stats.unknown} running={stats.running} "
        f"p50_s={_fmt(percentile(stats.durations, 0.5))} "
        f"p90_s={_fmt(percentile(stats.durations, 0.9))} "
        f"p50_tokens={_fmt(percentile(stats.tokens, 0.5))}"
    )
    failures = _top_failures(stats.failures)
    return line + (f"\n      failures: {failures}" if failures else "")


def _render_telemetry(rows: list[sqlite3.Row], now: int, weeks: int,
                      failure_reason: TaskText) -> list[str]:
    arms = collect_arms(rows, now, failure_reason)
    if not arms:
        return ["telemetry: (no runs in the last 30 days)"]
    lines = ["telemetry (last 30 days, weekly buckets):"]
    for key, by_week in sorted(arms.items()):
        lines.append("  " + " / ".join(key))
        lines.extend(render_week(week, by_week[week]) for week in sorted(by_week) if week < weeks)
    return lines


def _render_history(routes: list[Route]) -> list[str]:
    if not routes:
        return ["recent decisions: (none)"]
    return ["recent decisions:"] + [
        f"  {triple(r.cli, r.model, r.effort)} — {r.reason}" for r in routes
    ]


def _render_outcomes(rows: list[sqlite3.Row], samples: int, outcome: TaskText) -> list[str]:
    finished = [row for row in rows if _status(row) == "ok"][:samples]
    if not finished:
        return []
    return ["recent outcomes (newest first):"] + [
        f"  - {' / '.join(_arm_key(row))}: "
        f"{(outcome(row['id']) or '(no result line)')[:OUTCOME_CHARS]}"
        for row in finished
    ]


def _render_role_header(role: str, current: dict[str, str]) -> list[str]:
    meta = role_metadata(role) or {}
    return [
        f"## ROLE {role}",
        f"description: {meta.get('description', '')}",
        f"reads: {meta.get('reads', '')}",
        f"writes: {meta.get('writes', '')}",
        f"impact: {meta.get('impact', '')}",
        f"current: {current.get('cli') or '?'} / "
        f"{current.get('model') or '(cli default)'} / "
        f"{current.get('effort') or '(cli default)'} ({current.get('cli_source', '')})",
    ]


def render_role(conn: sqlite3.Connection, role: str, current: dict[str, str], now: int,
                samples: int, weeks: int, failure_reason: TaskText,
                outcome: TaskText) -> str:
    rows = _task_rows(conn, role, now - WINDOW_S)
    lines = (
        _render_role_header(role, current)
        + _render_history(recent_routes(conn, role, 3))
        + _render_telemetry(rows, now, weeks, failure_reason)
        + _render_outcomes(rows, samples, outcome)
    )
    return "\n".join(lines)


def _efforts_text(options: CliOptions, model: str) -> str:
    if options.effort_mode == "model":
        return "effort is encoded in the model; use an empty effort"
    return ", ".join(options.efforts_for(model))


def render_catalog(options: dict[str, CliOptions]) -> str:
    lines = ["## AVAILABLE OPTIONS (cli / model: allowed efforts)"]
    for cli in sorted(options):
        lines.extend(
            f"  {cli} / {model}: {_efforts_text(options[cli], model)}"
            for model in options[cli].models
        )
    return "\n".join(lines)


def _default_failure_reason(task_id: str) -> str:
    from .notify import _reason_from_task_log

    return _reason_from_task_log(task_id)


def _default_outcome(task_id: str) -> str:
    from .agent_status import extract_useful_result

    return extract_useful_result(task_id)


def build_digest(conn: sqlite3.Connection, roles: list[str],
                 options: dict[str, CliOptions], current: dict[str, dict[str, str]],
                 now: int, failure_reason: Optional[TaskText] = None,
                 outcome: Optional[TaskText] = None) -> str:
    """Render the router's input, trimmed to MAX_DIGEST_CHARS."""
    failure_reason = failure_reason or _default_failure_reason
    outcome = outcome or _default_outcome
    text = ""
    for samples, weeks in _TRIM_STEPS:
        sections = [
            render_role(conn, role, current.get(role, {}), now, samples, weeks,
                        failure_reason, outcome)
            for role in roles
        ]
        text = "\n\n".join(sections + [render_catalog(options)])
        if len(text) <= MAX_DIGEST_CHARS:
            return text
    return text[:MAX_DIGEST_CHARS]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_model_router_digest.py tests/test_agent_status.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add threadkeeper/agent_status.py threadkeeper/model_router_digest.py tests/test_model_router_digest.py
git commit -m "feat: build the model-router telemetry digest"
```

---

### Task 5: `model_route_write` tool

**Files:**
- Create: `threadkeeper/tools/model_router.py`
- Modify: `threadkeeper/server.py` (tool imports, next to `from .tools import dialectic_feed`)
- Modify: `threadkeeper/adapters/base.py:27` (`CHILD_MCP_ENV_KEYS`)
- Modify: `threadkeeper/config.py` (`_EXTRA_THREADKEEPER_ENV_KEYS`)
- Modify: `docs/ARCHITECTURE.md` (MCP tools table)
- Test: `tests/test_model_router_tools.py`

**Interfaces:**
- Consumes: `model_routes.PASS_ID_ENV`, `get_pass`, `decision_errors`, `cached_options`, `insert_routes`, `finish_pass`, `latest_route`, `routing_summary`, `triple`; `config.SPAWNED_CHILD`, `config.WRITE_ORIGIN`; `identity._ensure_session`, `identity._emit`.
- Produces: MCP tool `model_route_write(pass_id: str, decisions: list[dict] | str) -> str` returning `ok <summary>`, `ok already_recorded`, or `ERR …`; event kind `model_router_pass`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_model_router_tools.py`:

```python
"""model_route_write — the router child's only write path."""
from __future__ import annotations

import json

CATALOG = {
    "claude": {"models": ["opus", "sonnet"]},
    "codex": {"models": ["gpt-5.6-luna", "gpt-5.6-terra"],
              "effort_options": ["low", "medium", "high", "xhigh"]},
}
DECISIONS = [
    {"role": "curator", "cli": "codex", "model": "gpt-5.6-luna", "effort": "high",
     "reason": "luna matches terra outcomes at lower cost"},
    {"role": "shadow_observer", "cli": "claude", "model": "sonnet", "effort": "high",
     "reason": "terra hit capacity errors this week"},
]


def _setup(pkg, monkeypatch, *, origin="model_router", child=True, env_pass=None):
    from threadkeeper import model_routes
    conn = pkg["db"].get_db()
    router_pass = model_routes.open_pass(conn, ["curator", "shadow_observer"], 1000)
    monkeypatch.setattr(pkg["config"], "SPAWNED_CHILD", child)
    monkeypatch.setattr(pkg["config"], "WRITE_ORIGIN", origin)
    monkeypatch.setenv(model_routes.PASS_ID_ENV, env_pass or router_pass.pass_id)
    monkeypatch.setattr(model_routes, "cached_options",
                        lambda: model_routes.options_from_catalog(CATALOG))
    return conn, router_pass


def _write(pass_id, decisions):
    from threadkeeper.tools.model_router import model_route_write
    return model_route_write(pass_id=pass_id, decisions=decisions)


def test_write_records_routes_completes_pass_and_emits_event(fresh_mp, monkeypatch):
    from threadkeeper import model_routes
    conn, router_pass = _setup(fresh_mp, monkeypatch)
    result = _write(router_pass.pass_id, DECISIONS)

    assert result.startswith("ok routed ")
    assert model_routes.latest_route(conn, "curator").model == "gpt-5.6-luna"
    assert model_routes.get_pass(conn, router_pass.pass_id).status == "complete"
    event = conn.execute(
        "SELECT target, summary FROM events WHERE kind='model_router_pass' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert event["target"] == router_pass.pass_id
    assert "curator:" in event["summary"]


def test_write_accepts_json_string_decisions(fresh_mp, monkeypatch):
    from threadkeeper import model_routes
    conn, router_pass = _setup(fresh_mp, monkeypatch)
    assert _write(router_pass.pass_id, json.dumps(DECISIONS)).startswith("ok routed ")
    assert model_routes.latest_route(conn, "shadow_observer").cli == "claude"


def test_replay_after_completion_is_a_no_op(fresh_mp, monkeypatch):
    conn, router_pass = _setup(fresh_mp, monkeypatch)
    _write(router_pass.pass_id, DECISIONS)
    assert _write(router_pass.pass_id, DECISIONS) == "ok already_recorded"
    count = conn.execute("SELECT COUNT(*) FROM model_routes").fetchone()[0]
    assert count == 2


def test_write_rejects_unauthorized_callers(fresh_mp, monkeypatch):
    _, router_pass = _setup(fresh_mp, monkeypatch, origin="curator")
    assert _write(router_pass.pass_id, DECISIONS) == "ERR route_write_not_authorized"


def test_write_rejects_foreground_and_wrong_pass(fresh_mp, monkeypatch):
    _, router_pass = _setup(fresh_mp, monkeypatch, child=False)
    assert _write(router_pass.pass_id, DECISIONS) == "ERR route_write_not_authorized"
    monkeypatch.setattr(fresh_mp["config"], "SPAWNED_CHILD", True)
    assert _write("mr-other", DECISIONS) == "ERR route_write_not_authorized"


def test_invalid_decisions_write_nothing(fresh_mp, monkeypatch):
    from threadkeeper import model_routes
    conn, router_pass = _setup(fresh_mp, monkeypatch)
    bad = [dict(DECISIONS[0], effort="max"), dict(DECISIONS[1], model="gpt-9")]
    result = _write(router_pass.pass_id, bad)

    assert result.startswith("ERR invalid_decisions")
    assert "effort 'max' is not allowed for gpt-5.6-luna" in result
    assert "model 'gpt-9' is not offered by claude" in result
    assert conn.execute("SELECT COUNT(*) FROM model_routes").fetchone()[0] == 0
    assert model_routes.get_pass(conn, router_pass.pass_id).status == "running"


def test_unparseable_or_unknown_input(fresh_mp, monkeypatch):
    _, router_pass = _setup(fresh_mp, monkeypatch)
    assert _write(router_pass.pass_id, "not json") == "ERR decisions_must_be_a_list"
    assert _write(router_pass.pass_id, {"role": "curator"}) == "ERR decisions_must_be_a_list"
    monkeypatch.setenv("THREADKEEPER_MODEL_ROUTER_PASS_ID", "mr-missing")
    assert _write("mr-missing", DECISIONS) == "ERR unknown_pass"


def test_pass_id_reaches_child_mcp_env():
    from threadkeeper.adapters.base import CHILD_MCP_ENV_KEYS
    assert "THREADKEEPER_MODEL_ROUTER_PASS_ID" in CHILD_MCP_ENV_KEYS
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_model_router_tools.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'threadkeeper.tools.model_router'`.

- [ ] **Step 3: Plumb the pass-id environment key**

In `threadkeeper/adapters/base.py` `CHILD_MCP_ENV_KEYS`, after `"THREADKEEPER_CURATOR_SNAPSHOT_DIR",` add:

```python
    "THREADKEEPER_MODEL_ROUTER_PASS_ID",
```

In `threadkeeper/config.py` `_EXTRA_THREADKEEPER_ENV_KEYS`, after `"THREADKEEPER_CURATOR_SNAPSHOT_DIR",` add:

```python
    "THREADKEEPER_MODEL_ROUTER_PASS_ID",
```

- [ ] **Step 4: Create `threadkeeper/tools/model_router.py`**

```python
"""Model router MCP tools.

  model_route_write(pass_id, decisions) — the router child records its decisions
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

from .. import config
from .. import model_routes as routes
from .._mcp import write_tool
from ..agent_metadata import ROUTER_ROLE
from ..db import get_db
from ..identity import _emit, _ensure_session


def _write_authorized(pass_id: str) -> bool:
    return (
        bool(pass_id)
        and bool(config.SPAWNED_CHILD)
        and config.WRITE_ORIGIN == ROUTER_ROLE
        and os.environ.get(routes.PASS_ID_ENV, "").strip() == pass_id
    )


def _decision_list(decisions: Any) -> list[Any] | None:
    if isinstance(decisions, str):
        try:
            decisions = json.loads(decisions)
        except ValueError:
            return None
    return decisions if isinstance(decisions, list) else None


def _previous_triple(conn: sqlite3.Connection, role: str) -> str:
    route = routes.latest_route(conn, role)
    if route is not None:
        return routes.triple(route.cli, route.model, route.effort)
    from ..identity import active_cli
    from ..spawn_config import resolution_details

    details = resolution_details(role, active_cli())
    return routes.triple(details["cli"], details["model"], details["effort"])


def _record(conn: sqlite3.Connection, router_pass: routes.RouterPass,
            decisions: list[dict[str, str]]) -> str:
    now = int(time.time())
    previous = {role: _previous_triple(conn, role) for role in router_pass.roles}
    recorded = routes.insert_routes(conn, decisions, router_pass.pass_id, now)
    summary = routes.routing_summary(recorded, previous)
    routes.finish_pass(conn, router_pass.pass_id, "complete", now)
    _emit(conn, "model_router_pass", target=router_pass.pass_id, summary=summary)
    conn.commit()
    return summary


@write_tool(idempotent=True)
def model_route_write(pass_id: str, decisions: list[dict] | str) -> str:
    """Record the model router's decision for every role in this pass.

    Only the spawned router child carrying the parent-issued pass id may call
    this. `decisions` is a list (or a JSON-encoded list) with one object per
    role: {"role", "cli", "model", "effort", "reason"}. Every decision is
    checked against the local model catalog; any problem rejects the whole
    call and lists every error so the router can fix them and call again.
    """
    clean_id = (pass_id or "").strip()
    if not _write_authorized(clean_id):
        return "ERR route_write_not_authorized"
    decision_list = _decision_list(decisions)
    if decision_list is None:
        return "ERR decisions_must_be_a_list"
    conn = get_db()
    _ensure_session(conn)
    router_pass = routes.get_pass(conn, clean_id)
    if router_pass is None:
        return "ERR unknown_pass"
    if router_pass.status == "complete":
        return "ok already_recorded"
    if router_pass.status != "running":
        return f"ERR pass_not_running status={router_pass.status}"
    errors = routes.decision_errors(
        decision_list, set(router_pass.roles), routes.cached_options()
    )
    if errors:
        return "ERR invalid_decisions\n" + "\n".join(f"- {e}" for e in errors)
    return f"ok {_record(conn, router_pass, decision_list)}"
```

- [ ] **Step 5: Register the module and document the tool**

In `threadkeeper/server.py`, after `from .tools import dialectic_feed  # noqa: F401` add:

```python
from .tools import model_router  # noqa: F401
```

In `docs/ARCHITECTURE.md`, add a row to the `| Module | Tools |` table after the `dialectic_feed` row:

```
| model_router | model_route_write |
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_model_router_tools.py tests/test_documentation_inventory.py tests/test_tool_annotations.py tests/test_config_settings.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add threadkeeper/tools/model_router.py threadkeeper/server.py threadkeeper/adapters/base.py threadkeeper/config.py docs/ARCHITECTURE.md tests/test_model_router_tools.py
git commit -m "feat: add model_route_write for router decisions"
```

---

### Task 6: Router loop and daemon registration

**Files:**
- Create: `threadkeeper/model_router.py`
- Modify: `threadkeeper/host.py` (`_DAEMON_STARTERS`, after `dialectic_validator`)
- Modify: `threadkeeper/agent_status.py` (`_ROLE_HINTS`, `_ROLE_DESCRIPTIONS`, `_LOOP_DEFS`, `_DAEMON_BINDINGS`)
- Modify: `threadkeeper/tools/dashboard.py` (`_LOOP_TASK_PREFIXES`)
- Modify: `threadkeeper/notify.py` (`_LOOP_PASS_KINDS`)
- Test: `tests/test_model_router.py`

**Interfaces:**
- Consumes: Tasks 1–5 (`spawn_config.roles_on_auto`, `spawn_config.resolution_details`, `model_routes.*`, `model_router_digest.build_digest`, tool name `mcp__thread-keeper__model_route_write`); `tools.spawn._spawn_impl`; `spawn_result.parse_spawn_result`; `daemon_state.claim_pass`; `helpers.single_flight_lock`, `helpers.alive`, `helpers.daemon_sleep`.
- Produces: `model_router.run_router_pass(force: bool = False, *, scheduled: bool = False) -> str`; `model_router.decide(state: TickState) -> str` with actions `skip | running | fallback | fail | backoff | start | idle`; `model_router.start_model_router_daemon()`; `ROUTER_PROMPT_PREFIX = "You are a MODEL ROUTER"`; event kind `model_router_pass`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_model_router.py`:

```python
"""model_router — tick state machine, primary/fallback spawning, backoff."""
from __future__ import annotations

import os
import time

CATALOG = {
    "claude": {"models": ["opus", "sonnet"]},
    "codex": {"models": ["gpt-5.6-luna", "gpt-6.1-sol"],
              "effort_options": ["low", "medium", "high", "xhigh"]},
}


def _setup(pkg, monkeypatch, auto_roles=("curator",)):
    from threadkeeper import model_router, model_routes
    config = pkg["config"]
    monkeypatch.setattr(config, "MODEL_ROUTER_INTERVAL_S", 604800.0)
    for role in auto_roles:
        monkeypatch.setitem(config.settings.spawn.loop, role, "auto")
    monkeypatch.setattr(model_routes, "cached_options",
                        lambda: model_routes.options_from_catalog(CATALOG))
    conn = pkg["db"].get_db()
    calls = []

    def fake_spawn(prompt, cli="", model="", effort=""):
        task_id = f"tk_{len(calls):06x}"
        calls.append({"prompt": prompt, "cli": cli, "model": model, "effort": effort,
                      "pass_env": os.environ.get(model_routes.PASS_ID_ENV)})
        conn.execute(
            "INSERT INTO tasks (id, pid, cwd, prompt, started_at, role) "
            "VALUES (?, ?, '/tmp', ?, ?, 'model_router')",
            (task_id, os.getpid(), prompt, int(time.time())),
        )
        conn.commit()
        return f"ok task={task_id} pid={os.getpid()}"

    monkeypatch.setattr(model_router, "_spawn_child", fake_spawn)
    return model_router, model_routes, conn, calls


def _exit(conn, task_id, rc):
    conn.execute("UPDATE tasks SET ended_at=?, return_code=? WHERE id=?",
                 (int(time.time()), rc, task_id))
    conn.commit()


def _record_valid_route(model_routes, conn, router_pass, decided_at=None):
    model_routes.insert_routes(conn, [{"role": "curator", "cli": "codex",
        "model": "gpt-5.6-luna", "effort": "high", "reason": "r"}],
        router_pass.pass_id, decided_at or int(time.time()))
    model_routes.finish_pass(conn, router_pass.pass_id, "complete",
                             decided_at or int(time.time()))
    conn.commit()


def test_disabled_without_force(fresh_mp):
    from threadkeeper import model_router
    assert model_router.run_router_pass() == "disabled"


def test_skip_without_auto_roles(fresh_mp, monkeypatch):
    router, _, _, calls = _setup(fresh_mp, monkeypatch, auto_roles=())
    assert router.run_router_pass() == "skip no_auto_roles"
    assert calls == []


def test_new_auto_role_starts_primary_pass(fresh_mp, monkeypatch):
    router, routes, conn, calls = _setup(fresh_mp, monkeypatch)
    result = router.run_router_pass()

    assert result.startswith("spawned attempt=primary pass=mr-")
    call = calls[0]
    assert (call["cli"], call["model"], call["effort"]) == ("", "", "")
    assert call["prompt"].startswith(router.ROUTER_PROMPT_PREFIX)
    assert "## ROLE curator" in call["prompt"]
    router_pass = routes.latest_pass(conn)
    assert call["pass_env"] == router_pass.pass_id
    assert os.environ.get(routes.PASS_ID_ENV) is None
    assert (router_pass.roles, router_pass.status, router_pass.task_id) == (
        ("curator",), "running", "tk_000000")


def test_running_child_blocks_new_spawn(fresh_mp, monkeypatch):
    router, _, _, calls = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    assert router.run_router_pass().startswith("running pass=mr-")
    assert len(calls) == 1


def test_failed_primary_falls_back_to_sol(fresh_mp, monkeypatch):
    router, routes, conn, calls = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    _exit(conn, "tk_000000", 1)
    result = router.run_router_pass()

    assert result.startswith("spawned attempt=fallback")
    assert (calls[1]["cli"], calls[1]["model"], calls[1]["effort"]) == (
        "codex", "gpt-6.1-sol", "xhigh")
    assert calls[1]["pass_env"] == calls[0]["pass_env"]
    assert routes.latest_pass(conn).attempt == "fallback"


def test_clean_exit_without_decision_also_falls_back(fresh_mp, monkeypatch):
    router, _, conn, _ = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    _exit(conn, "tk_000000", 0)
    assert router.run_router_pass().startswith("spawned attempt=fallback")


def test_recorded_pass_does_not_fall_back_after_crash(fresh_mp, monkeypatch):
    router, routes, conn, calls = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    _record_valid_route(routes, conn, routes.latest_pass(conn))
    _exit(conn, "tk_000000", 1)
    assert router.run_router_pass().startswith("idle next_due_in=")
    assert len(calls) == 1


def test_failed_fallback_marks_pass_and_backs_off(fresh_mp, monkeypatch):
    router, routes, conn, calls = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    _exit(conn, "tk_000000", 1)
    router.run_router_pass()
    _exit(conn, "tk_000001", 1)

    assert router.run_router_pass().startswith("spawn_failed pass=mr-")
    assert routes.latest_pass(conn).status == "failed"
    assert router.run_router_pass().startswith("backoff pass=mr-")
    assert len(calls) == 2

    conn.execute("UPDATE model_router_passes SET ended_at=?",
                 (int(time.time()) - router._BACKOFF_S - 1,))
    conn.commit()
    assert router.run_router_pass().startswith("spawned attempt=primary")
    assert len(calls) == 3


def test_spawn_error_on_primary_falls_back_next_tick(fresh_mp, monkeypatch):
    router, routes, conn, _ = _setup(fresh_mp, monkeypatch)
    monkeypatch.setattr(router, "_spawn_child",
                        lambda prompt, cli="", model="", effort="": "ERR budget_exceeded")
    assert router.run_router_pass().startswith("spawn_error attempt=primary")
    assert routes.latest_pass(conn).task_id == ""
    assert router.run_router_pass().startswith("spawn_error attempt=fallback")


def test_idle_until_interval_then_due(fresh_mp, monkeypatch):
    router, routes, conn, calls = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    router_pass = routes.latest_pass(conn)
    _record_valid_route(routes, conn, router_pass)
    _exit(conn, "tk_000000", 0)
    assert router.run_router_pass().startswith("idle next_due_in=")

    conn.execute("UPDATE model_router_passes SET ended_at=?",
                 (int(time.time()) - 604800 - 1,))
    conn.commit()
    assert router.run_router_pass().startswith("spawned attempt=primary")
    assert len(calls) == 2


def test_role_added_mid_pass_starts_next_pass(fresh_mp, monkeypatch):
    router, routes, conn, calls = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    monkeypatch.setitem(fresh_mp["config"].settings.spawn.loop, "probe_runner", "auto")
    assert router.run_router_pass().startswith("running pass=")

    _record_valid_route(routes, conn, routes.latest_pass(conn))
    _exit(conn, "tk_000000", 0)
    assert router.run_router_pass().startswith("spawned attempt=primary")
    assert routes.latest_pass(conn).roles == ("curator", "probe_runner")


def test_force_starts_even_when_idle(fresh_mp, monkeypatch):
    router, routes, conn, calls = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    _record_valid_route(routes, conn, routes.latest_pass(conn))
    _exit(conn, "tk_000000", 0)
    assert router.run_router_pass(force=True).startswith("spawned attempt=primary")


def test_state_changes_are_recorded_as_events(fresh_mp, monkeypatch):
    router, _, conn, _ = _setup(fresh_mp, monkeypatch)
    router.run_router_pass()
    router.run_router_pass()
    kinds = conn.execute(
        "SELECT summary FROM events WHERE kind='model_router_pass' ORDER BY id"
    ).fetchall()
    assert [row["summary"].split()[0] for row in kinds] == ["spawned"]


def test_loop_is_registered(fresh_mp):
    from threadkeeper import agent_status, host
    from threadkeeper.notify import _LOOP_PASS_KINDS
    from threadkeeper.tools.dashboard import _LOOP_TASK_PREFIXES

    loop = next(d for d in agent_status._LOOP_DEFS if d["id"] == "model_router")
    assert (loop["event"], loop["role"], loop["interval"]) == (
        "model_router_pass", "model_router", "MODEL_ROUTER_INTERVAL_S")
    assert ("model_router", "start_model_router_daemon") in host._DAEMON_STARTERS
    assert "model_router_pass" in _LOOP_PASS_KINDS
    assert _LOOP_TASK_PREFIXES["model_router"] == "You are a MODEL ROUTER"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_model_router.py -q`
Expected: FAIL with `ImportError: cannot import name 'model_router' from 'threadkeeper'`.

- [ ] **Step 3: Create `threadkeeper/model_router.py`**

```python
"""Model router loop — weekly CLI/model/effort routing for `auto` roles.

Each hourly tick resolves to exactly one action (see `decide`). A pass spawns
the router on its primary settings. If that child exits without recording a
decision, the same pass reruns once on the fallback settings; a pass that
fails both ways backs off before the primary is tried again.
"""
from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from . import config, daemon_state, identity
from . import model_routes as routes
from .agent_metadata import ROUTER_ROLE
from .db import get_db
from .helpers import alive, daemon_sleep, single_flight_lock
from .identity import _ensure_session
from .spawn_result import parse_spawn_result

logger = logging.getLogger(__name__)

_started = False
_TICK_S = 3600
_BACKOFF_S = 6 * 3600
_RECORDED_ACTIONS = frozenset({"start", "fallback", "fail"})
ROUTER_TOOL = "mcp__thread-keeper__model_route_write"
ROUTER_PROMPT_PREFIX = "You are a MODEL ROUTER"

ROUTER_PROMPT = ROUTER_PROMPT_PREFIX + """ for thread-keeper's background agents.

Each role below runs as a spawned child on one CLI, model and reasoning effort.
Choose the best CLI + model + effort for every listed role for the next week.

Weigh three things:
  1. Price: tokens per run, reasoning effort, and model tier. Cheaper is
     better when quality holds.
  2. Speed and reliability: run time, success rate, and provider failures
     such as "Selected model is at capacity".
  3. Result quality: the recent outcome lines of each role.

Rules:
  - Keeping the current setting is a valid and expected answer. Switch only
    when the data gives a concrete reason, and name that reason.
  - With fewer than 5 runs on the current setting, prefer keeping it.
  - Roles with impact code_write or external_write weigh quality above price.
  - Choose only from AVAILABLE OPTIONS. The effort must be listed for that
    model; for a CLI that encodes effort in the model, use an empty effort.

When you are done, call model_route_write exactly once with
  pass_id = "%(pass_id)s"
  decisions = a list with one object per role:
              {"role", "cli", "model", "effort", "reason"}
  roles to decide: %(roles)s
If it returns ERR, fix every listed problem and call it again.
Finish with one line: "Routed N of M roles."

%(fence)s

%(digest)s
"""


@dataclass(frozen=True)
class TickState:
    roles: tuple[str, ...]
    last_pass: Optional[routes.RouterPass]
    child_running: bool
    last_complete_at: Optional[int]
    missing_route: bool
    now: int
    interval_s: float
    force: bool = False


def _pending_action(pending: routes.RouterPass, child_running: bool) -> str:
    if child_running:
        return "running"
    return "fallback" if pending.attempt == "primary" else "fail"


def _in_backoff(state: TickState) -> bool:
    last = state.last_pass
    if last is None or last.status != "failed":
        return False
    return state.now - int(last.ended_at or 0) < _BACKOFF_S


def _due(state: TickState) -> bool:
    if state.missing_route or state.last_complete_at is None:
        return True
    return state.now - state.last_complete_at >= state.interval_s


def decide(state: TickState) -> str:
    """Name the single action this tick takes."""
    if not state.roles:
        return "skip"
    last = state.last_pass
    if last is not None and last.status == "running":
        return _pending_action(last, state.child_running)
    if state.force:
        return "start"
    if _in_backoff(state):
        return "backoff"
    return "start" if _due(state) else "idle"


def _child_running(conn: sqlite3.Connection, task_id: str, now: int) -> bool:
    """True while the pass's child is alive; reaps a dead row on the way."""
    if not task_id:
        return False
    row = conn.execute(
        "SELECT pid, ended_at FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None or row["ended_at"] is not None:
        return False
    if alive(int(row["pid"] or 0)):
        return True
    conn.execute(
        "UPDATE tasks SET ended_at=? WHERE id=? AND ended_at IS NULL", (now, task_id)
    )
    conn.commit()
    return False


def _tick_state(conn: sqlite3.Connection, now: int, interval_s: float,
                force: bool) -> TickState:
    from .spawn_config import roles_on_auto

    roles = tuple(roles_on_auto())
    last_pass = routes.latest_pass(conn)
    pending_task = last_pass.task_id if last_pass and last_pass.status == "running" else ""
    return TickState(
        roles=roles,
        last_pass=last_pass,
        child_running=_child_running(conn, pending_task, now),
        last_complete_at=routes.last_complete_at(conn),
        missing_route=any(routes.active_route(role) is None for role in roles),
        now=now,
        interval_s=interval_s,
        force=force,
    )


def build_prompt(conn: sqlite3.Connection, router_pass: routes.RouterPass) -> str:
    from .model_router_digest import build_digest
    from .review_prompts import DATA_FENCE, fence_observed
    from .spawn_config import resolution_details

    active = identity.active_cli()
    current = {role: resolution_details(role, active) for role in router_pass.roles}
    digest = build_digest(
        conn, list(router_pass.roles), routes.cached_options(), current,
        int(time.time()),
    )
    return ROUTER_PROMPT % {
        "pass_id": router_pass.pass_id,
        "roles": ", ".join(router_pass.roles),
        "fence": DATA_FENCE,
        # Outcome lines echo child output, which can carry untrusted text.
        "digest": fence_observed(digest, "router telemetry"),
    }


def _spawn_child(prompt: str, cli: str = "", model: str = "", effort: str = "") -> str:
    from .tools.spawn import _spawn_impl  # type: ignore

    return _spawn_impl(
        prompt=prompt,
        visible=False,
        capture_output=True,
        permission_mode="auto",
        role=ROUTER_ROLE,
        write_origin=ROUTER_ROLE,
        slim=True,
        extra_allowed_tools=ROUTER_TOOL,
        cli=cli,
        model=model,
        effort=effort,
    )


@contextlib.contextmanager
def _pass_env(pass_id: str) -> Iterator[None]:
    """Export the pass id only around the launch, never to other loops."""
    previous = os.environ.get(routes.PASS_ID_ENV)
    os.environ[routes.PASS_ID_ENV] = pass_id
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(routes.PASS_ID_ENV, None)
        else:
            os.environ[routes.PASS_ID_ENV] = previous


def _attempt_settings(attempt: str) -> dict[str, str]:
    if attempt == "primary":
        return {}
    return {
        "cli": config.MODEL_ROUTER_FALLBACK_CLI,
        "model": config.MODEL_ROUTER_FALLBACK_MODEL,
        "effort": config.MODEL_ROUTER_FALLBACK_EFFORT,
    }


def _launch(conn: sqlite3.Connection, router_pass: routes.RouterPass, attempt: str) -> str:
    prompt = build_prompt(conn, router_pass)
    with _pass_env(router_pass.pass_id):
        result = parse_spawn_result(_spawn_child(prompt, **_attempt_settings(attempt)))
    routes.set_attempt(conn, router_pass.pass_id, attempt, result.task_id or "")
    conn.commit()
    if not result.ok:
        return (
            f"spawn_error attempt={attempt} pass={router_pass.pass_id} "
            f":: {result.text[:160]}"
        )
    return f"spawned attempt={attempt} pass={router_pass.pass_id} task={result.task_id}"


def _skip(conn: sqlite3.Connection, state: TickState) -> str:
    return "skip no_auto_roles"


def _running(conn: sqlite3.Connection, state: TickState) -> str:
    return f"running pass={state.last_pass.pass_id} attempt={state.last_pass.attempt}"


def _fallback(conn: sqlite3.Connection, state: TickState) -> str:
    return _launch(conn, state.last_pass, "fallback")


def _fail(conn: sqlite3.Connection, state: TickState) -> str:
    routes.finish_pass(conn, state.last_pass.pass_id, "failed", state.now)
    conn.commit()
    return f"spawn_failed pass={state.last_pass.pass_id} attempts=primary,fallback"


def _backoff(conn: sqlite3.Connection, state: TickState) -> str:
    retry_in = int(state.last_pass.ended_at or 0) + _BACKOFF_S - state.now
    return f"backoff pass={state.last_pass.pass_id} retry_in={max(0, retry_in)}s"


def _start(conn: sqlite3.Connection, state: TickState) -> str:
    return _launch(conn, routes.open_pass(conn, list(state.roles), state.now), "primary")


def _idle(conn: sqlite3.Connection, state: TickState) -> str:
    next_due = int(state.last_complete_at or state.now) + int(state.interval_s)
    return f"idle next_due_in={max(0, next_due - state.now)}s"


_ACTIONS: dict[str, Callable[[sqlite3.Connection, TickState], str]] = {
    "skip": _skip,
    "running": _running,
    "fallback": _fallback,
    "fail": _fail,
    "backoff": _backoff,
    "start": _start,
    "idle": _idle,
}


def _record_tick(conn: sqlite3.Connection, outcome: str) -> None:
    try:
        identity._emit(
            conn, "model_router_pass", target=str(int(time.time())),
            summary=outcome[:300],
        )
        conn.commit()
    except sqlite3.Error:
        logger.debug("model_router: record_tick failed", exc_info=True)


def run_router_pass(force: bool = False, *, scheduled: bool = False) -> str:
    """One router tick. `force` starts a pass even when none is due."""
    interval_s = float(config.MODEL_ROUTER_INTERVAL_S)
    if interval_s <= 0 and not force:
        return "disabled"
    conn = get_db()
    if not daemon_state.claim_pass(
        "model_router", _TICK_S, scheduled=scheduled, conn=conn,
    ):
        return "not_due"
    _ensure_session(conn)
    with single_flight_lock("model-router") as locked:
        if not locked:
            return "router_tick_running (single-flight lock)"
        state = _tick_state(conn, int(time.time()), interval_s, force)
        action = decide(state)
        outcome = _ACTIONS[action](conn, state)
        if action in _RECORDED_ACTIONS:
            _record_tick(conn, outcome)
        return outcome


def _serve_loop() -> None:
    while True:
        try:
            run_router_pass(scheduled=True)
        except Exception:
            logger.debug("model_router tick failed", exc_info=True)
        daemon_sleep(_TICK_S)


def start_model_router_daemon() -> None:
    """Idempotent. Same cascade guards as the other spawning loops."""
    global _started
    from .daemon_liveness import daemon_thread_alive, start_daemon_thread

    if _started and daemon_thread_alive("model_router"):
        return
    if float(config.MODEL_ROUTER_INTERVAL_S) <= 0:
        return
    from .config import BACKGROUND_DAEMONS_ALLOWED, SEMANTIC_AVAILABLE

    if not BACKGROUND_DAEMONS_ALLOWED or not SEMANTIC_AVAILABLE:
        return
    start_daemon_thread("model_router", _serve_loop)
    _started = True
```

- [ ] **Step 4: Register the loop**

`threadkeeper/host.py` `_DAEMON_STARTERS`, after `("dialectic_validator", "start_dialectic_validator_daemon"),`:

```python
    ("model_router", "start_model_router_daemon"),
```

`threadkeeper/agent_status.py`:
- `_ROLE_HINTS`, add before the `CURATOR` entry: `(re.compile(r"\bMODEL ROUTER\b", re.I), "model_router"),`
- `_ROLE_DESCRIPTIONS`, add:

```python
    "model_router": (
        "Weekly reviews which CLI, model and effort each auto role runs on, "
        "and re-assigns them."
    ),
```

- `_LOOP_DEFS`, add after the `dialectic_validator` entry:

```python
    {
        "id": "model_router",
        "name": "Model router",
        "event": "model_router_pass",
        "interval": "MODEL_ROUTER_INTERVAL_S",
        "role": "model_router",
        "description": _ROLE_DESCRIPTIONS["model_router"],
        "work": "Re-assigns CLI, model and effort for roles set to auto",
    },
```

- `_DAEMON_BINDINGS`, add: `"model_router": ("model_router", "model_router", "start_model_router_daemon"),`

`threadkeeper/tools/dashboard.py` `_LOOP_TASK_PREFIXES`, add: `"model_router": "You are a MODEL ROUTER",`

`threadkeeper/notify.py` `_LOOP_PASS_KINDS`, add `"model_router_pass",` after `"dialectic_validate_pass",`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_model_router.py tests/test_dashboard.py tests/test_agent_status.py tests/test_daemon_liveness.py tests/test_host_main.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add threadkeeper/model_router.py threadkeeper/host.py threadkeeper/agent_status.py threadkeeper/tools/dashboard.py threadkeeper/notify.py tests/test_model_router.py
git commit -m "feat: run the model router loop with Sol fallback and backoff"
```

---

### Task 7: Operator tools `model_router_run` and `model_router_status`

**Files:**
- Modify: `threadkeeper/tools/model_router.py`
- Modify: `docs/ARCHITECTURE.md` (the `model_router` tool-table row)
- Test: `tests/test_model_router_tools.py`

**Interfaces:**
- Consumes: `model_router.run_router_pass`, `model_routes.latest_pass`, `model_routes.active_route`, `spawn_config.roles_on_auto`, `helpers.fmt_age`.
- Produces: MCP tools `model_router_run(force: bool = True) -> str`, `model_router_status() -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_model_router_tools.py`:

```python
def test_status_without_auto_roles(fresh_mp):
    from threadkeeper.tools.model_router import model_router_status
    text = model_router_status()
    assert "interval_s=0 fallback=codex/gpt-6.1-sol/xhigh" in text
    assert "last pass: none" in text
    assert "auto roles: (none)" in text


def test_status_lists_routes_and_missing_decisions(fresh_mp, monkeypatch):
    from threadkeeper import model_routes
    from threadkeeper.tools.model_router import model_router_status
    conn = fresh_mp["db"].get_db()
    monkeypatch.setattr(fresh_mp["config"], "MODEL_ROUTER_INTERVAL_S", 604800.0)
    monkeypatch.setitem(fresh_mp["config"].settings.spawn.loop, "curator", "auto")
    monkeypatch.setitem(fresh_mp["config"].settings.spawn.loop, "probe_runner", "auto")
    monkeypatch.setattr(model_routes, "cached_options",
                        lambda: model_routes.options_from_catalog(CATALOG))
    router_pass = model_routes.open_pass(conn, ["curator", "probe_runner"], 1000)
    model_routes.insert_routes(conn, [DECISIONS[0]], router_pass.pass_id, 1000)
    conn.commit()

    text = model_router_status()
    assert f"last pass: {router_pass.pass_id} status=running attempt=primary" in text
    assert "auto roles: curator, probe_runner" in text
    assert "curator: codex gpt-5.6-luna high since" in text
    assert "luna matches terra outcomes at lower cost" in text
    assert "probe_runner: no decision yet — follows active CLI" in text


def test_run_tool_forces_a_tick(fresh_mp, monkeypatch):
    from threadkeeper import model_router
    from threadkeeper.tools.model_router import model_router_run
    seen = {}
    monkeypatch.setattr(model_router, "run_router_pass",
                        lambda force=False, scheduled=False: seen.setdefault("force", force) and "ok")
    assert model_router_run() == "ok"
    assert seen == {"force": True}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_model_router_tools.py -q -k "status or run_tool"`
Expected: FAIL with `ImportError: cannot import name 'model_router_status'`.

- [ ] **Step 3: Add the tools**

In `threadkeeper/tools/model_router.py`, extend the module docstring:

```python
"""Model router MCP tools.

  model_route_write(pass_id, decisions) — the router child records its decisions
  model_router_run(force=True)          — run one router tick now
  model_router_status()                 — config, last pass, current routes
"""
```

Change the `_mcp` import to `from .._mcp import read_tool, write_tool` and add `from ..helpers import fmt_age`. Append:

```python
def _config_line() -> str:
    return (
        f"interval_s={float(config.MODEL_ROUTER_INTERVAL_S):.0f} "
        f"fallback={config.MODEL_ROUTER_FALLBACK_CLI}/"
        f"{config.MODEL_ROUTER_FALLBACK_MODEL}/{config.MODEL_ROUTER_FALLBACK_EFFORT}"
    )


def _last_pass_line(router_pass: routes.RouterPass | None, now: int) -> str:
    if router_pass is None:
        return "last pass: none"
    return (
        f"last pass: {router_pass.pass_id} status={router_pass.status} "
        f"attempt={router_pass.attempt} "
        f"started={fmt_age(max(0, now - router_pass.started_at))} ago"
    )


def _route_line(role: str, now: int) -> str:
    route = routes.active_route(role)
    if route is None:
        return f"  {role}: no decision yet — follows active CLI"
    return (
        f"  {role}: {routes.triple(route.cli, route.model, route.effort)} "
        f"since {fmt_age(max(0, now - route.decided_at))} ago — {route.reason}"
    )


@write_tool()
def model_router_run(force: bool = True) -> str:
    """Run one model-router tick now. force=True starts a pass even when
    none is due (a pass that is already running is never duplicated)."""
    from ..model_router import run_router_pass

    return run_router_pass(force=force)


@read_tool()
def model_router_status() -> str:
    """Model-router config, last pass, and the current route of each auto role."""
    from ..spawn_config import roles_on_auto

    now = int(time.time())
    roles = roles_on_auto()
    lines = [
        _config_line(),
        _last_pass_line(routes.latest_pass(get_db()), now),
        "auto roles: " + (", ".join(roles) if roles else "(none)"),
    ]
    lines.extend(_route_line(role, now) for role in roles)
    return "\n".join(lines)
```

Update the ARCHITECTURE row to:

```
| model_router | model_route_write, model_router_run, model_router_status |
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_model_router_tools.py tests/test_documentation_inventory.py tests/test_tool_annotations.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add threadkeeper/tools/model_router.py docs/ARCHITECTURE.md tests/test_model_router_tools.py
git commit -m "feat: add model_router_run and model_router_status"
```

---

### Task 8: Settings catalog fields and menu-bar UI

**Files:**
- Modify: `threadkeeper/model_catalog.py` (`settings_catalog`)
- Modify: `apps/macos-agent-status/ThreadKeeperAgentStatus.swift` (`LearningAgentDefinition` ~line 253, role LOOP definition ~line 747, `cliChoices` ~line 691, `LearningAgentCard` ~line 826, `ImpactBadge` ~line 3097)
- Test: `tests/test_model_routes.py` (settings catalog), `tests/test_menubar_app.py` (Swift compile)

**Interfaces:**
- Consumes: `spawn_config.route_for(role)`.
- Produces: settings-catalog role keys `routed: bool`, `routed_at: int`; Swift `LearningAgentDefinition.routed: Bool?`, `.routedAt: Int?`; `RouterAssignmentLine` view.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_model_routes.py`:

```python
def test_settings_catalog_marks_routed_roles(fresh_mp, monkeypatch):
    from threadkeeper import model_catalog, model_routes

    monkeypatch.setitem(fresh_mp["config"].settings.spawn.loop, "curator", "auto")
    route = model_routes.Route("curator", "codex", "gpt-5.6-luna", "high", "r", "mr-1", 1234)
    monkeypatch.setattr(model_routes, "active_route",
                        lambda role: route if role == "curator" else None)
    monkeypatch.setattr(model_catalog, "cli_catalog", lambda refresh=False: [])

    roles = {r["role"]: r for r in model_catalog.settings_catalog()["agent_roles"]}
    assert (roles["curator"]["routed"], roles["curator"]["routed_at"]) == (True, 1234)
    assert (roles["curator"]["cli"], roles["curator"]["cli_dynamic"]) == ("codex", False)
    assert (roles["shadow_observer"]["routed"], roles["shadow_observer"]["routed_at"]) == (False, 0)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_model_routes.py -q -k settings_catalog`
Expected: FAIL with `KeyError: 'routed'`.

- [ ] **Step 3: Add the catalog fields**

In `threadkeeper/model_catalog.py` `settings_catalog`, inside the `for meta in AGENT_ROLES:` loop, add as the first line after `role = meta["role"]`:

```python
        route = spawn_config.route_for(role)
```

and add two keys to the dict appended to `roles`:

```python
            "routed": route is not None,
            "routed_at": route.decided_at if route is not None else 0,
```

- [ ] **Step 4: Update the Swift app**

In `LearningAgentDefinition`, add the fields and coding keys:

```swift
    let routed: Bool?
    let routedAt: Int?
```

```swift
        case role, name, description, reads, writes, impact, cli, model, effort, routed
        case routedAt = "routed_at"
```

In `EnvSettingsStore`, add next to `cliChoices`:

```swift
    func roleCLIChoices(for role: LearningAgentDefinition) -> [ChoiceOption] {
        guard role.role != "model_router" else { return cliChoices }
        return [ChoiceOption("auto", label: "Auto (model router)")] + cliChoices
    }
```

In `allDefinitions`, change the role LOOP definition to:

```swift
            definitions.append(EnvSettingDefinition(
                group: "Learning Loop Agents",
                key: "THREADKEEPER_SPAWN__LOOP__\(token)",
                title: "\(role.name) CLI",
                detail: "Default inherits the global CLI. Auto lets the model router choose.",
                defaultValue: role.cli.isEmpty ? activeCLI : role.cli,
                kind: .choice(roleCLIChoices(for: role))
            ))
```

In `LearningAgentCard`, add the property:

```swift
    private var isRouted: Bool { draftCLI == "auto" }
```

and wrap the model and effort rows:

```swift
            if isRouted {
                RouterAssignmentLine(role: role)
            } else {
                if let definition = envStore.definition(for: "THREADKEEPER_SPAWN__MODEL__\(token)") {
                    EnvSettingRow(definition: definition, envStore: envStore, showKey: false)
                }
                if cli?.effortMode == "independent",
                   let definition = envStore.definition(for: "THREADKEEPER_SPAWN__EFFORT__\(token)") {
                    EnvSettingRow(definition: definition, envStore: envStore, showKey: false)
                } else if let cli, !cli.effortNote.isEmpty {
                    Label(cli.effortNote, systemImage: "slider.horizontal.below.square.filled.and.square")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
            }
```

Add after `LearningAgentCard`:

```swift
struct RouterAssignmentLine: View {
    let role: LearningAgentDefinition

    private var sinceText: String {
        guard let routedAt = role.routedAt, routedAt > 0 else { return "" }
        let date = Date(timeIntervalSince1970: TimeInterval(routedAt))
        return " (since \(date.formatted(date: .abbreviated, time: .omitted)))"
    }

    private var text: String {
        guard role.routed == true else { return "No decision yet — follows active CLI" }
        let effort = role.effort.isEmpty ? "" : " · \(role.effort)"
        return "Model router: \(role.cli) · \(role.model)\(effort)\(sinceText)"
    }

    var body: some View {
        Label(text, systemImage: "arrow.triangle.branch")
            .font(.system(size: 11))
            .foregroundStyle(.secondary)
    }
}
```

In `ImpactBadge`, add a case:

```swift
            case "config_write": return ("Routing write", .teal)
```

- [ ] **Step 5: Run the tests and the Swift build**

Run: `.venv/bin/python -m pytest tests/test_model_routes.py tests/test_menubar_app.py -q`
Expected: PASS (the Swift compile test runs when `swiftc` is installed).

Run: `apps/macos-agent-status/build.sh`
Expected: exits 0 and produces `apps/macos-agent-status/build/ThreadKeeperAgentStatus.app`.

- [ ] **Step 6: Commit**

```bash
git add threadkeeper/model_catalog.py apps/macos-agent-status/ThreadKeeperAgentStatus.swift tests/test_model_routes.py
git commit -m "feat: show model-router assignments in the settings window"
```

---

### Task 9: Documentation, full suite, and live check

**Files:**
- Modify: `README.md` (spawn routing section ~line 1446; env table ~line 1390)
- Modify: `docs/ARCHITECTURE.md` (daemon list near `dialectic_validator` ~line 543; "Spawn architecture"; "Env knobs")
- Modify: `.env.example` (after `THREADKEEPER_DIALECTIC_VALIDATE_INTERVAL_S`)
- Modify: `CHANGELOG.md` (`## [Unreleased]`)
- Modify: `apps/macos-agent-status/README.md`
- Modify: `docs/superpowers/specs/2026-10-12-model-router-design.md` ("Open checks" → resolved)

- [ ] **Step 1: README**

After the resolution paragraph (`Resolution per role: …`), add:

````markdown
### Model router (`auto` roles)

Set a role's CLI to `auto` to let the weekly **model router** choose its CLI,
model and effort:

```bash
THREADKEEPER_SPAWN__LOOP__SHADOW_OBSERVER=auto
```

The router reviews the last 30 days of that role's spawns (success rate,
failures such as provider capacity errors, run time, tokens, recent outcomes)
plus the local model catalog, and records one decision per `auto` role with a
reason. Until it has decided, `auto` keeps its old meaning and follows the
active CLI. While a role is routed, its `SPAWN__MODEL__<ROLE>` and
`SPAWN__EFFORT__<ROLE>` pins are ignored and `spawn_status()` warns about them.

The router runs on `claude` / `opus` / `max` (override with the usual
`SPAWN__LOOP|MODEL|EFFORT__MODEL_ROUTER` keys). If that run fails, the same pass
reruns on the fallback below; a pass that fails twice retries after 6 hours.
`model_router_status()` shows current decisions and `model_router_run()` runs a
pass now. Set `THREADKEEPER_MODEL_ROUTER_INTERVAL_S=0` to disable the router.
````

Change the existing example line `THREADKEEPER_SPAWN__LOOP__CANDIDATE_REVIEWER=auto  # "auto" = follow active CLI` to:

```bash
THREADKEEPER_SPAWN__LOOP__CANDIDATE_REVIEWER=auto  # "auto" = model router decides (active CLI until it has)
```

Add rows to the env table after `THREADKEEPER_DIALECTIC_VALIDATE_INTERVAL_S`:

```markdown
| `THREADKEEPER_MODEL_ROUTER_INTERVAL_S` | 604800 (weekly) | model-router pass interval (s); 0 disables it and `auto` follows the active CLI |
| `THREADKEEPER_MODEL_ROUTER_FALLBACK_CLI` | codex | CLI for the router's fallback run |
| `THREADKEEPER_MODEL_ROUTER_FALLBACK_MODEL` | gpt-6.1-sol | model for the router's fallback run |
| `THREADKEEPER_MODEL_ROUTER_FALLBACK_EFFORT` | xhigh | effort for the router's fallback run |
```

- [ ] **Step 2: ARCHITECTURE, `.env.example`, app README**

In `docs/ARCHITECTURE.md`, after the `dialectic_validator` daemon bullet add:

```markdown
- **model_router** — ticks hourly; starts a pass once per
  `MODEL_ROUTER_INTERVAL_S` (default 604800, weekly) or as soon as a role set to
  `auto` has no valid decision. The parent builds a fenced telemetry digest
  (`model_router_digest.py`) and spawns the router (`claude`/`opus`/`max` by
  default). The child can only call `model_route_write`, which validates every
  decision against the model catalog and writes `model_routes` atomically. A
  child that exits without a decision triggers one fallback run
  (`MODEL_ROUTER_FALLBACK_*`, default `codex`/`gpt-6.1-sol`/`xhigh`); two failures
  back off for 6 h. State lives in `model_router_passes`.
```

In "Spawn architecture", add one paragraph:

```markdown
`spawn_config.resolve_*` checks `route_for(role)` first: a role whose loop value
is `auto` takes CLI, model and effort from its latest `model_routes` row when the
cached model catalog still offers it. Otherwise resolution is unchanged:
`SPAWN__LOOP__<role>` → `ROLE_DEFAULTS` → `SPAWN__DEFAULT` → active CLI →
`claude`.
```

Add the four `THREADKEEPER_MODEL_ROUTER_*` keys to the "Env knobs" list with the same defaults as README.

In `.env.example`, after the dialectic validator line add:

```bash
# Model router: weekly CLI/model/effort routing for roles set to `auto`.
# THREADKEEPER_MODEL_ROUTER_INTERVAL_S=604800
# THREADKEEPER_MODEL_ROUTER_FALLBACK_CLI=codex
# THREADKEEPER_MODEL_ROUTER_FALLBACK_MODEL=gpt-6.1-sol
# THREADKEEPER_MODEL_ROUTER_FALLBACK_EFFORT=xhigh
```

In `apps/macos-agent-status/README.md`, add to the settings description:

```markdown
Each Learning Loop agent's CLI picker also offers **Auto (model router)**. With
Auto selected, the model and effort pickers are hidden and the card shows the
router's current decision and its date, or "No decision yet — follows active
CLI".
```

- [ ] **Step 3: CHANGELOG**

Add at the top of `## [Unreleased]`:

```markdown
- **Added: model router for `auto` roles.** A new weekly loop reviews spawn
  telemetry for every role whose CLI is set to `auto` and picks its CLI, model
  and effort, with a reason per decision. The router runs on Claude Opus at
  `max` effort and reruns on `gpt-6.1-sol` at `xhigh` when that run fails.
  New tools: `model_route_write`, `model_router_run`, `model_router_status`.
  **Behaviour change:** roles already set to `SPAWN__LOOP__<ROLE>=auto` are
  routed once the router records a decision (the first pass runs soon after
  upgrade). Set `THREADKEEPER_MODEL_ROUTER_INTERVAL_S=0` to keep the old
  meaning (follow the active CLI).
```

- [ ] **Step 4: Mark the spec's open checks resolved**

Replace the "Open checks for the plan" section of the spec with:

```markdown
## Open checks (resolved during planning)

- Claude Code 2.1.278 reports `unrecognized_model` for `claude-opus-5-5` and accepts `opus`, so the router default is `opus`.
- Codex children already call thread-keeper write tools from the existing Codex-routed loops; the new tool only needs to be in `extra_allowed_tools`.
- The Swift `ImpactBadge` shows unknown values verbatim; a `config_write` case is added anyway.
```

- [ ] **Step 5: Full test suite**

Run: `.venv/bin/python -m pytest -q -m "not slow"`
Expected: PASS, with no new failures compared to `main`. If anything fails, compare with `git stash && .venv/bin/python -m pytest -q -m "not slow" <failing test>` on `main` before changing code.

- [ ] **Step 6: Commit the docs**

```bash
git add README.md docs/ARCHITECTURE.md .env.example CHANGELOG.md apps/macos-agent-status/README.md docs/superpowers/specs/2026-10-12-model-router-design.md
git commit -m "docs: document the model router and auto routing"
```

- [ ] **Step 7: Live check on the installed host** (requires the user's go-ahead to change `~/.threadkeeper/.env`)

1. Restart the daemon host so it loads the new code, then confirm the loop is alive: `agent_status()` lists "Model router" with `thread_alive=true`.
2. Set one low-risk role to auto: `THREADKEEPER_SPAWN__LOOP__DIALECTIC_VALIDATOR=auto` in `~/.threadkeeper/.env`.
3. Call `model_router_run()`; expect `spawned attempt=primary pass=mr-… task=tk_…`.
4. When the child ends, `model_router_status()` shows a decision with a reason for `dialectic_validator`, and `sqlite3 ~/.threadkeeper/db.sqlite "select * from model_routes order by id desc limit 3"` shows the row.
5. Trigger `dialectic_validate_run()` and confirm the new `tasks` row uses the routed `chosen_cli`, `model` and `effort`.
6. Open the menu-bar settings window: the Dialectic validator card shows "Model router: … (since …)".
