#!/bin/bash
# thread-keeper: UserPromptSubmit / SessionStart hook
#
# Surfaces unread peer signals (broadcast/whisper/question/answer) addressed
# to this conversation as additionalContext for the next claude turn — so the
# parent always sees pending peer messages even when not actively wait()ing.
#
# Self-cid resolution mirrors server.py: walk the process tree until we find
# a claude CLI invocation with --resume / --session-id / --continue <uuid>.

set -u

DB="${THREADKEEPER_DB:-$HOME/.threadkeeper/db.sqlite}"

resolve_self_cid() {
  # If env override set (e.g. spawned children), use it
  if [ -n "${THREADKEEPER_FORCE_CID:-}" ]; then
    echo "$THREADKEEPER_FORCE_CID"
    return
  fi
  local pid=$$
  local i ppid cmd m
  for i in $(seq 1 14); do
    local line
    line=$(ps -p "$pid" -o ppid=,command= 2>/dev/null)
    [ -z "$line" ] && return
    ppid=$(echo "$line" | awk '{print $1}')
    cmd=$(echo "$line" | cut -d' ' -f2-)
    m=$(echo "$cmd" | grep -oE -- "--(resume|session-id|continue) [a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}" | head -1 | awk '{print $2}')
    if [ -n "$m" ]; then
      echo "$m"
      return
    fi
    [ -z "$ppid" ] && return
    [ "$ppid" -le 1 ] && return
    pid="$ppid"
  done
}

SELF_CID="$(resolve_self_cid)"
if [ -z "$SELF_CID" ] || [ ! -f "$DB" ]; then
  exit 0  # silent — no self_cid or no db means nothing to surface
fi

# Pull unread signals: addressed to me (or broadcast), not from me, not yet read.
# Use a delimiter unlikely to appear in content (\x1f = ASCII unit separator).
ROWS=$(sqlite3 -separator $'\x1f' "$DB" "
SELECT id, from_cid, COALESCE(to_cid,'*'), kind,
       (strftime('%s','now') - created_at),
       substr(replace(replace(content, char(10), ' / '), char(13), ''), 1, 240)
FROM signals
WHERE (to_cid = '$SELF_CID' OR to_cid IS NULL)
  AND from_cid != '$SELF_CID'
  AND read_at IS NULL
ORDER BY created_at
LIMIT 10;
" 2>/dev/null)

if [ -z "$ROWS" ]; then
  exit 0
fi

# Build the human-readable block
COUNT=$(echo "$ROWS" | grep -c .)
SELF_SHORT="${SELF_CID:0:8}"

block=""
while IFS=$'\x1f' read -r id from to kind ago content; do
  [ -z "$id" ] && continue
  from_short="${from:0:8}"
  if [ "$to" = "*" ]; then
    scope="(broadcast)"
  else
    scope="→ me"
  fi
  # use printf to avoid bash echo's flag interpretation
  block+="  #${id} ${scope} from=${from_short} +${kind} ${ago}s_ago: ${content}
"
done <<< "$ROWS"

# Compose final additionalContext
ctx="📨 thread-keeper: ${COUNT} unread peer signal(s)

${block}
Address these BEFORE the user's prompt if substantive. Tools:
- mcp__thread-keeper__inbox() — read + mark
- mcp__thread-keeper__respond(qid, content) — reply to a +question
- mcp__thread-keeper__whisper(cid, ...) / broadcast(...) — directed/broadcast
- mcp__thread-keeper__wait(timeout, kinds) — long-poll for more

⚠️ When replying to the user: paraphrase in plain language. Do NOT quote internal IDs (signal #ids, cids, thread T-codes, qids) — those are tool-call internals only."

# Return as JSON for UserPromptSubmit/SessionStart hook
python3 -c '
import json, sys
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": sys.argv[1]
    }
}))
' "$ctx"
