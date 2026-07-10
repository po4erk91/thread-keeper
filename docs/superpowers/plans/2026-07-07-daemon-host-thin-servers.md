# Daemon-host + thin servers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace "a full MCP server per CLI session" with one headless daemon-host (owns the 15 background loops + the warm ONNX model + a narrow embed socket) and thin per-session servers (stdio MCP + direct SQLite, no daemons, no ONNX).

**Architecture:** A machine-wide host process is elected via a flock. Thin servers spawn it detached on startup and route only query-embedding to it over a unix socket (`~/.threadkeeper/host.sock`), falling back to FTS when it is unreachable. Everything is gated by `THREADKEEPER_DAEMON_HOST` (dark by default), so with the flag off behavior is byte-for-byte today's.

**Tech Stack:** Python 3.11+, stdlib `socket`/`json`/`subprocess`, pydantic-settings, sqlite (WAL), pytest (`--forked` in CI). No new third-party dependency.

## Global Constraints

- Python floor **3.11** (config uses `tomllib` + `dict[str, ...]`); target 3.11/3.12/3.13.
- **No new third-party dependency.** Embed IPC is stdlib `socket` + `json`.
- Flag default **off**: `THREADKEEPER_DAEMON_HOST=0` ⇒ zero behavior change. Every new code path is gated on it.
- Env knobs are read via the pydantic `Settings` class (prefix `THREADKEEPER_`) and re-exported as UPPER_CASE module constants in `config.py`; hot-reloadable ones flow through `_derive_constants`.
- Tests run under `pytest --forked`; each test re-imports the package via `fresh_mp`/bootstrap against a `tmp_path` DB. Never touch the real `~/.threadkeeper`.
- Socket + lock + host log live under `DB_PATH.parent` so a custom `THREADKEEPER_DB` keeps host and servers co-located.
- Run tests with the project venv from the worktree cwd, PYTHONPATH pinned to the worktree:
  `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest ...`

---

### Task 1: Config knobs + derived constants

**Files:**
- Modify: `threadkeeper/config.py` (Settings fields near the other `auto_update_*`/`memory_guard_*` fields; `_derive_constants`; module-constant publish list)
- Test: `tests/test_daemon_host_config.py`

**Interfaces:**
- Produces: module constants `DAEMON_HOST_ENABLED: bool`, `PROCESS_ROLE: str` (`"server"`|`"host"`), `HOST_SOCK_PATH: Path`, `HOST_HEARTBEAT_TTL_S: float`, `THIN_EMBED_FALLBACK: str` (`"fts"`|`"local"`), `HOST_LOCK_PATH: Path`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_daemon_host_config.py
"""Phase 1 config knobs (daemon-host)."""
from __future__ import annotations
import importlib, sys
from pathlib import Path


def _reimport(monkeypatch, tmp_path, **env):
    base = {
        "THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
        "THREADKEEPER_DISABLE_BG_DAEMONS": "1",
        "THREADKEEPER_INGEST_INTERVAL_S": "0",
    }
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    return importlib.import_module("threadkeeper.config")


def test_defaults_are_dark(monkeypatch, tmp_path):
    cfg = _reimport(monkeypatch, tmp_path)
    assert cfg.DAEMON_HOST_ENABLED is False
    assert cfg.PROCESS_ROLE == "server"
    assert cfg.THIN_EMBED_FALLBACK == "fts"
    assert cfg.HOST_SOCK_PATH == (tmp_path / "host.sock")
    assert cfg.HOST_LOCK_PATH == (tmp_path / "host.lock")
    assert cfg.HOST_HEARTBEAT_TTL_S > 0


def test_flag_and_role_and_sock_override(monkeypatch, tmp_path):
    sock = tmp_path / "custom.sock"
    cfg = _reimport(
        monkeypatch, tmp_path,
        THREADKEEPER_DAEMON_HOST="1",
        THREADKEEPER_ROLE="host",
        THREADKEEPER_HOST_SOCK=str(sock),
        THREADKEEPER_THIN_EMBED_FALLBACK="local",
    )
    assert cfg.DAEMON_HOST_ENABLED is True
    assert cfg.PROCESS_ROLE == "host"
    assert cfg.HOST_SOCK_PATH == sock
    assert cfg.THIN_EMBED_FALLBACK == "local"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_daemon_host_config.py -v`
Expected: FAIL — `AttributeError: module 'threadkeeper.config' has no attribute 'DAEMON_HOST_ENABLED'`.

- [ ] **Step 3: Add the Settings fields**

In `class Settings(BaseSettings)`, next to the `auto_update_*` block, add:

```python
    # ── Phase 1: daemon-host + thin servers (dark by default) ──
    daemon_host: bool = False              # THREADKEEPER_DAEMON_HOST
    role: str = "server"                   # THREADKEEPER_ROLE: server | host
    host_sock: str = ""                    # "" -> <db dir>/host.sock
    host_heartbeat_ttl_s: float = 120.0
    thin_embed_fallback: str = "fts"       # fts | local
```

- [ ] **Step 4: Publish derived constants**

In `_derive_constants(s)` add to the returned dict (so hot-reload picks up the flag/fallback), keeping path constants next to `DB_PATH`:

```python
        "DAEMON_HOST_ENABLED": bool(s.daemon_host),
        "PROCESS_ROLE": (s.role or "server").strip().lower(),
        "HOST_HEARTBEAT_TTL_S": float(s.host_heartbeat_ttl_s),
        "THIN_EMBED_FALLBACK": (s.thin_embed_fallback or "fts").strip().lower(),
```

Then, after `DB_PATH` is defined (module scope, non-hot-reloaded like the other path constants), add:

```python
_HOST_SOCK_OVERRIDE = (settings.host_sock or "").strip()
HOST_SOCK_PATH: Path = (
    Path(_HOST_SOCK_OVERRIDE).expanduser() if _HOST_SOCK_OVERRIDE
    else DB_PATH.parent / "host.sock"
)
HOST_LOCK_PATH: Path = DB_PATH.parent / "host.lock"
```

Add `DAEMON_HOST_ENABLED, PROCESS_ROLE, HOST_HEARTBEAT_TTL_S, THIN_EMBED_FALLBACK` to the `globals().update(_derive_constants(settings))` publish path (it already publishes everything the dict returns — no extra wiring), and export the two Path constants alongside `DB_PATH` in the module's constant list if one is maintained.

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_daemon_host_config.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add threadkeeper/config.py tests/test_daemon_host_config.py
git commit -m "feat(host): config knobs for daemon-host + thin servers (dark)"
```

---

### Task 2: Embed IPC — `host_embed.py`

**Files:**
- Create: `threadkeeper/host_embed.py`
- Test: `tests/test_host_embed.py`

**Interfaces:**
- Produces:
  - `serve_embed_socket(sock_path: Path, encode_fn) -> threading.Thread` — binds a unix socket, serves `{"v":1,"op":"embed","texts":[...]}` by calling `encode_fn(texts) -> list[list[float]] | None`, replies `{"v":1,"vectors":[...]}` or `{"v":1,"error":"..."}`. Returns the started daemon thread. `encode_fn` is injected so the socket layer has no import cycle with `embeddings`.
  - `embed_via_host(texts: list[str], sock_path: Path, timeout: float = 3.0) -> list[list[float]] | None` — client; returns vectors, or `None` on any failure (no host, timeout, malformed).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_host_embed.py
from __future__ import annotations
import time
from pathlib import Path
from threadkeeper import host_embed


def test_roundtrip_and_batch(tmp_path):
    sock = tmp_path / "h.sock"
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
    sock = tmp_path / "h.sock"
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_host_embed.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'threadkeeper.host_embed'`.

- [ ] **Step 3: Write the implementation**

```python
# threadkeeper/host_embed.py
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


def _handle(conn: socket.socket, encode_fn: Callable[[list], Optional[list]]) -> None:
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


def serve_embed_socket(sock_path: Path, encode_fn: Callable[[list], Optional[list]]) -> threading.Thread:
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
    srv.bind(str(sock_path))
    os.chmod(sock_path, 0o600)
    srv.listen(64)
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


def embed_via_host(texts: list, sock_path: Path, timeout: float = 3.0) -> Optional[list]:
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_host_embed.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add threadkeeper/host_embed.py tests/test_host_embed.py
git commit -m "feat(host): narrow embed-only unix-socket IPC (server + client)"
```

---

### Task 3: `embeddings._encode` role-aware routing

**Files:**
- Modify: `threadkeeper/embeddings.py` (`_encode`, line ~132)
- Test: `tests/test_embed_routing.py`

**Interfaces:**
- Consumes: `host_embed.embed_via_host` (Task 2); `config.DAEMON_HOST_ENABLED`, `config.PROCESS_ROLE`, `config.HOST_SOCK_PATH`, `config.THIN_EMBED_FALLBACK` (Task 1).
- Produces: unchanged `_encode(texts) -> np.ndarray | None` signature. In a thin process with the flag on, it returns host-embedded vectors (normalized) or `None` (fallback=fts) / local (fallback=local).

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_embed_routing.py -v`
Expected: FAIL — `test_thin_role_uses_socket` errors (`_encode` has no `host_embed` attr / still hits local model).

- [ ] **Step 3: Add the routing branch to `_encode`**

At the top of `_encode`, before the `with _model_lock:` block, add the thin-role branch. Add `from . import host_embed` and the config imports near the other `from .config import ...` at module top.

```python
def _encode(texts: list[str]):
    """(docstring unchanged)"""
    global _last_used_at
    from . import config as _cfg  # read live (hot-reloadable flag)
    if _cfg.DAEMON_HOST_ENABLED and _cfg.PROCESS_ROLE == "server":
        vecs = host_embed.embed_via_host(list(texts), _cfg.HOST_SOCK_PATH)
        if vecs is None:
            if _cfg.THIN_EMBED_FALLBACK == "local":
                pass  # fall through to the local model below
            else:
                return None  # fts fallback: caller degrades to FTS
        else:
            import numpy as np  # type: ignore
            arr = np.asarray(vecs, dtype="float32")
            norms = np.linalg.norm(arr, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            _last_used_at = time.time()
            return (arr / norms).astype("float32")
    with _model_lock:
        m = _get_model()
        ...  # unchanged remainder
```

(Add `from . import host_embed` at the module top with the other imports.)

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_embed_routing.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Regression — embeddings still fine with flag off**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_onnx_embeddings.py tests/test_vec_search.py -q`
Expected: PASS (flag defaults off → local path unchanged).

- [ ] **Step 6: Commit**

```bash
git add threadkeeper/embeddings.py tests/test_embed_routing.py
git commit -m "feat(host): route query embedding to host socket in thin role"
```

---

### Task 4: `host.py` — the daemon-host process

**Files:**
- Create: `threadkeeper/host.py`
- Test: `tests/test_host_main.py`

**Interfaces:**
- Consumes: `config.HOST_LOCK_PATH`, `config.HOST_SOCK_PATH` (Task 1); `host_embed.serve_embed_socket` (Task 2); `helpers.single_flight_lock`; `embeddings._encode`; the 15 daemon `start_*_daemon` functions.
- Produces:
  - `start_daemons() -> list[str]` — the exact daemon-start block moved out of `identity._ensure_session`; returns started names. Reused by tests.
  - `main() -> int` — acquire lock (exit 0 if held), set role=host, start daemons, bind embed socket, heartbeat loop until SIGTERM.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_host_main.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_host_main.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'threadkeeper.host'`.

- [ ] **Step 3: Write `host.py`**

Move the daemon-start block verbatim from `identity._ensure_session` (lines ~128–217, the `try: from . import X; X.start_*_daemon(); except: pass` series **plus** `ingest._start_background_ingester`) into `start_daemons()`. Keep the one-shot session ingest reads (`_ingest_all`, `_backfill_dialog_fts_if_empty`) in `identity` (Task 5) — only the *daemon* starters move.

```python
# threadkeeper/host.py
"""The daemon-host (Phase 1): one headless process per machine that owns the
background loops + the warm ONNX model + the embed socket. Elected via a flock;
always-on (the loops must run with no active CLI session)."""
from __future__ import annotations

import logging
import os
import signal
import time
from pathlib import Path

from . import config
from . import host_embed
from .helpers import single_flight_lock

logger = logging.getLogger(__name__)

_DAEMON_STARTERS = [
    ("retention", "start_retention_daemon"),
    ("search_proxy", "start_search_proxy"),
    ("spawn_budget", "start_budget_daemon"),
    ("memory_guard", "start_memory_guard_daemon"),
    ("auto_update", "start_auto_update_daemon"),
    ("skill_watcher", "start_skill_watcher"),
    ("skill_updater", "start_skill_update_daemon"),
    ("config_watcher", "start_config_watcher"),
    ("shadow_review", "start_shadow_daemon"),
    ("curator", "start_curator_daemon"),
    ("extract_daemon", "start_extract_daemon"),
    ("candidate_reviewer", "start_candidate_reviewer_daemon"),
    ("probe_daemon", "start_probe_daemon"),
    ("evolve_daemon", "start_evolve_daemon"),
    ("evolve_applier", "start_evolve_applier_daemon"),
    ("thread_janitor", "start_thread_janitor"),
    ("dialectic_miner", "start_dialectic_miner_daemon"),
    ("dialectic_validator", "start_dialectic_validator_daemon"),
]


def start_daemons() -> list[str]:
    """Start every background loop once, in THIS process. Mirrors the block
    that used to live in identity._ensure_session; each starter is idempotent
    and single-flight-guarded, so a double call is safe."""
    started: list[str] = []
    # periodic background ingester (moved from _ensure_session)
    try:
        from . import ingest
        ingest._start_background_ingester()
        started.append("ingest")
    except Exception:
        logger.debug("host: ingest start failed", exc_info=True)
    for modname, fn in _DAEMON_STARTERS:
        try:
            mod = __import__(f"threadkeeper.{modname}", fromlist=[fn])
            getattr(mod, fn)()
            started.append(modname)
        except Exception:
            logger.debug("host: start %s failed", modname, exc_info=True)
    return started


def start_embed_server():
    """Bind the embed socket, wiring the LOCAL encoder (host role)."""
    from . import embeddings

    def _encode_texts(texts):
        arr = embeddings._encode(list(texts))
        return None if arr is None else [list(map(float, row)) for row in arr]

    return host_embed.serve_embed_socket(config.HOST_SOCK_PATH, _encode_texts)


def main() -> int:
    """Elected headless host. Exits 0 immediately if another host holds the
    lock (idempotent spawn)."""
    os.environ["THREADKEEPER_ROLE"] = "host"
    config.reload_settings()  # re-derive PROCESS_ROLE == "host"
    with single_flight_lock("daemon-host") as locked:
        if not locked:
            return 0
        start_daemons()
        start_embed_server()
        stop = {"v": False}
        signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("v", True))
        while not stop["v"]:
            _heartbeat()
            time.sleep(min(30.0, config.HOST_HEARTBEAT_TTL_S / 2))
    return 0


def _heartbeat() -> None:
    from .db import get_db
    from . import identity
    try:
        # `_ensure_session(conn, client=)` (identity.py:83) registers/refreshes
        # a presence row under the given client label — used here to stamp the
        # host's own row that `_host_alive()` (Task 5) reads back.
        identity._ensure_session(get_db(), client="daemon-host")
    except Exception:
        logger.debug("host: heartbeat failed", exc_info=True)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_host_main.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add threadkeeper/host.py tests/test_host_main.py
git commit -m "feat(host): daemon-host main — election, daemons, embed server, heartbeat"
```

---

### Task 5: `ensure_host_running()` + thin `_ensure_session` split

**Files:**
- Modify: `threadkeeper/host.py` (add `ensure_host_running`)
- Modify: `threadkeeper/identity.py` (`_ensure_session`: gate the daemon-start block on the flag)
- Test: `tests/test_thin_session.py`

**Interfaces:**
- Consumes: `config.DAEMON_HOST_ENABLED`, `config.PROCESS_ROLE`, `config.HOST_LOCK_PATH` (Tasks 1, 4).
- Produces: `host.ensure_host_running() -> bool` — under the host lock, if no live host spawn one detached and return True (spawned) / False (already live or we are the host).

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_thin_session.py -v`
Expected: FAIL — `ensure_host_running` missing / thin session still starts daemons.

- [ ] **Step 3: Add `ensure_host_running` to `host.py`**

```python
import subprocess
import sys as _sys


def ensure_host_running() -> bool:
    """Called by a thin server at session start. If no live host, spawn one
    detached and return True; else False. Idempotent under the host lock."""
    if config.PROCESS_ROLE == "host":
        return False
    if _host_alive():
        return False
    with single_flight_lock("daemon-host-spawn") as locked:
        if not locked or _host_alive():
            return False
        log = open(config.HOST_LOCK_PATH.parent / "host.log", "ab", buffering=0)
        subprocess.Popen(
            [_sys.executable, "-m", "threadkeeper.host"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True,
        )
        return True


def _host_alive() -> bool:
    """A live host heartbeat within the TTL."""
    from .db import get_db
    try:
        row = get_db().execute(
            "SELECT heartbeat_at FROM presence WHERE client='daemon-host' "
            "ORDER BY heartbeat_at DESC LIMIT 1"
        ).fetchone()
    except Exception:
        return False
    if not row or row["heartbeat_at"] is None:
        return False
    return (time.time() - int(row["heartbeat_at"])) < config.HOST_HEARTBEAT_TTL_S
```

- [ ] **Step 4: Gate the daemon block in `identity._ensure_session`**

Wrap the daemon-start block (lines ~128–217) so it only runs when the flag is OFF; when ON, ensure the host instead. Keep the one-shot ingest reads (lines ~113–122) unconditional (cheap SQLite, per-session freshness).

Replace the existing in-line `try: from . import X; X.start_*_daemon()` series
(lines ~128–217) with a call to the single shared `host.start_daemons()` (Task
4), gated on the flag. No logic is duplicated — `host.start_daemons()` starts
the loops in *whatever process calls it*, so the flag-off path runs them
in-process exactly as before, and the host path runs them in the host. Both use
lazy `from . import host` (no import cycle).

Three cases (the host role must NOT restart daemons — `host.main()` already
did, and `_ensure_session` runs again on the host's own heartbeat):

```python
        # ... one-shot ingest reads stay here (unchanged: _ingest_all,
        #     _backfill_dialog_fts_if_empty) ...
        from . import config as _cfg
        from . import host
        if not _cfg.DAEMON_HOST_ENABLED:
            # legacy (flag off): run every loop in THIS process, as before.
            try:
                host.start_daemons()
            except Exception:
                pass
        elif _cfg.PROCESS_ROLE == "server":
            # thin server: delegate the loops to the shared host.
            try:
                host.ensure_host_running()
            except Exception:
                pass  # a thin server must never fail to start on host trouble
        # else: flag on + role == "host" — host.main() already started the
        # loops; a re-entrant _ensure_session (e.g. the host heartbeat) must
        # NOT restart them. Do nothing.
```

Note: the current code starts the background ingester via
`ingest._start_background_ingester()` and each daemon in its own `try/except`;
`host.start_daemons()` (Task 4) already wraps the ingester + all 18 starters the
same way, so the flag-off path is the *same* behavior, just centralized. The
`elif`/no-`else` structure ensures the host process never double-starts its own
loops on the heartbeat's re-entrant `_ensure_session`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_thin_session.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add threadkeeper/host.py threadkeeper/identity.py tests/test_thin_session.py
git commit -m "feat(host): thin _ensure_session ensures host, skips in-proc daemons"
```

---

### Task 6: `memory_guard` — supervise host, stop thrashing thin

**Files:**
- Modify: `threadkeeper/memory_guard.py` (`_idle_retire_candidates` / `_retire_plan`; add host-liveness supervision)
- Test: `tests/test_memory_guard_host.py`

**Interfaces:**
- Consumes: `config.DAEMON_HOST_ENABLED`, `config.HOST_HEARTBEAT_TTL_S` (Task 1); `host.ensure_host_running` (Task 5).
- Produces: with the flag on, a memory-guard pass (a) never lists a thin (non-host) process as an idle-retire candidate, (b) calls `ensure_host_running()` when the host row is stale.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_guard_host.py
from __future__ import annotations
import sys, importlib, time


def _reimport(monkeypatch, tmp_path, flag="1"):
    for k, v in {"THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
                 "THREADKEEPER_DAEMON_HOST": flag,
                 "THREADKEEPER_DISABLE_BG_DAEMONS": "1"}.items():
        monkeypatch.setenv(k, v)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    importlib.import_module("threadkeeper.server")
    return importlib.import_module("threadkeeper.memory_guard")


def test_thin_not_retired_when_flag_on(monkeypatch, tmp_path):
    mg = _reimport(monkeypatch, tmp_path, flag="1")
    procs = [{"pid": 111, "client": "claude", "heartbeat_age_s": 99999,
              "parent_alive": False, "rss_kb": 120000}]
    assert mg._idle_retire_candidates(procs) == []   # thin never retired under host mode


def test_stale_host_triggers_respawn(monkeypatch, tmp_path):
    mg = _reimport(monkeypatch, tmp_path, flag="1")
    from threadkeeper import host
    respawned = {"n": 0}
    monkeypatch.setattr(host, "ensure_host_running",
                        lambda: respawned.__setitem__("n", respawned["n"] + 1) or True)
    monkeypatch.setattr(mg, "_host_alive", lambda: False)
    mg.supervise_host()
    assert respawned["n"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_memory_guard_host.py -v`
Expected: FAIL — `supervise_host` missing; thin still a retire candidate.

- [ ] **Step 3: Implement supervision + guard the retire path**

In `_idle_retire_candidates`, short-circuit when the flag is on and the row is not the host:

```python
def _idle_retire_candidates(procs):
    from . import config as _cfg
    out = []
    for p in procs:
        if _cfg.DAEMON_HOST_ENABLED and p.get("client") != "daemon-host":
            continue  # thin servers are cheap; never idle-retire under host mode
        ...  # existing conditions unchanged
    return out
```

Add:

```python
def _host_alive() -> bool:
    from . import host
    return host._host_alive()


def supervise_host() -> None:
    """Respawn the daemon-host if its heartbeat is stale (flag on only)."""
    from . import config as _cfg
    if not _cfg.DAEMON_HOST_ENABLED:
        return
    if _host_alive():
        return
    try:
        from . import host
        host.ensure_host_running()
    except Exception:
        pass
```

Call `supervise_host()` once per `run_memory_guard` pass (near the top, before RSS work).

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_memory_guard_host.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add threadkeeper/memory_guard.py tests/test_memory_guard_host.py
git commit -m "feat(host): memory_guard supervises host, stops thin-server thrash"
```

---

### Task 7: Integration — host + two thin servers, end to end

**Files:**
- Test: `tests/test_daemon_host_integration.py`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_daemon_host_integration.py
"""End-to-end: a host process + two thin sessions against one tmp DB."""
from __future__ import annotations
import sys, importlib, time
import numpy as np


def _reimport(monkeypatch, tmp_path, role="server"):
    for k, v in {"THREADKEEPER_DB": str(tmp_path / "db.sqlite"),
                 "THREADKEEPER_DAEMON_HOST": "1",
                 "THREADKEEPER_ROLE": role,
                 "THREADKEEPER_DISABLE_BG_DAEMONS": "1",
                 "THREADKEEPER_INGEST_INTERVAL_S": "0"}.items():
        monkeypatch.setenv(k, v)
    for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
        del sys.modules[name]
    importlib.import_module("threadkeeper.server")
    return importlib


def test_thin_search_embeds_via_host_socket(monkeypatch, tmp_path):
    # host side: bind the embed socket with a deterministic encoder
    _reimport(monkeypatch, tmp_path, role="host")
    from threadkeeper import host_embed, config
    enc = lambda texts: [[1.0, 0.0] for _ in texts]
    t = host_embed.serve_embed_socket(config.HOST_SOCK_PATH, enc)
    try:
        for _ in range(50):
            if config.HOST_SOCK_PATH.exists():
                break
            time.sleep(0.02)
        # thin side: same DB dir, server role -> _encode must use the socket
        _reimport(monkeypatch, tmp_path, role="server")
        from threadkeeper import embeddings as thin_emb
        out = thin_emb._encode(["query"])
        assert out is not None
        np.testing.assert_allclose(out[0], [1.0, 0.0], atol=1e-6)
    finally:
        host_embed.stop_embed_socket()
        t.join(timeout=2)
```

- [ ] **Step 2: Run test to verify it fails, then passes**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest tests/test_daemon_host_integration.py -v`
Expected: FAIL before Tasks 2–3 land; PASS after (the socket path + routing exist).

- [ ] **Step 3: Full-suite regression (flag off is the default everywhere)**

Run: `PYTHONPATH="$PWD" /Users/dmytro/ai-memory/.venv/bin/python -m pytest -q --forked`
Expected: PASS — all existing tests green (nothing sets the flag except the new tests).

- [ ] **Step 4: Commit**

```bash
git add tests/test_daemon_host_integration.py
git commit -m "test(host): end-to-end host socket + thin routing integration"
```

---

### Task 8: Docs + rollout knobs surfaced

**Files:**
- Modify: `README.md` (env table + an architecture paragraph)
- Modify: `docs/ARCHITECTURE.md` (a "daemon-host / thin servers" section)
- Modify: `.env.example` (the five new knobs, commented, flag off)
- Modify: `CHANGELOG.md` (Unreleased → Added)

- [ ] **Step 1: Add the knobs to `.env.example`**

```bash
# ── Phase 1: daemon-host + thin servers (dark by default) ──
# THREADKEEPER_DAEMON_HOST=0            # 1 = one headless host owns the loops+model; per-session servers go thin
# THREADKEEPER_HOST_SOCK=              # embed socket path (default <db dir>/host.sock)
# THREADKEEPER_HOST_HEARTBEAT_TTL_S=120 # host liveness window for supervision/respawn
# THREADKEEPER_THIN_EMBED_FALLBACK=fts # fts | local — how a thin server embeds a query if the host is unreachable
# THREADKEEPER_ROLE=server             # set to 'host' only by `python -m threadkeeper.host` (do not set by hand)
```

- [ ] **Step 2: README env table rows + one architecture paragraph**

Add the five rows to the env-var table and a short paragraph under the architecture section describing the host/thin split, the embed socket, and the flag (dark by default; no CLI config change).

- [ ] **Step 3: `docs/ARCHITECTURE.md` section**

Add a "daemon-host + thin servers (#Phase-1)" subsection mirroring the design spec: election flock, moved daemon block, embed socket + FTS fallback, memory_guard supervision, always-on host.

- [ ] **Step 4: CHANGELOG entry**

```markdown
### Added

- **Daemon-host + thin per-session servers (dark, `THREADKEEPER_DAEMON_HOST`).**
  One headless host per machine owns the 15 background loops + the warm ONNX
  model + a narrow embed unix-socket; per-session servers go thin (stdio MCP +
  direct SQLite, no daemons, no ONNX) and route only query-embedding to the
  host, falling back to FTS when it is unreachable. Elected via a flock, spawned
  detached by the first thin server, supervised by memory_guard. Off by default;
  no CLI config change. Removes the per-session RAM multiplier and the
  reclaim-thrash root.
```

- [ ] **Step 5: Commit**

```bash
git add README.md docs/ARCHITECTURE.md .env.example CHANGELOG.md
git commit -m "docs(host): document daemon-host + thin servers + rollout flag"
```

---

## Self-Review

**Spec coverage:** host.py (T4), embed IPC (T2), embed_text routing (T3), _ensure_session split + ensure_host_running (T5), memory_guard supervision (T6), config knobs (T1), integration + failover (T7), rollout flag + docs (T1/T8). Every spec §Components item maps to a task.

**Placeholder scan:** New-file code is complete. The daemon-start block is centralized in `host.start_daemons()` (T4) and called from both the host main and the flag-off branch (T5) — no verbatim duplication. T3's "unchanged remainder" references the real current `_encode` model path the implementer keeps in place. Presence writer resolved: `identity._ensure_session(conn, client="daemon-host")` (identity.py:83).

**Type consistency:** `embed_via_host(texts, sock_path, timeout)` and `serve_embed_socket(sock_path, encode_fn)` are used identically in T2/T3/T4/T7. `ensure_host_running() -> bool`, `_host_alive() -> bool`, `start_daemons() -> list[str]` consistent across T4/T5/T6. `_encode` returns a normalized `np.ndarray | None` on every branch.

**Open confirmation for the implementer:** the `_DAEMON_STARTERS` list in T4 must mirror the current `_ensure_session` daemon-start block (identity.py ~128–217) exactly — diff the two before committing T4 so no starter is dropped or added, then T5 replaces that block with a single `host.start_daemons()` call (no duplication).
