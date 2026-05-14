#!/bin/bash
# thread-keeper: SessionStart hook
#
# Injects brief() + context() into the new session's system prompt as
# additionalContext. Replaces the CLAUDE.md instruction "call brief()
# and context() at start of every conversation" with a deterministic
# pre-loaded snapshot.
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

# Run brief() + context() and (when live>0) live_status() in one python
# invocation. We split output into two parts:
#  - STATUS\t<line>  → compact visible status (shown to user via systemMessage)
#  - CONTEXT\t<rest> → full brief, injected into system prompt
# Any exception inside python is swallowed so the hook never blocks startup.
OUTPUT=$(
  PYTHONPATH="$PKG_ROOT" "$VENV_PY" - <<'PY' 2>/dev/null
import re, sys
try:
    from threadkeeper.tools.threads import brief, context
    b = brief()  # SessionStart fires BEFORE the user's first message, so no query yet
    c = context()
    parts = [b, "", c]

    # Build compact visible status line. Parsed straight from brief output,
    # no IDs cited (per user_facing_style — paraphrase only).
    live_n = 0
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
        f"live_peers={live_n}",
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

# Emit hook protocol JSON. Two safety measures for clients with stricter
# parsers (Claude VS Code extension trips on `Unhandled case: [object
# Object]` when given both top-level systemMessage AND hookSpecificOutput):
#
#   1. Output ONLY `hookSpecificOutput.additionalContext` (the
#      important payload). The cosmetic status line is folded into the
#      same additionalContext as a first line so no information is lost
#      and no extra top-level fields confuse the client.
#   2. Cap the total payload at 32 KB. A full brief in a chatty week can
#      exceed 100 KB and some extensions choke on huge injections.
python3 -c '
import json, sys
status = sys.argv[1]
ctx = sys.argv[2]
combined = (f"[{status}]\n\n" if status else "") + ctx
# 32 KB cap — large enough for any realistic brief, small enough to
# stay inside picky UI parser limits. Truncate at codepoint boundary.
MAX = 32 * 1024
if len(combined) > MAX:
    combined = combined[:MAX] + "\n…[truncated by tk-brief hook]"
out = {
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": combined,
    }
}
print(json.dumps(out))
' "$STATUS_LINE" "$CONTEXT_BODY"
