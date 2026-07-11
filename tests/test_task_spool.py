from __future__ import annotations

import os

import pytest

from threadkeeper import task_spool


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership/mode test")
def test_ensure_task_spool_dir_creates_owner_only(tmp_path):
    path = tmp_path / "tasks"

    task_spool.ensure_task_spool_dir(path)

    assert path.is_dir()
    assert path.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink test")
def test_ensure_task_spool_dir_refuses_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "tasks"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(PermissionError, match="symlink"):
        task_spool.ensure_task_spool_dir(link)


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership test")
def test_ensure_task_spool_dir_refuses_foreign_owned(monkeypatch, tmp_path):
    path = tmp_path / "tasks"
    path.mkdir()
    owner_uid = path.stat().st_uid
    monkeypatch.setattr(task_spool.os, "getuid", lambda: owner_uid + 1)

    with pytest.raises(PermissionError, match="not owned"):
        task_spool.ensure_task_spool_dir(path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-follow test")
def test_write_spool_text_refuses_planted_symlink(tmp_path):
    spool = task_spool.ensure_task_spool_dir(tmp_path / "tasks")
    target = tmp_path / "target.txt"
    target.write_text("keep\n", encoding="utf-8")
    planted = spool / "dialog-tail.command"
    planted.symlink_to(target)

    with pytest.raises(OSError):
        task_spool.write_spool_text(planted, "secret\n")

    assert target.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership test")
def test_write_spool_text_validates_before_truncate(monkeypatch, tmp_path):
    spool = task_spool.ensure_task_spool_dir(tmp_path / "tasks")
    path = spool / "dialog-tail.command"
    path.write_text("keep\n", encoding="utf-8")
    owner_uid = path.stat().st_uid
    monkeypatch.setattr(task_spool.os, "getuid", lambda: owner_uid + 1)

    with pytest.raises(PermissionError, match="not owned"):
        task_spool.write_spool_text(path, "secret\n")

    assert path.read_text(encoding="utf-8") == "keep\n"
