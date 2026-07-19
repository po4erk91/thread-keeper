# Cross-machine sync (active-active P2P)

Thread-keeper stores memory in one local SQLite file. This feature keeps that
memory synchronized across a user's machines (e.g. an always-on desktop + a
laptop), **active-active**: every machine writes autonomously — including
offline, with several agents at once — and the end state on every node is the
**union** of all write-sets, with concurrent edits resolved deterministically.

Topology is a **decentralized P2P mesh, no hub**: each instance holds a peer
list and reconciles with those peers. Peer lists may be partial/asymmetric —
because replication is transitive (below), any *connected* graph converges.
Adding a machine = add its address on some node; remove one and the rest keep
working.

> Postgres / a central server were rejected: TK is one process per machine, so
> there is no local write concurrency to solve, and a central server breaks the
> offline case (a laptop off-network would have no memory at all).

## Model

Each replicated row carries three sync columns (added additively, safe on any
DB): `origin_node` (who first wrote it), `hlc` (a Hybrid Logical Clock
timestamp), and `deleted` (tombstone). Rows are globally identified by a TEXT
id (the primary key).

- **HLC** (`phys_ms:counter:node_id`, zero-padded → lexically sortable) gives a
  total order that stays causally correct even when machines' wall clocks drift.
- **Version vector** = the highest HLC each node has seen *per origin*.
- **Anti-entropy**: peers exchange version vectors; each sends the other every
  row/tombstone whose HLC exceeds what the peer knows for that origin. Merge is
  **last-writer-wins by HLC**; deletes propagate as tombstones so a peer that
  still holds a row cannot resurrect it.
- **Transitive relay**: a row carries its *origin's* HLC, not the sender's — so
  B forwards A's rows to C with no direct A–C link. A connected graph converges.

Derived indexes (FTS5, sqlite-vec) are **never shipped** — each node rebuilds
them locally from the base rows after a merge. Embedding BLOBs are omitted from
the wire and recomputed locally (every node has the model).

### Table classes

- **Replicated** (memory): threads, notes, verbatim, dialog_messages,
  core_memory, concepts, distill, distill_votes, user_dialectic,
  dialectic_evidence, dialectic_observations, edges, skill_usage, reliability,
  probes, probe_results, evolve, style.
- **Node-local** (never synced): sessions, presence, cursors, ingest_state,
  resource_controls, events, signals, tasks, extract_candidates, daemon_state.
  Keeping `events` local means its autoincrement id (the live-channel cursor)
  never collides across machines; `daemon_state` is per-machine background
  cadence (single-flight claim timestamps) — meaningless to replicate.
- **Derived** (rebuilt locally): notes_fts, dialog_fts, notes_vec, dialog_vec,
  and the *_vec_map sidecars.

## Global ids

Replicated ids must be globally unique without coordination. Generated ids use
a **ULID** (`helpers.gen_global_id`, 48-bit ms + 80 random bits, time-sortable);
tables rebuilt from INTEGER PKs get a random-hex `DEFAULT` so inserts that omit
an id still get a global one. `dialog_messages.uuid` and natural keys
(`core_memory.key`, `style.key`, `skill_usage.name`, `reliability.category`)
are already global and merge by key.

## Capture

On a migrated DB, per-table triggers stamp `origin_node`/`hlc` on local writes
and append to `sync_oplog`. The HLC is advanced in pure SQL off the
`sync_state` singleton. During a merge the triggers are suppressed by
`applying_guard`, which creates a **connection-local `TEMP` table**
(`_tk_sync_applying`) that the triggers probe via `pragma_table_list` — so
applying a peer's rows does not get re-captured as a local write (preserving the
relayed row's origin/hlc and keeping reconcile idempotent), while a concurrent
local write on *another* connection is still captured normally. Suppression is
deliberately not a shared `sync_state` flag: a shared row would leak through an
inner commit and silently drop unrelated concurrent writes.

The receive path also absorbs the highest HLC it applies into the local clock
(`identity.hlc_absorb`, inside the same guarded transaction) so a subsequent
local edit is stamped from a clock that dominates everything just received —
otherwise a local edit after a clock-ahead remote row would lose under LWW.

## Transport

Each instance runs a client daemon (`sync/daemon.py`) and an HTTP server
(`sync/server.py`), symmetric:

```
POST /sync/pull  {vv}       -> {vv, changes}   rows the caller is missing
POST /sync/push  {changes}  -> {applied}        merge the caller's changes
```

Bearer-token auth (`THREADKEEPER_SYNC_TOKEN`), compared in constant time.
Transport is plain HTTP intended to run over an already-encrypted private
network (WireGuard/OpenVPN/Tailscale or LAN); TLS can be fronted or added later
without protocol changes. NAT'd peers need a relay/rendezvous (future).
Self-healing: an unreachable peer just retries next tick.

## Trust model

**Every peer is equal-trust.** There are no per-table or per-row ACLs: any peer
that holds the shared token can write **any** replicated row (LWW by HLC means a
peer can also overwrite or tombstone rows another node authored). The mesh
assumes all machines belong to the same user. Guard the token accordingly and
run only over a private/encrypted network.

Because the DB replicates full private transcripts (`dialog_messages`), the
server **refuses to bind a wildcard or public address by default** — `0.0.0.0`,
`::`, a public IP, or an unresolvable hostname are rejected; only loopback and
private/link-local addresses are allowed. Bind an explicit private/VPN address
(e.g. your LAN or `10.8.0.x` VPN IP), or set
`THREADKEEPER_SYNC_ALLOW_PUBLIC_BIND=1` to override — only behind your own
network controls.

## Configuration (all OFF by default)

| env | meaning |
|---|---|
| `THREADKEEPER_SYNC_INTERVAL_S` | client reconcile tick; 0 = daemon off |
| `THREADKEEPER_SYNC_PEERS` | CSV of peer base URLs (`http://host:port`) |
| `THREADKEEPER_SYNC_LISTEN` | local `host:port` to serve; empty = no server |
| `THREADKEEPER_SYNC_TOKEN` | shared bearer token for peer auth |
| `THREADKEEPER_SYNC_ALLOW_PUBLIC_BIND` | allow binding a wildcard/public listen address (default off) |

Tools: `sync_status`, `sync_peers`, `sync_now`.

## Migration (opt-in, one-time, destructive)

The feature is dormant until `sync_state.sync_schema_version >=
SYNC_SCHEMA_VERSION`. (The gate deliberately lives in `sync_state`, not
`PRAGMA user_version` — the core DB owns `user_version` as its own
schema-migration counter.) The additive schema (sync columns + tables) rides the
core schema versioning and is applied automatically and safely. The **re-id**
(INTEGER PKs → global TEXT ids, references rewritten in lockstep, derived indexes
rebuilt) is a separate opt-in script and is **never auto-run**:

```
tk-sync-migrate            # dry-run: shows the plan, writes nothing
tk-sync-migrate --apply    # snapshots db.sqlite, then migrates; sets the sync gate
```

`--apply` first writes a **consistent single-file snapshot** (`VACUUM INTO`,
which reads through a live connection so WAL-resident committed pages are
captured — a plain file copy could miss them), then migrates in a transaction,
and is idempotent. Installing the feature without migrating leaves an existing
user completely unaffected (sync stays off).

## Cloning a DB / replica identity

The `node_id` lives inside the DB (`sync_state`), so **copying a migrated
`db.sqlite` — or the whole `~/.threadkeeper` state directory — onto a second
active machine duplicates the sync identity.** Because the version vector is
`MAX(hlc)` per `origin_node`, two independent HLC streams under the same origin
are indistinguishable: once a peer sees the higher stream it assumes every lower
timestamp for that origin is already known, and a valid change from the other
machine can be dropped **forever** (data loss, not an LWW collision on one row).

The distinction that matters:

- **Restoring a backup as a replacement** for the same writer (old machine gone)
  may keep the identity — there is still only one live writer for it.
- **Creating a second, simultaneously-writable replica** from a DB/state-dir
  copy **must reset the clone's identity before it accepts any local write or
  sync**:

```
tk-sync-reset-node            # dry-run: prints old/new node id + HLC high-water
tk-sync-reset-node --apply    # assigns a fresh node_id on THIS (clone) DB
```

`--apply` runs in one exclusive transaction: it preserves the DB's existing HLC
high-water mark (so a post-reset local write still dominates anything already
observed, including a future-skewed remote HLC), changes only
`sync_state.node_id`, clears seen-cursor state (`sync_peer_vv`), and atomically
replaces the `node.id` mirror. Historical rows keep their `origin_node` — that
history is already present in the copied DB. An identity file/DB mismatch check
is defense-in-depth but cannot detect a full state-dir copy, so this explicit
reset is the required path.

## Status / follow-ups

- **Learning-loop double-processing.** Once `dialog_messages` replicates, each
  machine's shadow_review / extract / dialectic miners will independently
  process the *same* synced rows → the same lessons/claims minted on every node
  (N machines ≈ N× LLM spend on one window; write-time dedup soaks some of it).
  Planned fix: scope those loops to locally-ingested rows (`origin_node = self`),
  or replicate the loop cursors/claims so work is done once. The cursor/claim
  bookkeeping already lives in node-local tables, so this is additive.
- Counter columns (`skill_usage.*_count`) merge by LWW for now (an increment on
  the losing side is dropped); a per-node partial-count CRDT summed at read
  (G-Counter style) fits the schema as a follow-up.
- Synced rows arrive without embeddings (never shipped). `/sync/push` re-embeds
  only a bounded slice per request so it can't blow the client timeout on a large
  initial corpus; the background ingester drains the remainder in bounded ticks
  under `applying_guard`. All migrated-DB embedding backfills are derived-only:
  they preserve the row's `origin_node`/HLC and never append to the sync oplog.
  NULL embeddings are a valid eventual state until then.
- TLS termination in-process, mDNS/reachability-triggered sync, and a relay for
  NAT'd peers are follow-ups; the current MVP relies on a private network.
