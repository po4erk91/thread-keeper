"""SQLCipher profile safety contracts.

The native tests are skipped in the default developer environment because
SQLCipher is an explicit extra. CI installs that extra in its dedicated job.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import threading

import pytest

from threadkeeper import storage


class _MemoryKeyring:
    def __init__(self):
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


@pytest.fixture()
def memory_keyring(monkeypatch):
    keyring = _MemoryKeyring()
    monkeypatch.setattr(storage, "_keyring_module", lambda: keyring)
    return keyring


def test_plain_profile_stays_dependency_free(tmp_path: Path):
    db_path = tmp_path / "default.sqlite"
    conn = storage.connect(db_path, profile="sqlite")
    try:
        conn.execute("CREATE TABLE facts (body TEXT)")
        conn.execute("INSERT INTO facts VALUES ('plain default')")
        conn.commit()
    finally:
        conn.close()
    assert db_path.exists()


def test_encrypted_open_fails_closed_when_key_is_unavailable(
    tmp_path: Path, memory_keyring
):
    db_path = tmp_path / "secure.sqlite"
    with pytest.raises(storage.StorageKeyUnavailable, match="secret store"):
        storage.connect(db_path, profile="sqlcipher", create=False)
    assert not db_path.exists()


def test_encrypted_init_removes_orphaned_key_when_backend_is_unavailable(
    tmp_path: Path, memory_keyring, monkeypatch
):
    db_path = tmp_path / "secure.sqlite"

    def unavailable():
        raise storage.StorageBackendUnavailable("backend unavailable")

    monkeypatch.setattr(storage, "_sqlcipher_driver", unavailable)
    with pytest.raises(storage.StorageBackendUnavailable):
        storage.initialize_encrypted(db_path)

    assert not db_path.exists()
    assert memory_keyring.values == {}


def test_storage_profile_normalization_rejects_unknown_values():
    assert storage.normalize_profile("encrypted") == "sqlcipher"
    assert storage.normalize_profile("plain") == "sqlite"
    with pytest.raises(ValueError, match="STORAGE_PROFILE"):
        storage.normalize_profile("field-crypto")


def test_encrypted_profile_rejects_non_os_keyring_backend():
    class FileBackend:
        pass

    FileBackend.__module__ = "keyrings.alt.file"

    class InsecureKeyring:
        @staticmethod
        def get_keyring():
            return FileBackend()

    # Arbitrary file-based keyrings must not silently become key custody.
    with pytest.raises(storage.StorageKeyUnavailable, match="supported OS"):
        storage._validate_keyring_backend(InsecureKeyring)


@pytest.mark.skipif(
    importlib.util.find_spec("sqlcipher3") is None,
    reason="SQLCipher extra is not installed",
)
def test_sqlcipher_sidecars_backups_rekey_and_migration_hide_canary(
    tmp_path: Path, memory_keyring
):
    canary = "threadkeeper-secret-canary-8caef3"
    encrypted_db = tmp_path / "secure.sqlite"
    storage.initialize_encrypted(encrypted_db)

    conn = storage.connect(encrypted_db, profile="sqlcipher", create=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE facts (body TEXT)")
        conn.execute("INSERT INTO facts VALUES (?)", (canary,))
        conn.commit()
        sidecars = [
            encrypted_db,
            encrypted_db.with_name(encrypted_db.name + "-wal"),
            encrypted_db.with_name(encrypted_db.name + "-shm"),
        ]
        for path in sidecars:
            if path.exists():
                assert canary.encode() not in path.read_bytes()
    finally:
        conn.close()

    backup = tmp_path / "secure-backup.sqlite"
    storage.copy_database(
        encrypted_db,
        backup,
        source_profile="sqlcipher",
        destination_profile="sqlcipher",
    )
    assert canary.encode() not in backup.read_bytes()
    assert storage.integrity_check(backup, profile="sqlcipher") == "ok"

    mutated = storage.connect(encrypted_db, profile="sqlcipher", create=False)
    try:
        mutated.execute("INSERT INTO facts VALUES ('later write')")
        mutated.commit()
    finally:
        mutated.close()
    storage.restore_database(backup, encrypted_db, profile="sqlcipher")
    restored = storage.connect(encrypted_db, profile="sqlcipher", create=False)
    try:
        assert restored.execute("SELECT body FROM facts").fetchall() == [(canary,)]
    finally:
        restored.close()

    account = storage._keyring_account(encrypted_db)
    old_key = memory_keyring.values[(storage.KEYRING_SERVICE, account)]
    storage.rekey(encrypted_db)
    current_key = memory_keyring.values[(storage.KEYRING_SERVICE, account)]
    assert current_key != old_key
    assert storage.integrity_check(encrypted_db, profile="sqlcipher") == "ok"

    # A keyring key swap must fail before any caller can create replacement
    # schema. The original encrypted bytes remain exactly as they were.
    before = encrypted_db.read_bytes()
    memory_keyring.values[(storage.KEYRING_SERVICE, account)] = "0" * 64
    with pytest.raises(storage.StorageOpenError):
        storage.connect(encrypted_db, profile="sqlcipher", create=False)
    assert encrypted_db.read_bytes() == before
    memory_keyring.values[(storage.KEYRING_SERVICE, account)] = current_key
    assert storage.integrity_check(encrypted_db, profile="sqlcipher") == "ok"

    plain = tmp_path / "plain.sqlite"
    plain_conn = storage.connect(plain, profile="sqlite")
    try:
        plain_conn.execute("CREATE TABLE migration_facts (body TEXT)")
        plain_conn.execute("INSERT INTO migration_facts VALUES (?)", (canary,))
        plain_conn.commit()
    finally:
        plain_conn.close()
    migrated = tmp_path / "migrated.sqlite"
    storage.migrate_database(
        plain,
        migrated,
        source_profile="sqlite",
        destination_profile="sqlcipher",
    )
    assert canary.encode() not in migrated.read_bytes()
    roundtrip = tmp_path / "roundtrip.sqlite"
    storage.migrate_database(
        migrated,
        roundtrip,
        source_profile="sqlcipher",
        destination_profile="sqlite",
    )
    check = storage.connect(roundtrip, profile="sqlite")
    try:
        assert check.execute("SELECT body FROM migration_facts").fetchone()[0] == canary
    finally:
        check.close()


@pytest.mark.skipif(
    importlib.util.find_spec("sqlcipher3") is None,
    reason="SQLCipher extra is not installed",
)
def test_interrupted_rekey_requires_explicit_recovery(tmp_path: Path, memory_keyring):
    db_path = tmp_path / "secure.sqlite"
    storage.initialize_encrypted(db_path)
    pending = "a" * 64
    memory_keyring.set_password(
        storage.KEYRING_SERVICE, storage._keyring_account(db_path, pending=True), pending
    )
    with pytest.raises(storage.RekeyRecoveryRequired):
        storage.connect(db_path, profile="sqlcipher", create=False)
    storage.recover_rekey(db_path)
    assert storage.integrity_check(db_path, profile="sqlcipher") == "ok"


@pytest.mark.skipif(
    importlib.util.find_spec("sqlcipher3") is None,
    reason="SQLCipher extra is not installed",
)
def test_sqlcipher_db_factory_serializes_concurrent_writers(
    tmp_path: Path, memory_keyring, monkeypatch
):
    from threadkeeper import db, storage as live_storage

    db_path = tmp_path / "shared.sqlite"
    # Other tests intentionally re-import threadkeeper for isolation. Use the
    # currently imported storage module, not this test module's stale import.
    monkeypatch.setattr(live_storage, "_keyring_module", lambda: memory_keyring)
    live_storage.initialize_encrypted(db_path)
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(db, "STORAGE_PROFILE", "sqlcipher")
    monkeypatch.setattr(db, "_BOOTSTRAPPED", False)
    db.bootstrap_db()

    errors: list[BaseException] = []

    def write(worker: int) -> None:
        try:
            for index in range(10):
                db.run_write(
                    "cipher-concurrency",
                    lambda conn, key=f"{worker}:{index}": conn.execute(
                        "INSERT INTO style(key, value, updated_at) VALUES (?, 'ok', 1)",
                        (key,),
                    ),
                    deadline_s=5,
                )
        except BaseException as exc:  # noqa: BLE001 - return worker failure
            errors.append(exc)

    workers = [threading.Thread(target=write, args=(worker,)) for worker in range(3)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert errors == []
    with db.read_db() as conn:
        assert conn.execute("SELECT count(*) FROM style").fetchone()[0] == 30
