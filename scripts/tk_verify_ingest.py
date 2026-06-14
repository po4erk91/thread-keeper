#!/usr/bin/env python3
"""Cross-CLI ingest verification.

Two complementary checks, both read-only with respect to the live store:

  CONTRACT TEST (default + ``--contract``)
    Walks every installed CLI adapter, asks it to enumerate its transcript
    files, and runs the ingestion pipeline against an *isolated test
    database* (a tempdir — the live ~/.threadkeeper/db.sqlite is never
    touched). Reports per-adapter discovery, parse yield, and post-ingest
    counts, flagging any adapter that parsed messages but failed to
    persist them. Answers: "does the parse/ingest pipeline work?"

  PRODUCTION VERIFICATION (default + ``--live``)
    Reads the *live* dialog_messages table read-only and scores the three
    acceptance criteria from roadmap issue #1:
      1. dialog_messages.source carries rows from every targeted CLI slot
      2. shadow-review sees >1 adapter in the same recent window
      3. the learning loop has fired on non-Claude sessions
    Emits a PASS / PARTIAL / FAIL verdict. See threadkeeper/verify_ingest.py.

Run:
    .venv/bin/python scripts/tk_verify_ingest.py            # both checks
    .venv/bin/python scripts/tk_verify_ingest.py --live     # production only
    .venv/bin/python scripts/tk_verify_ingest.py --contract # contract only
    .venv/bin/python scripts/tk_verify_ingest.py --json     # machine-readable
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path


def _human_ts(ts: int) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _resolve_live_db() -> Path:
    """Live DB path, read BEFORE the contract test mangles the env."""
    env = os.environ.get("THREADKEEPER_DB")
    if env:
        return Path(env).expanduser()
    return Path("~/.threadkeeper/db.sqlite").expanduser()


def run_contract_test(quiet: bool = False) -> dict:
    """Adapter parse/ingest contract test against a throwaway DB.

    Returns a structured result; prints a human report unless ``quiet``.
    """
    out: dict = {"discovery": [], "ingest": [], "per_source": [], "sanity": []}
    # Hard-wire all daemons OFF in this throwaway env so we don't fork
    # background workers we'll have to terminate.
    with tempfile.TemporaryDirectory(prefix="tk_verify_") as td:
        td_path = Path(td)
        env = {
            "THREADKEEPER_DB": str(td_path / "db.sqlite"),
            "THREADKEEPER_INGEST_INTERVAL_S": "0",
            "THREADKEEPER_INGEST_CAP": "0",
            "THREADKEEPER_SKILL_WATCH_INTERVAL_S": "0",
            "THREADKEEPER_SPAWN_BUDGET_POLL_S": "0",
            "THREADKEEPER_SEARCH_PROXY_POLL_S": "0",
            "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S": "0",
            "THREADKEEPER_NO_EMBEDDINGS": "1",  # skip torch warmup for speed
            "THREADKEEPER_TASK_LOG_DIR": str(td_path / "tasks"),
        }
        for k, v in env.items():
            os.environ[k] = v
        # Drop any cached imports so config picks up the new env.
        for name in [m for m in list(sys.modules) if m.startswith("threadkeeper")]:
            del sys.modules[name]

        from threadkeeper.adapters import ADAPTERS, installed_adapters
        from threadkeeper.db import get_db
        from threadkeeper.ingest import _ingest_file

        if not quiet:
            print("=" * 68)
            print("thread-keeper cross-CLI ingest — contract test (temp DB)")
            print("=" * 68)
            print(f"  test db: {env['THREADKEEPER_DB']}")
            print()

        # Per-adapter discovery report
        if not quiet:
            print("[discovery]")
        installed = installed_adapters()
        for adapter in ADAPTERS:
            present = adapter in installed
            n_files = len(adapter.transcript_files()) if present else 0
            out["discovery"].append(
                {"adapter": adapter.name, "installed": present,
                 "transcripts": n_files}
            )
            if not quiet:
                print(
                    f"  {adapter.name:14s} installed={str(present):5s} "
                    f"transcripts={n_files}"
                )
        if not quiet:
            print()

        if not installed:
            if not quiet:
                print("No CLIs detected — nothing to ingest.")
            out["ok"] = False
            return out

        # Actual ingest — process each adapter independently with its own
        # generous cap so a chatty CLI doesn't starve the others. Track
        # both raw parse yield and post-ingest count to distinguish "empty
        # transcripts" from "adapter bug".
        if not quiet:
            print("[ingest]")
        conn = get_db()
        parse_yield: dict[str, int] = {}
        total_new = 0
        total_files = 0
        for adapter in installed:
            files = adapter.transcript_files()
            files = sorted(
                files,
                key=lambda p: p.stat().st_mtime if p.exists() else 0,
                reverse=True,
            )[:100]  # newest 100 — enough to exercise the parse path
            raw = 0
            for fp in files:
                for _ in adapter.iter_messages(fp):
                    raw += 1
            parse_yield[adapter.name] = raw
            ad_new = 0
            for fp in files:
                ad_new += _ingest_file(conn, fp, 5000, adapter=adapter)
            conn.commit()
            total_new += ad_new
            total_files += len(files)
            out["ingest"].append(
                {"adapter": adapter.name, "files_processed": len(files),
                 "parse_yield": raw, "new_msgs": ad_new}
            )
            if not quiet:
                print(
                    f"  {adapter.name:14s}  files_processed={len(files):4d}  "
                    f"parse_yield={raw:6d}  new_msgs={ad_new}"
                )
        if not quiet:
            print(f"  total: new_msgs={total_new}  files_processed={total_files}")
            print()

        # Per-source post-ingest stats
        if not quiet:
            print("[per-source breakdown]")
        rows = conn.execute(
            "SELECT source, COUNT(*) AS n, MIN(created_at) AS oldest, "
            "MAX(created_at) AS newest "
            "FROM dialog_messages GROUP BY source ORDER BY n DESC"
        ).fetchall()
        if not rows and not quiet:
            print("  (no messages ingested)")
        for r in rows:
            out["per_source"].append(
                {"source": r["source"], "msgs": r["n"],
                 "oldest": r["oldest"], "newest": r["newest"]}
            )
            if not quiet:
                print(
                    f"  {r['source']:14s}  msgs={r['n']:7d}  "
                    f"oldest={_human_ts(r['oldest'] or 0)}  "
                    f"newest={_human_ts(r['newest'] or 0)}"
                )
        if not quiet:
            print()

        # Sanity flags — three states per adapter:
        #   ✓ parsed > 0 AND ingested > 0  → working end-to-end
        #   · parsed = 0                  → empty transcripts (not a bug)
        #   ⚠ parsed > 0 AND ingested = 0 → real adapter / pipeline issue
        if not quiet:
            print("[sanity]")
        installed_names = {a.name for a in installed}
        ingested_names = {r["source"] for r in rows}
        for name in sorted(installed_names):
            yielded = parse_yield.get(name, 0)
            ingested = name in ingested_names
            if yielded > 0 and ingested:
                state, msg = "ok", "ingest path working"
            elif yielded == 0:
                state, msg = "empty", "transcripts present but no user/assistant turns"
            else:
                state, msg = "bug", "parsed messages but 0 persisted — adapter/pipeline bug"
            out["sanity"].append({"adapter": name, "state": state, "yielded": yielded})
            if not quiet:
                glyph = {"ok": "✓", "empty": "·", "bug": "⚠"}[state]
                print(f"  {glyph} {name}: {msg} (parsed {yielded})")
        out["ok"] = True
        # A parsed>0/persisted=0 adapter is the only hard failure of the
        # contract: it means the pipeline silently dropped real turns.
        out["contract_failures"] = [
            s["adapter"] for s in out["sanity"] if s["state"] == "bug"
        ]
        return out


def run_live_verification(live_db: Path, window_hours: int, quiet: bool = False) -> dict:
    """Production verification against the live DB (read-only)."""
    # Imported lazily and from a clean module state so the contract test's
    # tempdir env (if it ran first) can't leak the wrong DB path in here —
    # this reads ``live_db`` directly, not config.DB_PATH.
    from threadkeeper.verify_ingest import live_production_report, format_report

    if not live_db.exists():
        report = {
            "db_path": str(live_db),
            "verdict": "FAIL",
            "summary": f"live DB not found at {live_db}",
            "slots": {}, "criteria": {}, "signals": {},
            "error": "live_db_missing",
        }
        if not quiet:
            print(f"\n[live production verification]\n  live DB not found: {live_db}")
        return report

    report = live_production_report(live_db, window_hours=window_hours)
    if not quiet:
        print()
        print(format_report(report))
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Cross-CLI ingest verification")
    ap.add_argument("--contract", action="store_true",
                    help="run only the adapter parse/ingest contract test")
    ap.add_argument("--live", action="store_true",
                    help="run only the live production verification")
    ap.add_argument("--json", action="store_true",
                    help="emit a machine-readable JSON report")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero unless the live verdict is PASS")
    ap.add_argument("--window-hours", type=int, default=24,
                    help="recent-window size for the cross-adapter check")
    args = ap.parse_args(argv)

    # Neither flag → run both. One flag → run just that one.
    run_contract = args.contract or not args.live
    run_live = args.live or not args.contract

    # Resolve the live DB path up front: the contract test rewrites
    # THREADKEEPER_DB to a tempdir, so capture the real path before it runs.
    live_db = _resolve_live_db()

    result: dict = {}
    if run_contract:
        result["contract"] = run_contract_test(quiet=args.json)
    if run_live:
        result["live"] = run_live_verification(
            live_db, args.window_hours, quiet=args.json
        )

    if args.json:
        print(json.dumps(result, indent=2, default=str))

    # Exit code:
    #   1  → no CLIs detected at all (contract couldn't run)
    #   2  → a real contract failure (parsed>0 but persisted 0), or --strict
    #         and the live verdict is not PASS
    #   0  → otherwise (PARTIAL is a valid, non-error real-world state)
    contract = result.get("contract")
    if contract is not None and contract.get("ok") is False:
        return 1
    if contract is not None and contract.get("contract_failures"):
        return 2
    live = result.get("live")
    if args.strict and live is not None and live.get("verdict") != "PASS":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
