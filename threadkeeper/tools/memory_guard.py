"""MCP tools for the thread-keeper server RSS guard."""

from .._mcp import read_tool, write_tool
from .. import memory_guard
from ..db import get_db
from ..identity import _ensure_session


def _fmt_proc(p: dict, prefix: str) -> str:
    return (
        f"  {prefix} pid={p['pid']} rss={p['rss_mb']}MB "
        f"ppid={p['ppid']} etime={p['etime']}"
    )


@read_tool()
def memory_guard_status() -> str:
    """Show memory-guard thresholds and current thread-keeper RSS rows."""
    conn = get_db()
    _ensure_session(conn)
    result = memory_guard.scan_over_limit()
    procs = [
        dict(p, rss_mb=p.get("rss_mb", p["rss_kb"] // 1024))
        for p in result["procs"]
    ]
    total_mb = sum(p["rss_mb"] for p in procs)
    state = "disabled" if result["poll_s"] <= 0 else "active"
    agg = result["aggregate"]
    agg_marker = "KILL" if agg["kill"] else ("WARN" if agg["warn"] else "ok")
    out = [
        f"state={state} poll_s={result['poll_s']:.0f} "
        f"warn_mb={result['warn_mb']} kill_mb={result['kill_mb']} "
        f"agg_warn_mb={agg['warn_mb']} agg_kill_mb={agg['kill_mb']} "
        f"target_servers={agg['target_servers']} "
        f"retire_live={'on' if agg['retire_live'] else 'off'} "
        f"coordinator={'on' if result['coordinator'] else 'off'} "
        f"coordinator_pid={result['coordinator_pid'] or '-'} "
        f"notify={'on' if result['notify'] else 'off'}",
        f"processes={len(procs)} rss_total={total_mb}MB aggregate={agg_marker}",
    ]
    warn_pids = {p["pid"] for p in result["warn"]}
    kill_pids = {p["pid"] for p in result["kill"]}
    retire_pids = {p["pid"] for p in result["retire"]}
    for p in procs:
        marker = "KILL" if p["pid"] in kill_pids else (
            "RETIRE" if p["pid"] in retire_pids else (
                "WARN" if p["pid"] in warn_pids else "ok"
            )
        )
        out.append(_fmt_proc(p, marker))
    return "\n".join(out)


@write_tool(destructive=True)
def memory_guard_check(dry_run: bool = True, notify: bool = False) -> str:
    """Run one memory-guard pass now.

    Defaults to dry-run and no desktop notification. Pass dry_run=False to
    SIGTERM thread-keeper server processes over the kill threshold.
    """
    conn = get_db()
    _ensure_session(conn)
    result = memory_guard.check_once(dry_run=dry_run, notify=notify)
    warn = result["warn"]
    kill = result["kill"]
    retire = result["retire"]
    agg = result["aggregate"]
    if not warn and not kill and not agg["warn"] and not retire:
        return (
            f"ok: no process over thresholds "
            f"(warn={result['warn_mb']}MB kill={result['kill_mb']}MB "
            f"agg_warn={agg['warn_mb']}MB agg_kill={agg['kill_mb']}MB)"
        )
    action = "dry_run" if dry_run else "applied"
    out = [
        f"{action}: warn={len(warn)} kill={len(kill)} "
        f"aggregate={'KILL' if agg['kill'] else ('WARN' if agg['warn'] else 'ok')} "
        f"retire={len(retire)}"
    ]
    for p in warn:
        out.append(_fmt_proc(p, "WARN"))
    for p in kill:
        verb = "would SIGTERM" if dry_run else "SIGTERM"
        out.append(_fmt_proc(p, verb))
    for p in retire:
        verb = "would retire" if dry_run else "retired"
        out.append(_fmt_proc(p, verb))
    if not dry_run:
        out.append(
            f"killed={len(result['killed'])} retired={len(result['retired'])} "
            f"trim_requested={result['reclaim_requests']['count']} "
            f"skipped={len(result.get('skipped', []))} "
            f"failed={len(result['failed'])}"
        )
        if result.get("local_reclaim"):
            r = result["local_reclaim"]
            out.append(
                f"  reclaim self before={r['before_mb']}MB "
                f"after={r['after_mb']}MB freed={r['freed_mb']}MB"
            )
        for f in result["failed"]:
            out.append(f"  ERR pid={f['pid']} {f['err']}")
        for s in result.get("skipped", []):
            out.append(f"  SKIP pid={s['pid']} {s['action']} {s['reason']}")
    return "\n".join(out)


@write_tool()
def memory_guard_reclaim(scope: str = "self") -> str:
    """Unload thread-keeper model/caches now.

    `scope`: `self` trims this MCP process immediately. `all` also queues
    trim requests for peer thread-keeper server processes; peers handle the
    request on their next guard tick.
    """
    conn = get_db()
    _ensure_session(conn)
    scope = (scope or "self").strip().lower()
    if scope not in {"self", "all"}:
        return "ERR bad_scope (use self|all)"
    result = memory_guard.reclaim_memory(reason=f"manual:{scope}")
    out = [
        f"self pid={result['pid']} before={result['before_mb']}MB "
        f"after={result['after_mb']}MB freed={result['freed_mb']}MB",
        "actions=" + ",".join(result["actions"]),
    ]
    if scope == "all":
        req = memory_guard.request_reclaim(reason="manual_all")
        out.append(
            f"peer_trim_requested={req['count']} "
            f"pids={','.join(str(p) for p in req['requested']) or '-'}"
        )
    return "\n".join(out)
