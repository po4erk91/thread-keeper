"""Storage profiles and SQLCipher lifecycle helpers.

The default ``sqlite`` profile deliberately stays dependency-free.  The
``sqlcipher`` profile is opt-in, obtains a random database key from the
platform keyring, and never accepts a key through configuration or CLI input.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
from typing import Any


PROFILE_SQLITE = "sqlite"
PROFILE_SQLCIPHER = "sqlcipher"
KEYRING_SERVICE = "threadkeeper.sqlcipher"
_PENDING_SUFFIX = ":rekey-pending"


class StorageError(RuntimeError):
    """Base class for storage-profile failures safe to show to operators."""


class StorageBackendUnavailable(StorageError):
    """SQLCipher support was requested but its optional dependency is absent."""


class StorageKeyUnavailable(StorageError):
    """The operating-system secret store could not provide a database key."""


class StorageOpenError(StorageError):
    """An encrypted database could not be authenticated or opened."""


class RekeyRecoveryRequired(StorageError):
    """A prior rekey stopped before its keyring update completed."""


@dataclass(frozen=True)
class SnapshotResult:
    source: Path
    destination: Path
    integrity: str
    bytes_written: int


@dataclass(frozen=True)
class MigrationResult:
    source: Path
    destination: Path
    source_profile: str
    destination_profile: str
    integrity: str


def normalize_profile(profile: str) -> str:
    """Return one supported profile or reject configuration mistakes early."""
    normalized = (profile or PROFILE_SQLITE).strip().lower()
    aliases = {"plain": PROFILE_SQLITE, "encrypted": PROFILE_SQLCIPHER}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {PROFILE_SQLITE, PROFILE_SQLCIPHER}:
        raise ValueError(
            "THREADKEEPER_STORAGE_PROFILE must be 'sqlite' or 'sqlcipher'"
        )
    return normalized


def encrypted(profile: str) -> bool:
    return normalize_profile(profile) == PROFILE_SQLCIPHER


def row_factory(profile: str) -> Any:
    """Return the DB-API Row type from the active driver."""
    if encrypted(profile):
        return _sqlcipher_driver().Row
    return sqlite3.Row


def _keyring_module() -> Any:
    try:
        import keyring  # type: ignore
    except ImportError as exc:
        raise StorageBackendUnavailable(
            "encrypted storage requires the 'keyring' package; install "
            "threadkeeper[encrypted]"
        ) from exc
    _validate_keyring_backend(keyring)
    return keyring


def _validate_keyring_backend(keyring: Any) -> None:
    getter = getattr(keyring, "get_keyring", None)
    if getter is not None:
        try:
            backend = getter()
            module = type(backend).__module__
        except Exception as exc:
            raise StorageKeyUnavailable(
                "encrypted storage key is unavailable from the OS secret store"
            ) from exc
        supported = (
            "keyring.backends.macOS",
            "keyring.backends.Windows",
            "keyring.backends.SecretService",
            "keyring.backends.libsecret",
            "keyring.backends.kwallet",
        )
        if not module.startswith(supported):
            raise StorageKeyUnavailable(
                "encrypted storage requires a supported OS secret-store backend"
            )


def _sqlcipher_driver() -> Any:
    try:
        from sqlcipher3 import dbapi2 as sqlcipher  # type: ignore
    except ImportError as exc:
        raise StorageBackendUnavailable(
            "encrypted storage requires SQLCipher; install threadkeeper[encrypted]"
        ) from exc
    return sqlcipher


def _path(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser()


def _keyring_account(path: Path, *, pending: bool = False) -> str:
    """Return an opaque, stable keyring account name for a database path."""
    canonical = str(path.resolve(strict=False)).encode("utf-8")
    account = "db-" + hashlib.sha256(canonical).hexdigest()
    return account + (_PENDING_SUFFIX if pending else "")


def _read_key(path: Path, *, pending: bool = False) -> str:
    try:
        key = _keyring_module().get_password(
            KEYRING_SERVICE, _keyring_account(path, pending=pending)
        )
    except Exception as exc:  # keyring backends raise backend-specific errors
        raise StorageKeyUnavailable(
            "encrypted storage key is unavailable from the OS secret store"
        ) from exc
    if not isinstance(key, str) or len(key) != 64:
        raise StorageKeyUnavailable(
            "encrypted storage key is unavailable from the OS secret store"
        )
    try:
        int(key, 16)
    except ValueError as exc:
        raise StorageKeyUnavailable(
            "encrypted storage key is unavailable from the OS secret store"
        ) from exc
    return key.lower()


def _write_key(path: Path, key: str, *, pending: bool = False) -> None:
    try:
        _keyring_module().set_password(
            KEYRING_SERVICE, _keyring_account(path, pending=pending), key
        )
    except Exception as exc:
        raise StorageKeyUnavailable(
            "encrypted storage key cannot be saved to the OS secret store"
        ) from exc


def _delete_key(path: Path, *, pending: bool = False) -> None:
    try:
        _keyring_module().delete_password(
            KEYRING_SERVICE, _keyring_account(path, pending=pending)
        )
    except Exception:
        # A stale pending account is fail-closed at the next open and is
        # recoverable through ``tk-storage recover-rekey``.
        pass


def _has_pending_key(path: Path) -> bool:
    try:
        return bool(
            _keyring_module().get_password(
                KEYRING_SERVICE, _keyring_account(path, pending=True)
            )
        )
    except Exception as exc:
        raise StorageKeyUnavailable(
            "encrypted storage key is unavailable from the OS secret store"
        ) from exc


def _key_pragma(name: str, key: str) -> str:
    # ``key`` is generated or validated as hexadecimal above. SQLCipher does
    # not bind PRAGMA values, so this is intentionally the only interpolation.
    return f"PRAGMA {name} = \"x'{key}'\""


def _configure_cipher(conn: Any, key: str) -> None:
    try:
        conn.execute(_key_pragma("key", key))
        # Keep SQLCipher temporary structures in process memory and scrub
        # sensitive pages there. WAL/journal pages remain SQLCipher-encrypted.
        conn.execute("PRAGMA cipher_memory_security = ON")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA secure_delete = ON")
        version = conn.execute("PRAGMA cipher_version").fetchone()
        if version is None or not version[0]:
            raise StorageOpenError("SQLCipher backend did not report cipher support")
        # This query forces key validation before a caller can create schema or
        # overwrite an existing database with a wrong key.
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except StorageError:
        raise
    except Exception as exc:
        raise StorageOpenError(
            "encrypted storage could not be opened; verify the OS secret-store key"
        ) from exc


def _connect_cipher_with_key(
    path: Path,
    key: str,
    *,
    create: bool,
    timeout: float,
    autocommit: bool,
) -> Any:
    if not create and not path.exists():
        raise StorageOpenError("encrypted storage is not initialized; run tk-storage init")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {"timeout": timeout}
    if autocommit:
        kwargs["isolation_level"] = None
    try:
        conn = _sqlcipher_driver().connect(str(path), **kwargs)
    except StorageError:
        raise
    except Exception as exc:
        raise StorageOpenError("encrypted storage connection could not be opened") from exc
    try:
        _configure_cipher(conn, key)
        return conn
    except Exception:
        conn.close()
        raise


def connect(
    path: str | os.PathLike[str],
    *,
    profile: str,
    create: bool | None = None,
    timeout: float = 10.0,
    autocommit: bool = False,
) -> Any:
    """Open one profile-aware connection.

    A normal application open of the encrypted profile requires an existing
    database. This prevents a missing/wrong-key open from silently creating a
    replacement store. Only explicit lifecycle commands pass ``create=True``.
    """
    db_path = _path(path)
    selected = normalize_profile(profile)
    if selected == PROFILE_SQLITE:
        kwargs: dict[str, Any] = {"timeout": timeout}
        if autocommit:
            kwargs["isolation_level"] = None
        return sqlite3.connect(str(db_path), **kwargs)

    if _has_pending_key(db_path):
        raise RekeyRecoveryRequired(
            "encrypted storage has an interrupted rekey; run tk-storage recover-rekey"
        )
    return _connect_cipher_with_key(
        db_path,
        _read_key(db_path),
        create=bool(create),
        timeout=timeout,
        autocommit=autocommit,
    )


def initialize_encrypted(path: str | os.PathLike[str], *, timeout: float = 30.0) -> None:
    """Create a new SQLCipher database and its keyring entry.

    Existing paths are refused. A key is never emitted, returned, or accepted
    as an argument.
    """
    db_path = _path(path)
    if db_path.exists():
        raise StorageError("encrypted storage already exists; refusing to replace it")
    key = secrets.token_hex(32)
    _write_key(db_path, key)
    try:
        conn = _connect_cipher_with_key(
            db_path, key, create=True, timeout=timeout, autocommit=False
        )
        try:
            _assert_integrity(conn, profile=PROFILE_SQLCIPHER)
            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            db_path.unlink()
        except FileNotFoundError:
            pass
        _delete_key(db_path)
        raise


def _assert_integrity(conn: Any, *, profile: str) -> str:
    if encrypted(profile):
        # SQLCipher reports no rows for a successful cipher_integrity_check;
        # a returned row is an error detail on affected builds.
        cipher_rows = conn.execute("PRAGMA cipher_integrity_check").fetchall()
        if cipher_rows and any(str(row[0]).lower() != "ok" for row in cipher_rows):
            raise StorageOpenError("encrypted storage cipher integrity check failed")
    row = conn.execute("PRAGMA integrity_check").fetchone()
    integrity = "" if row is None else str(row[0])
    if integrity.lower() != "ok":
        raise StorageOpenError(f"storage integrity check failed: {integrity}")
    return integrity


def integrity_check(
    path: str | os.PathLike[str], *, profile: str, timeout: float = 30.0
) -> str:
    conn = connect(path, profile=profile, create=False, timeout=timeout)
    try:
        return _assert_integrity(conn, profile=profile)
    finally:
        conn.close()


def rekey(path: str | os.PathLike[str], *, timeout: float = 30.0) -> None:
    """Rotate one SQLCipher key with an OS-secret-store recovery marker."""
    db_path = _path(path)
    if _has_pending_key(db_path):
        raise RekeyRecoveryRequired(
            "encrypted storage has an interrupted rekey; run tk-storage recover-rekey"
        )
    old_key = _read_key(db_path)
    new_key = secrets.token_hex(32)
    # The pending key makes either crash point recoverable without exposing a
    # secret in a file, process argument, event, or log.
    _write_key(db_path, new_key, pending=True)
    try:
        conn = _connect_cipher_with_key(
            db_path, old_key, create=False, timeout=timeout, autocommit=False
        )
        try:
            conn.execute(_key_pragma("rekey", new_key))
            conn.commit()
        finally:
            conn.close()
        # SQLCipher re-encrypts pages during PRAGMA rekey. Re-open with the
        # candidate key before replacing the primary keyring entry; checking
        # via the old live connection can report stale page HMACs.
        verified = _connect_cipher_with_key(
            db_path, new_key, create=False, timeout=timeout, autocommit=False
        )
        try:
            _assert_integrity(verified, profile=PROFILE_SQLCIPHER)
        finally:
            verified.close()
        _write_key(db_path, new_key)
    except Exception:
        # Leave the pending key in the OS keyring. ``recover_rekey`` probes both
        # candidates and repairs the primary entry without ever printing either.
        raise
    _delete_key(db_path, pending=True)


def recover_rekey(path: str | os.PathLike[str], *, timeout: float = 30.0) -> None:
    """Repair the primary keyring entry after an interrupted rekey."""
    db_path = _path(path)
    candidates: list[tuple[bool, str]] = []
    try:
        candidates.append((False, _read_key(db_path)))
    except StorageKeyUnavailable:
        pass
    try:
        candidates.append((True, _read_key(db_path, pending=True)))
    except StorageKeyUnavailable:
        pass
    for is_pending, candidate in candidates:
        try:
            conn = _connect_cipher_with_key(
                db_path, candidate, create=False, timeout=timeout, autocommit=False
            )
            try:
                _assert_integrity(conn, profile=PROFILE_SQLCIPHER)
            finally:
                conn.close()
        except StorageError:
            continue
        _write_key(db_path, candidate)
        _delete_key(db_path, pending=True)
        return
    raise StorageOpenError("encrypted storage recovery could not validate a key")


def _temporary_path(parent: Path, name: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{name}.tmp-", suffix=".sqlite", dir=parent)
    os.close(fd)
    path = Path(raw)
    path.unlink()
    return path


def _unlink_sidecars(path: Path) -> tuple[Path, ...]:
    removed: list[Path] = []
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        try:
            sidecar.unlink()
        except FileNotFoundError:
            continue
        removed.append(sidecar)
    return tuple(removed)


def _single_file_mode(conn: Any) -> None:
    conn.commit()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass
    conn.execute("PRAGMA journal_mode=DELETE").fetchone()
    conn.commit()


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _coverage(conn: Any) -> tuple[tuple[tuple[str, str, str, str], ...], tuple[tuple[str, int], ...]]:
    objects = tuple(
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger', 'view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    )
    table_names = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_data' "
            "AND name NOT LIKE '%_idx' AND name NOT LIKE '%_content' "
            "AND name NOT LIKE '%_docsize' AND name NOT LIKE '%_config' "
            "ORDER BY name"
        )
    ]
    counts = tuple(
        (name, int(conn.execute(f"SELECT count(*) FROM {_quote_identifier(name)}").fetchone()[0]))
        for name in table_names
    )
    return objects, counts


def _export_between_profiles(
    source: Any,
    destination: Any,
    *,
    source_path: Path,
    destination_path: Path,
    source_profile: str,
    destination_profile: str,
) -> Any:
    """Copy across SQLite/SQLCipher without a plaintext SQL dump on disk."""
    if source_profile == destination_profile:
        source.backup(destination)
        return destination
    if destination_profile == PROFILE_SQLCIPHER:
        # SQLCipher owns the destination connection. Attach the plaintext
        # source read-only through SQLCipher, then export into encrypted main.
        destination.execute("ATTACH DATABASE ? AS source_plain KEY ''", (str(source_path),))
        try:
            destination.execute("SELECT sqlcipher_export('main', 'source_plain')")
        finally:
            destination.execute("DETACH DATABASE source_plain")
        return destination

    # SQLCipher owns the encrypted source and exports directly into the plain
    # destination. Close the standalone sqlite handle first so the attached
    # connection is the only writer to the staging path.
    destination.close()
    source.execute("ATTACH DATABASE ? AS destination_plain KEY ''", (str(destination_path),))
    try:
        source.execute("SELECT sqlcipher_export('destination_plain')")
    finally:
        source.execute("DETACH DATABASE destination_plain")
    return connect(destination_path, profile=PROFILE_SQLITE, create=False)


def copy_database(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    source_profile: str,
    destination_profile: str,
    replace: bool = False,
    timeout: float = 30.0,
    destination_key: str | None = None,
) -> SnapshotResult:
    """Copy a live store without ever materializing an unencrypted staging DB."""
    src = _path(source)
    dst = _path(destination)
    src_profile = normalize_profile(source_profile)
    dst_profile = normalize_profile(destination_profile)
    if not src.exists():
        raise StorageError("source database does not exist")
    if src.resolve(strict=False) == dst.resolve(strict=False):
        raise StorageError("source and destination must be different databases")
    if dst.exists() and not replace:
        raise StorageError("destination exists; pass --replace")

    tmp = _temporary_path(dst.parent, dst.name)
    generated_key: str | None = None
    src_conn = None
    dst_conn = None
    try:
        src_conn = connect(src, profile=src_profile, create=False, timeout=timeout)
        source_coverage = _coverage(src_conn)
        if dst_profile == PROFILE_SQLCIPHER:
            # Replacing an encrypted destination retains its existing key. It
            # avoids changing secret-store state before the atomic file swap
            # and keeps an interrupted --replace recovery-safe.
            key = destination_key
            if key is None and dst.exists():
                key = _read_key(dst)
            if key is None:
                key = secrets.token_hex(32)
                generated_key = key
            dst_conn = _connect_cipher_with_key(
                tmp, key, create=True, timeout=timeout, autocommit=False
            )
        else:
            dst_conn = connect(tmp, profile=PROFILE_SQLITE, create=True, timeout=timeout)
        dst_conn = _export_between_profiles(
            src_conn,
            dst_conn,
            source_path=src,
            destination_path=tmp,
            source_profile=src_profile,
            destination_profile=dst_profile,
        )
        _single_file_mode(dst_conn)
        integrity = _assert_integrity(dst_conn, profile=dst_profile)
        if _coverage(dst_conn) != source_coverage:
            raise StorageError("storage copy did not preserve table and index coverage")
        dst_conn.close()
        dst_conn = None
        src_conn.close()
        src_conn = None
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        if generated_key is not None:
            # Set before the atomic rename: a crash can leave an unused secret,
            # but never an unreadable completed encrypted artifact.
            _write_key(dst, generated_key)
        os.replace(tmp, dst)
        _unlink_sidecars(dst)
        return SnapshotResult(src, dst, integrity, dst.stat().st_size)
    except Exception:
        if generated_key is not None and not dst.exists():
            _delete_key(dst)
        raise
    finally:
        if src_conn is not None:
            src_conn.close()
        if dst_conn is not None:
            dst_conn.close()
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        _unlink_sidecars(tmp)


def restore_database(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    profile: str,
    timeout: float = 30.0,
) -> SnapshotResult:
    """Restore a profile-matched snapshot via an encrypted temporary database."""
    dst = _path(destination)
    selected = normalize_profile(profile)
    existing_key = _read_key(dst) if selected == PROFILE_SQLCIPHER and dst.exists() else None
    result = copy_database(
        source,
        dst,
        source_profile=selected,
        destination_profile=selected,
        replace=True,
        timeout=timeout,
        destination_key=existing_key,
    )
    _unlink_sidecars(dst)
    integrity_check(dst, profile=selected, timeout=timeout)
    return result


def migrate_database(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    source_profile: str,
    destination_profile: str,
    timeout: float = 30.0,
) -> MigrationResult:
    """Create a separately validated profile migration; the source is untouched."""
    result = copy_database(
        source,
        destination,
        source_profile=source_profile,
        destination_profile=destination_profile,
        timeout=timeout,
    )
    return MigrationResult(
        result.source,
        result.destination,
        normalize_profile(source_profile),
        normalize_profile(destination_profile),
        result.integrity,
    )


def _cli_profile(value: str) -> str:
    try:
        return normalize_profile(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _cli_path(value: str) -> Path:
    return Path(value).expanduser()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tk-storage",
        description="Manage opt-in SQLCipher thread-keeper storage without exposing keys.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="initialize a new encrypted database")
    init.add_argument("--db", type=_cli_path, required=True)

    check = sub.add_parser("integrity-check", help="validate an encrypted database")
    check.add_argument("--db", type=_cli_path, required=True)

    rekey_cmd = sub.add_parser("rekey", help="rotate an encrypted database key")
    rekey_cmd.add_argument("--db", type=_cli_path, required=True)
    recover = sub.add_parser("recover-rekey", help="recover an interrupted rekey")
    recover.add_argument("--db", type=_cli_path, required=True)

    backup = sub.add_parser("backup", help="create an encrypted database snapshot")
    backup.add_argument("destination", type=_cli_path)
    backup.add_argument("--db", type=_cli_path, required=True)
    backup.add_argument("--replace", action="store_true")

    restore = sub.add_parser("restore", help="restore an encrypted database snapshot")
    restore.add_argument("source", type=_cli_path)
    restore.add_argument("--db", type=_cli_path, required=True)
    restore.add_argument("--yes", action="store_true")

    migrate = sub.add_parser("migrate", help="copy a store between sqlite and SQLCipher")
    migrate.add_argument("source", type=_cli_path)
    migrate.add_argument("destination", type=_cli_path)
    migrate.add_argument("--from-profile", type=_cli_profile, required=True)
    migrate.add_argument("--to-profile", type=_cli_profile, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            initialize_encrypted(args.db)
            print("ok encrypted storage initialized")
        elif args.command == "integrity-check":
            print(f"ok integrity={integrity_check(args.db, profile=PROFILE_SQLCIPHER)}")
        elif args.command == "rekey":
            rekey(args.db)
            print("ok encrypted storage rekeyed")
        elif args.command == "recover-rekey":
            recover_rekey(args.db)
            print("ok encrypted storage rekey recovery complete")
        elif args.command == "backup":
            result = copy_database(
                args.db, args.destination,
                source_profile=PROFILE_SQLCIPHER,
                destination_profile=PROFILE_SQLCIPHER,
                replace=args.replace,
            )
            print(f"ok backup bytes={result.bytes_written} integrity={result.integrity}")
        elif args.command == "restore":
            if not args.yes:
                raise StorageError("restore is destructive; stop writers and pass --yes")
            result = restore_database(args.source, args.db, profile=PROFILE_SQLCIPHER)
            print(f"ok restored bytes={result.bytes_written} integrity={result.integrity}")
        elif args.command == "migrate":
            result = migrate_database(
                args.source, args.destination,
                source_profile=args.from_profile,
                destination_profile=args.to_profile,
            )
            print(
                "ok migrated "
                f"from={result.source_profile} to={result.destination_profile} "
                f"integrity={result.integrity}"
            )
        return 0
    except StorageError as exc:
        print(f"ERR {exc}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
