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
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_GROUND_TRUTH = HERE / "ground_truth.json"

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
    from threadkeeper import db, config, brief as brief_mod
    from threadkeeper.tools import dialog as dialog_tool, threads as threads_tool
    return {
        "db": db,
        "config": config,
        "brief": brief_mod,
        "dialog_search": dialog_tool.dialog_search,
        "search": threads_tool.search,
    }


# ──────────────────────────────────────────────────────────────────────────
# Corpus seeding (demo fixture → fresh DB)
# ──────────────────────────────────────────────────────────────────────────
def seed_corpus(conn, corpus: dict, now: int | None = None) -> None:
    """Insert the demo corpus into a fresh DB the way ingest()/note() would.

    dialog_messages are mirrored into dialog_fts by hand (the ingest path does
    this; there is no trigger). notes rely on the notes_fts AFTER INSERT
    trigger defined in the schema."""
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
        conn.execute(
            "INSERT OR IGNORE INTO dialog_fts (uuid, content) VALUES (?, ?)",
            (m["uuid"], m["content"]),
        )
    for n in corpus.get("notes", []):
        ts = now + int(n.get("day_offset", 0)) * 86400
        conn.execute(
            "INSERT INTO notes (thread_id, content, kind, created_at, session_id) "
            "VALUES (?, ?, ?, ?, 'memory-eval')",
            (n.get("thread_id"), n["content"], n.get("kind", "insight"), ts),
        )
    conn.commit()


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
# Judges
# ──────────────────────────────────────────────────────────────────────────
_NO_HIT_MARKERS = ("no_matches", "no_idle", "fts_error")


def judge_lexical(item: dict, ctx: str) -> tuple[bool, str]:
    """Deterministic substring judge.

    Normal question  → correct if the gold fact was surfaced (recall).
    Abstention (`abstain`) → correct if NO `trap_substrings` (fabricated
    claims, absent from the corpus) leaked into the retrieved context. A bare
    `no_matches` trivially contains no trap, so it counts as a correct refusal.
    """
    low = ctx.lower()
    if item.get("abstain"):
        leaked = [t for t in item.get("trap_substrings", []) if t.lower() in low]
        if leaked:
            return False, f"leaked trap: {leaked[0]!r}"
        return True, "abstained (no trap surfaced)"
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
    backend = "semantic" if tk["config"].SEMANTIC_AVAILABLE else "fts"
    rows: list[dict] = []
    for item in ground_truth["questions"]:
        ctx = retrieve(tk, item)
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
    out.append("")
    out.append("per-axis accuracy:")
    for ax, st in report["per_type"].items():
        out.append(f"  {ax:<24} {st['accuracy']:.1%}  ({st['correct']}/{st['n']})")
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
    args = ap.parse_args(argv)

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

        _prepare_env(db_path, semantic=args.semantic)
        tk = _import_threadkeeper()
        if seed:
            seed_corpus(tk["db"].get_db(), gt["corpus"])

        try:
            report = evaluate(tk, gt, judge=args.judge, llm_model=args.llm_model)
        except LLMJudgeUnavailable as e:
            print(f"ERR: {e}", file=sys.stderr)
            return 3

        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(format_report(report))
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
