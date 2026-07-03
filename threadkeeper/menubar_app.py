"""Best-effort macOS menu-bar app autoinstall/autolaunch.

This runs from the MCP server entry point, so it must never write to stdout:
stdio is reserved for the MCP protocol. All diagnostics go to a log file and
failures are non-fatal.
"""
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import (
    MENUBAR_AUTO_LAUNCH,
    SPAWNED_CHILD,
    TASK_LOG_DIR,
    WRITE_ORIGIN,
)


APP_NAME = "ThreadKeeperAgentStatus"
APP_BUNDLE = f"{APP_NAME}.app"
LAUNCH_LABEL = "local.threadkeeper.agent-status"
SOURCE_FILES = ("ThreadKeeperAgentStatus.swift", "Info.plist", "build.sh")
SOURCE_FINGERPRINT_FILE = "threadkeeper-source.sha256"

_attempted = False


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _package_source_dir() -> Path:
    return Path(__file__).resolve().parent / "assets" / "macos-agent-status"


def _dev_source_dir() -> Path:
    return _repo_root() / "apps" / "macos-agent-status"


def _source_dir() -> Path:
    dev = _dev_source_dir()
    if dev.exists():
        return dev
    return _package_source_dir()


def _prepare_build_source(src: Path) -> Path:
    build_src = TASK_LOG_DIR / "menubar-build" / "source"
    if build_src.exists():
        shutil.rmtree(build_src)
    build_src.mkdir(parents=True, exist_ok=True)
    for name in SOURCE_FILES:
        shutil.copy2(src / name, build_src / name)
    return build_src


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


def _app_pids() -> list[int]:
    try:
        r = _run(["pgrep", "-x", APP_NAME], timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    pids = []
    for line in (r.stdout or "").splitlines():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            continue
    return pids


def _app_running() -> bool:
    return bool(_app_pids())


def _process_start_time(pid: int) -> float | None:
    try:
        r = _run(["ps", "-o", "lstart=", "-p", str(pid)], timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    raw = (r.stdout or "").strip()
    if not raw:
        return None
    try:
        return time.mktime(datetime.strptime(raw, "%a %b %d %H:%M:%S %Y").timetuple())
    except ValueError:
        _log(f"process_start_parse_failed pid={pid} raw={raw!r}")
        return None


def _running_app_is_stale(app: Path) -> bool:
    binary = app / "Contents" / "MacOS" / APP_NAME
    if not binary.exists():
        return False
    binary_mtime = binary.stat().st_mtime
    for pid in _app_pids():
        started_at = _process_start_time(pid)
        if started_at is not None and started_at + 1.0 < binary_mtime:
            return True
    return False


def _terminate_running_app() -> None:
    if not _app_running():
        return
    for args in (
        ["osascript", "-e", f'tell application "{APP_NAME}" to quit'],
        ["pkill", "-x", APP_NAME],
    ):
        try:
            r = _run(args, timeout=5)
        except (OSError, subprocess.SubprocessError) as e:
            _log(f"terminate_failed args={args} err={e}")
            continue
        if r.returncode != 0 and _app_running():
            _log(f"terminate_failed args={args} rc={r.returncode} output={r.stdout[-1000:]}")
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if not _app_running():
                return
            time.sleep(0.1)
    if _app_running():
        _log("terminate_timeout")


def _source_fingerprint(src: Path) -> str:
    digest = hashlib.sha256()
    for name in SOURCE_FILES:
        path = src / name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _source_fingerprint_path(app: Path) -> Path:
    return app / "Contents" / "Resources" / SOURCE_FINGERPRINT_FILE


def _write_source_fingerprint(src: Path, app: Path) -> None:
    marker = _source_fingerprint_path(app)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{_source_fingerprint(src)}\n", encoding="utf-8")


def _app_is_current(src: Path, app: Path) -> bool:
    binary = app / "Contents" / "MacOS" / APP_NAME
    plist = app / "Contents" / "Info.plist"
    if not binary.exists() or not plist.exists():
        return False
    marker = _source_fingerprint_path(app)
    if not marker.exists():
        return False
    try:
        return marker.read_text(encoding="utf-8").strip() == _source_fingerprint(src)
    except OSError as e:
        _log(f"source_fingerprint_check_failed err={e}")
        return False


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
        build_src = _prepare_build_source(src)
    except OSError as e:
        _log(f"build_source_prepare_failed err={e}")
        return False
    try:
        r = _run(["/bin/bash", str(build_src / "build.sh")], timeout=120, cwd=build_src)
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
        _write_source_fingerprint(src, app)
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
    if not MENUBAR_AUTO_LAUNCH or SPAWNED_CHILD or WRITE_ORIGIN != "foreground":
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

            app = _installed_app()
            was_current = _app_is_current(src, app)
            was_running = _app_running()
            was_stale = was_running and _running_app_is_stale(app)
            _ensure_status_command()
            if not _install_app(src, app):
                return
            plist = _write_launch_agent(app)
            if plist is None:
                return
            if (not was_current and was_running) or was_stale:
                _terminate_running_app()
            if not _app_running():
                _bootstrap_launch_agent(plist)
                if not _app_running():
                    try:
                        _run(["open", str(app)], timeout=10)
                    except (OSError, subprocess.SubprocessError) as e:
                        _log(f"open_failed err={e}")
    except OSError as e:
        _log(f"autolaunch_failed err={e}")
