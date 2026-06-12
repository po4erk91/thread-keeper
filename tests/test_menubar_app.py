from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def test_menubar_packaged_assets_are_available(fresh_mp):
    import threadkeeper.menubar_app as menubar_app

    src = menubar_app._package_source_dir()

    assert src.name == "macos-agent-status"
    assert (src / "ThreadKeeperAgentStatus.swift").exists()
    assert (src / "Info.plist").exists()
    assert (src / "build.sh").exists()


def test_menubar_source_falls_back_to_packaged_assets(fresh_mp, tmp_path, monkeypatch):
    import threadkeeper.menubar_app as menubar_app

    monkeypatch.setattr(menubar_app, "_dev_source_dir", lambda: tmp_path / "missing")

    assert menubar_app._source_dir() == menubar_app._package_source_dir()


def test_install_app_builds_from_task_log_scratch_without_executable_bit(
    fresh_mp,
    tmp_path,
    monkeypatch,
):
    import threadkeeper.menubar_app as menubar_app

    src = tmp_path / "source"
    src.mkdir()
    (src / "ThreadKeeperAgentStatus.swift").write_text("// swift\n", encoding="utf-8")
    (src / "Info.plist").write_text("<plist></plist>\n", encoding="utf-8")
    build = src / "build.sh"
    build.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    build.chmod(0o644)

    task_logs = tmp_path / "tasks"
    monkeypatch.setattr(menubar_app, "TASK_LOG_DIR", task_logs)
    monkeypatch.setattr(menubar_app, "_app_is_current", lambda src, app: False)
    calls = []

    def fake_run(args, timeout=60, cwd=None):
        calls.append((args, timeout, Path(cwd)))
        app_dir = Path(cwd) / "build" / menubar_app.APP_BUNDLE
        bin_dir = app_dir / "Contents" / "MacOS"
        bin_dir.mkdir(parents=True)
        (app_dir / "Contents" / "Info.plist").write_text(
            "<plist></plist>\n",
            encoding="utf-8",
        )
        (bin_dir / menubar_app.APP_NAME).write_text("binary\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=f"{app_dir}\n")

    monkeypatch.setattr(menubar_app, "_run", fake_run)

    installed = tmp_path / "Applications" / menubar_app.APP_BUNDLE
    assert menubar_app._install_app(src, installed) is True

    assert calls
    assert calls[0][0][0] == "/bin/bash"
    assert calls[0][0][1] == str(task_logs / "menubar-build" / "source" / "build.sh")
    assert calls[0][2] == task_logs / "menubar-build" / "source"
    assert (installed / "Contents" / "Info.plist").exists()
    assert (installed / "Contents" / "MacOS" / menubar_app.APP_NAME).exists()
    assert not (src / "build").exists()
