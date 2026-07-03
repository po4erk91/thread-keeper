# ThreadKeeper Agent Status

Small macOS menu-bar app for live thread-keeper autonomous learning loop status.
The status-bar item itself is AppKit `NSStatusItem`; the popover content is
SwiftUI. That lets the app update the menu-bar image directly instead of relying
on SwiftUI `MenuBarExtra` label animation.

It polls `tk-agent-status --json` every 120 seconds and shows:

- an icon-only menu-bar status item, with loop counts in the popover and
  tooltip,
- a black chip icon while idle, replaced by fixed-center, synchronized spinning
  gear frames while at least one autonomous loop is running,
- every autonomous learning loop,
- a stable role description for what each loop/agent is responsible for,
- running / idle / ready / off state,
- active loops first (`running`, then `ready`),
- last pass summary,
- backlog count,
- active spawned-child RSS when a loop has a worker running,
- a power button that toggles `THREADKEEPER_DISABLE_BG_DAEMONS` in
  `~/.threadkeeper/.env` and requests a ThreadKeeper restart,
- a Clean memory button that runs `tk-agent-status --cleanup-memory`,
- a Settings gear that opens a separate `~/.threadkeeper/.env` editor with
  guided controls, exact dropdowns for spawn CLI/model choices, raw text
  editing, three saved presets, and Save & Restart,
- macOS notifications for newly completed autonomous child tasks that produced
  a useful result.

Status polling and cleanup commands run in the background, so opening the
popover does not wait for `tk-agent-status --json`.

The first poll primes the seen-result list, so the app does not notify for old
completed tasks that existed before it started.

The app also watches its own RSS and self-restarts when it crosses
`THREADKEEPER_MENUBAR_RESTART_RSS_MB` (1024 MB default, `0` disables). That
keeps the menu-bar helper from becoming the memory-pressure offender.

## Automatic startup

On macOS, `python -m threadkeeper.server` installs and launches this app
automatically when the MCP server starts. The startup hook is idempotent: it
rebuilds when the installed app is missing or its recorded source fingerprint no
longer matches the bundled/source Swift files, registers the LaunchAgent, and
restarts the app when a rebuild or stale running process means the menu-bar
process is still using older code.

Disable automatic startup with:

```sh
THREADKEEPER_MENUBAR_AUTO_LAUNCH=0
```

Tune or disable the widget self-restart threshold with:

```sh
THREADKEEPER_MENUBAR_RESTART_RSS_MB=1536  # MB; 0 disables
```

The Settings gear edits `~/.threadkeeper/.env` by default, or the path in
`THREADKEEPER_ENV_FILE` when the app was launched with that override. Save &
Restart writes the file, runs the safe cleanup command, and sends TERM to
running `threadkeeper.server` processes so MCP hosts reconnect with the new
environment. In the spawn routing controls, `antigravity` is the stored CLI
value and `agy` is only the executable alias; `gemini` remains available as the
legacy Gemini CLI adapter.

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
