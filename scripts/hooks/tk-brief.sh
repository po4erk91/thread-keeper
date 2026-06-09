#!/bin/bash
# thread-keeper: SessionStart hook
#
# Injects a lean brief() into the new session's system prompt as
# additionalContext. Replaces the CLAUDE.md instruction "call brief()
# at start of every conversation" with a deterministic pre-loaded
# snapshot. Lean mode (THREADKEEPER_BRIEF_LEAN below) drops the nudge/meta
# sections from this once-per-session injection; data sections are kept.
#
# Also surfaces live_status() output when brief reports live=N>0, so
# concurrent-session activity is visible immediately.
#
# Failures are silent (return empty additionalContext) — the agent will
# still work, just without the pre-loaded brief.

set -u

# Resolve venv + package root. Order: env override → ~/.threadkeeper/path
# config → caller's $0 traversal. Fail silently so a broken install never
# blocks Claude Code session start.
PKG_ROOT="${THREADKEEPER_REPO:-$HOME/ai-memory}"
VENV_PY="${THREADKEEPER_PYTHON:-$PKG_ROOT/.venv/bin/python}"

# Bail silently if venv missing — don't break new sessions over a broken
# thread-keeper install.
if [ ! -x "$VENV_PY" ]; then
  exit 0
fi

# Run a lean brief() and (when live>0) live_status() in one python
# invocation. We split output into two parts:
#  - STATUS\t<line>  → compact visible status (shown to user via systemMessage)
#  - CONTEXT\t<rest> → full brief, injected into system prompt
# Any exception inside python is swallowed so the hook never blocks startup.
OUTPUT=$(
  # THREADKEEPER_BRIEF_NO_THREAD_NUDGE: this CLI has hooks, so the
  # open-thread reminder is delivered by tk-thread-nudge.sh (UserPromptSubmit)
  # instead — suppress the in-brief copy so it doesn't double-fire.
  PYTHONPATH="$PKG_ROOT" THREADKEEPER_BRIEF_NO_THREAD_NUDGE=1 THREADKEEPER_BRIEF_LEAN=1 "$VENV_PY" - <<'PY' 2>/dev/null
import re, sys
try:
    from threadkeeper.tools.threads import brief
    b = brief()  # SessionStart fires BEFORE the user's first message, so no query yet
    # context() dropped: its sess/sem/db/thread-count line duplicates brief's
    # ctx header. The context() MCP tool stays callable for explicit use.
    parts = [b]

    # Build compact visible status line. Parsed straight from brief output,
    # no IDs cited (per user_facing_style — paraphrase only).
    #
    # Two distinct counters from brief's ctx line (do not conflate):
    #   live=N   → unread cross-session events (broadcast/whisper/note backlog)
    #   peers=N  → distinct other session_ids writing to dialog_messages
    #              in last 5 min (the actual "who's alive next to me")
    live_n = 0
    peers_n = 0
    m = re.search(r"live=(\d+)", b or "")
    if m:
        live_n = int(m.group(1))
        if live_n > 0:
            try:
                from threadkeeper.tools.peers import live_status
                ls = live_status(advance_cursor=False)
                if ls and "no_fresh_events" not in ls:
                    parts += ["", "live_status (peek):", ls]
            except Exception:
                pass
    m_peers = re.search(r"peers=(\d+)", b or "")
    if m_peers:
        peers_n = int(m_peers.group(1))

    # Count active / recently-closed threads, orphan mp processes.
    threads_open = len(re.findall(r"^\s+T[a-f0-9]{3}\s+q=", b or "", re.M))
    threads_closed_recent = len(
        re.findall(r"^\s+T[a-f0-9]{3}\s+out=", b or "", re.M)
    )
    orphans = 0
    try:
        from threadkeeper.process_health import scan
        procs = scan()
        orphans = sum(1 for p in procs if p.get("is_orphaned"))
    except Exception:
        pass

    parts_status = [
        "thread-keeper: ok",
        f"threads_open={threads_open}",
        f"closed_recent={threads_closed_recent}",
        f"live_peers={peers_n}",
        f"unread_events={live_n}",
    ]
    if orphans > 0:
        parts_status.append(f"⚠️ orphan_procs={orphans}")
    status_line = "  ".join(parts_status)

    # Emit on two well-delimited lines so the shell can split.
    sys.stdout.write("STATUS\t" + status_line + "\n")
    sys.stdout.write("CONTEXT\t" + "\n".join(p for p in parts if p is not None))
except Exception:
    pass  # silent — hook must never crash session start
PY
)

# Empty output → no additionalContext at all (hook is a no-op for this run)
if [ -z "$OUTPUT" ]; then
  exit 0
fi

# Split into status (one line) and context (rest)
STATUS_LINE=$(printf '%s\n' "$OUTPUT" | awk -F'\t' '/^STATUS\t/ { sub(/^STATUS\t/, ""); print; exit }')
CONTEXT_BODY=$(printf '%s\n' "$OUTPUT" | awk -F'\t' '
  /^CONTEXT\t/ { sub(/^CONTEXT\t/, ""); inblock=1; print; next }
  inblock { print }
')

# Emit hook protocol JSON.
#
#   1. `hookSpecificOutput.additionalContext` — full brief into model
#      context (invisible to user, drives behavior). The status line is
#      folded in as a first line so no info is lost.
#   2. `systemMessage` — short, user-visible chip in chat ("🧵
#      thread-keeper: 8 threads open, 0 live peers"). This signals
#      keeper IS alive on every new session — without it the user sees
#      a normal chat with no indication that memory is loaded. Toggle
#      off by setting THREADKEEPER_VISIBLE_STATUS="" if it breaks any
#      client.
#   3. 32 KB cap on additionalContext — picky UI parsers choke on huge
#      injections.
python3 -c '
import json, os, sys
status = sys.argv[1]
ctx = sys.argv[2]
combined = (f"[{status}]\n\n" if status else "") + ctx
MAX = 32 * 1024
if len(combined) > MAX:
    combined = combined[:MAX] + "\n…[truncated by tk-brief hook]"
out = {
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": combined,
    }
}
# User-visible chip — opt-out via env var. Prefix with 🧵 so it reads
# as a thread-keeper signal at a glance.
visible = os.environ.get("THREADKEEPER_VISIBLE_STATUS", "1")
if visible and status:
    out["systemMessage"] = "🧵 " + status
print(json.dumps(out))
' "$STATUS_LINE" "$CONTEXT_BODY"
