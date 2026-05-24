#!/bin/bash
# thread-keeper: Stop hook — close_thread / session_end safety net.
#
# Backstops the CLAUDE.md rules "topic resolved → close_thread(outcome)"
# and "end of conversation → session_end(summary)".
#
# NOTE on semantics: Claude Code's Stop event fires at the END OF EVERY
# assistant turn, not once at true session end (there is no model-visible
# "session end" event — SessionEnd output cannot be acted on). So this
# hook is deliberately throttled to fire AT MOST ONCE per session, and
# only when a thread was actually opened this session (`.opened` marker
# present, written by tk-status.sh) — otherwise there is nothing to close.
# It is advisory (a systemMessage chip), never blocks stopping.

set -u

STATE_DIR="${THREADKEEPER_STATE_DIR:-$HOME/.threadkeeper/state}"
[ -d "$STATE_DIR" ] || exit 0

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
reminded = os.path.join(state_dir, f"sess-{sid}.endreminded")
# No thread opened this session, or already reminded → stay quiet.
if not os.path.exists(opened) or os.path.exists(reminded):
    sys.exit(0)
open(reminded, "w").close()
print(json.dumps({
    "systemMessage": (
        "\U0001f9f5 thread-keeper: you opened thread(s) this session. "
        "Before wrapping up, close_thread(thread_id, outcome) the resolved ones "
        "and call session_end(summary)."
    ),
    "suppressOutput": True,
}))
' "$STATE_DIR"
