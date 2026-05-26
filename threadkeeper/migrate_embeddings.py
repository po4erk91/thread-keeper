"""One-shot migration: recompute stored embeddings with the active backend.

Every stored vector was produced by whatever embedding backend was active when
its row was written. Legacy rows (pre-ONNX) came from sentence-transformers and
carry a NULL `embed_backend` tag. fastembed/ONNX produces 384-dim vectors that
are numerically *not identical* to sentence-transformers' for the same model
(quantization + pooling detail), so after switching the default backend the
stored corpus and fresh queries drift into slightly different spaces.

This command re-encodes every stale row (those whose `embed_backend` differs
from the active backend) with the active backend, rewriting both the BLOB
column and the `vec0` mirror, and stamps the new tag. It is:

  * resumable  — only rows still tagged stale are selected, so an interrupted
                 run picks up where it left off (each batch commits);
  * idempotent — a second full run finds nothing stale and is a no-op.

Usage:
    tk-migrate-embeddings --all
    tk-migrate-embeddings --notes-only --batch 512
    tk-migrate-embeddings --dry-run
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from typing import Callable

from .config import EMBED_BACKEND, SEMANTIC_AVAILABLE
from .db import get_db
from . import embeddings as _emb


def _stale_where() -> str:
    """Predicate for rows that need recomputing under the active backend."""
    return ("embedding IS NOT NULL "
            "AND (embed_backend IS NULL OR embed_backend != ?)")


def _count_stale(conn: sqlite3.Connection, table: str, active: str) -> int:
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {_stale_where()}",
        (active,),
    ).fetchone()[0]


def _migrate_table(conn: sqlite3.Connection, *, table: str, id_col: str,
                   text_limit: int | None, active: str, batch: int,
                   dry_run: bool, log: Callable[[str], None]) -> tuple[int, int]:
    total = _count_stale(conn, table, active)
    log(f"{table}: {total} stale vector(s) to recompute")
    if dry_run or total == 0:
        return total, 0
    done = 0
    started = time.time()
    while True:
        rows = conn.execute(
            f"SELECT {id_col} AS rid, content FROM {table} "
            f"WHERE {_stale_where()} LIMIT ?",
            (active, batch),
        ).fetchall()
        if not rows:
            break
        texts = []
        for r in rows:
            t = r["content"] or ""
            texts.append(t[:text_limit] if text_limit else t)
        vecs = _emb.encode_many(texts)
        if vecs is None:
            log("  semantic backend unavailable mid-run — aborting")
            break
        for i, r in enumerate(rows):
            blob = vecs[i].astype("float32").tobytes()
            conn.execute(
                f"UPDATE {table} SET embedding=?, embed_backend=? WHERE {id_col}=?",
                (blob, active, r["rid"]),
            )
            if table == "notes":
                _emb._vec_upsert_note(conn, r["rid"], blob)
            else:
                _emb._vec_upsert_dialog(conn, r["rid"], blob)
        conn.commit()
        done += len(rows)
        elapsed = max(1e-6, time.time() - started)
        rate = done / elapsed
        eta = (total - done) / rate if rate > 0 else 0.0
        log(f"  {table}: {done}/{total}  ({rate:.0f}/s, eta {eta:.0f}s)")
    return total, done


def run(*, do_notes: bool, do_dialog: bool, batch: int, dry_run: bool,
        log: Callable[[str], None] | None = None) -> int:
    if log is None:
        def log(m: str) -> None:  # noqa: E306
            print(m, file=sys.stderr, flush=True)
    if not SEMANTIC_AVAILABLE:
        log("ERROR: no embedding backend available. Install `.[semantic]` "
            "(ONNX/fastembed) or `.[semantic-st]` (sentence-transformers).")
        return 1
    active = EMBED_BACKEND
    conn = get_db()
    conn.row_factory = sqlite3.Row
    log(f"active backend = {active}{'  (dry run)' if dry_run else ''}")
    # notes hold short text; the model caps at its own token limit, so no slice.
    if do_notes:
        _migrate_table(conn, table="notes", id_col="id", text_limit=None,
                       active=active, batch=batch, dry_run=dry_run, log=log)
    # dialog messages can be long; match the [:2000] slice used at ingest time.
    if do_dialog:
        _migrate_table(conn, table="dialog_messages", id_col="uuid",
                       text_limit=2000, active=active, batch=batch,
                       dry_run=dry_run, log=log)
    log("done.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="tk-migrate-embeddings",
        description="Recompute stored embeddings with the active backend "
                    "(THREADKEEPER_EMBED_BACKEND, default onnx).",
    )
    p.add_argument("--all", action="store_true",
                   help="recompute both notes and dialog_messages")
    p.add_argument("--notes-only", action="store_true",
                   help="recompute only notes")
    p.add_argument("--dialog-only", action="store_true",
                   help="recompute only dialog_messages")
    p.add_argument("--batch", type=int, default=256,
                   help="rows per encode/commit batch (default 256)")
    p.add_argument("--dry-run", action="store_true",
                   help="report stale counts without writing")
    args = p.parse_args(argv)

    do_notes = args.all or args.notes_only
    do_dialog = args.all or args.dialog_only
    if not (do_notes or do_dialog):
        p.error("specify a scope: --all, --notes-only, or --dialog-only")
    return run(do_notes=do_notes, do_dialog=do_dialog,
               batch=args.batch, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
