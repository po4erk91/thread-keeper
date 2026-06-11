# Architecture (current state, May 2026)

thread-keeper is a local MCP server that holds working memory across Claude
conversations. The target client is **Claude Code** (CLI, VS Code extension);
Desktop also works through the same MCP protocol, but the primary environment
is Code, because only there are the jsonl transcripts and hooks.

One process per session, shared SQLite in WAL mode — multiple windows can
read-write the same database simultaneously. One state file:
`~/.threadkeeper/db.sqlite`.

## Package map

```
threadkeeper/
├── _mcp.py            FastMCP singleton (shared @mcp.tool registrar)
├── server.py          entry point: import all tools/ → mcp.run() (stdio)
├── config.py          pydantic-settings Settings ← ~/.threadkeeper/.env (DB_PATH, …)
├── db.py              SCHEMA + migrations + WAL-knobs + sqlite-vec loader
├── identity.py        per-process session + self-cid + daemon launchers
├── ingest.py          live ingest of jsonl transcripts + skill_usage backfill
├── embeddings.py      pluggable backend (ONNX/fastembed default; ST fallback), cosine search
├── migrate_embeddings.py  CLI: recompute stored vectors after a backend switch
├── helpers.py         ID generators, fmt_age, q-quoting, alive-pid check
├── agent_status.py    structured loop/agent/recent-result status for UI clients
├── brief.py           render_brief() / render_context() — main digest
├── nudges.py          counter-driven memory_nudge / skill_hint / auto-review
├── review_prompts.py  MEMORY/SKILL/COMBINED/ANTI_CAPTURE for review-forks
├── process_health.py  orphan-detection (ppid + heartbeat)
├── menubar_app.py     macOS MenuBarExtra app autoinstall/autolaunch
├── memory_guard.py    daemon: notify + SIGTERM when server RSS exceeds limits
├── skill_watcher.py   daemon: external edits to SKILL.md → patch_count++
├── search_proxy.py    daemon: serves search_via_parent from slim children
├── spawn_budget.py    daemon: measures subtree RSS, admission control
├── shadow_review.py   daemon: periodically decides "is it worth materializing a skill"
└── tools/             @mcp.tool() — each file = group
    ├── threads.py     open/note/close/idle, brief, context, search, …
    ├── peers.py       broadcast/whisper/ask/respond/wait/inbox/live_status
    ├── spawn.py       spawn/tournament/tasks/task_logs/spawn_status/budget
    ├── agent_status.py autonomous loop status JSON/text for menu-bar app
    ├── skills.py      skill_manage/skill_record/skill_list/curator_run/review_thread
    ├── dialectic.py   claim/evidence/review/synthesis/supersede (tier + discount)
    ├── core_memory.py set/get/list/remove (Letta-tier RAM)
    ├── shadow_review.py shadow_review_run/status
    ├── process_health.py mp_health/mp_cleanup
    ├── memory_guard.py memory_guard_status/check
    ├── probes.py      register/run/record/reliability_for/weak_spots
    ├── distill.py     distill/vote/pending/export
    ├── extract.py     extract_recent/review/accept/reject candidates
    ├── candidate_reviewer.py candidate_review_run/status
    ├── curator.py     curator_review/status
    ├── lessons.py     lesson_append/list/get
    ├── concepts.py    register/list/expand
    ├── graph.py       link/unlink/neighbors
    ├── correlation.py tag_signal/task_thread
    ├── pickup.py      pickup_candidates/claim/release
    ├── dialog.py      dialog_search/open_dialog_window/ingest
    ├── validate.py    validate_threads
    ├── style.py       style_set/verbatim_user
    ├── invariants.py, missed_spawns.py, consolidate.py, session.py, …
```

Launch: `python -m threadkeeper.server`. Stdio-MCP, no ports. On macOS, the
entry point also best-effort installs and launches the loop-status menu-bar
app before `mcp.run()`; all subprocess output is captured so stdout remains
reserved for MCP frames. The menu-bar app polls `tk-agent-status --json`,
receives loops sorted by active state (`running` → `ready` → `idle` → `off`),
shows Probe backlog as due objective probes only, and posts macOS notifications
for newly observed useful `recent_results`.
The legacy monolith `server.py` at the repo root was removed in May 2026 — the
runtime is fully on the package.

## Storage layers

The database is `~/.threadkeeper/db.sqlite`. Logically six levels:

1. **threads + notes** — the main state machine of working memory.
   Thread = an open question; note = a move in it (`move`/`failed`/`insight`/`open_q`).
   `close_thread` records the outcome; `idle_thread` freezes it, and the next
   note automatically reactivates it.

2. **core_memory** — Letta-style RAM tier. High-priority lines that ALWAYS
   appear in the brief regardless of relevance. Flat key/priority/content;
   tier-policy (what to evict, how to promote/demote) is not yet implemented.

3. **dialog_messages + dialog_fts (+ dialog_vec)** — full conversation
   transcripts, pulled live from `~/.claude/projects/**/*.jsonl`.
   Used by `peers()`, `brief()`, `search()`, `dialog_search()` and the
   shadow-review daemon.

4. **events + cursors + presence + signals** — live channel: every mutating
   action writes an event, each session has a cursor, and `live_status()`
   counts `live=N` by cursor delta. Signals — broadcast/whisper/
   search_request/search_response between parallel windows.

5. **skill_usage** — telemetry for mirrored Skill.md entries. Fields:
   `last_used_at`, `last_viewed_at`, `last_patched_at`, counters, `state`
   (active/stale/archived), `pinned`, `created_by_origin` (foreground vs
   background_review vs shadow_review). This is the input for the curator.

6. **dialectic_claims + dialectic_evidence** — Honcho-style discrete user
   model. Claim with a domain, evidence support/contradict/clarifying, sm-ratio
   confidence; brief() renders medium+high grouped by domain.
   `dialectic_observations` is the capture buffer: `pending` rows are unclaimed
   backlog, `claimed_at`/`claimed_by_task` means a validator child owns the
   batch, and `processed` is terminal. Stale claims are requeued.

In addition: `probe_results`/`reliability`, `concepts`, `edges`,
`extract_candidates`, `distillates`/`votes`, `tasks` (spawned children),
`shadow_review_pass` (as event.kind).

## Identity and self-cid

The conversation identifier is `conversation_id` (stem from jsonl). Resolvers:

1. `THREADKEEPER_FORCE_CID` env — used by spawn() for children; sets the cid
   directly, without guessing.
2. **ppid walk** — recursively `ps -p $pid -o ppid,command`, looking for
   `claude … --resume/--session-id <uuid>` in one of 12 ancestors. Stable,
   doesn't flap; cached forever per-process.
3. Fallback: latest-mtime jsonl. Flaps when several windows are active in
   parallel.

`_session_id` is a different thing: per-process `s_{pid}_{hex}`, never reliable
as window-identity (a single MCP process can multiplex several Desktop
windows). The regression of the snapshot bug (`from identity import _session_id`
created a local None snapshot in 7 files) was closed in May 2026 — all callers
read via `identity._session_id` attr-access, pinned by the test
`test_brief_ctx_line_carries_live_session_id`.

## Daemons inside the parent process

`identity._ensure_session()` brings up background threads on first call.
All daemon threads are cheap (ticks 0.5–30 s), no-op when env-knobs disable them:

- **background_ingester** (`ingest._start_background_ingester`) — ticks every
  `INGEST_INTERVAL_S` (default 3 s), reads fresh jsonl chunks, tops up
  dialog_messages/_fts and backfills NULL-embeddings on notes.
- **search_proxy** — serves `search_via_parent` from slim children via
  signals (see below).
- **spawn_budget** — once per `SPAWN_BUDGET_POLL_S` (default 10 s) walks
  the subtree of each `running` task via `ps`, updates `tasks.rss_kb` and
  closes dead ones.
- **memory_guard** — once per `MEMORY_GUARD_POLL_S` (default 30 s) scans
  all `threadkeeper.server` processes; warns above `MEMORY_GUARD_WARN_MB`
  and sends SIGTERM above `MEMORY_GUARD_KILL_MB` after logging/notifying.
  It also watches aggregate server RSS. Aggregate side effects are owned by a
  single live coordinator server so multiple open clients do not duplicate
  warn/reclaim/retire actions: above `MEMORY_GUARD_AGG_WARN_MB` it asks peer
  servers to unload embedding models/caches; under pressure it retires stale
  non-self servers whose parent is gone toward `MEMORY_GUARD_TARGET_SERVERS`.
  Parent-alive retirement is opt-in via `MEMORY_GUARD_RETIRE_LIVE`.
- **skill_watcher** — once per `SKILL_WATCH_INTERVAL_S` (default 5 s) walks
  the primary `~/.claude/skills/*/SKILL.md` root and bumps `last_patched_at`
  if the file was changed outside `skill_manage`.
- **shadow_review** — once per `SHADOW_REVIEW_INTERVAL_S` (default 0 = off),
  scans a dialog window and, if needed, spawns a slim-child evaluator.
- **evolve_applier** (`evolve_applier.start_evolve_applier_daemon`) — once per
  `EVOLVE_APPLY_INTERVAL_S` (default 0 = off) first looks for the latest
  complete Curator `REPORT-*.md` that has not been marked applied, then falls
  back to the oldest promoted + unapplied `evolve_format` suggestion. Curator
  report apply uses the same `evolve_applier` child but only memory MCP tools
  (`lesson_append`, `lesson_remove`, `skill_manage`) and records
  `curator_report_applied`; no code edit or PR. Code-evolve apply still edits
  `render_brief`, adds a golden brief test, runs the full suite, and opens a
  **pull request** (never commits to main). The generated branch PR title uses
  an allowed Conventional Commit type (`feat:`/`fix:` etc.) rather than the
  internal `evolve:` label. PR-gated: a human reviews + merges; on a successful
  PR the child calls `evolve_mark_applied(evolve_id, pr_url)` → `applied=1`.
  Machine-wide single-flight via a short
  `evolve-applier.lock` dispatch file lock plus the `"You are an EVOLVE
  APPLIER"` prompt prefix. Manual tools `evolve_apply(evolve_id)` and
  `evolve_apply_curator_report(report_path="")` fire the same paths regardless
  of the interval. Distinct from `evolve_reviewer`, which only triages
  (promote/dismiss) and never edits code.

Autonomous learning daemons only run in foreground parent processes. Spawned
children carry `THREADKEEPER_SPAWNED_CHILD=1`, and review forks also carry a
non-foreground `THREADKEEPER_WRITE_ORIGIN`; either condition prevents
shadow/extract/curator/candidate-reviewer daemons from starting recursively.

The daemons share the `get_db()` connection pool; sqlite WAL allows one writer
+ many readers without blocking.

## Spawn architecture

`spawn(prompt, slim=True, role=…, visible=False, …)` brings up a child agent
session via the configured CLI adapter (`claude -p`, `codex exec`, etc.).
**Architectural principle: children are hands, not heads.** The parent (the
only thread-keeper with full state and embeddings) plans and makes decisions;
spawned children are light-executors. Trigger: **N≥2 modular independent units,
≥5 min each**.

For Codex children, normal `permission_mode="auto"` spawns use
`codex exec --sandbox workspace-write`. PR-gated code-evolve spawns use
`permission_mode="bypassPermissions"`, which maps to Codex's
`--dangerously-bypass-approvals-and-sandbox` so the child can write `.git` refs
for branch/commit/PR creation. All spawned children receive the parent's
`THREADKEEPER_DB`, task log dir, project dir, forced cid, and write-origin env
so their direct Python/MCP calls hit the same store as the parent.

### Slim vs full child

`slim=True` (default):
- a temporary `slim-mcp-<task_id>.json` contains only the `thread-keeper`
  section, passed as `--mcp-config <file> --strict-mcp-config`;
- the child does NOT load other MCPs (`context7`, `figma`, …);
- `THREADKEEPER_NO_EMBEDDINGS=1` → child doesn't load PyTorch/transformers;
- `THREADKEEPER_SPAWNED_CHILD=1` is injected into both the child process env
  and the generated slim MCP config, so child MCP servers do not run
  autonomous daemons;
- on-disk size ~400–500 MB RSS instead of ~1.3 GB for full;
- semantic search is delegated to the parent via `search_via_parent`.

`slim=False` is set explicitly when the child genuinely needs another MCP
(e.g. `context7` for library documentation).

### Search proxy (search_via_parent)

```
child:  search_via_parent("similar past lessons")
         → INSERT signals(kind='search_request', to_cid=parent_cid, content=JSON)
parent: search_proxy daemon catches the signal, executes cosine/RRF search,
         writes the response: INSERT signals(kind='search_response', to_cid=child_cid, …)
child:  reads the response signal, formats the lines
```

The daemon lives in every thread-keeper process, but processes requests
**only if `SEMANTIC_AVAILABLE=True`**. For light children it is a no-op.
The parent's cid is resolved via `tasks.parent_cid WHERE spawned_cid=self_cid`;
if no parent is found — the request goes broadcast to any peer with embeddings.

### Spawn budget (RSS cap)

`spawn_budget.py` enforces a cap on the **combined RSS of all running spawned
children** (the parent itself is not counted). Default 3 GB.

- `spawn()` admission control: `check_budget()` sums `rss_kb` of all running
  tasks (NULL = conservative full-estimate placeholder), refuses if the new
  child would push past the cap. ERR carries the exact numbers + how-to-override.
- After admission, INSERT into `tasks` writes an initial estimate
  (`SPAWN_ESTIMATE_SLIM_MB` / `SPAWN_ESTIMATE_FULL_MB`).
- Daemon ticks update real RSS via `ps`; dead root pids → `ended_at`.

Tools: `spawn_budget_status()` (cap/used/free/per-task), `spawn_budget_set(MB)`
(runtime override, not persisted). Visible spawns (Terminal.app, pid=0) aren't
tracked — their RSS column stays at the estimate.

## Learning loop

The cycle of materializing skills from closed threads. Two paths to the same point:

### 1. close_thread → auto-review (foreground-triggered)

When a rich thread is closed (≥5 notes, ≥2 insight/move) `close_thread` itself
spawns a review-fork via `review_thread(mode='auto')`:

```
close_thread → nudges.auto_review_should_fire()? → spawn(slim, role=reviewer,
   write_origin='background_review',
   prompt=SKILL_REVIEW_PROMPT + dump of all notes) → child writes the skill via
   skill_manage(action=create|patch|...) → child calls
   mark_skill_materialized(thread_id) → skill_hint in the brief goes away
```

`AUTO_REVIEW_ENABLED=1` — env flag (default off). There's also
`auto_review_trigger(force=True)` — manual hot-button for when the agent wants
to materialize without an explicit thread_id (combined mode: walks all pending
rich threads).

### 2. Shadow-review daemon (cross-session)

Foreground Claude is an unreliable narrator: sometimes it doesn't close threads,
or doesn't open them at all. Shadow-review closes that gap.

```
every SHADOW_REVIEW_INTERVAL_S (default 0=off, typical prod 900s):
1. _last_shadow_ts(): high-water mark from events.kind='shadow_review_pass'.target
2. _collect_window(): pull dialog_messages WHERE created_at > max(cursor, now-WINDOW_S)
   — ALL sessions, not just our own.
3. if n_chars < MIN_CHARS (default 500): write a 'too_short'/'no_window' event, exit.
4. if a shadow observer task is already running, return `shadow_child_running`
   without advancing the cursor; retry the same window next tick.
5. spawn a slim child with SHADOW_REVIEW_PROMPT + window dump; write_origin='shadow_review',
   allowed_tools = lesson_append + lesson_list + lesson_get + skill_manage
   + skill_list + mark_skill_materialized.
6. The child IS the LLM evaluator. Decides class-vs-incident, on materialization
   first checks existing lessons/skills, then prefers patching or creating a
   broad skill. `lesson_append(source='shadow')` is the compact fallback.
7. Child-side MCP startup sees `THREADKEEPER_SPAWNED_CHILD=1` /
   `write_origin='shadow_review'` and refuses to start its own shadow daemon.
8. Write events.kind='shadow_review_pass' with new high_water_ts.
```

Dedupe — via a cursor in `events.target` (timestamp of the last evaluated
message). Idempotent: a repeated tick will not re-evaluate what it has already
seen. SHADOW_REVIEW_PROMPT — inline rubric class-vs-incident, defense against
false positives (false negatives are "cheaper"). Shadow-origin lessons have
a hard body cap and a cheap slug-similarity duplicate gate; near-duplicate
or oversized writes are rejected so the child patches existing memory instead
of growing the flat lessons list.

Manual hook: `shadow_review_run(force=True)`, observability:
`shadow_review_status()`.

## Skills system

`~/.claude/skills/<name>/SKILL.md` is the primary write target. The same
skill directory is mirrored to Codex/shared/canonical roots. Optional
subfolders: `references/`, `templates/`, `scripts/`, `assets/`.

- **skill_manage(action, …)** — a single atomic tool. Actions:
  `create | edit | patch | write_file | remove_file | delete`.
  Frontmatter validator: strict YAML, `name` regex + ≤64 chars,
  `description` ≤1024 chars, total ≤100k chars. Generated frontmatter writes
  `name` and `description` as quoted YAML scalars so colon-containing
  descriptions load in Codex and other strict parsers. `write_file/remove_file`
  are restricted to subfolders
  `references|templates|scripts|assets` with path-traversal blocking.
  `patch` revalidates the result before writing. Every successful write
  mirrors the whole skill directory into all configured roots:
  `~/.claude/skills/`, `~/.codex/skills/`, existing `~/.agents/skills/`,
  `THREADKEEPER_EXTRA_SKILLS_DIRS`, and `~/.threadkeeper/skills/`.

- **skill_record(name, kind, outcome)** — manual bump of
  `use_count/view_count/patch_count`. Under `WRITE_ORIGIN=foreground`,
  `kind='use'` also bumps `foreground_use_count` and recomputes the
  skill's tier (may promote `hypothesis → observed → validated`).
  `outcome='wrong'` bumps `wrong_count` and may demote a tier.

- **skill_usage telemetry (passive)** — `ingest.py` parses `tool_use` blocks
  from jsonl: sees `name=Skill` → `use_count++`, `last_used_at=ts`. This way
  the curator gets real numbers without the agent being required to call
  `skill_record` manually. The `skill_watcher` daemon catches external edits
  to `SKILL.md` (Edit/Write directly, not through skill_manage).

- **skill_manage write_origin** — `THREADKEEPER_WRITE_ORIGIN`
  (`foreground` default | `background_review` | `shadow_review`) is written to
  `sessions.write_origin` and proxied into `skill_usage.created_by_origin`.

- **curator_run(stale_after_days, archive_after_days, dry_run=True)** —
  background cleanup of stale agent-created skills. Never touches
  `foreground`, `pinned=1`, or **`tier='validated'`** (proven externally).
  Hypothesis-tier ages at half the configured window (unproven skills
  don't linger); observed-tier uses the default window. On apply,
  physically archives into `.archive/<name>`.

- **Skill tier** (`hypothesis`/`observed`/`validated`) — discrete trust
  signal driven by `foreground_use_count` and `wrong_count`. Mirrors
  the dialectic tier state machine for the skill library:
  `hypothesis → observed` at `foreground_use_count ≥ 2`,
  `observed → validated` at `foreground_use_count ≥ 5` with no `'wrong'`
  outcome in 14 days. Demotion: validated → observed on any `'wrong'`,
  observed → hypothesis at `wrong_count ≥ 2`. Transitions emit
  `skill_tier_promoted` / `skill_tier_demoted` events.

- **mark_skill_materialized(thread_id, skill_path)** — writes a `move`-note
  + event, kills the `skill_hint` for the thread. When `skill_path` points
  at a `SKILL.md` or skill directory created outside `skill_manage`, it first
  imports that external skill into the canonical root and mirrors it to every
  configured skills root.

- **review_prompts.py** — MEMORY/SKILL/COMBINED/SHADOW + a shared ANTI_CAPTURE
  section (do-NOT-capture: env failures, negative tool claims, transient
  errors, one-off narratives). Defense against hardening noise into rules.

Compat: frontmatter shape + folder layout match agentskills.io.

## Dialectic user model

`tools/dialectic.py` — Honcho-inspired discrete claims. Each claim is a separate
proposition with a domain (`style`/`workflow`/`values`/`context`/`skills`/`other`);
evidence accumulates, confidence via **weighted** smoothed ratio:
`(Σw_support − Σw_contradict) / (Σw_support + Σw_contradict + 3)`.

- Smoothing 3 prevents jumping into `high` after a single supporting note:
  3 foreground supports → medium (3/6=0.5), 5 → high (5/8=0.625).
- A heavy contradict knocks back to `disputed`.
- `dialectic_supersede(old, new, reason)` — versioning of claims.
- `dialectic_synthesis(domain)` — text-render `support` vs `contradict`.
- `brief()` renders the `user_model (dialectic)` section gated by **tier**,
  groups by domain. `★` — validated, `·` — observed. Hypothesis-tier
  claims with ≥1 support surface separately under `currently_testing`.

### Source-based evidence discount

Each row in `dialectic_evidence` stores
`weight = base_weight × discount(WRITE_ORIGIN)` where the discount table
is:

| WRITE_ORIGIN          | discount |
|-----------------------|----------|
| `foreground`          | 1.0      |
| `shadow_review`       | 0.5      |
| `background_review`   | 0.5      |
| `candidate_review`    | 0.5      |
| `curator`             | 0.5      |
| (anything else)       | 1.0      |

Defends against the self-confirmation loop where a claim surfaced by
`brief()` gets "re-observed" by a shadow-review fork reading the same
dialog window. Internal observations still count, but earn half as much
confidence per row — twice as many internal supports are needed to
promote a claim into a load-bearing state.

The `support_count` / `contradict_count` columns on `user_dialectic`
remain as observability counters (incremented by 1 per row regardless of
weight); confidence and tier are driven by the weighted sums over the
`dialectic_evidence` table.

### Tier state machine

Independent of the continuous confidence band, each claim carries a
discrete `tier ∈ {hypothesis, observed, validated, disputed}` that is
the **action-gating** signal. Promotion/demotion is a discrete event
(`events.kind ∈ {tier_promoted, tier_demoted}` with summary
`old→new ws=… wc=…`) so the audit trail is queryable, unlike continuous
confidence drift.

```
hypothesis ──(w_support ≥ 2.0)──────────────────────► observed
observed   ──(w_support ≥ 4.0 AND quiet 14d)────────► validated
validated  ──(any recent contradict)─────────────► observed (demote)
observed   ──(w_contradict > w_support)──────────► hypothesis (drift back)
any        ──(w_contradict > w_support AND w_c ≥ 1)► disputed
disputed   ──(w_support > w_contradict)──────────► hypothesis (recovery)
```

`tier_changed_at` records the timestamp of the last transition so the
Curator and audit queries can reason about how recently a claim earned
or lost a tier.

## Hooks (multi-CLI)

`~/.threadkeeper/hooks/` — six shell wrappers, wired into every
hook-capable CLI by `thread-keeper-setup` (see [Cross-CLI
deployment](#cross-cli-deployment) below). The canonical wiring lives in
`~/.claude/settings.json`:

- **SessionStart → tk-brief.sh** — at the start of every session injects a
  **lean** `brief()` into the system prompt. Lean mode
  (`THREADKEEPER_BRIEF_LEAN=1`, set by the hook) drops the nudge/meta sections
  from this once-per-session injection — each stays reachable on demand via its
  own tool — while keeping every data section. `context()` is no longer
  injected separately: its sess/sem/db/thread-count line already appears in
  brief's `ctx` header. Additionally prints status
  `thread-keeper: ok threads_open=N closed_recent=M live_peers=K`.
  This removes the need to call `brief()` manually every time — the new Claude
  sees it right away. Mid-session, call `brief(query=..., scope="query")` to
  refresh only the live working set without re-injecting the static memory.

- **PostToolUse → tk-status.sh** (matcher `mcp__thread-keeper__.*`) — short
  human-readable markers for mutating calls:
  `🧵 opened: <thread>`, `✅ closed: <thread>`, `📝 +insight`,
  `🎯 skill materialized`, etc. Read-only tools (`search`, `brief`, `peers`)
  are deliberately silent, not to add noise. Also writes a per-session
  `state/sess-<id>.opened` marker on `open_thread` for the two nudge hooks.

- **UserPromptSubmit → inbox-check.sh** — before every user turn checks the
  inbox for fresh signals (broadcast/whisper/ask) from other windows and
  inlines them.

- **UserPromptSubmit → tk-thread-nudge.sh** — open-thread safety net.
  Backstops the prose rule "new substantive topic → `open_thread()`", which
  nothing watched before. Once per session, if no thread was opened yet,
  injects a reminder as `additionalContext` (non-blocking). Goes silent for
  the session once `open_thread` fires (the `.opened` marker) or after one
  nudge.

- **Stop → tk-session-end.sh** — `close_thread` / `session_end` safety net.
  Throttled to once per session and only when a thread was opened this
  session (`.opened` marker present); advisory `systemMessage`, never blocks.
  Note: Claude Code's `Stop` fires at the end of every turn (there is no
  model-actionable session-end event), hence the once-per-session throttle.

- **PreToolUse → tk-task-gate.sh** (matcher `^(Task|Agent|Workflow)$`) —
  steers the spawn-vs-native choice (see `core_memory.spawn_pattern`) with
  two OPPOSITE heuristics, since the right default flipped with opus 4.8.
  `Task` (legacy, non-opus-4.8 models): blocks fan-out lacking a synthesis
  cue → push to `spawn()` (modes via `TK_TASK_GATE`: `deny`/`warn`/`off`).
  `Agent`/`Workflow` (opus-4.8 native): native is the right default for
  in-turn fan-out, so advisory `warn` ONLY on persistence signals
  (cross-session, inter-agent channels, outlive-session, daemon) — never
  hard-blocks. Claude-Code-specific; other CLIs ignore the unknown event.

### Cross-CLI deployment

`thread-keeper-setup` installs the same event specs into every detected
adapter that reports `hooks_supported()`. The wiring shape is identical
(Claude-Code-style `hooks` object), only the target file differs:

| CLI            | hooks file                  | open-thread nudge path |
|----------------|-----------------------------|------------------------|
| Claude Code    | `~/.claude/settings.json`   | `tk-thread-nudge.sh` (UserPromptSubmit) |
| Gemini         | `~/.gemini/settings.json`   | `tk-thread-nudge.sh` (UserPromptSubmit) |
| Copilot        | `~/.copilot/hooks.json`     | `tk-thread-nudge.sh` (UserPromptSubmit) |
| Claude Desktop | — (no hook mechanism)       | in-`brief()` fallback  |
| Codex          | — (no hook mechanism)       | in-`brief()` fallback  |
| VS Code        | — (no hook mechanism)       | in-`brief()` fallback  |

Events that a given CLI doesn't fire (e.g. `PreToolUse`/`Stop` on a CLI
that lacks them) are simply never triggered — installing the spec is
harmless.

**Hook-less fallback.** Clients with no hook mechanism never run
`tk-thread-nudge.sh`, so the open-thread reminder is surfaced *inside*
`brief()` instead, by `nudges.compute_thread_nudge`. To avoid double-firing
on hook-capable CLIs, `tk-brief.sh` (the SessionStart hook) exports
`THREADKEEPER_BRIEF_NO_THREAD_NUDGE=1`, which makes `render_brief` skip the
in-brief copy; hook-less clients call `brief()` directly with no such env,
so the nudge appears there. Either way it fires at most once per session
(a `thread_hint_shown` event suppresses repeats). That bookkeeping event —
and the shadow-review daemon's `shadow_review_pass` cursor mark — are
excluded from the memory/skill nudge counters (`nudges._NONCOUNTING_KINDS`)
so they don't make those counters fire a turn early.

## Process health

`process_health.py` + `tools/process_health.py`:

Orphan-MCP-server detection = ALL of:
1. Process command contains `threadkeeper.server` (is this our process).
2. Parent gone (ppid == 1/launchd OR ppid does not exist).
3. No signs of life: heartbeat in `presence` older than `STALE_HEARTBEAT_S`,
   OR the corresponding session was not found.

Tools:
- `mp_health()` — list of orphan candidates with pid/rss/etime/heartbeat-age.
- `mp_cleanup(dry_run=True, force=False)` — kill orphans. Default is dry-run,
  so we don't accidentally kill an active mcp on a false-positive classification.
- `memory_guard_status()` — show RSS guard thresholds and current server rows.
- `memory_guard_check(dry_run=True, notify=False)` — one-shot guard pass;
  pass `dry_run=False` to SIGTERM processes over the hard memory limit.
- `memory_guard_reclaim(scope='self')` — immediately unload local
  embedding/caches; with `scope='all'` also queues peer trim requests.

The daemon-leak in tests (where `tests/` spawned orphan threads via fixture's
`mcp.run()`) is closed; daemon tests disable background loops explicitly.

## sqlite-vec (HNSW) and Python fallback

`db.py` tries to load the sqlite-vec extension on the first get_db():

- **Available** (`_VEC_AVAILABLE=True`): virtual tables `notes_vec`, `dialog_vec`
  on vec0 are created. KNN ~10× faster than Python-side cosine.
  Backfill via `_backfill_vec_tables` pulls in existing embedding BLOBs.

- **Not available**: fallback to legacy Python-side cosine — `_cosine_search`
  reads the entire `notes.embedding BLOB` into memory, computes the dot product.
  Works, but doesn't scale past ~50k notes.

Optional — not needed for basic functionality. Embeddings themselves are stored
as BLOB in `notes.embedding` regardless of vec0 availability.

### Embedding backend

`embeddings.py` is backend-pluggable via `THREADKEEPER_EMBED_BACKEND`. The
default `onnx` runs the model through **fastembed / ONNX Runtime** (no PyTorch,
~700 MB footprint / ~850 MB RSS); `sentence-transformers` is a heavier opt-in
fallback (~1.8 GB). `_encode()` L2-normalizes both backends' output so the dot product
used by vec0 and the legacy path equals cosine. Each row records its producing
backend in `embed_backend` (NULL = legacy). The two backends are not
numerically identical, so after a switch run `tk-migrate-embeddings --all`
(`migrate_embeddings.py`) to recompute stale rows into one consistent space.

## MCP tools (107 total)

Compact grouping by module. Full signatures are in the code; `_mcp.py`
auto-generates JSON-Schema from annotations.

| Module | N | Tools |
|---|---|---|
| threads | 12 | open_thread, note, close_thread, idle_thread, brief, context, search, compost, evolve_format, evolve_review, auto_review_trigger, mark_skill_materialized |
| peers | 11 | whoami, peers, presence, broadcast, whisper, ask, respond, wait, inbox, live_status, search_via_parent |
| spawn | 7 | spawn, tournament, tasks, task_logs, spawn_status, spawn_budget_status, spawn_budget_set |
| skills | 5 | skill_manage, skill_record, skill_list, curator_run, review_thread |
| dialectic | 5 | dialectic_claim, dialectic_evidence, dialectic_review, dialectic_synthesis, dialectic_supersede |
| probes | 5 | register_probe, run_probe, record_attempt, reliability_for, weak_spots |
| core_memory | 4 | core_set, core_get, core_list, core_remove |
| extract | 4 | extract_recent, review_candidates, accept_candidate, reject_candidate |
| distill | 4 | distill, vote_distill, pending_distillates, export_distillates |
| dialog | 3 | dialog_search, open_dialog_window, ingest |
| concepts | 3 | register_concept, list_concepts, expand_concept |
| graph | 3 | link, unlink, neighbors |
| pickup | 3 | pickup_candidates, claim_pickup, release_pickup |
| lessons | 4 | lesson_append, lesson_list, lesson_get, lesson_remove |
| shadow_review | 2 | shadow_review_run, shadow_review_status |
| candidate_reviewer | 2 | candidate_review_run, candidate_review_status |
| curator | 2 | curator_review, curator_review_status |
| evolve_applier | 5 | evolve_apply, evolve_apply_curator_report, evolve_mark_applied, evolve_mark_curator_report_applied, evolve_apply_status |
| style | 2 | style_set, verbatim_user |
| process_health | 2 | mp_health, mp_cleanup |
| dashboard | 1 | mp_dashboard |
| agent_status | 1 | agent_status |
| memory_guard | 2 | memory_guard_status, memory_guard_check |
| correlation | 2 | tag_signal, task_thread |
| consolidate | 1 | consolidate |
| validate | 1 | validate_threads |
| invariants | 1 | find_invariants |
| missed_spawns | 1 | find_missed_spawns |
| session | 1 | session_end |

Each @mcp.tool() is a synchronous Python function; FastMCP wraps it in
JSON-Schema automatically from type annotations. One process — one mcp
instance (`threadkeeper._mcp.mcp`).

## Tests

```
tests/
├── conftest.py                fresh_mp fixture: tmp DB, isolated env,
│                              re-import of the package per-test
├── test_tools_smoke.py        parametrized: every @mcp.tool() callable
├── test_identity.py           snapshot-bug regressions + ctx-line carries session_id
├── test_threads.py            lifecycle: open → note → close → idle revival
├── test_core_memory.py        Letta-tier: set/get/list/remove + brief surfacing
├── test_spawn_budget.py       admission control + daemon polling
├── test_search_proxy.py       request/response signal roundtrip
├── test_dialectic.py          smoothed-ratio confidence
├── test_skills.py             skill_manage frontmatter validation + curator
├── test_shadow_review.py      cursor advance + min_chars gate + idempotency
├── test_process_health.py     orphan detection with/without heartbeat
└── …
```

Run: `.venv/bin/python -m pytest tests/ -q`. Currently 495 tests (1 skipped),
all green. Smoke parametrization automatically picks up any new tools without
having to add tests.

## Env knobs (config.py)

| Knob | Default | Purpose |
|---|---|---|
| `THREADKEEPER_DB` | `~/.threadkeeper/db.sqlite` | sqlite file |
| `THREADKEEPER_EMBED_MODEL` | paraphrase-multilingual-MiniLM-L12-v2 | 384-dim, RU+EN |
| `THREADKEEPER_EMBED_BACKEND` | `onnx` | `onnx` (fastembed, no PyTorch) or `sentence-transformers` (fallback) |
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects` | jsonl for ingest |
| `CLAUDE_SKILLS_DIR` | `~/.claude/skills` | skills root |
| `THREADKEEPER_EXTRA_SKILLS_DIRS` | unset | os.pathsep-separated extra skills roots to mirror into |
| `THREADKEEPER_INGEST_INTERVAL_S` | 3 | daemon ingest tick |
| `THREADKEEPER_INGEST_CAP` | 50 | max msgs per call |
| `THREADKEEPER_SKILL_WATCH_INTERVAL_S` | 5 | skill_watcher tick |
| `THREADKEEPER_AUTO_REVIEW` | off | enable auto-review on close_thread |
| `THREADKEEPER_MEMORY_NUDGE_INTERVAL` | 10 | events between memory_save nudges |
| `THREADKEEPER_SKILL_NUDGE_INTERVAL` | 10 | events between skill_hint nudges |
| `THREADKEEPER_SPAWN_BUDGET_MB` | 3072 | combined child RSS cap; 0 disables |
| `THREADKEEPER_SPAWN_ESTIMATE_SLIM_MB` | 500 | initial slim child RSS guess |
| `THREADKEEPER_SPAWN_ESTIMATE_FULL_MB` | 1500 | initial full child RSS guess |
| `THREADKEEPER_SPAWN_BUDGET_POLL_S` | 10 | budget daemon tick; 0 disables |
| `THREADKEEPER_MENUBAR_AUTO_LAUNCH` | true | macOS: auto install/launch agent-status menu-bar app on MCP startup |
| `THREADKEEPER_MEMORY_GUARD_POLL_S` | 30 | server RSS guard tick; 0 disables |
| `THREADKEEPER_MEMORY_GUARD_WARN_MB` | 1536 | notify/log above this server RSS |
| `THREADKEEPER_MEMORY_GUARD_KILL_MB` | 3072 | SIGTERM server above this RSS; 0 disables killing |
| `THREADKEEPER_MEMORY_GUARD_AGG_WARN_MB` | 2048 | notify/request trim above combined server RSS |
| `THREADKEEPER_MEMORY_GUARD_AGG_KILL_MB` | 3072 | retire stale idle servers under aggregate pressure |
| `THREADKEEPER_MEMORY_GUARD_RECLAIM_MB` | 1024 | local RSS floor before warn-triggered self trim |
| `THREADKEEPER_MEMORY_GUARD_TARGET_SERVERS` | 1 | target process count after stale retirement |
| `THREADKEEPER_MEMORY_GUARD_RETIRE_IDLE_S` | 900 | stale heartbeat age before server retirement |
| `THREADKEEPER_MEMORY_GUARD_RETIRE_LIVE` | off | allow retiring parent-alive MCP servers |
| `THREADKEEPER_MEMORY_GUARD_NOTIFY` | on | send macOS desktop notification when possible |
| `THREADKEEPER_MEMORY_GUARD_COOLDOWN_S` | 300 | notification cooldown per pid/level |
| `THREADKEEPER_SHADOW_REVIEW_INTERVAL_S` | 0 | shadow daemon tick; 0 disables |
| `THREADKEEPER_SHADOW_REVIEW_WINDOW_S` | 900 | sliding window for shadow |
| `THREADKEEPER_SHADOW_REVIEW_MIN_CHARS` | 500 | spawn threshold |
| `THREADKEEPER_PROBE_INTERVAL_S` | 0 | probe daemon tick; 1800 = 30 min recommended for prompt answer grading |
| `THREADKEEPER_PROBE_COOLDOWN_S` | 604800 | per-category objective probe cooldown; 86400 = 1d recommended for active reliability tracking |
| `THREADKEEPER_NO_EMBEDDINGS` | off | force-disable st model (slim children) |
| `THREADKEEPER_WRITE_ORIGIN` | foreground | provenance tag for curator |
| `THREADKEEPER_SPAWNED_CHILD` | off | spawn-internal marker; disables autonomous child daemons |
| `THREADKEEPER_FORCE_CID` | — | test-only / spawn-injected cid override |
| `THREADKEEPER_SELF_CID_TTL_S` | 5 | mtime-fallback cache TTL |

## Behavioral nudges (active push)

`brief.py` + `nudges.py` contain sections that don't write data but push the
agent in the right direction:

- **spawn_hint** — a one-line reminder when conditions suggest parallel
  decomposition (≥3 active threads with no live children; ≥3 idle; cue-word
  "in parallel / while you / in the background" in the last user message). Not
  shown if there is already a live child. Why: spawn has existed for a while,
  but agents read the tool list as a catalog — not as a primitive. The trigger
  turns "the option exists" into "the moment to apply it".

- **skill_hint** — when there is a rich pending closed thread + the counter
  has crossed `SKILL_NUDGE_INTERVAL`. 2× → ⚠️ demanding.

- **memory_nudge** — turn counter: session events (open_thread, close_thread,
  note:insight/move, core_set, verbatim_user, concept_register, distill) since
  the last memory_save. Crossing `MEMORY_NUDGE_INTERVAL` → soft;
  2× → ⚠️ demanding.

Pattern for future nudges: short section, compact format, explicit
"→ consider X" line. Fire only when the not-doing-it cost > the brief
real-estate cost.

## What is NOT done

- No authentication / access control (see ROADMAP.md).
- No federation: one database file, one machine.
- Heavily Claude-Code-specific: ppid walk, jsonl parser, settings.json hooks,
  ~/.claude.json as MCP-config template.
- Extraction heuristics are simple regexes; no ML quality classifier.
- No hot-config reload: changing an env-knob requires restarting the MCP
  process.
- MCP-native `sampling/createMessage` (a native review fork without
  pay-per-use tokens) is not yet implemented in Claude Code
  (anthropics/claude-code#1785). spawn-subprocess is the fallback, slim-config
  brings the cost down to acceptable.
