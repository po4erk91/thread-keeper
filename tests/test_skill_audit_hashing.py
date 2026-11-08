"""Curator resource hashing stays bounded and never treats failed reads as equal."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tracemalloc

import pytest


def _legacy_tree_hash(root):
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


class _ObservedReader:
    def __init__(self, stream, path, observe):
        self.stream = stream
        self.path = path
        self.observe = observe

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def fileno(self):
        return self.stream.fileno()

    def read(self, *args):
        pytest.fail("Integrity hashing must use bounded reads into its reusable buffer")

    def readinto(self, buffer):
        count = self.stream.readinto(buffer)
        self.observe(self.path, buffer, count)
        return count


def _observe_reads(monkeypatch, observe):
    original = Path.open

    def open_file(path, mode="r", *args, **kwargs):
        stream = original(path, mode, *args, **kwargs)
        return _ObservedReader(stream, path, observe) if mode == "rb" else stream

    monkeypatch.setattr(Path, "open", open_file)


def test_streamed_digest_keeps_legacy_framing_and_order(tmp_path, monkeypatch):
    from threadkeeper import skill_audit as audit

    (tmp_path / "nested").mkdir()
    (tmp_path / "empty-dir").mkdir()
    (tmp_path / "z-empty").touch()
    (tmp_path / "nested" / "résumé.bin").write_bytes(bytes(range(256)) * 8193)
    (tmp_path / "a.txt").write_bytes(b"hello\x00world\r\n")
    expected = _legacy_tree_hash(tmp_path)
    original_walk = os.walk

    def reversed_walk(*args, **kwargs):
        for parent, directories, files in original_walk(*args, **kwargs):
            yield parent, directories, list(reversed(files))

    monkeypatch.setattr(audit.os, "walk", reversed_walk)
    assert audit._tree_hash(tmp_path) == expected
    assert audit._tree_hash(tmp_path / "empty-dir") == hashlib.sha256().hexdigest()
    assert audit._tree_hash(tmp_path / "missing") is None


@pytest.mark.parametrize("size_mib", [1, 32])
def test_large_resources_reuse_one_bounded_buffer(tmp_path, monkeypatch, size_mib):
    from threadkeeper import skill_audit as audit

    for name in ("first.bin", "second.bin"):
        with (tmp_path / name).open("wb") as stream:
            stream.truncate(size_mib * 1024 * 1024 + 7)
    expected = _legacy_tree_hash(tmp_path)
    buffers = set()
    sizes = set()
    counts = {}

    def observe(path, buffer, count):
        assert 0 < len(buffer) <= 64 * 1024
        assert count > 0
        buffers.add(id(buffer.obj))
        sizes.add(len(buffer))
        counts[path.name] = counts.get(path.name, 0) + count

    def reject_whole_file_read(path):
        pytest.fail("Integrity hashing must not use Path.read_bytes")

    monkeypatch.setattr(Path, "read_bytes", reject_whole_file_read)
    _observe_reads(monkeypatch, observe)
    tracemalloc.start()
    try:
        actual = audit._tree_hash(tmp_path)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert actual == expected
    assert len(buffers) == 1
    assert sizes == {64 * 1024, 7}
    assert counts == {name: size_mib * 1024 * 1024 + 7 for name in ("first.bin", "second.bin")}
    assert peak < 256 * 1024


@pytest.mark.parametrize("failure", ["open", "read", "short_read", "stat", "walk"])
def test_read_and_traversal_failures_are_incomplete(tmp_path, monkeypatch, failure):
    from threadkeeper import skill_audit as audit

    directory = tmp_path / "private-subdir"
    directory.mkdir()
    resource = directory / "asset.bin"
    resource.write_bytes(b"private contents")
    if failure == "open":
        original_open = Path.open

        def fail_open(path, *args, **kwargs):
            if path == resource:
                raise PermissionError("/private/read-error")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_open)
    elif failure == "walk":
        original_scandir = os.scandir

        def fail_scan(path):
            if Path(path) == directory:
                raise PermissionError("/private/walk-error")
            return original_scandir(path)

        monkeypatch.setattr(audit.os, "scandir", fail_scan)
    elif failure == "stat":
        def fail_stat(_fd):
            raise OSError("/private/stat-error")

        monkeypatch.setattr(audit.os, "fstat", fail_stat)
    else:
        def fail_read(self, buffer):
            if failure == "short_read":
                return 0
            raise OSError("/private/read-error")

        _observe_reads(monkeypatch, lambda *args: None)
        monkeypatch.setattr(_ObservedReader, "readinto", fail_read)
    assert audit._tree_hash(tmp_path) is None


@pytest.mark.parametrize("change", ["grow", "shrink", "rewrite", "replace", "remove"])
def test_file_changes_during_read_are_incomplete(tmp_path, monkeypatch, change):
    from threadkeeper import skill_audit as audit

    resource = tmp_path / "asset.bin"
    resource.write_bytes(b"x" * (2 * 64 * 1024))
    before = resource.stat()
    changed = False

    def mutate(path, buffer, count):
        nonlocal changed
        if changed:
            return
        changed = True
        if change == "grow":
            with path.open("ab") as stream:
                stream.write(b"extra")
        elif change == "shrink":
            with path.open("r+b") as stream:
                stream.truncate(1)
        elif change == "rewrite":
            with path.open("r+b") as stream:
                stream.write(b"y" * before.st_size)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        elif change == "replace":
            replacement = tmp_path / "replacement"
            replacement.write_bytes(b"x" * before.st_size)
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            replacement.replace(path)
        else:
            path.unlink()

    _observe_reads(monkeypatch, mutate)
    assert audit._tree_hash(tmp_path) is None
    assert changed


@pytest.mark.parametrize("change", ["add", "remove", "rewrite_previous"])
def test_changes_elsewhere_in_tree_are_incomplete(tmp_path, monkeypatch, change):
    from threadkeeper import skill_audit as audit

    previous = tmp_path / "a.bin"
    previous.write_bytes(b"first")
    (tmp_path / "z.bin").write_bytes(b"last")

    def mutate(path, buffer, count):
        if path.name != "z.bin":
            return
        if change == "add":
            (tmp_path / "new-empty-file").touch()
        elif change == "remove":
            previous.unlink()
        else:
            previous.write_bytes(b"FIRST")

    _observe_reads(monkeypatch, mutate)
    assert audit._tree_hash(tmp_path) is None


@pytest.mark.parametrize("failed_roots", [(), ("source",), ("mirror",), ("source", "mirror")])
def test_mirror_status_requires_both_complete_hashes(fresh_mp, monkeypatch, failed_roots):
    from threadkeeper import skill_audit as audit

    primary = fresh_mp["config"].CLAUDE_SKILLS_DIR
    mirror = fresh_mp["tmp"] / "mirror"
    name = "example-skill"
    source = primary / name
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Use when checking integrity.\n---\n\nCheck resources.\n",
    )
    (source / "asset.bin").write_bytes(b"PRIVATE_RESOURCE_CONTENTS")
    shutil.copytree(source, mirror / name)
    original_open = Path.open
    failed_paths = {source if key == "source" else mirror / name for key in failed_roots}

    def open_file(path, mode="r", *args, **kwargs):
        if mode == "rb" and path.name == "asset.bin" and path.parent in failed_paths:
            raise PermissionError("/private/failure-details PRIVATE_RESOURCE_CONTENTS")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    roots = [primary, mirror, fresh_mp["tmp"] / "absent"]
    record = audit._record_for(
        name, {"created_by_origin": "background_review", "state": "active"}, roots, {},
    )
    statuses = [item["status"] for item in record["mirrors"]]
    assert statuses[0] == ("integrity_incomplete" if "source" in failed_roots else "canonical")
    assert statuses[1] == ("integrity_incomplete" if failed_roots else "in_sync")
    assert statuses[2] == "missing"
    assert ("source_integrity_incomplete" in record["findings"]) == ("source" in failed_roots)
    assert ("mirror_integrity_incomplete" in record["findings"]) == bool(failed_roots)
    assert record["findings"] == sorted(set(record["findings"]))
    assert "/private/failure-details" not in json.dumps(record)
    assert "PRIVATE_RESOURCE_CONTENTS" not in json.dumps(record)
