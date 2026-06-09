# thread-keeper

[![tests](https://github.com/po4erk91/thread-keeper/actions/workflows/test.yml/badge.svg)](https://github.com/po4erk91/thread-keeper/actions/workflows/test.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/threadkeeper.svg)](https://pypi.org/project/threadkeeper/)
[![CLIs](https://img.shields.io/badge/CLIs-Claude%20%7C%20Codex%20%7C%20Gemini%20%7C%20Copilot%20%7C%20VS%20Code-green)](#multi-cli-integration)

**Multi-agent shared brain across Claude Code/Desktop, Codex, Gemini,
Copilot, and VS Code.** Cross-session memory, self-improving skill
loops, and inter-agent signaling — one local MCP server turns parallel
agent instances into a coordinated multi-agent system instead of N
isolated chats.

Every connected client (Claude Code, Claude Desktop, Codex CLI +
desktop, Gemini, Copilot, every MCP-aware VS Code extension) shares
one SQLite store, one set of threads, one user model, and one learning
loop that improves the skill library autonomously over time.

The brief format is dense — structural tags, opaque IDs, ~6 KB per
session-start injection. Optimized for agent consumption, not human reading.

---

## Why

Every agent CLI starts cold. Context dies at session boundaries.
Skills you taught Claude don't transfer to Codex. Threads you closed
in yesterday's Gemini chat are invisible to today's Copilot. Parallel
agent instances running the same task don't know about each other and
duplicate work or step on each other's writes.

thread-keeper is the substrate underneath. Three things that together
make it more than a memory store:

- **Collective memory** — threads, notes, verbatim quotes, dialectic
  claims about you. Survives session, restart, CLI swap. One agent
  records, every other agent (any CLI) reads. The brief injected at
  session start gives a new agent everything the previous one knew.
- **Multi-agent coordination** — `spawn` primitive launches child
  agents in parallel, each gets a self_cid + sees the same memory.
  `broadcast` / `whisper` / `inbox` / `wait` / `ask` / `respond` let
  concurrent sessions signal each other across CLIs. Parent /
  children / sibling agents become a coordinated swarm, not isolated
  chats.
- **Self-improving skill library** — autonomous background loops
  (auto-review on thread close, shadow-review daemon, extract
  harvester, candidate-reviewer, weekly Curator, and a thread-janitor
  that auto-closes idle threads so abandoned work reaches the harvest
  path — closing is reversible, a note reopens a closed thread)
  materialize class-level skills as the agents work. Adapted to multi-CLI:
  SKILL.md is the primary write target and gets mirrored to every
  known/configured skills root simultaneously (`~/.claude/skills/`,
  `~/.codex/skills/`, existing `~/.agents/skills/`, extra roots from
  `THREADKEEPER_EXTRA_SKILLS_DIRS`, and `~/.threadkeeper/skills/`),
  with lessons.md as a fallback for CLIs without a native skills loader.

---

## Quickstart

The shortest path — **PyPI + pipx** (recommended):

```bash
pipx install 'threadkeeper[semantic]' && thread-keeper-setup
```

`thread-keeper-setup` detects every CLI you have installed (Claude
Code / Claude Desktop / Codex CLI + desktop / Gemini / Copilot / VS
Code), registers the MCP server in each one's config, copies hooks to
`~/.threadkeeper/hooks/`, and writes a managed instructions block into
each CLI's per-user instructions file (`CLAUDE.md` / `AGENTS.md` /
`GEMINI.md` / `copilot-instructions.md` — Claude Desktop and VS Code
have no global instructions file, so that step is skipped for them).

Restart your CLI of choice. The SessionStart hook injects a brief on
first message; no manual `brief()` call required.

### Alternative installs

If you don't have `pipx` and don't want to install it:

```bash
# uv (Rust-fast Python tool runner) — no clone, single binary on PATH
uv tool install 'threadkeeper[semantic]' && thread-keeper-setup

# Plain pip into a venv
python3 -m venv ~/.threadkeeper-venv
~/.threadkeeper-venv/bin/pip install 'threadkeeper[semantic]'
~/.threadkeeper-venv/bin/thread-keeper-setup
```

For development (editable install from a git checkout) or to track the
bleeding edge:

```bash
# One-liner installer — clones to ~/thread-keeper, makes a venv,
# editable-installs, wires every detected CLI. Idempotent — re-run to
# update (it git-pulls + reinstalls).
curl -fsSL https://raw.githubusercontent.com/po4erk91/thread-keeper/main/install.sh | bash -s -- --semantic

# Or fully manual
git clone https://github.com/po4erk91/thread-keeper ~/thread-keeper
cd ~/thread-keeper && python3 -m venv .venv
.venv/bin/pip install -e '.[semantic]'
.venv/bin/thread-keeper-setup
```

To preview without writing anything:

```bash
thread-keeper-setup --dry-run
```

---

## Multi-CLI integration

| CLI | MCP config | Instructions file | Hooks | Transcripts ingested |
|---|---|---|---|---|
| Claude Code | `~/.claude.json` `mcpServers` | `~/.claude/CLAUDE.md` | `~/.claude/settings.json` `hooks` | `~/.claude/projects/**/*.jsonl` |
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` `mcpServers` (macOS); `%APPDATA%\Claude\…` (Win); `~/.config/Claude/…` (Linux) | none (GUI-only) | not supported by the app | none — chats live in Electron IndexedDB |
| Codex (CLI + desktop) | `~/.codex/config.toml` `[mcp_servers]` (shared between CLI and `Codex.app`) | `~/.codex/AGENTS.md` | not supported | `~/.codex/sessions/**/rollout-*.jsonl` |
| Gemini | `~/.gemini/settings.json` `mcpServers` | `~/.gemini/GEMINI.md` | `~/.gemini/settings.json` `hooks` | `~/.gemini/tmp/<user>/chats/session-*.jsonl` |
| Copilot | `~/.copilot/mcp-config.json` `mcpServers` | `~/.copilot/copilot-instructions.md` | `~/.copilot/hooks.json` | `~/.copilot/session-store.db` (sqlite) |
| VS Code | `~/Library/Application Support/Code/User/mcp.json` `servers` (macOS); `%APPDATA%\Code\User\mcp.json` (Win); `~/.config/Code/User/mcp.json` (Linux) | none (per-workspace only) | not supported | none — extensions own their history |

Every CLI that produces parseable transcripts feeds the same
`dialog_messages` table with a `source` tag, so `dialog_search()` finds
matches regardless of where the conversation happened. Claude Desktop
and the VS Code adapter are the exceptions — MCP registration only;
their chats don't reach the table for now (Electron IndexedDB on the
Claude Desktop side; per-extension stores on the VS Code side).

VS Code's user-level `mcp.json` is the central host that **every
MCP-aware VS Code extension** consumes — GitHub Copilot Chat, the
Anthropic Claude IDE plugin, the OpenAI Codex IDE plugin, Continue,
Cline, … — so a single registration there reaches all of them at once.

Adding a new CLI = one file under `threadkeeper/adapters/` implementing
the `CLIAdapter` contract. See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Core systems

### Spawn — primary parallelism primitive

`spawn(prompt, slim=True, role=..., visible=False, ...)` launches a child
Claude session via a `claude -p` subprocess. By default `slim=True`: the
child loads only the thread-keeper MCP, no embeddings, no third-party
servers. ~500 MB RSS versus ~1.3 GB for a full child. Heuristic for the
parent: N≥2 modular independent units of ≥5 min each = spawn signal.
Spawn also marks children with `THREADKEEPER_SPAWNED_CHILD=1`, so
autonomous learning daemons cannot recursively start inside review forks.

A daemon measures combined child RSS every 10 s; admission control
refuses a new spawn that would exceed `THREADKEEPER_SPAWN_BUDGET_MB`
(3 GB default). Slim children that need semantic search delegate to the
parent via `search_via_parent` — no per-child copy of the embedding model.

### Learning loops

Five loops turn raw agent dialog into a curated, multi-CLI-mirrored
skill library — autonomously, without requiring agents to call
`note()` / `verbatim_user()` / `close_thread()` on their own (audit
shows agents focused on their primary task rarely do).

**Pipeline at a glance:**

```
   every CLI's transcripts
            │
            ▼  (ingest, every 30s — always-on)
   dialog_messages  ◄──────────────────────────────────────┐
            │                                              │
            ├────────► [1] auto_review on close_thread     │
            │              (agent triggers — rare)         │
            │                  │                           │
            ├────────► [2] shadow_review daemon            │
            │              (cron, every 15 min)            │
            │                  │                           │
            ├────────► [3] extract daemon                  │
            │              (cron, every 10 min)            │
            │                  │                           │
            │              extract_candidates              │
            │                  │                           │
            │                  ▼                           │
            │          [4] candidate_reviewer daemon       │
            │              (cron, every 1 h) ──────────────┤
            │                  │                           │
            ▼                  ▼                           │
         brief()    SKILL.md + lessons.md ─► skill_usage   │
            │              │                  │            │
            │              ▼                  ▼            │
            │         (every configured       │            │
            │          skills/ root)          │            │
            │              │                  │            │
            │              └──────► [5] Curator daemon ───┘
            │                          (cron, every 7d)
            │                              │
            │                              ▼
            │                       REPORT-<date>.md
            ▼
   injected into every new session at SessionStart
```

**Each loop in one row:**

| # | Loop | Default tick | Reads | Writes |
|---|---|---|---|---|
| 1 | auto_review on close_thread | on `close_thread()` for rich threads | the thread's notes | SKILL.md, lessons.md |
| 2 | shadow_review daemon | every 15 min (env knob) | recent `dialog_messages` window | SKILL.md, lessons.md |
| 3 | extract daemon | every 10 min (env knob) | recent `dialog_messages` window | `extract_candidates` pending queue |
| 4 | candidate-reviewer daemon | every 1 h (env knob) | pending candidates queue | SKILL.md (create/patch) / notes / verbatim / reject |
| 5 | Curator daemon | every 7 days (env knob) | every existing lesson + recently-touched skill | REPORT-`<date>`.md (advisory) or direct PATCH/PRUNE/CONSOLIDATE |
| 6 | dialectic_miner daemon | configurable (env knob; 0=off) | recent `dialog_messages` — user replies + preceding-assistant context | `dialectic_observations` buffer |
| 7 | dialectic_validator daemon | configurable (env knob; 0=off) | buffered `dialectic_observations` | dialectic claims + evidence (support / contradict / supersede) via spawned opus child |

All five write into the universal Skill format (`SKILL.md` under each
known/configured skills root — `~/.claude/skills/`, `~/.codex/skills/`,
existing `~/.agents/skills/`, optional `THREADKEEPER_EXTRA_SKILLS_DIRS`,
plus the canonical `~/.threadkeeper/skills/` mirror), with
`~/.threadkeeper/lessons.md` as a CLI-agnostic fallback for clients
without a native skills loader (Gemini, Copilot, bare MCP).

#### 1. Auto-review on close_thread

When a closed thread is rich (≥5 notes, ≥2 insight/move),
`close_thread` spawns a slim child with `SKILL_REVIEW_PROMPT` + the
thread's notes. The prompt is rubric-form (Q1–Q5 yes/no) with explicit
positive examples for incident-vs-rule classification. The fork also
receives a "recently active skills" block so it prefers PATCHing
existing umbrellas over creating new ones (*active-update bias*).
Child appends a lesson via `lesson_append`, writes/patches a skill via
`skill_manage` or writes a skill file directly, then closes with
`mark_skill_materialized`. If `skill_path` points at a `SKILL.md` (or a
skill directory), thread-keeper immediately mirrors that whole skill
into every configured skills root. Opt in with
`THREADKEEPER_AUTO_REVIEW=1`.

#### 2. Shadow-review daemon

Every `THREADKEEPER_SHADOW_REVIEW_INTERVAL_S` seconds (default off,
900 = 15 min recommended) scans the diff of `dialog_messages` since
the last cursor **across all CLIs at once**. The window filters
internal review-child sessions (no self-pollution) and strips adapter
`[tool_result]` / `[tool_call]` noise (the "clean context" rule). If
≥500 chars of meaningful signal remain, spawns a slim observer child
that decides on class-level learning. It is single-flight across the shared
DB: if any shadow observer task is already running, the daemon does not spawn
another one and does not advance the cursor. Shadow observer children are
marked as spawned/background processes, so they cannot start their own shadow
daemon even if a CLI drops the no-embeddings env. Idempotent through
`events.kind='shadow_review_pass'`.

#### 3. Extract daemon

Every `THREADKEEPER_EXTRACT_INTERVAL_S` seconds (default off, 600 =
10 min recommended) scans recent `dialog_messages` with heuristic
matchers: locale-aware "I want / next time / always" patterns,
headers + insight markers, bullet regularities, and paraphrase
clusters via cosine ≥ 0.80. Each match enqueues a row in
`extract_candidates.status='pending'`. Same self-pollution filter as
shadow_review (internal review-child sessions excluded) plus
message-level noise filter (compaction summaries, SKILL.md
injections, subagent role prompts, test-runner log dumps).

Where shadow extracts CLASS-LEVEL durable rules, extract harvests
PER-INCIDENT decision-shaped utterances. Heuristic, not LLM —
findings get refined by loop 4.

#### 4. Candidate-reviewer daemon

Every `THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S` seconds (default off,
3600 = 1 h recommended) consumes the pending queue extract built up.
Spawns a slim LLM child that decides per candidate or per coherent
cluster:

- **SKILL.create** — class-level rule; merge 2-5 related candidates
  into one skill (active-update bias prefers PATCH over CREATE)
- **SKILL.patch** — refines a recently-active skill
- **SKILL.write_file** — adds `references/<topic>.md` under an
  existing umbrella
- **NOTE** — per-incident decision (requires `thread_id`)
- **VERBATIM** — user quote worth preserving in `brief()`
- **REJECT** — false positive that slipped past extract's filters

Hard limits: max 2 new skills per pass, `[PROTECTED]` (pinned +
foreground-authored) skills off-limits. Closes the gap between
heuristic harvest and SKILL.md materialization — previously pending
candidates accumulated indefinitely waiting for an agent to call
`accept_candidate()` manually.

#### 5. Autonomous Curator

Every `THREADKEEPER_CURATOR_INTERVAL_S` seconds (default off, 604800
= 7 days recommended) spawns a slim child that reviews the EXISTING
`lessons.md` + `skill_usage` inventory and writes
`~/.threadkeeper/curator/REPORT-<isodate>.md` with KEEP / PATCH /
CONSOLIDATE / PRUNE recommendations. Pinned and foreground-authored
entries are marked `[PROTECTED]` in the inventory so the curator
never proposes destructive changes against them.

Phase 1 is advisory-only (REPORT only); flip
`THREADKEEPER_CURATOR_DESTRUCTIVE=1` once trust builds to let the
child apply its own recommendations directly.

#### Honest take

What works **without** agent cooperation (passive, opt-in via env):

- Loop 2 (shadow), 3 (extract), 4 (candidate-reviewer), 5 (curator) —
  all run from the parent process, never require `note()` or
  `close_thread()` from the agent

What depends on the agent **calling tools explicitly**:

- Loop 1 (auto-review on close_thread) — only fires if the agent
  closes threads, which the audit shows agents focused on coding
  tasks rarely do
- Manual `skill_record(outcome='wrong')` — strongest feedback signal
  to the Curator, but agents need to remember to flag bad skills

The whole point of having five loops (not one) is graceful
degradation: even when agents don't actively contribute, loops 2-5
keep the library growing from passive observation of the dialog
stream.

### Dialectic user model

A model of you, accumulated as you use the agent. `dialectic_claim`,
`dialectic_evidence` (support / contradict),
`dialectic_synthesis`, `dialectic_supersede`. Honcho-inspired
**weighted, smoothed** ratio
`(Σw_support − Σw_contradict) / (Σw_support + Σw_contradict + 3)`
→ low / medium / high / disputed confidence.
Grouped by domain (style, values, workflow, ...) in `brief()`.

**Source-based evidence discount.** Each evidence row's effective weight
is `base_weight × discount(WRITE_ORIGIN)`. Foreground (direct user / human
signal) = 1.0. shadow_review / background_review / candidate_review /
curator review-forks = 0.5. Structural defence against self-confirmation
loops: a claim that surfaces in `brief()` and then gets "confirmed" by a
review-fork reading the same dialog can't ride that internal evidence
all the way to high confidence — internal evidence buys half as much.

**Discrete tier on each claim** — `hypothesis → observed → validated`
(plus `disputed`). Independent of the continuous confidence band; tier
is the **action-gating** signal:

- `validated` → agent applies by default (★ in brief)
- `observed`  → agent references and may mention the assumption (· in brief)
- `hypothesis` → active probe; surfaces in a separate `currently_testing`
  block so the agent watches the next user moves through that lens

Transitions are discrete events (`tier_promoted` / `tier_demoted` in the
`events` table) with timestamps for an auditable trail of when each
claim earned trust. Thresholds:

- `hypothesis → observed`: `w_support ≥ 2.0` (claim has real backing)
- `observed → validated`: `w_support ≥ 4.0` **and** no contradict in 14 days
- `validated → observed`: any recent contradict (demote on user pushback)
- any → `disputed`: `w_contradict > w_support`
- `disputed → hypothesis`: support overtakes contradict (recovery path)

### i18n bundle

All multilingual regex and prompt fragments live in
`threadkeeper/i18n.py` — the rest of the codebase stays English-only.
Currently ships ten locales: **English, Mandarin Chinese, Hindi,
Spanish, Portuguese, French, German, Arabic, Russian, Japanese**
(~82 % of the world's speakers).

Adding a new language is a two-file PR — see [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Configuration

The most-used env knobs (full list in `threadkeeper/config.py`):

| Knob | Default | Purpose |
|---|---|---|
| `THREADKEEPER_DB` | `~/.threadkeeper/db.sqlite` | SQLite file |
| `THREADKEEPER_AUTO_REVIEW` | "" (off) | auto-review on `close_thread` |
| `THREADKEEPER_SHADOW_REVIEW_INTERVAL_S` | 0 (off) | shadow daemon tick (s) |
| `THREADKEEPER_SHADOW_REVIEW_WINDOW_S` | 900 | sliding window for shadow scan (s) |
| `THREADKEEPER_EXTRACT_INTERVAL_S` | 0 (off) | extract daemon tick (s); 600 = 10 min recommended |
| `THREADKEEPER_EXTRACT_WINDOW_MIN` | 30 | sliding dialog window per extract pass (min) |
| `THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S` | 0 (off) | candidate-reviewer daemon tick (s); 3600 = 1h recommended |
| `THREADKEEPER_CANDIDATE_REVIEW_MIN` | 3 | min pending candidates before reviewer engages |
| `THREADKEEPER_CURATOR_INTERVAL_S` | 0 (off) | curator daemon tick (s); 604800 = 7d recommended |
| `THREADKEEPER_CURATOR_MIN_LESSONS` | 3 | min lessons before curator engages |
| `THREADKEEPER_CURATOR_DESTRUCTIVE` | "" (advisory) | when "1": curator child applies its own PATCH/PRUNE/CONSOLIDATE directly instead of writing advisory REPORT only |
| `THREADKEEPER_SPAWN_BUDGET_MB` | 3072 | combined child RSS cap (MB); 0 disables |
| `THREADKEEPER_MEMORY_GUARD_POLL_S` | 30 | server RSS guard tick (s); 0 disables |
| `THREADKEEPER_MEMORY_GUARD_WARN_MB` | 1536 | notify/log when a server crosses this RSS |
| `THREADKEEPER_MEMORY_GUARD_KILL_MB` | 3072 | SIGTERM server above this RSS; 0 disables killing |
| `THREADKEEPER_MEMORY_GUARD_AGG_WARN_MB` | 2048 | notify/request trim when all server RSS crosses this |
| `THREADKEEPER_MEMORY_GUARD_AGG_KILL_MB` | 3072 | under aggregate pressure, retire stale idle servers |
| `THREADKEEPER_MEMORY_GUARD_RECLAIM_MB` | 1024 | local RSS floor before warn-triggered self trim |
| `THREADKEEPER_MEMORY_GUARD_TARGET_SERVERS` | 1 | aggregate-pressure target after retiring stale idle servers |
| `THREADKEEPER_MEMORY_GUARD_RETIRE_IDLE_S` | 900 | heartbeat age before a non-self server is retireable |
| `THREADKEEPER_MEMORY_GUARD_RETIRE_LIVE` | "" (off) | allow retiring parent-alive MCP servers; off protects live clients |
| `THREADKEEPER_MEMORY_GUARD_NOTIFY` | "1" | send macOS desktop notification when possible |
| `THREADKEEPER_INGEST_INTERVAL_S` | 3 | transcript ingest tick (s) |
| `THREADKEEPER_NO_EMBEDDINGS` | "" | force-disable the embedding model (FTS5 + delegate only) |
| `THREADKEEPER_EMBED_BACKEND` | `onnx` | embedding runtime: `onnx` (fastembed, no PyTorch) or `sentence-transformers` (legacy fallback) |
| `THREADKEEPER_EMBED_MODEL` | `paraphrase-multilingual-MiniLM-L12-v2` | 384-dim cross-lingual embedding model |
| `THREADKEEPER_SPAWNED_CHILD` | "" | spawn-internal marker; disables autonomous daemons in children |
| `THREADKEEPER_SKILL_NUDGE_INTERVAL` | 10 | events between `skill_hint` nudges |
| `THREADKEEPER_DIALECTIC_MINE_INTERVAL_S` | 0 (off) | dialectic_miner daemon tick (s); 0 disables mechanical observation capture |
| `THREADKEEPER_DIALECTIC_VALIDATE_INTERVAL_S` | 0 (off) | dialectic_validator daemon tick (s); 0 disables LLM-driven claim synthesis |
| `THREADKEEPER_DIALECTIC_VALIDATE_MIN` | 5 | min buffered observations before validator engages |
| `THREADKEEPER_DIALECTIC_MAX_NEW_CLAIMS` | 3 | max new dialectic claims the validator may create per pass |

Persist them in `~/.threadkeeper/.env` (copy from `.env.example`) — one file,
read via pydantic-settings; real environment variables still override it.
Hot-config reload is
[tracked](https://github.com/po4erk91/thread-keeper/issues/2).

### Per-loop agent dispatch

By default every learning-loop spawn runs through the same CLI that
hosts thread-keeper — Opus-session ⇒ Opus spawn, Codex-session ⇒
Codex spawn, etc. Detection: process-tree walk at startup, cached for
the server lifetime. The MCP tool `spawn_status()` shows the live
resolution table.

Override per role in `~/.threadkeeper/.env` (there is no longer a `spawn.toml` —
all config lives in the one `.env`). Spawn routing uses nested `__` keys; dict
keys are lowercased:

```bash
# default agent for roles with no explicit pin ("" / unset = use the active CLI)
THREADKEEPER_SPAWN__DEFAULT=claude
# per-role CLI:  THREADKEEPER_SPAWN__LOOP__<ROLE>=<cli>
THREADKEEPER_SPAWN__LOOP__SHADOW_OBSERVER=claude   # heaviest reasoning → keep on Claude
THREADKEEPER_SPAWN__LOOP__CURATOR=codex            # weekly audit → Codex is fine
THREADKEEPER_SPAWN__LOOP__CANDIDATE_REVIEWER=auto  # "auto" = follow active CLI
# model pin per CLI or per role:  THREADKEEPER_SPAWN__MODEL__<KEY>=<model>
THREADKEEPER_SPAWN__MODEL__CLAUDE=opus
THREADKEEPER_SPAWN__MODEL__DIALECTIC_VALIDATOR=opus
```

Resolution per role: `SPAWN__LOOP__<role>` → `SPAWN__DEFAULT` → active CLI →
`claude`; `"auto"` (or unset) defers to the active CLI. Real environment
variables override the `.env`. Force host detection with
`THREADKEEPER_ACTIVE_CLI=claude`. See `.env.example` for the full knob list.

Adapters without headless support (Claude Desktop, VS Code) can't be
spawn targets — `spawn_status()` reports them as "no adapter" and any
override pointing at them falls back to the next priority level.

---

## Hygiene tools

Two tools keep the memory tidy — both default to `dry_run=True`, run
them with `dry_run=False` to apply:

- **`consolidate()`** — dedup near-identical notes (intra-thread cosine
  ≥ 0.95), deduplicate verbatim quotes, demote untouched-active threads
  to `idle` after 30 days, release orphaned thread claims.
- **`validate_threads()`** — heuristic triage of active threads with
  four categories (first match wins per thread):
  - `no_notes_old` — active with zero notes ≥ 7 days → close as abandoned.
  - `shipped` — last note matches a shipped-marker regex (EN+RU:
    shipped/fixed/works/passed/done/merged/закрыто/готово/сделано/…)
    and has settled ≥ 3 days → close with the last move as outcome.
  - `dropped_open_q` — last note is an `open_q` left unfollowed
    ≥ 14 days → close as dropped.
  - `stale_idle` — any active not touched in ≥ 30 days → demote to
    `idle` (not closed — revives on next `note()`).

  Idle threads are never touched. Tunable via `no_notes_days`,
  `shipped_settle_days`, `drop_open_q_days`, `stale_days`, and
  `shipped_markers` (comma-separated extra tokens).

---

## Telemetry

- **`mp_dashboard(window_days=7)`** — one-call rollup of the whole
  system, read-only. Three sections: **stores** (threads by state,
  notes/dialog/distill/concepts counts, skills + claims by tier,
  extract-candidate and evolve queues, probe/task counts), **loops**
  (how many times each autonomous daemon fired in the window vs 30 days,
  plus last-fire age), and **outcomes** (what those loops actually
  produced — skills materialized, tier promotions, candidate
  accept-vs-reject rate). Surfaces the gaps the point-tools can't:
  a loop firing constantly while its outcomes stay flat, or a queue
  backing up. Complements the per-loop `*_status` tools (`mp_health`,
  `spawn_budget_status`, `shadow_review_status`).

---

## Storage

`~/.threadkeeper/db.sqlite` (overridable via `THREADKEEPER_DB`). WAL
mode for multi-writer concurrency. Optional `notes_vec` / `dialog_vec`
HNSW indexes through `sqlite-vec` for sub-linear semantic search;
fallback to Python-side cosine when the extension is missing.

One file. Backup = `cp`. Wipe memory = `rm`.

Hooks and small runtime artifacts: `~/.threadkeeper/hooks/`.

---

## Embeddings

Semantic search runs `paraphrase-multilingual-MiniLM-L12-v2` (384-dim,
RU+EN+50 langs). The default backend is **fastembed / ONNX Runtime** — no
PyTorch. A model-loaded process sits at ~700 MB physical footprint
(~850 MB RSS), down from ~1.8 GB on the PyTorch backend.

A **sentence-transformers** (PyTorch) backend is kept as an opt-in fallback.
It is heavier (~1.8 GB RSS) and produces vectors that are *not numerically
identical* to the ONNX backend's, so switching backends warrants a recompute:

```bash
# Install the fallback runtime and switch to it:
pip install -e '.[semantic-st]'
export THREADKEEPER_EMBED_BACKEND=sentence-transformers

# After any backend switch, homogenize the stored corpus so queries and
# stored vectors live in the same space:
tk-migrate-embeddings --all          # or --notes-only / --dialog-only
tk-migrate-embeddings --dry-run      # report stale counts only
```

The migration is batched, resumable, and idempotent (a second run finds
nothing stale). Both backends emit 384-dim vectors, so the `vec0` schema is
unchanged.

---

## Verifying ingest across CLIs

```bash
python scripts/tk_verify_ingest.py
```

Walks every installed CLI adapter, parses recent transcripts in an
isolated tempdir DB, reports per-source message counts and any silent
parse failures. Read-only with respect to live state.

---

## Tests

```bash
pip install -e '.[semantic,dev]'
python -m pytest
```

495 tests passing on Python 3.11 / 3.12 / 3.13 (1 skipped). CI runs
the suite on every push and PR.

---

## Project layout

```
threadkeeper/
├── server.py             # MCP entry: python -m threadkeeper.server
├── _setup.py             # `thread-keeper-setup` installer
├── config.py             # env-driven defaults
├── db.py                 # SQLite schema + sqlite-vec loader
├── identity.py           # session, self-cid, daemon launchers
├── ingest.py             # adapter-driven transcript ingest
├── brief.py              # render_brief / render_context
├── shadow_review.py      # autonomous learning observer
├── i18n.py               # 10 locales of regex + prompt bundles
├── adapters/             # one file per supported CLI
│   ├── claude_code.py
│   ├── claude_desktop.py
│   ├── codex.py
│   ├── gemini.py
│   ├── copilot.py
│   └── vscode.py
└── tools/                # @mcp.tool entries — 89 of them
    ├── threads.py
    ├── peers.py
    ├── spawn.py
    ├── skills.py
    ├── dialectic.py
    ├── validate.py
    └── ...
```

Detailed map in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Open work in [docs/ROADMAP.md](docs/ROADMAP.md) and the
[Issues tab](https://github.com/po4erk91/thread-keeper/issues).

---

## Contributing

PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the project
map, test workflow, and recipes for adding a new CLI adapter or a new
locale. Look for the `good-first-issue` label.

---

## License

MIT — see [LICENSE](LICENSE).
