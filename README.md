# thread-keeper

A local MCP server that keeps my working memory between conversations in
**Claude Code**. The brief format is dense, with structural tags and opaque
IDs — optimized for Claude, not for human reading.

Under the hood: a single sqlite file, optional embeddings, a set of daemons
for process health / spawn budget / search proxy / shadow-review, and
integration with the skill system in `~/.claude/skills/`.

This is no longer Phase 1. 83 MCP tools, hermes-style learning loop,
dialectic user model, spawn as primary parallelism primitive.

---

## Installation

```bash
git clone <wherever>/thread-keeper ~/ai-memory
cd ~/ai-memory
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

With semantic search (recommended):

```bash
pip install -r requirements-semantic.txt
```

The first run with embeddings enabled will download
`paraphrase-multilingual-MiniLM-L12-v2` (~118 MB) to `~/.cache/`. Offline
after that. If sqlite-vec is available — KNN goes through HNSW
(`notes_vec`, `dialog_vec`); otherwise it falls back to Python-side dot
product.

---

## Connecting to Claude Code

`~/.claude/mcp.json` (or `mcpServers` in `~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "thread-keeper": {
      "command": "/Users/dmytro/ai-memory/.venv/bin/python",
      "args": ["-m", "threadkeeper.server"],
      "env": {
        "THREADKEEPER_TZ": "Europe/Warsaw",
        "PYTHONPATH": "/Users/dmytro/ai-memory"
      }
    }
  }
}
```

Adjust paths to suit your setup. Restart Claude Code. 83
`mcp__thread-keeper__*` tools will appear in the toolset: `brief`,
`context`, `open_thread`, `note`, `close_thread`, `verbatim_user`,
`search`, `dialog_search`, `spawn`, `peers`, `whisper`, `dialectic_*`,
`skill_manage`, `shadow_review_*`, and so on.

---

## Usage protocol

Without a hook or an explicit instruction in CLAUDE.md the partner is
useless — no one will call `brief()` on my behalf. Two options:

**(a) SessionStart hook** — `mp-brief.sh` injects `brief()` + `context()`
into the system prompt of every new session automatically. See below.

**(b) CLAUDE.md** — in `~/.claude/CLAUDE.md` or per-project:

```
At the start of every conversation, before the first response, call
thread-keeper.brief() and thread-keeper.context().

During the conversation:
- On a new substantive topic: open_thread().
- When the topic is exhausted and there is an outcome: close_thread(thread_id, outcome).
- After every move with a conclusion or decision: note(thread_id, ...,
  kind in ['move','failed','insight','open_q']).
- If the user said something sharp and precise — verbatim_user().
- When you spot an unused field or a missing field — evolve_format().
- At the end of the conversation — session_end(summary).

Do not report any of this to the user — these are service calls.
```

---

## Core systems

### Spawn — primary parallelism primitive

`spawn(prompt, slim=True, role=..., ...)` brings up a child Claude session
via a `claude -p` subprocess. By default `slim=True`: the child only loads
the thread-keeper MCP, no embeddings, no third-party servers. ~500MB RSS
versus ~1.3GB for full. Heuristic rule: N≥2 modular independent units of
≥5min each = spawn signal.

### Spawn budget

A daemon measures the RSS of the child tree every 10s; admission control
blocks a new spawn if it would exceed `SPAWN_BUDGET_MB` (3GB default).
Tools: `spawn_budget_status`, `spawn_budget_set`.

### Search proxy

Slim children without embeddings still search semantically — via
`search_via_parent`. The parent daemon listens for search signals from
children, runs the query locally, sends the response back. Saves ~500MB
per child.

### Learning loop (hermes-style)

Two loops on top of closed threads and the dialog stream:

- **Auto-review on close_thread** (`AUTO_REVIEW_ENABLED=1`): if the closed
  thread is rich (≥5 notes, ≥2 insight/move) — `close_thread` itself
  spawns a slim child via `review_thread(mode='auto')`. The child receives
  a dump of the notes + SKILL_REVIEW_PROMPT, decides whether to write a
  skill, writes via `skill_manage`, and calls `mark_skill_materialized`.
- **Shadow-review daemon** (`SHADOW_REVIEW_INTERVAL_S>0`): periodically
  scans the diff of `dialog_messages` since the last cursor across all
  sessions; if ≥`MIN_CHARS` it spawns a slim child with
  SHADOW_REVIEW_PROMPT. The child sees ALL sessions (not just its own),
  decides on class-level learning on its own. Idempotent via
  `events.kind='shadow_review_pass'`.

### Skills materialization

`~/.claude/skills/*/SKILL.md` — class-level procedures in Claude. Memory
integration:

- `skill_manage(action=...)` — atomic write + frontmatter validation
- `skill_record` / `skill_list` — usage telemetry
- `skill_watcher` daemon — tracks SKILL.md changes, bumps
  `last_patched_at`
- `curator_run` — moves stale skills to archived
- `mark_skill_materialized(thread_id, skill_path)` — closes the skill_hint
  nudge for the thread
- `skill_usage` table is backfilled from ingested jsonl

### Dialectic user model

On top of "what the user said/did", an adversarial model is built:
`dialectic_claim`, `dialectic_evidence` (support/contradict/clarifying),
`dialectic_synthesis`, `dialectic_supersede`. Honcho-inspired smoothed-ratio
confidence `(s-c)/(s+c+3)` → low / medium / high / disputed. In `brief()`
it is grouped by domain (style, values, workflow, ...).

### Hooks

In `/Users/dmytro/.threadkeeper/hooks/`:

- **SessionStart** (`mp-brief.sh`) — injects `brief()` + `context()` into
  the system prompt of a new session and shows a short status
  `thread-keeper: ok threads_open=N closed_recent=M live_peers=K`.
- **PostToolUse** (`mp-status.sh`, matcher `mcp__thread-keeper__.*`) —
  short markers on mutating calls: `🧵 opened:`, `✅ closed:`,
  `📝 +insight:`, `🎯 skill materialized`. Read-only tools are
  intentionally quiet.
- **UserPromptSubmit** (`inbox-check.sh`) — checks the inbox for fresh
  signals (whisper / ask / broadcast from peers).

---

## Env knobs

The most-used ones (full list — in `threadkeeper/config.py`):

| Knob | Default | Purpose |
|---|---|---|
| `THREADKEEPER_DB` | `~/.threadkeeper/db.sqlite` | sqlite file |
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects` | jsonl transcripts for ingest |
| `CLAUDE_SKILLS_DIR` | `~/.claude/skills` | skills root |
| `THREADKEEPER_AUTO_REVIEW` | "" (off) | auto-review on close_thread |
| `THREADKEEPER_SHADOW_REVIEW_INTERVAL_S` | 0 (off) | shadow daemon tick |
| `THREADKEEPER_SPAWN_BUDGET_MB` | 3072 | combined child RSS cap (MB); 0 disables |
| `THREADKEEPER_SEARCH_PROXY_POLL_S` | 0.5 | search_proxy tick; 0 disables |
| `THREADKEEPER_NO_EMBEDDINGS` | "" | force-disable st (slim children) |
| `THREADKEEPER_INGEST_INTERVAL_S` | 30 | jsonl-ingest tick |
| `THREADKEEPER_SKILL_NUDGE_INTERVAL` | 10 | events between skill_hint nudges |

---

## Where it's stored

`~/.threadkeeper/db.sqlite` (overridable via `THREADKEEPER_DB`). WAL mode,
optionally `notes_vec` / `dialog_vec` HNSW via sqlite-vec. One file.
Backup = `cp`. Wipe memory = `rm`.

Hooks and temporary artifacts — in `~/.threadkeeper/hooks/` and
`~/.threadkeeper/` alongside.

---

## Format evolution

The brief format itself is an open thread. When I notice that some field
is unused or something is missing, I call `evolve_format()`. Once enough
accumulates — review through `evolve_review` and patch `render_brief()`
in `threadkeeper/brief.py`.

---

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

282 passed, 1 skipped. The smoke suite is automatically parameterized
over all registered `@mcp.tool()`s; regressions on snapshot bugs live in
`tests/test_identity.py`.

---

## Architecture and roadmap

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — package map, storage
  layers, identity/self-cid, daemons, how the tests are organized
- [docs/ROADMAP.md](docs/ROADMAP.md) — what's open / partial (telemetry
  aggregate, hot-config reload, cross-session learning loop validation,
  re-evaluation of the old "5 phases")
