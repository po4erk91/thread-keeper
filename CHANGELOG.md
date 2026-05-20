# Changelog

All notable changes to this project are documented in this file.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/);
version bumps follow semver per the policy in
[CONTRIBUTING.md → Releases](CONTRIBUTING.md#releases).

## v0.5.1 — 2026-05-20

### CI

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
