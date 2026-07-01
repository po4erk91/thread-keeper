"""Best-effort POSIX permission hardening for local thread-keeper state."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def _posix_perms_available() -> bool:
    return os.name == "posix"


def _chmod_existing(path: Path, mode: int) -> None:
    """Best-effort chmod that never prevents startup."""
    if not _posix_perms_available():
        return
    try:
        if path.exists() and not path.is_symlink():
            os.chmod(path, mode)
    except OSError as e:
        logger.debug("permission hardening skipped for %s: %s", path, e)


def chmod_private_dir(path: Path | str) -> None:
    _chmod_existing(Path(path).expanduser(), PRIVATE_DIR_MODE)


def chmod_private_file(path: Path | str) -> None:
    _chmod_existing(Path(path).expanduser(), PRIVATE_FILE_MODE)


def ensure_private_file(path: Path | str) -> None:
    """Create a missing file as 0600, then chmod existing regular files."""
    p = Path(path).expanduser()
    if _posix_perms_available() and not p.exists():
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE_MODE)
        except FileExistsError:
            pass
        except OSError as e:
            logger.debug("private file pre-create skipped for %s: %s", p, e)
        else:
            try:
                os.close(fd)
            except OSError as e:
                logger.debug("private file close skipped for %s: %s", p, e)
    chmod_private_file(p)


def open_private_binary_write(path: Path | str) -> BinaryIO:
    """Open a file for binary write with owner-only permissions on POSIX."""
    p = Path(path).expanduser()
    if not _posix_perms_available():
        return p.open("wb")
    fd = os.open(p, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, PRIVATE_FILE_MODE)
    try:
        os.chmod(p, PRIVATE_FILE_MODE)
        return os.fdopen(fd, "wb")
    except Exception:
        os.close(fd)
        raise


def _is_default_data_dir(path: Path) -> bool:
    default_dir = Path("~/.threadkeeper").expanduser()
    try:
        return (
            path.expanduser().resolve(strict=False)
            == default_dir.resolve(strict=False)
        )
    except OSError:
        return path.expanduser() == default_dir


def harden_storage_paths(
    db_path: Path | str,
    *,
    env_file: Path | str | None = None,
    curator_reports_dir: Path | str | None = None,
    create_db: bool = False,
) -> None:
    """Tighten local memory files without crashing on unsupported platforms."""
    db = Path(db_path).expanduser()
    if _is_default_data_dir(db.parent):
        chmod_private_dir(db.parent)

    if create_db:
        ensure_private_file(db)
    else:
        chmod_private_file(db)

    for suffix in ("-wal", "-shm"):
        chmod_private_file(db.with_name(db.name + suffix))

    if env_file is not None:
        chmod_private_file(env_file)

    if curator_reports_dir is not None:
        reports_dir = Path(curator_reports_dir).expanduser()
        try:
            reports = (
                list(reports_dir.glob("REPORT-*.md"))
                if reports_dir.exists()
                else []
            )
        except OSError as e:
            logger.debug("curator report hardening skipped for %s: %s", reports_dir, e)
            reports = []
        for report in reports:
            chmod_private_file(report)
