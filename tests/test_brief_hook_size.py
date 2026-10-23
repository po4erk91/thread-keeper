"""The SessionStart brief stays under the CLI's hook output limit."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "tk-brief.sh"


def _run_hook(tmp_path: Path, context_lines: int) -> str:
    fake_python = tmp_path / "python"
    body = "\\n".join(f"rule {i}: " + "x" * 60 for i in range(context_lines))
    fake_python.write_text(
        "#!/bin/sh\ncat >/dev/null\n"
        f"printf 'STATUS\\tthread-keeper: ok\\nCONTEXT\\t{body}\\n'\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "THREADKEEPER_PYTHON": str(fake_python),
    }
    proc = subprocess.run(
        ["bash", str(HOOK)], input="{}", capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def test_oversized_brief_is_cut_at_a_line_break_under_the_hook_limit(tmp_path):
    context = _run_hook(tmp_path, context_lines=400)

    assert len(context) <= 9000
    assert context.endswith("call brief() for the rest]")
    assert "\nrule 0: " in context
    kept = context.splitlines()[-2]
    assert kept.startswith("rule ") and kept.endswith("x" * 60)


def test_brief_under_the_limit_is_passed_through_whole(tmp_path):
    context = _run_hook(tmp_path, context_lines=20)

    assert "rule 19: " in context
    assert "trimmed" not in context


def test_brief_without_line_breaks_is_cut_at_the_limit(tmp_path):
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\ncat >/dev/null\n"
        "printf 'STATUS\\tok\\nCONTEXT\\t%s\\n' \"$(head -c 20000 /dev/zero | tr '\\0' x)\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "THREADKEEPER_PYTHON": str(fake_python),
    }
    proc = subprocess.run(
        ["bash", str(HOOK)], input="{}", capture_output=True, text=True, env=env, timeout=30,
    )

    context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert 8000 < len(context) <= 9000
    assert context.endswith("call brief() for the rest]")
