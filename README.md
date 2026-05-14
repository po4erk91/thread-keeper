# thread-keeper

[![tests](https://github.com/po4erk91/thread-keeper/actions/workflows/test.yml/badge.svg)](https://github.com/po4erk91/thread-keeper/actions/workflows/test.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CLIs](https://img.shields.io/badge/CLIs-Claude%20Code%20%7C%20Codex%20%7C%20Gemini%20%7C%20Copilot-green)](#multi-cli-integration)

A local MCP server that holds **persistent working memory across agentic CLI
sessions** — Claude Code, OpenAI Codex, Google Gemini, and GitHub Copilot
share one SQLite store, one set of threads, one learning loop, one user
model.

The brief format is dense — structural tags, opaque IDs, ~6 KB per
session-start injection. Optimized for agent consumption, not human reading.

---

## Why

Today every agent CLI starts cold. Context dies at session boundaries.
Skills you taught Claude don't transfer to Codex. Threads you closed in
yesterday's Gemini chat are invisible to today's Copilot.

thread-keeper is the substrate underneath:

- **One memory store** — threads, notes, verbatim quotes, dialectic claims
  about you. Survives session, restart, CLI swap.
- **One learning loop (hermes-style)** — closed threads with rich content
  spawn a background reviewer that materializes lessons into
  `~/.claude/skills/`. Skills then load in every CLI that respects the
  Claude skills convention (all four supported CLIs do).
- **Cross-session signaling** — broadcast / whisper / inbox / wait between
  concurrent sessions across different CLIs.

---

## Quickstart

```bash
git clone https://github.com/po4erk91/thread-keeper ~/thread-keeper
cd ~/thread-keeper
python -m venv .venv && source .venv/bin/activate
pip install -e .
pip install -r requirements-semantic.txt    # optional, recommended
thread-keeper-setup                          # auto-registers in every detected CLI
```

That's it. `thread-keeper-setup` is idempotent: it detects which of
Claude Code / Codex / Gemini / Copilot you have installed, registers the
MCP server in each one's config, copies hooks to `~/.threadkeeper/hooks/`,
and writes a managed instructions block into each CLI's per-user
instructions file (`CLAUDE.md` / `AGENTS.md` / `GEMINI.md` /
`copilot-instructions.md`).

Restart your CLI of choice. The SessionStart hook injects a brief on
first message; no manual `brief()` call required.

To preview without writing anything:

```bash
thread-keeper-setup --dry-run
```

---

## Multi-CLI integration

| CLI | MCP config | Instructions file | Hooks | Transcripts ingested |
|---|---|---|---|---|
| Claude Code | `~/.claude.json` `mcpServers` | `~/.claude/CLAUDE.md` | `~/.claude/settings.json` `hooks` | `~/.claude/projects/**/*.jsonl` |
| Codex | `~/.codex/config.toml` `[mcp_servers]` | `~/.codex/AGENTS.md` | not supported by the CLI | `~/.codex/sessions/**/rollout-*.jsonl` |
| Gemini | `~/.gemini/settings.json` `mcpServers` | `~/.gemini/GEMINI.md` | `~/.gemini/settings.json` `hooks` | `~/.gemini/tmp/<user>/chats/session-*.jsonl` |
| Copilot | `~/.copilot/mcp-config.json` `mcpServers` | `~/.copilot/copilot-instructions.md` | `~/.copilot/hooks.json` | `~/.copilot/session-store.db` (sqlite) |

All four CLIs read transcripts into the same `dialog_messages` table with
a `source` tag, so `dialog_search()` finds matches regardless of where the
conversation happened.

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

### Learning loop (hermes-style)

Two loops feed `~/.claude/skills/`:

- **Auto-review on close_thread** — when a closed thread is rich
  (≥5 notes, ≥2 insight/move), `close_thread` itself spawns a slim child
  with `SKILL_REVIEW_PROMPT` + the thread's notes. The child decides
  whether to write a skill, calls `skill_manage`, then
  `mark_skill_materialized`. Opt in with `THREADKEEPER_AUTO_REVIEW=1`.
- **Shadow-review daemon** — every `THREADKEEPER_SHADOW_REVIEW_INTERVAL_S`
  seconds (default off; 15 min recommended), scans the diff of
  `dialog_messages` since the last cursor across **all** CLIs. If the
  window has ≥500 chars of meaningful content, spawns a slim observer
  child that decides on class-level learning autonomously. Idempotent
  through `events.kind='shadow_review_pass'`.

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
pip install -r requirements-dev.txt
python -m pytest
```

358 tests passing on Python 3.11 / 3.12 / 3.13 (1 skipped). CI runs
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
│   ├── codex.py
│   ├── gemini.py
│   └── copilot.py
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
