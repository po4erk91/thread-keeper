"""The lesson index lists every lesson title and follows every lesson write."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _lessons(tmp_path, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_LESSONS", str(tmp_path / "lessons.md"))
    monkeypatch.setenv("THREADKEEPER_DB", str(tmp_path / "db.sqlite"))
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    from threadkeeper import lessons

    return lessons


def test_index_lists_every_title_sorted_after_appends(tmp_path, monkeypatch):
    lessons = _lessons(tmp_path, monkeypatch)

    lessons.append_lesson(title="zebra crossing rule", body="Stop.")
    lessons.append_lesson(title="apple before banana", body="Order.")

    index = (tmp_path / "lesson-index.md").read_text()
    assert index.startswith("# thread-keeper lessons (2)")
    assert index.endswith("apple-before-banana\nzebra-crossing-rule\n")


def test_index_drops_removed_lessons(tmp_path, monkeypatch):
    lessons = _lessons(tmp_path, monkeypatch)
    lessons.append_lesson(title="keep me", body="Keep.")
    lessons.append_lesson(title="drop me", body="Drop.")

    assert lessons.remove_lesson("drop-me")

    index = (tmp_path / "lesson-index.md").read_text()
    assert "keep-me" in index
    assert "drop-me" not in index


def test_refresh_skips_an_index_newer_than_the_lessons(tmp_path, monkeypatch):
    lessons = _lessons(tmp_path, monkeypatch)
    lessons.append_lesson(title="only rule", body="Body.")
    index = tmp_path / "lesson-index.md"
    index.write_text("hand edited\n")
    later = (tmp_path / "lessons.md").stat().st_mtime + 10
    os.utime(index, (later, later))

    assert lessons.refresh_lesson_index() is False
    assert index.read_text() == "hand edited\n"


def test_refresh_without_lessons_writes_nothing(tmp_path, monkeypatch):
    lessons = _lessons(tmp_path, monkeypatch)

    assert lessons.refresh_lesson_index() is False
    assert not (tmp_path / "lesson-index.md").exists()


def test_managed_instructions_import_the_index():
    from threadkeeper.instructions import MANAGED_INSTRUCTIONS_BLOCK

    assert "@~/.threadkeeper/lesson-index.md" in MANAGED_INSTRUCTIONS_BLOCK


def test_setup_writes_the_index_and_reports_it(tmp_path, monkeypatch):
    lessons = _lessons(tmp_path, monkeypatch)
    lessons.append_lesson(title="first rule", body="Body.")
    (tmp_path / "lesson-index.md").unlink()
    from threadkeeper import _setup

    assert _setup.install_lesson_index(dry_run=True) == "lesson index: would refresh"
    assert not (tmp_path / "lesson-index.md").exists()
    assert _setup.install_lesson_index(dry_run=False) == "lesson index: written"
    assert _setup.install_lesson_index(dry_run=False) == "lesson index: current"
