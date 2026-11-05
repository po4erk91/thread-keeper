"""Host configuration advice needs a narrow condition, consent and rollback."""
import asyncio
import hashlib

import pytest

from tests.test_review_prompts import _bootstrap


UNSAFE = "Before launching WDA, read networksetup and reset the 127.0.0.1 proxy."
SAFE = (
    "Only when inspection has confirmed an observed test-owned proxy left by "
    "this test, ask the user for explicit consent before changing it. "
    "Save the original proxy settings, reset the proxy for the test, and "
    "restore the original settings in cleanup even if the test fails."
)
MUTATIONS = [
    (UNSAFE, "network"),
    ("networksetup -setwebproxystate Wi-Fi off", "network"),
    ("Unset HTTPS_PROXY before running tests.", "network"),
    ("Delete ~/.aws/credentials to fix login.", "credential"),
    ("rm -f ~/.aws/credentials", "credential"),
    ("security delete-generic-password -s example", "credential"),
    ("git config --global --unset credential.helper", "credential"),
    ("Rewrite ~/.zshrc to fix the shell environment.", "shell-global-config"),
    ("rm ~/.bashrc", "shell-global-config"),
    ("git config --global core.autocrlf false", "shell-global-config"),
    ("launchctl unload ~/Library/LaunchAgents/example.plist", "startup"),
    ("Disable the startup service before testing.", "startup"),
    ("chmod -R 777 ~/.ssh", "permission"),
    ("Change permissions on the home directory.", "permission"),
    ("pip install --user replacement-package", "package-state"),
    ("npm uninstall --global example-package", "package-state"),
]


@pytest.mark.parametrize("text,category", MUTATIONS)
def test_host_mutation_categories(text, category):
    from threadkeeper.consequential_state import screen_host_mutations
    assert category in screen_host_mutations(text)


@pytest.mark.parametrize("text", [
    "Inspect networksetup output and check whether a proxy is intentional.",
    "Read ~/.aws/credentials and report which profile is selected.",
    "Inspect ~/.zshrc without changing it.",
    "launchctl list; systemctl status example.service",
    "ls -ld ~/.ssh; stat ~/.config",
    "pip list; npm list --global",
    "git config --global --get credential.helper",
    "Do not reset the user's proxy automatically.",
    "Never delete global credentials as routine troubleshooting.",
    "Install a package in the project virtualenv.",
    "Reset the simulator proxy settings in the disposable simulator.",
    "chmod +x ./scripts/test.sh",
    SAFE,
])
def test_read_only_local_and_guarded_advice_is_clean(text):
    from threadkeeper.consequential_state import screen_host_mutations
    assert screen_host_mutations(text) == []


@pytest.mark.parametrize("text", [
    SAFE.replace("Only when inspection has confirmed an observed test-owned proxy left by this test", "Before every test"),
    SAFE.replace("ask the user for explicit consent before changing it", "do not ask for consent"),
    SAFE.replace("restore the original settings in cleanup even if the test fails", "continue the test"),
    SAFE + "\n\nAlways delete ~/.aws/credentials to fix login.",
    SAFE + " Always delete ~/.aws/credentials to fix login.",
    "Never reset the proxy; delete ~/.aws/credentials instead.",
    "git config --global --get credential.helper && rm ~/.aws/credentials",
    "Read git config --global --get credential.helper and delete credentials.",
    "Do not ask for consent, reset the proxy.",
    "Reset the user's proxy before running container tests.",
])
def test_incomplete_or_unrelated_guards_cannot_clear_warning(text):
    from threadkeeper.consequential_state import screen_host_mutations
    assert screen_host_mutations(text)


@pytest.mark.parametrize("origin", ["shadow_review", "candidate_review", "background_review", "evolve_apply"])
@pytest.mark.parametrize("artifact", ["lesson", "skill"])
def test_autonomous_create_refuses_unsafe_advice(tmp_path, monkeypatch, origin, artifact):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin=origin)
    if artifact == "lesson":
        result = pkg["lessons"].lesson_append("network recovery", UNSAFE)
        assert not (tmp_path / "lessons.md").exists()
    else:
        result = pkg["skills"].skill_manage("create", "network-recovery", UNSAFE, description="Recovery")
        assert not (tmp_path / "skills/network-recovery/SKILL.md").exists()
    assert result.startswith("ERR owner_review_required")


@pytest.mark.parametrize("text,category", MUTATIONS)
def test_each_category_is_refused_at_both_write_boundaries(tmp_path, monkeypatch, text, category):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="background_review")
    lesson = pkg["lessons"].lesson_append("recovery procedure", text)
    skill = pkg["skills"].skill_manage("create", "recovery-procedure", text, description="Recovery")
    assert lesson.startswith("ERR owner_review_required") and category in lesson
    assert skill.startswith("ERR owner_review_required") and category in skill


@pytest.mark.parametrize("artifact", ["lesson", "skill"])
def test_guarded_autonomous_create_and_patch(tmp_path, monkeypatch, artifact):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="background_review")
    if artifact == "lesson":
        assert pkg["lessons"].lesson_append("network recovery", SAFE).startswith("ok")
        patch = pkg["lessons"].lesson_patch
        result = patch("network-recovery", "ask the user for explicit consent before changing it", "skip consent")
    else:
        assert pkg["skills"].skill_manage("create", "network-recovery", SAFE, description="Recovery").startswith("ok")
        result = pkg["skills"].skill_manage("patch", "network-recovery", old_string="ask the user for explicit consent before changing it", new_string="skip consent")
    assert result.startswith("ERR owner_review_required")


@pytest.mark.parametrize("action", ["edit", "patch", "write_file"])
def test_skill_updates_use_the_same_screen(tmp_path, monkeypatch, action):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="background_review")
    manage = pkg["skills"].skill_manage
    assert manage("create", "network-recovery", "Inspect the proxy.", description="Recovery").startswith("ok")
    md = tmp_path / "skills/network-recovery/SKILL.md"
    before = md.read_bytes()
    args = {"content": md.read_text().replace("Inspect the proxy.", UNSAFE)}
    if action == "patch":
        args = {"old_string": "Inspect the proxy.", "new_string": UNSAFE}
    if action == "write_file":
        args = {"sub_path": "references/recovery.md", "content": UNSAFE}
    assert manage(action, "network-recovery", **args).startswith("ERR owner_review_required")
    assert md.read_bytes() == before
    assert not (md.parent / "references/recovery.md").exists()


def test_lesson_summary_is_screened_and_replacement_is_checked(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="background_review")
    append = pkg["lessons"].lesson_append
    assert append("network recovery", "Inspect the proxy.", summary=UNSAFE).startswith("ERR owner_review_required")
    assert append("network recovery", SAFE).startswith("ok")
    assert append("network recovery", UNSAFE).startswith("ERR owner_review_required")


def test_existing_protected_lesson_warns_without_mutation_and_owner_can_repair(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    lesson = pkg["lessons"]
    assert lesson.lesson_append("network recovery", UNSAFE).startswith("ok")
    stored = (tmp_path / "lessons.md").read_bytes()
    assert "OWNER REVIEW REQUIRED" in lesson.lesson_list()
    assert "OWNER REVIEW REQUIRED" in lesson.lesson_get("network-recovery")
    from threadkeeper import curator, lessons
    item = next(lessons.iter_lessons())
    assert "OWNER REVIEW REQUIRED" in curator._format_lesson(item)
    monkeypatch.setattr(lesson, "WRITE_ORIGIN", "curator")
    monkeypatch.setattr(lesson, "is_curator_verifier", lambda: True)
    assert lesson.lesson_patch("network-recovery", UNSAFE, SAFE).startswith("ERR owner_review_required")
    assert (tmp_path / "lessons.md").read_bytes() == stored
    monkeypatch.setattr(lesson, "WRITE_ORIGIN", "foreground")
    assert lesson.lesson_patch("network-recovery", UNSAFE, SAFE).startswith("ok")
    assert "OWNER REVIEW REQUIRED" not in lesson.lesson_get("network-recovery")


def test_protected_skill_warning_covers_curator_protocol_and_watcher(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    manage = pkg["skills"].skill_manage
    assert manage("create", "network-recovery", UNSAFE, description="Recovery").startswith("ok")
    md = tmp_path / "skills/network-recovery/SKILL.md"
    stored = md.read_bytes()
    assert "OWNER REVIEW REQUIRED" in pkg["skills"].skill_list()
    from threadkeeper.skill_audit import build_skill_audit, format_skill_checklist
    assert "owner_review_required:network" in format_skill_checklist(build_skill_audit(pkg["db"].get_db()))
    from threadkeeper.mcp_skills import skill_catalog
    entry = next(item for item in skill_catalog() if item.name == "network-recovery")
    assert "OWNER REVIEW REQUIRED" in entry.frontmatter["description"]
    from threadkeeper._mcp import mcp
    from mcp.server.mcpserver.exceptions import ResourceNotFoundError
    with pytest.raises(ResourceNotFoundError, match="OWNER REVIEW REQUIRED"):
        asyncio.run(mcp.read_resource(entry.uri))
    from threadkeeper.skill_watcher import _scan_once
    _scan_once(pkg["db"].get_db())
    _scan_once(pkg["db"].get_db())
    flags = pkg["db"].get_db().execute("SELECT target, summary FROM events WHERE kind='host_state_review'").fetchall()
    assert len(flags) == 1
    assert "127.0.0.1" not in str([tuple(row) for row in flags])
    assert "network-recovery" not in str([tuple(row) for row in flags])
    monkeypatch.setattr(pkg["skills"], "WRITE_ORIGIN", "curator")
    monkeypatch.setattr(pkg["skills"], "is_curator_verifier", lambda: True)
    assert manage("patch", "network-recovery", old_string=UNSAFE, new_string=SAFE).startswith("ERR owner_review_required")
    assert md.read_bytes() == stored


def test_telemetry_contains_only_category_and_fingerprint(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch, write_origin="background_review")
    text = "Reset proxy at private-host.example:31337 before tests."
    for _ in range(2):
        pkg["lessons"].lesson_append("private-host-sensitive-title", text)
    rows = pkg["db"].get_db().execute("SELECT target, summary FROM events WHERE kind='host_state_review'").fetchall()
    assert len(rows) == 1
    screened = "private-host-sensitive-title\n\n" + text
    assert rows[0]["target"] == hashlib.sha256(screened.encode()).hexdigest()
    assert rows[0]["summary"] == "categories=network"


def test_support_file_edit_warns_and_owner_repair_clears_delivery(tmp_path, monkeypatch):
    pkg = _bootstrap(tmp_path, monkeypatch)
    manage = pkg["skills"].skill_manage
    assert manage("create", "network-recovery", "Inspect the proxy.", description="Recovery").startswith("ok")
    assert manage("write_file", "network-recovery", UNSAFE, sub_path="references/recovery.md").startswith("ok")
    from threadkeeper._mcp import mcp
    from threadkeeper.mcp_skills import skill_catalog
    from threadkeeper.skill_watcher import _scan_once
    _scan_once(pkg["db"].get_db())
    assert pkg["db"].get_db().execute("SELECT COUNT(*) FROM events WHERE kind='host_state_review'").fetchone()[0] == 1
    assert "OWNER REVIEW REQUIRED" in pkg["skills"].skill_list()
    entry = next(item for item in skill_catalog() if item.name == "network-recovery")
    assert "OWNER REVIEW REQUIRED" in entry.frontmatter["description"]
    assert manage("write_file", "network-recovery", SAFE, sub_path="references/recovery.md").startswith("ok")
    assert "OWNER REVIEW REQUIRED" not in pkg["skills"].skill_list()
    resources = asyncio.run(mcp.read_resource(entry.uri))
    assert resources[0].content == (tmp_path / "skills/network-recovery/SKILL.md").read_bytes()
