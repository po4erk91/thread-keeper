"""Best-effort macOS menu-bar app autoinstall/autolaunch.

This runs from the MCP server entry point, so it must never write to stdout:
stdio is reserved for the MCP protocol. All diagnostics go to a log file and
failures are non-fatal.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import (
    BACKGROUND_DAEMONS_ALLOWED,
    MENUBAR_AUTO_LAUNCH,
    TASK_LOG_DIR,
)


APP_NAME = "ThreadKeeperAgentStatus"
APP_BUNDLE = f"{APP_NAME}.app"
LAUNCH_LABEL = "local.threadkeeper.agent-status"

_attempted = False


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _source_dir() -> Path:
    return _repo_root() / "apps" / "macos-agent-status"


def _installed_app() -> Path:
    return Path.home() / "Applications" / APP_BUNDLE


def _launch_agent_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"


def _log_path() -> Path:
    return TASK_LOG_DIR / "menubar-autolaunch.log"


def _log(message: str) -> None:
    try:
        TASK_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _log_path().open("a", encoding="utf-8") as f:
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            f.write(f"{ts} {message}\n")
    except OSError:
        pass


def _run(args: list[str], timeout: int = 60, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )


def _app_running() -> bool:
    try:
        r = _run(["pgrep", "-x", APP_NAME], timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _source_mtime(src: Path) -> float:
    newest = 0.0
    for name in ("ThreadKeeperAgentStatus.swift", "Info.plist", "build.sh"):
        p = src / name
        if p.exists():
            newest = max(newest, p.stat().st_mtime)
    return newest


def _app_is_current(src: Path, app: Path) -> bool:
    binary = app / "Contents" / "MacOS" / APP_NAME
    plist = app / "Contents" / "Info.plist"
    if not binary.exists() or not plist.exists():
        return False
    return binary.stat().st_mtime >= _source_mtime(src)


def _ensure_status_command() -> None:
    """Create a fallback command when package entry points are stale/missing."""
    if shutil.which("tk-agent-status"):
        return
    local_bin = Path.home() / ".local" / "bin"
    wrapper = local_bin / "tk-agent-status"
    try:
        local_bin.mkdir(parents=True, exist_ok=True)
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            f"cd {shlex_quote(str(_repo_root()))}\n"
            f"exec {shlex_quote(sys.executable)} -m threadkeeper.agent_status \"$@\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
    except OSError as e:
        _log(f"status_command_failed err={e}")


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def _install_app(src: Path, app: Path) -> bool:
    if _app_is_current(src, app):
        return True
    build = src / "build.sh"
    if not build.exists():
        _log(f"build_script_missing path={build}")
        return False
    try:
        r = _run([str(build)], timeout=120, cwd=src)
    except (OSError, subprocess.SubprocessError) as e:
        _log(f"build_failed err={e}")
        return False
    if r.returncode != 0:
        _log(f"build_failed rc={r.returncode} output={r.stdout[-4000:]}")
        return False

    built = Path((r.stdout or "").strip().splitlines()[-1])
    if not built.exists():
        _log(f"built_app_missing output={r.stdout[-1000:]}")
        return False
    try:
        app.parent.mkdir(parents=True, exist_ok=True)
        if app.exists():
            shutil.rmtree(app)
        shutil.copytree(built, app)
    except OSError as e:
        _log(f"copy_app_failed err={e}")
        return False
    return True


def _write_launch_agent(app: Path) -> Path | None:
    plist = _launch_agent_plist()
    try:
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(
            f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LAUNCH_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string>
    <string>-a</string>
    <string>{app}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>
""",
            encoding="utf-8",
        )
    except OSError as e:
        _log(f"launch_agent_write_failed err={e}")
        return None
    return plist


def _bootstrap_launch_agent(plist: Path) -> None:
    uid = os.getuid()
    domain = f"gui/{uid}"
    for args in (
        ["launchctl", "bootout", f"{domain}/{LAUNCH_LABEL}"],
        ["launchctl", "bootstrap", domain, str(plist)],
    ):
        try:
            r = _run(args, timeout=5)
        except (OSError, subprocess.SubprocessError) as e:
            _log(f"launchctl_failed args={args} err={e}")
            continue
        if r.returncode != 0 and args[1] != "bootout":
            _log(f"launchctl_failed args={args} rc={r.returncode} output={r.stdout[-1000:]}")


def ensure_menubar_app() -> None:
    """Install and launch the macOS menu-bar app when the MCP server starts."""
    global _attempted
    if _attempted:
        return
    _attempted = True

    if platform.system() != "Darwin":
        return
    if not MENUBAR_AUTO_LAUNCH or not BACKGROUND_DAEMONS_ALLOWED:
        return

    src = _source_dir()
    if not src.exists():
        _log(f"source_missing path={src}")
        return

    TASK_LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = TASK_LOG_DIR / "menubar-autolaunch.lock"
    try:
        import fcntl

        with lock_path.open("w") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return

            _ensure_status_command()
            app = _installed_app()
            if not _install_app(src, app):
                return
            plist = _write_launch_agent(app)
            if plist is None:
                return
            if not _app_running():
                _bootstrap_launch_agent(plist)
                if not _app_running():
                    try:
                        _run(["open", str(app)], timeout=10)
                    except (OSError, subprocess.SubprocessError) as e:
                        _log(f"open_failed err={e}")
    except OSError as e:
        _log(f"autolaunch_failed err={e}")
