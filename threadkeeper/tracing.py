"""Privacy-safe OTLP tracing for spawned agent workflows.

Tracing is deliberately opt-in.  This module only builds spans from its typed
allowlist; it never accepts prompts, tool arguments/results, paths, quotes, or
memory content as span attributes.  Exporting happens on a bounded daemon
worker, so an unavailable collector cannot delay the local workflow.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import logging
import os
import queue
import re
import secrets
import threading
import time
from typing import Iterator, Protocol
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

TRACEPARENT_ENV = "THREADKEEPER_TRACEPARENT"
_TRACEPARENT_RE = re.compile(
    r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$"
)
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")
_SENSITIVE_IDENTIFIER_RE = re.compile(
    r"(?:api[_-]?key|authorization|credential|password|secret|token)", re.I
)
_ALLOWED_OPERATIONS = frozenset({"mcp.tool", "workflow.run", "agent.run"})
_MAX_QUEUE_SIZE = 2_048
_MAX_IDENTIFIER_CHARS = 64
_MAX_MODEL_CHARS = 96
_MAX_MODEL_VALUES = 64
_MAX_TOKENS = 2_000_000_000
_MAX_COST_USD = 1_000_000.0

_active_context: ContextVar["TraceContext | None"] = ContextVar(
    "threadkeeper_trace_context", default=None
)


@dataclass(frozen=True)
class TraceContext:
    """Minimal W3C trace context safe to persist and pass through env."""

    trace_id: str
    span_id: str
    trace_flags: str = "01"

    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"


@dataclass(frozen=True)
class ChildTrace:
    """Persisted parent/workflow/agent linkage for one spawned task."""

    traceparent: str
    workflow_span_id: str
    parent_span_id: str
    started_ns: int


@dataclass(frozen=True)
class SpanRecord:
    """A completed span containing only the approved low-cardinality fields."""

    trace_id: str
    span_id: str
    parent_span_id: str
    name: str
    start_ns: int
    end_ns: int
    attributes: tuple[tuple[str, str | int | float], ...]
    outcome: str

    def otlp_json(self) -> dict:
        """Return the OTLP/HTTP JSON representation for this one span."""
        attrs = [
            {"key": key, "value": _otlp_value(value)}
            for key, value in self.attributes
        ]
        attrs.append({
            "key": "threadkeeper.outcome",
            "value": _otlp_value(self.outcome),
        })
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id,
            "name": self.name,
            "kind": 1,
            "startTimeUnixNano": str(self.start_ns),
            "endTimeUnixNano": str(self.end_ns),
            "attributes": attrs,
            "status": {"code": 1 if self.outcome == "success" else 2},
        }


class SpanExporter(Protocol):
    def export(self, spans: list[SpanRecord]) -> None: ...


class InMemorySpanExporter:
    """Test exporter.  Production code never stores spans in process memory."""

    def __init__(self) -> None:
        self.spans: list[SpanRecord] = []
        self._lock = threading.Lock()

    def export(self, spans: list[SpanRecord]) -> None:
        with self._lock:
            self.spans.extend(spans)


class OTLPHTTPExporter:
    """Small OTLP/HTTP JSON exporter with no SDK dependency.

    OTLP/HTTP accepts the protobuf JSON mapping at ``/v1/traces``.  Keeping the
    transport here avoids loading tracing dependencies when the feature is off.
    """

    def __init__(self, endpoint: str, timeout_s: float) -> None:
        self.endpoint = endpoint
        self.timeout_s = max(0.1, min(float(timeout_s), 30.0))

    def export(self, spans: list[SpanRecord]) -> None:
        if not spans:
            return
        payload = {
            "resourceSpans": [{
                "resource": {"attributes": [{
                    "key": "service.name",
                    "value": {"stringValue": "threadkeeper"},
                }]},
                "scopeSpans": [{
                    "scope": {"name": "threadkeeper"},
                    "spans": [span.otlp_json() for span in spans],
                }],
            }],
        }
        request = Request(
            self.endpoint,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_s):
            pass


class BufferedSpanExporter:
    """Bounded asynchronous failure-isolating exporter."""

    def __init__(self, exporter: SpanExporter, max_queue_size: int) -> None:
        self.exporter = exporter
        self.max_queue_size = _bounded_queue_size(max_queue_size)
        self._queue: queue.Queue[SpanRecord] = queue.Queue(self.max_queue_size)
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def emit(self, span: SpanRecord) -> None:
        try:
            self._queue.put_nowait(span)
        except queue.Full:
            # Newer terminal spans are more useful than a stale queued entry.
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                self._queue.put_nowait(span)
            except (queue.Empty, queue.Full):
                return
        self._start_worker()

    def flush(self, timeout_s: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while self._queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def _start_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._run, name="threadkeeper-otel-export", daemon=True
            )
            self._worker.start()

    def _run(self) -> None:
        while True:
            span = self._queue.get()
            try:
                self.exporter.export([span])
            except Exception:
                # Export must not become a dependency of any local operation.
                logger.debug("OTLP trace export failed", exc_info=True)
            finally:
                self._queue.task_done()


_pipeline: BufferedSpanExporter | None = None
_pipeline_lock = threading.Lock()
_test_enabled: bool | None = None
_model_values: set[str] = set()
_model_values_lock = threading.Lock()


def _otlp_value(value: str | int | float) -> dict:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": value}


def _bounded_queue_size(value: int | float) -> int:
    try:
        return max(1, min(int(value), _MAX_QUEUE_SIZE))
    except (TypeError, ValueError):
        return 256


def _config(name: str, default):
    try:
        from . import config
        return getattr(config, name, default)
    except Exception:
        return default


def _is_enabled() -> bool:
    if _test_enabled is not None:
        return _test_enabled
    return bool(_config("OTEL_ENABLED", False) and _config("OTEL_ENDPOINT", ""))


def _configured_pipeline() -> BufferedSpanExporter | None:
    global _pipeline
    if not _is_enabled():
        return None
    if _pipeline is not None:
        return _pipeline
    endpoint = str(_config("OTEL_ENDPOINT", "")).strip()
    if not endpoint.startswith(("http://", "https://")):
        return None
    with _pipeline_lock:
        if _pipeline is None:
            _pipeline = BufferedSpanExporter(
                OTLPHTTPExporter(endpoint, _config("OTEL_EXPORT_TIMEOUT_S", 2.0)),
                _config("OTEL_EXPORT_QUEUE_SIZE", 256),
            )
    return _pipeline


def _parse_traceparent(value: str | None) -> TraceContext | None:
    match = _TRACEPARENT_RE.fullmatch((value or "").strip().lower())
    if match is None:
        return None
    trace_id, span_id, flags = match.groups()
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    return TraceContext(trace_id, span_id, flags)


def current_context() -> TraceContext | None:
    return _active_context.get() or _parse_traceparent(
        os.environ.get(TRACEPARENT_ENV)
    )


def _new_context(parent: TraceContext | None) -> TraceContext:
    return TraceContext(
        trace_id=parent.trace_id if parent else secrets.token_hex(16),
        span_id=secrets.token_hex(8),
        trace_flags=parent.trace_flags if parent else "01",
    )


def _safe_identifier(value: object, *, maximum: int = _MAX_IDENTIFIER_CHARS) -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > maximum
        or text.startswith(("/", "~"))
        or "\\" in text
        or ".." in text.split("/")
        or _SENSITIVE_IDENTIFIER_RE.search(text)
        or not _SAFE_IDENTIFIER_RE.fullmatch(text)
    ):
        return "unknown"
    return text


def _safe_model(value: object) -> str:
    """Bound model cardinality while preserving current configured models."""
    model = _safe_identifier(value, maximum=_MAX_MODEL_CHARS)
    if model == "unknown":
        return model
    with _model_values_lock:
        if model in _model_values:
            return model
        if len(_model_values) >= _MAX_MODEL_VALUES:
            return "unknown"
        _model_values.add(model)
    return model


def _safe_operation(value: object) -> str:
    operation = str(value or "")
    return operation if operation in _ALLOWED_OPERATIONS else "unknown"


def _safe_int(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= _MAX_TOKENS else None


def _safe_cost(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= _MAX_COST_USD else None


def _emit(span: SpanRecord) -> None:
    pipeline = _configured_pipeline()
    if pipeline is not None:
        pipeline.emit(span)


def _record(
    context: TraceContext,
    *,
    parent_span_id: str,
    name: str,
    start_ns: int,
    end_ns: int,
    operation: str,
    outcome: str,
    tool: str = "",
    provider: str = "",
    model: str = "",
    queue_wait_ms: int | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    tokens_total: int | None = None,
    cost_usd: float | None = None,
    retry_count: int | None = None,
) -> SpanRecord:
    latency_ms = max(0, (end_ns - start_ns) // 1_000_000)
    attrs: list[tuple[str, str | int | float]] = [
        ("threadkeeper.operation", _safe_operation(operation)),
        ("threadkeeper.latency_ms", latency_ms),
    ]
    if tool:
        attrs.append(("threadkeeper.tool", _safe_identifier(tool)))
    if provider:
        attrs.append(("gen_ai.provider.name", _safe_identifier(provider)))
    if model:
        attrs.append((
            "gen_ai.request.model",
            _safe_model(model),
        ))
    if queue_wait_ms is not None:
        attrs.append(("threadkeeper.queue_wait_ms", max(0, int(queue_wait_ms))))
    for key, value in (
        ("gen_ai.usage.input_tokens", _safe_int(tokens_in)),
        ("gen_ai.usage.output_tokens", _safe_int(tokens_out)),
        ("threadkeeper.tokens.total", _safe_int(tokens_total)),
        ("threadkeeper.cost.usd", _safe_cost(cost_usd)),
        ("threadkeeper.retry_count", _safe_int(retry_count)),
    ):
        if value is not None:
            attrs.append((key, value))
    return SpanRecord(
        trace_id=context.trace_id,
        span_id=context.span_id,
        parent_span_id=parent_span_id,
        name=name,
        start_ns=start_ns,
        end_ns=end_ns,
        attributes=tuple(attrs),
        outcome=(
            outcome
            if outcome in {"success", "error", "timeout", "cancelled"}
            else "error"
        ),
    )


@contextmanager
def mcp_span(tool_name: str) -> Iterator[None]:
    """Emit a safe span for an MCP tool call without observing its payload."""
    if not _is_enabled():
        yield
        return
    parent = current_context()
    context = _new_context(parent)
    token = _active_context.set(context)
    started_ns = time.time_ns()
    outcome = "success"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        _active_context.reset(token)
        _emit(_record(
            context,
            parent_span_id=parent.span_id if parent else "",
            name="threadkeeper.mcp",
            start_ns=started_ns,
            end_ns=time.time_ns(),
            operation="mcp.tool",
            tool=tool_name,
            outcome=outcome,
        ))


def new_child_trace(traceparent_override: str = "") -> ChildTrace | None:
    """Create workflow → agent context without putting it in a prompt/argv."""
    if not _is_enabled():
        return None
    parent = (
        _parse_traceparent(traceparent_override)
        if traceparent_override
        else current_context()
    )
    workflow = _new_context(parent)
    agent = _new_context(workflow)
    return ChildTrace(
        traceparent=agent.traceparent(),
        workflow_span_id=workflow.span_id,
        parent_span_id=parent.span_id if parent else "",
        started_ns=time.time_ns(),
    )


def finish_task(conn, task_id: str) -> None:
    """Queue terminal workflow/agent spans exactly once for a completed task."""
    if not _is_enabled():
        return
    try:
        row = conn.execute(
            "SELECT traceparent, trace_workflow_span_id, trace_parent_span_id, "
            "trace_started_ns, trace_queue_wait_ms, trace_exported_at, "
            "started_at, ended_at, return_code, chosen_cli, model, tokens_in, "
            "tokens_out, tokens_total, cost_usd, retry_attempt "
            "FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
    except Exception:
        return
    if not row or row["ended_at"] is None or row["trace_exported_at"] is not None:
        return
    context = _parse_traceparent(row["traceparent"])
    workflow_id = str(row["trace_workflow_span_id"] or "")
    if context is None or not workflow_id:
        return
    try:
        claimed = conn.execute(
            "UPDATE tasks SET trace_exported_at=? "
            "WHERE id=? AND trace_exported_at IS NULL",
            (int(time.time()), task_id),
        )
        if claimed.rowcount != 1:
            return
        conn.commit()
    except Exception:
        return
    started_ns = int(
        row["trace_started_ns"] or int(row["started_at"]) * 1_000_000_000
    )
    ended_ns = max(started_ns, int(row["ended_at"]) * 1_000_000_000)
    return_code = row["return_code"]
    if return_code == 0:
        outcome = "success"
    elif return_code == 124:
        outcome = "timeout"
    elif return_code is not None and int(return_code) < 0:
        outcome = "cancelled"
    else:
        outcome = "error"
    common = {
        "provider": str(row["chosen_cli"] or ""),
        "model": str(row["model"] or ""),
        "queue_wait_ms": int(row["trace_queue_wait_ms"] or 0),
        "tokens_in": row["tokens_in"],
        "tokens_out": row["tokens_out"],
        "tokens_total": row["tokens_total"],
        "cost_usd": row["cost_usd"],
        "retry_count": row["retry_attempt"],
        "outcome": outcome,
    }
    _emit(_record(
        context,
        parent_span_id=workflow_id,
        name="threadkeeper.agent",
        start_ns=started_ns,
        end_ns=ended_ns,
        operation="agent.run",
        **common,
    ))
    workflow_context = TraceContext(
        context.trace_id, workflow_id, context.trace_flags
    )
    _emit(_record(
        workflow_context,
        parent_span_id=str(row["trace_parent_span_id"] or ""),
        name="threadkeeper.workflow",
        start_ns=started_ns,
        end_ns=ended_ns,
        operation="workflow.run",
        **common,
    ))


def finish_task_from_path(db_path: str, task_id: str) -> None:
    """Best-effort terminal hook for the standalone child exit recorder."""
    if not _is_enabled() or not db_path or not task_id:
        return
    try:
        import sqlite3
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            finish_task(conn, task_id)
        finally:
            conn.close()
    except Exception:
        logger.debug("could not finish task trace", exc_info=True)


def _set_exporter_for_tests(
    exporter: SpanExporter, *, queue_size: int = 256
) -> BufferedSpanExporter:
    """Install an in-memory/failing exporter for focused unit tests."""
    global _pipeline, _test_enabled
    _test_enabled = True
    _pipeline = BufferedSpanExporter(exporter, queue_size)
    return _pipeline


def _reset_for_tests() -> None:
    global _pipeline, _test_enabled
    _pipeline = None
    _test_enabled = None
    with _model_values_lock:
        _model_values.clear()


def _flush_for_tests(timeout_s: float = 2.0) -> bool:
    return _pipeline.flush(timeout_s) if _pipeline is not None else True
