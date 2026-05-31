# Changelog

All notable changes to this project are documented in this file.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/);
version bumps follow semver per the policy in
[CONTRIBUTING.md → Releases](CONTRIBUTING.md#releases).

## [Unreleased]

### Changed

- `tk-task-gate.sh` now covers the opus-4.8 native parallelism tools
  (`Agent` / `Workflow`), not only the legacy `Task` tool — with an
  *inverted* heuristic, because the right default flipped. The gate keyed
  only on `Task` (`matcher: ^Task$`), which opus 4.8 replaced with native
  `Agent`/`Workflow`, so it silently no-op'd on every native call. Now:
  - **`Task`** (still present on non-opus-4.8 models): unchanged — blocks
    parallel-fanout work lacking a synthesis cue, pushes to `spawn()`
    (`deny` default).
  - **`Agent` / `Workflow`**: native is the right default for ephemeral
    in-turn fan-out, so the gate stays out of the way there. Inverted —
    advisory `warn` only (never hard-blocks) when the prompt carries
    *persistence* signals (cross-session, inter-agent channels
    broadcast/whisper/inbox/wait, must-outlive-the-session, daemon) — work
    that belongs to `spawn()` but went to native.
  Matcher `^Task$` → `^(Task|Agent|Workflow)$` (`_setup.py` updated so fresh
  installs get it). No functional conflict between spawn and native existed —
  spawn's child-linking already skips `agent-`-prefixed native-subagent
  transcripts; this realigns the advisory. `core_memory.spawn_pattern` + the
  `spawn-vs-task-decision-tree` lesson rewritten to choose on SCOPE
  (cross-session / channels / daemon → spawn; in-turn fan-out → native)
  rather than the obsolete N≥2/duration rule.

### Fixed

- `spawn_status` carried an accidental duplicate `@mcp.tool()` decorator
  (copy-paste), so the second decorator registered the already-wrapped
  `FunctionTool` instead of the plain function. Removed the extra decorator;
  audited the whole package and confirmed it was the only double-decoration.

- `tasks.return_code` was NULL for **every** ended task (measured 0 of 944),
  so the dashboard could never measure a spawn→outcome conversion. Root cause
  was deeper than previously documented (not just slim children racing the
  poll): the `tasks` table outlives the MCP process that launched a child, so
  the cross-session reaper is almost never the spawning parent — its
  `os.waitpid` raises `ChildProcessError` and the exit code is lost. Fixed by
  running headless children under a thin stdlib recorder
  (`threadkeeper/_spawn_wrap.py`) that writes `return_code` from inside the
  child's own lifecycle, independent of any waitpid race or which session is
  alive. The recorder forwards `SIGTERM`/`SIGINT`/`SIGHUP` so `task_kill`
  still terminates the real child; the visible/Terminal path persists the
  code via a `--record` shell line. The parent reaper
  (`_reap_finished_tasks`) stays as a fallback. Run by file path (not
  `python -m`) so it adds zero package-init cost per spawn.

- extract_recent self-pollution: also exclude **curator** and
  **candidate-reviewer** daemon children by prompt opener. The v0.8.1
  `tasks.spawned_cid` exclusion catches `spawn()` children, but curator and
  candidate-reviewer are *daemons* whose sessions link into `tasks`
  unreliably (cid seen as `parent_cid` more often than `spawned_cid`), so
  ~49 of 126 historical rejects — curator/candidate prompt fragments
  re-harvested as candidates — slipped past it. Their openers are fixed, so
  they're now in `_INTERNAL_PROMPT_PREFIXES` ("You are an autonomous
  CURATOR", "You are a CANDIDATE REVIEWER") alongside shadow/probe/evolve —
  caught with no tasks-row dependency. Together with the spawned_cid filter
  this removes essentially all extract self-noise (the cause of the 1%
  candidate accept-rate). Same fix benefits `shadow_review._collect_window`,
  which shares the constant.

## v0.8.1 — 2026-05-30

### Added

- **Thread-janitor daemon** (`threadkeeper/thread_janitor.py`) + reversible
  close. The skill-harvest path is event-driven on `close_thread()`, but the
  user never closes threads and the agent rarely does, so it almost never
  ran (2 auto-review spawns ever; 5 skills from 115 closes; 32 threads left
  open, some idle 12d). The janitor closes threads idle past
  `THREAD_IDLE_CLOSE_DAYS` (default 1) through the normal `close_thread()`
  path, so the auto-review hook fires and the brief's skill_hint surfaces
  the rest. Aggressive auto-close is made safe by a reversed invariant:
  **a `note()` on a closed thread now revives it to active** (was terminal —
  only `idle` revived). Returning to a topic reopens it; nothing is lost,
  just parked. Knobs `THREADKEEPER_THREAD_JANITOR_INTERVAL_S` (default 0 =
  off; recommend 86400) and `THREADKEEPER_THREAD_IDLE_CLOSE_DAYS` (default
  1). Foreground-only, idempotent, records a `janitor_pass` event (visible
  in `mp_dashboard`).
- `mp_dashboard(window_days=7)` — aggregate telemetry rollup in one call.
  The point-view tools (`mp_health`, `spawn_budget_status`,
  `shadow_review_status`) each show one slice; nothing showed the whole
  system. The dashboard reports **stores** (threads by state, note/dialog/
  distill/concept counts, skills + dialectic claims by tier, extract-
  candidate + evolve queues, probe/task counts), **loops** (per-daemon
  fire counts over the window vs 30 days + last-fire age, from the
  `events.kind='*_pass'` markers), and **outcomes** (skills materialized,
  tier promotions, candidate accept-vs-reject rate). Read-only; never
  spawns or mutates; degrades to zeros on partial schemas. Surfaces
  "loop fires constantly but produces nothing" and "queue backing up"
  signals the per-loop tools can't.

## v0.8.0 — 2026-05-30

### Added

- Autonomous **evolve reviewer** daemon (`threadkeeper/evolve_daemon.py`) —
  triages the format-evolution suggestion queue that `evolve_format()` writes
  to (the audit found 5 filed, 0 ever actioned: a write-only graveyard). A
  weekly context-free child reviews pending suggestions and, per item, calls
  the new `evolve_decide(id, promote|dismiss)` tool: PROMOTE keeps a live one
  (brief now surfaces promoted suggestions first, marked ★), DISMISS drops
  duplicates/stale/superseded ones. The child NEVER applies a suggestion —
  applying edits format/code, a foreground/human action; the reviewer only
  keeps the queue honest. `evolve` table gains `status`/`reviewed_at`/
  `review_reason`. Knobs `THREADKEEPER_EVOLVE_REVIEW_INTERVAL_S` (default 0 =
  off; recommend 604800) and `EVOLVE_REVIEW_MIN` (default 2). Single-flight,
  foreground-only, same daemon shape as probe/curator.
- Curator now also reviews the **concepts** store (was: lessons + skills
  only). Each weekly curator pass appends a `## CONCEPTS` inventory —
  every concept with its confidence band and days since last
  corroboration, oldest-first — and the curator rubric gained a concepts
  section: CONSOLIDATE near-duplicate concepts (the store is thin and
  prone to restating the same idea), PRUNE `conf=low AND last_evidence
  >30d` concepts as false positives (registered once, never corroborated —
  the concept equivalent of an unused background_review skill), and flag
  aging medium+ concepts for confidence review. Advisory like the rest of
  the curator. Concepts enrich the review but don't lower the lesson
  threshold that gates a pass. Closes the audit gap where the concepts
  store (1 entry, never validated) had no autonomous maintainer.
- Judge panel (`threadkeeper/tools/panel.py`, `convene_panel`) — fills the
  distill/dialectic promotion quorum with SPAWNED agents that vote
  independently, instead of waiting for a second human or lowering
  thresholds. Single-CLI installs never reached `vote_sum >= 2` (distill) or
  the dialectic tier thresholds, because there's one human and the system's
  own review-forks are discounted to 0.5 so they can't self-promote.
  `convene_panel(target_kind, target_id)` spawns N role-diverse children;
  each evaluates the target and casts one vote (and may vote against). The
  honesty guard is structural: a panel earns the full-weight `panel_vote`
  origin ONLY when adversarial (a skeptic is present, `PANEL_REQUIRE_SKEPTIC`);
  otherwise it runs discounted as `background_review`, so a rubber-stamp
  panel can't promote anything. The spawner grants the origin for the whole
  panel — no child self-elevates. Distill votes (raw per-cid sum) work by
  headcount; dialectic evidence (origin-discounted) is lifted to full weight
  by the new `panel_vote` entry in `EVIDENCE_DISCOUNT`. Knobs:
  `THREADKEEPER_PANEL_SIZE` (3), `PANEL_ROLES` (skeptic,critic,generator),
  `PANEL_REQUIRE_SKEPTIC` (on), `PANEL_VOTE_WEIGHT` (1.0), `PANEL_MODEL`,
  `PANEL_EFFORT`.

### Fixed

- `extract_recent` no longer re-harvests thread-keeper's own spawned-child
  sessions. Curator / panel / research children open with arbitrary task
  framing ("You are auditing…", "You are analyzing whether…", "Use the
  Write tool to…") that the prompt-prefix noise list didn't match, so their
  system prompts re-entered the dialog and became extract candidates — the
  dominant noise source (66 of 107 historical decisions, ~5% accept rate).
  extract_recent now also excludes any session whose cid is a
  `tasks.spawned_cid`, reusing the same provenance link as
  `ingest._is_spawned_child_session`. Kills the whole self-pollution class
  regardless of prompt wording.
- `search()` / `brief(query=...)` / `dialog_search` no longer choke on
  everyday punctuation. A query containing an FTS5 operator char
  (`-`, `?`, `/`, `(`, `:`, `*`) previously raised `fts_error` from `search()`
  and silently returned nothing from the brief/dialog FTS fallbacks (the
  no-embeddings / slim-child path, where FTS5 MATCH is the search backend).
  Queries are now sanitized via `helpers._fts_query` — each whitespace term
  is quoted as a phrase, so operators become literal while the tokenizer
  still splits and matches; pure-punctuation queries return `no_matches`
  instead of erroring. Found via end-to-end flow verification; regression
  test in `tests/test_search_fts_punctuation.py`.
- Spawned tasks now record their real `return_code` and get reaped. A new
  `_reap_finished_tasks` does a non-blocking `waitpid` on every tracked
  headless child, persisting both `ended_at` and the exit code (negative for
  signal-kills, e.g. `-9` for SIGKILL). Previously the `Popen` handle was
  dropped at spawn time and nothing ever waited on it, so `return_code`
  stayed NULL for every task and finished children lingered as "running"
  zombie rows. `tasks()` now shows `rc=<n>` for completed tasks.
- Passive skill-use detection now feeds tier promotion. The ingest scanner
  bumped only `use_count` and never `foreground_use_count`, and never
  recomputed tier — so every skill was frozen at `hypothesis` regardless of
  real usage. Both scan sites now route through a shared `_record_skill_use`
  that bumps `foreground_use_count` and recomputes the tier ladder
  (hypothesis → observed → validated) for genuine foreground sessions, while
  spawned review-fork child sessions (matched via `tasks.spawned_cid`) bump
  only the raw `use_count` — so the system observing its own behavior can't
  self-promote a skill (mirroring the dialectic evidence discount).

### Added

- `scripts/backfill_skill_tiers.py` — one-shot, idempotent backfill that
  recomputes `foreground_use_count` + tier for every skill from a transcript
  re-scan, iterating to a tier fixpoint. Dry-run by default; `--apply` writes.
- Probe daemon (`threadkeeper/probe_daemon.py`) — drives the self-test probe
  loop that was defined but never run, so `probe_results` / `reliability` were
  empty and the brief showed every weak-spot as `never_tested`. Each tick
  spawns one CONTEXT-FREE child to attempt a due probe (an isolated child is a
  clean capability measurement, uncontaminated by the parent conversation);
  the child writes only its raw answer and the PARENT grades it mechanically
  via `_grade_probe` — the child never sees the answer key, so it can't game
  the result. Only objective graders (regex/exact with a pattern) are driven;
  `manual` probes stay on the manual `run_probe` loop. Two-phase non-blocking
  (grade last tick's answer, then spawn the next), machine-wide single-flight,
  per-category cooldown. New knobs `THREADKEEPER_PROBE_INTERVAL_S` (default 0 =
  off; recommended 86400) and `THREADKEEPER_PROBE_COOLDOWN_S` (default 7d).

## v0.7.0 — 2026-05-27

### Changed

- **Default embedding backend is now fastembed / ONNX Runtime** instead of
  sentence-transformers / PyTorch. Same model
  (`paraphrase-multilingual-MiniLM-L12-v2`, 384-dim) and `vec0` schema, but no
  PyTorch: a model-loaded process drops from ~1.8 GB to ~670 MB physical
  footprint, and the install sheds ~650 MB (torch + transformers +
  scikit-learn + scipy).
- `THREADKEEPER_EMBED_BACKEND` selects the runtime (`onnx` default;
  `sentence-transformers` opt-in). The `semantic` extra now installs fastembed;
  the new `semantic-st` extra installs the legacy PyTorch backend.

### Added

- `tk-migrate-embeddings` — batched, resumable, idempotent CLI that recomputes
  stored embeddings with the active backend after a switch (both the BLOB
  column and the `vec0` mirror).
- `embed_backend` column on `notes` / `dialog_messages` recording which backend
  produced each stored vector (NULL = legacy).

### Fixed

- `config` is cheap to import again: backend availability is probed via
  `importlib.util.find_spec` rather than importing the heavy library at module
  load, so the embedding runtime (and its thread pools) load lazily on first use.

### Internal

- CI runs `pytest --forked` so each test is process-isolated. The suite's
  per-test package re-import otherwise accumulates native ONNX / tokenizer
  thread pools that can deadlock sqlite connection finalize.

## v0.6.2 — 2026-05-26

### Fixed

- Memory guard aggregate pressure handling is now single-coordinator across
  live MCP server processes. This prevents every open Codex/Claude session from
  independently emitting the same aggregate warn, queuing duplicate trim
  requests, and attempting the same idle-retirement plan.
- Aggregate warn/reclaim side effects now respect the guard cooldown globally,
  reducing repeated desktop warnings and repeated self-trim sweeps while total
  `threadkeeper.server` RSS remains above the aggregate threshold.

## v0.6.1 — 2026-05-26

### Fixed

- Aggregate memory retirement no longer terminates `threadkeeper.server`
  processes whose parent process is still alive by default. This prevents a
  newly-starting or idle-but-live MCP server with `heartbeat_age_s=None` from
  being killed mid-tool-call, which surfaced in clients as `Transport closed`
  on `brief()` / `context()`. Live-parent retirement now requires the explicit
  opt-in `THREADKEEPER_MEMORY_GUARD_RETIRE_LIVE=1`.

## v0.6.0 — 2026-05-26

### Added

- Thread-keeper server memory optimization:
  - `memory_guard` now watches aggregate RSS across all
    `threadkeeper.server` processes, not just per-process thresholds.
  - `memory_guard_reclaim(scope='self'|'all')` unloads the local embedding
    model, clears Python/import/line caches, asks PyTorch CUDA/MPS caches to
    empty when loaded, runs GC, and requests allocator pressure relief on
    supported platforms.
  - Cross-process `resource_controls` mailbox lets one MCP server ask peer
    servers to trim models/caches on their next guard tick.
  - Under aggregate memory pressure, stale non-self MCP servers can be
    retired toward `THREADKEEPER_MEMORY_GUARD_TARGET_SERVERS` instead of
    waiting for each individual process to hit the hard RSS limit.
- Shadow-review single-flight: shadow review now detects already-running
  shadow observer child tasks and skips spawning another evaluator until the
  current one ends.
- Spawned children are marked with `THREADKEEPER_SPAWNED_CHILD=1`; autonomous
  background daemons are gated to foreground parent processes so child agents
  cannot recursively start their own shadow/extract/curator/reviewer loops.
- New memory guard configuration:
  `THREADKEEPER_MEMORY_GUARD_AGG_WARN_MB`,
  `THREADKEEPER_MEMORY_GUARD_AGG_KILL_MB`,
  `THREADKEEPER_MEMORY_GUARD_RECLAIM_MB`,
  `THREADKEEPER_MEMORY_GUARD_TARGET_SERVERS`, and
  `THREADKEEPER_MEMORY_GUARD_RETIRE_IDLE_S`.
- Post-test release tagging workflow: successful `tests` runs on `main`
  now create the annotated `vX.Y.Z` tag from `pyproject.toml` and dispatch
  `publish.yml` on that tag ref. Manual tag publishing remains supported.
- Two hook-based safety nets for the thread lifecycle, wired by
  `thread-keeper-setup` (see [ARCHITECTURE.md → Hooks](docs/ARCHITECTURE.md)):
  - `tk-thread-nudge.sh` (UserPromptSubmit) — once per session, reminds you
    to `open_thread()` if none was opened yet, via non-blocking
    `additionalContext`. Backstops the "new substantive topic → open_thread"
    rule that previously nothing watched for.
  - `tk-session-end.sh` (Stop) — once per session, reminds you to
    `close_thread()` / `session_end()` when a thread was opened this session.
    Advisory `systemMessage`; throttled because `Stop` fires every turn.
  - `tk-status.sh` now writes a per-session `state/sess-<id>.opened` marker on
    `open_thread`, which both nudges read to suppress themselves once a thread
    is being tracked.

### Fixed

- Read-only MCP tool calls now refresh session heartbeat, preventing active
  sessions from looking idle to process-retirement heuristics.
- `thread-keeper-setup` now version-controls and installs `tk-task-gate.sh`
  (the spawn-vs-Task `PreToolUse` gate); it had been deployed out-of-band and
  was missing from the repo, so fresh installs lacked it.
- Synced the live `tk-brief.sh` `live=`/`peers=` counter fix back into the
  repo source — the deployed copy had drifted ahead of the tracked one.
- Memory/skill nudge counters no longer count bookkeeping events
  (`thread_hint_shown`, `shadow_review_pass`) as agent turns
  (`nudges._NONCOUNTING_KINDS`). The new open-thread nudge's
  `thread_hint_shown` marker was inflating the counter by one per session
  (firing nudges a turn early) and made `test_skill_nudge_soft_at_threshold`
  flaky against the shadow-review daemon's cursor mark.

## v0.5.3 — 2026-05-22

### Changed

- Skill materialization now syncs to every known/configured skills root,
  not only the primary Claude skills directory. `skill_manage` mirrors
  into Claude, Codex, existing `~/.agents/skills/`, extra roots from
  `THREADKEEPER_EXTRA_SKILLS_DIRS`, and the canonical
  `~/.threadkeeper/skills/` mirror. `mark_skill_materialized(skill_path=...)`
  now also imports an externally-created skill directory and mirrors it
  immediately, so agents no longer have to manually copy a new skill across
  CLI homes after a build.

## v0.5.2 — 2026-05-20

### Fixed

- `publish.yml` step names that contain inline colons (e.g.
  `Create GitHub Release (fallback: auto-generated notes)`) are now
  quoted as YAML scalars. The unquoted form crashed YAML parsing at
  load time, which is why v0.5.1 left a tag on GitHub but no PyPI
  upload and no Release entry — the tag-triggered workflow never even
  started running. v0.5.2 ships the same content v0.5.1 was supposed
  to.

## v0.5.1 — 2026-05-20 (broken release)

Tag exists but no artifacts on PyPI or GitHub Releases. The
publish.yml change in this commit had a YAML syntax error (unquoted
colon inside a step `name:`) that prevented the workflow from
loading. Superseded by v0.5.2.

### CI (shipped in v0.5.2)

- `publish.yml` now also creates a GitHub Release entry on tag push
  (after the PyPI upload completes). Notes are pulled from the
  matching `## vX.Y.Z` section of `CHANGELOG.md`; falls back to
  `--generate-notes` if the section is missing. dist artifacts are
  attached to the release for direct download. Closes the gap where
  v0.4.1 had a tag but no Release entry; future tags self-document.

## v0.5.0 — 2026-05-20

### Features

- **Dialectic tier promotion + source-weighted evidence**
  ([`b30f018`](https://github.com/po4erk91/thread-keeper/commit/b30f018)).
  Each claim in the dialectic user model now carries a discrete
  `tier ∈ {hypothesis, observed, validated, disputed}` on top of the
  continuous confidence band. Tier is the action-gating signal:
  validated = agent defaults to it (★ in brief); observed = agent
  references and may mention (·); hypothesis = active probe surfaced
  in a new `currently_testing` brief block.
  Evidence rows are stored with `weight = base_weight × discount(
  WRITE_ORIGIN)` — foreground=1.0, shadow/background/candidate/curator
  review-forks=0.5. Structural defence against self-confirmation loops
  where a claim surfaced in `brief()` gets "re-observed" by a review-
  fork reading the same dialog. Promotion/demotion fires as discrete
  events (`tier_promoted` / `tier_demoted`) with timestamps for an
  auditable trail.
- **Skill tier**: parallel state machine on `skill_usage`. Only
  foreground 'use' counter bumps drive promotion; `wrong` outcomes
  demote. Curator never archives validated tier and ages hypothesis at
  half the configured window.
- **`validate_threads` MCP tool**: heuristic triage of stale active
  threads with four categories (no_notes_old / shipped / dropped_open_q
  / stale_idle). Defaults to `dry_run=True`.
- 34 new tests covering the above (19 dialectic-tier, 15 skill-tier).
  Full suite at 495 passed / 1 skipped.

### Docs

- README + `docs/ARCHITECTURE.md` fully resynced with code state
  ([`21f8fad`](https://github.com/po4erk91/thread-keeper/commit/21f8fad),
  [`2369bcb`](https://github.com/po4erk91/thread-keeper/commit/2369bcb)):
  tool count 83 → 89, test count 412 → 495, MCP module table corrected
  (added `lessons` / `candidate_reviewer` / `curator` rows; `tag_signal`
  moved from `style` to `correlation`; `neighbors` moved from
  `correlation` to `graph`; removed gone `task_kill`). README also
  fixes: loop count "four" → "five", removed never-existed `clarifying`
  evidence kind, `THREADKEEPER_INGEST_INTERVAL_S` default 30 → 3.
- CONTRIBUTING.md "Releases" section now documents the manual
  bump-on-commit flow.

### Build / CI

- Initial `python-semantic-release` integration attempted and rolled
  back — see CONTRIBUTING.md "Releases" for the current manual flow.

## v0.4.1 — 2026-05-16

Tagged but never released. See `git log v0.4.0..v0.4.1` for the
intermediate changes.

## v0.4.0 — 2026-05-16

Hermes-borrow learning loops + multi-CLI mirror + PyPI initial.
See https://github.com/po4erk91/thread-keeper/releases/tag/v0.4.0

## v0.3.0 — 2026-05-14

CLI-agnostic learning loop.
See https://github.com/po4erk91/thread-keeper/releases/tag/v0.3.0

## v0.2.0 — 2026-05-14

Initial public release.
See https://github.com/po4erk91/thread-keeper/releases/tag/v0.2.0
