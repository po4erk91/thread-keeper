"""Consistent SQLite backup/restore helpers for the local thread-keeper store."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
from urllib.parse import quote


class BackupError(RuntimeError):
    """Raised when a backup or restore cannot be completed safely."""


@dataclass(frozen=True)
class BackupResult:
    source: Path
    destination: Path
    integrity: str
    bytes_written: int


@dataclass(frozen=True)
class RestoreResult:
    source: Path
    destination: Path
    integrity: str
    removed_sidecars: tuple[Path, ...]


def _default_db_path() -> Path:
    from .config import DB_PATH

    return DB_PATH


def _expand(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser()


def _ro_uri(path: Path) -> str:
    return f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"


def _connect_ro(path: Path, *, timeout: float) -> sqlite3.Connection:
    return sqlite3.connect(_ro_uri(path), uri=True, timeout=timeout)


def _sidecars(db_path: Path) -> tuple[Path, Path]:
    return (Path(f"{db_path}-wal"), Path(f"{db_path}-shm"))


def _single_file_mode(conn: sqlite3.Connection) -> None:
    conn.commit()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass
    conn.execute("PRAGMA journal_mode=DELETE").fetchone()
    conn.commit()


def _integrity_check(path: Path, *, timeout: float = 30.0) -> str:
    conn = _connect_ro(path, timeout=timeout)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    return "" if row is None else str(row[0])


def _unlink_sidecars(db_path: Path) -> tuple[Path, ...]:
    removed: list[Path] = []
    for sidecar in _sidecars(db_path):
        try:
            sidecar.unlink()
        except FileNotFoundError:
            continue
        removed.append(sidecar)
    return tuple(removed)


def _temp_path(parent: Path, final_name: str) -> Path:
    fd, raw = tempfile.mkstemp(
        prefix=f".{final_name}.tmp-",
        suffix=".sqlite",
        dir=parent,
    )
    os.close(fd)
    path = Path(raw)
    path.unlink()
    return path


def create_backup(
    destination: str | os.PathLike[str],
    *,
    source: str | os.PathLike[str] | None = None,
    replace: bool = False,
    timeout: float = 30.0,
) -> BackupResult:
    """Copy a live SQLite database into one integrity-checked file.

    ``VACUUM INTO`` reads a consistent snapshot through a normal SQLite
    connection, so committed frames still living in the source WAL are included
    without asking other thread-keeper writers to stop.
    """
    src = _expand(source) if source is not None else _default_db_path()
    dst = _expand(destination)
    if not src.exists():
        raise BackupError(f"source database does not exist: {src}")
    if dst.exists() and not replace:
        raise BackupError(f"destination exists; pass --replace: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_path(dst.parent, dst.name)
    try:
        src_conn = _connect_ro(src, timeout=timeout)
        try:
            src_conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
            src_conn.execute("VACUUM INTO ?", (str(tmp),))
        finally:
            src_conn.close()
        dst_conn = sqlite3.connect(str(tmp), timeout=timeout)
        try:
            _single_file_mode(dst_conn)
            row = dst_conn.execute("PRAGMA integrity_check").fetchone()
            integrity = "" if row is None else str(row[0])
            if integrity != "ok":
                raise BackupError(f"backup integrity_check failed: {integrity}")
        finally:
            dst_conn.close()

        os.chmod(tmp, 0o600)
        os.replace(tmp, dst)
        _unlink_sidecars(dst)
        return BackupResult(
            source=src,
            destination=dst,
            integrity=integrity,
            bytes_written=dst.stat().st_size,
        )
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        _unlink_sidecars(tmp)
        raise


def restore_backup(
    source: str | os.PathLike[str],
    *,
    destination: str | os.PathLike[str] | None = None,
    timeout: float = 30.0,
) -> RestoreResult:
    """Atomically replace the store with an integrity-checked backup file.

    Callers must stop thread-keeper/CLI processes first. Replacing a SQLite
    database while another process has it open can leave that process writing to
    the old unlinked file.
    """
    src = _expand(source)
    dst = _expand(destination) if destination is not None else _default_db_path()
    if not src.exists():
        raise BackupError(f"backup file does not exist: {src}")
    integrity = _integrity_check(src, timeout=timeout)
    if integrity != "ok":
        raise BackupError(f"backup integrity_check failed: {integrity}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_path(dst.parent, dst.name)
    removed: tuple[Path, ...] = ()
    try:
        shutil.copyfile(src, tmp)
        os.chmod(tmp, 0o600)
        rw = sqlite3.connect(str(tmp), timeout=timeout)
        try:
            _single_file_mode(rw)
            check = rw.execute("PRAGMA integrity_check").fetchone()
            restored_integrity = "" if check is None else str(check[0])
            if restored_integrity != "ok":
                raise BackupError(
                    f"restored copy integrity_check failed: {restored_integrity}"
                )
        finally:
            rw.close()

        removed = _unlink_sidecars(dst)
        os.replace(tmp, dst)
        removed = removed + _unlink_sidecars(dst)
        final_integrity = _integrity_check(dst, timeout=timeout)
        if final_integrity != "ok":
            raise BackupError(
                f"restored database integrity_check failed: {final_integrity}"
            )
        return RestoreResult(
            source=src,
            destination=dst,
            integrity=final_integrity,
            removed_sidecars=removed,
        )
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        _unlink_sidecars(tmp)
        raise


def _positive_float(raw: str) -> float:
    value = float(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tk-backup",
        description="Create or restore consistent thread-keeper SQLite snapshots.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser(
        "create",
        help="write a consistent single-file snapshot from the live SQLite store",
    )
    create.add_argument("destination", help="backup sqlite file to create")
    create.add_argument("--db", dest="db", help="source DB path")
    create.add_argument("--replace", action="store_true", help="replace destination")
    create.add_argument(
        "--timeout",
        type=_positive_float,
        default=30.0,
        help="SQLite busy timeout in seconds (default: 30)",
    )

    restore = sub.add_parser(
        "restore",
        help="replace the store with a previously created backup",
    )
    restore.add_argument("source", help="backup sqlite file to restore")
    restore.add_argument("--db", dest="db", help="destination DB path")
    restore.add_argument(
        "--timeout",
        type=_positive_float,
        default=30.0,
        help="SQLite busy timeout in seconds (default: 30)",
    )
    restore.add_argument(
        "--yes",
        action="store_true",
        help="confirm thread-keeper/CLI processes have been stopped",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    started = time.time()
    try:
        if args.command == "create":
            result = create_backup(
                args.destination,
                source=args.db,
                replace=args.replace,
                timeout=args.timeout,
            )
            elapsed = time.time() - started
            print(
                "ok backup "
                f"source={result.source} dest={result.destination} "
                f"bytes={result.bytes_written} integrity={result.integrity} "
                f"elapsed_s={elapsed:.2f}"
            )
            return 0
        if args.command == "restore":
            if not args.yes:
                parser.error(
                    "restore is destructive; stop thread-keeper/CLI processes "
                    "first, then pass --yes"
                )
            result = restore_backup(
                args.source,
                destination=args.db,
                timeout=args.timeout,
            )
            elapsed = time.time() - started
            removed = ",".join(str(p) for p in result.removed_sidecars) or "none"
            print(
                "ok restored "
                f"source={result.source} dest={result.destination} "
                f"integrity={result.integrity} removed_sidecars={removed} "
                f"elapsed_s={elapsed:.2f}"
            )
            return 0
    except BackupError as exc:
        print(f"ERR {exc}", file=sys.stderr)
        return 1
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
