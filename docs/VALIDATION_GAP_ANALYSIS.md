# Validation gap analysis

_Scope: validation across thread-keeper — input validation, schema/data
integrity, and the heuristic `validate_threads` triage tool. Method:
read of `tools/validate.py`, `tools/threads.py`, `db.py`,
`tools/consolidate.py`, `thread_janitor.py`, `tools/invariants.py`,
`tools/skills.py`, `nudges.py`, `config.py`. Code references are
`file:line` against the state of branch
`claude/validation-gap-analysis-qHHir`._

The project uses the word "validation" for three unrelated things. This
document covers all three and is explicit about which one each finding
belongs to:

1. **Tool-input validation** — does an `@mcp.tool()` reject malformed
   arguments? (e.g. unknown `note(kind=…)`, missing thread).
2. **Schema / data-integrity validation** — does the DB or a checker
   stop inconsistent rows from existing? (CHECK constraints, FKs,
   cross-table invariants).
3. **`validate_threads`** — the heuristic *thread-lifecycle* triage tool
   (`tools/validate.py`). Despite the name it validates **workflow
   state**, not data correctness.

---

## 1. Inventory — what validation exists today

### 1a. Schema-level (db.py)
Strong where present, but **inconsistently applied**. CHECK constraints
exist on:
- `threads.state IN ('active','idle','closed')` (db.py:59)
- `dialog_messages.speaker IN ('user','claude')` (db.py:81)
- `probes.grader IN ('regex','exact','manual')` (db.py:181-182)
- `probe_results.success IN (0,1)` (db.py:193)
- `distill.kind IN (...)` (db.py:231-233), `distill.confidence` (db.py:234-235)
- `dialectic_evidence.kind IN ('support','contradict')` (db.py:285)
- `concepts.confidence IN ('low','medium','high')` (db.py:217-218)

`REFERENCES` clauses are declared on `threads.parent_id`, `notes.thread_id`,
`edges`, `votes.distill_id`, `dialectic_evidence.claim_id`, etc.

### 1b. Tool-input (tools/*.py)
Per-tool, ad-hoc `return "ERR …"` strings. Present on:
- `open_thread` — parent existence (threads.py:67-69)
- `note` / `close_thread` / `mark_skill_materialized` — thread existence
  (threads.py:96, 123, 163)
- `evolve_review` — bad decision / missing id (threads.py:326, 332)
- `dialog_search` — bad mode (dialog.py:65)
- `skill_manage` — a **real** validator: name regex `^[a-z0-9][a-z0-9._-]*$`,
  name ≤64, description ≤1024, SKILL.md ≤100k, file ≤1 MiB,
  path-traversal block on subfolder writes (skills.py:51-126, 642).

### 1c. Heuristic / semantic
- `validate_threads` — four-category lifecycle triage (validate.py).
- `consolidate` — dedup notes, demote stale-active→idle, release orphan
  claims (consolidate.py).
- `find_invariants` — clusters recurring *assistant responses*
  (invariants.py); unrelated to data invariants despite the name.
- `find_missed_spawns` — workflow heuristic.

**Takeaway:** validation is real but lumpy. The skill subsystem is
rigorously validated; the thread/note core — the most-written tables — is
the least validated.

---

## 2. Findings (gaps), by severity

### 🔴 G1 — `note(kind=…)` is completely unvalidated
`note()` accepts any string for `kind` and writes it verbatim
(threads.py:83-103). The column has **no CHECK** —
`kind TEXT NOT NULL` only (db.py:72) — unlike every sibling enum column
in §1a. The documented vocabulary is `move|failed|insight|open_q`
(threads.py:86-87), but nothing enforces it.

**Impact (this is load-bearing, not cosmetic):**
- `validate_threads` matches `last_note["kind"] == "open_q"` *exactly*
  (validate.py:141). A note saved as `"question"`, `"openq"`, or
  `"open-q"` silently never qualifies for `dropped_open_q`.
- `_emit(f"note:{kind}")` (threads.py:113) creates an unbounded
  event-kind space. Anything counting `note:insight` / `note:move`
  (auto-review richness gate `≥2 insight/move`, nudge counters) miscounts
  when a model hallucinates `kind="decision"` / `"done"` / `"todo"`.
- The skill-harvest richness test in `auto_review_should_fire` keys off
  these kinds; a drifted vocabulary quietly starves the learning loop.

**Alternatives:**
- **(A) Schema CHECK** — `CHECK(kind IN ('move','failed','insight','open_q'))`.
  Cheapest, enforced at write, matches the codebase's own pattern. Risk:
  a hard failure mid-conversation if a model sends an unknown kind, and a
  migration must first audit existing rows.
- **(B) Normalize-and-map in `note()`** — coerce synonyms
  (`decision→move`, `question→open_q`, `bug/error→failed`) and fall back
  to `move` with a warning in the return string. Softer; preserves the
  write; keeps the enum clean. **Recommended** — agents fat-finger kinds
  and a hard reject wastes a turn.
- **(C) Both** — map in the tool, CHECK as backstop for direct writers
  (daemons insert notes directly, e.g. threads.py:186, shadow forks).

### 🔴 G2 — `idle_thread` returns false success on a bad id
`idle_thread` (threads.py:200-211) issues `UPDATE … WHERE id=?` with **no
existence check** and unconditionally returns `"ok"`. `note` and
`close_thread` both guard (threads.py:96, 123); `idle_thread` is the odd
one out. A typo'd / hallucinated thread id silently no-ops and reports
success — the agent believes it parked a thread it didn't.

**Alternative:** add the same
`if not conn.execute("SELECT 1 FROM threads WHERE id=?", …): return "ERR
thread_not_found=…"` guard. One line; closes the inconsistency. (Same
audit applies to any other UPDATE-only tool that reports `ok`
unconditionally — `idle_thread` is the clearest case.)

### 🟠 G3 — Empty `question` / `outcome` accepted
`open_thread(question="")` and `close_thread(thread_id, outcome="")`
write empty strings (threads.py:72-76, 126-129). An empty-outcome close
produces a closed thread the brief and the curator can't summarize, and
`validate_threads`' `shipped` path even has a special `"shipped (last_move
empty)"` branch (validate.py:130) — i.e. the codebase already knows empty
outcomes happen and patches around them downstream instead of rejecting
them at the source.

**Alternative:** reject empty/whitespace `question` and `outcome` (or for
`close_thread`, fall back to `last_move` when `outcome` is blank, making
the existing downstream patch unnecessary).

### 🟠 G4 — Declared foreign keys are not enforced
`get_db()` sets `PRAGMA journal_mode=WAL / synchronous=NORMAL /
busy_timeout` (db.py:440-442) but **never `PRAGMA foreign_keys=ON`**.
SQLite defaults FKs *off* per-connection, so every `REFERENCES` in the
schema is documentation only. Orphan rows are possible and, given the
aggressive auto-delete paths (curator archive, consolidate, thread
churn), likely: notes pointing at deleted threads, `dialectic_evidence`
pointing at superseded claims, `edges` pointing at gone endpoints.

**Alternatives:**
- **(A) Turn FKs on** (`PRAGMA foreign_keys=ON` in `get_db`). Correct
  long-term, but must be preceded by an orphan-cleanup migration or
  existing rows will start raising on cascading deletes. Also some deletes
  are intentionally soft (closed≠deleted), so `ON DELETE` behavior needs a
  per-table decision. Scope: M.
- **(B) Leave FKs off, add a periodic integrity checker** (see G7) that
  *reports* orphans instead of preventing them. Lower-risk, fits the
  existing "dry-run heuristic tool" idiom. Scope: S.
- Pragmatically: **(B) now, (A) as a tracked migration.**

### 🟠 G5 — Three tools own thread-staleness with incompatible windows
There is no single policy for "a thread has gone stale." Three independent
mechanisms overlap:

| Mechanism | Trigger | Action | Default window |
|---|---|---|---|
| `consolidate` `idle_stale` | active, untouched | → **idle** | 30d (`CONSOLIDATE_STALE_THREAD_DAYS`, consolidate.py:25) |
| `validate_threads` `stale_idle` | active, untouched | → **idle** | 30d (`VALIDATE_STALE_DAYS`, validate.py:49) |
| `thread_janitor` daemon | active **or idle**, untouched | → **close** | **1d** (`THREAD_IDLE_CLOSE_DAYS`, config.py:391-392) |

Two of these (`consolidate.idle_stale` and `validate_threads.stale_idle`)
are **the same query with the same threshold** — validate.py:22-23 even
admits "The companion `consolidate()` already covers idle_stale; we still
surface it here." Duplicated logic, two places to keep in sync.

Worse, when the janitor is enabled it closes anything idle >1d, so
`validate_threads`' gentler 3d/14d/30d categories almost never get a
chance to fire — the janitor has already closed the thread. The "idle as a
soft park" semantics that `validate_threads` and `consolidate` carefully
preserve are bulldozed by a daemon defaulting to a **1-day** close. The
windows (1d vs 14d vs 30d) were clearly chosen independently.

**Alternatives:**
- **(A) One policy module.** Extract a single `lifecycle_policy` with one
  threshold table; `validate_threads` is the dry-run previewer, the
  janitor is the applier, `consolidate` drops its duplicated `idle_stale`
  branch. Single source of truth. **Recommended.**
- **(B) Reconcile windows only** — at minimum, make the janitor's close
  window ≥ validate's idle window so demote-then-close is ordered, and
  document the intended ladder (active → idle@Nd → close@Md, M>N).
- **(C) Status quo + a doc note** — cheapest, but the 1-day janitor
  default will keep surprising anyone who reads `validate_threads`.

### 🟡 G6 — `validate_threads` "shipped" heuristic has no negation/recency guard
`shipped` closes a thread when the last note matches a marker regex
(validate.py:38-43, 124). The regex is word-boundary'd but **polarity-
blind**: "this is **not fixed** yet", "the fix **didn't work**", "almost
**done**", "tests should **pass** but currently **fail**" all match and,
after a 3-day settle, auto-close a thread that is the opposite of shipped.

Secondary: markers are hardcoded EN+RU (validate.py:38-43) while the
project ships a **10-locale** i18n bundle (`i18n.py`, README:366-373).
A shipped marker in Spanish/Chinese/etc. is invisible — inconsistent with
the rest of the multilingual surface.

**Alternatives:**
- Add a leading-negation guard (`\b(not|no|isn't|didn't|won't|fails?|
  broke|не|без)\b` within ~6 tokens before the marker) → demote the match.
- Require the marker to appear specifically on a `kind='move'` note (not a
  `failed`/`open_q`), once G1 makes kinds trustworthy.
- For high-value precision, replace the regex with a one-shot slim-spawn
  classifier (the project already spawns for shadow/extract review) —
  "is this thread actually shipped? y/n" over the last 1-2 notes.
- Source markers from `i18n.py` instead of hardcoding, to match the
  10-locale promise.

### 🟡 G7 — No data-integrity validator (the `validate_*` namespace is mis-occupied)
`validate_threads` validates workflow, `find_invariants` validates
*response* shape — **nothing validates the data**. There is no tool that
checks cross-table invariants that the schema can't express:
- closed thread whose `last_move` still describes in-progress work;
- `dialectic_evidence` weighted sums drifting from
  `support_count/contradict_count` observability counters
  (ARCHITECTURE.md:359-362 calls these out as separately maintained — a
  classic drift risk);
- `tasks.spawned_cid` with no matching `sessions` row;
- `events.target` pointing at a deleted thread/skill;
- `notes.embed_backend` mixing backends in one corpus after a switch
  without `tk-migrate-embeddings` having run (README:520-534 makes this a
  manual, forgettable step);
- embedding dimension mismatch between a query and stored BLOBs after a
  model change.

**Alternative:** a `validate_db(dry_run=True)` tool (or rename: the
`find_invariants` name should arguably belong here) that reports these in
the same dry-run idiom as `consolidate`/`validate_threads`, optionally run
by a low-frequency daemon. Pairs naturally with G4's option (B).

### 🟡 G8 — Manual-only, no provenance discount
`validate_threads` is invoked by hand; unlike shadow/extract/curator it
has no daemon (`thread_janitor` is the closest, but it *closes*, it
doesn't run validate's nuanced triage). So the careful 4-category logic
only helps users who remember to call it. Separately, the dialectic model
has a well-designed **source discount** for internal observations
(ARCHITECTURE.md:340-357); the thread validators have no analogous notion
— a `validate_close` is indistinguishable from a human close to every
downstream counter. Lower severity, but worth noting for symmetry.

---

## 3. What's missing entirely (not just weak)

1. **A schema-version / migration validator.** Migrations are additive
   (`CREATE TABLE IF NOT EXISTS`, ad-hoc `ALTER`); nothing asserts a live
   DB matches the expected shape, so a half-migrated DB fails at query
   time, not at startup.
2. **Write-time enum enforcement on the hot path** (notes.kind, G1).
3. **Referential integrity** (G4).
4. **A single staleness policy** (G5).
5. **A data-integrity checker** (G7).
6. **Negation/multilingual-aware shipped detection** (G6).
7. **Embedding-space consistency check** post backend/model switch (G7).
8. **Idempotency / false-success guards** on UPDATE-only tools (G2).

---

## 4. Prioritized recommendations

**Quick wins (S, low risk, high value):**
1. G2 — add existence guard to `idle_thread` (1 line, removes a
   false-success bug).
2. G1(B) — normalize `note(kind)` synonyms + fallback (protects the
   learning loop and `validate_threads`).
3. G3 — reject empty `question`/`outcome` (or fall back to `last_move`).
4. G6 — negation guard on the shipped regex (stops wrong-direction
   auto-closes).

**Design decisions needed (M):**
5. G5 — unify the three staleness mechanisms into one policy; reconcile
   the 1d/14d/30d windows. **This is the highest-leverage structural fix.**
6. G4 — orphan-cleanup migration → `PRAGMA foreign_keys=ON`.
7. G7 — add `validate_db()` integrity checker; consider it the rightful
   owner of the "invariants" name.

**Larger:**
8. G6 advanced — slim-spawn shipped classifier; i18n-sourced markers.
9. Schema-version validator at startup.

---

## 5. Open questions for the maintainer
- Is the 1-day `thread_janitor` close default intentional, or a holdover?
  It effectively overrides `validate_threads`/`consolidate` semantics
  (G5).
- Should unknown `note(kind)` hard-fail (CHECK) or soft-map (tool-level)?
  The answer depends on how much you trust models not to drift the
  vocabulary mid-session (G1).
- Are FKs off by design (soft-delete philosophy) or by omission (G4)?
  Determines whether the fix is "turn them on" or "report orphans."
