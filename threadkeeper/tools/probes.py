"""Self-diagnostic probes: claude-shape weak-spot tracking.

Probes encode known reliability sags (counting in long context, date math,
verbatim recall, format compliance, etc.). Each attempt → probe_results;
rolling stats → reliability cache; brief surfaces weak_spots.
"""

import sqlite3
import time
from typing import Optional

from .._mcp import mcp
from ..db import get_db
from ..helpers import fmt_age, q, gen_probe_id
from .. import identity
from ..identity import _ensure_session, _detect_self_cid, _emit


def _grade_probe(grader: str, expected_pattern: Optional[str],
                 response: str) -> bool:
    """Apply grader to a response. 'manual' always returns False — caller
    confirms correctness via record_attempt(success=True)."""
    if grader == "manual" or not expected_pattern:
        return False
    if grader == "exact":
        return expected_pattern in response
    if grader == "regex":
        try:
            import re as _re
            return _re.search(expected_pattern, response, _re.DOTALL) is not None
        except Exception:
            return False
    return False


def _recompute_reliability(conn: sqlite3.Connection, category: str) -> dict:
    """Recompute aggregate stats for one category from probe_results.
    UPSERTs the reliability row. Returns the new aggregate as a dict."""
    now_t = int(time.time())
    overall = conn.execute(
        "SELECT COUNT(*) c, SUM(success) s, MAX(created_at) last "
        "FROM probe_results WHERE category=?",
        (category,),
    ).fetchone()
    attempts = overall["c"] or 0
    successes = overall["s"] or 0
    last_at = overall["last"]

    def _fail_rate(window_s: int) -> Optional[float]:
        cutoff = now_t - window_s
        r = conn.execute(
            "SELECT COUNT(*) c, SUM(success) s "
            "FROM probe_results WHERE category=? AND created_at >= ?",
            (category, cutoff),
        ).fetchone()
        n = r["c"] or 0
        if n == 0:
            return None
        s = r["s"] or 0
        return (n - s) / n

    fr_7 = _fail_rate(7 * 86400)
    fr_30 = _fail_rate(30 * 86400)
    conn.execute(
        "INSERT INTO reliability (category, attempts, successes, last_at, "
        "fail_rate_7d, fail_rate_30d, updated_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(category) DO UPDATE SET "
        "  attempts=excluded.attempts, successes=excluded.successes, "
        "  last_at=excluded.last_at, fail_rate_7d=excluded.fail_rate_7d, "
        "  fail_rate_30d=excluded.fail_rate_30d, updated_at=excluded.updated_at",
        (category, attempts, successes, last_at, fr_7, fr_30, now_t),
    )
    return {
        "category": category, "attempts": attempts, "successes": successes,
        "last_at": last_at, "fail_rate_7d": fr_7, "fail_rate_30d": fr_30,
    }


@mcp.tool()
def register_probe(category: str, prompt: str,
                   expected_pattern: str = "",
                   grader: str = "regex") -> str:
    """Register a self-test probe: a known weak-spot task with a verifier.

    `grader`: 'regex' (pattern match in response), 'exact' (substring), or
    'manual' (claude self-grades — always counts as failure unless caller
    explicitly confirms success via record_attempt). `expected_pattern`
    optional for 'manual'.

    Categories should be claude-shape: 'count_long_context',
    'date_arithmetic', 'recall_verbatim_block', 'detect_contradiction',
    'follow_negative_instruction', 'preserve_list_order',
    'respect_length_limit', 'needle_mid_context', 'fact_vs_inference',
    'notice_absence', 'strict_format_compliance', 'uncertainty_acknowledgment'."""
    if grader not in ("regex", "exact", "manual"):
        return f"ERR bad_grader={grader}"
    if not category.strip() or not prompt.strip():
        return "ERR empty_category_or_prompt"
    conn = get_db()
    _ensure_session(conn)
    pid = gen_probe_id(conn)
    conn.execute(
        "INSERT INTO probes (id, category, prompt, expected_pattern, "
        "grader, created_at) VALUES (?,?,?,?,?,?)",
        (pid, category.strip(), prompt, expected_pattern or None,
         grader, int(time.time())),
    )
    _emit(conn, "probe_register", target=pid, summary=category)
    conn.commit()
    return f"ok id={pid} cat={category}"


@mcp.tool()
def run_probe(probe_id: str) -> str:
    """Surface a registered probe for self-attempt. Returns the prompt and
    the grader hint. After attempting, call record_attempt(category,
    success=true/false, probe_id=...) — the harness doesn't auto-grade
    because attempting and judging are the same model."""
    conn = get_db()
    _ensure_session(conn)
    p = conn.execute(
        "SELECT id, category, prompt, expected_pattern, grader, enabled "
        "FROM probes WHERE id=?",
        (probe_id.strip(),),
    ).fetchone()
    if not p:
        return f"ERR probe_not_found={probe_id}"
    if not p["enabled"]:
        return f"ERR probe_disabled={probe_id}"
    parts = [
        f"probe={p['id']} cat={p['category']} grader={p['grader']}",
    ]
    if p["expected_pattern"]:
        parts.append(f"expect={p['expected_pattern']}")
    parts.append(f"prompt={p['prompt']}")
    return "\n".join(parts)


@mcp.tool()
def record_attempt(category: str, success: bool, note: str = "",
                   probe_id: str = "", latency_ms: int = 0) -> str:
    """Record a self-test outcome. Updates reliability aggregates.

    Use for both registered probes (pass `probe_id`) and ad-hoc self-
    observations — e.g. you noticed yourself miscounting items in this
    very turn → record_attempt('count_long_context', false, note='said 32, actual 47')."""
    if not category.strip():
        return "ERR empty_category"
    conn = get_db()
    _ensure_session(conn)
    cat = category.strip()
    pid = probe_id.strip() or None
    if pid:
        if not conn.execute("SELECT 1 FROM probes WHERE id=?", (pid,)).fetchone():
            return f"ERR probe_not_found={pid}"
    now_t = int(time.time())
    cid = _detect_self_cid()
    cur = conn.execute(
        "INSERT INTO probe_results (probe_id, category, session_id, cid, "
        "success, latency_ms, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (pid, cat, identity._session_id, cid, 1 if success else 0,
         latency_ms or None, note or None, now_t),
    )
    agg = _recompute_reliability(conn, cat)
    _emit(conn, "probe_attempt", target=cat,
          summary=f"{'pass' if success else 'fail'}: {note[:120]}")
    conn.commit()
    fr_7 = agg["fail_rate_7d"]
    fr_str = f"{fr_7:.2f}" if fr_7 is not None else "?"
    return (
        f"ok rid={cur.lastrowid} cat={cat} "
        f"attempts={agg['attempts']} successes={agg['successes']} "
        f"fail7d={fr_str}"
    )


@mcp.tool()
def reliability_for(category: str, window_days: int = 30) -> str:
    """Reliability stats for one category over a window."""
    if not category.strip():
        return "ERR empty_category"
    conn = get_db()
    cat = category.strip()
    now_t = int(time.time())
    cutoff = now_t - max(1, window_days) * 86400
    win = conn.execute(
        "SELECT COUNT(*) c, SUM(success) s, MAX(created_at) last "
        "FROM probe_results WHERE category=? AND created_at >= ?",
        (cat, cutoff),
    ).fetchone()
    n = win["c"] or 0
    if n == 0:
        return f"cat={cat} no_data window={window_days}d"
    s = win["s"] or 0
    rate = s / n
    last = win["last"]
    cached = conn.execute(
        "SELECT fail_rate_7d, fail_rate_30d FROM reliability WHERE category=?",
        (cat,),
    ).fetchone()
    fr7 = (
        f"{cached['fail_rate_7d']:.2f}"
        if cached and cached["fail_rate_7d"] is not None
        else "?"
    )
    fr30 = (
        f"{cached['fail_rate_30d']:.2f}"
        if cached and cached["fail_rate_30d"] is not None
        else "?"
    )
    return (
        f"cat={cat} window={window_days}d attempts={n} success={s} "
        f"rate={rate:.2f} fail7d={fr7} fail30d={fr30} "
        f"last={fmt_age(now_t - last)}_ago"
    )


@mcp.tool()
def weak_spots(top_n: int = 5) -> str:
    """List categories ranked by recent failure rate (min 3 attempts in 30d),
    plus registered probe categories with no attempts yet (= unknown,
    equally important to test)."""
    conn = get_db()
    now_t = int(time.time())
    weak = conn.execute(
        "SELECT category, fail_rate_7d, fail_rate_30d, attempts, last_at "
        "FROM reliability WHERE fail_rate_30d IS NOT NULL AND attempts >= 3 "
        "ORDER BY COALESCE(fail_rate_7d, fail_rate_30d) DESC LIMIT ?",
        (max(1, top_n),),
    ).fetchall()
    unknown = conn.execute(
        "SELECT DISTINCT p.category FROM probes p "
        "LEFT JOIN reliability r ON r.category = p.category "
        "WHERE p.enabled = 1 AND (r.category IS NULL OR r.attempts = 0)"
    ).fetchall()
    out = [f"weak n={len(weak)}"]
    for r in weak:
        age = fmt_age(now_t - r["last_at"]) if r["last_at"] else "?"
        f7 = f"{r['fail_rate_7d']:.2f}" if r["fail_rate_7d"] is not None else "?"
        f30 = f"{r['fail_rate_30d']:.2f}" if r["fail_rate_30d"] is not None else "?"
        out.append(
            f"  {r['category']} fail7d={f7} fail30d={f30} "
            f"n={r['attempts']} last={age}_ago"
        )
    if unknown:
        out.append(f"unknown n={len(unknown)}")
        for r in unknown:
            out.append(f"  {r['category']} (never_tested)")
    if not weak and not unknown:
        return "no_data (register probes via register_probe)"
    return "\n".join(out)
