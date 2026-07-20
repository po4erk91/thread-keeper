# Enabling cross-machine sync — operator runbook

A practical, step-by-step guide to turning on [active-active sync](sync.md)
between two or more machines, plus the real-world gotchas that bite during a
first rollout. Read [sync.md](sync.md) first for the design; this doc is the
"how to actually deploy it" companion.

> Sync is **off by default** and opt-in. Nothing here changes behavior until you
> run the migration and configure peers.

## TL;DR

1. Put **every** machine on the **same version** (schemas must match exactly).
2. Back up each DB.
3. With **all thread-keeper clients closed**, run `tk-sync-migrate --apply`.
4. Configure `THREADKEEPER_SYNC_*` in `~/.threadkeeper/.env` (listener + peers +
   shared token), restart.
5. Verify: `401`/`200` on `/sync/pull`, then a one-shot reconcile shows
   `pulled=/pushed=`, and both `origin_node`s appear on both machines.

---

## Topology

One connection performs a **full bidirectional merge** (the client both pulls
and pushes in one reconcile). So the minimal setup is:

- **Always-on machine → listener** (fixed address, runs the sync server).
- **Mobile machine → client** (connects to the listener on whichever network is
  up). It does **not** need to listen, and the listener does **not** need the
  client in its peer list.

That single client→listener link gives complete active-active replication.

---

## Step 1 — Put every machine on the same version

**Sync requires identical replicated-table schemas on all peers.** A version
skew is the #1 cause of a failed first sync (see [Gotchas](#gotchas)). Upgrade
every machine to the same release before migrating.

Find the interpreter that actually runs thread-keeper (source of truth), then
upgrade **that** install:

```bash
# which python runs the MCP server (Claude Code registration)
python3 -c "import json,os; d=json.load(open(os.path.expanduser('~/.claude.json'))); s=d.get('mcpServers',{}); tk=s.get('thread-keeper') or s.get('threadkeeper'); print(tk.get('command') if tk else 'NOT FOUND')"
TKPY="…/bin/python"     # ← the path printed above
"$TKPY" -c "import importlib.metadata as m; print(m.version('threadkeeper'))"
```

Upgrade by install type:

| Install | Upgrade command |
|---|---|
| **pipx** | `pipx install --force 'threadkeeper[semantic]'` |
| **uv tool** | `uv tool upgrade threadkeeper` |
| plain venv (has pip) | `"$TKPY" -m pip install --upgrade 'threadkeeper[semantic]'` |
| editable git checkout | `git pull && "$TKPY" -m pip install -e '.[semantic]'` |

> ⚠️ **Do not use `"$TKPY" -m pip …` on a pipx install** — pipx venvs have no
> `pip` inside them (`No module named pip`). This also means the built-in
> auto-updater (which shells out to `pip install --upgrade`) **silently fails**
> on pipx installs, so such a machine can sit on a very old version without any
> visible error. Use `pipx install --force` / `pipx upgrade`.

Confirm the version matches on **all** machines before continuing.

## Step 2 — Back up each DB

The re-id migration is destructive (it rewrites primary keys). It takes its own
backup, but take one yourself too — a consistent snapshot that captures the WAL:

```bash
"$TKPY" - <<'PY'
import sqlite3, os, time
db = os.path.expanduser("~/.threadkeeper/db.sqlite")
bk = db + ".pre-reid-" + time.strftime("%Y%m%d-%H%M%S") + ".sqlite"
c = sqlite3.connect(db); c.execute("PRAGMA wal_checkpoint(TRUNCATE)"); c.execute("VACUUM INTO ?", (bk,)); c.close()
print("backup:", bk)
PY
```

> Note: `VACUUM INTO` does not preserve `PRAGMA user_version`; the copy reads
> `user_version = 0`. That is harmless — schema migrations are idempotent and
> re-run cleanly from 0 if you ever restore.

## Step 3 — Quiesce, then run the migration

The migration **rebuilds tables**. Any concurrent write corrupts the rebuild, so
**every** thread-keeper consumer must be stopped first — not just one client.

```bash
# 1. Fully quit ALL thread-keeper clients: Claude Code, Claude Desktop,
#    VS Code (if wired), Codex/agy, any other agent.
# 2. Kill residual daemons/servers and confirm the DB is free:
pkill -f 'threadkeeper.host'; pkill -f 'threadkeeper.server'; sleep 1
lsof ~/.threadkeeper/db.sqlite 2>/dev/null | grep -i python || echo "DB is free"
# 3. Dry-run (writes nothing), then apply (auto-backs-up, opt-in, destructive):
"$TKPY" -m threadkeeper.sync.migrate --db ~/.threadkeeper/db.sqlite            # plan
"$TKPY" -m threadkeeper.sync.migrate --db ~/.threadkeeper/db.sqlite --apply    # go
```

Expected tail: `done. sync_schema_version=1. Sync is now enabled once peers +
listen are configured.` (First run may download the embedding model.)

Verify:

```bash
"$TKPY" - <<'PY'
import sqlite3, os
c = sqlite3.connect(os.path.expanduser("~/.threadkeeper/db.sqlite")); c.row_factory=sqlite3.Row
print("integrity:", c.execute("PRAGMA quick_check").fetchone()[0])          # ok
print("FK violations:", len(c.execute("PRAGMA foreign_key_check").fetchall()))  # 0
r = c.execute("SELECT sync_schema_version, node_id FROM sync_state WHERE id=1").fetchone()
print("migrated:", r["sync_schema_version"], "| node_id:", r["node_id"])    # 1 | N…
nid = c.execute("SELECT id FROM notes LIMIT 1").fetchone()
print("notes id is TEXT ULID:", nid and not str(nid[0]).isdigit())          # True
PY
```

> Each machine gets its **own** `node_id`. Do **not** run `tk-sync-reset-node`
> here — that is only for a DB that was physically **copied** from another
> machine (see [Cloning](sync.md#cloning-a-db--replica-identity)).

## Step 4 — Configure peers in `~/.threadkeeper/.env`

All config is `THREADKEEPER_*` keys read from `~/.threadkeeper/.env` (a dotenv,
`chmod 600`). Real environment variables override the file; the file overrides
defaults. The config watcher hot-reloads value changes, **but a listener that
was not already running only starts on the next process restart** — so restart
after editing.

**On the listener (always-on machine):**

```dotenv
THREADKEEPER_SYNC_TOKEN=<shared secret>
THREADKEEPER_SYNC_LISTEN=<host:port>          # a single bind address
```

`SYNC_LISTEN` binds **one** `host:port`. To serve several networks at once
(e.g. a LAN address and a VPN address that are both interfaces of this machine),
bind all interfaces:

```dotenv
THREADKEEPER_SYNC_LISTEN=0.0.0.0:8787
THREADKEEPER_SYNC_ALLOW_PUBLIC_BIND=1         # required: 0.0.0.0 is a wildcard
```

> The bind-hardening refuses wildcard/public binds unless
> `SYNC_ALLOW_PUBLIC_BIND=1`. `0.0.0.0` exposes the port on **every** interface —
> only do this if the port is **not** forwarded to the internet (e.g. a home box
> behind NAT). The token is the only auth; keep it strong, and consider a
> host-firewall rule restricting the port to your LAN/VPN subnets.

**On the client (mobile machine):**

```dotenv
THREADKEEPER_SYNC_TOKEN=<same shared secret, byte-for-byte>
THREADKEEPER_SYNC_INTERVAL_S=30
THREADKEEPER_SYNC_PEERS=http://192.168.2.10:8787,http://10.8.0.1:8787
```

`SYNC_PEERS` is a **CSV** — list **all** addresses the same listener is
reachable at (e.g. its LAN IP *and* its VPN IP). The client tries each every
tick; the reachable one syncs, the unreachable one is skipped and retried
(self-healing). This is what makes sync resume automatically when you move
between home Wi-Fi and VPN.

Restart the client (and the listener, if its listener wasn't up yet) so the
server binds and the daemon begins ticking.

## Step 5 — Verify

**Reachability + auth** (from the client):

```bash
TOKEN="<shared secret>"
for URL in http://192.168.2.10:8787 http://10.8.0.1:8787; do
  echo -n "$URL -> "
  curl -s -o /dev/null -w "%{http_code}\n" -X POST "$URL/sync/pull" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{}' --max-time 4 || echo down
done
# reachable network -> 200 ; unreachable -> 000/timeout ; wrong token -> 401
```

**One-shot reconcile** (definitive; no waiting for the daemon):

```bash
"$TKPY" -c "
from threadkeeper.sync.daemon import sync_with_peer
from threadkeeper import config as c
for peer in [p.strip() for p in c.SYNC_PEERS.split(',') if p.strip()]:
    try:
        pulled, pushed = sync_with_peer(peer)
        print(f'{peer}: pulled={pulled} pushed={pushed}')
    except Exception as e:
        print(f'{peer}: ERROR {type(e).__name__}: {e}')
"
```

The first run reports a large `pulled=/pushed=` (the full initial merge).

**Proof of union** (run on both machines):

```bash
"$TKPY" -c "import sqlite3,os; c=sqlite3.connect(os.path.expanduser('~/.threadkeeper/db.sqlite')); print('origins:', [r[0] for r in c.execute('SELECT DISTINCT origin_node FROM threads WHERE origin_node IS NOT NULL')])"
```

Both machines should list **both** `node_id`s. Done — active-active is live, and
the daemon keeps them merged every `SYNC_INTERVAL_S` while a thread-keeper
process is running on each.

---

## Gotchas

Things that actually went wrong during a first rollout, and how to handle them.

### 1. Schema drift between peers → `no column named X`

Sync applies a peer's rows with `INSERT OR REPLACE`, so **both DBs must have the
same columns** on every replicated table. A one-off reconcile fails with e.g.:

```
OperationalError: table dialectic_observations has no column named requeue_count
```

We hit this because one machine's DB had a column (`requeue_count`) the other's
did not — even though both ran the "same" version string. Root cause: the column
was added to the table's `CREATE TABLE` in `SCHEMA` but **not** mirrored into the
`ALTER TABLE ADD COLUMN` legacy-migration list, so a **pre-existing** DB never
gained it on upgrade (only brand-new DBs get columns from `CREATE TABLE`).

**Diagnose** the exact drift on the lagging machine (read-only; prints the fixes
it would need, using the running code's `SCHEMA` as the source of truth):

```bash
"$TKPY" - <<'PY'
import sqlite3, os
from threadkeeper import db
ref = sqlite3.connect(":memory:")
for stmt in db._iter_sql_statements(db.SCHEMA):
    try: ref.execute(stmt)
    except Exception: pass
live = sqlite3.connect(os.path.expanduser("~/.threadkeeper/db.sqlite"))
REPL = ("threads notes verbatim dialog_messages core_memory concepts distill "
        "distill_votes user_dialectic dialectic_evidence dialectic_observations "
        "edges skill_usage reliability probes probe_results evolve style").split()
def coldef(r):
    _, name, typ, notnull, dflt, pk = r
    s = f"{name} {typ or 'TEXT'}"
    if notnull: s += " NOT NULL"
    if dflt is not None: s += f" DEFAULT {dflt}"
    return s
alters = []
for t in REPL:
    try: exp = list(ref.execute(f"PRAGMA table_info({t})"))
    except Exception: exp = []
    if not exp: continue
    act = {r[1] for r in live.execute(f"PRAGMA table_info({t})")}
    alters += [f"ALTER TABLE {t} ADD COLUMN {coldef(r)};" for r in exp if r[1] not in act]
print("\n".join(alters) or "OK: schema matches code — no drift")
PY
```

**Fix**: run the printed `ALTER TABLE … ADD COLUMN …` statements on the lagging
machine (they are additive and safe — data is never lost), then re-run the
reconcile. Best prevention: keep every peer on the **same** version.

> **Recommendations for maintainers.** (a) Mirror every `CREATE TABLE` column
> into the additive legacy-migration list so pre-existing DBs converge on
> upgrade. (b) Make `protocol.apply_changes` tolerant of column drift — apply
> the **intersection** of source columns and the target table's columns (and log
> the dropped ones) instead of hard-failing the whole reconcile. That turns a
> hard stop into graceful, eventually-consistent degradation.

### 2. pipx auto-update silently stalls

On a pipx install the venv has no `pip`, so the auto-updater's
`pip install --upgrade` fails with `No module named pip` — quietly, on a
background thread. The machine can sit far behind (we found one stuck on `0.13.0`
while the rest were on `0.16.x`). Upgrade pipx installs with
`pipx install --force` / `pipx upgrade`, and check versions across machines
before enabling sync.

### 3. Quiesce **all** consumers before migrating

The re-id migration rebuilds tables; a concurrent writer corrupts it. On a
multi-client box (Claude Code + Claude Desktop + VS Code + a daemon-host) it is
not enough to close one — quit them all, `pkill` the residual
`threadkeeper.host`/`threadkeeper.server`, and confirm `lsof db.sqlite` is empty
before `--apply`.

### 4. Different machines are not clones

Two machines that each migrated their own DB have **distinct** `node_id`s — do
nothing special. `tk-sync-reset-node` is **only** for a DB that was physically
copied (file/state-dir copy) onto a second active machine, which would otherwise
share one identity and silently lose writes. See
[Cloning](sync.md#cloning-a-db--replica-identity).

### 5. Don't downgrade after migrating

Migration advances the schema. An older thread-keeper build refuses a
newer-than-it-supports schema (`database schema version N is newer than this
build supports`). Keep versions moving forward, aligned across peers.

### 6. Multiple installs on one machine

A single machine can accumulate several installs (pipx, `uv tool`, an editable
git checkout, …) with different versions, and PATH shims (`~/.local/bin/tk-*`)
may point at a **stale** one. If a helper like `tk-agent-status` errors on a flag
the menu-bar app passes, re-point the shims at the install the MCP server
actually runs (the one from `~/.claude.json`):

```bash
ln -sf "$(dirname "$TKPY")/tk-agent-status" ~/.local/bin/tk-agent-status
# …and the other tk-* scripts likewise
```
