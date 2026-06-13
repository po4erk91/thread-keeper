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


def test_menubar_packaged_assets_match_dev_source():
    repo = Path(__file__).resolve().parents[1]
    dev_src = repo / "apps" / "macos-agent-status"
    package_src = repo / "threadkeeper" / "assets" / "macos-agent-status"

    for name in (
        "ThreadKeeperAgentStatus.swift",
        "Info.plist",
        "README.md",
        "build.sh",
        "install.sh",
    ):
        assert (package_src / name).read_text(encoding="utf-8") == (
            dev_src / name
        ).read_text(encoding="utf-8")


def test_menubar_status_item_uses_idle_chip_and_running_gears():
    repo = Path(__file__).resolve().parents[1]
    swift = (
        repo / "apps" / "macos-agent-status" / "ThreadKeeperAgentStatus.swift"
    ).read_text(encoding="utf-8")

    assert "NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)" in swift
    assert "NSPopover()" in swift
    assert "NSHostingController(" in swift
    assert "MenuBarExtra" not in swift
    assert "button.imagePosition = .imageOnly" in swift
    assert 'button.title = ""' in swift
    assert 'button.title = " TK' not in swift
    assert 'return "TK ' not in swift
    assert "Timer(timeInterval: gearSpinInterval" in swift
    assert "gearFrameStepDegrees = 17.0" in swift
    assert "largeGearDiameter: CGFloat = 12.0" in swift
    assert "smallGearDiameter: CGFloat = 9.0" in swift
    assert "-angle * largeGearDiameter / smallGearDiameter" in swift
    assert "drawGearSymbol(" in swift
    assert "by: 45.0" not in swift
    assert 'makeTemplateSymbolImage("memorychip")' in swift
    assert 'NSImage(systemSymbolName: "gearshape.fill"' in swift
    assert "store.snapshot.runningLoopCount > 0" in swift
    assert "store.snapshot.runningCount > 0" not in swift
    assert "button.image = gearFrames" in swift
    assert "TimelineView" not in swift
    assert 'THREADKEEPER_MENUBAR_RESTART_RSS_MB' in swift
    assert 'runStatusCommand(arguments: ["--cleanup-memory"])' in swift
    assert '.help("Clean memory")' in swift


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


def test_running_app_is_stale_when_process_started_before_binary(tmp_path, monkeypatch):
    import threadkeeper.menubar_app as menubar_app

    app = tmp_path / menubar_app.APP_BUNDLE
    binary = app / "Contents" / "MacOS" / menubar_app.APP_NAME
    binary.parent.mkdir(parents=True)
    binary.write_text("binary\n", encoding="utf-8")
    binary.touch()

    monkeypatch.setattr(menubar_app, "_app_pids", lambda: [123])
    monkeypatch.setattr(
        menubar_app,
        "_process_start_time",
        lambda pid: binary.stat().st_mtime - 10.0,
    )

    assert menubar_app._running_app_is_stale(app) is True


def test_ensure_menubar_restarts_stale_running_app(fresh_mp, tmp_path, monkeypatch):
    import threadkeeper.menubar_app as menubar_app

    src = tmp_path / "source"
    src.mkdir()
    app = tmp_path / "Applications" / menubar_app.APP_BUNDLE
    plist = tmp_path / "agent.plist"
    task_logs = tmp_path / "tasks"
    calls = []
    running = {"value": True}

    monkeypatch.setattr(menubar_app, "_attempted", False)
    monkeypatch.setattr(menubar_app.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(menubar_app, "MENUBAR_AUTO_LAUNCH", True)
    monkeypatch.setattr(menubar_app, "BACKGROUND_DAEMONS_ALLOWED", True)
    monkeypatch.setattr(menubar_app, "TASK_LOG_DIR", task_logs)
    monkeypatch.setattr(menubar_app, "_source_dir", lambda: src)
    monkeypatch.setattr(menubar_app, "_installed_app", lambda: app)
    monkeypatch.setattr(menubar_app, "_app_is_current", lambda source, installed: True)
    monkeypatch.setattr(menubar_app, "_running_app_is_stale", lambda installed: True)
    monkeypatch.setattr(menubar_app, "_ensure_status_command", lambda: None)
    monkeypatch.setattr(menubar_app, "_install_app", lambda source, installed: True)
    monkeypatch.setattr(menubar_app, "_write_launch_agent", lambda installed: plist)
    monkeypatch.setattr(menubar_app, "_bootstrap_launch_agent", lambda path: calls.append(("bootstrap", path)))

    def fake_app_running():
        return running["value"]

    def fake_terminate():
        calls.append(("terminate",))
        running["value"] = False

    def fake_run(args, timeout=60, cwd=None):
        calls.append(tuple(args))
        if args[:1] == ["open"]:
            running["value"] = True
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(menubar_app, "_app_running", fake_app_running)
    monkeypatch.setattr(menubar_app, "_terminate_running_app", fake_terminate)
    monkeypatch.setattr(menubar_app, "_run", fake_run)

    menubar_app.ensure_menubar_app()

    assert ("terminate",) in calls
    assert ("bootstrap", plist) in calls
    assert ("open", str(app)) in calls
