"""Static lifecycle guidance shared by MCP initialization and setup.

The MCP ``instructions`` field must be safe to send to every connected client,
so it deliberately contains protocol guidance only.  In particular, it never
reads configuration, memory, paths, transcripts, or identifiers at import time.
"""
from __future__ import annotations


SERVER_INSTRUCTIONS_MAX_CHARS = 1_200

# Keep these complete sentences as the single source for the lifecycle rules.
# Setup renders the same fragments as bullets in its longer, host-specific
# managed block; MCP initialization renders the compact paragraph below.
LIFECYCLE_GUIDANCE = (
    "At session start, load the brief and context: prefer the "
    "`memory://brief` and `memory://context` resources when the host can "
    "attach them; otherwise call `brief()` and `context()`. For a "
    "substantive opening request, pass it as `query` to `brief()`.",
    "Before the first substantive task, call `open_thread()`. Record "
    "decisions and outcomes with `note()`, then call "
    "`close_thread(thread_id, outcome)` when the topic is resolved and "
    "`session_end(summary)` at the end of the session.",
    "Use read-only resources for host-attached memory and read-only tools "
    "for queries; call a state-changing tool only when an action is intended.",
    "For destructive maintenance, first use the dry-run mode, explain its "
    "scope, and obtain user confirmation before the apply step.",
)

SERVER_INSTRUCTIONS = "\n".join(LIFECYCLE_GUIDANCE)

if len(SERVER_INSTRUCTIONS) > SERVER_INSTRUCTIONS_MAX_CHARS:  # pragma: no cover
    raise ValueError("MCP server instructions exceed the static text budget")


def _managed_lifecycle_guidance() -> str:
    return "\n".join(f"- {guidance}" for guidance in LIFECYCLE_GUIDANCE)


MANAGED_INSTRUCTIONS_BLOCK = f"""\
## thread-keeper

thread-keeper holds persistent working memory across conversations.

## Shared lifecycle

{_managed_lifecycle_guidance()}

## Local reinforcement

On hosts with a SessionStart hook, thread-keeper injects the brief and context
automatically. Hosts without hooks or a global instructions file receive the
same lifecycle guidance through MCP initialization; still use the resources or
tools above when the host does not attach them automatically.

When the user says something sharp and precise, call `verbatim_user()`. When a
brief field is missing or unused, call `evolve_format()`.

When the brief surfaces a thread or topic relevant to the current request (by
`question`, `last_move`, or semantic match), do not answer from the brief
alone — dig deeper. Search in this order and stop when the first source gives
enough context:

1. `thread-keeper.search()` — stored partner notes
2. `thread-keeper.dialog_search()` — full transcripts from connected CLIs
3. The host's native conversation-history search, when available

## Procedural lessons

At session start, call `lesson_list(query=<the opening request>)` to find the
lessons most relevant to it, then use `lesson_get(slug)` for the useful ones.
When a substantive task produces a reusable class-level lesson, first call
`lesson_neighbors(title, body, summary)`. If a neighbour already covers the
rule, extend it with `lesson_patch`; otherwise call
`lesson_append(title, body, summary, source=thread_id)`. A near-duplicate is
refused with the neighbour's slug; pass `new_despite=<slug>` only after reading
it and confirming the rule is different.

Do not report these tool calls to the user — they are internal.
"""
