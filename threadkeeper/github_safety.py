"""Safety wrapper for daemon-authored public GitHub bodies.

The evolve reviewer/applier children may run `gh issue create` / `gh pr create`
from a bypassPermissions shell. This module is used in two ways:

- as a PATH-prepended `gh` wrapper installed by spawn() for privileged evolve
  children, so public issue/PR bodies are scrubbed before the real gh sees them;
- as a small CLI for explicitly sanitizing a body file.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence


class GithubBodySafetyError(ValueError):
    """Raised when a public GitHub body still contains unsafe content."""


_HOME_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])/(?:Users|home)/[A-Za-z0-9._-]+"
    r"(?:/[^\s`\"'<>)]*)?"
)

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    (
        "openai_key",
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9][A-Za-z0-9_-]{18,}\b"),
    ),
    (
        "anthropic_key",
        re.compile(r"\bsk-ant-[A-Za-z0-9_-]{18,}\b"),
    ),
    (
        "slack_token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{18,}\b"),
    ),
    (
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    (
        "generic_secret_assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd|authorization)"
            r"\s*[:=]\s*['\"]?[A-Za-z0-9_./+=:@-]{8,}"
        ),
    ),
    (
        "bearer_token",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_./+=:@-]{16,}\b"),
    ),
)


def _secret_pattern_names(text: str) -> list[str]:
    return [name for name, pat in _SECRET_PATTERNS if pat.search(text)]


def sanitize_public_github_body(body: str) -> str:
    """Redact local home paths and common token shapes from a public body.

    The returned text is re-scanned; if any known secret shape survives, the
    caller gets a hard failure instead of an unsafe body.
    """
    text = str(body or "")
    text = _HOME_PATH_RE.sub("[REDACTED_HOME_PATH]", text)
    for _name, pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED_SECRET]", text)
    remaining = _secret_pattern_names(text)
    if remaining:
        raise GithubBodySafetyError(
            "unsafe GitHub body after redaction: " + ",".join(remaining)
        )
    if _HOME_PATH_RE.search(text):
        raise GithubBodySafetyError(
            "unsafe GitHub body after redaction: home_path"
        )
    return text


def sanitize_gh_body_args(args: Sequence[str]) -> tuple[list[str], list[Path]]:
    """Return argv with public GitHub body flags scrubbed.

    `gh issue create`, `gh issue comment`, and `gh pr create` are rewritten.
    Other gh commands are passed through unchanged. Returned temp files must be
    unlinked by the caller after the real gh exits.
    """
    out = list(args)
    if len(out) < 3 or out[0:2] not in (
        ["issue", "create"],
        ["issue", "comment"],
        ["pr", "create"],
    ):
        return out, []

    cleanup: list[Path] = []
    i = 2
    while i < len(out):
        arg = out[i]
        if arg in ("--body", "-b") and i + 1 < len(out):
            out[i + 1] = sanitize_public_github_body(out[i + 1])
            i += 2
            continue
        if arg.startswith("--body="):
            out[i] = "--body=" + sanitize_public_github_body(arg.split("=", 1)[1])
            i += 1
            continue
        if arg == "--body-file" and i + 1 < len(out):
            raw_path = Path(out[i + 1])
            raw = raw_path.read_text(encoding="utf-8", errors="replace")
            safe = sanitize_public_github_body(raw)
            fd, tmp_name = tempfile.mkstemp(
                prefix="threadkeeper-gh-body-", suffix=".md"
            )
            tmp = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(safe)
            try:
                tmp.chmod(0o600)
            except OSError:
                pass
            out[i + 1] = str(tmp)
            cleanup.append(tmp)
            i += 2
            continue
        if arg.startswith("--body-file="):
            raw_path = Path(arg.split("=", 1)[1])
            raw = raw_path.read_text(encoding="utf-8", errors="replace")
            safe = sanitize_public_github_body(raw)
            fd, tmp_name = tempfile.mkstemp(
                prefix="threadkeeper-gh-body-", suffix=".md"
            )
            tmp = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(safe)
            try:
                tmp.chmod(0o600)
            except OSError:
                pass
            out[i] = "--body-file=" + str(tmp)
            cleanup.append(tmp)
            i += 1
            continue
        i += 1
    return out, cleanup


def _resolve_real_gh() -> str:
    env = os.environ.get("THREADKEEPER_REAL_GH", "").strip()
    if env:
        return env
    wrapper_dir = os.environ.get("THREADKEEPER_GH_WRAPPER_DIR", "").strip()
    path_parts = [
        p for p in os.environ.get("PATH", "").split(os.pathsep)
        if p and Path(p) != Path(wrapper_dir)
    ]
    found = shutil.which("gh", path=os.pathsep.join(path_parts))
    return found or ""


def run_wrapped_gh(args: Sequence[str], real_gh: str | None = None) -> int:
    """Run the real gh with issue/PR bodies scrubbed first."""
    gh_bin = real_gh or _resolve_real_gh()
    if not gh_bin:
        print("thread-keeper gh safety: real gh binary not found", file=sys.stderr)
        return 127
    try:
        safe_args, cleanup = sanitize_gh_body_args(args)
    except (GithubBodySafetyError, OSError) as e:
        print(f"thread-keeper gh safety refused body: {e}", file=sys.stderr)
        return 2
    try:
        proc = subprocess.run([gh_bin, *safe_args], check=False)
        return int(proc.returncode)
    finally:
        for p in cleanup:
            try:
                p.unlink()
            except OSError:
                pass


def _sanitize_file(input_path: str, output_path: str) -> int:
    raw = Path(input_path).read_text(encoding="utf-8", errors="replace")
    safe = sanitize_public_github_body(raw)
    out = Path(output_path)
    out.write_text(safe, encoding="utf-8")
    try:
        out.chmod(0o600)
    except OSError:
        pass
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args and args[0] == "--sanitize-file":
        parser = argparse.ArgumentParser()
        parser.add_argument("--sanitize-file", action="store_true")
        parser.add_argument("--input", required=True)
        parser.add_argument("--output", required=True)
        ns = parser.parse_args(args)
        try:
            return _sanitize_file(ns.input, ns.output)
        except (GithubBodySafetyError, OSError) as e:
            print(f"thread-keeper gh safety refused body: {e}", file=sys.stderr)
            return 2
    return run_wrapped_gh(args)


if __name__ == "__main__":  # pragma: no cover - exercised via unit helpers.
    raise SystemExit(main())
