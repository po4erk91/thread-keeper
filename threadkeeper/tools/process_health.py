"""MCP tools for inspecting and pruning orphaned thread-keeper processes.

Each Claude client spawns its own thread-keeper subprocess. Crashed clients
leave orphan processes that hold RAM (especially with sentence-transformers
loaded). These tools surface the situation and let you clean up.
"""

from .._mcp import read_tool, write_tool, structured_result
from .. import process_health
from ..tool_schemas import MpHealth, MpProcess


@read_tool()
def mp_health() -> MpHealth:
    """Diagnostic snapshot of every running thread-keeper server process
    on this machine. Shows pid, parent status, RSS, heartbeat age, and
    whether each is classified as orphaned (parent gone + no fresh
    heartbeat from its session).

    Self (the process answering this call) is always marked is_self=true
    and never flagged as orphan. Returns structuredContent (MpHealth) plus
    the legacy text block."""
    procs = process_health.scan()
    total_kb = sum(p["rss_kb"] for p in procs)
    orphans = [p for p in procs if p.get("is_orphaned")]
    live = [p for p in procs if not p.get("is_orphaned")]
    model = MpHealth(
        total=len(procs),
        live=len(live),
        orphans=len(orphans),
        rss_total_mb=total_kb // 1024,
        processes=[MpProcess(**p) for p in procs],
    )
    if not procs:
        return structured_result("no_mp_processes_running", model)

    out = [
        f"total={len(procs)} live={len(live)} orphans={len(orphans)} "
        f"rss_total={total_kb // 1024}MB"
    ]
    for p in procs:
        flag = "self" if p["is_self"] else ("ORPHAN" if p["is_orphaned"] else "live")
        hb = p["heartbeat_age_s"]
        hb_disp = f"{hb}s" if hb is not None else "?"
        parent = "alive" if p["parent_alive"] else "dead"
        rss_mb = p["rss_kb"] // 1024
        out.append(
            f"  pid={p['pid']:<6} ppid={p['ppid']:<6} ({parent})  "
            f"rss={rss_mb}MB  hb={hb_disp}  etime={p['etime']}  "
            f"[{flag}]  {p.get('orphan_reason','-')}"
        )
    if orphans:
        out.append(
            f"\nCleanup plan: mp_cleanup(dry_run=False) would SIGTERM "
            f"{len(orphans)} orphan(s); add force=True for SIGKILL."
        )
    return structured_result("\n".join(out), model)


@write_tool(destructive=True)
def mp_cleanup(dry_run: bool = True, force: bool = False) -> str:
    """Kill orphaned thread-keeper processes (parent gone AND heartbeat
    stale for > 5 minutes). Defaults to dry-run — pass dry_run=False to
    actually send signals. force=True uses SIGKILL instead of SIGTERM.

    Never touches the current process or processes whose parent is still
    alive. Safe to run repeatedly."""
    result = process_health.cleanup(dry_run=dry_run, force=force)
    procs = result["all_procs"]
    orphans = result["orphans"]
    if not orphans:
        return (
            f"nothing_to_do: {len(procs)} mp process(es) running, "
            "all healthy"
        )
    if dry_run:
        lines = [f"plan dry_run=True orphans={len(orphans)}"]
        for p in orphans:
            rss_mb = p["rss_kb"] // 1024
            lines.append(
                f"  would SIGTERM pid={p['pid']} rss={rss_mb}MB "
                f"reason={p['orphan_reason']}"
            )
        free_mb = sum(p["rss_kb"] for p in orphans) // 1024
        lines.append(
            f"\napprox {free_mb}MB to be freed; call "
            "mp_cleanup(dry_run=False) to apply."
        )
        return "\n".join(lines)
    # Apply
    lines = [
        f"applied {'SIGKILL' if force else 'SIGTERM'}: "
        f"killed={len(result['killed'])} failed={len(result['failed'])}"
    ]
    for pid in result["killed"]:
        lines.append(f"  ok pid={pid}")
    for f in result["failed"]:
        lines.append(f"  ERR pid={f['pid']} {f['err']}")
    return "\n".join(lines)
