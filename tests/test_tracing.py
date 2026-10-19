"""Privacy-safe OTLP tracing around spawned ThreadKeeper workflows."""
from __future__ import annotations

import json
import os
import threading
import time


_FAKE_CID = "77774444-5555-6666-7777-888899990000"


def _task(
    conn,
    task_id,
    trace,
    *,
    retry_attempt=0,
    parent_span_id="",
    secret="",
    model="gpt-5.3",
):
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, pid, parent_cid, spawned_cid, cwd, prompt, "
        "started_at, ended_at, return_code, chosen_cli, model, tokens_in, "
        "tokens_out, tokens_total, cost_usd, retry_attempt, traceparent, "
        "trace_workflow_span_id, trace_parent_span_id, trace_started_ns, "
        "trace_queue_wait_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id, 0, "parent", "child", "/private/workspace", secret,
            now, now + 1, 0, "codex", model, 12, 34, 46, 0.02,
            retry_attempt, trace.traceparent, trace.workflow_span_id,
            parent_span_id or trace.parent_span_id, trace.started_ns, 7,
        ),
    )
    conn.commit()


def _attrs(span):
    return dict(span.attributes)


def test_trace_links_parent_workflow_agent_tool_and_continuation(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import tracing

    exporter = tracing.InMemorySpanExporter()
    tracing._set_exporter_for_tests(exporter)
    try:
        with tracing.mcp_span("spawn"):
            first = tracing.new_child_trace()
        assert first is not None

        monkeypatch.setenv(tracing.TRACEPARENT_ENV, first.traceparent)
        tool = pkg["mcp"]._tool_manager._tools["core_list"].fn
        tool()

        conn = pkg["db"].get_db()
        _task(conn, "tk_trace_first", first)
        tracing.finish_task(conn, "tk_trace_first")

        retry = tracing.new_child_trace(first.traceparent)
        assert retry is not None
        _task(
            conn,
            "tk_trace_retry",
            retry,
            retry_attempt=1,
            parent_span_id=first.traceparent.split("-")[2],
        )
        tracing.finish_task(conn, "tk_trace_retry")
        assert tracing._flush_for_tests()
    finally:
        monkeypatch.delenv(tracing.TRACEPARENT_ENV, raising=False)
        tracing._reset_for_tests()

    spans = exporter.spans
    by_name = {}
    for span in spans:
        by_name.setdefault(span.name, []).append(span)
    parent = by_name["threadkeeper.mcp"][0]
    first_workflow, retry_workflow = by_name["threadkeeper.workflow"]
    first_agent, retry_agent = by_name["threadkeeper.agent"]
    tool_span = next(
        span for span in by_name["threadkeeper.mcp"]
        if _attrs(span).get("threadkeeper.tool") == "core_list"
    )

    assert {span.trace_id for span in spans} == {parent.trace_id}
    assert first_workflow.parent_span_id == parent.span_id
    assert first_agent.parent_span_id == first_workflow.span_id
    assert tool_span.parent_span_id == first_agent.span_id
    assert retry_workflow.parent_span_id == first_agent.span_id
    assert retry_agent.parent_span_id == retry_workflow.span_id
    assert _attrs(retry_agent)["threadkeeper.retry_count"] == 1
    assert _attrs(first_agent)["threadkeeper.queue_wait_ms"] == 7


def test_trace_export_uses_allowlist_not_task_payloads(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import tracing

    exporter = tracing.InMemorySpanExporter()
    tracing._set_exporter_for_tests(exporter)
    secret = "prompt=super-secret-token path=/Users/alice/private quote=remember-me"
    try:
        trace = tracing.new_child_trace()
        assert trace is not None
        conn = pkg["db"].get_db()
        _task(
            conn,
            "tk_trace_private",
            trace,
            secret=secret,
            model="/Users/alice/private/api_key-token",
        )
        tracing.finish_task(conn, "tk_trace_private")
        assert tracing._flush_for_tests()
    finally:
        tracing._reset_for_tests()

    payload = json.dumps([span.otlp_json() for span in exporter.spans])
    assert "super-secret-token" not in payload
    assert "/Users/alice/private" not in payload
    assert "remember-me" not in payload
    assert "/Users/alice/private/api_key-token" not in payload
    for span in exporter.spans:
        assert set(_attrs(span)) <= {
            "threadkeeper.operation",
            "threadkeeper.latency_ms",
            "threadkeeper.provider",
            "gen_ai.provider.name",
            "gen_ai.request.model",
            "threadkeeper.queue_wait_ms",
            "gen_ai.usage.input_tokens",
            "gen_ai.usage.output_tokens",
            "threadkeeper.tokens.total",
            "threadkeeper.cost.usd",
            "threadkeeper.retry_count",
        }


def test_trace_context_travels_in_private_spawn_environment(mp_with_cid, monkeypatch):
    pkg = mp_with_cid(_FAKE_CID)
    from threadkeeper import tracing
    import threadkeeper.adapters.codex as codex
    import threadkeeper.identity as identity
    import threadkeeper.spawn_config as spawn_config
    import threadkeeper.tools.spawn as spawn

    exporter = tracing.InMemorySpanExporter()
    tracing._set_exporter_for_tests(exporter)
    monkeypatch.setattr(identity, "_active_cli", "codex")
    monkeypatch.setattr(spawn_config, "resolve_agent", lambda *_args: "codex")
    monkeypatch.setattr(spawn_config, "resolve_model", lambda *_args: "gpt-test")
    monkeypatch.setattr(codex.shutil, "which", lambda _name: "/fake/codex")
    captured = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = list(args)
            captured["env"] = dict(kwargs["env"])
            self.pid = 1234

    monkeypatch.setattr(spawn.subprocess, "Popen", FakePopen)
    try:
        result = spawn.spawn(
            prompt="safe task text",
            cwd=str(pkg["tmp"]),
            visible=False,
            capture_output=False,
        )
    finally:
        tracing._reset_for_tests()

    assert result.startswith("ok task=")
    traceparent = captured["env"][tracing.TRACEPARENT_ENV]
    assert traceparent.startswith("00-")
    assert traceparent not in captured["args"]
    task_id = result.split()[1].split("=", 1)[1]
    row = pkg["db"].get_db().execute(
        "SELECT traceparent FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    assert row["traceparent"] == traceparent


def test_trace_exporter_drops_on_failure_and_stays_bounded():
    from threadkeeper import tracing

    release = threading.Event()

    class BlockingExporter:
        def export(self, _spans):
            release.wait(1)
            raise OSError("collector unavailable")

    exporter = tracing.BufferedSpanExporter(BlockingExporter(), max_queue_size=2)
    context = tracing.TraceContext("a" * 32, "b" * 16)
    for _ in range(20):
        exporter.emit(tracing._record(
            context,
            parent_span_id="",
            name="threadkeeper.mcp",
            start_ns=time.time_ns(),
            end_ns=time.time_ns(),
            operation="mcp.tool",
            outcome="success",
        ))
    assert exporter.pending <= 2
    release.set()
    assert exporter.flush()


def test_trace_model_cardinality_is_capped():
    from threadkeeper import tracing

    tracing._reset_for_tests()
    context = tracing.TraceContext("a" * 32, "b" * 16)
    models = []
    for index in range(65):
        span = tracing._record(
            context,
            parent_span_id="",
            name="threadkeeper.agent",
            start_ns=time.time_ns(),
            end_ns=time.time_ns(),
            operation="agent.run",
            model=f"model-{index}",
            outcome="success",
        )
        models.append(_attrs(span)["gen_ai.request.model"])
    tracing._reset_for_tests()

    assert models[:64] == [f"model-{index}" for index in range(64)]
    assert models[64] == "unknown"
    assert tracing._safe_operation("prompt-derived-operation") == "unknown"
