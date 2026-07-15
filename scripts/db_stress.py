#!/usr/bin/env python3
"""Exercise thread-keeper's SQLite write path from concurrent processes.

The harness is intentionally self-contained so it can be used both by pytest
and during release/soak verification::

    python scripts/db_stress.py --processes 8 --ops 100
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _configure(db_path: str) -> None:
    os.environ["THREADKEEPER_DB"] = db_path
    os.environ["THREADKEEPER_NO_EMBEDDINGS"] = "1"
    os.environ["THREADKEEPER_DISABLE_BG_DAEMONS"] = "1"


def _worker(db_path: str, worker_id: int, ops: int, queue) -> None:
    _configure(db_path)
    try:
        from threadkeeper.db import read_db, run_write

        started = time.monotonic()
        latencies_ms: list[float] = []
        for index in range(ops):
            key = f"stress:{worker_id}:{index}"

            def insert(conn, *, _key=key) -> None:
                conn.execute(
                    "INSERT INTO style(key, value, updated_at) VALUES (?, ?, ?)",
                    (_key, "ok", int(time.time())),
                )

            op_started = time.perf_counter()
            run_write("db-stress-insert", insert, deadline_s=20.0)
            latencies_ms.append((time.perf_counter() - op_started) * 1000.0)
            if index % 10 == 0:
                with read_db() as conn:
                    conn.execute("SELECT COUNT(*) FROM style").fetchone()
        queue.put({
            "ok": True,
            "elapsed_s": time.monotonic() - started,
            "latencies_ms": latencies_ms,
        })
    except Exception:
        queue.put({"ok": False, "error": traceback.format_exc()})


def run(db_path: Path, processes: int, ops: int) -> dict:
    _configure(str(db_path))
    from threadkeeper.db import bootstrap_db, read_db

    bootstrap_db()
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    workers = [
        ctx.Process(target=_worker, args=(str(db_path), index, ops, queue))
        for index in range(processes)
    ]
    started = time.monotonic()
    for worker in workers:
        worker.start()
    results = [queue.get(timeout=90) for _ in workers]
    for worker in workers:
        worker.join(timeout=30)
        if worker.is_alive():
            worker.terminate()
            worker.join()
            results.append({"ok": False, "error": "worker timeout"})
        elif worker.exitcode != 0:
            results.append(
                {"ok": False, "error": f"worker exit code {worker.exitcode}"}
            )

    with read_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM style WHERE key LIKE 'stress:%'"
        ).fetchone()[0]
        quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
    errors = [result["error"] for result in results if not result["ok"]]
    latencies = sorted(
        latency
        for result in results
        for latency in result.get("latencies_ms", [])
    )

    def percentile(fraction: float) -> float:
        if not latencies:
            return 0.0
        rank = max(1, int(len(latencies) * fraction + 0.999))
        return round(latencies[min(rank, len(latencies)) - 1], 3)

    elapsed = time.monotonic() - started
    return {
        "processes": processes,
        "ops_per_process": ops,
        "expected_writes": processes * ops,
        "actual_writes": count,
        "quick_check": quick_check,
        "errors": errors,
        "elapsed_s": round(elapsed, 3),
        "writes_per_second": round(count / elapsed, 1) if elapsed else 0.0,
        "write_latency_ms": {
            "p50": percentile(0.50),
            "p95": percentile(0.95),
            "p99": percentile(0.99),
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
        "max_worker_s": round(
            max((result.get("elapsed_s", 0.0) for result in results), default=0.0),
            3,
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path)
    parser.add_argument("--processes", type=int, default=8)
    parser.add_argument("--ops", type=int, default=100)
    args = parser.parse_args()
    if args.processes < 1 or args.ops < 1:
        parser.error("--processes and --ops must be positive")

    if args.db:
        args.db.parent.mkdir(parents=True, exist_ok=True)
        result = run(args.db, args.processes, args.ops)
    else:
        with tempfile.TemporaryDirectory(prefix="threadkeeper-stress-") as tmp:
            result = run(Path(tmp) / "memory.db", args.processes, args.ops)
    print(json.dumps(result, sort_keys=True))
    return int(bool(result["errors"]) or result["actual_writes"] != result["expected_writes"]
               or result["quick_check"] != "ok")


if __name__ == "__main__":
    sys.exit(main())
