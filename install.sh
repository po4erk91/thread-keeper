#!/usr/bin/env bash
# thread-keeper one-line installer.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/po4erk91/thread-keeper/main/install.sh | bash
#
# With semantic-search deps (recommended for cross-language search):
#   curl -fsSL https://raw.githubusercontent.com/po4erk91/thread-keeper/main/install.sh | bash -s -- --semantic
#
# Custom install location (default ~/thread-keeper):
#   TK_HOME=/opt/thread-keeper curl ... | bash
#
# Idempotent — safe to re-run. On second run it git-pulls + reinstalls.

set -eu

TK_HOME="${TK_HOME:-$HOME/thread-keeper}"
TK_REPO="${TK_REPO:-https://github.com/po4erk91/thread-keeper}"
TK_BRANCH="${TK_BRANCH:-main}"
WITH_SEMANTIC=""
for arg in "${@:-}"; do
  case "$arg" in
    --semantic) WITH_SEMANTIC="1" ;;
    --no-semantic) WITH_SEMANTIC="" ;;
  esac
done

# ---- colored output helpers ----
if [ -t 1 ]; then
  CYAN=$'\033[36m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'
else
  CYAN=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi
say()  { printf "%s==>%s %s\n" "$CYAN"  "$RESET" "$*"; }
ok()   { printf "%s ✓%s %s\n" "$GREEN" "$RESET" "$*"; }
warn() { printf "%s ⚠%s %s\n" "$YELLOW" "$RESET" "$*"; }
die()  { printf "%s ✗%s %s\n" "$RED" "$RESET" "$*" >&2; exit 1; }

# ---- preflight ----

say "thread-keeper installer — target ${TK_HOME}"

command -v git >/dev/null 2>&1 || die "git not found. Install Xcode CLT or apt-get install git."

PYTHON_BIN=""
for cand in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
      PYTHON_BIN="$(command -v "$cand")"
      break
    fi
  fi
done
[ -n "$PYTHON_BIN" ] || die "Python 3.11+ not found. brew install python@3.13 (or distro equivalent)."
ok "python: $PYTHON_BIN ($("$PYTHON_BIN" --version))"

# ---- clone or update ----

if [ -d "$TK_HOME/.git" ]; then
  say "updating existing checkout: $TK_HOME"
  git -C "$TK_HOME" fetch --quiet origin "$TK_BRANCH"
  git -C "$TK_HOME" checkout --quiet "$TK_BRANCH"
  git -C "$TK_HOME" pull --quiet --ff-only origin "$TK_BRANCH"
elif [ -e "$TK_HOME" ]; then
  die "$TK_HOME exists and is not a git checkout. Move/remove it or set TK_HOME=other_path."
else
  say "cloning $TK_REPO → $TK_HOME"
  git clone --quiet --branch "$TK_BRANCH" "$TK_REPO" "$TK_HOME"
fi
ok "repo at $TK_HOME ($(git -C "$TK_HOME" rev-parse --short HEAD))"

# ---- venv ----

VENV="$TK_HOME/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  say "creating venv: $VENV"
  "$PYTHON_BIN" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
ok "venv ready"

# ---- editable install ----

if [ -n "$WITH_SEMANTIC" ]; then
  say "installing thread-keeper package with [semantic] extras"
  "$VENV/bin/pip" install --quiet -e "$TK_HOME[semantic]"
  ok "semantic search enabled"
else
  say "installing thread-keeper package (no semantic extras)"
  "$VENV/bin/pip" install --quiet -e "$TK_HOME"
  warn "semantic deps SKIPPED. Re-run with --semantic to enable cross-language search."
fi
ok "thread-keeper-setup on path: $VENV/bin/thread-keeper-setup"

# ---- wire into every detected CLI ----

say "registering MCP server in every detected CLI"
"$VENV/bin/thread-keeper-setup"

# ---- done ----

cat <<DONE

${GREEN}thread-keeper installed.${RESET}

Next steps:
  ${CYAN}1.${RESET} Restart your CLI of choice (Claude Code, Claude Desktop, Codex, Antigravity CLI/agy, Copilot, VS Code) — hook-capable clients auto-inject brief(); hookless clients should call brief() on the first message.

  ${CYAN}2.${RESET} (Optional, recommended) Enable the background learning daemons by adding to ~/.claude/settings.json under "env":
      "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "900",   # extract class-level skills every 15 min
      "THREADKEEPER_EXTRACT_INTERVAL_S": "600",         # harvest decision-shaped notes every 10 min
      "THREADKEEPER_CURATOR_INTERVAL_S": "604800",      # weekly skill-library audit

  ${CYAN}3.${RESET} Re-run this installer any time to update (it git-pulls + reinstalls).
DONE
