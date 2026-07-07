"""Narrow embed-only IPC between thin servers and the daemon-host (Phase 1).

Thin servers do not load the ONNX model; to embed a search query they send the
text to the host over a unix socket and get the vector back. Wire format is
newline-delimited, versioned JSON:

    req  {"v":1,"op":"embed","texts":["...", "..."]}
    resp {"v":1,"vectors":[[...],[...]]}  |  {"v":1,"error":"..."}

The socket layer is dependency-injected with `encode_fn` so it never imports
`embeddings` (no cycle); the host wires the real encoder in.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_WIRE_V = 1
_server_sock: Optional[socket.socket] = None
_stop = threading.Event()


def _handle(conn: socket.socket, encode_fn: Callable[[list[str]], Optional[list[list[float]]]]) -> None:
    with conn:
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
        try:
            req = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
            texts = list(req.get("texts") or [])
            vecs = encode_fn(texts)
            if vecs is None:
                reply = {"v": _WIRE_V, "error": "embeddings_unavailable"}
            else:
                reply = {"v": _WIRE_V, "vectors": [list(map(float, v)) for v in vecs]}
        except Exception as e:  # never crash the host on a bad request
            logger.warning("embed request failed: %s", e)
            reply = {"v": _WIRE_V, "error": f"{type(e).__name__}: {e}"}
        try:
            conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
        except OSError:
            pass


def _serve_loop(srv: socket.socket, encode_fn) -> None:
    while not _stop.is_set():
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        threading.Thread(target=_handle, args=(conn, encode_fn), daemon=True).start()


def serve_embed_socket(sock_path: Path, encode_fn: Callable[[list[str]], Optional[list[list[float]]]]) -> threading.Thread:
    """Bind `sock_path` and serve embed requests via `encode_fn`. Idempotent
    cleanup of a stale socket file; returns the started accept thread."""
    global _server_sock
    _stop.clear()
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        sock_path.unlink()          # clear a stale socket from a dead host
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        srv.listen(64)
    except OSError:
        srv.close()
        raise
    _server_sock = srv
    t = threading.Thread(target=_serve_loop, args=(srv, encode_fn),
                         name="embed-socket", daemon=True)
    t.start()
    return t


def stop_embed_socket() -> None:
    _stop.set()
    global _server_sock
    if _server_sock is not None:
        try:
            _server_sock.close()
        finally:
            _server_sock = None


def embed_via_host(texts: list[str], sock_path: Path, timeout: float = 3.0) -> Optional[list[list[float]]]:
    """Client: ask the host to embed `texts`. Returns a list of vectors, or
    None on ANY failure (no host / timeout / malformed) so the caller can fall
    back to FTS."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(timeout)
            c.connect(str(sock_path))
            c.sendall((json.dumps({"v": _WIRE_V, "op": "embed",
                                   "texts": list(texts)}) + "\n").encode("utf-8"))
            buf = b""
            while b"\n" not in buf:
                chunk = c.recv(65536)
                if not chunk:
                    return None
                buf += chunk
        resp = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        if resp.get("error") or "vectors" not in resp:
            return None
        return resp["vectors"]
    except (OSError, ValueError, json.JSONDecodeError):
        return None
