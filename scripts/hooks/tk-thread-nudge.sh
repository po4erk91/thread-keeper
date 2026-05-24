#!/bin/bash
# thread-keeper: UserPromptSubmit hook — open_thread safety net.
#
# Backstops the CLAUDE.md rule "new substantive topic → open_thread()".
# That rule was prose-only with nothing watching for compliance, so under
# task focus the agent would dive into work without ever opening a thread.
#
# Behaviour: ONCE per session, on the first user prompt, if no thread has
# been opened yet this session, inject a reminder as additionalContext
# (model-visible, non-blocking). Stays silent for the rest of the session
# as soon as either (a) open_thread fires — tk-status.sh writes the
# `.opened` marker — or (b) it has nudged once (`.nudged` marker).
#
# Per-session markers live under ~/.threadkeeper/state/ keyed by the
# hook payload's session_id. Parsing/output use python3 (always present —
# thread-keeper is a python package) to avoid a jq dependency.

set -u

STATE_DIR="${THREADKEEPER_STATE_DIR:-$HOME/.threadkeeper/state}"
mkdir -p "$STATE_DIR" 2>/dev/null || exit 0
# Best-effort prune of stale session markers (>7 days) so the dir can't grow without bound.
find "$STATE_DIR" -name 'sess-*' -mtime +7 -delete 2>/dev/null

# stdin = hook payload JSON; python reads it directly (no `cat` in bash).
python3 -c '
import json, os, sys
state_dir = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
sid = data.get("session_id") or ""
if not sid:
    sys.exit(0)
opened = os.path.join(state_dir, f"sess-{sid}.opened")
nudged = os.path.join(state_dir, f"sess-{sid}.nudged")
# A thread is already open this session, or we already nudged → stay quiet.
if os.path.exists(opened) or os.path.exists(nudged):
    sys.exit(0)
open(nudged, "w").close()
ctx = (
    "\U0001f9f5 thread-keeper: no thread opened yet this session.\n"
    "If this turn begins a substantive topic (debugging, a feature, a multi-step task), "
    "call mcp__thread-keeper__open_thread(question) BEFORE diving in. Then "
    "note(thread_id, ..., kind=insight|move|failed) as decisions land, and "
    "close_thread(thread_id, outcome) when it resolves. "
    "If you are continuing an already-open thread, or this is a trivial one-off, ignore this."
)
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": ctx,
    }
}))
' "$STATE_DIR"
