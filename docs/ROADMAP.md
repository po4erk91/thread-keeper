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

**ACL (former Phase 4).** Single-user machine, everything shared. Not
critical. Drop from roadmap — don't do until a multi-user scenario
appears. Scope: L.

**Federation / sync (former Phase 5).** Cross-machine memory. Expensive
(CRDT, encryption, transport), benefit not proven. Currently work happens
from a single laptop, other machines observe via the `dialog_search`
index. Drop. Scope: XL.

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
