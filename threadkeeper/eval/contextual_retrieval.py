"""Measure contextual retrieval on an explicitly supplied disposable corpus."""
from __future__ import annotations

import time


def measure(conn, cases: list[tuple[str, str]], *, k: int = 3) -> dict:
    from ..embeddings import embedding_fingerprint
    from ..retrieval import retrieve_notes

    original = conn.execute(
        "SELECT active_generation FROM embedding_generation_state WHERE id=1"
    ).fetchone()[0]
    reports = {}
    try:
        for label, generation in (("body", embedding_fingerprint(contextual=False)),
                                  ("context", embedding_fingerprint())):
            conn.execute("UPDATE embedding_generation_state SET active_generation=? WHERE id=1", (generation,))
            for mode in ("fts", "semantic", "hybrid"):
                found = reciprocal = leaked = output_bytes = 0
                started = time.perf_counter()
                for query, expected_id in cases:
                    hits = retrieve_notes(conn, query, k=k, mode=mode)
                    ids = [hit.id for hit in hits]
                    if expected_id in ids:
                        found += 1
                        reciprocal += 1 / (ids.index(expected_id) + 1)
                    leaked += sum(hit.project_id == "foreign-project" for hit in hits)
                    output_bytes += sum(len(hit.content.encode()) for hit in hits)
                reports[f"{label}_{mode}"] = {
                    f"recall_at_{k}": found / len(cases),
                    "mrr": reciprocal / len(cases),
                    "wrong_scope_hits": leaked,
                    "returned_bytes": output_bytes,
                    "latency_ms_per_query": 1000 * (time.perf_counter() - started) / len(cases),
                }
        reports["index"] = {
            "body_text_bytes": conn.execute("SELECT COALESCE(SUM(length(CAST(content AS BLOB))),0) FROM notes").fetchone()[0],
            "context_text_bytes": conn.execute("SELECT COALESCE(SUM(length(CAST(content AS BLOB))),0) FROM retrieval_documents WHERE memory_kind='note'").fetchone()[0],
            "context_index_bytes": conn.execute("SELECT COALESCE(SUM(pgsize),0) FROM dbstat WHERE name LIKE 'retrieval_%'").fetchone()[0],
            "body_index_bytes": conn.execute("SELECT COALESCE(SUM(pgsize),0) FROM dbstat WHERE name LIKE 'notes_fts%'").fetchone()[0],
        }
        return reports
    finally:
        conn.execute("UPDATE embedding_generation_state SET active_generation=? WHERE id=1", (original,))
        conn.commit()
