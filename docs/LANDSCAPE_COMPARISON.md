# Landscape comparison — thread-keeper vs the agent-memory field (June 2026)

_Companion to `VALIDATION_GAP_ANALYSIS.md`. Where that doc looked inward
(is the code correct?), this one looks outward: how does thread-keeper
compare to other projects in the same space, and what is it missing
relative to them? Sources are listed at the bottom; benchmark numbers are
as published by the vendors and should be read as such (self-reported,
varying setups)._

## The field, in four buckets

thread-keeper sits at the **intersection** of four product categories,
which is why no single competitor is apples-to-apples:

1. **Hosted memory layers** — mem0, Zep, Letta (MemGPT), Honcho, Memori,
   MemMachine, ByteRover. Cloud-first (most self-hostable), LLM-based fact
   extraction, benchmarked on LoCoMo / LongMemEval / BEAM.
2. **Local MCP memory servers** — official `Knowledge Graph Memory`
   (Anthropic/modelcontextprotocol), basic-memory, memory-graph, mem0's
   **OpenMemory MCP**, CaviraOSS **OpenMemory**. Local-first, MCP-native,
   cross-tool.
3. **User-modeling layers** — Honcho (dialectic Theory-of-Mind user
   representation). thread-keeper's dialectic module is explicitly
   inspired by this.
4. **Self-improving skill systems** — Voyager (the origin) and the 2026
   research wave: MUSE-Autoskill, Autoskill, Skill1, MemSkill. Skill
   library + creation/evolution/evaluation loop.

thread-keeper is the only project I found that tries to do **all four at
once, locally, across multiple agent CLIs**. That breadth is its identity
— and also why it's behind the specialists on each individual axis.

## Feature matrix

| Capability | thread-keeper | mem0 / OpenMemory | Zep (Graphiti) | Letta | Honcho | Official MCP KG / basic-memory | Voyager-lineage research |
|---|---|---|---|---|---|---|---|
| Deployment | **Local, 1 SQLite file, no infra** | Cloud + self-host (Docker+Qdrant) | Cloud + self-host | Cloud + self-host | Cloud + OSS | Local file | Research code |
| Transport | MCP stdio | API + OpenMemory MCP | API | API/framework | API | MCP | n/a |
| Memory model | threads/notes + embeddings + concepts/edges | LLM facts, vector(+graph) | **bi-temporal knowledge graph** | **OS-style tiers (core/recall/archival)** | dialectic user representation | entity/relation/observation graph | skill bank |
| Fact extraction | **regex + cosine (heuristic)** + 1 LLM review pass | **LLM single-pass hierarchical** | LLM + graph episodic | LLM self-edit | **fine-tuned ToM model** | manual (agent calls tools) | LLM |
| Conflict / update of facts | append-only + dedup; no UPDATE/INVALIDATE | **ADD/UPDATE/DELETE/NOOP** | **temporal invalidation (valid_from/to)** | LLM rewrites memory | contradiction in representation | overwrite | n/a |
| Ingest mode | **passive (parses CLI jsonl transcripts)** | pull (agent calls add) | pull | pull/managed | pull | **pull only** | n/a |
| User model | dialectic claims, weighted ratio + tiers (heuristic) | basic prefs | entities | persona memory | **dialectic ToM (deepest)** | none | none |
| Self-improving skills | **5 autonomous loops → portable SKILL.md** | none | none | none | none | none | **skill evolve + eval/RL (research)** |
| Multi-agent coordination | **spawn / broadcast / whisper / inbox / swarm** | shared store only | shared store | multi-agent framework | peers model | none | none |
| Cross-CLI breadth | **6 clients, mirrored skills + shared memory** | OpenMemory: Claude/Cursor/Codex/… (memory only) | n/a | n/a | n/a | per-client | n/a |
| Published recall benchmark | **none** | LoCoMo 92.5 / LongMemEval 94.4 | LoCoMo 79.8 | reported | reported | none | task-success metrics |
| Auth / multi-tenant | none (single-user) | yes (cloud) | yes | yes | yes | none | n/a |

## Where thread-keeper is genuinely differentiated (the moat)

1. **Passive ingest of real agent transcripts.** Almost every MCP memory
   server (official KG, basic-memory, OpenMemory) is **pull-only**: the
   agent must *call* `add_memory`/`create_entities`, and the audit in this
   very repo shows agents focused on a task rarely call memory tools.
   thread-keeper parses `~/.claude/projects/**/*.jsonl` (and the Codex /
   Gemini / Copilot equivalents) and learns *without cooperation*. This is
   underrated and rare.
2. **A self-improving skill library, not just a memory store.** mem0 /
   Zep / Letta / Honcho / OpenMemory all store **facts/memories**. None of
   them materialize **reusable, portable skills** (`SKILL.md`,
   agentskills.io-compatible) and mirror them across CLIs. The only
   comparable work is **academic** (Voyager → MUSE-Autoskill / Skill1).
   thread-keeper is the closest *production* analogue of the Voyager skill
   library, aimed at coding agents. **This is the real differentiator** —
   it's a different product category from the memory layers.
3. **Multi-agent swarm coordination fused with memory.** The memory
   products are single-agent; agent *frameworks* have coordination but not
   this shared substrate. `spawn`/`broadcast`/`whisper`/`inbox`/`wait` over
   one store is unusual.
4. **Zero-infra local.** OpenMemory needs Docker + Qdrant; mem0/Zep want a
   cloud key. thread-keeper is one SQLite file, `cp` to back up, `rm` to
   wipe. For a privacy-/cost-sensitive single user this wins outright.
5. **Provenance discount (anti self-confirmation).** The dialectic
   evidence discount for internal review-forks is a thoughtful guard most
   systems lack entirely.

## Where thread-keeper is behind (deep gap analysis)

### 🔴 GAP-1 — No retrieval benchmark at all
The entire commercial field competes on **LoCoMo / LongMemEval / BEAM**
(mem0 self-reports 92.5/94.4; Zep 79.8 LoCoMo; MemMachine ~0.92;
ByteRover 92.2). thread-keeper publishes **495 unit tests and zero recall
numbers**. There is no evidence its hybrid FTS+cosine `search()` returns
the right notes — only that it returns *something*. **This is the single
biggest credibility gap.** Borrowing the LoCoMo harness (1,540 Q over
single/multi-hop/temporal recall) would be high-ROI and is mechanically
feasible against the existing `notes`/`dialog_messages` store.

### 🔴 GAP-2 — No temporal / bi-temporal knowledge graph
Zep's 15-point LongMemEval lead over mem0 (in the older benchmark round)
came from a **temporal knowledge graph** (Graphiti): facts carry
`valid_from`/`valid_to`, get **invalidated** when superseded, and support
"what was true at time T" queries. thread-keeper has `concepts` + `edges`
(`link`/`unlink`/`neighbors`) but it is **static**: no entity extraction,
no time-validity, no automatic invalidation. Questions like "what did the
user prefer *before* they changed their mind" can't be answered. This is
the deepest *architectural* gap relative to the state of the art.

### 🟠 GAP-3 — Heuristic fact extraction
mem0's 2026 algorithm is **single-pass hierarchical LLM extraction**;
Honcho uses a **fine-tuned ToM model**. thread-keeper's front-end
`extract_recent` is **regex + cosine** and its own ROADMAP measured **~5%
precision** (1 accept / 107 decisions). The `candidate_reviewer` adds an
LLM pass *after* the queue, but the heuristic front-end is the field's
weakest link. The architecture (cheap heuristic → LLM refine) is
defensible for cost, but the heuristic stage needs either a similarity
classifier or to be demoted to a pre-filter feeding an LLM extractor.

### 🟠 GAP-4 — No fact-level conflict resolution
mem0 has explicit **ADD / UPDATE / DELETE / NOOP** memory operations with
conflict detection; Zep invalidates superseded edges; Honcho models
contradictions. thread-keeper notes are **append-only** with only
`consolidate()` cosine-dedup. There is no "this new fact *updates* that
old one." (This is the same hole flagged in `VALIDATION_GAP_ANALYSIS.md`
G7 — no data-conflict detection.) The dialectic module *does* handle
contradiction, but only for **user-model claims**, not general facts.

### 🟠 GAP-5 — Memory tiers are declared, not implemented
thread-keeper borrows Letta's tier vocabulary (`core_memory` =
"Letta-tier RAM") but ARCHITECTURE.md admits the **tier policy (eviction,
promotion/demotion) is not implemented** — it's a flat key/priority store.
Letta's whole point is the **LLM autonomously managing** what moves
between core/recall/archival. thread-keeper's `core_set`/`core_get` are
manual. So it has the label without the mechanism.

### 🟡 GAP-6 — Shallower user model than its inspiration
Honcho does dialectic user modeling with a **fine-tuned ToM model + LLM
reasoning** ("memory as a reasoning problem, not a retrieval problem").
thread-keeper's dialectic is a **weighted smoothed ratio + discrete
tiers** — cheaper and fully local, but heuristic, with no genuine
ToM reasoning step. Fine for "user prefers X" tallies; weaker for inferring
*why* or resolving subtle belief contradictions.

### 🟡 GAP-7 — No skill-efficacy evaluation loop
The 2026 research (MUSE-Autoskill, Skill1) closes the loop with
**evaluation/RL**: did using a skill actually improve task outcome?
thread-keeper's skill tiers track `foreground_use_count` / `wrong_count`
(a lightweight proxy) but never attribute a **task outcome** to a skill.
It already has `probes` + `reliability` infrastructure — wiring "skill
used → subsequent probe/outcome" would turn the tier signal from
usage-counting into efficacy-measuring.

### 🟡 GAP-8 — No hosted / auth / multi-tenant, no token-cost discipline
Acknowledged in ROADMAP. Additionally, mem0 reports **<7k tokens per
retrieval** as a headline metric; thread-keeper's brief is ~6 KB but there
is **no measured retrieval-cost budget** — relevant because the brief is
injected at *every* SessionStart across *every* CLI.

### ⚪ Note — "cross-CLI memory" is no longer unique
When thread-keeper was conceived, cross-tool memory was novel. As of 2026
**OpenMemory MCP** (mem0) and **CaviraOSS OpenMemory** explicitly do
local cross-tool memory across Claude / Cursor / Copilot / Codex /
Windsurf. thread-keeper's edge is no longer "memory crosses CLIs" — it's
**"skills + swarm coordination + passive learning cross CLIs."** The
positioning should shift accordingly; the bare memory-sync story is now
commoditized.

## What to borrow (prioritized)

1. **A LoCoMo/LongMemEval harness** (GAP-1). Highest ROI: turns "we have
   tests" into "we recall at X%." Even a mediocre first number is more
   credible than none.
2. **LLM extraction + conflict ops** on the note/memory path (GAP-3,
   GAP-4), mem0-style ADD/UPDATE/DELETE/NOOP, with the current regex as a
   cheap pre-filter. Directly closes the validation G7 hole too.
3. **A temporal layer on concepts/edges** (GAP-2): `valid_from`/`valid_to`
   + supersession — a "Graphiti-lite." Biggest architectural lift but the
   biggest recall payoff on temporal queries.
4. **Skill-efficacy attribution** (GAP-7) via the existing
   `probes`/`reliability` tables — cheap, and it's the thing the *research*
   frontier says matters most for self-improving agents.
5. **Reposition** (GAP-cross-CLI): lead with the **self-improving skill
   library + swarm**, not with cross-CLI memory sync, which competitors now
   match.

## One-line verdict
thread-keeper is **not** a better mem0/Zep — its memory-recall core is
behind the specialists (no benchmark, no temporal graph, heuristic
extraction). It is a **different and largely unoccupied product**: a
local, zero-infra, **self-improving skill library + multi-agent swarm**
that learns passively from real agent transcripts across many CLIs. The
roadmap should double down on that moat (skills, swarm, passive learning)
and borrow the field's table-stakes (a recall benchmark, LLM extraction
with conflict resolution) rather than try to out-graph Zep.

---

### Sources
- [5 AI Agent Memory Systems Compared (2026)](https://dev.to/varun_pratapbhardwaj_b13/5-ai-agent-memory-systems-compared-mem0-zep-letta-supermemory-superlocalmemory-2026-benchmark-59p3)
- [AI Agent Memory 2026: Mem0 vs Zep vs Letta vs Cognee](https://dev.to/agdex_ai/ai-agent-memory-in-2026-mem0-vs-zep-vs-letta-vs-cognee-a-practical-guide-cfa)
- [Agent Memory at Scale 2026 — Letta, Zep, Mem0, LangMem](https://agentmarketcap.ai/blog/2026/04/10/agent-memory-vendor-landscape-2026-letta-zep-mem0-langmem)
- [State of AI Agent Memory 2026 (mem0)](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- [AI Memory Benchmarks in 2026 (mem0)](https://mem0.ai/blog/ai-memory-benchmarks-in-2026)
- [Introducing OpenMemory MCP (mem0)](https://mem0.ai/blog/introducing-openmemory-mcp)
- [CaviraOSS/OpenMemory (GitHub)](https://github.com/CaviraOSS/OpenMemory)
- [Knowledge Graph Memory MCP Server (Anthropic)](https://github.com/modelcontextprotocol/servers/tree/main/src/memory)
- [Honcho (Plastic Labs) — docs](https://docs.honcho.to/) · [GitHub](https://github.com/plastic-labs/honcho)
- [Honcho Review 2026](https://andrew.ooo/posts/honcho-plastic-labs-agent-memory-review/)
- [Voyager: Open-Ended Embodied Agent (arXiv 2305.16291)](https://arxiv.org/abs/2305.16291)
- [MUSE-Autoskill: Self-Evolving Agents (arXiv 2605.27366)](https://arxiv.org/html/2605.27366v1)
- [MemSkill: Evolving Memory Skills (arXiv 2602.02474)](https://arxiv.org/pdf/2602.02474)
