"""Public GitHub body scrubber used by privileged evolve children."""
from __future__ import annotations

import json


def test_wrapped_gh_redacts_public_body_before_real_gh(tmp_path, monkeypatch):
    from threadkeeper.github_safety import run_wrapped_gh

    capture = tmp_path / "argv.json"
    fake_gh = tmp_path / "gh-real"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o700)
    monkeypatch.setenv("CAPTURE", str(capture))

    body = (
        "Token ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa and "
        "path /Users/alice/.threadkeeper/db.sqlite"
    )
    rc = run_wrapped_gh(
        ["issue", "create", "--title", "safe", "--body", body],
        real_gh=str(fake_gh),
    )

    assert rc == 0
    argv = json.loads(capture.read_text(encoding="utf-8"))
    safe_body = argv[argv.index("--body") + 1]
    assert "ghp_aaaaaaaa" not in safe_body
    assert "/Users/alice" not in safe_body
    assert "[REDACTED_SECRET]" in safe_body
    assert "[REDACTED_HOME_PATH]" in safe_body


def test_body_file_is_sanitized_before_real_gh(tmp_path, monkeypatch):
    from threadkeeper.github_safety import run_wrapped_gh

    raw = tmp_path / "body.md"
    raw.write_text(
        "OpenAI sk-proj-aaaaaaaaaaaaaaaaaaaaaaaa and /home/bob/private.txt",
        encoding="utf-8",
    )
    capture = tmp_path / "body_seen.txt"
    fake_gh = tmp_path / "gh-real"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "p = Path(args[args.index('--body-file') + 1])\n"
        "Path(os.environ['CAPTURE']).write_text(p.read_text())\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o700)
    monkeypatch.setenv("CAPTURE", str(capture))

    rc = run_wrapped_gh(
        ["pr", "create", "--title", "safe", "--body-file", str(raw)],
        real_gh=str(fake_gh),
    )

    assert rc == 0
    seen = capture.read_text(encoding="utf-8")
    assert "sk-proj-" not in seen
    assert "/home/bob" not in seen
    assert "[REDACTED_SECRET]" in seen
    assert "[REDACTED_HOME_PATH]" in seen
