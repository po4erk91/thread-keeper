"""Secure task-spool directory and file helpers."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO

TASK_SPOOL_DIR_MODE = 0o700
TASK_SPOOL_FILE_MODE = 0o600
TASK_SPOOL_EXEC_MODE = 0o700


def _posix_owner_checks_available() -> bool:
    return os.name == "posix" and hasattr(os, "getuid")


def _nofollow_flag() -> int:
    return int(getattr(os, "O_NOFOLLOW", 0))


def _as_path(path: Path | str) -> Path:
    return Path(path).expanduser()


def _verify_owned_dir(path: Path) -> Path:
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise PermissionError(f"refusing task spool symlink: {path}")
    if not stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError(f"task spool path is not a directory: {path}")
    if _posix_owner_checks_available() and st.st_uid != os.getuid():
        raise PermissionError(
            f"refusing task spool not owned by current user: {path}"
        )
    if os.name == "posix":
        os.chmod(path, TASK_SPOOL_DIR_MODE)
    return path


def ensure_task_spool_dir(path: Path | str) -> Path:
    """Create and verify a task-spool directory as owner-only.

    The final path component is checked with lstat so an attacker-controlled
    symlink is refused instead of followed. Existing directories must be owned
    by the current user on POSIX.
    """
    p = _as_path(path)
    try:
        return _verify_owned_dir(p)
    except FileNotFoundError:
        p.parent.mkdir(parents=True, mode=TASK_SPOOL_DIR_MODE, exist_ok=True)
        try:
            os.mkdir(p, TASK_SPOOL_DIR_MODE)
        except FileExistsError:
            pass
        return _verify_owned_dir(p)


def _verify_private_file_fd(fd: int, path: Path, file_mode: int) -> None:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise PermissionError(f"refusing non-regular task spool file: {path}")
    if _posix_owner_checks_available() and st.st_uid != os.getuid():
        raise PermissionError(
            f"refusing task spool file not owned by current user: {path}"
        )
    if os.name == "posix" and st.st_nlink != 1:
        raise PermissionError(f"refusing linked task spool file: {path}")
    if os.name == "posix":
        os.fchmod(fd, file_mode)


def _open_private_fd(path: Path | str, flags: int, file_mode: int) -> int:
    p = _as_path(path)
    ensure_task_spool_dir(p.parent)
    fd = os.open(p, flags | _nofollow_flag(), file_mode)
    try:
        _verify_private_file_fd(fd, p, file_mode)
    except Exception:
        os.close(fd)
        raise
    return fd


def _open_existing_checked_fd(path: Path | str, flags: int) -> int:
    p = _as_path(path)
    ensure_task_spool_dir(p.parent)
    fd = os.open(p, flags | _nofollow_flag())
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PermissionError(f"refusing non-regular task spool file: {p}")
        if _posix_owner_checks_available() and st.st_uid != os.getuid():
            raise PermissionError(
                f"refusing task spool file not owned by current user: {p}"
            )
        if os.name == "posix" and st.st_nlink != 1:
            raise PermissionError(f"refusing linked task spool file: {p}")
    except Exception:
        os.close(fd)
        raise
    return fd


def write_spool_text(
    path: Path | str,
    text: str,
    *,
    file_mode: int = TASK_SPOOL_FILE_MODE,
    exclusive: bool = False,
    encoding: str = "utf-8",
) -> None:
    flags = os.O_WRONLY | os.O_CREAT
    if exclusive:
        flags |= os.O_EXCL
    fd = _open_private_fd(path, flags, file_mode)
    try:
        if not exclusive:
            os.ftruncate(fd, 0)
        with os.fdopen(fd, "w", encoding=encoding) as fp:
            fp.write(text)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def append_spool_text(
    path: Path | str,
    text: str,
    *,
    file_mode: int = TASK_SPOOL_FILE_MODE,
    encoding: str = "utf-8",
) -> None:
    fd = _open_private_fd(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, file_mode)
    try:
        with os.fdopen(fd, "a", encoding=encoding) as fp:
            fp.write(text)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def touch_spool_file(
    path: Path | str,
    *,
    file_mode: int = TASK_SPOOL_FILE_MODE,
) -> None:
    append_spool_text(path, "", file_mode=file_mode)


def open_new_spool_binary(
    path: Path | str,
    *,
    file_mode: int = TASK_SPOOL_FILE_MODE,
) -> BinaryIO:
    fd = _open_private_fd(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, file_mode)
    try:
        return os.fdopen(fd, "wb")
    except Exception:
        os.close(fd)
        raise


def open_spool_binary_read(path: Path | str) -> BinaryIO:
    fd = _open_existing_checked_fd(path, os.O_RDONLY)
    try:
        return os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise
