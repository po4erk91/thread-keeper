"""Local thread tracking shared by long-lived daemon starters.

The daemon host is a process, so ``threading.enumerate()`` is the only source
of truth for a thread's current liveness.  Keeping the latest ``Thread``
object as well gives a starter a reliable way to replace a thread that exited
after its old module-level ``_started`` latch was set.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class _TrackedThread:
    thread: threading.Thread
    started_at: int


_threads: dict[str, _TrackedThread] = {}
_lock = threading.RLock()


def daemon_thread(name: str) -> threading.Thread | None:
    """Return the current named daemon thread, if it exists in this process."""
    with _lock:
        tracked = _threads.get(name)
        if tracked is not None:
            return tracked.thread
    for thread in threading.enumerate():
        if thread.name == name:
            return thread
    return None


def daemon_thread_status(name: str) -> dict[str, int | bool | None]:
    """Small serializable liveness snapshot for one named daemon thread."""
    thread = daemon_thread(name)
    with _lock:
        tracked = _threads.get(name)
        started_at = tracked.started_at if tracked is not None else None
    return {
        "thread_alive": bool(thread and thread.is_alive()),
        "thread_started_at": started_at,
    }


def daemon_thread_alive(name: str) -> bool:
    return bool(daemon_thread_status(name)["thread_alive"])


def start_daemon_thread(name: str, target: Callable[[], None]) -> threading.Thread:
    """Return a live daemon thread for ``name``, creating one when needed.

    This is deliberately idempotent.  A dead tracked thread is replaced even
    when its owning module's legacy ``_started`` flag remains true.
    """
    with _lock:
        current = daemon_thread(name)
        if current is not None and current.is_alive():
            return current
        thread = threading.Thread(target=target, name=name, daemon=True)
        _threads[name] = _TrackedThread(thread=thread, started_at=int(time.time()))
        thread.start()
        return thread
