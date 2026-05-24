#!/usr/bin/env bash
# thread-keeper: PreToolUse gate for the built-in Task tool.
#
# Enforces the spawn-vs-Task rule defined in core_memory.spawn_pattern.
# When Claude calls Task for work that matches the "should be spawned"
# heuristic, this hook blocks (or warns) and points at
# mcp__thread-keeper__spawn().
#
# Modes via env TK_TASK_GATE:
#   deny  — exit 2, hard block, stderr message visible to Claude (default)
#   warn  — exit 0, stderr warning, tool proceeds
#   off   — exit 0 silent, no-op
#
# Heuristic for "should be spawn":
#   - subagent_type NOT in {Explore, claude-code-guide} (always-legit subagents)
#   - prompt length > 600 chars (substantial work, not a quick lookup)
#   - prompt/description matches parallel-fanout / duration pattern
#   - NO synthesis cue in prompt (parent doesn't claim needing result this turn)
#
# Decision rule (see lesson 'spawn-vs-task-decision-tree'):
#   N≥2 units ≥5min OR result not needed this turn → spawn()
#   N≥2 units <5min + result needed this turn      → Task fan-out (add "synthesize"/"combine results")
#   Single targeted lookup                          → Task Explore

set -u

MODE="${TK_TASK_GATE:-deny}"
[ "$MODE" = "off" ] && exit 0

# Require jq for safe JSON parsing; if absent, fail open.
command -v jq >/dev/null 2>&1 || exit 0

INPUT=$(cat)
TOOL=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || echo "")
[ "$TOOL" != "Task" ] && exit 0

SUBAGENT=$(printf '%s' "$INPUT" | jq -r '.tool_input.subagent_type // empty' 2>/dev/null || echo "")
PROMPT=$(printf '%s' "$INPUT" | jq -r '.tool_input.prompt // empty' 2>/dev/null || echo "")
DESC=$(printf '%s' "$INPUT" | jq -r '.tool_input.description // empty' 2>/dev/null || echo "")

# Always-legitimate subagents — exempt from gate.
case "$SUBAGENT" in
  Explore|claude-code-guide|statusline-setup) exit 0 ;;
esac

PROMPT_LEN=${#PROMPT}

# Heuristic A: parallel-fanout or long-duration pattern.
PARALLEL='(across|for each|all) +(locale|language|file|module|service|directory|repo|package|component|migration|build|test|workspace)|in parallel|параллел|fan.?out|long.running|long-running|migration|full test suite|build all|migrate all|run.*on (each|every|all)|for each of|across all'

# Heuristic B: synthesis intent (parent needs result THIS turn).
SYNTH='synthesi[sz]e|same.turn|this.turn|combine.*result|aggregate.*(and|to) (return|use|feed)|feed.*into.*(next|apply|synth)|for the apply|summari[sz]e (the|all) (result|finding)|merge.*(result|finding)|consolidate.*(result|finding)|return.*combined|return.*summary'

MATCH=0
if [ "$PROMPT_LEN" -gt 600 ]; then
  if printf '%s\n%s\n' "$PROMPT" "$DESC" | grep -qiE "$PARALLEL"; then
    MATCH=1
  fi
fi

# Exempt when synthesis cue is present.
if [ "$MATCH" -eq 1 ]; then
  if printf '%s\n%s\n' "$PROMPT" "$DESC" | grep -qiE "$SYNTH"; then
    MATCH=0
  fi
fi

[ "$MATCH" -eq 0 ] && exit 0

REASON=$(cat <<'EOF'
tk-task-gate: Task tool вызван для работы, которая по эвристике должна идти через mcp__thread-keeper__spawn().

Сигналы: prompt >600 chars + parallel-fanout/duration паттерн, БЕЗ synthesis cue.

Rule (core_memory.spawn_pattern):
  N≥2 units ≥5min, OR result не нужен в этом turn, OR пережить сессию → spawn()
  N≥2 units <5min + result нужен в этом turn для синтеза → Task (упомяни "synthesize"/"combine results"/"aggregate" в prompt)
  Single targeted lookup → Task subagent_type=Explore

Чтобы пройти через Task:
  • Добавь synthesis cue в prompt ("synthesize results", "combine and return ...", "aggregate for next step")
  • ИЛИ используй subagent_type=Explore (read-only research)
  • ИЛИ TK_TASK_GATE=warn (advisory) / TK_TASK_GATE=off (disabled)

Lesson: spawn-vs-task-decision-tree
EOF
)

if [ "$MODE" = "warn" ]; then
  printf '⚠️ %s\n' "$REASON" >&2
  exit 0
fi

printf '🛑 %s\n' "$REASON" >&2
exit 2
