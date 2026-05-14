#!/bin/bash
# thread-keeper: PostToolUse hook
#
# Surfaces a short, plain-language status line after each mutating
# thread-keeper tool call. Matcher in settings.json should be
# "mcp__thread-keeper__.*" — this script then filters to mutating
# tools and emits a systemMessage. Read-only tools (brief, context,
# search, dialog_search, peers, inbox, etc.) are silently skipped.
#
# Input on stdin: hook protocol JSON with tool_name, tool_input,
# tool_response. We parse with jq.

set -u

# Read full input
INPUT=$(cat)

# Tool name without the mcp__thread-keeper__ prefix.
TOOL=$(jq -r '.tool_name // empty' <<<"$INPUT" 2>/dev/null | sed 's|^mcp__thread-keeper__||')
[ -z "$TOOL" ] && exit 0

# Truncate helper — first N chars, single-line
trim() {
  local s="$1"
  local n="${2:-80}"
  printf '%s' "$s" | tr '\n' ' ' | awk -v n="$n" '{
    if (length($0) > n) print substr($0, 1, n) "…"
    else print $0
  }'
}

# Quick jq access to tool_input.<field>
input_field() {
  jq -r ".tool_input.$1 // empty" <<<"$INPUT" 2>/dev/null
}

# tool_response.result (the MCP tool's return string, when present)
RESULT=$(jq -r '.tool_response.result // .tool_response.output // empty' <<<"$INPUT" 2>/dev/null)

MSG=""

case "$TOOL" in
  open_thread)
    Q=$(input_field question)
    MSG="🧵 opened: $(trim "$Q" 80)"
    ;;
  close_thread)
    OUT=$(input_field outcome)
    MSG="✅ closed: $(trim "$OUT" 80)"
    ;;
  note)
    KIND=$(input_field kind)
    CONTENT=$(input_field content)
    [ -z "$KIND" ] && KIND="move"
    MSG="📝 +$KIND: $(trim "$CONTENT" 80)"
    ;;
  idle_thread)
    MSG="💤 thread idled"
    ;;
  mark_skill_materialized)
    PATH_=$(input_field skill_path)
    if [ -n "$PATH_" ]; then
      MSG="🎯 skill materialized → $(basename "$(dirname "$PATH_")")"
    else
      MSG="🎯 skill materialized"
    fi
    ;;
  core_set)
    KEY=$(input_field key)
    MSG="📌 core[$KEY] updated"
    ;;
  core_remove)
    KEY=$(input_field key)
    MSG="🗑️ core[$KEY] removed"
    ;;
  verbatim_user)
    CONTENT=$(input_field content)
    MSG="❝ verbatim: $(trim "$CONTENT" 80)"
    ;;
  dialectic_claim)
    CLAIM=$(input_field claim)
    DOM=$(input_field domain)
    [ -n "$DOM" ] && DOM=" [$DOM]" || DOM=""
    MSG="🧠 claim+$DOM: $(trim "$CLAIM" 70)"
    ;;
  dialectic_evidence)
    KIND=$(input_field kind)
    Q=$(input_field quote)
    [ -z "$KIND" ] && KIND="support"
    MSG="🧠 evidence ($KIND): $(trim "$Q" 70)"
    ;;
  dialectic_supersede)
    NEW=$(input_field new_claim)
    MSG="🧠 claim superseded → $(trim "$NEW" 70)"
    ;;
  skill_manage)
    ACTION=$(input_field action)
    NAME=$(input_field name)
    MSG="🎓 skill $ACTION: $NAME"
    ;;
  skill_record)
    # telemetry, too noisy — skip
    echo '{}'; exit 0
    ;;
  review_thread)
    TID=$(input_field thread_id)
    MODE=$(input_field mode)
    [ -z "$MODE" ] && MODE="auto"
    MSG="🔄 review spawned ($MODE) for closed thread"
    ;;
  auto_review_trigger)
    MSG="🔄 auto-review triggered"
    ;;
  spawn)
    PROMPT=$(input_field prompt)
    MSG="⚡ spawn: $(trim "$PROMPT" 70)"
    ;;
  tournament)
    ROLES=$(input_field roles)
    MSG="⚡ tournament: roles=$ROLES"
    ;;
  task_kill)
    MSG="🛑 task killed"
    ;;
  broadcast)
    CONTENT=$(input_field content)
    MSG="📡 broadcast: $(trim "$CONTENT" 80)"
    ;;
  whisper)
    CONTENT=$(input_field content)
    MSG="🤫 whisper: $(trim "$CONTENT" 80)"
    ;;
  ask)
    Q=$(input_field question)
    MSG="❓ asked peer: $(trim "$Q" 80)"
    ;;
  respond)
    CONTENT=$(input_field content)
    MSG="💬 responded: $(trim "$CONTENT" 80)"
    ;;
  evolve_format)
    SUG=$(input_field suggestion)
    MSG="📐 evolve_format: $(trim "$SUG" 80)"
    ;;
  register_concept)
    DESC=$(input_field description)
    MSG="💡 concept registered: $(trim "$DESC" 70)"
    ;;
  distill)
    CONTENT=$(input_field content)
    MSG="🧪 distill: $(trim "$CONTENT" 70)"
    ;;
  vote_distill)
    MSG="🗳️ vote on distill"
    ;;
  accept_candidate)
    MSG="✓ candidate accepted"
    ;;
  reject_candidate)
    MSG="✗ candidate rejected"
    ;;
  claim_pickup)
    MSG="🤲 claimed stale thread"
    ;;
  release_pickup)
    MSG="↩️ released claim"
    ;;
  consolidate)
    DR=$(input_field dry_run)
    if [ "$DR" = "false" ]; then
      MSG="🧹 consolidate applied"
    else
      # dry-run, skip
      echo '{}'; exit 0
    fi
    ;;
  curator_run)
    DR=$(input_field dry_run)
    if [ "$DR" = "false" ]; then
      MSG="🧹 curator: stale skills archived"
    else
      echo '{}'; exit 0
    fi
    ;;
  mp_cleanup)
    DR=$(input_field dry_run)
    if [ "$DR" = "false" ]; then
      MSG="🧹 orphan mp processes killed"
    else
      echo '{}'; exit 0
    fi
    ;;
  spawn_budget_set)
    LIM=$(input_field limit_mb)
    MSG="💰 spawn budget: ${LIM}MB"
    ;;
  ingest)
    # touches the dialog log on every UserPromptSubmit cycle — too noisy
    echo '{}'; exit 0
    ;;
  *)
    # All read-only / silent tools fall here: brief, context, search,
    # dialog_search, peers, presence, inbox, wait, whoami, live_status,
    # tasks, task_logs, search_via_parent, compost, mp_health,
    # spawn_budget_status, skill_list, dialectic_review,
    # dialectic_synthesis, list_concepts, expand_concept,
    # pending_distillates, export_distillates, find_invariants,
    # weak_spots, reliability_for, register_probe, run_probe,
    # record_attempt, review_candidates, extract_recent,
    # missed_spawns, neighbors, evolve_review, style_set, link,
    # unlink, tag_signal, etc.
    echo '{}'; exit 0
    ;;
esac

[ -z "$MSG" ] && { echo '{}'; exit 0; }

# Append a status indicator if the tool returned ERR
if printf '%s' "$RESULT" | grep -qE '^ERR'; then
  MSG="$MSG  ❌ FAILED"
fi

# Emit hook protocol JSON
python3 -c '
import json, sys
print(json.dumps({"systemMessage": sys.argv[1], "suppressOutput": True}))
' "$MSG"
