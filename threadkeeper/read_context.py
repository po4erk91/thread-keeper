"""Per-call guard for MCP surfaces advertised as observational reads."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


_read_only_call: ContextVar[bool] = ContextVar("threadkeeper_read_only_call", default=False)


def is_read_only_call() -> bool:
    """Whether the current call entered through an advertised read surface."""
    return _read_only_call.get()


@contextmanager
def read_only_call() -> Iterator[None]:
    """Make database connections opened by this call SQLite query-only."""
    token = _read_only_call.set(True)
    try:
        yield
    finally:
        _read_only_call.reset(token)
