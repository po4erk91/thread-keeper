"""Shared helpers for installing Claude-Code-style hooks into a JSON
config file. Claude Code and Gemini both honor the same shape
(`settings.json["hooks"]`), so the merging logic is identical — only
the target file path differs. Pulled out so both adapters can call it
without code duplication.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


def install_claude_style_hooks(
    settings_path: Path,
    specs: Iterable[dict],
    dry_run: bool = False,
) -> str:
    """Merge `specs` into `settings_path` under the "hooks" key.

    Each spec: {event: str, command: str, matcher: str}.

    Idempotent: for each (event, command) pair, leave existing entries
    in place (and update matcher if it differs); add a new entry if the
    command isn't already present. Other hooks (from the user or other
    plugins) are preserved.
    """
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            return f"{settings_path.name}: malformed JSON — refused"
    else:
        settings = {}
    hooks = settings.setdefault("hooks", {})

    changed = False
    for spec in specs:
        event = spec["event"]
        command = spec["command"]
        matcher = spec.get("matcher", "")
        blocks = hooks.get(event, [])
        # Look for an existing block whose first hook command matches.
        found = False
        for block in blocks:
            inner = block.get("hooks") or []
            for h in inner:
                if h.get("command") == command:
                    found = True
                    if block.get("matcher", "") != matcher:
                        block["matcher"] = matcher
                        changed = True
                    break
            if found:
                break
        if not found:
            new_block = {
                "hooks": [{"type": "command", "command": command}],
            }
            if matcher:
                new_block["matcher"] = matcher
            blocks.append(new_block)
            changed = True
        hooks[event] = blocks

    if not changed:
        return f"{settings_path.name}: hooks already current"
    if dry_run:
        return f"{settings_path.name}: would update hooks"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2))
    return f"{settings_path.name}: hooks updated"
