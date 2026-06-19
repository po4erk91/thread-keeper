# Roadmap

The old version of this file described five phases: portability, three-tier
memory, ML-extraction, ACL, federation. Since then the architecture has
moved in a different direction — the priority became spawn-driven learning
loop and skills, not multi-user / sync. Below — what landed and what
remains a live question.

---

## Closed

- Spawn as primary parallelism primitive (`spawn`, `tournament`, `tasks`,
  `task_logs`, `task_kill`).
- Slim children by default: `NO_EMBEDDINGS=1`, no third-party MCP, ~500MB
  RSS instead of ~1.3GB.
- Search proxy in parent process — slim child performs semantic search
  via `search_via_parent`.
- Spawn budget daemon: RSS accounting of child processes every 10s,
  admission control by `SPAWN_BUDGET_MB` (default 3GB), tools
  `spawn_budget_status` / `spawn_budget_set`.
- Auto-review on `close_thread`: for rich threads (≥5 notes, ≥2
  insight/move) a review-child is spawned, reads the notes dump,
  materializes a skill via `skill_manage`, calls `mark_skill_materialized`.
- Shadow-review daemon: periodic slim-child, scans the diff of
  `dialog_messages`, decides on class-level learning across all sessions,
  idempotent via `events.kind='shadow_review_pass'`.
- Skills system: `skill_manage` (create/edit/patch/write_file/remove_file/
  delete), `skill_record`, `skill_list`, `curator_run` for archiving stale.
- `skill_watcher` daemon — tracks SKILL.md changes, bumps
  `last_patched_at`.
- `skill_usage` telemetry + backfill from historical jsonl.
- Dialectic user model: `dialectic_claim` / `evidence` / `synthesis` /
  `review` / `supersede`, smoothed-ratio confidence, grouping by domain
  in brief.
- Hooks for Claude Code: SessionStart (`mp-brief.sh` — brief+context into
  system prompt), PostToolUse (`mp-status.sh` — markers of mutating
  calls), UserPromptSubmit (`inbox-check.sh`).
- Process health: orphan detection via ppid+heartbeat, `mp_health`,
  `mp_cleanup(dry_run, force)`.
- sqlite-vec HNSW: `notes_vec` / `dialog_vec` virtual tables on vec0,
  ~10x faster than Python-side cosine, fallback when vec0 is absent.
- FTS5 + semantic hybrid in `search` / `dialog_search`.
- `extract_recent` + review/accept/reject ledger — regex candidates with
  manual approval (mem0-style without LLM on this side).
- ingest fix — Skill-tool-only messages are no longer skipped.
- Issue-backed evolve loop: Evolve reviewer audits thread-keeper for safety,
  leaks, cost, reliability, optimizations, and current agent/MCP ideas, then
  creates/updates roadmap issues; Evolve applier drains one open issue at a
  time behind a visible GitHub issue claim comment and PR, advances past
  unstartable issues, and falls back to Curator reports and legacy
  `evolve_format` suggestions when no issue is startable.
- Cross-CLI ingest production verification (issue #1): the contract test in
  `scripts/tk_verify_ingest.py` gained a read-only `--live` mode that scores
  the three acceptance criteria — all CLI slots have production rows, shadow-
  review spans >1 adapter in one window, and the learning loop fires on
  non-Claude sessions — into a `PASS`/`PARTIAL`/`FAIL` verdict
  (`threadkeeper/verify_ingest.py`). Turns the ad-hoc, one-off manual check
  into a single reproducible command. Note: the "Google" slot is currently
  covered by data only when Gemini-legacy transcripts exist; the Antigravity
  (`agy`) successor adapter does not yet parse its sqlite/protobuf
  conversation store (tracked below under "more adapters"), so on a
  migrated-to-`agy` box that slot reports absent until that ingestion lands.

---

## Open

**More IDE / agent adapters — Cursor, Windsurf, JetBrains, Zed, etc.**
Current registry covers seven clients (Claude Code / Claude Desktop /
Codex CLI + desktop / Antigravity CLI `agy` / Gemini legacy / Copilot /
VS Code). The MCP ecosystem is wider:

- **Cursor** — AI-first VS Code fork, has its own MCP config at
  `~/.cursor/mcp.json`. Schema close to VS Code's but a separate file.
- **Windsurf** — Codeium's editor, MCP support via
  `~/.codeium/windsurf/mcp_config.json` (subject to change between
  versions).
- **JetBrains** (WebStorm / IntelliJ / PyCharm / etc.) — MCP plugin
  available; config typically per-IDE in the plugin's settings XML or
  a sidecar JSON.
- **Zed** — native MCP host, config in `~/.config/zed/settings.json`
  under `experimental.mcp_servers`.
- **Continue** — uses `~/.continue/config.json`.
- **Aider** — no native MCP yet (file-based config + CLI flags);
  revisit when MCP support ships.

Each adapter follows the existing pattern (`threadkeeper/adapters/<name>.py`
implementing `CLIAdapter`). Mechanically straightforward; the work is
chasing each tool's config conventions and keeping up with their
schema churn. Scope: S per adapter, ongoing. Triage by user demand —
don't pre-build adapters for tools no one runs.

**Multi-user / remote deployment** (re-opens former Phase 4 ACL +
Phase 5 federation). Today thread-keeper is single-user / single-
machine by design; the SQLite store lives at
`~/.threadkeeper/db.sqlite` and every adapter assumes local file
paths. Move to a hosted topology where N users connect their CLIs to
one shared MCP server (e.g. running on AWS / VPS / Tailscale-net):

- **HTTP / SSE transport.** FastMCP already supports
  `streamable_http`; expose via env knob (`THREADKEEPER_HTTP_PORT`).
  Scope: S.
- **Per-user auth.** Bearer token in `Authorization` header on every
  MCP call. Token → user_id binding stored in a new `users` table.
  Scope: M.
- **Row-level isolation.** Every existing table (threads, notes,
  dialog_messages, skill_usage, ...) gains a `user_id` column;
  queries filter by the authenticated session's user. Migration is
  the painful part — existing single-user data assigned to a default
  user. Scope: M-L.
- **ACL: what's shared vs private.** Some data is private per user
  (verbatim_user, personal threads); some is shared infrastructure
  (skills, lessons, dialectic about a common subject). Need a sharing
  model — explicit `share_with` field, or roles, or scopes
  (`private` / `team` / `global`). Open design question. Scope: L.
- **Cloud deployment playbook.** Containerise, document AWS / Fly.io /
  Railway recipes, secrets handling, backups. Scope: M.
- **Cost / threat model.** Hosted means one bad client can flood the
  server; rate limiting + abuse mitigation become real concerns.
  Scope: M.

Total scope: XL. Decision-needed up front: are we building a SaaS
posture (multi-tenant hosted with paid plans) or a self-host enabler
(team installs their own instance behind Tailscale)? The two paths
diverge on auth, abuse-handling, and what we put in the docs.

**Portability (former Phase 1).** Originally planned to extract
`IdentityProvider` / `TranscriptSource` interfaces for other stacks.
Counter-argument: the target client is Claude Code, the rest is YAGNI.
Leave as pending until an actual second stack appears. Scope: L (if done),
but most likely never. **Decision needed: dropping vs deferring.**

**Three-tier memory (former Phase 2).** Idea of promotion/eviction between
working_set / recall / archival. In practice brief() with priority +
core_set + FTS+semantic search cover the working case. The metric "do
we even need tiers" was not collected — there's no data showing that
briefs lose anything important. **Open question, not a task.** Scope for
prototype in shadow-mode: M.

**ML-extraction (former Phase 3).** `extract_recent` + accept_candidate
ledger provides a positive/negative corpus. Could replace regex with
sentence-transformers similarity scorer plus a classifier, bootstrap from
the current ledger. But: review_candidates is not actively used yet,
first need to understand — why. Possibly a UX problem, not ML. Scope: M.

**ACL (former Phase 4).** Folded into the "Multi-user / remote
deployment" item above — see the ACL sub-bullet there.

**Federation / sync (former Phase 5).** Subsumed by the
"Multi-user / remote deployment" item above. Hosted multi-tenant
deployment is the more concrete need; cross-machine CRDT-based sync
between independent installs is a strictly harder problem and
probably never the right answer here.

**Hot-config reload.** ✅ DONE (#2). The `config_watcher` daemon polls
`~/.claude/settings.json` (one mtime stat per tick, default 2 s) and, on a
change, mirrors the threadkeeper-relevant `env` keys into the live process,
calls `config.reload_settings()` to re-instantiate `Settings` and re-publish
the module constants, and propagates each changed value into every loaded
`threadkeeper.*` module that imported a copy — so daemons and tools pick up
the new knob without a Claude Code restart. Newly-enabled daemons (interval
0 → >0) are started; already-running ones self-adjust on their next tick
(`daemon_sleep` keeps a hot-disabled loop from busy-spinning). Manual trigger
`config_reload()`; diagnostics `config_watch_status()`; off via
`THREADKEEPER_CONFIG_WATCH_INTERVAL_S=0`. Does not help host-CLI hooks (those
are read by the CLI, not us); half-written files are debounced via an
mtime-cursor + JSON-parse guard.

**Telemetry dashboard.** ✅ DONE. `mp_dashboard(window_days)` is the
aggregate the point views (`shadow_review_status`, `spawn_budget_status`,
`mp_health`) lacked: store sizes, per-loop fire counts (window vs 30d +
last-fire age), and outcomes (skills materialized, tier promotions,
candidate accept-vs-reject rate). Read-only, degrades gracefully on
partial schemas. The first live run surfaced the "Shadow-review proof"
item below (shadow fires ≫ skills materialized). Possible follow-up:
periodic dump-to-file for historical trend lines (currently a
point-in-time snapshot). Scope of follow-up: S.

**Shadow-review production telemetry.** ✅ DONE (#6). `shadow_review_status()`
now carries a per-loop production-validation rollup for the 24h / 7d windows:
fire count, outcome mix (no_window / too_short / spawned / deferred / error),
the MATERIALIZED-vs-SKIP hit rate of spawned evaluator children (read from each
child's captured log tail), durable skill writes attributable to
`write_origin='shadow_review'`, and total Claude-spawn time spent — so "is this
loop earning its Opus minutes or just emitting SKIPs?" is now a number, not a
guess. Pure aggregator `shadow_telemetry()`; `snapshot_path` dumps a markdown
table for human review; ephemeral/aged-out child logs count as `unknown` so the
hit-rate denominator stays honest. The token/$ half of spawn cost remains #25.

**Shadow-review proof in production.** ✅ ANSWERED (~16d of live data,
read via `mp_dashboard` + an evidence dive). Verdict: **complementary,
not duplicate — but it was over-firing.** Numbers: 1423 passes, 773
(54%) actually spawned an evaluator (NOT cheap skips), ~120 spawns/day
and climbing. Its real product is **`lessons.md` (1054 sections),
not skills** — it stopped creating skill files 15 days in; the headline
"5 skills materialized" undercounts its output ~200× because it writes
to a different store than auto-review. Scope difference (from code):
auto-review fires once per rich CLOSED thread reading curated notes;
shadow fires on a timer over ALL sessions' raw dialog and feeds the
CLI-agnostic lessons store. It is also the only safety net when the
agent never closes a thread. Decision taken: keep it, but cut the churn
— `SHADOW_REVIEW_INTERVAL_S` 900→3600, `WINDOW_S` 900→3600 (kept equal
so no dialog falls between ticks), `MIN_CHARS` 500→1500 so marginal
windows don't spawn a child just to emit SKIP. 4× fewer children, lessons
store is already saturating so recall loss is negligible.

Resolved (was "surfaced-but-deferred"): `tasks.return_code` recorded
NULL even after children ended (0 of 944 ended tasks had a code), so the
dashboard couldn't measure spawn→outcome conversion. The original
diagnosis (slim children racing the poll) was incomplete — it affected
*every* ended task, including pid>0 headless ones. Real cause: the
`tasks` table outlives the MCP process that launched a child, so the
cross-session reaper is almost never the spawning parent and
`os.waitpid` raises `ChildProcessError` (exit code unknowable for a
process you didn't spawn). Fix shipped: headless children run under a
thin stdlib recorder (`threadkeeper/_spawn_wrap.py`) that persists
`return_code` from inside the child's own lifecycle — no waitpid race,
no dependency on the launching session staying alive. It forwards
termination signals so `task_kill` still works; the parent reaper stays
as a fallback. The visible/Terminal path records via a `--record` shell
line.

**Documentation.** README / ARCHITECTURE / this file — update now.
Going forward: keep in sync when the set of tools or daemons changes.
Scope: ongoing.

**Curator policy tuning.** ✅ DONE — superseded the old time-based archive
heuristic. `curator_run` now spawns a slim child that grades every lesson +
recently-active skill (and any concepts) against an explicit rubric
(KEEP / PATCH / CONSOLIDATE / PRUNE), writes an auditable
`REPORT-<isodate>.md`, and — **destructive-by-default** — applies its own
PATCH/PRUNE/CONSOLIDATE directly via `lesson_append` / `lesson_remove`
(always without `force`, so user/foreground lessons are refused) /
`skill_manage`. `[PROTECTED]` (pinned / foreground / user) entries are never
mutated, and the pass is single-flight across processes (a non-blocking
`fcntl.flock` pidfile plus a running-children check). The "dry-run mode with
a dump of what would be archived" this item asked for already exists: set
`THREADKEEPER_CURATOR_DESTRUCTIVE=0` for advisory REPORT-only.

Open follow-ups (issue-backed): restorable deletion / pre-mutation snapshot
before autonomous prune (#40, #41, #52); a write lock for the unlocked
`lessons.md` read-modify-write now that the curator and shadow_review both
mutate it (#91); bounding the curator/candidate_reviewer prompt argv so the
full inventory dump can't hit `E2BIG` — the single-flight half of #24 has
landed but the argv bound has not (#24); debouncing passes on unchanged
inventories (#35); and making the curator's `PRUNE_CONCEPT` /
`CONSOLIDATE_CONCEPT` rubric actually appliable, since no concept-mutation
tool exists today (#75). Scope: S–M each.

**Concepts store lifecycle.** ✅ DONE (#75). The `concepts` table was
write-only / grow-only: no remove/consolidate/confidence tool, auto-registered
entries piling up, and `last_evidence_at` frozen at registration so the
Curator's concept-prune rubric and the brief's concept ordering both degenerated
to registration-age. Fixed end-to-end: `register_concept` /
`accept_candidate(kind='concept')` now dedup on write — a re-surfaced equivalent
invariant (description cosine ≥ 0.85, normalized-string fallback when embeddings
are off) corroborates the existing row (bumps `last_evidence_at`, raises
confidence to `max(existing, incoming)`) instead of inserting a near-duplicate;
the brief orders concepts by `COALESCE(last_evidence_at, registered_at)`; and a
new `concept_manage` tool (`remove` / `consolidate` / `set_confidence`) makes the
Curator's `CONSOLIDATE_CONCEPT` / `PRUNE_CONCEPT` / confidence-review rubric
applyable — wired into the Curator's destructive toolset and the curator-report
applier (the old "NEVER mutate concepts" punt is gone). Concepts are all
system-generated, so `concept_manage` needs no `force` guard. Shares the
recency/corroboration treatment proposed for the saturating lessons store (#27).

**Extract_recent precision.** ✅ PARTIALLY DONE. The hypothesis was
"too many / too noisy", and the ledger confirmed it hard: 107 decisions,
1 accept, ~5% precision. Root cause found by reading the 106 rejects —
the dominant noise (66/107, all on the H1 user_want heuristic) was
thread-keeper's OWN spawned-child sessions (curator/panel/research
agents) whose system prompts re-entered the dialog and got re-extracted.
The prompt-prefix noise list only caught known openers; arbitrary task
framing ("You are auditing…", "You are analyzing whether…", "Use the
Write tool to…") slipped through. Fix: extract_recent now also excludes
any session whose cid is a `tasks.spawned_cid` (reusing the
`ingest._is_spawned_child_session` provenance link) — kills the whole
class regardless of wording. Remaining open: even with self-noise gone,
the surviving heuristics (H2 long_insight on assistant summaries, H3
example_regularity on bulleted reports) still over-fire on work
artifacts; a precision re-measurement after a few real sessions decides
whether they need tightening or a similarity-scorer (the ML-extraction
item above). Auto-flow-to-notes is explicitly NOT pursued — at the
observed precision it would inject garbage. Scope of remainder: S.

---

## Open — 2026-06-14 audit (issue-backed)

A multi-dimensional self-audit (security/privacy, daemon cost & leaks,
learning-loop reliability, and current MCP/memory research) surfaced the
following concrete gaps. Each is tracked as a GitHub issue; the evolve
applier drains them. Listed here so the roadmap reflects the live backlog.

**Security & privilege hardening.** Two real gaps:
- The local store is world-readable. `~/.threadkeeper/db.sqlite`, `.env`, and
  curator reports are created with default perms while the DB holds full
  transcripts + `verbatim` + the dialectic user model — any local account can
  read it. chmod 0600/0700 on creation. (#21)
- The autonomous GitHub-writing daemons run `bypassPermissions` with `gh`,
  guarded only by a prompt line; untrusted stored/issue content is injected
  into them and nothing mechanically redacts issue/PR bodies. De-privilege the
  appliers, fence injected content as data, sanitize bodies for paths/secrets,
  and role-gate the dangerous spawn mode. (#22) Scope: S–M.
- ✅ DONE (#76). The **learning-loop synthesis children** (distinct from #22's
  GitHub daemons) turn *raw observed dialog* into *auto-loaded* skill / lesson /
  user-model artifacts with no injection fence and no provenance trust-tiering —
  a durable memory-poisoning channel (a poisoned `SKILL.md` auto-triggers on
  every future `SessionStart`, across every CLI). Extended #22's "fence injected
  content as data" principle to these loops: every synthesis prompt
  (`shadow_review`, `candidate_reviewer`, the three `review_prompts` templates,
  the dialectic validator) wraps the observed span in an explicit
  `<observed_dialog>…</observed_dialog>` data fence; stated-policy rules are
  tiered to genuine foreground `role='user'` turns; the shadow / candidate /
  close-thread children are de-privileged (no bare `Read`/`Write`); loop-authored
  skills stay distinguishable by `created_by_origin` for an auto-load gate / #26
  elicitation; and a write-time screen refuses loop-origin bodies with
  imperative-override / remote-exec idioms. `SECURITY.md` documents the trust
  boundary. Scope was S–M.
- ✅ DONE (#74). **Cross-provider memory egress.** `brief()` rendered the
  personal-class user-model — `verbatim_user` quotes + the `dialectic` claims
  *about the user* — unconditionally, and `brief()` is consumed by whatever LLM
  vendor backs the active/spawned CLI, so a quote said to Claude could egress to
  OpenAI / Google / Microsoft on the next spawn or session-start with no policy
  or opt-out. Added a static sensitivity-class map + CLI→vendor map (`egress.py`)
  and the `THREADKEEPER_MEMORY_EGRESS` knob (`all` default — byte-identical
  behavior | `same-vendor` — personal only to Anthropic/Claude | `work-only` —
  personal to no vendor). `render_brief` resolves the consuming vendor (explicit
  arg → `THREADKEEPER_EGRESS_CONSUMER` set by `spawn()` → `active_cli()`) and
  omits the personal sections for a restricted third-party consumer, leaving a
  one-line `withheld` disclosure; `spawn()` propagates the target vendor so a
  third-party child can't pull more than the policy allows. README + ARCHITECTURE
  document the default and the opt-out. Distinct from the local-perms gap
  (#21/#68) and the injection surface (#22/#76). Scope was S.

**Evolve issue-flow reliability.** The applier posts a claim comment *before*
spawning the implementer; a spawn failure or red-CI abort leaks the claim for a
full 24h (TTL-only, no reaper), and a marker-write failure after `gh pr create`
can open a duplicate PR. Add a claim reaper + open-PR dedup + the missing
spawn-after-claim test. (#23) Scope: S.

**Daemon robustness under load.** Curator lacks the machine-wide single-flight
every other spawning daemon has, and `candidate_reviewer`/`curator` dump an
unbounded queue/inventory into the child prompt argv (the `E2BIG` class already
fixed for `dialectic_validator`). Add curator single-flight + bound the
prompts. (#24) Scope: S.

**Spawn cost accounting.** The spawn budget caps child RSS only; there is no
token/$ accounting, so "is this loop worth the Opus minutes?" (the recurring
shadow-review-proof question, #6) can't be answered with a number. Capture
token/cost in the `_spawn_wrap` recorder, add a daily cost/token ceiling in the
admission path, surface per-loop spend in `mp_dashboard`. (#25, extends #6)
Scope: M.

**Research-driven memory upgrades** (sourced):
- MCP **elicitation** — now shipping in Claude Code v2.1.76 — for high-stakes
  confirmations (supersede, curator apply, the under-used `review_candidates`
  flow), replacing ignorable text nudges with a real confirm/choose dialog,
  graceful fallback where unsupported. (#26)
- **Bi-temporal** dialectic claims (`valid_from`/`valid_to`, Zep/Graphiti
  "invalidate, don't delete") so a superseded preference records *when* it
  stopped being valid, enabling time-scoped user-model queries. (#28)
- **Decay/eviction** scoring for the saturating ~1054-section lessons store
  (the curator ages skills but not lessons; mem0 Ebbinghaus pattern). (#27)
- Note: MCP **sampling** (host-run completions, which would let daemons skip
  paid spawn children entirely) remains *unsupported* on Claude Code
  (anthropics/claude-code#1785) — tracked, not actionable yet. The slim-spawn
  subprocess model stays correct until a host exposes the capability.

Scope: S–M each.

Also filed in the same audit: status-path `gh` fan-out on the menu-bar poll
(#18), auto-update self-restart with no smoke-check/rollback (#19), and
Antigravity transcript ingest not yet implemented (#20).

Follow-up gaps from the 2026-06-17 audit:
- Semantic lesson dedup at write time (#34).
- Curator pass debounce / unchanged-inventory coalescing (#35).
- Full-lineage harvest exclusion for native Agent/Workflow descendants (#36).
- Transcript secret scrubbing before persistence into `dialog_messages` /
  `dialog_fts` (#37).
- Shared GitHub API budget/backoff across roadmap automation (#38).
- Curator went **destructive-by-default** (`THREADKEEPER_CURATOR_DESTRUCTIVE=1`):
  the autonomous child now prunes/consolidates lessons + skills in place with no
  pre-mutation snapshot, no restorable tombstone of pruned bodies (`lesson_remove`
  records the slug only; `lessons.md` is not version-controlled), and no
  destructive-action telemetry in `mp_dashboard`. Add a snapshot/restore safety
  net + structured prune/consolidate counts (#40).
- Retention/GC for the `tasks` table and `TASK_LOG_DIR` spool files — every
  spawn leaves a permanent `tasks` row (full `prompt`) plus
  `.log`/`.stdin.txt`/`.command` files that nothing prunes; `tasks.prompt`
  holds curator/issue/audit content indefinitely and the default
  `/tmp/thread-keeper-tasks` spool sits outside the `~/.threadkeeper`
  perimeter that #21 hardens (#42).
- Git working-tree safety for evolve roadmap automation: dirty-tree guard
  (mirroring `auto_update`'s `skipped_dirty_checkout`), branch-from-clean-`main`,
  and reviewer/applier mutual exclusion or `git worktree` isolation so concurrent
  PR-producing children don't race on `.git/index.lock` or contaminate PR diffs
  with unrelated working-tree WIP (#43).
- Auto-update payload integrity/provenance: the on-by-default daily pip/git
  self-update installs and runs new code with **no version pin, hash, or PyPI
  attestation/signed-tag verification**, then restarts on it. A compromised
  release auto-propagates to every install within ~24h. Distinct from #19
  (reliability smoke-check/rollback — a malicious-but-importable release passes
  that) and #22 (GitHub-writing daemons). Verify provenance before upgrade and
  document auto-update as standing consent to run maintainer code (#44).

Deep code-audit pass (2026-06-17, evolve_reviewer second pass; each finding
verified at the cited file:line, deduplicated against the issues above):
- Extract H4 paraphrase-cluster path re-harvests **rejected** candidates
  forever — its inline dedup checks `status IN ('pending','accepted')` only,
  omitting `'rejected'`, so a rejected cluster reappears on the next
  overlapping window. Same incident class as the documented #157/#158
  prod loop, on the one heuristic path that never got the `_candidate_exists`
  fix (#62).
- Author-trust boundary on autonomous issue pickup: the applier fetches no
  `authorAssociation` and treats every open issue on this **public** repo as
  backlog for a `bypassPermissions` child; separately, the Python-generated
  claim comment leaks hostname/PID/git-rev even though an opaque
  `_host_branch_slug()` already exists. Gate pickup by author association and
  redact the claim body. Complements #22 (fencing) and #50 (skip-label) (#63).
- Spawn budget is blind to **visible (pid=0)** children: their real RSS is
  never measured (the daemon skips `pid<=0`), and a visible row whose jsonl
  never resolves pins its full-estimate budget share forever. (The
  admission-time check-then-spawn TOCTOU is #58; kill-path safety is #66.) (#64).
- ✅ DONE (#68). Spawn **slim MCP config** was written world-readable with no
  `chmod` and embedded the host server `env` block, while the stdin prompt file
  is correctly `0600` and the `.command` script was `0755`. Now: slim config
  `chmod 0600`, `.command` `0700`, and the slim config copies only the env keys
  a slim child needs (`PYTHONPATH`/`VIRTUAL_ENV`/`PYTHONHOME` + `THREADKEEPER_*`),
  dropping host secrets. (Spool-file retention/cleanup is #42.)
- `shadow_review` + `dialectic_miner` advance a single global `created_at`
  high-water cursor, so **late/out-of-order ingested** messages (resumed
  sessions, newly-installed adapters, post-downtime backfill) that land below
  the cursor are evaluated by neither loop. Use a grace lookback or an
  ingest-order watermark instead of the transcript timestamp (#69).
- ✅ DONE (#71). Memory **recall/abstention** eval harness (LongMemEval-style
  QA + abstention + tokens-per-retrieval) to give the lessons-decay (#27) and
  bi-temporal (#28) work a number to optimize against — complementary to the
  learning-loop **decision-quality** harness (#72). `scripts/memory_eval/run.py`
  runs the real `search()`/`dialog_search()`/`brief()` tools over a fixed
  ground-truth set and reports accuracy (per the five LongMemEval axes),
  abstention rate (never-happened questions correctly refused), and
  tokens-per-retrieval. Lexical judge by default (offline, reproducible,
  CI-safe); optional `--judge llm` for answer-reasoning grading. Bundled demo
  corpus is a golden baseline; `--db` evaluates a snapshot **read-only** (copied
  to temp). Use the temporal-reasoning + knowledge-update axes as the
  optimization target for #27/#28.

- ✅ DONE (#72). Learning-loop **decision-quality** eval harness. The
  quality-control daemons (`shadow_review`, `candidate_reviewer`, `curator`)
  make accept/reject/materialize calls with decision telemetry but no labeled
  set and no precision/recall — nothing scored how often the hard-coded
  class-vs-incident rubric was right. New `threadkeeper/eval/`
  (`python -m threadkeeper.eval`) replays the *current* daemon rubrics over a
  small hand-labeled, anonymized fixture set and reports precision/recall/F1 for
  the shadow-review and candidate decisions plus a calibrated judge↔human
  agreement (accuracy + Cohen's kappa) on the open-ended "is this skill high
  quality" question, with a `verify_ingest`-style PASS/PARTIAL/FAIL verdict on
  harness readiness. Default **rubric** judge is offline/deterministic and
  section-coupled to the live prompt, so editing a rubric *moves the metric*
  (caught in CI against the golden baseline); `--judge llm` replays the actual
  prompts for the high-fidelity number. This gives the ROADMAP's own open
  questions — **extract precision re-measurement** and **"do we even need
  tiers — metric not collected"** — a harness to measure against; point
  `--fixtures-dir` at a production-derived labeled set to collect those numbers.
- MCP **tool annotations** (`readOnly`/`destructive`/`idempotent` hints) +
  structured output across the tool registry (independently confirmed; canonical
  issue #67) — gives hosts a mechanical read-vs-write signal and composes with
  #22 and the elicitation work in #26.
- **Learning-loop memory poisoning** — the synthesis children (`shadow_review`,
  `candidate_reviewer`, close-thread auto-review, `dialectic_validator`) turn the
  **raw observed-dialog stream** into **auto-loaded** `SKILL.md` / `lessons.md` /
  user-model claims with **no injection fence and no provenance trust-tiering**:
  `_collect_window` keeps all `user`/`assistant`/`[thinking]` content (incl.
  untrusted text the agent read from web/files/paste), the review prompts carry a
  *quality* fence (`ANTI_CAPTURE`) but no "treat observed dialog as data, not
  instructions" boundary, and the writer child runs `permission_mode="auto"` with
  bare `Write` allow-listed. A poisoned skill auto-triggers on every future
  SessionStart across every CLI. Fence observed content as data, trust-tier
  policy capture to genuine user turns, de-privilege the writer (drop bare
  `Write`), and gate auto-load of loop-minted skills (compose with #26). Distinct
  from #22 (GitHub-writing daemons; redactable public sink) and #37 (secret
  scrub, outbound) (#76).
- Concepts store is **write-only / grow-only**: no remove/consolidate/
  confidence tool exists (`tools/concepts.py` has only register/list/expand),
  concepts are auto-registered (`accept_candidate kind='concept'` at conf=low +
  agent `register_concept`) so the store grows unbounded, and `last_evidence_at`
  is set once at registration and **never bumped** (only `user_dialectic` bumps
  it via `dialectic.py`) — so the curator's concept-prune rubric (`conf=low AND
  last_evidence >30d`) and the brief's concept ordering both degenerate to pure
  registration-age. The curator's destructive toolset carries no concept tool,
  and the curator-report applier hard-codes "NEVER mutate concepts for now", so
  the `PRUNE_CONCEPT`/`CONSOLIDATE_CONCEPT` rubric is unappliable by design.
  Distinct from the lessons-only decay item (#27) (#75).

Also folded into existing issues rather than filed anew: auto-update restarts
even when `_run_setup` reports `setup=failed` (→ #19); `dialectic_claim` lacks
the write-time dedup gate `lesson_append` has (→ #34); `agent_status` log-sample
scraping resurfaces unredacted child `gh`/`git` output (→ #37).

Deep code-audit pass (2026-06-17, evolve_reviewer third pass; five parallel
read-only subsystem audits, each finding re-verified at the cited file:line and
deduplicated against the issues above):
- Per-file **ingest cursor loses messages**: `_ingest_file` advances
  `last_mtime` even when the `max_msgs` cap truncates the read (default 50 at
  session start), and the skip guard compares only mtime — never the stored
  `last_size` — so same-second appends are dropped. Distinct from the global
  out-of-order cursor #69 (#89).
- Two learning daemons **drop dialog windows**: `shadow_review` records its
  high-water cursor even when `spawn()` returns an `ERR ...` budget-cap string
  (a value, not an exception, so the `try/except` misses it), and the `extract`
  daemon scans a fixed wall-clock window with a dead cursor, leaving an
  uncovered gap whenever `interval > window` (#90).
- `lessons.md` append/remove is an **unlocked read-modify-write**, so concurrent
  loop writers (shadow / candidate / auto-review / foreground) last-writer-win
  and silently clobber each other's edits — the new curator single-flight only
  serializes curators against each other (#91).
- `spawn_budget` daemon **starts inside spawned children**: `start_budget_daemon`
  lacks the `BACKGROUND_DAEMONS_ALLOWED` gate `memory_guard` already has, so
  every slim child runs a perpetual `ps`-polling thread it was explicitly
  designed not to run (#92).
- Budget/guard **RSS accounting**: `ps` failures are read as 0 MB (suppressing a
  needed retire/kill, or freeing in-use budget), and the liveness refresh caps
  at 100 rows while the budget sums over *all* un-ended rows, so a >100-row tail
  of unrefreshed rows pins the budget. Complements #64/#66 (#93).
- Security: the `/tmp/thread-keeper-tasks` **spool dir** is created world-knowable
  with `exist_ok=True` and no owner/`O_NOFOLLOW` check, then per-file
  create-then-`chmod` — a symlink + brief-disclosure vector for spawn-prompt
  content on shared hosts. Distinct from #21 (`~/.threadkeeper`) and #68 (#94).
- Legacy **DB migration** copies the live `-wal`/`-shm` sidecars with non-atomic
  `shutil.copy2` and no checkpoint — pairing a stale `-shm` with a copied `-wal`
  can produce a torn/corrupt DB at the new path (#95).
- **Pickup claims leak**: `threads.claimed_at` has no TTL/reaper and the
  `auto_spawn` child is never told to `release_pickup`, so even a successful
  pickup pins the thread out of the candidate pool forever (#96).
- Codex adapter: the fallback message **UUID** has no per-line offset, so
  timestamp-colliding messages collapse to one uuid and the later ones are
  deduped away; separately, each rollout file is fully scanned twice per ingest
  pass (#97).
- The candidate-reviewer's "max 2 new skills per pass" cap is **prompt-only**;
  `skill_manage(create)` has no server-side per-pass counter, so an injected or
  confused (injection-prone) child can mass-create skills in one pass (#98).

Also extended existing issues with verified file:line detail rather than filing
anew: `get_db` re-runs the full schema + ~25 migrations per call and leaks
connections (→ #59); the `project='subagents'` exclusion is dead across six
modules (→ #36); pid-reuse also hits `task_kill`/`_reap_finished_tasks` (→ #66);
a SIGKILL'd `_spawn_wrap` leaves a budget row pinned (→ #64); applied markers key
on issue number, not PR url (→ #51); the roadmap apply pass double-fetches the
issue list and churns per-candidate claim comments (→ #38); `agent_status`
re-reads up to ~30 task logs per menu-bar poll (→ #18); `extract_candidates` is
another unbounded table with a full-scan dedup probe (→ #45); the auto-update
due-gate reads the prunable `events` table and `_setup` re-registration can
drift the launch interpreter (→ #19).

---

## Open — 2026-06-17 reviewer follow-up (issue-backed)

A reviewer pass over the autonomous roadmap-automation surface (evolve
reviewer/applier, curator) surfaced three concrete gaps not covered by the
existing backlog. Each is tracked as a GitHub issue.

**Evolve applier never refuses inappropriate issues.** `_open_roadmap_issues()`
treats every open issue as backlog (`roadmap` label first, then FIFO) and only
skips already-applied / actively-claimed ones — there is no opt-out. The child
runs `bypassPermissions` + `Bash/Edit/Write`, so it can auto-attempt human-gated
work (design/discussion questions, the XL multi-user item, the security
hardening issues #21/#22, `good-first-issue`s). Add a configurable skip-label
denylist (and optional opt-in posture). (#50) Scope: S.

**Closed-unmerged applier PR strands its issue.** The child records a permanent
`roadmap_issue_applied` marker once it opens a PR; if a human closes that PR
without merging, GitHub leaves the issue open but the applier skips it forever.
Reconcile applied-markers against PR merge state (re-queue closed-unmerged PRs
with a bounded retry). Distinct from the shipped claim-leak / duplicate-PR
guards (#23). (#51) Scope: S.

**Lesson removal is irreversible as the curator goes destructive-by-default.**
`lesson_remove` physically rewrites `lessons.md`; the audit event stores only
slug + source, not the body. With `curator_destructive` now defaulting on, an
autonomously-pruned lesson is unrecoverable (unlike threads, which reopen on a
note). Add soft-delete / tombstone + restore with a retention window.
Complements decay scoring (#27) and write-time dedup (#34). (#52) Scope: S–M.

---

## Principle

Don't add phases for the sake of "architectural completeness". Each open
item above exists because there's a concrete gap in the current flow.
If an item becomes "open question without a gap" — drop it, don't defer.
