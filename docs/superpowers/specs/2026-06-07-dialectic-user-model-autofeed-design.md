# Dialectic user-model: auto-feed + tier-fix + agent settings

**Date:** 2026-06-07 (revised 2026-06-08)
**Status:** Design — forks approved, pending plan

## Problem

thread-keeper has a dialectic user-model (`user_dialectic` + `dialectic_evidence`
tables; tools `dialectic_claim/evidence/review/supersede/synthesis`) meant to build a
persistent "profile + behavior" portrait of the user: discrete claims per domain
(`style/workflow/values/context/skills/other`), with confidence and a tier
(`hypothesis → observed → validated`, or `disputed`) emerging from accumulated evidence.

An audit on 2026-06-07 found it populated once and then frozen. Three root causes:

1. **Tier-stuck (bug).** 8 claims / 20 evidence rows, all seeded in a single burst on
   2026-05-14. The tier state-machine landed 6 days later (commit `6b52d85`,
   2026-05-20); its migration added the `tier` column with `DEFAULT 'hypothesis'` but
   never backfilled existing claims, and `_recompute_tier` only fires when new evidence
   is added. So 3 claims with `support_count=5` / `confidence=high` — which qualify for
   `validated` (w_support ≈ 5 ≥ 4.0) — are frozen at `hypothesis` with
   `tier_changed_at=NULL`. `brief()` then frames well-supported facts about the user as
   "currently_testing — watch next user moves."

2. **No auto-feeder (root gap).** The only callers of `dialectic_claim` /
   `dialectic_evidence` are the MCP tool definitions and `panel.py` (which only emits a
   prompt *string*, not a real call). No daemon or hook mines conversations into the
   model. Every other store has an autonomous daemon (`extract_daemon`, `shadow_review`,
   `candidate_reviewer`, `curator`, `skill_watcher`, `thread_janitor`); the user-model is
   the only one without. `last_evidence_at` has been static for 24 days. The "build the
   profile in parallel to everything" intent was never wired.

3. **Model/agent selection is an override hack, not a setting.**
   `spawn_config.resolve_model(cli)` is keyed by CLI, so `[models].claude='sonnet'` pins
   *every* claude-resolved role to one model — per-role model is inexpressible.
   `resolve_agent` is an override chain layered over an active-CLI fallback. Agent+model
   choice should be a first-class keeper setting: "which agent (CLI + model) for which
   purpose (role)."

## Goals

- The user-model self-updates continuously and autonomously (no manual/user-run
  trigger), with self-correction — claims can be contradicted/superseded, not only
  accreted.
- User replies are captured losslessly; interpretation into claims is a separate,
  careful step.
- Existing well-supported claims show their true tier.
- Per-role agent+model is a first-class configurable setting.

## Non-goals

- No human approval gate in the feed loop (per user value: no manual-run features).
- No change to the tier thresholds or confidence math (they are sound; the missing
  recompute and missing feeder are the problem, not the formula).
- No rewrite of the existing dialectic MCP tools' write semantics.
- No forced migration of existing roles to the new settings form (back-compat retained).

## Workstream A — tier recompute + migration backfill

- Add an idempotent `recompute_all_tiers()` that runs `_recompute_tier` over every
  active `user_dialectic` claim.
- Invoke it from the migration path (one-shot backfill on upgrade) so existing installs
  heal automatically without manual action.
- Effect: the 3 strong claims promote (observed/validated); `brief()` stops mislabeling
  them as hypotheses.
- **Test:** seed a claim with 5 support evidences (weight 1.0), `tier='hypothesis'`,
  `tier_changed_at=NULL`; run `recompute_all_tiers()`; assert `tier=='validated'`
  (>14d quiet) and that a `tier_promoted` event was emitted.

## Workstream C — role-keyed agent+model settings

Reframe `spawn_config` from override-chain to first-class assignments.

- New primary config section, role-keyed:
  ```toml
  [agents.dialectic_validator]
  cli   = "claude"
  model = "opus"
  ```
- `resolve_agent(role)` and `resolve_model(role, cli)` consult `[agents.<role>]` FIRST.
  Per-role model becomes expressible — e.g. `dialectic_validator` runs opus while every
  other spawned role stays on sonnet. (The dialectic miner is mechanical and spawns
  nothing, so it has no agent/model entry.)
- Resolution priority (highest → lowest), all retained for back-compat:
  `[agents.<role>]` → per-role env (`THREADKEEPER_SPAWN_LOOP_<ROLE>` for cli, new
  `THREADKEEPER_SPAWN_MODEL_<ROLE>` for model) → legacy `[loops]` / `[models]` →
  `[default]` → active CLI → `claude`. Existing `[loops]`/`[models]` configs keep
  working unchanged.
- `summary_table()` / `spawn_status` show `role → cli/model (source)` from the new
  resolution.
- Existing roles (`shadow_observer`, `archivist`, `curator`, `candidate_reviewer`,
  `extract`, `evolve_reviewer`, `probe_runner`) continue resolving to claude/sonnet via
  the legacy keys until/unless migrated to `[agents.*]`. No behavior change is forced.

## Workstream B — two-daemon auto-feeder

Two daemons with distinct jobs: a **mechanical capture** stage (no LLM) and an **LLM
interpretation** stage. The capture stage runs often and cheap; interpretation runs
infrequently on the careful model. (User decision: capture must be lossless; the miner
records raw user signal and cannot mis-propose; all judgment lives in one validator.)

### Stage 1 — `dialectic_miner` daemon (mechanical capture, no LLM)

- File `threadkeeper/dialectic_miner.py`: `_serve_loop` → `run_mine_pass(force)` →
  `start_dialectic_miner_daemon()`. Mirrors `extract_daemon` (local DB work, no spawn).
- Interval `DIALECTIC_MINE_INTERVAL_S` (default 3600 = 1h; 0 = off).
- Pulls user-role `dialog_messages` since the last `dialectic_mine_pass` high-water
  cursor. Applies the SAME session filtering as `extract_recent` so only the REAL user's
  turns are captured, never agent-to-agent: exclude sessions whose first user message
  matches `_INTERNAL_PROMPT_PREFIXES`, and exclude sessions whose id appears in
  `tasks.spawned_cid`.
- For each captured user reply, stores a row in the `dialectic_observations` buffer: the
  verbatim `user_quote`, a `context` slice = the most recent preceding assistant message
  in the same session (truncated to ~600 chars), `dialog_uuid`, `source_cid`,
  `created_at`, `status='pending'`. Dedup by `dialog_uuid` (UNIQUE).
- No model, no spawn, no embeddings — deterministic and lossless. Guard:
  `BACKGROUND_DAEMONS_ALLOWED` only (no `SEMANTIC_AVAILABLE` needed).
- Telemetry: `events.kind='dialectic_mine_pass'` with captured/skipped counts.

### Stage 2 — `dialectic_validator` daemon (LLM interpretation)

- File `threadkeeper/dialectic_validator.py`: same skeleton as `candidate_reviewer`
  (spawns an LLM child).
- Interval `DIALECTIC_VALIDATE_INTERVAL_S` (default 21600 = 6h; 0 = off).
- Cost guard: below `DIALECTIC_VALIDATE_MIN` pending observations → record
  `below_threshold` and skip the spawn.
- Spawns one slim child, role `dialectic_validator` (→ claude/opus via C). The prompt
  carries (a) the pending-observations inventory (user_quote + context per row) and (b)
  the full current model (`dialectic_review` output: every active claim with
  domain/tier/confidence). The child is the SOLE interpreter — it turns raw user replies
  into the dialectic via the existing tools:
  - `dialectic_claim` — genuinely new territory.
  - `dialectic_evidence(kind=support|contradict)` — corroborate or challenge an existing
    claim; weight set by source kind (see below).
  - `dialectic_supersede` — replace a claim with a refined one.
  Then it calls the new `dialectic_observation_resolve(id, note)` to mark each consumed
  observation `processed` (mirrors `reject_candidate`), so it is never re-interpreted.
- Decision rules (in the prompt): PREFER evidence-on-existing over new claims (dedup);
  MERGE near-duplicate observations into one claim; `contradict`/`supersede` only on a
  clear conflict with a stored claim; LIMIT `DIALECTIC_MAX_NEW_CLAIMS` new claims per
  pass; resolve noise/chit-chat observations as `processed` without writing anything.
- Telemetry: `events.kind='dialectic_validate_pass'`.

### Buffer — `dialectic_observations` table

Columns: `id` (PK), `dialog_uuid` (UNIQUE — dedup), `user_quote`, `context`,
`source_cid`, `status` (`pending|processed`), `created_at`, `processed_at` (nullable).
The validator surfaces only `pending` rows within the last 30 days; older pending rows
are stale and auto-skipped (mirror `candidate_reviewer`'s cutoff).

### New tool

`dialectic_observation_resolve(id: int, note: str = "")` — set an observation
`processed`; append `note` for auditing. Used by the validator child. (There is NO
`dialectic_propose`: the miner writes the buffer directly from Python; proposing claims
is the validator's job alone.)

### Evidence weighting (anti-overfit)

- The captured `user_quote` is the user's own words. When the validator writes evidence
  that quotes the user directly (an explicit statement of preference/decision), it uses
  full base weight. When it INFERS a trait from behavior/context rather than an explicit
  statement, it passes ~0.5. The validator runs as a review-fork origin, so its writes
  are additionally origin-discounted by the existing machinery.
- Corroboration across DIFFERENT sessions/cids is what moves a claim up the ladder; one
  chatty session cannot. Promotion stays deliberately slow.

### Config knobs (new, in `config.py` with env binding; 0 disables each daemon)

`DIALECTIC_MINE_INTERVAL_S` (3600 = 1h), `DIALECTIC_VALIDATE_INTERVAL_S` (21600 = 6h),
`DIALECTIC_VALIDATE_MIN` (5 pending observations), `DIALECTIC_MAX_NEW_CLAIMS` (3 per
pass). The miner has no cost-guard threshold — mechanical capture is cheap and lossless,
so it always drains the new-message backlog. All defaults are starting points, tunable
per install.

### Wiring

- Start both daemons where the others start (server startup). Miner guarded by
  `BACKGROUND_DAEMONS_ALLOWED`; validator guarded by `BACKGROUND_DAEMONS_ALLOWED` +
  `SEMANTIC_AVAILABLE` + the standard slim/spawned-child cascade prevention.
- Manual triggers for ops/testing only: `dialectic_mine_run` / `dialectic_validate_run`
  (force=True) + `dialectic_mine_status` / `dialectic_validate_status`, mirroring the
  extract / candidate_review tool pairs.

## Testing

- **A:** recompute promotes a seeded `support=5` claim hypothesis→validated; emits
  `tier_promoted`.
- **C:** `[agents.<role>]` resolves cli+model; per-role model works (validator opus while
  default stays sonnet); legacy `[loops]`/`[models]` still honored; env beats file;
  active-CLI fallback intact.
- **B miner (mechanical):** seed `dialog_messages` for a real-user session + an
  internal-prompt session + a spawned-child session; `run_mine_pass(force=True)`; assert
  only the real user's replies land in `dialectic_observations`, each carrying its
  preceding-assistant `context`; re-run → no duplicates (dedup by `dialog_uuid`);
  high-water cursor advances; no spawn occurs.
- **B validator:** seed pending observations; below `DIALECTIC_VALIDATE_MIN` → skip +
  `below_threshold` event; at/above → spawns (assert `spawn` called with role
  `dialectic_validator`); cascade-prevention (slim child does not start the daemon).
- **B resolve tool:** `dialectic_observation_resolve` sets `status='processed'` +
  `processed_at`, so a second validator pass does not re-feed it; unknown id → ERR.

## Docs (per update-docs-after-functional-changes)

Update in the same change-set: README (new daemons + `[agents.*]` settings + knobs),
CHANGELOG, the `spawn.toml` header comment, and any architecture doc that enumerates the
learning loops.

## Open / deferred

- Possibly collapse the two daemons later if 1h/6h proves wasteful — kept split per user
  decision (capture cheap+frequent, interpret careful+infrequent).
- A periodic `dialectic_synthesis` pass to merge fragmented claims — out of scope here
  (curator-adjacent); revisit later.
