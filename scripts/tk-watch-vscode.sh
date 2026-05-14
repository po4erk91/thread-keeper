#!/bin/bash
# tk-watch-vscode.sh — live debug watcher for the Claude VS Code extension.
#
# Tails every active "Claude VSCode.log" across all open VS Code windows
# and surfaces only the lines that matter for diagnosing crashes /
# `Unhandled case: [object Object]` banners. Run in a side terminal
# while you reproduce the bug.
#
# Patterns highlighted (with colored prefix):
#   [HOOK]    — anything about our thread-keeper hooks
#   [ERROR]   — extension-level errors and stream failures
#   [STREAM]  — stream stalls, idle partials, aborts
#   [API]     — request lifecycle around the error window
#
# Anything else is filtered out so the signal stays high.

set -u

LOG_ROOT="$HOME/Library/Application Support/Code/logs"
if [ ! -d "$LOG_ROOT" ]; then
  echo "VS Code log dir not found: $LOG_ROOT" >&2
  exit 1
fi

# Most recent log session directory (VS Code starts a new one per launch)
LATEST_SESSION=$(ls -1t "$LOG_ROOT" | head -1)
if [ -z "$LATEST_SESSION" ]; then
  echo "No VS Code log sessions found" >&2
  exit 1
fi

# All Claude VSCode logs in the latest session (one per open window)
mapfile -t LOGS < <(find "$LOG_ROOT/$LATEST_SESSION" \
  -path "*Anthropic.claude-code/Claude VSCode.log" 2>/dev/null)

if [ "${#LOGS[@]}" -eq 0 ]; then
  echo "No Claude VS Code logs in $LATEST_SESSION" >&2
  echo "(make sure VS Code is running with the Claude extension active)" >&2
  exit 1
fi

echo "Watching ${#LOGS[@]} log file(s) under $LATEST_SESSION"
for l in "${LOGS[@]}"; do
  echo "  - $l"
done
echo
echo "Press Ctrl-C to stop. Reproduce the crash now."
echo

# Color helpers (no-ops if not a TTY).
if [ -t 1 ]; then
  C_HOOK='\033[36m'    # cyan
  C_ERR='\033[31m'     # red
  C_STREAM='\033[33m'  # yellow
  C_API='\033[35m'     # magenta
  C_RST='\033[0m'
else
  C_HOOK=''; C_ERR=''; C_STREAM=''; C_API=''; C_RST=''
fi

# tail -F follows file rotations; -n 0 to only show NEW lines.
tail -n 0 -F "${LOGS[@]}" 2>/dev/null | grep --line-buffered -E \
  "Hook .*(success|fail|provided|parsed|validated|error)|Hook output|\
\[ERROR\]|Unhandled case|sdk_stream_ended|had_error|stream_idle|\
Bash tool error|tool_dispatch.*error|MCP server.*Failed|\
stall|stalled|abort|Successfully parsed and validated hook" \
| while IFS= read -r line; do
  ts=$(printf '%s' "$line" | awk '{print substr($0,1,19)}')
  body=$(printf '%s' "$line" | sed 's/^[0-9-]* [0-9:.]* \[info\] *//; s/^==> .* <==$//')
  case "$body" in
    *"Hook"*|*"hook"*)
      printf "${C_HOOK}[HOOK]${C_RST}    %s  %s\n" "$ts" "$body" ;;
    *"[ERROR]"*|*"Unhandled case"*|*"had_error"*|*"Failed"*)
      printf "${C_ERR}[ERROR]${C_RST}   %s  %s\n" "$ts" "$body" ;;
    *"stream_idle"*|*"stalled"*|*"abort"*|*"stall"*)
      printf "${C_STREAM}[STREAM]${C_RST}  %s  %s\n" "$ts" "$body" ;;
    *"tool_dispatch"*|*"Bash tool error"*)
      printf "${C_API}[API]${C_RST}     %s  %s\n" "$ts" "$body" ;;
    *)
      printf "          %s  %s\n" "$ts" "$body" ;;
  esac
done
