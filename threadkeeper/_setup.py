"""thread-keeper installer / updater.

Idempotently wires thread-keeper into every detected CLI adapter:
  1. Registers `thread-keeper` MCP server in each CLI's native config
  2. Installs hooks for adapters whose hook schema is supported
  3. Copies hook scripts to ~/.threadkeeper/hooks/
  4. Updates the managed instructions block between sentinel markers —
     content outside the markers is preserved.

Re-run any time: the script reads existing config, merges its own
contribution, and writes back. Other MCP servers / hooks / instructions
content are left untouched.

Console entry point:
    thread-keeper-setup [--dry-run]

Or:
    python -m threadkeeper._setup [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .permissions import chmod_private_dir

# ----------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------

HOME = Path.home()
PACKAGE_ROOT = Path(__file__).resolve().parent           # .../threadkeeper/
REPO_ROOT = PACKAGE_ROOT.parent                          # .../ai-memory/

CLAUDE_DIR = HOME / ".claude"
CLAUDE_MD = CLAUDE_DIR / "CLAUDE.md"
CLAUDE_JSON = HOME / ".claude.json"
SETTINGS_JSON = CLAUDE_DIR / "settings.json"

TK_DIR = HOME / ".threadkeeper"
TK_HOOKS_DIR = TK_DIR / "hooks"

HOOKS_SRC = REPO_ROOT / "scripts" / "hooks"

# Sentinel markers — content between these lines is managed by this
# installer. The user can edit OUTSIDE the block freely.
MARK_BEGIN = "<!-- THREADKEEPER:BEGIN — managed by `thread-keeper setup`; do not edit between these markers -->"
MARK_END = "<!-- THREADKEEPER:END -->"


# ----------------------------------------------------------------------
# Content of the managed per-CLI instructions block
# ----------------------------------------------------------------------

CLAUDE_MD_BLOCK = """\
## thread-keeper

thread-keeper holds persistent working memory across conversations.
At session start:
  * On Claude Code, the SessionStart hook
    (`~/.threadkeeper/hooks/tk-brief.sh`) auto-injects `brief()` +
    `context()` — and `live_status()` if `live=N>0`.
  * On CLIs without thread-keeper hooks (Codex, Antigravity CLI/`agy`,
    Copilot, …), call `brief()` and `context()` yourself before
    the first answer.
    If the user's opening message is substantive, pass it as `query`
    to `brief()` to inline relevant past notes.

During the conversation (a UserPromptSubmit hook nudges you once per
session if you haven't opened a thread, and a Stop hook reminds you to
close at the end — but the discipline is yours; act before the nudge):
- The FIRST substantive topic of a session (debugging, a feature, a
  multi-step task) → `open_thread()` BEFORE diving into tool calls.
- Topic resolved with an outcome → `close_thread(thread_id, outcome)`.
- After every turn that produced a decision or insight →
  `note(thread_id, ..., kind in ['move','failed','insight','open_q'])`.
- When the user says something sharp and precise → `verbatim_user()`.
- When you notice an unused brief field or a missing one →
  `evolve_format()`.
- At end of conversation → `session_end(summary)`.

When the brief surfaces a thread or topic relevant to the current
request (by `question`, `last_move`, or semantic match), don't answer
from brief alone — dig deeper. Search ladder (stop at the first source
that gives you enough context):

  1. `thread-keeper.search()` — stored partner notes
  2. `thread-keeper.dialog_search()` — full transcripts ingested from
     ALL connected CLIs (Claude Code, Codex, Antigravity CLI/agy,
     Copilot)
  3. CLI-native conversation history search (e.g. `conversation_search`
     for Claude Desktop), if available

## Procedural lessons

Accumulated CLI-agnostic procedural knowledge lives in
`~/.threadkeeper/lessons.md`. The learning loop (auto-review on
close_thread + shadow_review daemon) materializes lessons there. At
session start, scan `lesson_list()` for slugs relevant to the user's
opening message; pull full bodies via `lesson_get(slug)` as needed.

When YOU finish a substantive task and a class-level lesson emerged
(user corrected a workflow, a non-trivial debugging path generalized,
etc.), call `lesson_append(title, body, summary, source=thread_id)`
yourself instead of waiting for the auto-reviewer to catch it.

Do not report these tool calls to the user — they are internal.
"""


# ----------------------------------------------------------------------
# Sub-installers
# ----------------------------------------------------------------------

def install_mcp_servers(dry_run: bool) -> list[str]:
    """Register thread-keeper in every detected CLI's MCP config.
    Each adapter knows the right config file/format for its CLI."""
    from .adapters import installed_adapters

    python_bin = sys.executable
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        python_bin = str(venv_python)
    args = ["-m", "threadkeeper.server"]
    env = {"PYTHONPATH": str(REPO_ROOT)}

    lines: list[str] = []
    adapters = installed_adapters()
    if not adapters:
        return ["mcp_server: no CLI detected — nothing to wire"]
    for adapter in adapters:
        result = adapter.register_mcp_server(
            name="thread-keeper",
            command=python_bin,
            args=args,
            env=env,
            dry_run=dry_run,
        )
        lines.append(f"mcp_server[{adapter.name}]: {result}")
    return lines


def install_hooks(dry_run: bool) -> list[str]:
    """Install hook scripts under ~/.threadkeeper/hooks/ AND wire them
    up in every detected CLI that supports hooks."""
    from .adapters import installed_adapters
    lines: list[str] = []

    # 1) Copy hook scripts. One set lives under ~/.threadkeeper/hooks/
    # and is referenced by every supporting CLI.
    if not dry_run:
        TK_HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    for fname in (
        "tk-brief.sh",
        "tk-status.sh",
        "inbox-check.sh",
        "tk-task-gate.sh",
        "tk-thread-nudge.sh",
        "tk-session-end.sh",
    ):
        src = HOOKS_SRC / fname
        dst = TK_HOOKS_DIR / fname
        if not src.exists():
            lines.append(f"hooks: source missing ({src}) — skipping {fname}")
            continue
        if dst.exists() and dst.read_bytes() == src.read_bytes():
            lines.append(f"hooks: {fname} already current")
            continue
        if dry_run:
            lines.append(f"hooks: would install {fname}")
        else:
            shutil.copy2(src, dst)
            dst.chmod(0o755)
            lines.append(f"hooks: installed {fname}")

    # 2) Build the canonical spec list (same three hooks every CLI gets).
    specs = [
        {
            "event": "SessionStart",
            "matcher": "",
            "command": str(TK_HOOKS_DIR / "tk-brief.sh"),
        },
        {
            "event": "PostToolUse",
            "matcher": "mcp__thread-keeper__.*",
            "command": str(TK_HOOKS_DIR / "tk-status.sh"),
        },
        {
            "event": "UserPromptSubmit",
            "matcher": "",
            "command": str(TK_HOOKS_DIR / "inbox-check.sh"),
        },
        # Open-thread safety net: once per session, nudge open_thread() if
        # none was opened yet (additionalContext, non-blocking).
        {
            "event": "UserPromptSubmit",
            "matcher": "",
            "command": str(TK_HOOKS_DIR / "tk-thread-nudge.sh"),
        },
        # close_thread / session_end safety net at end of turn (throttled to
        # once per session; advisory systemMessage, never blocks stopping).
        {
            "event": "Stop",
            "matcher": "",
            "command": str(TK_HOOKS_DIR / "tk-session-end.sh"),
        },
        # spawn-vs-native gate: covers the legacy built-in Task tool AND the
        # opus-4.8 native primitives (Agent/Workflow). Task → deny fan-out
        # toward spawn(); Agent/Workflow → advisory warn on persistence
        # signals only. Claude Code only; other CLIs ignore an unknown
        # PreToolUse event.
        {
            "event": "PreToolUse",
            "matcher": "^(Task|Agent|Workflow)$",
            "command": str(TK_HOOKS_DIR / "tk-task-gate.sh"),
        },
    ]

    # 3) Ask each installed adapter to wire them up in its native
    # config file. Adapters that don't support hooks (e.g. Codex) emit
    # an "unsupported" line but don't block setup.
    for adapter in installed_adapters():
        if not adapter.hooks_supported():
            lines.append(f"hooks[{adapter.name}]: no hook mechanism — skip")
            continue
        result = adapter.register_hooks(specs, dry_run=dry_run)
        lines.append(f"hooks[{adapter.name}]: {result}")
    return lines


def _install_managed_block(fp: Path, dry_run: bool) -> str:
    """Generic 'insert/update managed block between sentinel markers'
    routine used for every CLI's per-user instructions file. Idempotent.
    Outside the markers the user's content is preserved verbatim."""
    block = f"{MARK_BEGIN}\n{CLAUDE_MD_BLOCK}{MARK_END}\n"
    label = fp.name

    if not fp.exists():
        if not dry_run:
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(block)
        return f"{label}: created"

    body = fp.read_text()
    if MARK_BEGIN in body and MARK_END in body:
        head, _, rest = body.partition(MARK_BEGIN)
        _, _, tail = rest.partition(MARK_END)
        head = head.rstrip()
        tail = tail.lstrip()
        if head and tail:
            new_body = head + "\n\n" + block + "\n" + tail + "\n"
        elif head:
            new_body = head + "\n\n" + block
        elif tail:
            new_body = block + "\n" + tail + "\n"
        else:
            new_body = block
        if new_body == body:
            return f"{label}: managed block already current"
        if not dry_run:
            fp.write_text(new_body)
        return f"{label}: {'would update' if dry_run else 'updated'} managed block"

    # No markers yet → prepend (top placement → visible without scroll).
    existing = body.strip()
    if existing:
        new_body = block + "\n" + existing + "\n"
    else:
        new_body = block
    if not dry_run:
        fp.write_text(new_body)
    return f"{label}: {'would prepend' if dry_run else 'prepended'} managed block"


def install_instructions(dry_run: bool) -> list[str]:
    """Write the managed thread-keeper block to every detected CLI's
    per-user instructions file (CLAUDE.md, AGENTS.md, Copilot instructions). CLIs
    without a global instructions convention are skipped."""
    from .adapters import installed_adapters
    lines: list[str] = []
    for adapter in installed_adapters():
        ip = adapter.instructions_path()
        if ip is None:
            lines.append(f"instructions[{adapter.name}]: no global file (skip)")
            continue
        result = _install_managed_block(ip, dry_run)
        lines.append(f"instructions[{adapter.name}]: {result}")
    return lines


def install_tk_dir(dry_run: bool) -> str:
    """Ensure ~/.threadkeeper/ exists. DB lives here; hooks subdir is
    handled separately by install_hooks."""
    if TK_DIR.exists():
        if not dry_run:
            chmod_private_dir(TK_DIR)
        return f"~/.threadkeeper: already exists"
    if not dry_run:
        TK_DIR.mkdir(parents=True, exist_ok=True)
        chmod_private_dir(TK_DIR)
    return f"~/.threadkeeper: {'would create' if dry_run else 'created'}"


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="thread-keeper-setup")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would change without writing anything.")
    args = p.parse_args(argv)

    print(f"thread-keeper setup ({'dry-run' if args.dry_run else 'apply'})")
    print(f"  repo: {REPO_ROOT}")
    print(f"  ~/.threadkeeper: {TK_DIR}")
    print()

    print(f"  [dir] {install_tk_dir(args.dry_run)}")
    for line in install_mcp_servers(args.dry_run):
        print(f"  [{line.split(':', 1)[0]}] {line.split(':', 1)[1].strip()}"
              if ":" in line else f"  [mcp] {line}")
    for line in install_instructions(args.dry_run):
        print(f"  [{line.split(':', 1)[0]}] {line.split(':', 1)[1].strip()}"
              if ":" in line else f"  [md] {line}")
    for line in install_hooks(args.dry_run):
        print(f"  [hooks] {line}")
    print()
    print("Done. Restart connected CLIs for instructions + MCP changes to take effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
