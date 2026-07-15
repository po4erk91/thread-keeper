"""Server side of cross-machine sync: a tiny HTTP endpoint peers pull from and
push to. Symmetric with sync/daemon.py — every instance runs both.

Endpoints (bearer-token auth via THREADKEEPER_SYNC_TOKEN):
  POST /sync/pull  {vv}        -> {vv, changes}   changes the caller is missing
  POST /sync/push  {changes}   -> {applied}       merge the caller's changes

Transport is plain HTTP: the deployment is expected to run over an already-
encrypted private network (WireGuard/OpenVPN/Tailscale or LAN). The shared
token authenticates peers. TLS termination can be added in front (or as a
follow-up) without touching the protocol. OFF by default (no listen address).
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..config import SYNC_LISTEN, SYNC_TOKEN
from ..db import get_db
from . import protocol
from .capture import is_migrated

logger = logging.getLogger(__name__)
_started = False


class _Handler(BaseHTTPRequestHandler):
    def _authorized(self) -> bool:
        if not SYNC_TOKEN:
            return False  # never serve without a token configured
        # Constant-time compare: no early-out timing leak on the token even on a
        # private network. compare_digest also tolerates a missing/short header.
        return hmac.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {SYNC_TOKEN}"
        )

    def _send_json(self, code: int, obj: dict) -> None:
        data = protocol.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = protocol.loads(self.rfile.read(length).decode() or "{}")
        except Exception:
            self._send_json(400, {"error": "bad_json"})
            return
        conn = get_db()
        try:
            if not is_migrated(conn):
                self._send_json(409, {"error": "not_migrated"})
                return
            if self.path.rstrip("/") == "/sync/pull":
                vv = body.get("vv", {})
                self._send_json(200, {
                    "vv": protocol.version_vector(conn),
                    "changes": protocol.collect_changes(conn, vv),
                })
            elif self.path.rstrip("/") == "/sync/push":
                n = protocol.apply_changes(conn, body.get("changes", []))
                protocol.rebuild_derived(conn)
                self._send_json(200, {"applied": n})
            else:
                self._send_json(404, {"error": "not_found"})
        finally:
            conn.close()

    def log_message(self, *a) -> None:  # silence default stderr logging
        pass


def _parse_listen(listen: str) -> tuple[str, int] | None:
    if not listen or ":" not in listen:
        return None
    host, _, port = listen.rpartition(":")
    try:
        return (host or "0.0.0.0", int(port))
    except ValueError:
        return None


def _is_safe_bind_host(host: str) -> bool:
    """A bind host needs no override only if it is loopback or a private/link-
    local address. Wildcard binds (0.0.0.0 / ::) expose every interface — public
    ones included — and a bare hostname can't be proven private, so both require
    the explicit override. The DB replicates full private transcripts."""
    h = (host or "").strip().strip("[]")
    if h in ("", "0.0.0.0", "::", "*"):
        return False
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False  # hostname, not a literal IP — cannot verify it's private
    return ip.is_loopback or ip.is_private or ip.is_link_local


def start_server() -> None:
    """Start the sync HTTP server if a listen address + token are configured."""
    global _started
    if _started:
        return
    addr = _parse_listen(SYNC_LISTEN)
    if addr is None or not SYNC_TOKEN:
        return
    from ..config import BACKGROUND_DAEMONS_ALLOWED, SYNC_ALLOW_PUBLIC_BIND
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    if not _is_safe_bind_host(addr[0]) and not SYNC_ALLOW_PUBLIC_BIND:
        logger.error(
            "sync server refusing to bind %s:%d — not a loopback/private "
            "address. The DB replicates full private transcripts. Bind a "
            "private/VPN address, or set THREADKEEPER_SYNC_ALLOW_PUBLIC_BIND=1 "
            "to override (only behind your own network controls).", *addr,
        )
        return
    httpd = ThreadingHTTPServer(addr, _Handler)
    t = threading.Thread(target=httpd.serve_forever, name="sync_server", daemon=True)
    t.start()
    _started = True
    logger.info("sync server listening on %s:%d", *addr)
