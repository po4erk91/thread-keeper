# Installation choices

ThreadKeeper offers a portable plugin and the existing adapter installer.
Both connect to the `thread-keeper` stdio MCP server, start with the `core`
tool profile, and provide the same memory lifecycle guidance. Choose one
registration method per host to avoid duplicate server connections.

| Host capability | Installation choice | Guidance and hooks |
|---|---|---|
| Agent Plugins 1.0 with Skills and stdio MCP | Release plugin directory | Bootstrap skill and MCP initialize instructions; no portable hooks |
| Supported ThreadKeeper adapter | pipx/uv and `thread-keeper-setup` | Managed instructions where supported; adapter hooks where supported |
| Generic MCP client without plugin loading | Manual MCP registration | MCP initialize instructions; consult the client's setup guide |

Agent Plugins 1.0 standardizes packaging, not installation commands. Check
your host's documentation for its supported version and plugin loader. Native
plugin formats such as `.claude-plugin/` do not by themselves confirm support
for the portable root `plugin.json`. The
[standard's client list](https://agent-plugins.org/compatible-clients) can help locate host
support; ThreadKeeper does not claim a tested plugin installation on every
listed client.

## Plugin installation

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and
   make `uvx` visible on the host application's PATH. A graphical host may
   need a restart after PATH changes. Python 3.11 or newer is required by
   ThreadKeeper; uv can provision a compatible interpreter.
2. Download `thread-keeper-agent-plugin-vX.Y.Z.zip` from the matching
   [release](https://github.com/po4erk91/thread-keeper/releases).
3. Extract to a directory. `plugin.json`, `mcp.json` and `skills/` must be
   directly inside that directory. Load it with the host's plugin manager.
4. Restart or reconnect the host. Use the `thread-keeper` bootstrap skill
   when beginning work. It explains brief/context, thread notes and lessons.

The first launch needs network access to install the exact release with the
`semantic` extra into uv's isolated tool cache. The plugin calls
`uvx --from 'threadkeeper[semantic]==X.Y.Z' python -m threadkeeper.server`.
It clears inherited `PYTHONPATH` and enables Python safe-path mode so another
checkout does not replace the selected server. No checkout-specific paths
or credentials appear in the manifests. Normal configuration and memory live
under `~/.threadkeeper` (or your configured storage profile), rather than a
host-owned plugin data directory, so both installation methods use the same
memory by default.

An installed Python release also exports a plugin without running adapter
setup:

```bash
thread-keeper-setup --export-plugin ./thread-keeper-plugin --dry-run
thread-keeper-setup --export-plugin ./thread-keeper-plugin
```

Use a fresh output directory. Export updates the three owned files and
preserves other files; it does not register MCP servers, install hooks or
edit host instructions. The same bundle is included under
`threadkeeper/assets/agent-plugin/` in wheels and source distributions.

## Adapter installation and coexistence

For hosts without portable plugin loading, continue to use:

```bash
pipx install 'threadkeeper[semantic]'
thread-keeper-setup --dry-run
thread-keeper-setup
```

Alternatively, use `uv tool install 'threadkeeper[semantic]'`. Setup still
registers every detected supported adapter, installs available hooks and
updates its marked instructions. Learned skills still mirror into the
existing native and configured skills roots with either installation method.
The plugin's bootstrap skill does not replace that learned-skill library.

When switching a host to the plugin, remove its previous `thread-keeper` MCP
entry through that host's configuration interface first. To switch back,
remove the plugin in that host, then rerun setup. Setup registers all detected
adapters; if other hosts keep using a plugin, remove their duplicate manual
entries afterwards. Existing adapter hooks may be retained deliberately; the
portable bundle neither installs nor removes them.

## Upgrades

Upgrade the plugin through its host manager, or replace its directory with
the next release bundle and reconnect. The manifest version and server
package pin advance together. In-process package self-updates are disabled
for the plugin so its server cannot move ahead of its packaged guidance.
This does not disable learned-skill updates.

Adapter installations retain their existing self-update behavior. For a
manual update, use `pipx upgrade threadkeeper` or `uv tool upgrade threadkeeper`,
then rerun `thread-keeper-setup` and reconnect. Updating a Python installation
does not replace a plugin directory already installed in a host; re-export
from the updated release and update that directory separately.

## Uninstall

Remove the plugin through the host manager and reconnect to stop its server
and unload its skill. For a manually loaded directory, unload it in the host
before deleting that directory. uv's cached environment can remain; cache
cleanup is managed separately by uv.

For an adapter installation, remove its `thread-keeper` MCP registration and
ThreadKeeper hook entries in each host, and remove only the instruction block
between the `THREADKEEPER:BEGIN` and `THREADKEEPER:END` markers. Then uninstall
the Python tool with `pipx uninstall threadkeeper` or
`uv tool uninstall threadkeeper`. There is no automatic setup uninstall flag.

Neither removal path deletes `~/.threadkeeper`, stored memory, backups, or
learned-skill mirrors. Keep those for another host or reinstall. Review and
remove them separately only if you intend to erase that data; other hosts
may still be using them.
