"""Auto-update daemon for the foreground MCP server.

Once per interval, the daemon checks the installation source and applies the
safe update path:

- editable git checkout: fetch, fast-forward pull, reinstall editable package;
- installed package: pip install --upgrade in the current interpreter env.

Successful updates schedule the current MCP server to exit so the host can
restart it against the newly installed code.
"""
from __future__ import annotations

from contextlib import contextmanager
import importlib.metadata
import importlib.util
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Iterator

from .config import (
    AUTO_UPDATE_INTERVAL_S,
    AUTO_UPDATE_RESTART,
    AUTO_UPDATE_TIMEOUT_S,
    BACKGROUND_DAEMONS_ALLOWED,
    DB_PATH,
)
from .db import get_db
from . import identity

logger = logging.getLogger(__name__)

PACKAGE_NAME = "threadkeeper"
SMOKE_IMPORT_CODE = "import threadkeeper; from threadkeeper import server"
FAILED_UPDATE_MARKERS = (" install=failed", " setup=failed", " smoke=failed")
_started = False
_restart_scheduled = False


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int | float | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _record_auto_update_pass(summary: str) -> None:
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, 'auto_update_pass', '', ?, ?)",
            (identity._session_id or "", summary[:300], int(time.time())),
        )
        conn.commit()
    except Exception:
        logger.debug("auto_update: failed to record pass", exc_info=True)


def _last_auto_update_ts() -> int:
    try:
        row = get_db().execute(
            "SELECT created_at FROM events WHERE kind='auto_update_pass' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    except Exception:
        return 0
    if not row:
        return 0
    try:
        return int(row["created_at"] or 0)
    except (TypeError, ValueError):
        return 0


def _due(now: int | None = None) -> tuple[bool, int]:
    if AUTO_UPDATE_INTERVAL_S <= 0:
        return False, 0
    now = int(now or time.time())
    last = _last_auto_update_ts()
    age = now - last if last else int(AUTO_UPDATE_INTERVAL_S)
    return last == 0 or age >= AUTO_UPDATE_INTERVAL_S, max(0, age)


@contextmanager
def _update_lock() -> Iterator[bool]:
    lock_path = DB_PATH.parent / "auto-update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            yield True
            return
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            try:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


def _package_root() -> Path:
    return Path(__file__).resolve().parent


def _repo_root() -> Path:
    return _package_root().parent


def _is_git_checkout(repo: Path) -> bool:
    return (repo / ".git").exists()


def _semantic_extra_is_installed() -> bool:
    return (
        importlib.util.find_spec("fastembed") is not None
        or importlib.util.find_spec("sentence_transformers") is not None
    )


def _package_spec() -> str:
    return f"{PACKAGE_NAME}[semantic]" if _semantic_extra_is_installed() else PACKAGE_NAME


def _editable_spec(repo: Path) -> str:
    suffix = "[semantic]" if _semantic_extra_is_installed() else ""
    return f"{repo}{suffix}"


def _installed_version() -> str:
    try:
        return importlib.metadata.version(PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _short(text: str, limit: int = 160) -> str:
    return " ".join((text or "").split())[:limit]


def _git_stdout(repo: Path, *args: str, timeout: int = 60) -> tuple[int, str, str]:
    res = _run(["git", "-C", str(repo), *args], timeout=timeout)
    return res.returncode, res.stdout.strip(), res.stderr.strip()


def _git_branch_remote(repo: Path) -> tuple[str, str, str] | str:
    code, branch, err = _git_stdout(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        return f"error mode=git stage=branch err={_short(err)}"
    if branch == "HEAD":
        return "skipped_detached_checkout"

    _, remote, _ = _git_stdout(repo, "config", f"branch.{branch}.remote")
    if not remote:
        remote = "origin"
    _, merge_ref, _ = _git_stdout(repo, "config", f"branch.{branch}.merge")
    remote_branch = (
        merge_ref.removeprefix("refs/heads/")
        if merge_ref.startswith("refs/heads/")
        else branch
    )
    return branch, remote, remote_branch


def _run_setup() -> str:
    setup = _run(
        [sys.executable, "-m", "threadkeeper._setup"],
        timeout=min(120, AUTO_UPDATE_TIMEOUT_S),
    )
    if setup.returncode != 0:
        return f" setup=failed err={_short(setup.stderr)}"
    return " setup=ok"


def _run_post_update_smoke_check() -> str:
    try:
        smoke = _run(
            [sys.executable, "-c", SMOKE_IMPORT_CODE],
            timeout=min(30, AUTO_UPDATE_TIMEOUT_S),
        )
    except Exception as e:  # noqa: BLE001 — smoke failure must not crash daemon
        return f" smoke=failed err={_short(f'{type(e).__name__}: {e}')}"
    if smoke.returncode != 0:
        return f" smoke=failed err={_short(smoke.stderr or smoke.stdout)}"
    return " smoke=ok"


def _updated_result_allows_restart(result: str) -> bool:
    return result.startswith("updated ") and not any(
        marker in result for marker in FAILED_UPDATE_MARKERS
    )


def _suppress_restart(result: str) -> str:
    if " restart=suppressed" in result:
        return result
    return f"{result} restart=suppressed"


def _update_git_checkout(repo: Path) -> str:
    code, dirty, err = _git_stdout(
        repo, "status", "--porcelain", "--untracked-files=no"
    )
    if code != 0:
        return f"error mode=git stage=status err={_short(err)}"
    if dirty:
        return "skipped_dirty_checkout mode=git"

    branch_info = _git_branch_remote(repo)
    if isinstance(branch_info, str):
        return branch_info
    branch, remote, remote_branch = branch_info

    fetch = _run(
        ["git", "-C", str(repo), "fetch", "--quiet", remote, remote_branch],
        timeout=AUTO_UPDATE_TIMEOUT_S,
    )
    if fetch.returncode != 0:
        return f"error mode=git stage=fetch err={_short(fetch.stderr)}"

    remote_ref = f"{remote}/{remote_branch}"
    code, counts, err = _git_stdout(
        repo, "rev-list", "--left-right", "--count", f"HEAD...{remote_ref}"
    )
    if code != 0:
        return f"error mode=git stage=compare err={_short(err)}"
    try:
        ahead, behind = (int(part) for part in counts.split())
    except ValueError:
        return f"error mode=git stage=compare out={_short(counts)}"
    if behind == 0:
        return f"no_update mode=git branch={branch}"
    if ahead > 0:
        return f"skipped_diverged_checkout mode=git branch={branch}"

    _, old_rev, _ = _git_stdout(repo, "rev-parse", "--short", "HEAD")
    pull = _run(
        ["git", "-C", str(repo), "pull", "--quiet", "--ff-only", remote, remote_branch],
        timeout=AUTO_UPDATE_TIMEOUT_S,
    )
    if pull.returncode != 0:
        return f"error mode=git stage=pull err={_short(pull.stderr)}"
    _, new_rev, _ = _git_stdout(repo, "rev-parse", "--short", "HEAD")

    install = _run(
        [sys.executable, "-m", "pip", "install", "--quiet", "-e", _editable_spec(repo)],
        timeout=AUTO_UPDATE_TIMEOUT_S,
    )
    if install.returncode != 0:
        return (
            f"updated mode=git old={old_rev} new={new_rev} "
            f"install=failed err={_short(install.stderr)}"
        )
    return f"updated mode=git old={old_rev} new={new_rev}" + _run_setup()


def _update_installed_package() -> str:
    old_version = _installed_version()
    install = _run(
        [sys.executable, "-m", "pip", "install", "--upgrade", _package_spec()],
        timeout=AUTO_UPDATE_TIMEOUT_S,
    )
    if install.returncode != 0:
        return f"error mode=pip version={old_version} err={_short(install.stderr)}"
    new_version = _installed_version()
    if new_version == old_version:
        return f"no_update mode=pip version={old_version}"
    return f"updated mode=pip old={old_version} new={new_version}" + _run_setup()


def _request_and_apply_update() -> str:
    repo = _repo_root()
    if _is_git_checkout(repo):
        return _update_git_checkout(repo)
    return _update_installed_package()


def _schedule_restart() -> None:
    global _restart_scheduled
    if _restart_scheduled:
        return
    _restart_scheduled = True

    def _exit() -> None:
        logger.info("auto_update: exiting MCP process after successful update")
        os._exit(0)

    timer = threading.Timer(5.0, _exit)
    timer.daemon = True
    timer.start()


def run_auto_update_pass(
    *,
    force: bool = False,
    restart_on_update: bool | None = None,
) -> str:
    """Run one auto-update check/apply pass.

    `force=True` bypasses the interval gate for tests or future manual tools.
    `restart_on_update=False` lets tests verify update decisions without
    terminating the process.
    """
    if AUTO_UPDATE_INTERVAL_S <= 0 and not force:
        return "disabled"
    if not force:
        is_due, age = _due()
        if not is_due:
            return f"not_due age_s={age}"

    should_restart = AUTO_UPDATE_RESTART if restart_on_update is None else restart_on_update
    restart_ready = False
    with _update_lock() as locked:
        if not locked:
            return "update_running"
        try:
            result = _request_and_apply_update()
        except Exception as e:  # noqa: BLE001 — never crash daemon thread
            logger.debug("auto_update: pass failed", exc_info=True)
            result = f"error {type(e).__name__}: {e}"
        if should_restart and result.startswith("updated "):
            if _updated_result_allows_restart(result):
                result += _run_post_update_smoke_check()
                restart_ready = _updated_result_allows_restart(result)
            if not restart_ready:
                result = _suppress_restart(result)

    _record_auto_update_pass(result)
    if should_restart and restart_ready:
        _schedule_restart()
    return result


def _serve_loop() -> None:
    while True:
        try:
            run_auto_update_pass()
        except Exception:
            logger.debug("auto_update daemon tick failed", exc_info=True)
        sleep_s = AUTO_UPDATE_INTERVAL_S if AUTO_UPDATE_INTERVAL_S > 0 else 3600
        time.sleep(min(max(60.0, float(sleep_s)), 3600.0))


def start_auto_update_daemon() -> None:
    """Start the once-per-day auto-update daemon in foreground MCP parents."""
    global _started
    if _started:
        return
    if AUTO_UPDATE_INTERVAL_S <= 0:
        return
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    t = threading.Thread(
        target=_serve_loop, name="auto_update_daemon", daemon=True,
    )
    t.start()
    _started = True
