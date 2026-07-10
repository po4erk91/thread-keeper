# tests/test_embed_routing.py
from __future__ import annotations
import sys, importlib
import numpy as np


def _reimport(monkeypatch, tmp_path, **env):
    base = {"THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
            "THREADKEEPER_DISABLE_BG_DAEMONS": "1"}
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    return importlib.import_module("threadkeeper.embeddings")


def test_thin_role_uses_socket(monkeypatch, tmp_path):
    emb = _reimport(monkeypatch, tmp_path,
                    THREADKEEPER_DAEMON_HOST="1", THREADKEEPER_ROLE="server")
    monkeypatch.setattr(emb.host_embed, "embed_via_host",
                        lambda texts, sock, timeout=3.0: [[3.0, 4.0]] * len(texts))
    out = emb._encode(["hello"])
    # returned vector is L2-normalized: [3,4] -> [0.6,0.8]
    assert out is not None
    np.testing.assert_allclose(out[0], [0.6, 0.8], atol=1e-6)


def test_thin_role_fts_fallback_returns_none(monkeypatch, tmp_path):
    emb = _reimport(monkeypatch, tmp_path,
                    THREADKEEPER_DAEMON_HOST="1", THREADKEEPER_ROLE="server",
                    THREADKEEPER_THIN_EMBED_FALLBACK="fts")
    monkeypatch.setattr(emb.host_embed, "embed_via_host",
                        lambda texts, sock, timeout=3.0: None)
    assert emb._encode(["hello"]) is None


def test_flag_off_uses_local(monkeypatch, tmp_path):
    emb = _reimport(monkeypatch, tmp_path, THREADKEEPER_DAEMON_HOST="0",
                    THREADKEEPER_ROLE="server")
    called = {"local": 0}
    monkeypatch.setattr(emb, "_get_model", lambda: called.__setitem__("local", called["local"] + 1) or None)
    emb._encode(["hello"])  # flag off -> local path (model None here -> None)
    assert called["local"] == 1
