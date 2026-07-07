from __future__ import annotations
import tempfile
import time
from pathlib import Path
from threadkeeper import host_embed


def _short_sock():
    # AF_UNIX sun_path is limited to ~104 bytes (macOS/BSD) / 108 (Linux);
    # pytest's tmp_path nests deep enough to exceed that. Bind under a short
    # /tmp dir instead so the test exercises the real bind/connect path.
    d = tempfile.mkdtemp(prefix="tk", dir="/tmp")
    return Path(d) / "h.sock"


def test_roundtrip_and_batch(tmp_path):
    sock = _short_sock()
    # deterministic fake encoder: vector = [len(text), first-ord]
    enc = lambda texts: [[float(len(t)), float(ord(t[0]) if t else 0)] for t in texts]
    t = host_embed.serve_embed_socket(sock, enc)
    try:
        for _ in range(50):
            if sock.exists():
                break
            time.sleep(0.02)
        out = host_embed.embed_via_host(["ab", "xyz"], sock)
        assert out == [[2.0, 97.0], [3.0, 120.0]]
    finally:
        host_embed.stop_embed_socket()
        t.join(timeout=2)


def test_client_returns_none_when_no_host(tmp_path):
    assert host_embed.embed_via_host(["q"], tmp_path / "absent.sock", timeout=0.3) is None


def test_server_error_maps_to_none(tmp_path):
    sock = _short_sock()
    enc = lambda texts: (_ for _ in ()).throw(RuntimeError("boom"))
    t = host_embed.serve_embed_socket(sock, enc)
    try:
        for _ in range(50):
            if sock.exists():
                break
            time.sleep(0.02)
        assert host_embed.embed_via_host(["q"], sock) is None
    finally:
        host_embed.stop_embed_socket()
        t.join(timeout=2)
