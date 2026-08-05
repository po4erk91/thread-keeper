"""Safe read-modify-write helpers for user-owned CLI configuration files.

CLI configuration is shared with the host application, so a failed setup must
never truncate it.  These helpers write a fully synced sibling temporary file
and atomically replace the destination only after that succeeds.  A small
advisory lock also keeps concurrent setup processes from applying mutations to
stale snapshots of the same file.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from typing import Callable, Iterator, TypeVar


T = TypeVar("T")


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Durably replace ``path`` without ever truncating its current contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fp:
            fp.write(content)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, value: dict, *, indent: int = 2) -> None:
    """Atomically serialize a JSON config file."""
    atomic_write_text(path, json.dumps(value, indent=indent))


@contextmanager
def locked_config_file(path: Path) -> Iterator[None]:
    """Hold a POSIX advisory lock for one configuration mutation.

    The lock file is separate from the config itself so replacing the config
    inode does not release the lock mid-mutation.  Platforms without
    ``fcntl`` retain atomic replacement but skip this best-effort race guard.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+") as lock:
        try:
            import fcntl
        except ImportError:
            yield
            return
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def mutate_json_file(
    path: Path,
    mutate: Callable[[dict], tuple[bool, T]],
    *,
    allow_empty: bool = False,
) -> T:
    """Lock, read, mutate, and atomically replace a JSON config when changed."""
    with locked_config_file(path):
        if path.exists():
            body = path.read_text()
            config = {} if allow_empty and not body.strip() else json.loads(body)
        else:
            config = {}
        changed, result = mutate(config)
        if changed:
            atomic_write_json(path, config)
        return result


def mutate_text_file(
    path: Path,
    mutate: Callable[[str, bool], tuple[str, T]],
) -> T:
    """Lock, transform, and atomically replace arbitrary text config content."""
    with locked_config_file(path):
        exists = path.exists()
        body = path.read_text() if exists else ""
        new_body, result = mutate(body, exists)
        if new_body != body:
            atomic_write_text(path, new_body)
        return result
