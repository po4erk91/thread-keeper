# Phase 1 — Single daemon-host + thin per-session servers

**Status:** design / approved shape, pending spec review
**Date:** 2026-07-07
**Roadmap:** audit Phase 1 ("единый daemon-host вместо «полный сервер на каждую сессию»").

## Problem

Every CLI session (Claude Code / Desktop, Codex, Antigravity, Gemini, Copilot,
VS Code) launches its own **full** MCP server: `python -m threadkeeper.server`
→ `mcp.run()`. In each such foreground process, `identity._ensure_session`
starts **all ~15 background daemons** (retention, curator, shadow, extract,
candidate-reviewer, probe, evolve reviewer/applier, dialectic miner/validator,
thread-janitor, spawn-budget, memory-guard, auto-update, skill-updater,
config-watcher) and the process lazily loads the ONNX embedding model
(fastembed + onnxruntime + tokenizers native thread pools).

Consequences observed in the audit:

- **~3.4 GB aggregate RAM** — N concurrent sessions ⇒ N heavy processes
  (~300–340 MB each: interpreter + package + FastMCP + embedding model + DB
  connections + 15 daemon threads).
- **reclaim-thrash** — `memory_guard` targets `TARGET_SERVERS=1` and SIGTERMs
  *idle* servers (heartbeat past `RETIRE_IDLE_S`), but a live session's server
  can't be killed (it owns the stdio channel), so the guard can never collapse
  to 1. Sessions come and go ⇒ constant spawn/retire churn.

Machine-wide `single_flight_lock(...)` (used by several daemons) and the
`daemon_state` cadence gate (#209) already ensure the *work* of some loops runs
once; but every server still **starts the threads**, **loads the model**, and
**is a full process**. The RAM and thrash come from N full processes, not from
duplicated loop work.

## Goal / success criteria

1. Exactly **one** heavy process per machine (the daemon-host) regardless of
   how many CLI sessions are open.
2. Per-session servers are **thin**: no daemon threads, no ONNX model; RSS a
   fraction of today's full server.
3. `memory_guard` no longer thrashes: nothing to aggressively retire; it
   supervises the single host instead.
4. **No CLI config change** — the launch command stays `python -m
   threadkeeper.server`.
5. Semantic search still works from a thin server (via the host), degrading to
   FTS only when the host is transiently unreachable.
6. Ships behind a flag, dark by default; reversible.

Non-goals (later phases): DB storage dedup (−1.3 GB, Phase 2), trigger-scoped
memory, durable work-ledger, event-sourced child resume. This spec is **only**
the process-topology split.

## Topology

```
CLI session A ─ python -m threadkeeper.server (THIN) ┐
CLI session B ─ python -m threadkeeper.server (THIN) ┼─ embed socket ─► daemon-host  (1/machine, headless)
CLI session C ─ python -m threadkeeper.server (THIN) ┘                     ├─ 15 daemons (the loops)
        └─────────── all read/write ──────────► shared SQLite (WAL) ◄──────┤─ warm ONNX model
                                                                           └─ embed socket listener
```

## Components (isolated units)

### 1. `threadkeeper/host.py` (new) — the daemon-host

- **Purpose:** own the background loops + the warm embedding model + the embed
  socket for one machine.
- **Entry:** `python -m threadkeeper.host` (also callable as `host.main()`).
- **Startup:**
  1. Acquire the machine-wide **host lock** (`single_flight_lock("daemon-host")`
     / flock on `~/.threadkeeper/host.lock`). If already held by a live host,
     exit 0 (idempotent — someone beat us).
  2. Set role = host (`THREADKEEPER_ROLE=host` in-process) so `embed_text()`
     uses the local model, not the socket.
  3. Start all 15 daemons (the exact `start_*_daemon()` calls currently in
     `identity._ensure_session`'s foreground branch).
  4. Bind the **embed socket** listener (below).
  5. Write/refresh a `presence` row with `client='daemon-host'` and heartbeat.
- **Lifecycle:** **always-on** (decided). The host persists after every CLI
  session closes — the loops (retention daily, curator weekly, probe, etc.)
  must run even with no active session; that is the product ("autonomous
  background agents"). No idle-exit. It exits only on SIGTERM (memory-guard
  restart) or host-lock loss.
- **Depends on:** `single_flight_lock`, the daemon modules, `embeddings`
  (local model), `db`.

### 2. Embed IPC — `threadkeeper/host_embed.py` (new)

- **Socket:** `~/.threadkeeper/host.sock` (unix domain; override
  `THREADKEEPER_HOST_SOCK`). Path lives next to the DB, so a custom
  `THREADKEEPER_DB` dir keeps host + servers co-located.
- **Wire format:** newline-delimited JSON, versioned.
  - request: `{"v":1,"op":"embed","texts":["...", "..."]}`
  - reply:   `{"v":1,"vectors":[[...],[...]]}` or `{"v":1,"error":"..."}`
- **Server side** (runs in host): a small threaded socket server; each request
  embeds via the local warm model and replies. Batch-first (`texts`) so ingest
  backfill and multi-text calls are one round-trip.
- **Client side** (runs in thin server): `embed_via_host(texts) ->
  list[vector] | None`. Connects with a short timeout; returns `None` on any
  failure (no host, timeout, malformed reply) so the caller can fall back.
- **Depends on:** stdlib `socket`, `json`. No new third-party dep.

### 3. `embeddings.embed_text()` — role-aware routing (change)

- Today: loads the local model lazily and embeds.
- New: branch on role.
  - **host role:** unchanged (local warm model).
  - **thin role:** call `embed_via_host(...)`. On `None`:
    **fallback = FTS-only** (decided) — the caller (`search`, `neighbors`, …)
    proceeds without a query vector and returns FTS results, logging a
    one-line "host unreachable, semantic degraded to FTS" note. A config knob
    `THREADKEEPER_THIN_EMBED_FALLBACK = fts | local` lets an operator opt into
    lazy-local ONNX instead, but `fts` is the default (keeps thin thin at the
    worst moment).
- Ingest embedding (content vectors) is produced by the host's ingest daemon,
  so thin servers never embed content — only, at most, a query.

### 4. `identity._ensure_session` — split daemon-start from session-register (change)

- Today (foreground branch): register presence **and** start all 15 daemons.
- New: register presence + heartbeat as today, but **do not** start daemons.
  Instead call `host.ensure_host_running()`:
  - Under the host lock: if a live host heartbeat exists, no-op. Else
    **spawn a detached** `python -m threadkeeper.host` via
    `subprocess.Popen(..., start_new_session=True)` with stdin/stdout/stderr
    redirected (own session, no inherited stdio), then return. The thin server
    never becomes the host itself (keeps the stdio server light and lets the
    host outlive the session).
- `BACKGROUND_DAEMONS_ALLOWED` semantics fold into role: daemons start **only**
  in the host process. Spawned review children stay daemon-free as today.
- **Manual pass triggers still work from thin servers.** MCP tools like
  `curator_run` / `shadow_review_run` / `evolve_apply` call the pass function
  synchronously, not the daemon thread; they remain callable from a thin server
  and stay single-flight-safe against the host's loops via the existing
  `single_flight_lock` + `daemon_state` gate — no double-run.

### 5. `memory_guard` — supervise, don't thrash (change)

- Thin servers are cheap (no ONNX, no daemons); drop them from the aggressive
  idle-retire path (still reap truly-dead rows).
- New responsibility: **host supervision** — if the host row is stale (no
  heartbeat past a TTL) or its RSS exceeds the kill threshold, SIGTERM it; the
  next thin-server tick re-spawns it via `ensure_host_running()`.
- `TARGET_SERVERS` reinterpreted: the host is the one heavy process; thin
  servers are ephemeral and light, not retire targets.

### 6. Config (change) — `threadkeeper/config.py`

- `THREADKEEPER_ROLE` = `server` (default) | `host`. Set to `host` by
  `host.main()`; thin servers keep `server`.
- `THREADKEEPER_DAEMON_HOST` = `0` (default, dark) | `1`. When `0`, everything
  behaves as today (each foreground server starts daemons; `embed_text` local).
  When `1`, the split above is active. **Rollout flag.**
- `THREADKEEPER_HOST_SOCK` (default `<db dir>/host.sock`).
- `THREADKEEPER_HOST_HEARTBEAT_TTL_S` (host liveness window for supervision).
- `THREADKEEPER_THIN_EMBED_FALLBACK` = `fts` (default) | `local`.

## Data flow

- **Light tool call** (brief/note/open_thread/core_*/FTS search): thin server →
  shared SQLite (WAL) directly. No host involvement. (Unchanged code paths;
  they just run in a process that didn't start daemons.)
- **Semantic search**: thin server → `embed_via_host(query)` over the socket →
  host embeds with warm model → vector back → thin server runs the vec0 search
  on the shared DB. Host down ⇒ FTS-only.
- **Content ingest / backfill embeddings**: host's ingest daemon reads new
  dialog rows and writes vectors — same as today, but now only in the host.
- **Loop work** (retention, curator, …): host only.

## Failure handling

- **No host at thin startup:** `ensure_host_running()` spawns one; the first
  semantic search in the race window falls back to FTS. Idempotent under the
  host lock (two thin servers racing ⇒ one host).
- **Host crash:** lock released; next thin-server `_ensure_session` (every new
  tool session) or `memory_guard` re-spawns it. Loops resume from
  `daemon_state` cadence (no double-fire).
- **Socket present but host dead** (stale sock file): client connect fails →
  `None` → fallback; supervision SIGTERM + respawn cleans the stale sock on
  next host bind (`unlink` before bind).
- **Flag off:** zero behavior change (current per-server daemons + local embed).

## Testing

- **Unit**
  - host lock election: two `ensure_host_running()` calls ⇒ exactly one spawn.
  - embed socket round-trip: host server + client `embed_via_host` returns a
    correct-dim vector; malformed/no-host ⇒ `None`.
  - `embed_text` routing: role=host → local; role=server → socket; socket
    `None` + fallback=fts → caller gets FTS path; fallback=local → lazy model.
  - thin `_ensure_session` starts **zero** daemons (assert no
    `start_*_daemon` called); host `main` starts all 15.
- **Integration**
  - spawn a host + 2 thin servers against a tmp DB: assert (a) each daemon
    ran once (single-flight/host-only), (b) a semantic search from a thin
    server returns host-embedded results, (c) `kill host` ⇒ next thin call
    re-spawns it and search recovers.
  - flag off ⇒ existing suite behavior unchanged.
- Reuse `fresh_mp` (thin server) + a new `fresh_host` fixture. All host/socket
  paths under `tmp_path`; no real `~/.threadkeeper`.

## Rollout

1. Land dark (`THREADKEEPER_DAEMON_HOST=0`), full test coverage.
2. Enable locally (`.env`), watch: one `daemon-host` process, thin server RSS,
   embed-socket latency, loop liveness, `memory_guard` no longer retiring.
3. Flip default to `1` in a later release once soaked.
4. Menubar/env-editor surfaces the flag + host status.

## Open sub-decisions (defaults chosen; flip at review)

- **Host shutdown:** always-on ✅ (vs idle-exit TTL).
- **Thin embed fallback:** FTS-only ✅ (vs lazy-local ONNX).

## Risks

- A detached host spawned from a stdio child must fully daemonize (no inherited
  stdio, own session) or a CLI closing could HUP it — covered by
  `start_new_session` + redirected fds.
- SQLite write concurrency: thin servers + host all write. Already WAL + the
  code already runs N writers today; net writer count does not increase.
- Socket security: unix socket under the user's `~/.threadkeeper` (0700 dir),
  local-only; no network surface.
