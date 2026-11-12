from __future__ import annotations
import sys, importlib


def test_cold_host_resolves_curator_tools_before_starting_loops(monkeypatch, tmp_path):
    monkeypatch.setenv("THREADKEEPER_DB", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("THREADKEEPER_DISABLE_BG_DAEMONS", "1")
    monkeypatch.setenv("THREADKEEPER_NO_EMBEDDINGS", "1")
    for name in [name for name in sys.modules if name.startswith("threadkeeper")]:
        del sys.modules[name]
    host = importlib.import_module("threadkeeper.host")
    ingest = importlib.import_module("threadkeeper.ingest")
    monkeypatch.setattr(ingest, "_start_initial_ingest", lambda: None)
    monkeypatch.setattr(ingest, "_start_background_ingester", lambda: None)
    observed = {}

    def start_curator():
        from threadkeeper._mcp import active_tool_names
        from threadkeeper.tool_profiles import (
            TOOL_PARENT_ALLOWLIST_ENV, child_profile_environment,
        )

        child_env = child_profile_environment(active_tool_names(), "curator")
        observed["tools"] = set(child_env[TOOL_PARENT_ALLOWLIST_ENV].split(","))

    curator = importlib.import_module("threadkeeper.curator")
    monkeypatch.setattr(curator, "start_curator_daemon", start_curator)
    monkeypatch.setattr(host, "_DAEMON_STARTERS", [("curator", "start_curator_daemon")])

    host.start_daemons()

    assert {"curator_report_write", "lesson_get", "whisper"} <= observed["tools"]


def _reimport(monkeypatch, tmp_path):
    for k, v in {"THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
                 "THREADKEEPER_DAEMON_HOST": "1",
                 "THREADKEEPER_DISABLE_BG_DAEMONS": "1"}.items():
        monkeypatch.setenv(k, v)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    importlib.import_module("threadkeeper.server")  # register runtime
    return importlib.import_module("threadkeeper.host")


def test_start_daemons_calls_each_starter(monkeypatch, tmp_path):
    host = _reimport(monkeypatch, tmp_path)
    calls = []
    for modname, fn in [("retention", "start_retention_daemon"),
                        ("curator", "start_curator_daemon"),
                        ("shadow_review", "start_shadow_daemon")]:
        mod = importlib.import_module(f"threadkeeper.{modname}")
        monkeypatch.setattr(mod, fn, (lambda n: (lambda: calls.append(n)))(modname))
    started = host.start_daemons()
    assert "retention" in started and "curator" in started and "shadow_review" in started
    assert set(calls) >= {"retention", "curator", "shadow_review"}


def test_serve_wires_local_encoder(monkeypatch, tmp_path):
    host = _reimport(monkeypatch, tmp_path)
    seen = {}
    monkeypatch.setattr(host.host_embed, "serve_embed_socket",
                        lambda sock, enc: seen.update(sock=sock, enc=enc) or __import__("threading").Thread(target=lambda: None))
    host.start_embed_server()
    from threadkeeper import config
    assert seen["sock"] == config.HOST_SOCK_PATH
    # the wired encoder delegates to embeddings._encode
    assert callable(seen["enc"])


def test_wedge_deadline_self_heal(monkeypatch, tmp_path):
    host = _reimport(monkeypatch, tmp_path)
    f = host._wedge_deadline_passed
    # no starvation streak → never heals
    assert f(None, 1000.0, 600.0) is False
    # streak shorter than the window → hold on
    assert f(1000.0, 1000.0 + 599.0, 600.0) is False
    # streak reached the window → self-heal
    assert f(1000.0, 1000.0 + 600.0, 600.0) is True
    assert f(1000.0, 1000.0 + 900.0, 600.0) is True
    # feature disabled (wedge_s <= 0) → never heals even when long-starved
    assert f(1000.0, 1000.0 + 100000.0, 0.0) is False


def test_heartbeat_returns_bool(monkeypatch, tmp_path):
    host = _reimport(monkeypatch, tmp_path)
    import threadkeeper.db as db
    monkeypatch.setattr(db, "run_write", lambda *a, **k: None)
    monkeypatch.setattr(host, "_heartbeat", host._heartbeat)  # ensure real fn
    assert host._heartbeat() is True
    def _boom(*a, **k):
        raise RuntimeError("wedged")
    monkeypatch.setattr(db, "run_write", _boom)
    assert host._heartbeat() is False
