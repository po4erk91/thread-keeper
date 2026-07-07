# tests/test_daemon_host_integration.py
"""End-to-end: a host process + two thin sessions against one tmp DB."""
from __future__ import annotations
import sys, importlib, tempfile, time
from pathlib import Path
import numpy as np


def _short_sock() -> Path:
    # AF_UNIX sun_path is limited to ~104 bytes (macOS/BSD) / 108 (Linux);
    # pytest's tmp_path nests deep enough to exceed that. Bind under a short
    # /tmp dir instead (same fix as tests/test_host_embed.py) so the host and
    # thin reimports below can still share one real socket.
    d = tempfile.mkdtemp(prefix="tk", dir="/tmp")
    return Path(d) / "host.sock"


def _reimport(monkeypatch, tmp_path, role="server", host_sock: Path | None = None):
    env = {"THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
           "THREADKEEPER_DAEMON_HOST": "1",
           "THREADKEEPER_ROLE": role,
           "THREADKEEPER_DISABLE_BG_DAEMONS": "1",
           "THREADKEEPER_INGEST_INTERVAL_S": "0"}
    if host_sock is not None:
        # Both host and thin reimports must agree on HOST_SOCK_PATH, so pin
        # it explicitly rather than relying on the (too-long) tmp_path-derived
        # default of <db dir>/host.sock.
        env["THREADKEEPER_HOST_SOCK"] = str(host_sock)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    importlib.import_module("threadkeeper.server")
    return importlib


def test_thin_search_embeds_via_host_socket(monkeypatch, tmp_path):
    sock = _short_sock()
    # host side: bind the embed socket with a deterministic encoder
    _reimport(monkeypatch, tmp_path, role="host", host_sock=sock)
    from threadkeeper import host_embed, config
    enc = lambda texts: [[1.0, 0.0] for _ in texts]
    t = host_embed.serve_embed_socket(config.HOST_SOCK_PATH, enc)
    try:
        for _ in range(50):
            if config.HOST_SOCK_PATH.exists():
                break
            time.sleep(0.02)
        # thin side: same DB dir, server role -> _encode must use the socket
        _reimport(monkeypatch, tmp_path, role="server", host_sock=sock)
        from threadkeeper import embeddings as thin_emb
        out = thin_emb._encode(["query"])
        assert out is not None
        np.testing.assert_allclose(out[0], [1.0, 0.0], atol=1e-6)
    finally:
        host_embed.stop_embed_socket()
        t.join(timeout=2)
