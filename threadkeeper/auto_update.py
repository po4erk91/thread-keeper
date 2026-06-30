"""Auto-update daemon for the foreground MCP server.

Once per interval, the daemon checks the installation source and applies the
safe update path:

- editable git checkout: fetch, fast-forward pull, reinstall editable package;
- installed package: pip install --upgrade in the current interpreter env.

Successful updates schedule the current MCP server to exit so the host can
restart it against the newly installed code.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import importlib.metadata
import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import (
    AUTO_UPDATE_EXPECTED_PUBLISHER_ENVIRONMENT,
    AUTO_UPDATE_EXPECTED_PUBLISHER_REPOSITORY,
    AUTO_UPDATE_EXPECTED_PUBLISHER_WORKFLOW,
    AUTO_UPDATE_INTERVAL_S,
    AUTO_UPDATE_PYPI_BASE_URL,
    AUTO_UPDATE_RESTART,
    AUTO_UPDATE_TIMEOUT_S,
    AUTO_UPDATE_VERIFY_PROVENANCE,
    BACKGROUND_DAEMONS_ALLOWED,
    DB_PATH,
)
from .db import get_db
from . import identity

logger = logging.getLogger(__name__)

PACKAGE_NAME = "threadkeeper"
SMOKE_IMPORT_CODE = "import threadkeeper; from threadkeeper import server"
FAILED_UPDATE_MARKERS = (" install=failed", " setup=failed", " smoke=failed")
PYPI_JSON_ACCEPT = "application/json"
PYPI_INTEGRITY_ACCEPT = "application/vnd.pypi.integrity.v1+json"
ATTESTATION_PREDICATE_TYPES = {
    "https://docs.pypi.org/attestations/publish/v1",
    "https://slsa.dev/provenance/v1",
}
_started = False
_restart_scheduled = False


@dataclass(frozen=True)
class _ProvenanceDecision:
    allowed: bool
    version: str = ""
    reason: str = ""


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


def _pypi_url(path: str) -> str:
    return f"{AUTO_UPDATE_PYPI_BASE_URL.rstrip('/')}/{path.lstrip('/')}"


def _fetch_json(url: str, *, accept: str = PYPI_JSON_ACCEPT) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "threadkeeper-auto-update",
        },
    )
    timeout = min(30, AUTO_UPDATE_TIMEOUT_S)
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed PyPI URL
        payload = response.read()
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return data


def _pypi_project_metadata() -> dict[str, Any]:
    return _fetch_json(_pypi_url(f"pypi/{quote(PACKAGE_NAME)}/json"))


def _release_files(metadata: dict[str, Any], version: str) -> list[dict[str, Any]]:
    releases = metadata.get("releases")
    files = (
        releases.get(version)
        if isinstance(releases, dict)
        else None
    )
    if not files:
        files = metadata.get("urls", [])
    if not isinstance(files, list):
        return []
    return [
        f for f in files
        if isinstance(f, dict) and not bool(f.get("yanked"))
    ]


def _fetch_pypi_provenance(version: str, filename: str) -> dict[str, Any]:
    project = quote(PACKAGE_NAME)
    ver = quote(version, safe="")
    name = quote(filename, safe="")
    return _fetch_json(
        _pypi_url(f"integrity/{project}/{ver}/{name}/provenance"),
        accept=PYPI_INTEGRITY_ACCEPT,
    )


def _publisher_matches_expected(publisher: Any) -> bool:
    if not isinstance(publisher, dict):
        return False
    if publisher.get("kind") != "GitHub":
        return False
    expected_repo = AUTO_UPDATE_EXPECTED_PUBLISHER_REPOSITORY.strip().lower()
    actual_repo = str(publisher.get("repository") or "").strip().lower()
    if not expected_repo or actual_repo != expected_repo:
        return False
    expected_workflow = AUTO_UPDATE_EXPECTED_PUBLISHER_WORKFLOW.strip()
    if expected_workflow and publisher.get("workflow") != expected_workflow:
        return False
    expected_environment = AUTO_UPDATE_EXPECTED_PUBLISHER_ENVIRONMENT.strip()
    if expected_environment and publisher.get("environment") != expected_environment:
        return False
    return True


def _decode_attestation_statement(attestation: dict[str, Any]) -> dict[str, Any]:
    envelope = attestation.get("envelope")
    if not isinstance(envelope, dict):
        raise ValueError("missing envelope")
    raw_statement = envelope.get("statement")
    if not isinstance(raw_statement, str) or not raw_statement:
        raise ValueError("missing statement")
    decoded = base64.b64decode(raw_statement, validate=True)
    statement = json.loads(decoded.decode("utf-8"))
    if not isinstance(statement, dict):
        raise ValueError("statement_not_object")
    return statement


def _attestation_matches_file(
    attestation: dict[str, Any],
    *,
    filename: str,
    sha256: str,
) -> bool:
    try:
        statement = _decode_attestation_statement(attestation)
    except Exception:
        return False
    if statement.get("predicateType") not in ATTESTATION_PREDICATE_TYPES:
        return False
    subject = statement.get("subject")
    if not isinstance(subject, list) or len(subject) != 1:
        return False
    item = subject[0]
    if not isinstance(item, dict) or item.get("name") != filename:
        return False
    digest = item.get("digest")
    if not isinstance(digest, dict):
        return False
    return str(digest.get("sha256") or "").lower() == sha256.lower()


def _provenance_matches_file(
    provenance: dict[str, Any],
    *,
    filename: str,
    sha256: str,
) -> tuple[bool, str]:
    if provenance.get("version") != 1:
        return False, "provenance_version"
    bundles = provenance.get("attestation_bundles")
    if not isinstance(bundles, list) or not bundles:
        return False, "provenance_empty"

    saw_expected_publisher = False
    for bundle in bundles:
        if not isinstance(bundle, dict):
            continue
        if not _publisher_matches_expected(bundle.get("publisher")):
            continue
        saw_expected_publisher = True
        attestations = bundle.get("attestations")
        if not isinstance(attestations, list):
            continue
        for attestation in attestations:
            if isinstance(attestation, dict) and _attestation_matches_file(
                attestation,
                filename=filename,
                sha256=sha256,
            ):
                return True, "ok"

    if saw_expected_publisher:
        return False, "attestation_mismatch"
    return False, "publisher_mismatch"


def _verify_pypi_release_provenance(old_version: str) -> _ProvenanceDecision:
    try:
        metadata = _pypi_project_metadata()
    except (HTTPError, URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as e:
        return _ProvenanceDecision(
            False,
            old_version,
            f"metadata_unavailable err={_short(str(e))}",
        )

    info = metadata.get("info")
    version = str((info or {}).get("version") or "").strip() if isinstance(info, dict) else ""
    if not version:
        return _ProvenanceDecision(False, old_version, "metadata_missing_version")
    if old_version != "unknown" and version == old_version:
        return _ProvenanceDecision(True, version, "already_current")

    files = _release_files(metadata, version)
    if not files:
        return _ProvenanceDecision(False, version, "release_files_missing")

    for file_info in files:
        filename = str(file_info.get("filename") or "").strip()
        digests = file_info.get("digests")
        sha256 = (
            str((digests or {}).get("sha256") or "").strip().lower()
            if isinstance(digests, dict)
            else ""
        )
        if not filename:
            return _ProvenanceDecision(False, version, "release_file_missing_name")
        if not sha256:
            return _ProvenanceDecision(
                False,
                version,
                f"release_file_missing_sha256 file={filename}",
            )

        try:
            provenance = _fetch_pypi_provenance(version, filename)
        except HTTPError as e:
            if e.code == 404:
                reason = "provenance_missing"
            else:
                reason = f"provenance_http_{e.code}"
            return _ProvenanceDecision(False, version, f"{reason} file={filename}")
        except (URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as e:
            return _ProvenanceDecision(
                False,
                version,
                f"provenance_unavailable file={filename} err={_short(str(e))}",
            )

        ok, reason = _provenance_matches_file(
            provenance,
            filename=filename,
            sha256=sha256,
        )
        if not ok:
            return _ProvenanceDecision(False, version, f"{reason} file={filename}")

    return _ProvenanceDecision(True, version, "verified")


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
    if AUTO_UPDATE_VERIFY_PROVENANCE:
        provenance = _verify_pypi_release_provenance(old_version)
        if not provenance.allowed:
            version = provenance.version or old_version
            return (
                f"refused mode=pip version={version} "
                f"reason={_short(provenance.reason)}"
            )
        if provenance.version and provenance.version == old_version:
            return f"no_update mode=pip version={old_version}"

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
