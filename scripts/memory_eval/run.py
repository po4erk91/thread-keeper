#!/usr/bin/env python3
"""Memory-quality eval harness (issues #71 and #347).

Measures the two contracts an agent needs from memory: evidence retrieval and
the final answer deterministically replayed from that evidence. The systems
under test remain the real ``search()``, ``dialog_search()`` and ``brief()``
tools; the response replayer is deliberately local and rule-based so CI can
separate a retrieval regression from a reasoning regression without an API
call or a model-version change.

It reports, over a fixed ground-truth set:
  • evidence recall     — whether the right supporting evidence was retrieved
  • final-answer correctness — whether the replayed answer is correct
  • per-type outcomes   — LongMemEval's five axes plus dynamic state,
                          workflow recall, premise awareness, and composed asks
  • abstention rate     — of the never-happened questions, the fraction the
                          system correctly refused (did not leak a trap fact)
  • tokens-per-retrieval — mean / median / max tokens of what each query
                          returned, so recall is never read apart from cost
  • retrieval latency   — mean / p50 / p95 / max wall-clock milliseconds

The default judge is **lexical** (deterministic, offline, no API key, no
embeddings) so a single command is reproducible and CI-safe. ``--matrix``
runs the public fixture across FTS/hybrid retrieval, baseline/retained/curated
state, and all personal-memory egress policies. Private holdouts use
``--private-holdout`` to suppress per-case output.

Usage (from the repo root, with the project venv):

    .venv/bin/python scripts/memory_eval/run.py                # bundled demo corpus
    .venv/bin/python scripts/memory_eval/run.py --json         # machine-readable
    .venv/bin/python scripts/memory_eval/run.py --db snap.sqlite \
        --ground-truth my_labels.json                          # real snapshot
    .venv/bin/python scripts/memory_eval/run.py --semantic     # use embeddings if installed
    .venv/bin/python scripts/memory_eval/run.py --matrix       # migration gate matrix
    age -d holdout.json.age | .venv/bin/python scripts/memory_eval/run.py \
        --ground-truth /dev/stdin --private-holdout
    .venv/bin/python scripts/memory_eval/run.py --judge llm     # LLM-graded (needs key)

``--db`` runs READ-ONLY: the snapshot is copied to a throwaway temp file and
the original is never opened for writing. With no ``--db`` the harness builds
the bundled demo corpus (scripts/memory_eval/ground_truth.json) into a fresh
temp DB, so the command is fully self-contained.
"""
from __future__ import annotations

import argparse
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
DEFAULT_GROUND_TRUTH = HERE / "ground_truth.json"

# Subprocess runs set ``sys.path[0]`` to ``scripts/memory_eval`` rather than
# the checkout root.  Prefer this checkout over any unrelated editable install
# so the harness always evaluates the code beside its fixture.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The LongMemEval axes plus ThreadKeeper-specific outcome checks, in report
# order.  Keep this explicit: fixture additions are a corpus-version change,
# not an accidental consequence of whatever labels happen to be present.
AXES = [
    "information_extraction",
    "multi_session_reasoning",
    "temporal_reasoning",
    "knowledge_update",
    "abstention",
    "dynamic_state",
    "workflow_recall",
    "premise_awareness",
    "implicit_composed_request",
]

MATRIX_STATES = ("baseline", "retained", "curated")
MATRIX_POLICIES = ("all", "same-vendor", "work-only")
MATRIX_BACKENDS = ("fts", "hybrid")
_REPLAY_ABSTENTION = "I cannot confirm that from the available memory."

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
    from threadkeeper import db, config, brief as brief_mod, embeddings
    from threadkeeper.tools import dialog as dialog_tool, threads as threads_tool
    return {
        "db": db,
        "config": config,
        "brief": brief_mod,
        "embeddings": embeddings,
        "dialog_search": dialog_tool.dialog_search,
        "search": threads_tool.search,
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
    for v in corpus.get("verbatim", []):
        ts = now + int(v.get("day_offset", 0)) * 86400
        conn.execute(
            "INSERT INTO verbatim (speaker, content, thread_id, created_at, "
            "session_id) VALUES ('user', ?, ?, ?, 'memory-eval')",
            (v["content"], v.get("thread_id"), ts),
        )
    conn.commit()


def apply_fixture_state(conn, corpus: dict, state: str) -> None:
    """Apply a deterministic post-retention/curation fixture state.

    These are deliberately data-level snapshots, not calls to asynchronous
    daemons.  That keeps the migration gate reproducible while still querying
    the actual FTS triggers and retrieval tools after evidence was removed.
    A state can remove only public synthetic rows named in the fixture.
    """
    if state == "baseline":
        return
    states = corpus.get("matrix", {}).get("states", {})
    transform = states.get(state)
    if transform is None:
        raise ValueError(f"fixture does not define state {state!r}")
    for uuid in transform.get("remove_dialog_messages", []):
        conn.execute("DELETE FROM dialog_messages WHERE uuid=?", (uuid,))
    for content in transform.get("remove_notes", []):
        conn.execute("DELETE FROM notes WHERE content=?", (content,))
    for content in transform.get("remove_verbatim", []):
        conn.execute("DELETE FROM verbatim WHERE content=?", (content,))
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
        return tk["brief"].render_brief(
            conn, query=query, k=k, consumer_cli=item.get("consumer_cli"))
    # default: dialog_search
    return tk["dialog_search"](query=query, k=k, mode=item.get("mode", "hybrid"))


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


def _gold_terms(item: dict) -> list[str]:
    """Return the fixture's positive evidence terms in answer order."""
    return list(item.get("gold_all") or item.get("gold_any") or [])


def _terms_found(terms: list[str], text: str) -> list[str]:
    low = text.lower()
    return [term for term in terms if term.lower() in low]


def expects_abstention(item: dict, *, state: str, egress_policy: str) -> bool:
    """Resolve whether this matrix cell must refuse the question.

    A normal never-happened request is always an abstention.  Removed fixture
    evidence and personal content blocked by the egress policy must likewise
    produce a refusal instead of a confident answer.
    """
    if item.get("abstain") or state in item.get("removed_in", []):
        return True
    if item.get("memory_class") != "personal":
        return False
    from threadkeeper import egress

    return not egress.personal_allowed(item.get("consumer_cli"), egress_policy)


def judge_evidence(
    item: dict,
    ctx: str,
    *,
    expected_abstention: bool,
) -> tuple[bool, str]:
    """Score the evidence layer independently from the replayed response."""
    if not expected_abstention or item.get("abstain"):
        return judge_lexical(item, ctx)
    leaked = _terms_found(_gold_terms(item), ctx)
    if leaked:
        return False, f"protected evidence surfaced: {leaked[0]!r}"
    return True, "protected evidence absent"


def replay_final_answer(item: dict, ctx: str, *, expected_abstention: bool) -> str:
    """Produce a deterministic final response from exactly the retrieved text.

    This intentionally tiny responder is a transcript replay contract, not an
    LLM substitute.  It only emits facts demonstrably present in the returned
    context, which makes a bad final result attributable to the evidence or
    answer stage instead of model randomness.
    """
    if expected_abstention:
        return _REPLAY_ABSTENTION
    terms = _gold_terms(item)
    found = _terms_found(terms, ctx)
    if item.get("gold_all") and len(found) != len(terms):
        return _REPLAY_ABSTENTION
    if item.get("gold_any") and not found:
        return _REPLAY_ABSTENTION
    return "Answer: " + "; ".join(found)


def judge_final_answer(
    item: dict,
    answer: str,
    *,
    expected_abstention: bool,
) -> tuple[bool, str]:
    """Grade the replayer's final response without reading raw retrieval text."""
    if expected_abstention:
        ok = answer == _REPLAY_ABSTENTION
        return ok, "refused" if ok else "answered despite required abstention"
    return judge_lexical({**item, "abstain": False}, answer)


def classify_outcome(
    item: dict,
    ctx: str,
    answer: str,
    *,
    evidence_correct: bool,
    answer_correct: bool,
    expected_abstention: bool,
    egress_policy: str,
) -> str:
    """Give each failure one actionable owner without exposing its content."""
    protected = _gold_terms(item)
    protected_seen = bool(_terms_found(protected, ctx + "\n" + answer))
    if item.get("memory_class") == "personal" and expected_abstention:
        from threadkeeper import egress

        allowed = egress.personal_allowed(item.get("consumer_cli"), egress_policy)
        if not allowed and protected_seen:
            return "privacy_policy_violation"
    if expected_abstention and item.get("removed_in") and protected_seen:
        return "removed_memory_use"
    if _terms_found(list(item.get("stale", [])), answer):
        return "stale_memory_use"
    if not evidence_correct:
        return "retrieval_failure"
    if not answer_correct:
        return "reasoning_failure"
    return "pass"


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
def _rate(rows: list[dict], key: str) -> dict:
    correct = sum(bool(row[key]) for row in rows)
    return {
        "n": len(rows),
        "correct": correct,
        "rate": round(correct / len(rows), 4) if rows else None,
    }


def _thresholds_pass(report: dict, thresholds: dict) -> bool:
    evidence_min = float(thresholds.get("evidence_recall", 0.0))
    answer_min = float(thresholds.get("final_answer", 0.0))
    evidence_rate = report["evidence_recall"]["rate"] or 0.0
    answer_rate = report["final_answer"]["rate"] or 0.0
    return (
        evidence_rate >= evidence_min
        and answer_rate >= answer_min
        and report["safety"]["violations"] == 0
    )


def evaluate(
    tk: dict,
    ground_truth: dict,
    *,
    judge: str = "lexical",
    llm_model: str = "claude-haiku-4-5-20251001",
    state: str = "baseline",
    egress_policy: str = "all",
    private_holdout: bool = False,
) -> dict:
    """Replay every transcript question and keep evidence and answer scores apart."""
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
        required_abstention = expects_abstention(
            item, state=state, egress_policy=egress_policy)
        if judge == "llm" and not required_abstention:
            correct, reason = judge_llm(
                item, ctx, model=llm_model, api_key=api_key)
        else:
            correct, reason = judge_evidence(
                item, ctx, expected_abstention=required_abstention)
        answer_started = time.perf_counter()
        answer = replay_final_answer(
            item, ctx, expected_abstention=required_abstention)
        answer_latency_ms = (time.perf_counter() - answer_started) * 1000.0
        answer_correct, answer_reason = judge_final_answer(
            item, answer, expected_abstention=required_abstention)
        outcome = classify_outcome(
            item, ctx, answer,
            evidence_correct=correct,
            answer_correct=answer_correct,
            expected_abstention=required_abstention,
            egress_policy=egress_policy,
        )
        rows.append({
            "id": item["id"],
            "type": item.get("type", "information_extraction"),
            "system": item.get("system", "dialog_search"),
            "abstain": required_abstention,
            "correct": correct,
            "reason": reason,
            "final_answer_correct": answer_correct,
            "final_answer_reason": answer_reason,
            "outcome": outcome,
            "tokens": tokens,
            "latency_ms": round(latency_ms, 3),
            "answer_tokens": estimate_tokens(answer),
            "answer_latency_ms": round(answer_latency_ms, 3),
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
                "evidence_recall": _rate(sub, "correct"),
                "final_answer": _rate(sub, "final_answer_correct"),
            }
    abst = [r for r in rows if r["abstain"]]
    toks = [r["tokens"] for r in rows]
    latencies = [r["latency_ms"] for r in rows]
    answer_tokens = [r["answer_tokens"] for r in rows]
    outcomes = {
        kind: sum(r["outcome"] == kind for r in rows)
        for kind in (
            "retrieval_failure", "reasoning_failure", "stale_memory_use",
            "removed_memory_use", "privacy_policy_violation",
        )
    }
    safety_violations = outcomes["removed_memory_use"] + outcomes["privacy_policy_violation"]
    with tk["db"].read_db() as conn:
        index_health = tk["embeddings"].embedding_index_health(conn)
    report = {
        "corpus_version": ground_truth.get("version", 1),
        "fixture_state": state,
        "egress_policy": egress_policy,
        "backend": backend,
        "judge": judge,
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 4) if total else 0.0,
        "evidence_recall": _rate(rows, "correct"),
        "final_answer": _rate(rows, "final_answer_correct"),
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
        "tokens_per_final_answer": {
            "mean": round(statistics.fmean(answer_tokens), 1) if answer_tokens else 0.0,
            "median": int(statistics.median(answer_tokens)) if answer_tokens else 0,
            "max": max(answer_tokens) if answer_tokens else 0,
            "total": sum(answer_tokens),
        },
        "retrieval_latency_ms": {
            "mean": round(statistics.fmean(latencies), 3) if latencies else 0.0,
            "p50": round(_percentile(latencies, 0.50), 3),
            "p95": round(_percentile(latencies, 0.95), 3),
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
        "outcomes": outcomes,
        "safety": {
            "protected_cases": sum(r["abstain"] for r in rows),
            "violations": safety_violations,
        },
        "embedding_index": index_health,
        "rows": rows,
    }
    report["thresholds"] = ground_truth.get("thresholds", {})
    report["thresholds_pass"] = _thresholds_pass(report, report["thresholds"])
    if private_holdout:
        report.pop("rows")
    return report


def format_report(report: dict) -> str:
    """Human-readable summary (the default stdout)."""
    out: list[str] = []
    out.append("── memory-quality eval ──────────────────────────────────")
    out.append(
        f"corpus=v{report['corpus_version']}  backend={report['backend']}  "
        f"state={report['fixture_state']}  egress={report['egress_policy']}"
    )
    out.append(
        f"evidence recall    : {report['evidence_recall']['rate']:.1%}  "
        f"({report['evidence_recall']['correct']}/{report['total']})"
    )
    out.append(
        f"final answer       : {report['final_answer']['rate']:.1%}  "
        f"({report['final_answer']['correct']}/{report['total']})"
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
    fta = report["tokens_per_final_answer"]
    out.append(
        f"tokens/final answer: mean={fta['mean']}  median={fta['median']}  "
        f"max={fta['max']}  total={fta['total']}"
    )
    lat = report["retrieval_latency_ms"]
    out.append(
        f"latency (ms)       : mean={lat['mean']}  p50={lat['p50']}  "
        f"p95={lat['p95']}  max={lat['max']}"
    )
    out.append("")
    out.append("per-axis evidence / final answer:")
    for ax, st in report["per_type"].items():
        out.append(
            f"  {ax:<27} {st['evidence_recall']['rate']:.1%} / "
            f"{st['final_answer']['rate']:.1%}  ({st['n']})"
        )
    outcomes = report["outcomes"]
    out.append(
        "outcomes           : " + "  ".join(
            f"{kind}={count}" for kind, count in outcomes.items())
    )
    out.append(
        f"safety             : violations={report['safety']['violations']}  "
        f"thresholds={'PASS' if report['thresholds_pass'] else 'FAIL'}"
    )
    fails = [
        r for r in report.get("rows", [])
        if not r["correct"] or not r["final_answer_correct"]
    ]
    if fails:
        out.append("")
        out.append(f"failures ({len(fails)}):")
        for r in fails:
            out.append(f"  ✗ {r['id']:<20} [{r['outcome']}] {r['reason']}")
    out.append("─────────────────────────────────────────────────────────")
    return "\n".join(out)


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────
def evaluate_matrix(args: argparse.Namespace) -> dict:
    """Run the fixed migration matrix in isolated processes.

    Configuration and embedding availability are captured during package import,
    so one fresh process per cell is the only faithful way to compare FTS with
    hybrid retrieval.  Child stdout contains aggregate JSON only; fixture text
    and replayed answers are never copied into this report.
    """
    if args.db:
        raise ValueError("--matrix only supports a fixture corpus, not --db snapshots")
    if str(args.ground_truth) == "/dev/stdin":
        raise ValueError("--matrix cannot replay a stdin holdout more than once")

    cells: list[dict] = []
    for backend in args.matrix_backends:
        for state in MATRIX_STATES:
            for policy in MATRIX_POLICIES:
                cmd = [
                    sys.executable, str(Path(__file__).resolve()), "--json",
                    "--backend", backend,
                    "--fixture-state", state,
                    "--egress-policy", policy,
                ]
                if args.ground_truth != DEFAULT_GROUND_TRUTH:
                    cmd.extend(["--ground-truth", str(args.ground_truth)])
                if args.private_holdout:
                    cmd.append("--private-holdout")
                proc = subprocess.run(
                    cmd, cwd=str(REPO_ROOT), capture_output=True,
                    text=True, timeout=300,
                )
                cell = {
                    "backend": backend,
                    "fixture_state": state,
                    "egress_policy": policy,
                }
                if proc.returncode != 0:
                    cell.update({
                        "status": "unavailable" if backend == "hybrid" else "failed",
                        "error": "backend unavailable" if backend == "hybrid" else "runner failed",
                    })
                    cells.append(cell)
                    continue
                try:
                    report = json.loads(proc.stdout)
                except json.JSONDecodeError:
                    cell.update({"status": "failed", "error": "invalid runner report"})
                    cells.append(cell)
                    continue
                if report["backend"] != backend:
                    cell.update({"status": "unavailable", "error": "backend unavailable"})
                else:
                    cell.update({
                        "status": "ok",
                        "evidence_recall": report["evidence_recall"]["rate"],
                        "final_answer": report["final_answer"]["rate"],
                        "retrieval_tokens": report["tokens_per_retrieval"]["total"],
                        "retrieval_p95_ms": report["retrieval_latency_ms"]["p95"],
                        "safety_violations": report["safety"]["violations"],
                        "thresholds_pass": report["thresholds_pass"],
                    })
                cells.append(cell)
    passed = all(
        cell["status"] == "ok" and cell["thresholds_pass"]
        for cell in cells
    )
    return {
        "corpus_version": None if args.private_holdout else _read_corpus_version(args.ground_truth),
        "matrix": cells,
        "thresholds_pass": passed,
    }


def _read_corpus_version(path: Path) -> int | None:
    """Read only the public fixture version for a matrix header."""
    if path == DEFAULT_GROUND_TRUTH:
        return json.loads(path.read_text()).get("version", 1)
    return None


def format_matrix_report(report: dict) -> str:
    """Render aggregate matrix results without fixture or answer contents."""
    out = ["── memory-quality migration matrix ───────────────────────"]
    for cell in report["matrix"]:
        prefix = (
            f"{cell['backend']:<6} {cell['fixture_state']:<9} "
            f"{cell['egress_policy']:<11} {cell['status']:<11}"
        )
        if cell["status"] == "ok":
            out.append(
                prefix
                + f" evidence={cell['evidence_recall']:.1%}"
                + f" final={cell['final_answer']:.1%}"
                + f" tokens={cell['retrieval_tokens']}"
                + f" p95ms={cell['retrieval_p95_ms']}"
                + f" violations={cell['safety_violations']}"
            )
        else:
            out.append(prefix + f" {cell['error']}")
    out.append(f"thresholds={'PASS' if report['thresholds_pass'] else 'FAIL'}")
    out.append("─────────────────────────────────────────────────────────")
    return "\n".join(out)


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
    ap.add_argument("--backend", choices=MATRIX_BACKENDS,
                    help="explicit retrieval backend; --semantic is the hybrid alias.")
    ap.add_argument("--fixture-state", choices=MATRIX_STATES, default="baseline",
                    help="public fixture state after retention/curation (default: baseline).")
    ap.add_argument("--egress-policy", choices=MATRIX_POLICIES, default="all",
                    help="personal-memory egress policy to apply to brief cases.")
    ap.add_argument("--matrix", action="store_true",
                    help="run FTS/hybrid × state × egress migration matrix.")
    ap.add_argument("--matrix-backends", choices=MATRIX_BACKENDS, nargs="+",
                    default=list(MATRIX_BACKENDS),
                    help=argparse.SUPPRESS)
    ap.add_argument("--private-holdout", action="store_true",
                    help="suppress per-case rows and failure details for an external holdout.")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero when stable corpus thresholds or safety checks fail.")
    ap.add_argument("--json", action="store_true",
                    help="emit the full report as JSON instead of a table.")
    args = ap.parse_args(argv)

    if args.semantic and args.backend == "fts":
        ap.error("--semantic conflicts with --backend fts")
    if args.matrix:
        try:
            matrix = evaluate_matrix(args)
        except ValueError as e:
            print(f"ERR: {e}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(matrix, indent=2))
        else:
            print(format_matrix_report(matrix))
        return 0 if not args.strict or matrix["thresholds_pass"] else 1

    try:
        gt = json.loads(Path(args.ground_truth).read_text())
    except (OSError, json.JSONDecodeError) as e:
        message = "private holdout could not be read" if args.private_holdout else str(e)
        print(f"ERR: {message}", file=sys.stderr)
        return 2
    semantic = args.semantic or args.backend == "hybrid"

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

        _prepare_env(db_path, semantic=semantic)
        tk = _import_threadkeeper()
        if semantic and not tk["config"].SEMANTIC_AVAILABLE:
            print("ERR: hybrid backend unavailable; install threadkeeper[semantic]", file=sys.stderr)
            return 4
        tk["config"].reload_settings({
            "THREADKEEPER_MEMORY_EGRESS": args.egress_policy,
        })
        if seed:
            seed_conn = tk["db"].get_db()
            try:
                seed_corpus(seed_conn, gt["corpus"])
                apply_fixture_state(seed_conn, gt["corpus"], args.fixture_state)
            finally:
                seed_conn.close()
            if semantic:
                seed_embeddings(tk)
        elif args.fixture_state != "baseline":
            print("ERR: --fixture-state needs the seeded fixture, not --db", file=sys.stderr)
            return 2

        try:
            report = evaluate(
                tk, gt, judge=args.judge, llm_model=args.llm_model,
                state=args.fixture_state, egress_policy=args.egress_policy,
                private_holdout=args.private_holdout,
            )
        except LLMJudgeUnavailable as e:
            print(f"ERR: {e}", file=sys.stderr)
            return 3

        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(format_report(report))
        return 0 if not args.strict or report["thresholds_pass"] else 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
