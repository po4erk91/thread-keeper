#!/usr/bin/env python3
"""Memory-quality eval harness (issue #71).

Measures whether thread-keeper's retrieval surface — the real ``search()``,
``dialog_search()`` and ``brief()`` tools — actually recalls the facts the
agent needs, and (the high-payoff axis) whether it *abstains* on events that
never happened instead of surfacing a fabricated or stale answer. Modeled on
LongMemEval (ICLR 2025) + mem0's 2026 tokens-per-retrieval cost axis.

It reports, over a fixed ground-truth set:
  • accuracy            — fraction of questions whose retrieval recalled the
                          gold fact (or, for abstention questions, did NOT
                          surface the fabricated claim)
  • per-type accuracy   — the five LongMemEval axes (information extraction,
                          multi-session reasoning, temporal reasoning,
                          knowledge updates, abstention)
  • abstention rate     — of the never-happened questions, the fraction the
                          system correctly refused (did not leak a trap fact)
  • tokens-per-retrieval — mean / median / max tokens of what each query
                          returned, so recall is never read apart from cost
  • retrieval latency   — mean / p50 / p95 / max wall-clock milliseconds
  • forgetting lifecycle — a full-store mutation sequence with explicit
                         negative credit for obsolete evidence

The default judge is **lexical** (deterministic, offline, no API key, no
embeddings) so a single command is reproducible and CI-safe. An optional
``--judge llm`` backend grades answer correctness with an Anthropic model
(via urllib, no SDK dependency) when ``ANTHROPIC_API_KEY`` is set — useful
once temporal/knowledge-update *reasoning* (not just retrieval recall) becomes
the optimization target for #27/#28.

Usage (from the repo root, with the project venv):

    .venv/bin/python scripts/memory_eval/run.py                # bundled demo corpus
    .venv/bin/python scripts/memory_eval/run.py --json         # machine-readable
    .venv/bin/python scripts/memory_eval/run.py --db snap.sqlite \
        --ground-truth my_labels.json                          # real snapshot
    .venv/bin/python scripts/memory_eval/run.py --semantic     # use embeddings if installed
    .venv/bin/python scripts/memory_eval/run.py --judge llm     # LLM-graded (needs key)

``--db`` runs READ-ONLY: the snapshot is copied to a throwaway temp file and
the original is never opened for writing. With no ``--db`` the harness builds
the bundled demo corpus (scripts/memory_eval/ground_truth.json) into a fresh
temp DB, so the command is fully self-contained.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
# A developer may have another thread-keeper checkout on PYTHONPATH. The eval
# must always exercise the source tree that owns this fixture and its tests.
sys.path.insert(0, str(REPO_ROOT))
DEFAULT_GROUND_TRUTH = HERE / "ground_truth.json"
DEFAULT_LIFECYCLE_FIXTURE = HERE / "lifecycle_fixture.json"

# The five LongMemEval question axes, in report order.
AXES = [
    "information_extraction",
    "multi_session_reasoning",
    "temporal_reasoning",
    "knowledge_update",
    "abstention",
]

# A deterministic, dependency-free token proxy: words + standalone
# punctuation. Within ~25% of a BPE count for English/code, and stable
# across machines (no tiktoken/model download). Documented as an estimate.
_TOK_RE = re.compile(r"\w+|[^\w\s]")


def estimate_tokens(text: str) -> int:
    """Rough token count of a retrieved context blob (see _TOK_RE)."""
    if not text:
        return 0
    return len(_TOK_RE.findall(text))


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile, deterministic for small eval sets."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction + 0.999) - 1))
    return ordered[index]


# ──────────────────────────────────────────────────────────────────────────
# Environment + package import
# ──────────────────────────────────────────────────────────────────────────
def _prepare_env(db_path: Path, semantic: bool) -> None:
    """Point thread-keeper at our DB and silence every background daemon.

    Must run BEFORE importing threadkeeper.* — config captures these at
    import time (DB_PATH, SEMANTIC_AVAILABLE)."""
    os.environ["THREADKEEPER_DB"] = str(db_path)
    proj = db_path.parent / "fake_projects"
    proj.mkdir(parents=True, exist_ok=True)
    os.environ["CLAUDE_PROJECTS_DIR"] = str(proj)
    if semantic:
        os.environ.pop("THREADKEEPER_NO_EMBEDDINGS", None)
    else:
        os.environ["THREADKEEPER_NO_EMBEDDINGS"] = "1"
    # Hard-disable all background work: this is a read-only measurement, not a
    # live session. Mirrors tests/conftest.py's clean-env block.
    os.environ["THREADKEEPER_DISABLE_BG_DAEMONS"] = "1"
    for knob in (
        "THREADKEEPER_AUTO_UPDATE_INTERVAL_S",
        "THREADKEEPER_INGEST_INTERVAL_S",
        "THREADKEEPER_INGEST_CAP",
        "THREADKEEPER_SKILL_WATCH_INTERVAL_S",
        "THREADKEEPER_SHADOW_REVIEW_INTERVAL_S",
        "THREADKEEPER_CURATOR_INTERVAL_S",
        "THREADKEEPER_EXTRACT_INTERVAL_S",
        "THREADKEEPER_PROBE_INTERVAL_S",
        "THREADKEEPER_THREAD_JANITOR_INTERVAL_S",
        "THREADKEEPER_CONFIG_WATCH_INTERVAL_S",
        "THREADKEEPER_SEARCH_PROXY_POLL_S",
    ):
        os.environ[knob] = "0"
    os.environ.setdefault("THREADKEEPER_CLIENT", "memory-eval")


def _import_threadkeeper():
    """Import after _prepare_env. Returns the handful of modules we touch."""
    import threadkeeper.server  # noqa: F401  (registers tools + inits schema)
    from threadkeeper import db, config, brief as brief_mod, embeddings, identity
    from threadkeeper.tools import (
        consolidate as consolidate_tool,
        dialog as dialog_tool,
        dialectic as dialectic_tool,
        forget as forget_tool,
        lessons as lessons_tool,
        style as style_tool,
        threads as threads_tool,
    )
    return {
        "db": db,
        "config": config,
        "brief": brief_mod,
        "embeddings": embeddings,
        "identity": identity,
        "open_thread": threads_tool.open_thread,
        "note": threads_tool.note,
        "brief_tool": threads_tool.brief,
        "dialog_search": dialog_tool.dialog_search,
        "search": threads_tool.search,
        "verbatim_user": style_tool.verbatim_user,
        "dialectic_claim": dialectic_tool.dialectic_claim,
        "dialectic_evidence": dialectic_tool.dialectic_evidence,
        "dialectic_supersede": dialectic_tool.dialectic_supersede,
        "dialectic_synthesis": dialectic_tool.dialectic_synthesis,
        "lesson_append": lessons_tool.lesson_append,
        "lesson_get": lessons_tool.lesson_get,
        "consolidate": consolidate_tool.consolidate,
        "forget": forget_tool.forget,
    }


# ──────────────────────────────────────────────────────────────────────────
# Corpus seeding (demo fixture → fresh DB)
# ──────────────────────────────────────────────────────────────────────────
def seed_corpus(conn, corpus: dict, now: int | None = None) -> None:
    """Insert the demo corpus into a fresh DB the way ingest()/note() would.

    dialog_messages are mirrored into dialog_fts by the dialog_fts_ai AFTER
    INSERT trigger (schema v2: external-content FTS5, no manual mirror).
    notes rely on the notes_fts AFTER INSERT trigger defined in the schema."""
    now = now if now is not None else int(time.time())
    for t in corpus.get("threads", []):
        conn.execute(
            "INSERT OR IGNORE INTO threads "
            "(id, question, state, opened_at, last_touched_at) "
            "VALUES (?, ?, 'active', ?, ?)",
            (t["id"], t["question"], now, now),
        )
    for m in corpus.get("dialog_messages", []):
        ts = now + int(m.get("day_offset", 0)) * 86400
        conn.execute(
            "INSERT OR IGNORE INTO dialog_messages "
            "(uuid, source, project, session_id, role, content, model, created_at) "
            "VALUES (?, 'claude-code', 'memory-eval', ?, ?, ?, 'demo', ?)",
            (m["uuid"], m.get("session_id"), m["role"], m["content"], ts),
        )
    for n in corpus.get("notes", []):
        ts = now + int(n.get("day_offset", 0)) * 86400
        conn.execute(
            "INSERT INTO notes (thread_id, content, kind, created_at, session_id) "
            "VALUES (?, ?, ?, ?, 'memory-eval')",
            (n.get("thread_id"), n["content"], n.get("kind", "insight"), ts),
        )
    conn.commit()


def seed_embeddings(tk: dict) -> None:
    """Give the demo corpus full current-generation vector coverage.

    Encoding happens before the write transaction, matching production's
    transaction contract. The semantic eval therefore exercises dense+FTS
    fusion rather than merely proving the empty-index FTS fallback.
    """
    with tk["db"].read_db() as conn:
        note_rows = conn.execute(
            "SELECT id, content FROM notes ORDER BY id"
        ).fetchall()
        dialog_rows = conn.execute(
            "SELECT uuid, content FROM dialog_messages ORDER BY rowid"
        ).fetchall()
    texts = [row["content"] for row in note_rows]
    texts.extend(row["content"] for row in dialog_rows)
    vectors = tk["embeddings"].encode_many(texts)
    if vectors is None:
        return
    blobs = [vector.astype("float32").tobytes() for vector in vectors]
    note_blobs = blobs[:len(note_rows)]
    dialog_blobs = blobs[len(note_rows):]
    tag = tk["embeddings"].embedding_fingerprint()

    def _write(conn) -> None:
        for row, blob in zip(note_rows, note_blobs):
            conn.execute(
                "UPDATE notes SET embedding=?, embed_backend=? WHERE id=?",
                (blob, tag, row["id"]),
            )
            tk["embeddings"]._vec_upsert_note(conn, row["id"], blob)
        for row, blob in zip(dialog_rows, dialog_blobs):
            conn.execute(
                "UPDATE dialog_messages SET embedding=?, embed_backend=? "
                "WHERE uuid=?",
                (blob, tag, row["uuid"]),
            )
            tk["embeddings"]._vec_upsert_dialog(conn, row["uuid"], blob)

    tk["db"].run_write("memory-eval-seed-embeddings", _write)


# ──────────────────────────────────────────────────────────────────────────
# Retrieval — call the REAL systems-under-test
# ──────────────────────────────────────────────────────────────────────────
def retrieve(tk: dict, item: dict) -> str:
    """Issue the question's query to its system-under-test, return raw output.

    The returned string is exactly what a downstream agent would receive, so
    tokens-per-retrieval is measured on it and the judge reads it verbatim."""
    system = item.get("system", "dialog_search")
    query = item.get("query") or item.get("question", "")
    k = int(item.get("k", 5))
    if system == "search":
        return tk["search"](query=query, k=k)
    if system == "brief":
        conn = tk["db"].get_db()
        return tk["brief"].render_brief(conn, query=query, k=k)
    # default: dialog_search
    return tk["dialog_search"](query=query, k=k, mode=item.get("mode", "hybrid"))


# ──────────────────────────────────────────────────────────────────────────
# Forgetting lifecycle fixture — real writes followed by real reads
# ──────────────────────────────────────────────────────────────────────────
_LIFECYCLE_PATHS = {
    "thread_notes": "search",
    "verbatim_memory": "brief",
    "dialectic_claims": "dialectic_synthesis",
    "lessons_or_skills": "lesson_get",
}


def _require_write(result: str, operation: str) -> str:
    """Return a successful tool result or fail the fixture at its write."""
    if result.startswith("ERR") or result.startswith("error"):
        raise RuntimeError(f"lifecycle {operation} failed: {result}")
    return result


def _result_value(result: str, key: str) -> str:
    match = re.search(rf"\b{re.escape(key)}=([^\s]+)", result)
    if not match:
        raise RuntimeError(f"lifecycle result missing {key}: {result}")
    return match.group(1)


def _start_lifecycle_session(tk: dict) -> str:
    """Start a distinct real session so forgetting can target its source."""
    identity = tk["identity"]
    identity._session_id = None
    identity._session_start = None
    return identity.ensure_session_started(client="memory-eval")


def _write_fact(tk: dict, fixture: dict, thread_id: str, fact: str,
                source: str) -> None:
    """Write one assertion through the normal note, verbatim, and lesson tools."""
    _require_write(tk["note"](thread_id, fact, kind="insight"), "note")
    _require_write(tk["verbatim_user"](fact, thread_id), "verbatim_user")
    _require_write(
        tk["lesson_append"](
            title=fixture["lesson_title"],
            body=fact,
            summary="Current lifecycle fixture value.",
            source=source,
        ),
        "lesson_append",
    )


def _add_evidence(tk: dict, claim_id: str, fact: str, source: str) -> None:
    _require_write(
        tk["dialectic_evidence"](
            claim_id=claim_id,
            quote=fact,
            source=f"session:{source}",
        ),
        "dialectic_evidence",
    )


def _supersede(tk: dict, claim_id: str, fact: str, domain: str) -> str:
    result = asyncio.run(
        tk["dialectic_supersede"](
            old_claim_id=claim_id,
            new_claim=fact,
            domain=domain,
            quote=fact,
        )
    )
    return _result_value(_require_write(result, "dialectic_supersede"), "new")


def lifecycle_score(context: str, current_fact: str,
                    obsolete_facts: list[str]) -> dict:
    """Score one retrieval path, penalizing obsolete evidence explicitly."""
    lowered = context.lower()
    current_recalled = current_fact.lower() in lowered
    obsolete_retrieved = [
        fact for fact in obsolete_facts if fact.lower() in lowered
    ]
    retrieval_credit = 1.0 if current_recalled else 0.0
    negative_credit = -0.5 * len(obsolete_retrieved)
    return {
        "current_recalled": current_recalled,
        "obsolete_retrieved": obsolete_retrieved,
        "retrieval_credit": retrieval_credit,
        "negative_credit": negative_credit,
        "score": round(retrieval_credit + negative_credit, 3),
    }


def _read_lifecycle_surfaces(tk: dict, fixture: dict) -> dict[str, str]:
    """Read every user-facing retrieval path used by the lifecycle score."""
    query = fixture["query"]
    return {
        "brief": tk["brief_tool"](query=query, k=12),
        "search": tk["search"](query=query, k=12),
        "dialectic_synthesis": tk["dialectic_synthesis"](
            domain=fixture["domain"], include_history=True
        ),
        "lesson_get": tk["lesson_get"](
            fixture["lesson_title"].lower().replace(" ", "-")
        ),
    }


def _capture_lifecycle_stage(stages: list[dict], fixture: dict, name: str,
                             fact_index: int, expect_clean: bool,
                             surfaces: dict[str, str]) -> None:
    facts = fixture["facts"]
    current_fact = facts[fact_index]
    obsolete_facts = facts[:fact_index]
    rows = []
    for memory_class, path in _LIFECYCLE_PATHS.items():
        row = lifecycle_score(surfaces[path], current_fact, obsolete_facts)
        row.update({
            "stage": name,
            "memory_class": memory_class,
            "retrieval_path": path,
            "expect_clean": expect_clean,
        })
        rows.append(row)
    stages.append({
        "name": name,
        "current_fact": current_fact,
        "expect_clean": expect_clean,
        "retrieval_paths": list(surfaces),
        "score": round(statistics.fmean(row["score"] for row in rows), 3),
        "rows": rows,
    })


def _restart_lifecycle_surfaces(db_path: Path, fixture_path: Path,
                                semantic: bool) -> dict[str, str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--lifecycle-read",
        str(db_path),
        "--lifecycle-fixture",
        str(fixture_path),
    ]
    if semantic:
        command.append("--semantic")
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=180,
        env=os.environ.copy(),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"lifecycle restart failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return json.loads(proc.stdout)["surfaces"]


def evaluate_forgetting_lifecycle(tk: dict, fixture: dict, *, db_path: Path,
                                  fixture_path: Path, semantic: bool) -> dict:
    """Exercise corrections, supersession, consolidation, erasure, and restart.

    The fixture writes through the same tools an agent uses. It intentionally
    scores intermediate stale reads below one, then requires a clean score
    after source forgetting and in a fresh process.
    """
    facts = fixture["facts"]
    if len(facts) != 3:
        raise ValueError("lifecycle fixture requires exactly three fact versions")
    stages: list[dict] = []

    erased_source = _start_lifecycle_session(tk)
    thread_id = _require_write(
        tk["open_thread"]("Track the lifecycle subscription plan"), "open_thread"
    )
    _write_fact(tk, fixture, thread_id, facts[0], erased_source)
    first_claim = _result_value(
        _require_write(
            tk["dialectic_claim"](facts[0], domain=fixture["domain"]),
            "dialectic_claim",
        ),
        "id",
    )
    _add_evidence(tk, first_claim, facts[0], erased_source)
    _add_evidence(tk, first_claim, facts[0], erased_source)
    _capture_lifecycle_stage(
        stages, fixture, "assert", 0, False, _read_lifecycle_surfaces(tk, fixture)
    )

    _write_fact(tk, fixture, thread_id, facts[1], erased_source)
    _require_write(tk["verbatim_user"](facts[1], thread_id), "verbatim_duplicate")
    _capture_lifecycle_stage(
        stages, fixture, "correction_1", 1, False,
        _read_lifecycle_surfaces(tk, fixture),
    )
    second_claim = _supersede(tk, first_claim, facts[1], fixture["domain"])
    _add_evidence(tk, second_claim, facts[1], erased_source)
    _capture_lifecycle_stage(
        stages, fixture, "supersession_1", 1, False,
        _read_lifecycle_surfaces(tk, fixture),
    )

    current_source = _start_lifecycle_session(tk)
    _write_fact(tk, fixture, thread_id, facts[2], current_source)
    _capture_lifecycle_stage(
        stages, fixture, "correction_2", 2, False,
        _read_lifecycle_surfaces(tk, fixture),
    )
    third_claim = _supersede(tk, second_claim, facts[2], fixture["domain"])
    _add_evidence(tk, third_claim, facts[2], current_source)
    _add_evidence(tk, third_claim, facts[2], current_source)
    _capture_lifecycle_stage(
        stages, fixture, "supersession_2", 2, False,
        _read_lifecycle_surfaces(tk, fixture),
    )

    consolidation = _require_write(
        tk["consolidate"](dry_run=False), "consolidate"
    )
    if "dedupe=1" not in consolidation:
        raise RuntimeError(f"lifecycle consolidation did not dedupe: {consolidation}")
    _capture_lifecycle_stage(
        stages, fixture, "consolidation", 2, False,
        _read_lifecycle_surfaces(tk, fixture),
    )

    forgotten = _require_write(
        tk["forget"](
            selector=erased_source,
            selector_type="session_id",
            dry_run=False,
        ),
        "forget",
    )
    if "mode=applied" not in forgotten:
        raise RuntimeError(f"lifecycle forgetting did not apply: {forgotten}")
    _capture_lifecycle_stage(
        stages, fixture, "source_forgetting", 2, True,
        _read_lifecycle_surfaces(tk, fixture),
    )
    restart_surfaces = _restart_lifecycle_surfaces(db_path, fixture_path, semantic)
    _capture_lifecycle_stage(
        stages, fixture, "restart", 2, True, restart_surfaces
    )

    backend = "hybrid" if tk["config"].SEMANTIC_AVAILABLE else "fts"
    threshold = float(fixture["release_thresholds"][backend])
    rows = [row for stage in stages for row in stage["rows"]]
    failures = [
        row for row in rows
        if row["expect_clean"] and row["score"] < threshold
    ]
    return {
        "fixture": fixture["id"],
        "backend": backend,
        "release_threshold": threshold,
        "negative_credit_per_obsolete_fact": -0.5,
        "score": round(
            statistics.fmean(row["score"] for row in rows if row["expect_clean"]),
            3,
        ),
        "verdict": "PASS" if not failures else "FAIL",
        "failures": failures,
        "stages": stages,
    }


# ──────────────────────────────────────────────────────────────────────────
# Judges
# ──────────────────────────────────────────────────────────────────────────
_NO_HIT_MARKERS = ("no_matches", "no_idle", "fts_error")


def judge_lexical(item: dict, ctx: str) -> tuple[bool, str]:
    """Deterministic substring judge.

    Normal question  → correct if the gold fact was surfaced (recall).
    Abstention (`abstain`) → correct only when no fabricated trap leaked AND
    retrieval either returned no candidates or surfaced labeled counterevidence.
    Unrelated nearest neighbours do not count as abstention.
    """
    low = ctx.lower()
    if item.get("abstain"):
        leaked = [t for t in item.get("trap_substrings", []) if t.lower() in low]
        if leaked:
            return False, f"leaked trap: {leaked[0]!r}"
        evidence = [
            value for value in item.get("abstain_evidence", [])
            if value.lower() in low
        ]
        if evidence:
            return True, f"counterevidence recalled: {evidence[0]!r}"
        if any(marker in low for marker in _NO_HIT_MARKERS):
            return True, "abstained (no candidates)"
        return False, "unrelated candidates surfaced instead of abstaining"
    gold_all = item.get("gold_all")
    if gold_all:
        missing = [g for g in gold_all if g.lower() not in low]
        if missing:
            return False, f"missing {missing!r}"
        return True, "all gold facts recalled"
    gold_any = item.get("gold_any", [])
    hit = next((g for g in gold_any if g.lower() in low), None)
    if hit:
        return True, f"recalled {hit!r}"
    return False, f"none of {gold_any!r} recalled"


class LLMJudgeUnavailable(RuntimeError):
    pass


def judge_llm(item: dict, ctx: str, *, model: str, api_key: str,
              timeout: float = 60.0) -> tuple[bool, str]:
    """Grade answer correctness with an Anthropic model over plain urllib.

    No SDK dependency: POSTs the Messages API and parses a strict-JSON verdict.
    Falls back to the lexical judge's contract (bool, reason)."""
    gold = item.get("gold_all") or item.get("gold_any") or []
    if item.get("abstain"):
        rubric = (
            "This question asks about an event that NEVER happened. The "
            "correct behavior is to ABSTAIN: the context must not support any "
            "specific factual claim answering it. Mark correct=true if the "
            "context does not contain a confident answer, false if it does."
        )
    else:
        rubric = (
            f"The correct answer should mention one of: {gold}. Mark "
            "correct=true only if the retrieved context actually supports that "
            "answer, false otherwise."
        )
    prompt = (
        "You grade a memory system's retrieval. Reply with ONLY a JSON object "
        '{"correct": bool, "reason": "<=12 words}.\n\n'
        f"QUESTION: {item.get('question', item.get('query', ''))}\n\n"
        f"RUBRIC: {rubric}\n\n"
        f"RETRIEVED CONTEXT:\n{ctx[:4000]}\n"
    )
    body = json.dumps({
        "model": model,
        "max_tokens": 128,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except Exception as e:  # noqa: BLE001 — surface any transport/API error
        raise LLMJudgeUnavailable(f"anthropic request failed: {e}") from e
    text = "".join(
        b.get("text", "") for b in payload.get("content", [])
        if b.get("type") == "text"
    )
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return False, f"unparseable verdict: {text[:60]!r}"
    try:
        verdict = json.loads(m.group(0))
    except json.JSONDecodeError:
        return False, f"unparseable verdict: {text[:60]!r}"
    return bool(verdict.get("correct")), str(verdict.get("reason", ""))[:80]


# ──────────────────────────────────────────────────────────────────────────
# Eval driver
# ──────────────────────────────────────────────────────────────────────────
def evaluate(tk: dict, ground_truth: dict, *, judge: str = "lexical",
             llm_model: str = "claude-haiku-4-5-20251001") -> dict:
    """Run every question, judge it, and aggregate the report dict."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if judge == "llm" and not api_key:
        raise LLMJudgeUnavailable(
            "ANTHROPIC_API_KEY not set; rerun with --judge lexical (default)."
        )
    backend = "hybrid" if tk["config"].SEMANTIC_AVAILABLE else "fts"
    rows: list[dict] = []
    for item in ground_truth["questions"]:
        started = time.perf_counter()
        ctx = retrieve(tk, item)
        latency_ms = (time.perf_counter() - started) * 1000.0
        tokens = estimate_tokens(ctx)
        if judge == "llm":
            correct, reason = judge_llm(
                item, ctx, model=llm_model, api_key=api_key)
        else:
            correct, reason = judge_lexical(item, ctx)
        rows.append({
            "id": item["id"],
            "type": item.get("type", "information_extraction"),
            "system": item.get("system", "dialog_search"),
            "abstain": bool(item.get("abstain")),
            "correct": correct,
            "reason": reason,
            "tokens": tokens,
            "latency_ms": round(latency_ms, 3),
            "no_hit": any(mk in ctx for mk in _NO_HIT_MARKERS),
        })

    total = len(rows)
    correct = sum(r["correct"] for r in rows)
    per_type: dict[str, dict] = {}
    for ax in AXES:
        sub = [r for r in rows if r["type"] == ax]
        if sub:
            per_type[ax] = {
                "n": len(sub),
                "correct": sum(r["correct"] for r in sub),
                "accuracy": round(sum(r["correct"] for r in sub) / len(sub), 4),
            }
    abst = [r for r in rows if r["abstain"]]
    toks = [r["tokens"] for r in rows]
    latencies = [r["latency_ms"] for r in rows]
    with tk["db"].read_db() as conn:
        index_health = tk["embeddings"].embedding_index_health(conn)
    return {
        "backend": backend,
        "judge": judge,
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 4) if total else 0.0,
        "per_type": per_type,
        "abstention": {
            "n": len(abst),
            "correct": sum(r["correct"] for r in abst),
            "rate": round(sum(r["correct"] for r in abst) / len(abst), 4)
            if abst else None,
        },
        "tokens_per_retrieval": {
            "mean": round(statistics.fmean(toks), 1) if toks else 0.0,
            "median": int(statistics.median(toks)) if toks else 0,
            "max": max(toks) if toks else 0,
            "total": sum(toks),
        },
        "retrieval_latency_ms": {
            "mean": round(statistics.fmean(latencies), 3) if latencies else 0.0,
            "p50": round(_percentile(latencies, 0.50), 3),
            "p95": round(_percentile(latencies, 0.95), 3),
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
        "embedding_index": index_health,
        "rows": rows,
    }


def format_report(report: dict) -> str:
    """Human-readable summary (the default stdout)."""
    out: list[str] = []
    out.append("── memory-quality eval ──────────────────────────────────")
    out.append(
        f"backend={report['backend']}  judge={report['judge']}  "
        f"questions={report['total']}"
    )
    out.append(
        f"accuracy           : {report['accuracy']:.1%}  "
        f"({report['correct']}/{report['total']})"
    )
    ab = report["abstention"]
    if ab["rate"] is not None:
        out.append(
            f"abstention rate    : {ab['rate']:.1%}  "
            f"({ab['correct']}/{ab['n']} never-happened questions refused)"
        )
    tpr = report["tokens_per_retrieval"]
    out.append(
        f"tokens/retrieval   : mean={tpr['mean']}  median={tpr['median']}  "
        f"max={tpr['max']}  total={tpr['total']}"
    )
    lat = report["retrieval_latency_ms"]
    out.append(
        f"latency (ms)       : mean={lat['mean']}  p50={lat['p50']}  "
        f"p95={lat['p95']}  max={lat['max']}"
    )
    out.append("")
    out.append("per-axis accuracy:")
    for ax, st in report["per_type"].items():
        out.append(f"  {ax:<24} {st['accuracy']:.1%}  ({st['correct']}/{st['n']})")
    lifecycle = report.get("forgetting_lifecycle")
    if lifecycle:
        out.append("")
        out.append(
            "forgetting lifecycle: "
            f"{lifecycle['verdict']} score={lifecycle['score']:.1%} "
            f"threshold={lifecycle['release_threshold']:.1%} "
            f"backend={lifecycle['backend']}"
        )
        if lifecycle["failures"]:
            for failure in lifecycle["failures"]:
                out.append(
                    f"  ✗ {failure['stage']} [{failure['memory_class']}] "
                    f"obsolete={len(failure['obsolete_retrieved'])}"
                )
    fails = [r for r in report["rows"] if not r["correct"]]
    if fails:
        out.append("")
        out.append(f"failures ({len(fails)}):")
        for r in fails:
            out.append(f"  ✗ {r['id']:<20} [{r['type']}] {r['reason']}")
    out.append("─────────────────────────────────────────────────────────")
    return "\n".join(out)


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────
def _prepare_lifecycle_storage(db_path: Path) -> None:
    """Keep the mutation fixture's file-backed lesson store inside its temp DB."""
    os.environ["THREADKEEPER_LESSONS"] = str(
        db_path.parent / "memory-eval-lifecycle-lessons.md"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Memory-quality eval harness (LongMemEval-style).")
    ap.add_argument("--db", type=Path, default=None,
                    help="snapshot DB to evaluate (copied to temp; read-only). "
                         "Omit to build the bundled demo corpus.")
    ap.add_argument("--ground-truth", type=Path, default=DEFAULT_GROUND_TRUTH,
                    help="ground-truth JSON (default: bundled demo set).")
    ap.add_argument("--judge", choices=("lexical", "llm"), default="lexical",
                    help="lexical (default, offline) or llm (needs ANTHROPIC_API_KEY).")
    ap.add_argument("--llm-model", default="claude-haiku-4-5-20251001",
                    help="model id for --judge llm.")
    ap.add_argument("--semantic", action="store_true",
                    help="use semantic embeddings if installed (default: FTS only).")
    ap.add_argument("--json", action="store_true",
                    help="emit the full report as JSON instead of a table.")
    ap.add_argument("--lifecycle-fixture", type=Path,
                    default=DEFAULT_LIFECYCLE_FIXTURE,
                    help="frozen full-store mutation fixture.")
    ap.add_argument("--lifecycle-read", type=Path, metavar="DB",
                    help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.lifecycle_read:
        fixture = json.loads(args.lifecycle_fixture.read_text())
        _prepare_lifecycle_storage(args.lifecycle_read)
        _prepare_env(args.lifecycle_read, semantic=args.semantic)
        tk = _import_threadkeeper()
        print(json.dumps({"surfaces": _read_lifecycle_surfaces(tk, fixture)}))
        return 0

    gt = json.loads(Path(args.ground_truth).read_text())

    tmpdir = Path(tempfile.mkdtemp(prefix="tk-memeval-"))
    try:
        if args.db:
            src = Path(args.db).expanduser()
            if not src.exists():
                print(f"ERR: snapshot not found: {src}", file=sys.stderr)
                return 2
            # Copy so the user's real DB is never opened for writing.
            db_path = tmpdir / "snapshot.sqlite"
            shutil.copy2(src, db_path)
            seed = False
        else:
            db_path = tmpdir / "demo.sqlite"
            seed = True

        _prepare_lifecycle_storage(db_path)
        _prepare_env(db_path, semantic=args.semantic)
        tk = _import_threadkeeper()
        if seed:
            seed_conn = tk["db"].get_db()
            try:
                seed_corpus(seed_conn, gt["corpus"])
            finally:
                seed_conn.close()
            if args.semantic:
                seed_embeddings(tk)

        try:
            report = evaluate(tk, gt, judge=args.judge, llm_model=args.llm_model)
        except LLMJudgeUnavailable as e:
            print(f"ERR: {e}", file=sys.stderr)
            return 3

        fixture = json.loads(args.lifecycle_fixture.read_text())
        report["forgetting_lifecycle"] = evaluate_forgetting_lifecycle(
            tk,
            fixture,
            db_path=db_path,
            fixture_path=args.lifecycle_fixture,
            semantic=args.semantic,
        )

        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(format_report(report))
        return 0 if report["forgetting_lifecycle"]["verdict"] == "PASS" else 4
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
