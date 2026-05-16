"""Self-improvement review prompts.

`MEMORY_REVIEW_PROMPT` / `SKILL_REVIEW_PROMPT` drive the spawned review
fork. The "do NOT capture" list is the part that prevents
auto-curation from harming itself by hardening transient failures into
permanent rules.

Used by:
- review_thread(mode='auto') — spawned background child receives one of these
  as its prompt and runs through the conversation reading recent notes.
- review_thread(mode='inline') — foreground agent gets the text back and
  processes it in the current turn.
"""

# Rubric-form opener for the review prompts. The review fork uses
# rubric-based grading rather than free-form "should this update
# memory/skills?" — empirically halves the false-negative rate on
# substantive incidents. 5 yes/no questions, each with a concrete
# action attached. "Nothing to save." is allowed ONLY when all five
# answers are No.
RUBRIC_QUESTIONS = (
    "RUBRIC — answer each question. ANY \"YES\" answer requires action; "
    "only ALL-\"NO\" allows the \"Nothing to save.\" stop.\n\n"
    "  Q1. Did the user state a workflow rule as POLICY "
    "(\"always do X\", \"next time Y\", \"prefer Path-1 over Path-2 "
    "when Z\")? Frustration signals (\"stop doing X\", \"this is too "
    "verbose\") and explicit \"remember this\" count as YES.\n"
    "      → YES: capture as stated-policy lesson; embed the preference "
    "verbatim so next session starts already knowing.\n\n"
    "  Q2. Did a RECOVERY / CLEANUP procedure for flaky infra emerge "
    "(network reset before tool start, proxy state hygiene, "
    "zombie-process cleanup, port-reuse wait-loops)?\n"
    "      → YES: capture as recovery-pattern lesson. The env-specific "
    "incident becomes ONE worked example inside a rule-shaped lesson, "
    "NOT the whole content.\n\n"
    "  Q3. Did a DEBUGGING STRATEGY generalize beyond this specific "
    "bug (pattern-recognition rules like \"check testID drift before "
    "chasing logic\", \"3 compounding bugs detection via element-cache "
    "+ Z-order + fixture mismatch\", \"verify state transition, not "
    "destination label\")?\n"
    "      → YES: capture as debugging-pattern lesson.\n\n"
    "  Q4. Was an EXISTING skill or lesson corrected, missing a step, "
    "or outdated relative to what just happened?\n"
    "      → YES: PATCH the existing one BEFORE considering a new "
    "lesson. New lessons that overlap existing ones pollute the store.\n\n"
    "  Q5. Did a non-trivial TECHNIQUE / FIX / TOOL-USAGE PATTERN "
    "emerge that someone else hitting the same class of problem would "
    "want to know — not the specific bug, the SHAPE of the solution?\n"
    "      → YES: capture under the appropriate umbrella; prefer "
    "references/<topic>.md under an existing skill if it fits."
)


# Counter-weight to ANTI_CAPTURE. The original anti-capture clause is
# strong enough that early calibration data showed shadow children
# SKIPping 100% of substantive incidents — every real-world fix has
# *some* env-specific surface, and the children kept classifying the
# whole episode as env-specific even when the underlying pattern was
# durable. POSITIVE_EXAMPLES draws the surface/pattern line explicitly.
POSITIVE_EXAMPLES = (
    "CAPTURE these even when they emerged in a single incident — the "
    "FIX/PATTERN is durable even if the failure surface was env-specific:\n"
    "  • Recovery patterns for flaky infra (network resets before WDA "
    "start, proxy state hygiene, zombie-process cleanup, port-reuse "
    "wait-loops). The HOW-TO is generalizable across every future "
    "instance, not specific to today's test.\n"
    "  • Debugging-strategy patterns: \"3 compounding bugs detection "
    "via element-cache + Z-order + fixture mismatch\", \"check testID "
    "drift before chasing logic\", \"verify state transition, not just "
    "destination label\". Pattern-recognition rules outlive the bug "
    "that surfaced them.\n"
    "  • Workflow rules the user stated as policy (\"on each test "
    "start, do X\", \"before claiming a fix, verify Y\", \"prefer "
    "Path-1 over Path-2 when Z\"). Stated policies are first-class "
    "skill content.\n"
    "  • iOS/Android testing recovery — WDA + macOS Wi-Fi proxy state, "
    "XCUITest element-cache invalidation, share-cluster bug "
    "triangulation, Detox/Maestro selector hierarchies. Class-level "
    "even when discovered in one suite.\n\n"
    "KEY DISTINCTION — \"episode env-specific\" vs \"rule env-specific\":\n"
    "  If the SYMPTOM looked env-specific (Plaid flake, fixture testID "
    "drift, payout step ordering) but the underlying FIX generalizes "
    "(always reset network before WDA start; always check for testID "
    "drift before chasing logic bugs; always make optional/ad-hoc "
    "fixture steps explicit) — CAPTURE the generalized rule, not the "
    "incident. Use the incident as ONE illustrative example inside a "
    "rule-shaped lesson.\n\n"
    "ANTI_CAPTURE still applies — but only for genuinely transient env "
    "errors (\"npm i fixed it\", \"reboot fixed it\") with no durable "
    "rule. If you find yourself writing the verdict \"environment-"
    "specific E2E debugging — no class-level rule\" but the conversation "
    "ALSO contains stated policies, recovery procedures, or "
    "pattern-recognition heuristics — those ARE class-level, capture "
    "them as a rule lesson with the incident as the worked example."
)


# Shared do-NOT-capture clause. Quoted in both prompts so a foreground agent
# trying to "save everything" stops at this fence.
ANTI_CAPTURE = (
    "Do NOT capture (these become persistent self-imposed constraints that "
    "bite you later when the environment changes):\n"
    "  • Environment-dependent failures: missing binaries, fresh-install "
    "errors, post-migration path mismatches, 'command not found', "
    "unconfigured credentials, uninstalled packages. The user can fix "
    "these — they are not durable rules.\n"
    "  • Negative claims about tools or features ('browser tools do not "
    "work', 'X tool is broken', 'cannot use Y from execute_code'). These "
    "harden into refusals the agent cites against itself for months "
    "after the actual problem was fixed.\n"
    "  • Session-specific transient errors that resolved before the "
    "conversation ended. If retrying worked, the lesson is the retry "
    "pattern, not the original failure.\n"
    "  • One-off task narratives. A user asking 'summarize today's "
    "market' or 'analyze this PR' is not a class of work that warrants "
    "a skill.\n\n"
    "If a tool failed because of setup state, capture the FIX (install "
    "command, config step, env var to set) under an existing setup or "
    "troubleshooting skill — never 'this tool does not work' as a "
    "standalone constraint."
)


MEMORY_REVIEW_PROMPT = (
    "Review the closed thread above (use search() or the notes_for_thread "
    "context below) and consider saving to memory if appropriate.\n\n"
    "Focus on:\n"
    "1. Has the user revealed things about themselves — persona, "
    "preferences, work style, personal details worth remembering?\n"
    "2. Has the user expressed expectations about how you should "
    "behave or operate in this kind of task?\n\n"
    "If something stands out, write it via core_set() for high-priority "
    "always-on lines OR verbatim_user() for a quoted fragment OR an "
    "appropriate note() on the source thread. " + ANTI_CAPTURE + "\n\n"
    "If nothing is worth saving, broadcast 'Nothing to save.' and stop."
)


SKILL_REVIEW_PROMPT = (
    "Review the closed thread above and materialize any class-level "
    "lessons.\n\n"
    "PRIMARY output: a SKILL.md under ~/.claude/skills/<name>/ via "
    "skill_manage(action='create'|'patch'|'write_file'|'delete'). The "
    "Skill format is the universal format — Claude Code, Claude "
    "Desktop, Codex, the Anthropic IDE plugins, and any MCP-aware tool "
    "consume it. SKILL.md auto-triggers via the frontmatter "
    "description field, so the right skill loads when relevant — vs. "
    "an opt-in scan of lessons.md.\n\n"
    "FALLBACK output (only when target CLI has no skills/ directory — "
    "Gemini / Copilot / generic MCP clients without a skill loader): "
    "lesson_append(title, body, summary, source=thread_id) writes into "
    "~/.threadkeeper/lessons.md. Use this only if the primary path "
    "isn't available; otherwise the SKILL.md is strictly better.\n\n"
    + RUBRIC_QUESTIONS + "\n\n"
    "PREFERENCE ORDER (pick the earliest action that fits):\n"
    "  1. PATCH an existing skill. If the conversation referenced (or "
    "the RECENTLY ACTIVE SKILLS block surfaces) an existing skill "
    "covering the new learning, use skill_manage(action='patch', "
    "name=..., old_string=..., new_string=...). New skills that "
    "overlap existing ones pollute the store — patch beats create.\n"
    "  2. ADD a `references/<topic>.md` under an existing umbrella for "
    "session-specific detail. Use skill_manage(action='write_file', "
    "name=..., sub_path='references/<topic>.md', content=...). Keeps "
    "the parent SKILL.md compact; references load lazily.\n"
    "  3. CREATE a new class-level umbrella via skill_manage(action="
    "'create', ...). Name MUST be class-level — never an incident "
    "codename, PR number, or 'fix-X-today' artifact. If the name only "
    "makes sense for today's task, fall back to (1) or (2).\n"
    "  4. DELETE if you discover the consulted skill was a false "
    "positive (created in error, doesn't actually apply): "
    "skill_manage(action='delete', name=...). Don't leave wrong "
    "skills in the store hoping next session ignores them — they "
    "auto-load via frontmatter and bias future runs.\n\n"
    "Target shape: CLASS-LEVEL umbrella skills with rich SKILL.md and "
    "optional references/ directory for session-specific detail — NOT "
    "a long flat list of narrow one-incident skills.\n\n"
    "When done, call mark_skill_materialized(thread_id, skill_path) so "
    "the brief's skill_hint stops firing for this thread.\n\n"
    + POSITIVE_EXAMPLES + "\n\n"
    + ANTI_CAPTURE + "\n\n"
    "STOP CONDITION: \"Nothing to save.\" is only legal when ALL of "
    "Q1-Q5 above answer No. If even one answers Yes, you must act."
)


COMBINED_REVIEW_PROMPT = (
    "Review the closed thread above and update two dimensions in one "
    "pass:\n\n"
    "  **Memory** — who the user is. Did the user reveal persona, "
    "preferences, work style, personal details, or expectations about "
    "how you should operate? If yes, save via core_set / verbatim_user "
    "/ note as appropriate.\n\n"
    "  **Skills** — how to handle this class of task. PRIMARY: "
    "skill_manage(action='create'|'patch'|'write_file'|'delete') → "
    "~/.claude/skills/<name>/SKILL.md. The Skill format auto-triggers "
    "via frontmatter description and is consumed by every modern "
    "agentic CLI (Claude Code/Desktop, Codex CLI/desktop, IDE plugins) "
    "— strictly better than an opt-in lessons.md scan. FALLBACK: "
    "lesson_append(...) → ~/.threadkeeper/lessons.md only for CLIs "
    "without a skills/ directory (Gemini / Copilot / bare MCP).\n\n"
    + RUBRIC_QUESTIONS + "\n\n"
    "After any materialization, call mark_skill_materialized("
    "thread_id, skill_path_or_lessons_md) to close the loop.\n\n"
    + POSITIVE_EXAMPLES + "\n\n"
    + ANTI_CAPTURE + "\n\n"
    "STOP CONDITION: \"Nothing to save.\" is only legal when ALL of "
    "Q1-Q5 AND both Memory questions above answer No."
)
