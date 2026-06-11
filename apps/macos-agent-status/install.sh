#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

repo_root="$(cd ../.. && pwd)"
app_path="$("./build.sh")"
install_dir="$HOME/Applications"
installed_app="$install_dir/ThreadKeeperAgentStatus.app"
agent_plist="$HOME/Library/LaunchAgents/local.threadkeeper.agent-status.plist"

mkdir -p "$install_dir" "$HOME/Library/LaunchAgents"
rm -rf "$installed_app"
cp -R "$app_path" "$installed_app"

if ! command -v tk-agent-status >/dev/null 2>&1; then
  local_bin="$HOME/.local/bin"
  wrapper="$local_bin/tk-agent-status"
  python_bin="$repo_root/.venv/bin/python"
  if [[ ! -x "$python_bin" ]]; then
    python_bin="$(command -v python3 || true)"
  fi
  if [[ -n "$python_bin" ]]; then
    mkdir -p "$local_bin"
    cat > "$wrapper" <<SH
#!/usr/bin/env bash
cd "$repo_root"
exec "$python_bin" -m threadkeeper.agent_status "\$@"
SH
    chmod +x "$wrapper"
  fi
fi

cat > "$agent_plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>local.threadkeeper.agent-status</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string>
    <string>-a</string>
    <string>$installed_app</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>
PLIST

launchctl unload "$agent_plist" >/dev/null 2>&1 || true
launchctl load "$agent_plist"
open "$installed_app"

echo "$installed_app"
