#!/usr/bin/env bash
# thread-keeper: PreToolUse gate for parallelism tools — steers the
# spawn-vs-native choice from core_memory.spawn_pattern.
#
# TWO tool families, OPPOSITE heuristics, because the right default flipped
# with opus 4.8 (native parallelism matured):
#
#   Task (legacy built-in, still present on non-opus-4.8 models)
#     Old premise: native is weak, spawn() is the strong path for any
#     substantial parallel work. So BLOCK Task on parallel-fanout/long work
#     lacking a synthesis cue → push to spawn(). Honors TK_TASK_GATE mode
#     (deny default).
#
#   Agent / Workflow (opus 4.8+ native primitive)
#     New premise: native IS the right default for ephemeral in-turn
#     fan-out. So DON'T block fan-out. INVERTED: only flag work that
#     genuinely belongs to spawn() — carrying PERSISTENCE signals
#     (cross-session, inter-agent thread-keeper channels, must outlive the
#     session, daemon-style). Advisory WARN only — never hard-block a
#     native call (blocking a primitive the harness wants to use is hostile;
#     worst case of a false positive should be one ignorable stderr line).
#
# Modes via env TK_TASK_GATE (Task branch only; Agent/Workflow is always
# advisory regardless of mode):
#   deny  — exit 2, hard block, stderr visible to Claude (default)
#   warn  — exit 0, stderr warning, tool proceeds
#   off   — exit 0 silent, no-op (silences both branches)
#
# Decision rule (lesson 'spawn-vs-task-decision-tree' +
# core_memory.spawn_pattern) — choose on SCOPE, not size:
#   cross-session / inter-agent channels / daemon / outlives chat → spawn()
#   in-turn parallel fan-out (synthesize this turn)               → native Agent/Workflow
#   single targeted lookup                                         → native Agent Explore

set -u

MODE="${TK_TASK_GATE:-deny}"
[ "$MODE" = "off" ] && exit 0

# Require jq for safe JSON parsing; if absent, fail open.
command -v jq >/dev/null 2>&1 || exit 0

INPUT=$(cat)
TOOL=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || echo "")

SUBAGENT=$(printf '%s' "$INPUT" | jq -r '.tool_input.subagent_type // empty' 2>/dev/null || echo "")
PROMPT=$(printf '%s' "$INPUT" | jq -r '.tool_input.prompt // empty' 2>/dev/null || echo "")
DESC=$(printf '%s' "$INPUT" | jq -r '.tool_input.description // empty' 2>/dev/null || echo "")
PROMPT_LEN=${#PROMPT}

# ──────────────────────────────────────────────────────────────────────
# Agent / Workflow branch — INVERTED heuristic, advisory only.
# Native fan-out is fine; only nudge toward spawn() when the prompt shows
# the work needs thread-keeper persistence/coordination native can't give
# (separate CLI process, own cid, shared DB, survives the session).
# ──────────────────────────────────────────────────────────────────────
if [ "$TOOL" = "Agent" ] || [ "$TOOL" = "Workflow" ]; then
  # PERSISTENCE signals — the spawn()-only niche. Tight on purpose: a false
  # positive wrongly nudges away from the now-preferred native primitive, so
  # match only explicit cross-session / inter-agent / daemon language, NOT
  # generic "parallel"/"background" (native does those well).
  PERSIST='cross.?session|across sessions|between sessions|outlive|survive the (session|chat|conversation)|next session|переживать|между сесси|пережить сесси|other CLI|another CLI|other agent instance|sibling session|shared brain|thread.?keeper (channel|DB|memory|spawn)|broadcast\(|whisper\(|\binbox\(|\bwait\(|respond\(|\bask\(|inter.?agent|coordinate with (the )?(peer|sibling|other)|spawn a (persistent|long.lived|daemon)|run as a daemon|persist (it|the result|across)'
  if printf '%s\n%s\n' "$PROMPT" "$DESC" | grep -qiE "$PERSIST"; then
    REASON=$(cat <<'EOF'
tk-task-gate: native Agent/Workflow вызван для работы с PERSISTENCE-сигналами — это ниша mcp__thread-keeper__spawn(), не нативного сабагента.

Сигналы: prompt упоминает cross-session / inter-agent каналы (broadcast/whisper/inbox/wait) / переживание сессии / daemon / координацию с другим CLI.

Почему spawn(), а не native:
  • native Agent/Workflow — ЭФЕМЕРНЫЙ внутрисессионный параллелизм (общий контекст, результат в этот turn). НЕ переживает сессию, не виден другим CLI, нет inter-agent каналов.
  • spawn() — отдельный CLI-процесс, свой cid, общая thread-keeper БД, переживает чат, broadcast/whisper/inbox/wait.

Реально нужна персистентность/координация → mcp__thread-keeper__spawn().
Просто параллельный веер с синтезом в этот turn → игнорируй, native корректен (это advisory-warn, вызов продолжается).

Rule: core_memory.spawn_pattern · Lesson: spawn-vs-task-decision-tree
EOF
)
    printf '⚠️ %s\n' "$REASON" >&2
  fi
  # Advisory only — never block a native call. Proceed.
  exit 0
fi

# ──────────────────────────────────────────────────────────────────────
# Task branch (legacy tool, non-opus-4.8 models) — original heuristic.
# ──────────────────────────────────────────────────────────────────────
[ "$TOOL" != "Task" ] && exit 0

# Always-legitimate subagents — exempt from gate.
case "$SUBAGENT" in
  Explore|claude-code-guide|statusline-setup) exit 0 ;;
esac

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
