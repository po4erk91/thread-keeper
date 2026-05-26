# Changelog

All notable changes to this project are documented in this file.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/);
version bumps follow semver per the policy in
[CONTRIBUTING.md → Releases](CONTRIBUTING.md#releases).

## [Unreleased]

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
