"""Thread validation: heuristic triage of active threads.

Scans active threads, classifies each by content-and-age signals, and
proposes (or applies) close/idle actions. dry_run=True by default.

Categories, evaluated in priority order (first match wins per thread):

  no_notes_old     active, no notes recorded, age ≥ no_notes_days
                   → close as "never advanced"
  shipped          last note contains a shipped-marker token AND has
                   been settled (no new notes) ≥ shipped_settle_days
                   → close with outcome=last_move
  dropped_open_q   last note has kind='open_q' AND has gone unfollowed
                   ≥ drop_open_q_days
                   → close as "open question dropped"
  stale_idle       active, no touch in ≥ stale_days (catch-all)
                   → demote to idle (NOT close — recoverable on next note)

Idle threads are never touched. Per CLAUDE.md, close_thread is the only
hard close path; this tool composes that path with heuristics. The
companion `consolidate()` already covers idle_stale; we still surface
it here so the validator gives a complete picture in one call.

Shipped-marker regex matches English + Russian. Add tokens via the
shipped_markers param (comma-separated additions appended to defaults).
"""

import re
import sqlite3
import time

from .._mcp import mcp
from ..db import get_db
from ..helpers import fmt_age, q
from ..identity import _ensure_session, _emit


_SHIPPED_DEFAULT = (
    r"shipped|fixed|works|passing|passed|done|merged|completed|landed|"
    r"finished|resolved|"
    r"закрыто|готово|сделано|починен[оа]?|пройден[оа]?|решен[оа]?|"
    r"запущен[оа]?|внедрен[оа]?"
)


VALIDATE_NO_NOTES_DAYS = 7
VALIDATE_SHIPPED_SETTLE_DAYS = 3
VALIDATE_DROP_OPEN_Q_DAYS = 14
VALIDATE_STALE_DAYS = 30


def _build_shipped_re(extra: str) -> "re.Pattern[str]":
    parts = [_SHIPPED_DEFAULT]
    extra = (extra or "").strip()
    if extra:
        extras = [re.escape(t.strip()) for t in extra.split(",") if t.strip()]
        if extras:
            parts.append("|".join(extras))
    return re.compile(r"\b(" + "|".join(parts) + r")\b", re.IGNORECASE)


@mcp.tool()
def validate_threads(
    dry_run: bool = True,
    no_notes_days: int = VALIDATE_NO_NOTES_DAYS,
    shipped_settle_days: int = VALIDATE_SHIPPED_SETTLE_DAYS,
    drop_open_q_days: int = VALIDATE_DROP_OPEN_Q_DAYS,
    stale_days: int = VALIDATE_STALE_DAYS,
    shipped_markers: str = "",
) -> str:
    """Heuristic triage of active threads — propose (dry_run=True, default)
    or apply (dry_run=False) close/idle actions per category.

    Categories (first match wins):
      no_notes_old     no notes + age ≥ no_notes_days        → close
      shipped          last-note shipped-marker + settled    → close (outcome=last_move)
      dropped_open_q   last note open_q, unfollowed          → close
      stale_idle       no touch ≥ stale_days                 → idle (not close)

    Idle threads are never touched. shipped_markers is a comma-separated
    list of extra tokens to OR into the default English+Russian regex.
    """
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    shipped_re = _build_shipped_re(shipped_markers)

    findings: dict[str, list[dict]] = {
        "no_notes_old": [],
        "shipped": [],
        "dropped_open_q": [],
        "stale_idle": [],
    }

    threads = conn.execute(
        "SELECT id, question, opened_at, last_touched_at, last_move "
        "FROM threads WHERE state='active' "
        "ORDER BY last_touched_at ASC"
    ).fetchall()

    for t in threads:
        tid = t["id"]
        age_sec = now - t["opened_at"]
        idle_sec = now - t["last_touched_at"]

        last_note = conn.execute(
            "SELECT kind, content, created_at FROM notes "
            "WHERE thread_id=? ORDER BY created_at DESC LIMIT 1",
            (tid,),
        ).fetchone()
        notes_count = conn.execute(
            "SELECT COUNT(*) c FROM notes WHERE thread_id=?", (tid,)
        ).fetchone()["c"]

        if notes_count == 0 and age_sec >= no_notes_days * 86400:
            findings["no_notes_old"].append({
                "thread": tid,
                "question": (t["question"] or "")[:120],
                "age": fmt_age(age_sec),
                "outcome": f"never advanced ({fmt_age(age_sec)} since open, no notes)",
            })
            continue

        if last_note and shipped_re.search(last_note["content"] or ""):
            note_age = now - last_note["created_at"]
            if note_age >= shipped_settle_days * 86400:
                outcome_src = (t["last_move"] or last_note["content"] or "").strip()
                outcome = (
                    f"shipped: {outcome_src[:90]}"
                    if outcome_src else "shipped (last_move empty)"
                )
                findings["shipped"].append({
                    "thread": tid,
                    "question": (t["question"] or "")[:120],
                    "last_move": outcome_src[:120],
                    "settled": fmt_age(note_age),
                    "outcome": outcome,
                })
                continue

        if last_note and last_note["kind"] == "open_q":
            note_age = now - last_note["created_at"]
            if note_age >= drop_open_q_days * 86400:
                findings["dropped_open_q"].append({
                    "thread": tid,
                    "question": (t["question"] or "")[:120],
                    "open_q": (last_note["content"] or "")[:120],
                    "dropped": fmt_age(note_age),
                    "outcome": f"open question dropped after {fmt_age(note_age)} without follow-up",
                })
                continue

        if idle_sec >= stale_days * 86400:
            findings["stale_idle"].append({
                "thread": tid,
                "question": (t["question"] or "")[:120],
                "stale_for": fmt_age(idle_sec),
            })

    applied = {k: 0 for k in findings}
    if not dry_run:
        for f in findings["no_notes_old"]:
            conn.execute(
                "UPDATE threads SET state='closed', outcome=?, last_touched_at=? "
                "WHERE id=?",
                (f["outcome"], now, f["thread"]),
            )
            _emit(conn, "validate_close:no_notes_old",
                  target=f["thread"], summary=f["outcome"])
            applied["no_notes_old"] += 1
        for f in findings["shipped"]:
            conn.execute(
                "UPDATE threads SET state='closed', outcome=?, last_touched_at=? "
                "WHERE id=?",
                (f["outcome"], now, f["thread"]),
            )
            _emit(conn, "validate_close:shipped",
                  target=f["thread"], summary=f["outcome"])
            applied["shipped"] += 1
        for f in findings["dropped_open_q"]:
            conn.execute(
                "UPDATE threads SET state='closed', outcome=?, last_touched_at=? "
                "WHERE id=?",
                (f["outcome"], now, f["thread"]),
            )
            _emit(conn, "validate_close:dropped_open_q",
                  target=f["thread"], summary=f["outcome"])
            applied["dropped_open_q"] += 1
        for f in findings["stale_idle"]:
            conn.execute(
                "UPDATE threads SET state='idle', last_touched_at=? WHERE id=?",
                (now, f["thread"]),
            )
            _emit(conn, "validate_idle:stale", target=f["thread"])
            applied["stale_idle"] += 1
        conn.commit()

    close_total = (
        len(findings["no_notes_old"])
        + len(findings["shipped"])
        + len(findings["dropped_open_q"])
    )
    out = [
        f"validate_threads dry_run={dry_run} "
        f"scanned={len(threads)} "
        f"close={close_total} idle={len(findings['stale_idle'])}"
    ]
    if not dry_run:
        out.append("applied " + " ".join(f"{k}={v}" for k, v in applied.items()))

    for category, items in findings.items():
        if not items:
            continue
        out.append("")
        out.append(f"[{category}]")
        for f in items:
            if category == "no_notes_old":
                out.append(
                    f"  {f['thread']} age={f['age']} q={q(f['question'])} "
                    f"→ close: {q(f['outcome'])}"
                )
            elif category == "shipped":
                out.append(
                    f"  {f['thread']} settled={f['settled']} q={q(f['question'])} "
                    f"→ close: {q(f['outcome'])}"
                )
            elif category == "dropped_open_q":
                out.append(
                    f"  {f['thread']} dropped={f['dropped']} q={q(f['question'])} "
                    f"→ close: {q(f['outcome'])}"
                )
            elif category == "stale_idle":
                out.append(
                    f"  {f['thread']} stale={f['stale_for']} q={q(f['question'])} "
                    f"→ idle"
                )

    return "\n".join(out)
