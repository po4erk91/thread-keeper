"""Stage, validate, and atomically activate an embedding generation.

Unlike the former in-place re-embedder, this command leaves the durable active
generation readable while it builds the target in ``embedding_generation_vectors``.
Every committed batch is resumable. A target becomes visible only after one
``BEGIN IMMEDIATE`` validation-and-pointer-switch transaction succeeds.

Usage:
    tk-migrate-embeddings --all
    tk-migrate-embeddings --status
    tk-migrate-embeddings --rollback
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from typing import Callable

from .config import SEMANTIC_AVAILABLE
from .db import get_db
from . import embeddings as _emb
from . import memory_compat as _compat


_TABLE_KIND = {"notes": "note", "dialog_messages": "dialog"}


def _count_stale(conn: sqlite3.Connection, table: str, target: str) -> int:
    """Return sources missing an exact staged vector for ``target``."""
    kind = _TABLE_KIND[table]
    report = _compat.coverage(conn, target)
    return int(report[f"{kind}s_total"]) - int(report[f"{kind}s_valid"])


def _format_state(conn: sqlite3.Connection, configured: str) -> str:
    state = _compat.generation_state(conn, configured)
    target = state["staging_generation"] or state["active_generation"]
    report = _compat.coverage(conn, str(target))
    return (
        "migration "
        f"active={state['active_generation']} "
        f"staging={state['staging_generation'] or '-'} "
        f"state={state['state']} "
        f"notes={report['notes_valid']}/{report['notes_total']} "
        f"dialog={report['dialogs_valid']}/{report['dialogs_total']} "
        f"validated={'yes' if report['complete'] else 'no'}"
    )


def _migrate_table(
    conn: sqlite3.Connection,
    *,
    table: str,
    target: str,
    batch: int,
    dry_run: bool,
    log: Callable[[str], None],
) -> tuple[int, int]:
    """Build or resume one target table without touching active vectors."""
    kind = _TABLE_KIND[table]
    total = _count_stale(conn, table, target)
    log(f"{table}: {total} source row(s) pending in target generation")
    if dry_run or total == 0:
        return total, 0
    done = 0
    started = time.time()
    for rows in _compat.pending_sources(conn, target, kind, chunk=batch):
        texts = [content for _, content in rows]
        vecs = _emb.encode_many(texts)
        if vecs is None:
            log("  semantic backend unavailable mid-run — staging remains resumable")
            break
        blobs = [vecs[i].astype("float32").tobytes() for i in range(len(rows))]
        _compat.stage_vectors(conn, target, kind, rows, blobs)
        conn.commit()
        done += len(rows)
        elapsed = max(1e-6, time.time() - started)
        rate = done / elapsed
        eta = (total - done) / rate if rate > 0 else 0.0
        log(f"  {table}: {done}/{total} ({rate:.0f}/s, eta {eta:.0f}s)")
    return total, done


def _upgrade_replay_gate(
    conn: sqlite3.Connection,
    *,
    target: str,
    log: Callable[[str], None],
) -> bool:
    """Run and retain the fixed-corpus writer+embedding upgrade gate."""
    from . import config
    from .eval import harness

    report = harness.run_eval()
    writer_generation = ":".join(
        value for value in (
            getattr(config, "WRITER_PROVIDER", "") or getattr(config, "CLIENT_LABEL", ""),
            getattr(config, "WRITER_MODEL", "") or "unknown",
            getattr(config, "WRITER_REVISION", "") or "unknown",
        )
    )
    for direction in ("old_to_new", "new_to_old"):
        _compat.record_upgrade_replay(
            conn, direction=direction, writer_generation=writer_generation,
            embedding_generation=target,
            passed=bool(report["upgrade"][direction]["passed"]),
            report=report["upgrade"][direction],
        )
    conn.commit()
    passed = bool(report["upgrade"]["passed"])
    log("upgrade_replay=" + ("pass" if passed else "fail") + " " + report["summary"])
    return passed


def run(
    *,
    do_notes: bool,
    do_dialog: bool,
    batch: int,
    dry_run: bool,
    rollback: bool = False,
    status: bool = False,
    discard: bool = False,
    log: Callable[[str], None] | None = None,
) -> int:
    if log is None:
        def log(message: str) -> None:  # noqa: E306
            print(message, file=sys.stderr, flush=True)

    conn = get_db()
    conn.row_factory = sqlite3.Row
    configured = _emb.embedding_fingerprint()
    try:
        if rollback:
            changed = _compat.rollback_active(conn, configured)
            log("rollback=" + ("activated" if changed else "unavailable"))
            log(_format_state(conn, configured))
            conn.commit()
            return 0 if changed else 1
        if discard:
            changed = _compat.discard_staging(conn, configured)
            conn.commit()
            log("staging=" + ("discarded" if changed else "absent"))
            log(_format_state(conn, configured))
            return 0
        if status:
            log(_format_state(conn, configured))
            return 0
        if not SEMANTIC_AVAILABLE:
            log("ERROR: no embedding backend available. Install `.[semantic]` "
                "(ONNX/fastembed) or `.[semantic-st]` (sentence-transformers).")
            return 1

        state = _compat.generation_state(conn, configured)
        active = str(state["active_generation"])
        target = configured
        if not dry_run:
            state = _compat.start_staging(conn, target, configured)
            conn.commit()  # staging identity survives an interrupted encode.
        log(_format_state(conn, configured) + (" (dry run)" if dry_run else ""))
        if do_notes:
            _migrate_table(conn, table="notes", target=target, batch=batch,
                           dry_run=dry_run, log=log)
        if do_dialog:
            _migrate_table(conn, table="dialog_messages", target=target, batch=batch,
                           dry_run=dry_run, log=log)

        if dry_run:
            log("activation=dry_run")
            return 0
        if target == active:
            # A legacy backfill had no distinct prior/new vector space. Its
            # active pointer was already durable, so there is nothing to flip.
            log("activation=not_needed active_generation_already_target")
            log(_format_state(conn, configured))
            return 0
        if not (do_notes and do_dialog):
            log("activation=pending (run --all to validate the complete corpus)")
            log(_format_state(conn, configured))
            return 0
        if not _upgrade_replay_gate(conn, target=target, log=log):
            log("activation=pending upgrade_replay_failed")
            return 1
        activated, report = _compat.activate_staging(conn, target, configured)
        if activated:
            log("activation=atomic_success")
            log(_format_state(conn, configured))
            return 0
        log(
            "activation=pending validation_failed "
            f"notes={report['notes_valid']}/{report['notes_total']} "
            f"dialog={report['dialogs_valid']}/{report['dialogs_total']}"
        )
        return 1
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tk-migrate-embeddings",
        description="Stage a target embedding generation, validate coverage, then "
                    "atomically activate it.",
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true",
                       help="stage notes and dialog_messages, then activate if valid")
    scope.add_argument("--notes-only", action="store_true",
                       help="stage notes only; leaves activation pending")
    scope.add_argument("--dialog-only", action="store_true",
                       help="stage dialog_messages only; leaves activation pending")
    parser.add_argument("--batch", type=int, default=256,
                        help="rows per encode/commit batch (default 256)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report target coverage without staging or activating")
    parser.add_argument("--status", action="store_true",
                        help="show active/staging generation and validation coverage")
    parser.add_argument("--rollback", action="store_true",
                        help="atomically restore the previous active generation")
    parser.add_argument("--discard-staging", action="store_true",
                        help="delete an unactivated staging generation only")
    args = parser.parse_args(argv)
    special = args.status or args.rollback or args.discard_staging
    if special and (args.all or args.notes_only or args.dialog_only or args.dry_run):
        parser.error("--status/--rollback/--discard-staging cannot be combined with a scope")
    if not special and not (args.all or args.notes_only or args.dialog_only):
        parser.error("specify a scope: --all, --notes-only, or --dialog-only")
    if args.batch < 1:
        parser.error("--batch must be positive")
    return run(
        do_notes=args.all or args.notes_only,
        do_dialog=args.all or args.dialog_only,
        batch=args.batch,
        dry_run=args.dry_run,
        status=args.status,
        rollback=args.rollback,
        discard=args.discard_staging,
    )


if __name__ == "__main__":
    raise SystemExit(main())
