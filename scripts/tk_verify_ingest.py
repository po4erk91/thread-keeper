#!/usr/bin/env python3
"""Cross-CLI ingest verification.

Walks every installed CLI adapter, asks it to enumerate its transcript
files, and runs the ingestion pipeline against an isolated test
database. Reports:

  * How many transcripts each adapter discovered
  * How many messages were ingested per `source` tag
  * The newest/oldest ingested timestamps per source
  * Anything that looks suspicious (zero ingest from a CLI that has
    files, malformed jsonl, etc.)

Doesn't touch the live ~/.threadkeeper/db.sqlite — uses a tempdir.
Read-only with respect to all CLI configs and transcripts.

Run:
    .venv/bin/python scripts/tk_verify_ingest.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path


def _human_ts(ts: int) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def main() -> int:
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

        print("=" * 68)
        print("thread-keeper cross-CLI ingest verification")
        print("=" * 68)
        print(f"  test db: {env['THREADKEEPER_DB']}")
        print()

        # Per-adapter discovery report
        print("[discovery]")
        installed = installed_adapters()
        for adapter in ADAPTERS:
            present = adapter in installed
            n_files = len(adapter.transcript_files()) if present else 0
            print(
                f"  {adapter.name:14s} installed={str(present):5s} "
                f"transcripts={n_files}"
            )
        print()

        if not installed:
            print("No CLIs detected — nothing to ingest.")
            return 1

        # Actual ingest — process each adapter independently with its
        # own generous cap so a chatty CLI (Claude with thousands of
        # transcripts) doesn't starve the others. Track both the raw
        # parse yield (every NormalizedMessage the adapter produced) and
        # the post-ingest count (after dialog_messages skip filters)
        # so we can distinguish "empty transcripts" from "adapter bug".
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
            print(
                f"  {adapter.name:14s}  files_processed={len(files):4d}  "
                f"parse_yield={raw:6d}  new_msgs={ad_new}"
            )
        print(f"  total: new_msgs={total_new}  files_processed={total_files}")
        print()

        # Per-source post-ingest stats
        print("[per-source breakdown]")
        rows = conn.execute(
            "SELECT source, COUNT(*) AS n, MIN(created_at) AS oldest, "
            "MAX(created_at) AS newest "
            "FROM dialog_messages GROUP BY source ORDER BY n DESC"
        ).fetchall()
        if not rows:
            print("  (no messages ingested)")
        else:
            for r in rows:
                src = r["source"]
                n = r["n"]
                oldest = _human_ts(r["oldest"] or 0)
                newest = _human_ts(r["newest"] or 0)
                print(
                    f"  {src:14s}  msgs={n:7d}  oldest={oldest}  newest={newest}"
                )
        print()

        # Sanity flags — three states per adapter:
        #   ✓ parsed > 0 AND ingested > 0  → working end-to-end
        #   · parsed = 0                  → empty transcripts (not a bug)
        #   ⚠ parsed > 0 AND ingested = 0 → real adapter / pipeline issue
        print("[sanity]")
        installed_names = {a.name for a in installed}
        ingested_names = {r["source"] for r in rows}
        for name in sorted(installed_names):
            yielded = parse_yield.get(name, 0)
            ingested = name in ingested_names
            if yielded > 0 and ingested:
                print(f"  ✓ {name}: ingest path working "
                      f"(parsed {yielded}, persisted)")
            elif yielded == 0:
                print(f"  · {name}: transcripts present but contain no "
                      f"user/assistant turns — empty sessions, skip")
            else:
                print(f"  ⚠ {name}: parsed {yielded} messages but 0 made "
                      f"it to dialog_messages — adapter or pipeline bug")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
