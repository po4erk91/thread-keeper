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
├── _mcp.py            FastMCP singleton (shared @mcp.tool / .resource / .prompt registrar)
├── server.py          entry point: import all tools/ → mcp.run() (stdio)
├── config.py          pydantic-settings Settings ← ~/.threadkeeper/.env (DB_PATH, …)
├── db.py              SCHEMA + user_version migrations + WAL-knobs + sqlite-vec loader
├── identity.py        per-process session + self-cid + daemon launchers
├── ingest.py          live ingest of jsonl transcripts + skill_usage backfill
├── verify_ingest.py   cross-CLI production verification — slot coverage + PASS/PARTIAL/FAIL verdict (issue #1)
├── eval/              offline learning-loop decision-quality harness — precision/recall/F1 + judge↔human agreement (issue #72)
├── embeddings.py      pluggable backend (ONNX/fastembed default; ST fallback), cosine search
├── migrate_embeddings.py  CLI: recompute stored vectors after a backend switch
├── helpers.py         ID generators, fmt_age, q-quoting, alive-pid check
├── elicitation.py     capability-gated MCP form confirmations (#26)
├── github_budget.py   shared gh API rate-limit/cooldown ledger
├── agent_status.py    structured loop/agent/recent-result status for UI clients
├── brief.py           render_brief() / render_context() — main digest
├── egress.py          cross-provider memory egress policy (issue #74)
├── nudges.py          counter-driven memory_nudge / skill_hint / auto-review
├── review_prompts.py  MEMORY/SKILL/COMBINED/ANTI_CAPTURE for review-forks
├── process_health.py  orphan-detection (ppid + heartbeat)
├── menubar_app.py     macOS NSStatusItem app autoinstall/autolaunch
├── assets/macos-agent-status/
│                     Swift NSStatusItem source bundled in wheel/sdist
├── memory_guard.py    daemon: notify + SIGTERM when server RSS exceeds limits
├── auto_update.py     daemon: daily git/pip self-update + restart-on-update
├── skill_watcher.py   daemon: external edits to SKILL.md → patch_count++
├── skill_updater.py   daemon: twice-weekly installed skill update + mirror sync
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
    ├── curator.py     curator_review/status/restore
    ├── lessons.py     lesson_append/list/get
    ├── lessons.py     lesson_append/list/get/remove/restore
    ├── concepts.py    register/list/expand/manage
    ├── graph.py       link/unlink/neighbors
    ├── correlation.py tag_signal/task_thread
    ├── pickup.py      pickup_candidates/claim/release
    ├── dialog.py      dialog_search/open_dialog_window/ingest
    ├── validate.py    validate_threads
    ├── style.py       style_set/verbatim_user
    ├── resources.py   @mcp.resource — memory://brief|context|dashboard|agent-status (#78)
    ├── prompts.py     @mcp.prompt — review_recent_threads/run_library_curation/audit_threadkeeper (#78)
    ├── invariants.py, missed_spawns.py, consolidate.py, session.py, …
```

Launch: `python -m threadkeeper.server`. Stdio-MCP, no ports. On macOS, the
entry point also best-effort installs and launches the loop-status menu-bar
app before `mcp.run()`; all subprocess output is captured so stdout remains
reserved for MCP frames. Source checkouts keep the Swift app at
`apps/macos-agent-status/`; packaged installs use the bundled copy under
`threadkeeper/assets/macos-agent-status/` and build from a scratch directory
under `~/.threadkeeper/tasks/`, so the widget does not depend on a repo clone or
writes inside `site-packages`. Installed bundles store a source fingerprint in
`Contents/Resources/threadkeeper-source.sha256`; missing or mismatched markers
force a rebuild even when file mtimes make an older helper look newer than the
packaged source. The menu-bar app uses AppKit `NSStatusItem` for an icon-only
status-bar item and a SwiftUI popover for the panel. It polls
`tk-agent-status --json` every 15 seconds off the main actor, receives loops
sorted by active state (`running` → `ready` → `idle` → `off`), shows Probe
backlog as due objective probes only, updates the idle chip / running gears
directly on the status button, keeps loop counts in the popover/tooltip, and
posts macOS notifications for newly observed useful `recent_results`. The
popover includes a power button that writes `THREADKEEPER_DISABLE_BG_DAEMONS`
to the same `.env` file and requests a ThreadKeeper restart, giving the widget
a one-click pause/resume path for autonomous loops. The
popover header gear opens a separate AppKit window
for editing `~/.threadkeeper/.env` (or `THREADKEEPER_ENV_FILE`): SwiftUI guided
controls cover common daemon, memory, and spawn-routing knobs, an advanced tab
preserves raw `.env` text, three presets are stored in `UserDefaults`, and
Save & Restart writes the file before terminating live `threadkeeper.server`
processes so MCP hosts restart them with the new environment. Spawn routing UI
stores `antigravity` as the canonical CLI key (`agy` is the executable alias),
keeps `gemini` as a legacy Gemini CLI key, and uses exact dropdown values for
model pins instead of free-text model strings.
Foreground parent MCP sessions also start `auto_update.py` when
`THREADKEEPER_AUTO_UPDATE_INTERVAL_S>0` (86400 seconds by default). Each due pass
is single-flight across live servers, records `events.kind='auto_update_pass'`,
and applies the install-appropriate update path: clean git checkouts fetch and
fast-forward their tracked branch, then reinstall editable; package installs run
`pip install --upgrade` in the current interpreter environment only after the
latest PyPI release's non-yanked files pass the provenance gate. That gate
queries PyPI JSON metadata plus the Integrity API, requires a Trusted Publisher
bundle for `po4erk91/thread-keeper` from `publish.yml` in environment `pypi`,
and checks the attested subject filename/SHA-256 against PyPI metadata before
`pip` is invoked. Missing provenance, mismatched publisher identity, or digest
mismatch returns `refused mode=pip ...` and records an `auto_update_pass` without
restarting. After a successful install, `THREADKEEPER_AUTO_UPDATE_SETUP` governs
the setup step: `check` (default) runs `thread-keeper-setup --dry-run`, records
`setup=checked status=unchanged` or `status=changes_pending`, and logs when the
dry-run would rewrite MCP registrations, hook wiring, or managed instruction
blocks; `apply` is standing consent to run the full setup writer; `skip` avoids
the setup subprocess. Successful updates optionally exit the current MCP process
(`THREADKEEPER_AUTO_UPDATE_RESTART` default true) so the host reconnects to the
new code, but only after setup check/apply and a subprocess import smoke check
both pass. Install/setup/import failures are recorded on the `auto_update_pass`
event with `restart=suppressed`, and the already-running process stays alive on
its current in-memory code. Set `THREADKEEPER_AUTO_UPDATE_INTERVAL_S=0` to opt
out of the standing-consent update channel entirely; the provenance gate itself
is controlled by `THREADKEEPER_AUTO_UPDATE_VERIFY_PROVENANCE` for break-glass
mirrors.
The upstream publish path is gated before that provenance exists: the
post-test release readiness workflow is read-only and does not dispatch PyPI,
while `publish.yml` requires a GitHub-verified signed annotated `v*` tag and
the protected `pypi` environment before the Trusted Publisher upload job can
run.
The legacy monolith `server.py` at the repo root was removed in May 2026 — the
runtime is fully on the package.

## Storage layers

The database is `~/.threadkeeper/db.sqlite`. On POSIX systems, config
startup and `get_db()` best-effort harden the default store:
`~/.threadkeeper` is `0700`, while `db.sqlite`, SQLite `-wal`/`-shm`
sidecars, `~/.threadkeeper/.env`, and curator `REPORT-*.md` files are
`0600`. Logically six levels:

`get_db()` gates baseline schema setup and legacy column migrations with
SQLite `PRAGMA user_version`. Databases at the current version skip the
historical `ALTER TABLE` list entirely; v0 databases acquire a
`BEGIN IMMEDIATE` writer transaction, create baseline tables/indexes, apply
legacy columns, run data backfills, then set `user_version` to the code's
`CURRENT_SCHEMA_VERSION`. A second process that waited on the writer lock
re-checks `user_version` before doing work, so only one process performs the
migration. Duplicate-column `ALTER TABLE` failures are the only swallowed
migration errors; any other `OperationalError` is logged and raised with the
version left unchanged for a retry.

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
   shadow-review daemon. The retention pass can prune aged dialog rows and
   deletes their FTS/vec mirrors in the same pass. Before new transcript
   content is written to
   `dialog_messages`, mirrored into `dialog_fts`, embedded, or backfilled into
   FTS, ingest masks common credential-shaped values such as authorization
   headers, bearer/OAuth tokens, AWS keys, `.npmrc` / `.netrc` credentials, and
   `*_TOKEN=` / `*_SECRET=` assignments. The redaction is default-on and can be
   disabled with `THREADKEEPER_REDACT_DIALOG_SECRETS=0` only for local debugging
   that intentionally trades away the durable secret-scrubbing guarantee.

4. **events + cursors + presence + signals** — live channel: every mutating
   action writes an event, each session has a cursor, and `live_status()`
   counts `live=N` by cursor delta. Signals — broadcast/whisper/
   search_request/search_response between parallel windows. Event and signal
   pruning is owned by the retention daemon, not by read tools.

5. **skill_usage** — telemetry for mirrored Skill.md entries. Fields:
   `last_used_at`, `last_viewed_at`, `last_patched_at`, counters, `state`
   (active/stale/archived), `pinned`, `created_by_origin` (foreground vs
   background_review vs shadow_review). This is the input for the curator.

6. **lesson_usage** — telemetry for `lessons.md` slugs. `lesson_list` bumps
   `view_count`, `lesson_get` bumps `use_count`, and the curator uses
   `last_used_at` / `last_viewed_at` / pull counts for decay scoring.
   `pinned=1` and `tier='validated'` exclude a lesson from stale-compost
   recommendations.

7. **curator snapshots** — file archives under
   `<curator_reports_dir>/snapshots/<pass-id>/` written before a destructive
   curator child is spawned. Each snapshot contains `lessons.md`, copied
   in-scope skill dirs, `manifest.json`, and tombstones emitted by curator
   prune/delete tool calls. `curator_restore` restores a lesson or skill from
   that archive. Retention is bounded by
   `THREADKEEPER_CURATOR_SNAPSHOT_RETENTION`.

8. **dialectic_claims + dialectic_evidence** — Honcho-style discrete user
   model. Claim with a domain, evidence support/contradict/clarifying, sm-ratio
   confidence; brief() renders medium+high grouped by domain.
   `dialectic_observations` is the capture buffer: `pending` rows are unclaimed
   backlog, `claimed_at`/`claimed_by_task` means a validator child owns the
   batch, and `processed` is terminal. Stale claims are requeued.

In addition: `probe_results`/`reliability`, `concepts`, `edges`,
`extract_candidates`, `distillates`/`votes`, `tasks` (spawned children:
`started_at`/`ended_at`/`duration_s`, `return_code`, RSS, and optional
`tokens_in`/`tokens_out`/`tokens_total`/`cost_usd` captured from CLI usage
trailers), `github_rate_budget` (per-account GitHub API remaining/reset and
cooldown state shared by roadmap automation), `shadow_review_pass` (as
event.kind). Ended `tasks` rows are
bounded by `consolidate()`: it retains rows protected by either
`THREADKEEPER_TASK_RETENTION_DAYS` (30 by default) or
`THREADKEEPER_TASK_RETENTION_COUNT` (1000 by default), never deletes live
`ended_at IS NULL` rows, and garbage-collects task spool files whose task row is
no longer retained.

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
- **retention** — ticks every `RETENTION_INTERVAL_S` (default 0/off). When
  enabled, prunes opted-in aged `dialog_messages`/`tasks`/`signals`/`events`/
  `probe_results`, keeps dialog FTS/vec mirrors consistent, and can run
  `VACUUM` plus `PRAGMA wal_checkpoint(TRUNCATE)`.
- **search_proxy** — serves `search_via_parent` from slim children via
  signals (see below).
- **spawn_budget** — once per `SPAWN_BUDGET_POLL_S` (default 10 s) walks
  the subtree of each `running` task via `ps`, updates `tasks.rss_kb` and
  closes dead ones. The same sweep is the **wall-clock watchdog** (#80): a
  pid>0 child whose row outlives `SPAWN_MAX_RUNTIME_S` (1 h default; 0
  disables) is `SIGTERM`'d, then `SIGKILL`'d after `SPAWN_KILL_GRACE_S`, and
  its row is closed with `return_code` 124 so the spawning loop's single-flight
  (`_running_*_children`) releases. It then immediately starts a capped
  continuation retry with the original assignment plus the previous
  task/cid/log pointers so the new child can inspect workspace state and resume
  instead of restarting blindly. (When the RSS budget is disabled but the
  watchdog is on, the daemon still runs.)
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
- **skill_updater** — once per `SKILL_UPDATE_INTERVAL_S` (default 302400 s,
  twice weekly) runs single-flight across live MCP servers, imports the newest
  local installed skill copy from any configured CLI root into the primary
  `~/.claude/skills` root, mirrors successful updates back to every root, and
  updates GitHub-backed skills that carry `.threadkeeper-skill-source.json` or
  can be inferred from `THREADKEEPER_SKILL_UPDATE_SOURCES`. Replaced local
  skills are backed up under the state dir, and source-tracked local edits after
  the last upstream hash are skipped rather than overwritten.
- **config_watcher** (`config_watcher.start_config_watcher`) — once per
  `CONFIG_WATCH_INTERVAL_S` (default 2 s, 0 = off) stats **two** targets, each
  with its own mtime cursor (#2, generalized cross-CLI in #133):
  1. the **universal env-file** `~/.threadkeeper/.env` (`config._ENV_FILE`,
     overridable via `THREADKEEPER_ENV_FILE`). Every host's `Settings()` reads
     this file, so it is the one layer that hot-reloads config for all seven
     CLIs — not just Claude. On change it calls `config.reload_settings()` with
     **no** `os.environ` mirroring: pydantic re-reads the file natively and
     real spawn-time env vars keep precedence (env var > `.env` > default), so
     there is no precedence inversion.
  2. the **host CLI's own env-block file**, resolved from the active-CLI
     identity (`identity.active_cli()`; Claude Code → `~/.claude/settings.json`).
     Its `env` block's threadkeeper-relevant keys (`THREADKEEPER_*` plus the
     unprefixed `CLAUDE_SKILLS_DIR`/`CLAUDE_PROJECTS_DIR`) are mirrored into
     `os.environ` (applying new values, dropping deleted ones) — **minus** any
     key a higher-priority scope pinned at spawn (detected at first sighting
     when `os.environ` disagrees with the file's value), so the lowest-priority
     user file never silently overrides a project/local/managed value (#133
     problem 1). CLIs whose env block is not a flat JSON map rely on target 1.

  `THREADKEEPER_CONFIG_WATCH_PATH` is an escape hatch / test seam: it pins ONE
  file (watched as a CLI-settings target) and disables the universal env-file
  target — legacy single-file mode. Either target's change calls
  `config.reload_settings()`, which re-instantiates `Settings`, re-publishes
  the UPPER_CASE module constants, and `_propagate`s each changed value into
  every loaded `threadkeeper.*` module that did `from .config import X` — which
  works because a function resolves a module-global name at call time, so the
  next daemon tick / tool call sees the new value with no restart (the issue-#2
  acceptance test: change the shadow interval, `shadow_review_status()` reflects
  it within ~1 s). A daemon whose interval crossed 0 → >0 is started here; the
  rest self-adjust (and `daemon_sleep` keeps a hot-disabled loop from
  busy-spinning on `time.sleep(0)`, and jitters every sleep by ±15% so
  concurrent MCP instances on one host don't fire their `ps`/notify work in
  lockstep — #86). Cold start records only a baseline (the env is already
  applied at spawn); a half-written file is skipped via the mtime-cursor +
  JSON-parse guard and retried. Manual trigger `config_reload()`; diagnostics
  `config_watch_status()` (reports both watched files). Embedding-backend /
  process-identity flags are intentionally not hot-reloaded.

Spawning daemons that enforce single-flight share one non-blocking
`helpers.single_flight_lock(name)` primitive around their local
check-running-then-spawn critical section. The `fcntl.flock` pidfile under
the DB directory closes the same-host TOCTOU window; the tasks-table
running-child check remains for stale-pid cleanup and status visibility.

Orthogonal to concurrency, **cadence** is persisted in the `daemon_state`
table (`daemon_state.claim_pass`): each scheduled tick claims its slot with
one atomic upsert keyed by loop name, so a freshly started MCP server does
not treat every interval daemon as overdue and refire it (pre-fix, every new
CLI session ran its own "overdue" curator/probe/shadow pass — the flock only
stops *concurrent* passes, not *frequent sequential* ones). Scheduled ticks
that lose the claim return `not_due`; manual / tool-invoked passes bypass
the gate but still record the run, pushing the next scheduled fire a full
interval out. Applies to shadow_review, curator, candidate_reviewer,
extract, dialectic miner/validator, probe, thread_janitor, and retention;
evolve reviewer/applier, skill_updater, and auto_update keep their own
pre-existing due gates.

- **shadow_review** — once per `SHADOW_REVIEW_INTERVAL_S` (default 0 = off),
  scans a dialog window and, if needed, spawns a slim-child evaluator behind
  `shadow-review.lock` plus the running shadow-child check. Lock-busy passes
  return `shadow_child_running ... (single-flight lock)` without advancing the
  dialog cursor, so the same useful window is retried after the active pass.
- **candidate_reviewer** — once per `CANDIDATE_REVIEW_INTERVAL_S` (default
  0 = off) reviews pending `extract_candidates` through one slim child. The
  queue is machine-wide single-flight through the shared helper's
  `candidate-reviewer.lock` plus running-task detection, preventing multiple
  foreground MCP servers from spawning duplicate reviewers for the same
  pending candidates.
- **probe_daemon** — once per `PROBE_INTERVAL_S` (default 0 = off) grades
  finished probe answers, then spawns at most one due objective probe runner
  behind `probe-daemon.lock` plus the running probe-child check. A busy lock
  reports `probe_child_running ... (single-flight lock)` and never blocks a
  daemon tick.
- **dialectic_validator** — once per `DIALECTIC_VALIDATE_INTERVAL_S` (default
  0 = off) leases a concrete batch of `dialectic_observations` before building
  the validator prompt, then spawns one child behind `dialectic-validator.lock`
  plus the running validator-task check. Spawn failures release the just-claimed
  rows; parent crashes leave them under the normal stale-claim lease requeue.
- **curator** — once per `CURATOR_INTERVAL_S` (default 0 = off) audits the
  existing lessons / skills / concepts inventory through one slim child. Before
  spawning, it hashes the stable inventory state and compares it to the last
  recorded complete/endorsed pass; unchanged snapshots record an
  `unchanged_inventory` no-op event instead of re-deriving the same report.
  Wake-ups also coalesce behind the shared helper's non-blocking
  `curator.lock` plus the running curator-task check, so multiple foreground
  servers do not re-read and spawn against the same snapshot.
  `curator_review_status()` exposes the last
  endorsed `inventory_sha256` and the current inventory hash.
- **evolve_reviewer** (`evolve_daemon.start_evolve_daemon`) — once per
  `EVOLVE_REVIEW_INTERVAL_S` (default 0 = off) it reviews thread-keeper itself
  for security/privacy risks, memory leaks, runaway daemons, cost waste,
  reliability gaps, optimizations, and current agent/MCP/memory tooling ideas. It
  may update `docs/ROADMAP.md` through a PR and create/update GitHub issues with
  acceptance criteria and research sources. It does not implement roadmap issues.
  To avoid completing the lethal trifecta (private data + untrusted web content +
  exfiltration) in one child (#79), the pass is **split across two alternating
  phases**, chosen by the last recorded spawn phase: (1) a **research** child
  (`permission_mode="auto"`, tools `WebSearch,WebFetch,Read,Glob,Grep,Write` —
  no `Bash`, no `bypassPermissions`, no `gh`) that distills external findings to
  `~/.threadkeeper/evolve-research/RESEARCH-<ts>.md` and has no network-write
  tool to exfiltrate with; then (2) an **audit** child (`bypassPermissions` +
  `Bash,Edit,Write` but **no** `WebSearch`/`WebFetch`) that audits the repo and
  does the GitHub/ROADMAP writes, consuming the digest inside an explicit
  `<<<EVOLVE_RESEARCH_DATA … EVOLVE_RESEARCH_DATA` fence it must treat as data.
  Its duplicate-issue check uses a paginated oldest-first REST issue listing,
  not a newest-first 50-item `gh issue list` window, so older open issues remain
  visible as the backlog grows. Before that child can create a roadmap-doc PR,
  the parent also checks open PRs for automation-owned changes touching
  `docs/ROADMAP.md` and embeds the result in the audit prompt. Existing
  roadmap-doc PRs are appended to or skipped; new ones use/reuse the
  deterministic daily `docs/roadmap-audit-YYYY-MM-DD` branch and carry a
  PR-body marker for future passes.
  Both phase prompts open with the same `"You are an EVOLVE REVIEWER"` line, so
  the running-child check and shadow/extract exclusion cover both; dispatch is
  serialized by `evolve-reviewer.lock`. A full research → audit cycle spans two
  due passes.
- **evolve_applier** (`evolve_applier.start_evolve_applier_daemon`) — once per
  `EVOLVE_APPLY_INTERVAL_S` (default 0 = off) first fetches open GitHub PRs via
  `gh pr list --json mergeStateStatus,mergeable,...` and repairs the oldest
  same-repo applier PR whose merge state is conflicted (`DIRTY` /
  `CONFLICTING`). This sweep is a hard preflight before new work: if PR state
  cannot be read, the pass records `conflicted_pr_fetch_error` and does not
  pick a fresh roadmap issue, Curator report, or promoted evolve suggestion.
  Only same-repository `roadmap/…` and `evolve/…` head branches are eligible;
  fork PRs are never fed to the privileged repair child. The repair child checks
  out the existing PR branch, merges the current base branch, resolves
  conflicts, runs the full suite, and pushes back to the same branch. It then
  waits for GitHub checks on the pushed PR head and runs
  `gh pr merge --squash --delete-branch`, so GitHub lands the PR into `main`
  through branch protection instead of a raw local `git push origin main`.
  When no conflicted applier PR exists, it fetches open GitHub issues via the
  REST API (`gh api repos/{owner}/{repo}/issues` — needed because `gh issue
  list --json` cannot return `author_association`; pull requests in the
  response are filtered out). The fetch is explicit `--include --paginate`,
  `sort=created&direction=asc`, so the subsequent local priority
  (`roadmap`-labeled issues first, then FIFO by issue number) applies across
  the open backlog rather than only the newest page. A generous local candidate
  window is retained as a runaway guard; if exceeded, a warning logs the number
  of open issues outside the window. The applier then spawns one
  `evolve_applier` child to implement exactly one issue.
  **Shared GitHub budget (#38):** parent `gh` calls and the privileged child
  PATH `gh` wrapper consult `github_rate_budget` before every request. Included
  REST headers update `X-RateLimit-Remaining` / `X-RateLimit-Reset`; primary
  403s cool down until reset (bounded to one hour), and secondary-limit or
  `Retry-After` responses create a bounded exponential cooldown. While the
  cooldown is active, foreground status commands, reviewer/applier daemons, and
  spawned shell `gh` calls fail fast instead of independently retrying the same
  account quota. `agent_status` / `tk-agent-status` and
  `evolve_apply_status()` expose the current remaining count or cooldown window.
  **Author-trust gate (#63):** the repo is public, so any account can open an
  issue whose body is injected into the permission-bypassing child. Autonomous
  pickup is therefore limited to issues whose `authorAssociation` is in
  `EVOLVE_TRUSTED_AUTHOR_ASSOCIATIONS` (default `OWNER,MEMBER,COLLABORATOR`) or
  that carry a maintainer-applied label in `EVOLVE_TRUST_LABELS` (empty by
  default; only collaborators can label a public repo, so a trust label is
  itself an endorsement). Untrusted issues are skipped until promoted; naming
  the exact number (`evolve_apply_roadmap_issue(issue_number=N)`) bypasses the
  gate as explicit human promotion. This removes the untrusted input at the
  boundary and complements the in-prompt data-fencing of #22/#76.
  **Skip-label gate (#50):** before claim/spawn work, `_open_roadmap_issues()`
  also excludes issues whose labels match `EVOLVE_APPLY_SKIP_LABELS` (default
  `blocked,needs-design,wontfix,question,discussion,help wanted`). These are
  human-gated backlog items: blocked, discussion/design, rejected, or reserved
  for contributors rather than an unsupervised `bypassPermissions` child. Queue
  mode records `roadmap_issue_skipped` telemetry and continues with clean
  candidates; exact mode returns `skipped: label X` for the named issue instead
  of switching tasks. The skip count/reasons show in `evolve_apply_status()`;
  `mp_dashboard()` counts the skip outcome.
  Before spawning, the parent runs five multi-host conflict guards in order: (1) skip
  if an active `<!-- thread-keeper:evolve-applier-claim -->` comment already
  exists; (2) skip if `gh pr list --search "in:body Closes #N"` shows an open
  PR already closing the issue; (3) post the parent's own claim comment (body
  carries only an opaque per-host token — `sha1(hostname)[:6]` — never raw
  hostname/PID/git-rev, which stay in the local `roadmap_issue_claim_host`
  event for triage); (4) wait
  `ROADMAP_CLAIM_RACE_WINDOW_S` (default 3s), re-fetch claims, and delete the
  parent's own claim when a competing host got there first (earliest
  `createdAt` wins); (5) on `spawn()` failure, retract the just-posted claim
  so the next pass can retry immediately. Claims expire after 24 hours as a
  fallback so crashed workers do not block issues forever; the implementer
  branch carries a 6-char hostname-hash suffix so two hosts past the claim
  check do not collide on `git push`. **Poison-issue backoff + dead-letter
  (#82):** each spawn records a `roadmap_issue_attempt` event, so an issue
  whose child keeps aborting without a PR is not retried every ~24h forever.
  An escalating cooldown — `ROADMAP_ISSUE_BACKOFF_BASE_S * 2^(attempts-1)`,
  default base 2 days so it exceeds the 24h claim TTL — defers re-selection,
  and after `ROADMAP_ISSUE_MAX_ATTEMPTS` (default 3) the issue is
  **dead-lettered**: a `blocked` label and a one-time summary comment are
  applied (composes with the #50 skip-label gate) and it drops out of the
  auto-drain until a human intervenes. A `roadmap_issue_dead_letter` event is
  the authoritative idempotent marker; the label/comment are best-effort
  signals. A successful child writes `roadmap_issue_applied` (checked first
  everywhere), so only genuinely-failing issues accrue attempts. An exact
  `evolve_apply_roadmap_issue(issue_number=N)` override bypasses the cooldown
  and the cap, but the default skip-label gate still refuses the `blocked`
  label until it is removed or reconfigured; per-issue attempt counts/states
  surface in `evolve_apply_status()` and stuck/dead-letter counts in
  `mp_dashboard()`. In queue mode, issue-local dispatch
  failures advance to the next issue; exact
  `evolve_apply_roadmap_issue(issue_number=N)` calls report the specific
  failure instead of switching tasks. The PR body must include `Closes #N`;
  after `gh pr create` prints a real URL, the child calls
  `evolve_mark_roadmap_issue_applied(issue_number, pr_url)` so the daemon does
  not pick it again while human review/merge is pending.
  **Applied-marker reconciliation (#51):** before honoring that marker for an
  open issue, `_open_roadmap_issues()` checks the recorded applier PR state via
  `gh pr list --state all`. Open PRs and merged PRs keep the issue suppressed;
  a closed-unmerged PR records `roadmap_issue_requeued`, supersedes the marker,
  and lets the issue pass through the existing retry backoff/dead-letter gates.
  **Issue/body safety (#22):** the issue body and legacy evolve suggestions are
  embedded only inside explicit data fences, and privileged evolve children get
  a PATH-prepended
  `gh` wrapper. For `gh issue create`, `gh issue comment`, and `gh pr create`,
  the wrapper redacts `/Users/<name>/...` and `/home/<name>/...` paths plus
  common token shapes before the real GitHub CLI receives the body, refusing if
  a known unsafe pattern remains. Parent-authored claim/dead-letter comments use
  the same scrubber before spawning `gh`. If no issue is pending,
  it falls back to the latest complete Curator `REPORT-*.md`, then to the oldest
  promoted + unapplied legacy `evolve_format` suggestion. Curator report apply
  uses memory MCP tools only (`lesson_append`, `lesson_remove`, `skill_manage`)
  and records `curator_report_applied`; no code edit or PR. Legacy code-evolve
  apply still opens a PR and calls `evolve_mark_applied(evolve_id, pr_url)`.
  Machine-wide single-flight uses `evolve-applier.lock` plus the `"You are an
  EVOLVE APPLIER"` prompt prefix. Automatic apply passes respect the configured
  interval to avoid duplicate issue workers across foreground server startups;
  manual apply tools still dispatch immediately. If no roadmap issue is
  startable, the pass falls back to Curator reports and then legacy promoted
  `evolve_format` suggestions. Both the reviewer and the code/PR applier paths
  operate on a real git checkout, resolved by `_ensure_repo_ready()` in this
  order: (1) an explicit `EVOLVE_REPO_ROOT` (`THREADKEEPER_EVOLVE_REPO_ROOT`);
  (2) by default, a dedicated **managed checkout** under the DB dir
  (`~/.threadkeeper/evolve-repo`), **auto-cloned on first use** (from
  `EVOLVE_REPO_URL`/`EVOLVE_REPO_BRANCH`, defaulting to the upstream repo) and
  given its own `.venv` with the `[semantic,dev]` extras so the children can
  branch, run the suite, and open PRs; (3) only when auto-clone is disabled does
  the package's parent dir (when it carries a `.git` entry — the
  editable-from-checkout `install.sh`) serve as an in-place fallback. The
  managed checkout is the default even for editable installs on purpose
  (**isolation, #164**): the loops branch-switch, merge and hard-reset the tree
  they work in, and the editable package-parent is the user's own working tree —
  running there would flip its branch out from under an in-progress edit. It
  also gives the issue → PR flow a clean origin-tracking base. This makes the
  loops work by default with no configuration and without touching your
  checkout. Set `THREADKEEPER_EVOLVE_AUTO_CLONE=0` to disable provisioning and
  keep the pre-isolation in-place behaviour on an editable install; on a
  non-checkout install with auto-clone off the loops report
  `ERR evolve_repo_unavailable=<path>` until an explicit `EVOLVE_REPO_ROOT` is
  provided. An explicit override that is not itself a checkout is never
  auto-cloned into and reports `ERR repo_root_not_git`. Provisioning is
  serialized by `evolve-repo-provision.lock`. Curator report apply needs no git
  tree and runs regardless.

  Before the privileged reviewer audit or any code/PR applier child is spawned,
  the parent enforces local git safety on that checkout: `git status
  --porcelain --untracked-files=no` must be empty (`skipped_dirty_worktree
  mode=git` is recorded on `events.kind='evolve_git_safety'` when tracked WIP is
  present), and no other PR-producing evolve reviewer/applier task may already
  be running. The guard intentionally ignores untracked scratch files, matching
  `auto_update`'s dirty-check semantics. Child prompts then branch from a fetched
  base ref (`origin/main` by default, or `origin/<EVOLVE_REPO_BRANCH>`) instead
  of arbitrary current `HEAD`; reviewer roadmap-doc prompts additionally reuse
  the daily `docs/roadmap-audit-YYYY-MM-DD` branch or an existing open
  roadmap-doc PR branch so repeated audits do not collide.
- **curator → evolve bridge** — the Curator's lessons/skills audit remains
  snapshot-first and report-first: destructive mode writes a recoverable
  snapshot before spawning the child, then the child writes its REPORT before
  mutating. When a skill or lesson exposes a concrete improvement for
  thread-keeper itself it may call `evolve_format(...)` and record an
  `EVOLVE_CANDIDATE:` line in the report. That candidate is input for
  `evolve_reviewer`, not something the Curator implements directly.
- **concepts lifecycle** — the `concepts` store is no longer write-only.
  `register_concept` / `accept_candidate(kind='concept')` dedup on write: a
  re-surfaced equivalent invariant (description cosine ≥ 0.85, with a
  normalized-string fallback when embeddings are off) corroborates the existing
  row — bumping `last_evidence_at` to now and raising confidence to
  `max(existing, incoming)` — instead of inserting a near-duplicate. That keeps
  `last_evidence_at` a live corroboration-recency signal, which the brief orders
  on and the Curator's concept rubric reads. The Curator (in destructive mode)
  and the curator-report applier apply their `CONSOLIDATE_CONCEPT` /
  `PRUNE_CONCEPT` / confidence-review recommendations via the `concept_manage`
  tool (`remove` / `consolidate` / `set_confidence`). Concepts are all
  system-generated, so — unlike `lesson_remove` — `concept_manage` needs no
  `force` guard; every concept is curatable.

Autonomous learning daemons only run in foreground parent processes. Spawned
children carry `THREADKEEPER_SPAWNED_CHILD=1`, and review forks also carry a
non-foreground `THREADKEEPER_WRITE_ORIGIN`; either condition prevents
shadow/extract/curator/candidate-reviewer daemons from starting recursively.

The daemons share the `get_db()` connection pool; sqlite WAL allows one writer
+ many readers without blocking.

### Daemon-host + thin servers (Phase 1)

Behind `THREADKEEPER_DAEMON_HOST` (`0` by default — dark; no CLI config
change), the daemon block above moves out of the per-session server and into
one always-on headless process per machine:

- **Election** — `python -m threadkeeper.host` (`host.main()`) takes
  `HOST_LOCK_PATH` (`<db dir>/host.lock`) via `single_flight_lock`. A second
  host racing for the same lock exits 0 immediately (idempotent spawn), so
  there is never more than one host per machine.
- **The moved daemon block** — `host.start_daemons()` centralizes exactly the
  background ingester plus the 18 daemon starters that otherwise run inline in
  `identity._ensure_session`'s foreground branch (above), starting each once
  in the host process instead of once per session. With the flag off,
  `_ensure_session` calls `host.start_daemons()` itself in-process — byte-for-
  byte the pre-Phase-1 behavior, just centralized in one function.
- **Embed socket + FTS fallback** — the host binds a narrow embed-only unix
  socket (`host_embed.serve_embed_socket`, path `THREADKEEPER_HOST_SOCK`,
  default `<db dir>/host.sock`; newline-delimited versioned JSON —
  `{"v":1,"op":"embed","texts":[...]}` in, `{"v":1,"vectors":[...]}` or
  `{"v":1,"error":...}` out). In a thin server (`THREADKEEPER_ROLE=server`,
  the default under the flag), `embeddings._encode()` tries
  `embed_via_host(...)` first for any text it needs to embed — query or the
  session-start catch-up ingest pass below — instead of loading the model
  itself. `embed_via_host` returns `None` on any failure (no host, timeout,
  malformed reply), and `_encode` then honors
  `THREADKEEPER_THIN_EMBED_FALLBACK`: `fts` (default) propagates that `None` so
  the caller degrades to FTS-only, keeping the thin server model-free even
  when the host is down; `local` instead falls through to lazily loading the ONNX
  model in-process, trading the RAM savings for keeping semantic search fully
  available. The ongoing content-embedding work (new dialog rows) is produced
  by the host's own background ingest daemon; a thin server's session start
  still runs the pre-existing bounded catch-up ingest pass
  (`THREADKEEPER_INGEST_CAP`, unchanged by Phase 1), and any embedding it
  needs goes through this same host-then-fallback path.
- **`memory_guard` supervision** — thin servers are cheap (no ONNX, no
  daemon threads) and are excluded from aggregate idle-retire while the flag
  is on (`_idle_retire_candidates`); instead `supervise_host()` checks the
  host's presence heartbeat and, once it has gone stale past
  `THREADKEEPER_HOST_HEARTBEAT_TTL_S`, calls `host.ensure_host_running()` to
  spawn a replacement.
- **Always-on host, lazy spawn** — a thin server's `identity._ensure_session`
  calls `host.ensure_host_running()` on every session start: a no-op if a
  live heartbeat already exists, otherwise a **detached** spawn
  (`subprocess.Popen(..., start_new_session=True)`, redirected stdio) so the
  host outlives the session that spawned it and a closing CLI can never HUP
  it. The host then runs forever — no idle-exit — because the loops (daily
  retention, weekly curator, probe, …) must keep ticking with no active
  session; it exits only on SIGTERM (a `memory_guard` restart) or losing the
  host lock.

Flag off ⇒ zero behavior change: every session starts its own daemons and
embeds locally, exactly as before Phase 1.

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
for branch/commit/PR creation. The exposed `spawn()` MCP tool refuses
`bypassPermissions` unless the request comes from the evolve daemon
role/write-origin pairs (`evolve_reviewer`/`evolve`,
`evolve_applier`/`evolve_apply`), or the operator explicitly sets
`THREADKEEPER_ALLOW_BYPASS_PERMISSIONS_SPAWN=1`. Web tools
(`WebSearch`/`WebFetch`) are never
granted to a `bypassPermissions` child: the evolve reviewer's web research runs
in a separate read-only `permission_mode="auto"` child with no shell, so the
untrusted web content and the exfiltration-capable context are never the same
child (#79). All spawned children receive the parent's `THREADKEEPER_DB`, task
log dir, project dir, forced cid, and write-origin env so their direct
Python/MCP calls hit the same store as the parent.

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

### Cross-provider memory egress (issue #74)

`spawn()` resolves the child's target CLI via `resolve_agent(role, active_cli)`
and may route it to a third-party vendor (Codex→OpenAI, Gemini/Antigravity→
Google, Copilot→Microsoft). A slim child still loads the thread-keeper MCP, so
it can call `brief()` and pull the **personal-class** user-model into a prompt
processed by that vendor. `egress.py` is the control layer:

- `THREADKEEPER_MEMORY_EGRESS` (`all` default | `same-vendor` | `work-only`)
  decides whether personal-class sections (`verbatim`, `user_model (dialectic)`,
  `currently_testing`) render for a given consuming vendor. `all` skips the gate
  entirely → brief stays byte-identical to pre-#74.
- `render_brief(..., consumer_cli=…)` resolves the consumer from (1) the explicit
  arg, (2) `THREADKEEPER_EGRESS_CONSUMER` (set by `spawn()` so the spawn path is
  deterministic and doesn't depend on the child's own ppid walk), then (3)
  `identity.active_cli()`. When the policy restricts a third-party vendor, the
  personal sections are dropped and replaced by a one-line `withheld` disclosure.
- `spawn()` injects `THREADKEEPER_EGRESS_CONSUMER=<chosen_cli>` into both the
  child process env and the slim MCP config, so a child spawned to a third-party
  CLI cannot retrieve more personal memory than the policy allows for that vendor.

The native vendor is Anthropic — the brief format and personal memory are
authored in Claude sessions. `work`/`shared` classes always egress.

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

### Spawn budget (RSS + spend caps)

`spawn_budget.py` enforces a cap on the **combined RSS of all running spawned
children** (the parent itself is not counted). Default 3 GB. It also checks
optional 24h spawned-child token and dollar ceilings when
`THREADKEEPER_SPAWN_TOKEN_BUDGET` or
`THREADKEEPER_SPAWN_COST_BUDGET_USD` is configured; both default to `0`
(disabled), so unset budgets preserve prior behavior.

- `spawn()` admission control: inside `BEGIN IMMEDIATE`, `check_budget()` sums
  `rss_kb` of all running tasks (NULL = conservative full-estimate placeholder)
  and the recorded 24h `tokens_total`/`tokens_in`/`tokens_out`/`cost_usd` spend,
  then refuses if the new child would push past the RSS cap or if daily
  token/cost spend has already reached its configured ceiling. If admitted, the
  same transaction inserts the `tasks` row with the initial estimate
  (`SPAWN_ESTIMATE_SLIM_MB` / `SPAWN_ESTIMATE_FULL_MB`) before `Popen`; launch
  failure rolls the reservation back. ERR carries the exact numbers +
  how-to-override.
- Headless children run through `_spawn_wrap.py`, which tees the child's
  output, parses final JSON or human-readable usage trailers when present,
  stores `tokens_in`, `tokens_out`, `tokens_total`, and `cost_usd`, and always
  stores `duration_s` from the task timestamps. If no usage trailer is emitted,
  the row still has wall-time for cost/benefit triage. Captured `.log` files
  in `THREADKEEPER_TASK_LOG_DIR` are created owner-only (`0600`), matching the
  stdin prompt spool.
- Daemon ticks update real RSS via `ps`; dead root pids → `ended_at`.
- Visible spawns (Terminal.app) persist `pid=0`; the daemon resolves their live
  pid from the `--session-id <cid>` the child carries in `ps` argv and measures
  the same subtree RSS, so they contribute real memory, not the estimate. A
  visible row whose cid never resolves to a live process is reaped past
  `SPAWN_VISIBLE_TTL_S` (1 h default) so it can't pin budget capacity forever.
- Wall-clock watchdog (#80): a pid>0 child whose row outlives
  `SPAWN_MAX_RUNTIME_S` (1 h default; 0 disables) is killed
  (`SIGTERM`→`SPAWN_KILL_GRACE_S`→`SIGKILL` on its process group) and its row is
  closed with `return_code` `SPAWN_TIMEOUT_RETURN_CODE` (124, the `timeout(1)`
  convention). This frees the spawning loop's single-flight slot and the pinned
  budget share that a hung-but-alive child would otherwise hold forever. The
  kill is idempotent (the `ended_at IS NULL` guard). After the row is closed,
  the watchdog immediately launches a continuation retry unless
  `SPAWN_TIMEOUT_RETRY_LIMIT` is exhausted or disabled; retry rows keep
  `retry_of`, `retry_root`, and `retry_attempt`, while the killed row records
  `timeout_respawned_as` when spawn succeeds. The retry prompt points at the
  previous task/cid/log and tells the new child to inspect current state,
  preserve completed work, repair partial work, and continue. The kill is
  observable: `mp_dashboard` reports `tasks_timed_out` and `agent_status` reports
  `timed_out`. Complementary to #64 (visible/pid=0 RSS) and #66 (kill-path
  liveness correctness).

Tools: `spawn_budget_status()` (RSS cap/used/free/per-task plus recorded 24h
tokens/cost and remaining daily budget), `spawn_budget_set(MB)` (runtime RSS
override, not persisted). `mp_dashboard()` shows each loop's fire count with
24h spawn count, recorded tokens, dollar spend, wall-time, and mutation count,
so the #6 "is this loop worth the Opus minutes?" question has a numeric cost
dimension from #25 instead of only fire/outcome counts.

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
1. _last_shadow_rowid(): ingest-order high-water mark from
   events.kind='shadow_review_pass'.target (a dialog_messages rowid, #69).
2. _collect_window(): pull dialog_messages WHERE rowid > cursor (first-ever pass
   seeds the floor from now-WINDOW_S) — ALL sessions, not just our own — then
   apply the shared harvest lineage exclusion from `threadkeeper.harvest`.
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
8. Write events.kind='shadow_review_pass' with the new high-water rowid.
```

Dedupe — via an **ingest-order** cursor in `events.target` (the rowid of the
last evaluated message, #69). The cursor is the `dialog_messages` rowid rather
than the transcript `created_at`, so a late/out-of-order ingested row — a
resumed session, a freshly-installed adapter, or a post-downtime backfill,
whose `created_at` lands below the cursor — still gets a fresh rowid above it
and is reviewed exactly once (a `created_at` cursor silently stepped over it).
Idempotent: the monotonic rowid advance means a repeated tick will not
re-evaluate (or re-spawn on) what it has already seen.

Harvest boundary (#36): raw transcript ingest keeps autonomous child rows for
diagnostics, but dialog-derived memory loops must not learn from their own
exhaust. `threadkeeper.harvest` builds a recursive excluded-session set from
known internal prompt openers, spawn preambles, direct `tasks.spawned_cid`
children, native `agent-*` parent cids, and descendants reached through
`tasks.parent_cid -> tasks.spawned_cid`. `shadow_review`, `extract_recent`,
`dialectic_miner`, dialectic-validator pending cleanup, and passive skill-use
foreground promotion all consult this same boundary.
SHADOW_REVIEW_PROMPT — inline rubric class-vs-incident, defense against
false positives (false negatives are "cheaper"). Shadow-origin lessons have
a hard body cap, a cheap slug-similarity duplicate gate, and a semantic body
duplicate gate. Strong semantic matches patch/append evidence to the incumbent
lesson, while borderline or protected matches are rejected with the incumbent
slug so the child/curator patches existing memory instead of growing the flat
lessons list.

Manual hook: `shadow_review_run(force=True)`, observability:
`shadow_review_status()`. Beyond the last few passes, the status tool carries a
production-validation rollup (24h / 7d): fire count, outcome mix
(no_window / too_short / spawned / deferred / error), the MATERIALIZED-vs-SKIP
hit rate (read from each evaluator child's captured log tail), shadow-origin
skill writes (`skill_usage.created_by_origin='shadow_review'`), and total
Claude-spawn time spent — read-only, computed from the trail every pass already
leaves (events / tasks / child logs / skill_usage). `shadow_telemetry()` is the
pure aggregator; `snapshot_path` dumps the same numbers as a markdown table for
human review. Children whose ephemeral `/tmp` log has aged out (or are skipped
past the per-call read cap) count as `unknown`, keeping the hit-rate honest.

## Skills system

`~/.claude/skills/<name>/SKILL.md` is the primary write target. The same
skill directory is mirrored to Codex/Antigravity/shared/canonical roots.
Optional subfolders: `references/`, `templates/`, `scripts/`, `assets/`.

- **skill_manage(action, …)** — a single atomic tool. Actions:
  `create | edit | patch | write_file | remove_file | delete | restore`.
  Frontmatter validator: strict YAML, `name` regex + ≤64 chars,
  `description` ≤1024 chars, total ≤100k chars. Generated frontmatter writes
  `name` and `description` as quoted YAML scalars so colon-containing
  descriptions load in Codex and other strict parsers. `write_file/remove_file`
  are restricted to subfolders
  `references|templates|scripts|assets` with path-traversal blocking.
  `patch` revalidates the result before writing. Every successful write
  mirrors the whole skill directory into all configured roots:
  `~/.claude/skills/`, `~/.codex/skills/`,
  `~/.gemini/config/skills/` for Antigravity, existing `~/.agents/skills/`,
  `THREADKEEPER_EXTRA_SKILLS_DIRS`, and `~/.threadkeeper/skills/`.
  The scheduled `skill_updater` repairs drift in the same mirror set and can
  import newer copies that were installed into a non-primary CLI root.

- **skill_record(name, kind, outcome)** — manual bump of
  `use_count/view_count/patch_count`. Under `WRITE_ORIGIN=foreground`,
  `kind='use'` also bumps `foreground_use_count` and recomputes the
  skill's tier (may promote `hypothesis → observed → validated`).
  `outcome='wrong'` bumps `wrong_count` and may demote a tier.

- **skill_usage telemetry (passive)** — `ingest.py` parses `tool_use` blocks
  from jsonl: sees `name=Skill` → `use_count++`, `last_used_at=ts`. This way
  the curator gets real numbers without the agent being required to call
  `skill_record` manually. `foreground_use_count` is gated by the same harvest
  lineage exclusion, so autonomous child self-use cannot promote a skill tier.
  The `skill_watcher` daemon catches external edits to `SKILL.md` (Edit/Write
  directly, not through skill_manage).

- **lesson_usage telemetry (passive reads)** — `lesson_list(k=...)` records a
  `view_count` bump for each displayed lesson row; `lesson_get(slug)` records a
  `use_count` bump for the returned body. The curator computes
  `access_frequency × exp(-days_since_access / tau)` over this table and
  surfaces a ranked `STALE LESSONS (dry-run decay ranking)` section for lessons
  with no recent access and low pull-count. The section is advisory only; it is
  not an automatic deletion path, and foreground/user, pinned, and validated
  lessons are excluded.

- **Curator recovery and destructive telemetry** — destructive curator passes
  receive a pass id and pre-mutation snapshot dir in their environment. When the
  normal `lesson_append`, `lesson_remove`, or `skill_manage` tools run under
  that pass, they emit `events.kind='curator_destructive_action'` rows such as
  `lesson_pruned`, `lesson_patched`, `lesson_consolidated`, and
  `skill_deleted`, with a tombstone path when a deleted body is captured.
  Separately, delete-class tools persist a full pre-image before mutating:
  `lesson_remove` writes the exact `LESSON:BEGIN/END` section plus its
  `lesson_usage` row under `<db dir>/curator/trash/`, and
  `skill_manage(action='delete')` writes the whole skill directory plus its
  `skill_usage` row there. Restore snapshots with `curator_restore(...)`, and
  restore trash artifacts with `lesson_restore(slug=...)` or
  `skill_manage(action='restore', name=...)`. Trash is bounded by
  `THREADKEEPER_CURATOR_TRASH_TTL_DAYS` (default 30); expired artifacts are
  swept on new trash writes. Protected refusal behavior is unchanged:
  user/foreground lessons still require `force`, and pinned skills still refuse
  deletion. `mp_dashboard()` renders destructive action counts by window.

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
- Each row is bi-temporal: `created_at` is ingestion/transaction time;
  `valid_from` / `valid_to` are valid-time bounds for when the claim applies.
  New claims get `valid_from=created_at`; open-ended current claims have
  `valid_to=NULL`.
- `dialectic_supersede(old, new, reason)` — invalidate-don't-delete versioning:
  the old claim moves to `state='superseded'`, keeps its evidence, links
  `superseded_by=<new_id>`, and gets `valid_to=<new.valid_from>`.
- `dialectic_review(..., as_of=...)` — time-scoped review over valid-time
  intervals; `include_validity=True` prints `valid_from` / `valid_to`.
- `dialectic_synthesis(domain, include_history=True)` — optionally renders
  superseded history with validity intervals.
- `dialectic_synthesis(domain)` — text-render `support` vs `contradict`.
- `brief()` renders the `user_model (dialectic)` section gated by **tier**,
  groups by domain. `★` — validated, `·` — observed. Hypothesis-tier
  claims with ≥1 support surface separately under `currently_testing`. When
  any closed validity interval exists, the section header says
  `current as of <date>` to make clear the brief is the current slice.

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
| Gemini legacy  | `~/.gemini/settings.json`   | `tk-thread-nudge.sh` (UserPromptSubmit) |
| Copilot        | `~/.copilot/hooks.json`     | `tk-thread-nudge.sh` (UserPromptSubmit) |
| Claude Desktop | — (no hook mechanism)       | in-`brief()` fallback  |
| Codex          | — (no hook mechanism)       | in-`brief()` fallback  |
| Antigravity CLI (`agy`) | — (hook schema not wired yet) | in-`brief()` fallback  |
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
  Before sending a signal, cleanup re-reads the pid command and skips it if the
  pid no longer belongs to the real `threadkeeper.server` process.
- `memory_guard_status()` — show RSS guard thresholds and current server rows.
- `memory_guard_check(dry_run=True, notify=False)` — one-shot guard pass;
  pass `dry_run=False` to SIGTERM processes over the hard memory limit. The
  guard uses the same pid-identity recheck before hard-kill or idle-retire.
- `memory_guard_reclaim(scope='self')` — immediately unload local
  embedding/caches; with `scope='all'` also queues peer trim requests.
- `agent_memory_cleanup(dry_run=False)` / `tk-agent-status --cleanup-memory` —
  run the unified safe cleanup path: request server cache trims, apply the
  memory guard, and remove orphan MCP servers without killing active spawned
  child agents.

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

**vec0 lifecycle (sync + dimension).** The `notes_vec` mirror is kept in sync
with `notes` *explicitly*, not by trigger: inserts dual-write via
`_vec_upsert_note`, and deletes (only `consolidate()` merges notes today) call
`_vec_delete_note` so a removed note can't strand an orphan KNN row — `notes.id`
is `AUTOINCREMENT` so a stale id is never reclaimed by reuse. As belt-and-braces
for any pre-existing orphan backlog, `_vec0_notes_search` over-fetches from vec0
and trims after the join so a query still yields `k` live hits. The vector width
the `*_vec` tables are created with is `EMBED_DIM` (config-driven —
`THREADKEEPER_EMBED_DIM`, default 384). Because `THREADKEEPER_EMBED_MODEL` is
user-configurable, a model that emits a different width would otherwise make
every vec0 insert raise and silently leave the mirror empty while `_vec_on()`
still reports the fast path live; `_vec_dim_ok` validates the blob width before
insert and logs one actionable warning (set `THREADKEEPER_EMBED_DIM` to the new
width and recreate the `*_vec` tables) instead of swallowing the error.

### Embedding backend

`embeddings.py` is backend-pluggable via `THREADKEEPER_EMBED_BACKEND`. The
default `onnx` runs the model through **fastembed / ONNX Runtime** (no PyTorch,
~700 MB footprint / ~850 MB RSS); `sentence-transformers` is a heavier opt-in
fallback (~1.8 GB). `_encode()` L2-normalizes both backends' output so the dot product
used by vec0 and the legacy path equals cosine. Each row records its producing
backend in `embed_backend` (NULL = legacy). The two backends are not
numerically identical, so after a switch run `tk-migrate-embeddings --all`
(`migrate_embeddings.py`) to recompute stale rows into one consistent space.

## MCP tools (108 total)

Compact grouping by module. Full signatures are in the code; `_mcp.py`
auto-generates JSON-Schema from annotations. Every tool also carries an
explicit read/write **`ToolAnnotations`** hint (see the annotation contract
below).

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
| concepts | 4 | register_concept, list_concepts, expand_concept, concept_manage |
| graph | 3 | link, unlink, neighbors |
| pickup | 3 | pickup_candidates, claim_pickup, release_pickup |
| lessons | 5 | lesson_append, lesson_list, lesson_get, lesson_remove, lesson_restore |
| shadow_review | 2 | shadow_review_run, shadow_review_status |
| candidate_reviewer | 2 | candidate_review_run, candidate_review_status |
| curator | 3 | curator_review, curator_review_status, curator_restore |
| evolve_applier | 8 | evolve_apply, evolve_apply_conflicted_pr, evolve_apply_roadmap_issue, evolve_apply_curator_report, evolve_mark_applied, evolve_mark_roadmap_issue_applied, evolve_mark_curator_report_applied, evolve_apply_status |
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

Each tool is a synchronous Python function; FastMCP wraps it in JSON-Schema
automatically from type annotations. One process — one mcp instance
(`threadkeeper._mcp.mcp`).

### Tool annotation contract (#67)

Tools register through two thin wrappers in `_mcp.py` instead of bare
`@mcp.tool()`, so `tools/list` exposes MCP 2025-06-18 `ToolAnnotations` for
every tool:

- `@read_tool()` → `readOnlyHint=True` — pure queries (`brief`, `context`,
  `search`, `dialog_search`, the status tools, `compost`, …).
- `@write_tool(destructive=…, idempotent=…)` → `readOnlyHint=False` —
  mutations. `lesson_list` and `lesson_get` are non-destructive writes because
  they update lesson access counters. The eleven delete/overwrite/kill tools carry
  `destructiveHint=True`:
  `agent_memory_cleanup`, `concept_manage`, `consolidate`, `core_remove`,
  `curator_restore`, `curator_run`, `lesson_remove`, `memory_guard_check`,
  `mp_cleanup`, `skill_manage`, `unlink`. `idempotentHint=True` marks
  no-op-on-repeat tools
  (`close_thread`, `mark_skill_materialized`, `core_set`, deletes-by-key, …).

This is the static metadata a confirmation/elicitation host reads to decide
which calls warrant a prompt (substrate for #26). The five status tools
(`context`, `spawn_budget_status`, `spawn_status`, `mp_health`,
`agent_status`) additionally return an `outputSchema` + `structuredContent`
(typed models in `tool_schemas.py`, built via `structured_result()`), keeping
the legacy human-readable text block for backward compatibility. The contract
is enforced by `tests/test_tool_annotations.py`.

### MCP resources & prompts (#78)

Tools are only one of MCP's three server primitives. thread-keeper also adopts
the other two for the read/act split they fit naturally:

- **Resources** (`tools/resources.py`, `@mcp.resource`) — *application-controlled,
  read-only* memory snapshots at stable URIs: `memory://brief`,
  `memory://context`, `memory://dashboard`, `memory://agent-status`. Each is
  backed by the same render function as the matching tool (`render_brief`,
  `render_context`, `mp_dashboard`, `agent_status`), so a host can pull memory as
  attachable / `@`-mentionable context without the agent *remembering* to call a
  tool — the mechanical channel that hookless CLIs lacked. The brief resource
  renders `lean=True` and agent-status uses `refresh=False`, so an automatic host
  pull is **side-effect-free** (no `*_hint_shown` events, no process re-scan).
  URIs are static: resource *templates* (`{param}`) are still unevenly supported
  across hosts, so parameterized URIs are a later, host-gated step.
- **Prompts** (`tools/prompts.py`, `@mcp.prompt`) — *user-controlled,
  parameterized* templates for the curation / audit / review flows:
  `review_recent_threads`, `run_library_curation`, `audit_threadkeeper`. Claude
  Code surfaces them as `/mcp__thread-keeper__<name>` slash commands; each returns
  one instruction message that drives the existing read/act tools (it does not act
  on its own).

Both are **additive**: FastMCP advertises the `resources` / `prompts`
capabilities, which only changes what a capability-aware host *sees* — never the
tool surface. A host that uses neither falls back to the hook-injected brief and
the `brief()` / `context()` tools, with identical content. Resource/prompt functions register on
their own managers, so they never enter the tool registry (pinned by
`tests/test_mcp_resources_prompts.py`, which also covers list/read, prompt
rendering, capability advertisement, and the tool-only fallback).

### MCP elicitation (#26)

Elicitation is a **client feature**: the server can ask the host to collect
structured user input while a tool call is in progress. thread-keeper keeps this
behind a thin helper in `elicitation.py`:

- `supports_form_elicitation(ctx)` probes request metadata first
  (`io.modelcontextprotocol/clientCapabilities`), then the SDK's
  initialize-time session capabilities. If no form-mode elicitation capability
  is present, the caller uses its existing fallback behavior.
- `elicit_confirm_reject(ctx, message)` sends `elicitation/create` through
  `Context.elicit()` only after that probe passes. Decline, cancel, invalid, and
  transport-error paths are non-mutating.
- Schemas stay **flat** and spec-valid: root object only, primitive fields only.
  The shared `ConfirmRejectForm` has one string enum field,
  `decision in {"confirm", "reject"}`; no nested objects, arrays of objects, or
  sensitive data collection.

The first wired flow is `dialectic_supersede`. On supported hosts, replacing a
user-model claim prompts with a confirm/reject form before writing the new claim
and marking the old one superseded. On unsupported hosts (Codex, hookless MCP
clients, older Claude clients), behavior is unchanged: the tool applies
immediately and the existing brief/hook nudge ecosystem remains the UX fallback.

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
├── test_spawn_watchdog.py     wall-clock kill + single-flight release (#80)
├── test_search_proxy.py       request/response signal roundtrip
├── test_dialectic.py          smoothed-ratio confidence
├── test_skills.py             skill_manage frontmatter validation + curator
├── test_shadow_review.py      cursor advance + min_chars gate + idempotency
├── test_process_health.py     orphan detection with/without heartbeat
└── …
```

Run: `.venv/bin/python -m pytest tests/ -q`. Currently 869 tests (1 skipped),
all green. Smoke parametrization automatically picks up any new tools without
having to add tests.

## Memory-quality evaluation (issue #71)

Two read-only harnesses measure the memory layer, not the code:

- `scripts/tk_verify_ingest.py` — *write/ingest* side: did we capture rows from
  every CLI? (slot coverage, PASS/PARTIAL/FAIL; issue #1).
- `scripts/memory_eval/run.py` — *read/retrieval* side: when we retrieve, do we
  recall the right fact and **refuse** to answer about things that never
  happened? Modeled on LongMemEval (ICLR 2025) + mem0's 2026
  tokens-per-retrieval cost axis.

The eval harness is deliberately thin and treats the retrieval surface as a
black box: each ground-truth question carries a `system`
(`search` → notes, `dialog_search` → ingested transcripts, `brief` → the
auto-injected context) and a `query`; `retrieve()` calls the *real* tool
function and the judge reads its verbatim output, so tokens-per-retrieval is
measured on exactly what an agent would receive. The five LongMemEval axes map
onto thread-keeper as:

| Axis | What it probes here |
|---|---|
| information_extraction | single-fact recall from one message/note |
| multi_session_reasoning | union of top-k spans facts from ≥2 sessions |
| temporal_reasoning | retrieval surfaces the time-relevant evidence (before/after, latest) |
| knowledge_update | the *current* value wins over a superseded one in the corpus |
| abstention | never-happened question → no fabricated `trap_substring` leaks into context |

The default **lexical** judge is a deterministic substring scorer (gold recall;
abstention = no trap surfaced) — offline, no API key, no embeddings, so it runs
in CI and as a golden baseline (the bundled `ground_truth.json` demo corpus
scores 100% under a faithful retrieval; a regression in `search()`/
`dialog_search()` drops it). An optional `--judge llm` grades answer
*reasoning* (true temporal ordering, knowledge-update correctness) via the
Anthropic Messages API over `urllib` — no SDK dependency — and is an
optimization target for lesson-decay tuning (#27) and bi-temporal (#28) work.
`--db snapshot.sqlite` evaluates a real production snapshot, copied to a temp
file first so the original is never opened for writing. Backend (`fts` vs
`semantic`) is auto-detected and reported. Smoke-tested in
`tests/test_memory_eval.py` (subprocess, to keep import-time env setup off the
shared in-process package state).

## Evaluating the learning loop

`verify_ingest.py` measures the *ingest plumbing* (did rows land, does shadow
span >1 adapter). `threadkeeper/eval/` measures the orthogonal axis —
**decision quality**: when `shadow_review` calls materialize-vs-skip and
`candidate_reviewer` calls accept-vs-reject, are those calls right? There was
no labeled set and no precision/recall before this harness (issue #72); the
shadow rubric hard-codes a class-vs-incident decision but nothing scored how
often it's correct.

The harness (`python -m threadkeeper.eval`, pure verdict logic in
`threadkeeper/eval/harness.py`) has three parts, surfaced with the same
`PASS/PARTIAL/FAIL` verdict shape as `verify_ingest`:

- **Fixtures** (`threadkeeper/eval/fixtures/*.json`) — small, hand-labeled,
  fully-synthetic sets: `shadow.json` (dialog windows + expected
  materialize/skip), `candidates.json` (candidate snippets + expected
  accept/reject), and `skill_quality.json` (skill bodies + a human high/low
  label). `test_eval_harness.py` asserts they carry no secrets/private paths.
- **Two judges** (mirroring the `verify_ingest` / memory-recall split between an
  offline CI-safe path and an LLM path):
  - `rubric` (default, deterministic, offline) — a *signal-vote* classifier
    **coupled to the live daemon prompt**. Each fixture item carries the human
    rubric `signals` it contains (`stated_policy`, `false_positive`, …); each
    signal maps to an `anchor` phrase, and a signal only votes if its anchor is
    present **in its decision's section of the current prompt**
    (`SHADOW_SECTIONS` / `CANDIDATE_SECTIONS`, with graceful fallback to
    whole-prompt presence if a section header was reworded). So editing a rubric
    — dropping a materialize/reject criterion — deactivates the signals anchored
    to it and **moves the precision/recall**, deterministically and offline.
    Calibrated so the golden fixtures classify cleanly, the offline judge is a
    cheap regression guard: a rubric edit that silently drops a criterion shows
    up as an F1 drop in CI.
  - `llm` (opt-in, needs `ANTHROPIC_API_KEY`) — replays the *actual*
    `SHADOW_REVIEW_PROMPT` / `CANDIDATE_REVIEW_PROMPT` (plus the same
    `<observed_dialog>` data fence the daemons use) over each item via the
    Anthropic Messages API over `urllib` (no SDK), and parses the daemon's own
    verdict contract. This is the high-fidelity decision-quality measurement; a
    prompt edit obviously moves it because the model reads the edited prompt.
- **Calibration** — the skill-quality axis reports judge↔human **agreement**
  (raw accuracy + Cohen's kappa). Per the evidently.ai LLM-as-a-judge guidance,
  agreement against a fixed human-labeled set is what makes a drifting judge
  (offline heuristic or LLM) visible before its scores are trusted.

The CLI exits non-zero only when the harness itself is broken (no fixtures /
nothing computable), never on model quality — quality is a number to track and
optimize against (e.g. the ROADMAP's extract-precision and "do we need tiers"
open questions), not a gate. `--fixtures-dir` scores a custom labeled set.
Smoke-tested in `tests/test_eval_harness.py` (pure-function units +
rubric-sensitivity + a subprocess end-to-end run).
## Env knobs (config.py)

`Settings` keeps pydantic's permissive `extra="ignore"` behavior, but startup
and hot-config reload log a one-line warning for unknown `THREADKEEPER_*` keys
present in the process environment. Spawn routing is similarly fail-soft:
unsupported CLI overrides still fall through to the next priority, and
`spawn_status()` shows the warning beside the resolution table.

| Knob | Default | Purpose |
|---|---|---|
| `THREADKEEPER_DB` | `~/.threadkeeper/db.sqlite` | sqlite file |
| `THREADKEEPER_RETENTION_INTERVAL_S` | 0 | retention/compaction daemon tick; 0 disables |
| `THREADKEEPER_DIALOG_RETENTION_DAYS` | 0 | prune old dialog rows plus `dialog_fts` / `dialog_vec` mirrors; 0 keeps forever |
| `THREADKEEPER_TASK_RETENTION_DAYS` | 30 | prune completed `tasks` older than this many days; 0 keeps forever |
| `THREADKEEPER_SIGNAL_RETENTION_DAYS` | 0 | prune handled old `signals` and aged search proxy messages; 0 keeps forever |
| `THREADKEEPER_EVENTS_RETENTION_DAYS` | 0 | prune old `events` during retention passes; 0 keeps forever |
| `THREADKEEPER_PROBE_RESULT_RETENTION_DAYS` | 0 | prune old `probe_results` and refresh reliability aggregates; 0 keeps forever |
| `THREADKEEPER_RETENTION_WAL_CHECKPOINT` | false | run `PRAGMA wal_checkpoint(TRUNCATE)` during retention passes |
| `THREADKEEPER_RETENTION_VACUUM_AFTER_ROWS` | 0 | run `VACUUM` after at least this many deleted rows; 0 disables |
| `THREADKEEPER_MEMORY_EGRESS` | `all` | personal-class memory egress scope: `all` / `same-vendor` / `work-only` (see Spawn → Cross-provider memory egress) |
| `THREADKEEPER_EMBED_MODEL` | paraphrase-multilingual-MiniLM-L12-v2 | 384-dim, RU+EN |
| `THREADKEEPER_EMBED_BACKEND` | `onnx` | `onnx` (fastembed, no PyTorch) or `sentence-transformers` (fallback) |
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects` | jsonl for ingest |
| `CLAUDE_SKILLS_DIR` | `~/.claude/skills` | skills root |
| `THREADKEEPER_EXTRA_SKILLS_DIRS` | unset | os.pathsep-separated extra skills roots to mirror into |
| `THREADKEEPER_INGEST_INTERVAL_S` | 3 | daemon ingest tick |
| `THREADKEEPER_INGEST_CAP` | 50 | max msgs per call |
| `THREADKEEPER_SKILL_WATCH_INTERVAL_S` | 5 | skill_watcher tick |
| `THREADKEEPER_SKILL_UPDATE_INTERVAL_S` | 302400 | installed-skill update/mirror interval; 0 disables |
| `THREADKEEPER_SKILL_UPDATE_TIMEOUT_S` | 300 | max seconds for upstream skill source downloads |
| `THREADKEEPER_SKILL_UPDATE_SOURCES` | `openai/skills@main:skills/.curated` | comma-separated GitHub source roots (`owner/repo@ref:path`) for inferred upstream updates |
| `THREADKEEPER_SKILL_UPDATE_INFER_SOURCES` | true | infer source by skill name from configured source roots |
| `THREADKEEPER_SKILL_UPDATE_ALLOW_UNTRACKED_OVERWRITE` | false | allow overwriting inferred untracked local skill copies; default false only adopts exact matches |
| `THREADKEEPER_AUTO_REVIEW` | off | enable auto-review on close_thread |
| `THREADKEEPER_MEMORY_NUDGE_INTERVAL` | 10 | events between memory_save nudges |
| `THREADKEEPER_SKILL_NUDGE_INTERVAL` | 10 | events between skill_hint nudges |
| `THREADKEEPER_AUTO_UPDATE_INTERVAL_S` | 86400 | MCP self-update check interval; 0 disables |
| `THREADKEEPER_AUTO_UPDATE_RESTART` | true | exit MCP process after an update passes setup/import smoke checks |
| `THREADKEEPER_AUTO_UPDATE_TIMEOUT_S` | 600 | max seconds for git/pip update commands |
| `THREADKEEPER_AUTO_UPDATE_SETUP` | `check` | post-update setup mode: `check` dry-runs and logs pending CLI config rewrites; `apply` writes setup config; `skip` disables setup |
| `THREADKEEPER_AUTO_UPDATE_VERIFY_PROVENANCE` | true | require PyPI Integrity API provenance before packaged `pip` self-upgrades |
| `THREADKEEPER_AUTO_UPDATE_PYPI_BASE_URL` | `https://pypi.org` | PyPI base URL used for JSON metadata and Integrity API checks |
| `THREADKEEPER_AUTO_UPDATE_EXPECTED_PUBLISHER_REPOSITORY` | `po4erk91/thread-keeper` | expected GitHub Trusted Publisher repository for packaged self-upgrades |
| `THREADKEEPER_AUTO_UPDATE_EXPECTED_PUBLISHER_WORKFLOW` | `publish.yml` | expected GitHub Actions workflow filename in PyPI provenance |
| `THREADKEEPER_AUTO_UPDATE_EXPECTED_PUBLISHER_ENVIRONMENT` | `pypi` | expected GitHub Actions environment in PyPI provenance |
| `THREADKEEPER_SPAWN_BUDGET_MB` | 3072 | combined child RSS cap; 0 disables |
| `THREADKEEPER_SPAWN_ESTIMATE_SLIM_MB` | 500 | initial slim child RSS guess |
| `THREADKEEPER_SPAWN_ESTIMATE_FULL_MB` | 1500 | initial full child RSS guess |
| `THREADKEEPER_SPAWN_BUDGET_POLL_S` | 10 | budget daemon tick; 0 disables |
| `THREADKEEPER_SPAWN_VISIBLE_TTL_S` | 3600 | reap a visible (pid=0) row whose cid never resolves to a live process; 0 disables |
| `THREADKEEPER_SPAWN_MAX_RUNTIME_S` | 3600 | wall-clock lifetime cap (s) for a spawned child; over-cap live children are SIGTERM→SIGKILL'd and closed with `return_code` 124; 0 disables |
| `THREADKEEPER_SPAWN_KILL_GRACE_S` | 10 | grace between SIGTERM and SIGKILL when the watchdog kills a timed-out child |
| `THREADKEEPER_SPAWN_TIMEOUT_RETRY_LIMIT` | 3 | immediate continuation retries after a watchdog kill; 0 disables |
| `THREADKEEPER_SPAWN_TIMEOUT_RETRY_DELAY_S` | 0 | delay before a watchdog continuation retry |
| `THREADKEEPER_MENUBAR_AUTO_LAUNCH` | true | macOS: auto install/launch agent-status menu-bar app on MCP startup |
| `THREADKEEPER_MENUBAR_RESTART_RSS_MB` | 1024 | macOS widget self-restart RSS threshold; 0 disables |
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
| `THREADKEEPER_CURATOR_INTERVAL_S` | 0 | curator daemon tick; 604800 = 7d recommended |
| `THREADKEEPER_CURATOR_MIN_LESSONS` | 3 | min lessons before curator engages |
| `THREADKEEPER_CURATOR_DESTRUCTIVE` | `1` | curator child writes its REPORT then applies PATCH/PRUNE/CONSOLIDATE directly; set `0` for advisory-only |
| `THREADKEEPER_CURATOR_TRASH_TTL_DAYS` | 30 | days to retain `lesson_remove` / `skill_manage(delete)` recovery artifacts under `<db dir>/curator/trash` |
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

For high-stakes writes, nudges are now complemented by MCP elicitation when the
host advertises it. `dialectic_supersede` is the first protected flow: a
supported host shows a structured confirm/reject dialog; unsupported hosts keep
the old text/tool path so Codex, Claude Desktop, Antigravity, and generic MCP
clients do not regress.

## What is NOT done

- No authentication / access control (see ROADMAP.md).
- No federation: one database file, one machine.
- Some legacy paths are still Claude-Code-specific: ppid walk, jsonl parser,
  settings.json hooks, ~/.claude.json as MCP-config template. Antigravity CLI
  (`agy`) is wired for MCP/instructions/skills/spawn, but its sqlite/protobuf
  conversation history and hook schema are not parsed/wired yet.
- Extraction heuristics are simple regexes; no ML quality classifier.
- MCP-native `sampling/createMessage` (a native review fork without
  pay-per-use tokens) is not yet implemented in Claude Code
  (anthropics/claude-code#1785). spawn-subprocess is the fallback, slim-config
  brings the cost down to acceptable.
