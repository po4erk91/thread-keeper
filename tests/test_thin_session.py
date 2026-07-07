# tests/test_thin_session.py
from __future__ import annotations
import sys, importlib


def _reimport(monkeypatch, tmp_path, flag="1", role="server"):
    for k, v in {"THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
                 "THREADKEEPER_DAEMON_HOST": flag,
                 "THREADKEEPER_ROLE": role,
                 "THREADKEEPER_DISABLE_BG_DAEMONS": "1",
                 "THREADKEEPER_INGEST_INTERVAL_S": "0",
                 "THREADKEEPER_INGEST_CAP": "0"}.items():
        monkeypatch.setenv(k, v)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    importlib.import_module("threadkeeper.server")
    return (importlib.import_module("threadkeeper.identity"),
            importlib.import_module("threadkeeper.host"))


def test_thin_session_starts_no_daemons_and_ensures_host(monkeypatch, tmp_path):
    identity, host = _reimport(monkeypatch, tmp_path, flag="1", role="server")
    started = []
    monkeypatch.setattr(host, "start_daemons", lambda: started.append("daemons") or [])
    ensured = {"n": 0}
    monkeypatch.setattr(host, "ensure_host_running", lambda: ensured.__setitem__("n", ensured["n"] + 1) or True)
    # spy: no daemon starter should be invoked from a thin session
    import threadkeeper.shadow_review as sr
    monkeypatch.setattr(sr, "start_shadow_daemon", lambda: started.append("shadow"))
    from threadkeeper.db import get_db
    identity._ensure_session(get_db())
    assert "shadow" not in started        # thin server started no daemon
    assert ensured["n"] >= 1              # but ensured a host


def test_flag_off_still_starts_daemons_inproc(monkeypatch, tmp_path):
    identity, host = _reimport(monkeypatch, tmp_path, flag="0", role="server")
    hits = []
    import threadkeeper.shadow_review as sr
    monkeypatch.setattr(sr, "start_shadow_daemon", lambda: hits.append("shadow"))
    # other starters harmless under DISABLE_BG_DAEMONS; assert the gate path ran
    from threadkeeper.db import get_db
    identity._ensure_session(get_db())
    # with the flag OFF the legacy in-process branch runs (shadow starter reached)
    assert hits == ["shadow"]
