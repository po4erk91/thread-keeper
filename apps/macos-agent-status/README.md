# ThreadKeeper Agent Status

Small macOS menu-bar app for live thread-keeper autonomous learning loop status.

It polls `tk-agent-status --json` every 5 seconds and shows:

- running/enabled loop count in the menu bar,
- every autonomous learning loop,
- a stable role description for what each loop/agent is responsible for,
- running / idle / ready / off state,
- active loops first (`running`, then `ready`),
- last pass summary,
- backlog count,
- active spawned-child RSS when a loop has a worker running,
- macOS notifications for newly completed autonomous child tasks that produced
  a useful result.

The first poll primes the seen-result list, so the app does not notify for old
completed tasks that existed before it started.

## Automatic startup

On macOS, `python -m threadkeeper.server` installs and launches this app
automatically when the MCP server starts. The startup hook is idempotent: it
rebuilds only when the installed app is missing or older than the source,
registers the LaunchAgent, and opens the app if it is not already running.

Disable automatic startup with:

```sh
THREADKEEPER_MENUBAR_AUTO_LAUNCH=0
```

## Build

```sh
./build.sh
open build/ThreadKeeperAgentStatus.app
```

## Install at login

```sh
./install.sh
```

The app is installed to `~/Applications/ThreadKeeperAgentStatus.app` and a
LaunchAgent is registered at
`~/Library/LaunchAgents/local.threadkeeper.agent-status.plist`.

If `tk-agent-status` is not already installed on PATH, `install.sh` creates a
small fallback wrapper at `~/.local/bin/tk-agent-status` that runs the local
repo module through `.venv/bin/python`.

## Command lookup

The app looks for `tk-agent-status` in:

- `/opt/homebrew/bin/tk-agent-status`
- `/usr/local/bin/tk-agent-status`
- `~/.local/bin/tk-agent-status`
- `PATH` via `/usr/bin/env`

Set `THREADKEEPER_AGENT_STATUS_COMMAND` when launching the app if the command
lives somewhere else.
