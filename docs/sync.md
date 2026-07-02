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
  resource_controls, events, signals, tasks, extract_candidates. Keeping
  `events` local means its autoincrement id (the live-channel cursor) never
  collides across machines.
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
`sync_state` singleton. Triggers are suppressed while `sync_state.applying=1`
(set by `applying_guard` during a merge) so applying a peer's rows does not get
re-captured as a local write — this preserves the relayed row's origin/hlc and
keeps reconcile idempotent.

## Transport

Each instance runs a client daemon (`sync/daemon.py`) and an HTTP server
(`sync/server.py`), symmetric:

```
POST /sync/pull  {vv}       -> {vv, changes}   rows the caller is missing
POST /sync/push  {changes}  -> {applied}        merge the caller's changes
```

Bearer-token auth (`THREADKEEPER_SYNC_TOKEN`). Transport is plain HTTP intended
to run over an already-encrypted private network (WireGuard/OpenVPN/Tailscale
or LAN); TLS can be fronted or added later without protocol changes. NAT'd
peers need a relay/rendezvous (future). Self-healing: an unreachable peer just
retries next tick.

## Configuration (all OFF by default)

| env | meaning |
|---|---|
| `THREADKEEPER_SYNC_INTERVAL_S` | client reconcile tick; 0 = daemon off |
| `THREADKEEPER_SYNC_PEERS` | CSV of peer base URLs (`http://host:port`) |
| `THREADKEEPER_SYNC_LISTEN` | local `host:port` to serve; empty = no server |
| `THREADKEEPER_SYNC_TOKEN` | shared bearer token for peer auth |

Tools: `sync_status`, `sync_peers`, `sync_now`.

## Migration (opt-in, one-time, destructive)

The feature is dormant until `PRAGMA user_version >= SYNC_SCHEMA_VERSION`. The
additive schema (sync columns + tables) is applied automatically and is safe.
The **re-id** (INTEGER PKs → global TEXT ids, references rewritten in lockstep,
derived indexes rebuilt) is a separate opt-in script and is **never auto-run**:

```
tk-sync-migrate            # dry-run: shows the plan, writes nothing
tk-sync-migrate --apply    # backs up db.sqlite, then migrates; bumps user_version
```

`--apply` auto-backs-up `db.sqlite`, runs in a transaction, and is idempotent.
Run it with a fresh backup; installing the feature without migrating leaves an
existing user completely unaffected (sync stays off).

## Status / follow-ups

- Counter columns (`skill_usage.*_count`) merge by LWW for now (an increment on
  the losing side is dropped); a per-node partial-count CRDT is a follow-up.
- TLS termination in-process, mDNS/reachability-triggered sync, and a relay for
  NAT'd peers are follow-ups; the current MVP relies on a private network.
