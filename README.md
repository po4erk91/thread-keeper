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
- **Self-improving skill library** — four autonomous background loops
  (auto-review on thread close, shadow-review daemon, extract
  harvester, weekly Curator) materialize class-level skills as the
  agents work. Adapted to multi-CLI:
  SKILL.md is the primary write target and gets mirrored to every
  detected CLI's skills directory simultaneously
  (`~/.claude/skills/`, `~/.codex/skills/`, `~/.threadkeeper/skills/`),
  with lessons.md as a fallback for CLIs without a native skills
  loader.

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

A daemon measures combined child RSS every 10 s; admission control
refuses a new spawn that would exceed `THREADKEEPER_SPAWN_BUDGET_MB`
(3 GB default). Slim children that need semantic search delegate to the
parent via `search_via_parent` — no per-child copy of sentence-transformers.

### Learning loops

Five loops materialize knowledge into Anthropic-style Skill files
(`SKILL.md` under each detected CLI's skills directory — Claude's
`~/.claude/skills/`, Codex's `~/.codex/skills/`, plus the canonical
`~/.threadkeeper/skills/` mirror) with a CLI-agnostic
`~/.threadkeeper/lessons.md` fallback for CLIs that don't auto-trigger
on the Skill format (Gemini / Copilot / bare MCP clients):

- **Auto-review on close_thread** — when a closed thread is rich
  (≥5 notes, ≥2 insight/move), `close_thread` spawns a slim child with
  `SKILL_REVIEW_PROMPT` + the thread's notes. The prompt is rubric-form
  (Q1–Q5 yes/no) with explicit positive examples for incident-vs-rule
  classification. The fork also receives a "recently active skills"
  block so it prefers PATCHing existing umbrellas over creating new
  ones (*active-update bias*). Child appends a
  lesson via `lesson_append`, optionally mirrors to
  `~/.claude/skills/<name>/SKILL.md`, then closes with
  `mark_skill_materialized`. Opt in with `THREADKEEPER_AUTO_REVIEW=1`.
- **Shadow-review daemon** — every `THREADKEEPER_SHADOW_REVIEW_INTERVAL_S`
  seconds (default off; 15 min recommended), scans the diff of
  `dialog_messages` since the last cursor across **all** CLIs. The
  window filters internal review-child sessions (no self-pollution)
  and strips adapter `[tool_result]` / `[tool_call]` noise (the
  "clean context" rule). If ≥500 chars of meaningful signal
  remain, spawns a slim observer child that decides on class-level
  learning. Idempotent through `events.kind='shadow_review_pass'`.
- **Extract daemon** — every `THREADKEEPER_EXTRACT_INTERVAL_S` seconds
  (default off; 10 min recommended), scans recent `dialog_messages`
  with heuristic matchers (locale-aware "I want / next time / always"
  patterns, headers + insight markers, bullet regularities, paraphrase
  clusters via cosine ≥ 0.80) and enqueues candidates in
  `extract_candidates.status='pending'`. The same self-pollution
  filter as shadow_review excludes internal review-child sessions.
  Where shadow extracts CLASS-LEVEL durable rules, extract harvests
  PER-INCIDENT decision-shaped utterances.
- **Candidate-reviewer daemon** — every
  `THREADKEEPER_CANDIDATE_REVIEW_INTERVAL_S` seconds (default off;
  1 h recommended), consumes the pending queue extract built up.
  Spawns an LLM child that decides per candidate or per coherent
  cluster: SKILL.create / SKILL.patch (active-update bias toward
  recently-touched skills) / SKILL.write_file as references/ sub-file
  / NOTE (with thread_id) / VERBATIM / REJECT. Closes the gap
  between heuristic harvest and SKILL.md materialization — previously
  pending candidates accumulated indefinitely waiting for an agent to
  call `accept_candidate()` manually, which audit showed agents rarely
  do.
- **Autonomous Curator** — every `THREADKEEPER_CURATOR_INTERVAL_S`
  seconds (default off; 7 days recommended), spawns a slim child that
  reviews the EXISTING `lessons.md` + `skill_usage` inventory and
  writes `~/.threadkeeper/curator/REPORT-<isodate>.md` with KEEP /
  PATCH / CONSOLIDATE / PRUNE recommendations. Pinned and
  foreground-authored entries are marked `[PROTECTED]` in the
  inventory so the curator never proposes destructive changes against
  them. Phase 1 is advisory-only — user reviews the REPORT and
  applies changes manually.

### Dialectic user model

A model of you, accumulated as you use the agent. `dialectic_claim`,
`dialectic_evidence` (support / contradict / clarifying),
`dialectic_synthesis`, `dialectic_supersede`. Honcho-inspired smoothed
ratio `(s-c)/(s+c+3)` → low / medium / high / disputed confidence.
Grouped by domain (style, values, workflow, ...) in `brief()`.

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
| `THREADKEEPER_INGEST_INTERVAL_S` | 30 | transcript ingest tick (s) |
| `THREADKEEPER_NO_EMBEDDINGS` | "" | force-disable sentence-transformers |
| `THREADKEEPER_SKILL_NUDGE_INTERVAL` | 10 | events between `skill_hint` nudges |

Persist them via `~/.claude/settings.json`'s `env` block (Claude Code) or
the equivalent env section in each CLI's config. Hot-config reload is
[tracked](https://github.com/po4erk91/thread-keeper/issues/2).

---

## Storage

`~/.threadkeeper/db.sqlite` (overridable via `THREADKEEPER_DB`). WAL
mode for multi-writer concurrency. Optional `notes_vec` / `dialog_vec`
HNSW indexes through `sqlite-vec` for sub-linear semantic search;
fallback to Python-side cosine when the extension is missing.

One file. Backup = `cp`. Wipe memory = `rm`.

Hooks and small runtime artifacts: `~/.threadkeeper/hooks/`.

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

412 tests passing on Python 3.11 / 3.12 / 3.13 (1 skipped). CI runs
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
└── tools/                # @mcp.tool entries — 83 of them
    ├── threads.py
    ├── peers.py
    ├── spawn.py
    ├── skills.py
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
