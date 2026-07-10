from __future__ import annotations
import sys, importlib


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
