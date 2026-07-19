# ThreadKeeper Agent Status

Small macOS menu-bar app for live thread-keeper autonomous learning loop status.
The status-bar item itself is AppKit `NSStatusItem`; the popover content is
SwiftUI. That lets the app update the menu-bar image directly instead of relying
on SwiftUI `MenuBarExtra` label animation.

It polls `tk-agent-status --json` every two minutes and shows:

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
- a Settings gear that opens a separate `~/.threadkeeper/.env` editor with a
  sidebar for CLI Agents, Learning Loop Agents, System Automation, Memory &
  Budgets, and Advanced raw editing,
- runtime model catalogs from installed CLIs, with installed and latest official
  cloud versions, source, freshness, error/fallback state, a manual refresh
  action, and a confirmed Update button only when the versions differ,
- per-agent CLI, provider-filtered model, effort, inherited effective values,
  schedule, and read/write-impact controls; mechanical jobs never show model
  or effort fields,
- Codex effort choices filtered by the selected model's advertised reasoning
  levels; unsupported saved/custom effort remains visible with a warning,
- authoritative current-runtime chains separated from pending `.env` previews;
  process-environment overrides are called out because saving the file cannot
  supersede them, and an unpinned role remains dynamic to its active host CLI,
- dropdown-only guided controls: schedules are labelled in hours, known
  models/efforts/limits are selectable without free-form fields, and existing
  custom values are preserved as `.env` entries for Advanced editing,
- compact presets, raw unknown-key preservation, one primary Save Changes
  action, and secondary Save & Restart,
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
environment. In spawn routing, `antigravity` is the stored CLI value and `agy`
is only its executable alias. Gemini Legacy is unsupported; old raw keys are
preserved and shown as warnings instead of silently acting.

Guided controls and Advanced `.env` are two views of one reconciled draft:
switching sections or saving cannot overwrite newer edits from the other view.
Unknown keys, comments, ordering, and duplicate canonical assignments remain
in place; active legacy `AGY` model keys are migrated to canonical
`ANTIGRAVITY` without leaving a second active alias. Refreshing model catalogs
does not discard unsaved model or effort selections. Role effort assignments
remain part of the canonical draft even when their CLI is not known until the
catalog arrives. Active spawn overrides from the process environment (including
Pydantic's `THREADKEEPER_SPAWN={...}` JSON form) are shown explicitly because
they take precedence over edits saved to `.env`.

Reloading the file or loading a preset asks before discarding an unsaved draft;
closing and reopening the retained settings window keeps that draft in memory.
When an agent-specific model is not advertised by its newly selected CLI, the
value remains editable as custom but the card shows an explicit provider-
compatibility warning.

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
