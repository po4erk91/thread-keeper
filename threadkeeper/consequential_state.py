"""Keep learned host configuration changes behind an owner review boundary.

This deterministic screen complements injection screening. It does not infer
permission from an incident: each procedure must state its precondition,
consent requirement and restoration plan. Reads only annotate existing memory.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import time


_CHANGE = (
    r"\b(?:reset|unset|clear|delete|remove|disable|enable|change|rewrite|"
    r"overwrite|replace|revoke|rotate|install|uninstall|edit|set|rm|truncate)\b"
)
_HOST_PATH = r"(?:~/|\$HOME/|/etc/|/usr/|/Library/|/System/|/Users/|/home/)"
_SUBJECTS = {
    "network": (
        r"\b(?:proxy|proxies|network\s+(?:settings|configuration)|"
        r"dns\s+(?:settings|servers)|vpn)\b|\b(?:https?|all|no)_proxy\b"
    ),
    "credential": r"\b(?:credentials?|keychain|credential\.helper)\b|(?:~/|\$HOME/)(?:\.aws|\.ssh|\.netrc|\.npmrc)|\b(?:global|system|user)\s+(?:tokens?|passwords?|keys?)\b",
    "shell-global-config": r"(?:\.zshrc|\.bashrc|\.bash_profile|\.profile|\.gitconfig)|/etc/(?:profile|environment)|\b(?:global|system|user-wide|host-wide)\s+(?:config\w*|settings|environment)\b",
    "startup": r"\b(?:startup\s+(?:services?|items?)|login\s+items?|launch\s+agents?|launch\s+daemons?|system\s+services?)\b|(?:LaunchAgents|LaunchDaemons)",
    "permission": rf"\b(?:permissions?|ownership)\b.{{0,80}}(?:{_HOST_PATH}|\b(?:home|system|global)\b)",
    "package-state": r"\b(?:global|system|user-wide|host-wide)\s+packages?\b",
}
_COMMANDS = {
    "network": r"\bnetworksetup\s+-set\w+|\b(?:netsh|scutil)\b.{0,80}\b(?:reset|set|delete)\b",
    "credential": r"\bsecurity\s+(?:delete|add|set)-\w+|\bgit\s+config\b.{0,100}--(?:global|system)\b.{0,100}\bcredential\.",
    "shell-global-config": r"\bgit\s+config\b.{0,100}--(?:global|system)\b|(?:>>?|\bsed\s+-i\b).{0,100}(?:\.zshrc|\.bashrc|\.profile|/etc/)",
    "startup": r"\blaunchctl\s+(?:unload|load|bootout|bootstrap|disable|enable)\b|\b(?:sudo\s+)?systemctl\s+(?:disable|enable|stop|start|mask|unmask)\b",
    "permission": rf"\b(?:chmod|chown|chgrp)\b.{{0,120}}{_HOST_PATH}",
    "package-state": r"\b(?:pip\d*\s+(?:install|uninstall)\b.{0,100}--user|npm\s+(?:install|uninstall|update)\b.{0,100}(?:--global\b|-g\b)|brew\s+(?:install|uninstall|upgrade)|(?:apt(?:-get)?|dnf|yum)\s+(?:install|remove|upgrade))\b",
}
_NEGATIVE = re.compile(
    rf"^(?:[-*>\s]*)(?:do\s+not|don't|never|avoid)\s+(?:automatically\s+)?(?={_CHANGE})", re.I,
)
_LOCAL_NETWORK = re.compile(
    r"\b(?:simulator|container|project-local)\s+(?:network\s+)?(?:proxy|proxies|network\s+settings)\b", re.I,
)
_HOST_NETWORK = re.compile(r"\b(?:host|system|machine|macos|wi-fi|networksetup|vpn)\b", re.I)
_TEXT_SUFFIXES = {".md", ".txt", ".sh", ".bash", ".zsh", ".py", ".json", ".yaml", ".yml", ".toml"}


def _guarded(procedure: str, category: str) -> bool:
    narrow = re.search(r"\bonly\s+(?:when|if|after)\b", procedure, re.I)
    observed = re.search(r"\b(?:observed|confirmed|verified|diagnosed)\b", procedure, re.I)
    consent = re.search(
        r"\b(?:ask|obtain|require|get|with)\b[^.!?\n]{0,80}\b(?:consent|approval|permission)\b",
        procedure, re.I,
    )
    denied = re.search(r"\b(?:skip|without|no|not|never|don't)\b[^.!?\n]{0,35}\b(?:consent|approval|permission|restore|restoration)\b", procedure, re.I)
    snapshot = re.search(r"\b(?:save|capture|snapshot|back\s+up)\b[^.!?\n]{0,60}\b(?:original|current|prior|existing)\b", procedure, re.I)
    restore = re.search(r"\brestore\b[^.!?\n]{0,50}\b(?:original|prior|previous|saved)\b", procedure, re.I)
    precondition = re.split(r"[,\n]|(?<=[.!?])\s", procedure, maxsplit=1)[0]
    scoped = re.search(_SUBJECTS[category], precondition, re.I)
    return bool(narrow and observed and scoped and consent and not denied and snapshot and restore)


def _clause_categories(clause: str) -> set[str]:
    if _NEGATIVE.search(clause):
        return set()
    read_only_git = re.search(r"\bgit\s+config\b", clause) and re.search(
        r"--(?:get(?:-all|-regexp)?|list|show-origin)\b", clause,
    )
    categories = set()
    for category, subject in _SUBJECTS.items():
        advice = rf"{_CHANGE}.{{0,100}}(?:{subject})|(?:{subject}).{{0,60}}{_CHANGE}"
        command = re.search(_COMMANDS[category], clause, re.I)
        if read_only_git and category in {"credential", "shell-global-config"}:
            command = None
        if re.search(advice, clause, re.I) or command:
            categories.add(category)
    if _LOCAL_NETWORK.search(clause) and not _HOST_NETWORK.search(clause):
        categories.discard("network")
    return categories


def screen_host_mutations(text: str) -> list[str]:
    """Return safe categories for unguarded host mutations, never host values.

    Guards apply within one procedure paragraph, not across an entire skill.
    The screen is deliberately conservative, not a semantic safety proof.
    """
    categories: set[str] = set()
    for procedure in re.split(r"\n\s*\n", text):
        for clause in re.split(r"\n|;\s*|&&|\|\||\b(?:but|instead|then)\s+|(?<=[.!?])\s+(?=[A-Z])", procedure):
            categories.update(
                category for category in _clause_categories(clause)
                if not _guarded(procedure, category)
            )
    return sorted(categories)


def review_warning(categories: list[str]) -> str:
    if not categories:
        return ""
    return (
        f"[OWNER REVIEW REQUIRED: {','.join(categories)}] Do not apply this "
        "host-changing guidance until the owner reviews it. State a confirmed "
        "narrow precondition, require consent before changing intentional "
        "settings, and save and restore the original state, including on failure."
    )


def owner_review_warning(text: str) -> str:
    return review_warning(screen_host_mutations(text))


def record_review(conn, text: str, categories: list[str]) -> None:
    """Deduplicate privacy-safe telemetry; omit names, paths and host values."""
    if not categories:
        return
    digest = hashlib.sha256(text.encode()).hexdigest()
    summary = f"categories={','.join(sorted(categories))}"
    if conn.execute(
        "SELECT 1 FROM events WHERE kind='host_state_review' AND target=? AND summary=?",
        (digest, summary),
    ).fetchone():
        return
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES ('', 'host_state_review', ?, ?, ?)",
        (digest, summary, int(time.time())),
    )
    conn.commit()


def host_mutation_refusal(conn, text: str) -> str:
    categories = screen_host_mutations(text)
    if not categories:
        return ""
    record_review(conn, text, categories)
    return f"ERR owner_review_required {review_warning(categories)}"


def skill_review_categories(directory: Path) -> list[str]:
    """Inspect skill instructions and text support files without editing them."""
    categories: set[str] = set()
    for path in skill_text_files(directory):
        try:
            categories.update(screen_host_mutations(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError):
            continue
    return sorted(categories)


def skill_text_files(directory: Path):
    """Yield readable instruction formats while keeping symlinks out of scope."""
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() in _TEXT_SUFFIXES or not path.suffix:
            yield path
