"""Session-protocol hooks stay silent inside spawned background children."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "scripts" / "hooks"


@pytest.mark.parametrize(
    "script", ["tk-brief.sh", "tk-thread-nudge.sh", "tk-session-end.sh"],
)
def test_session_hooks_exit_before_work_in_spawned_children(tmp_path, script):
    marker = tmp_path / "python-ran"
    fake_python = tmp_path / "python"
    fake_python.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    fake_python.chmod(0o700)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "THREADKEEPER_PYTHON": str(fake_python),
        "THREADKEEPER_STATE_DIR": str(tmp_path / "state"),
        "THREADKEEPER_SPAWNED_CHILD": "1",
    }

    proc = subprocess.run(
        ["bash", str(HOOKS / script)],
        input='{"session_id": "s1", "prompt": "hello"}',
        capture_output=True, text=True, env=env, timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert not marker.exists()
    assert not (tmp_path / "state").exists()
