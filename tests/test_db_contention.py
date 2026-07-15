"""Multi-process regression coverage for the shared SQLite write path."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


def test_concurrent_processes_finish_without_lock_errors(tmp_path: Path):
    script = Path(__file__).parents[1] / "scripts" / "db_stress.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--db",
            str(tmp_path / "stress.db"),
            "--processes",
            "8",
            "--ops",
            "40",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["errors"] == []
    assert report["actual_writes"] == report["expected_writes"] == 320
    assert report["quick_check"] == "ok"
    assert report["write_latency_ms"]["p99"] > 0
    assert report["writes_per_second"] > 0
