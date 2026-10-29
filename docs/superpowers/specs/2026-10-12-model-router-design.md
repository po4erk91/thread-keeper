# Model router — weekly CLI/model/effort routing for `auto` roles

**Status:** design approved in chat, pending spec review
**Date:** 2026-10-12

## Problem

Every LLM-backed role (`shadow_observer`, `curator`, `evolve_applier`, …) runs on a CLI, model and effort that a human pinned once in `~/.threadkeeper/.env`. The pins drift out of date: new models appear in `model_catalog.json` (e.g. `gpt-6.1-sol`), a pinned model hits provider capacity (`shadow_observer` on `gpt-5.6-terra` failed with "Selected model is at capacity" three times between 2026-10-07 and 2026-10-12), and nobody re-checks whether a role really needs `xhigh` on a top-tier model.

The data needed to re-check already exists. The `tasks` table records `role`, `chosen_cli`, `model`, `effort`, `return_code`, `duration_s` and `tokens_total` for every spawn (about 30 days kept). `cost_usd` is always empty because all CLIs run on subscriptions, and Claude rows have no token counts.

## Goal / success criteria

1. A new weekly loop, the **model router**, reviews every role whose CLI is set to `auto` and records one CLI + model + effort per role, with a reason.
2. A role on `auto` spawns with the router's latest valid decision. With no decision yet, `auto` keeps today's meaning (follow the active CLI).
3. The router itself runs on `claude` · `opus` (Opus 5.5) · `max` by default. When that run fails, the same pass reruns on `codex` · `gpt-6.1-sol` · `xhigh`. A provider capacity error never costs the week.
4. The router can only record options that exist in the local model catalog. It can never route itself.
5. Nothing changes for roles with an explicit CLI, or when the router is disabled.

## Non-goals

- Per-spawn LLM routing.
- Champion/challenger traffic splits. A decision switches the whole role; the next weekly pass judges it and may keep it or roll it back.
- Recommendations for pinned roles.
- A failure fallback for the other loops.
- Dollar-cost estimates.

## Design

### 1. Role and configuration

`model_router` is added to `agent_metadata.AGENT_ROLES`:

| field | value |
|---|---|
| name | Model router |
| description | Weekly reviews which CLI, model and effort each `auto` role runs on, and re-assigns them. |
| reads | Spawn telemetry, recent run outcomes, the local model catalog |
| writes | Routing decisions for roles set to `auto` |
| impact | `config_write` |
| interval_key | `THREADKEEPER_MODEL_ROUTER_INTERVAL_S` |

New settings in `config.py`:

| key | default | meaning |
|---|---|---|
| `THREADKEEPER_MODEL_ROUTER_INTERVAL_S` | `604800` | Time between completed passes. `<= 0` disables the loop, and `auto` then keeps its old meaning. |
| `THREADKEEPER_MODEL_ROUTER_FALLBACK_CLI` | `codex` | CLI for the fallback attempt |
| `THREADKEEPER_MODEL_ROUTER_FALLBACK_MODEL` | `gpt-6.1-sol` | Model for the fallback attempt |
| `THREADKEEPER_MODEL_ROUTER_FALLBACK_EFFORT` | `xhigh` | Effort for the fallback attempt |

The primary attempt uses the normal role keys (`THREADKEEPER_SPAWN__LOOP__MODEL_ROUTER`, `…__MODEL__MODEL_ROUTER`, `…__EFFORT__MODEL_ROUTER`). When those are unset, a new code-level `ROLE_DEFAULTS` entry in `spawn_config.py` supplies `claude` / `opus` / `max`. The `opus` alias is used because the installed Claude Code (2.1.278) reports `unrecognized_model` for the full `claude-opus-5-5` ID and accepts the alias, which currently points to Opus 5.5:

- CLI: `spawn.loop[role]` → `ROLE_DEFAULTS[role].cli` → `spawn.default` → active CLI → `claude`.
- Model and effort: `spawn.model[role]` / `spawn.effort[role]` → `ROLE_DEFAULTS[role]`, **only when the resolved CLI equals the default's CLI** (so `LOOP__MODEL_ROUTER=codex` never inherits a Claude model) → CLI default → CLI native default.
- `resolution_details` reports the source as `role default`.

### 2. `auto` resolution

Routing applies only when a role's raw `spawn.loop[role]` value is `auto`. `spawn.default=auto` keeps its current meaning, and the `model_router` role is never routed.

`model_router.current_route(role)` returns the latest `model_routes` row for the role, or `None`. A route counts as valid unless the cached model catalog explicitly lists that CLI without that model, or that model without that effort. A missing or unreadable catalog does not invalidate a route.

Changes in `spawn_config.py`:

- `resolve_agent(role)`: role on `auto` with a valid route → `route.cli`. Otherwise the existing chain applies unchanged.
- `resolve_model(cli, role)` / `resolve_effort(cli, role)`: role on `auto` with a valid route **and** `cli == route.cli` → `route.model` / `route.effort`. Role-level pins are ignored for a routed role.
- `resolution_details`: source `model router` for all three values of a routed role.
- `_spawn_warnings`: warn when a routed role also has model/effort pins ("ignored while the role is on auto"), and when a route was dropped as invalid.

`tools/spawn.py` does not change. Explicit `cli`, `model` and `effort` arguments to `_spawn_impl` still win, because the call site already prefers them over `resolve_*`.

`spawn_config` imports `model_router` lazily to avoid an import cycle. Each resolution does one indexed `SELECT`, with no cache.

### 3. Storage

Added to `db.ADDITIVE_RUNTIME_SCHEMA` (additive, no schema version bump). The table is local only and not synced, because catalogs differ per machine.

```sql
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
```

The latest row per role is the current route. Older rows are the history the router reads next week. A "keep" decision is still a row, so every pass leaves a complete record for each role it reviewed.

Pass state lives in a second additive table, because `daemon_state` only stores one timestamp per loop:

```sql
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

### 4. The loop (`threadkeeper/model_router.py`)

The daemon follows the existing pattern: `start_model_router_daemon` → `_serve_loop` → `run_router_pass(force=False, scheduled=True)`, with an hourly tick (`_TICK_S = 3600`). It is registered in `host.py`, in `agent_status._LOOP_DEFS` and `_DAEMON_BINDINGS` (event `model_router_pass`), and in `agent_status._ROLE_HINTS` so its children are attributed to the role. A `daemon_state.claim_pass` cadence claim plus `helpers.single_flight_lock` prevent two processes from ticking at once.

Pass state (pass id, the snapshot of `auto` roles the pass covers, attempt `primary`|`fallback`, task id, status) is stored in `model_router_passes`. Each tick resolves to exactly one action:

| condition | action |
|---|---|
| interval `<= 0`, or no role on `auto` | record `skip` |
| a router child for the current pass is still running | record `running` |
| primary child exited without a complete decision (non-zero exit, or exit 0 without writing) | spawn the fallback attempt for the same pass |
| fallback child exited without a complete decision | mark the pass failed at `now`, record `failed` |
| last pass failed less than 6 h ago | record `backoff` |
| due: no completed pass within the interval, **or** some `auto` role has no valid route | open a new pass, build the digest, spawn the primary attempt |

The child is spawned with `role="model_router"`, `write_origin="model_router"`, `permission_mode="auto"`, `slim=True`. Only `mcp__thread-keeper__model_route_write` is allowed, plus the pass id in the environment (same mechanism as `curator_report_write`). The public `spawn()` has no `cli` argument, so both attempts go through `tools.spawn._spawn_impl` (as the watchdog retry in `spawn_budget` does): the primary with the role's resolved settings, the fallback with explicit `cli`, `model` and `effort` from the fallback settings.

### 5. Digest (built by the parent, deterministic)

The digest is fenced as data with `fence_observed`, because run outcomes can echo untrusted text, and capped at 60 000 characters. For each role on `auto`:

- **Role:** description, reads, writes and impact from `AGENT_ROLES`; its current effective CLI, model and effort; the last 3 router decisions with reasons.
- **Telemetry:** for each CLI + model + effort seen for the role in the last 30 days, split into weekly buckets: runs, successes, failures, still running, p50 and p90 `duration_s`, p50 `tokens_total` (`n/a` when missing), and the top 3 normalized failure lines (for example `Selected model is at capacity`).
- **Outcomes:** the 5 most recent completed runs, each reduced to its result line with the same extraction `agent_status` uses for recent results (`_extract_useful_result`, made public for reuse) and capped at 240 characters.

Shared section: the installed CLIs, with their models and allowed efforts per model, from `model_catalog.cli_catalog()`.

The prompt instructions live in a module-level `ROUTER_PROMPT` constant in `model_router.py`, like `SHADOW_REVIEW_PROMPT`:

- Weigh three things: price (tokens, effort level, model tier), speed and reliability (run time, success rate, capacity failures), and result quality (the outcome lines).
- Keeping the current setting is a valid and expected answer. Switch only when the data gives a concrete reason, and cite it in the reason. With fewer than 5 runs on the current setting, prefer keeping it.
- Roles with impact `code_write` or `external_write` weigh quality above price.
- Call `model_route_write` exactly once with a decision for every listed role. If it returns errors, fix them and call it again.

### 6. `model_route_write(pass_id, decisions)`

A `write_tool(idempotent=True)` in `threadkeeper/tools/model_router.py`. `decisions` is a JSON list of `{role, cli, model, effort, reason}`.

- **Authorization:** the caller must be a spawned child (`SPAWNED_CHILD`) with `WRITE_ORIGIN == "model_router"`, and `pass_id` must match the parent-issued pass id. Otherwise the call returns `ERR route_write_not_authorized`.
- **Validation:** all decisions are checked before anything is written. Any failure rejects the whole call and returns every error.
  - The set of roles must equal the pass's stored `auto` role snapshot exactly (no missing, extra or duplicate roles).
  - No role may be `model_router`.
  - The CLI must be supported and present in the catalog.
  - The model must be listed for that CLI.
  - The effort must be allowed for that model, or empty for Antigravity.
  - The reason must be non-empty and at most 500 characters.
- **Write:** one transaction inserts all rows, marks the pass `complete`, emits a `model_router_pass` event with a summary such as `routed 1/4: shadow_observer codex gpt-5.6-terra xhigh → codex gpt-5.6-luna high`, and returns `ok` with the same summary.

### 7. Operator surfaces

- `model_router_run(force=True)` runs one pass now, with the same semantics as `dialectic_validate_run`.
- `model_router_status()` shows the interval, when the next pass is due, the last pass outcome, and for each `auto` role its current route with when it was decided and why, or `no decision yet — follows active CLI`.
- `agent_status` / menu bar: the loop appears like the others. Its `recent_results` entry carries the routing summary.
- **macOS settings app** (`ThreadKeeperAgentStatus.swift`):
  - The per-role CLI picker gets an `Auto` choice. The global Default CLI picker does not.
  - When a role is on `Auto`, its model and effort pickers are hidden. A read-only line shows the routed setting and its date from the settings catalog, or "No decision yet — follows active CLI".
  - The settings catalog role payload adds `routed: bool` and `routed_at: int`, built from `resolution_details` / `current_route`.

## Error handling

| failure | behaviour |
|---|---|
| Primary attempt fails (capacity, auth, crash) or exits without a complete decision | The next tick (≤ 1 h) reruns the pass on the fallback |
| Fallback fails too | The pass is marked failed and retried from the primary after 6 h; current routes stay in force |
| The child writes invalid decisions | The tool rejects them with errors; the child can retry within its session; if it exits without a valid write, the attempt counts as failed |
| A route's model leaves the catalog | Resolution drops the route (the role uses the old `auto` behaviour), the validator warns, and the next pass is due at once because the role has no valid route |
| The digest exceeds 60 000 characters | Outcome samples are trimmed first, then the oldest weekly buckets |

## Backward compatibility

- Roles already set to `LOOP__<ROLE>=auto` become routed once the router records a decision for them. Because an `auto` role without a route makes a pass due, the first pass runs soon after upgrade. This behaviour change is noted in CHANGELOG. To keep the old meaning, set `THREADKEEPER_MODEL_ROUTER_INTERVAL_S=0`.
- Pinned roles, `spawn.default=auto`, and explicit `spawn()` arguments are unchanged.

## Testing

Unit tests (pytest, temp DB, fake `spawn`, fixture catalog):

- `spawn_config`:
  - an `auto` role with and without a route;
  - a route invalidated by the catalog;
  - a missing catalog;
  - role pins ignored while routed;
  - an explicit CLI that differs from `route.cli`;
  - `model_router` never routed;
  - `ROLE_DEFAULTS` applied only on a matching CLI;
  - `resolution_details` sources;
  - the new warnings.
- `model_route_write`: authorization, each validation rule, all-or-nothing writes, idempotent replay, and the event summary.
- Loop state machine: one test per row of the tick table, including primary fail → fallback → fail → backoff → primary, an exit-0 child without writes, and an immediate pass for a new `auto` role.
- Digest builder: weekly buckets, percentiles, failure-line normalization, `n/a` tokens, fencing, and size trimming. Uses fixture `tasks` rows and logs.
- `agent_status` / settings catalog: the loop is listed, and the `routed` / `routed_at` fields are present.

The Swift app gets a `build.sh` compile check. Manual check on the live install: set one low-risk role to `auto`, run `model_router_run`, then confirm the decision row, the spawn record of the next run of that role, and the settings window.

## Documentation

Same change set:

- README: spawn routing section, `auto` meaning, new env keys, new tools.
- `docs/ARCHITECTURE.md`: loop list and the routing resolution chain.
- `spawn_config.py` module docstring: resolution order.
- `apps/macos-agent-status/README.md`.
- CHANGELOG entry, including the `auto` behaviour change.

## Open checks (resolved during planning)

- Claude Code 2.1.278 reports `unrecognized_model` for `claude-opus-5-5` and accepts `opus`, so the router default is `opus`.
- Codex children already call thread-keeper write tools from the existing Codex-routed loops; the new tool only needs to be in `extra_allowed_tools`.
- The Swift `ImpactBadge` shows unknown values verbatim; a `config_write` case is added anyway.

## Amendment — 2026-10-25: task results drive model selection

The user's revised decision rule supersedes the keep-by-default prompt in
section 5. Every pass compares available models and effort for each role on
task effectiveness, cost and efficiency. A working current model, a small
sample or a model's tier does not by itself justify retaining or choosing it.

The digest now pairs previous task instructions with saved final assistant
answers within that task's session and execution window. Structured Claude
and Codex log results, then the existing result-line extractor, are fallbacks.
Samples include failed executions and cover previous model/effort combinations.
Long excerpts retain their beginning and conclusion. Redaction and the
untrusted-data fence apply to instructions and results; deleted transcript
messages are excluded. Size reduction retains task-result evidence.

Recorded USD costs and their coverage accompany per-run time and tokens.
Unknown costs remain unknown; subscription token use is a quota proxy.
Execution success and task success are distinct. Self-reported results need
supporting tests or reviews to establish quality; missing evidence and
untested alternatives must be called out in the decision's reason. Each
reason compares the three criteria for that role rather than waiting for
the current setting to fail. The routing schedule and write authorization
contract remain unchanged.

The parent also loads official pricing automatically for available CLIs.
Fixed Markdown sources cover OpenAI API prices and Codex credits, Anthropic
prices and Claude alias resolution, GitHub Copilot prices, and Antigravity
quota/overage terms. Rate-card data is cached atomically beside the database
for 24 hours. Bounded fetches reject redirects; failed fetches or changed
document layouts retain last-good data as stale, with its original fetch time.
URLs and freshness are supplied as fenced evidence with reserved prompt space.
The digest adds input/output token medians and coverage, per-task breakdowns
and transcript-reported model IDs. The router matches exact models, billing
scope, currency, context/cache/speed conditions and known alias overrides.
It compares effort from observed task outcomes, time and consumption, without
an invented effort multiplier. Missing breakdowns, alias versions or rates
remain explicit uncertainties; API-equivalent estimates do not replace
recorded USD spend or claim to measure subscription quota exactly.

Live verification also found that generic child-profile selection assigned
`core` to the router and hid its only write tool. Router children now use a
dedicated `model-router` profile exposing `model_route_write`, intersected
with the parent's permissions. The host registers that tool before launching
the child; a parent that cannot expose it receives a clear spawn refusal.

A further live pass exposed two separate problems. The MCP permission grant
did not restrict native Claude tools, so the router expanded its bounded input
into lengthy shell research. It now has only native `ToolSearch` plus its MCP
writer and must decide from the supplied evidence. The direct Claude spawn
path also lacked the adapter's streaming flags; captured hidden launches now
reuse those flags to expose intermediate events in task logs.

The completed proposal then hit a protocol replay-key collision: client software
metadata and a numeric JSON-RPC ID were shared with an older CLI process.
Replay keys now include a fresh stdio-connection namespace and retain ID types.
Same-connection replay/mismatch checks and caller-bound continuation checks
remain; the durable router pass still governs retry ownership.

The digest also includes dated CLI/model/effort tuples from completed router
passes with successful process exits. Later successful access supersedes older
startup failures, without establishing quality on another role's tasks. This
prevents a resolved client-version issue from remaining an assumed account block.
Recent failed router attempts carry parsed terminal error text, keeping quota
limits separate from model quality. Pending passes reconcile every 30 seconds;
both the scheduler claim interval and loop sleep use that cadence, while normal
due-pass checks remain hourly. This avoids a one-hour delay before fallback.
