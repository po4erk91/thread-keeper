"""Step 5: HTTP transport — auth, routing, and JSON round-trip of the pull
endpoint against a live localhost server. End-to-end two-machine convergence is
a live-smoke step (docs/sync.md); the merge itself is covered by
test_sync_protocol."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer


def _serve(handler_mod):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_mod._Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, httpd.server_address[1]


def _post(port, path, obj, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(obj).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def test_pull_requires_token_and_returns_changeset(fresh_mp, monkeypatch):
    from threadkeeper.sync import migrate, server as sync_server
    db = fresh_mp["db"]
    db.get_db().close()
    assert migrate.apply(db.DB_PATH, do_apply=True) == 0

    monkeypatch.setattr(sync_server, "SYNC_TOKEN", "s3cret")
    httpd, port = _serve(sync_server)
    try:
        # no token → 401
        try:
            _post(port, "/sync/pull", {})
            assert False, "expected 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401

        # valid token → 200 with a well-formed changeset
        out = _post(port, "/sync/pull", {"vv": {}}, token="s3cret")
        assert "vv" in out and "changes" in out
        assert isinstance(out["changes"], list)

        # push endpoint accepts an (empty) changeset
        out = _post(port, "/sync/push", {"changes": []}, token="s3cret")
        assert out.get("applied") == 0
    finally:
        httpd.shutdown()
