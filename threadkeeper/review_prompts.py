"""Self-improvement review prompts.

Adapted from hermes-agent's MEMORY_REVIEW_PROMPT / SKILL_REVIEW_PROMPT
constants. The "do NOT capture" list is the part that prevents auto-curation
from harming itself by hardening transient failures into permanent rules.

Used by:
- review_thread(mode='auto') — spawned background child receives one of these
  as its prompt and runs through the conversation reading recent notes.
- review_thread(mode='inline') — foreground agent gets the text back and
  processes it in the current turn.
"""

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
    "Review the closed thread above and update the Claude skill library "
    "under ~/.claude/skills/. Be ACTIVE — a rich thread usually yields at "
    "least one skill update, even small. A pass that does nothing is a "
    "missed learning opportunity, not a neutral outcome.\n\n"
    "Target shape: CLASS-LEVEL skills, each with a rich SKILL.md and "
    "optional references/ directory for session-specific detail. Not a "
    "long flat list of narrow one-incident skills. This shapes HOW you "
    "update, not WHETHER you update.\n\n"
    "Signals to look for (any one warrants action):\n"
    "  • User corrected style, tone, format, verbosity. Frustration "
    "signals like 'stop doing X', 'this is too verbose', 'just give me "
    "the answer', or an explicit 'remember this' are FIRST-CLASS skill "
    "signals — embed the preference so the next session starts already "
    "knowing.\n"
    "  • User corrected workflow, approach, or sequence of steps. "
    "Encode as a pitfall or explicit step in the governing skill.\n"
    "  • Non-trivial technique, fix, workaround, debugging path, or "
    "tool-usage pattern emerged. Capture it.\n"
    "  • A skill that got consulted this session turned out wrong, "
    "missing a step, or outdated. Patch it NOW.\n\n"
    "Preference order — pick the earliest action that fits:\n"
    "  1. PATCH a currently-loaded skill. If the conversation referenced "
    "an existing skill that covers the territory of the new learning, "
    "use skill_manage(action='patch', name=..., old_string=..., "
    "new_string=...) on that one first.\n"
    "  2. ADD a `references/<topic>.md` under an existing umbrella for "
    "session-specific detail. Use skill_manage(action='write_file', "
    "name=..., sub_path='references/<topic>.md', content=...).\n"
    "  3. CREATE a new class-level umbrella via skill_manage(action="
    "'create', ...). Name MUST be class-level — never an incident "
    "codename, PR number, or 'fix-X-today' artifact. If the name only "
    "makes sense for today's task, fall back to (1) or (2).\n\n"
    "When done, call mark_skill_materialized(thread_id, skill_path) so "
    "the brief's skill_hint stops firing for this thread.\n\n"
    + ANTI_CAPTURE + "\n\n"
    "'Nothing to save.' is a real option but should NOT be the default. "
    "If the thread ran smoothly with no corrections and produced no new "
    "technique, broadcast 'Nothing to save.' and stop. Otherwise, act."
)


COMBINED_REVIEW_PROMPT = (
    "Review the closed thread above and update two things:\n\n"
    "**Memory**: who the user is. Did the user reveal persona, "
    "preferences, work style, personal details, or expectations about "
    "how you should operate? If yes, save via core_set / verbatim_user / "
    "note as appropriate.\n\n"
    "**Skills**: how to handle this class of task. Did the user correct "
    "style, workflow, or approach? Did a non-trivial fix or technique "
    "emerge? If yes, prefer in order: patch an existing skill, add a "
    "references file, or create a new class-level umbrella under "
    "~/.claude/skills/ via skill_manage().\n\n"
    "After any skill write, call mark_skill_materialized(thread_id, "
    "skill_path) to close the loop.\n\n"
    + ANTI_CAPTURE + "\n\n"
    "If genuinely nothing on either dimension, broadcast 'Nothing to "
    "save.' and stop. But don't reach for that conclusion as a default."
)
