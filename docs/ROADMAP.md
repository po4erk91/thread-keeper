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

---

## Open

**More IDE / agent adapters — Cursor, Windsurf, JetBrains, Zed, etc.**
Current registry covers six clients (Claude Code / Claude Desktop /
Codex CLI + desktop / Gemini / Copilot / VS Code). The MCP ecosystem
is wider:

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

**Hot-config reload.** `settings.json` and env require server restart.
Ideally — pickup without restarting daemons. Scope: S (env via periodic
re-read in the config object, daemons already read per-tick).

**Telemetry dashboard.** `shadow_review_status`, `spawn_budget_status`,
`mp_health` provide point views. There's no aggregate across the whole
system: how many reviews passed per day, how many skills got
materialized, how many spawns rejected by budget, how notes/
dialog_messages grow. Scope: M — separate tool `mp_dashboard` or
periodic dump to file.

**Shadow-review proof in production.** The daemon just landed. Need an
observation period (two-three weeks) to understand — does it actually
find class-level patterns that auto-review on close_thread doesn't
catch, or does it duplicate work. After — decision on default
`SHADOW_REVIEW_INTERVAL_S` (currently 0 = disabled). Scope: S in volume,
M in time.

**Documentation.** README / ARCHITECTURE / this file — update now.
Going forward: keep in sync when the set of tools or daemons changes.
Scope: ongoing.

**Curator policy tuning.** `curator_run` currently archives by a simple
heuristic (time + absence of patches). Unclear whether this loses useful
skills. Need a dry-run mode with a dump of "what would be archived" for
review. Scope: S.

**Extract_recent UX.** `review_candidates` is rarely called. Hypothesis:
too many candidates or they're noisy. Need to either raise the
threshold, or make high-confidence candidates flow into notes
automatically (with rollback). Scope: S.

---

## Principle

Don't add phases for the sake of "architectural completeness". Each open
item above exists because there's a concrete gap in the current flow.
If an item becomes "open question without a gap" — drop it, don't defer.
