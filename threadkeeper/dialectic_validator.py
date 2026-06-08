"""Dialectic validator — the SOLE interpreter of the dialectic_observations
buffer. Periodically spawns one opus child that reads pending observations +
the full current model and turns raw user replies into claims via the existing
dialectic_* tools, then resolves each observation.

Mirrors candidate_reviewer (spawns an LLM child); the miner is the cheap
mechanical producer, this is the careful infrequent consumer."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time

from .config import DIALECTIC_VALIDATE_INTERVAL_S, DIALECTIC_VALIDATE_MIN, \
    DIALECTIC_MAX_NEW_CLAIMS
from .db import get_db
from . import identity
from .identity import _ensure_session

logger = logging.getLogger(__name__)

_started = False


DIALECTIC_VALIDATOR_PROMPT = """\
You are a DIALECTIC VALIDATOR for thread-keeper's user model.

The dialectic_miner mechanically captured recent USER replies (with the
preceding assistant turn as context) into a buffer. Your job: turn these raw
observations into the dialectic user-model, and self-correct it.

You are given (a) the CURRENT MODEL -- every active claim with its domain, tier
and confidence -- and (b) PENDING OBSERVATIONS. For each observation (or a
coherent cluster of them) choose exactly one action:

  1. dialectic_evidence(claim_id=..., kind='support', quote=..., source='dialog',
     weight=W) -- the observation corroborates an EXISTING claim. PREFER THIS
     over creating a near-duplicate claim.
  2. dialectic_evidence(claim_id=..., kind='contradict', quote=..., weight=W) --
     the observation conflicts with an existing claim (the user did/said the
     opposite). This is how the model self-corrects.
  3. dialectic_supersede(old_claim_id=..., new_claim=..., quote=...) -- an
     existing claim is right in spirit but needs refining/replacing.
  4. dialectic_claim(claim=..., domain=..., evidence=..., evidence_kind='support')
     -- genuinely NEW territory not covered by any existing claim.
  5. (write nothing) -- the observation is chit-chat / noise / a one-off with no
     durable signal about who the user is.

Then ALWAYS call dialectic_observation_resolve(id=<obs id>, note='...') for
every observation you processed (including ones you deliberately skipped), so
it is never re-interpreted.

WEIGHT (the `weight` arg, base trust in [0,1]; an automatic 0.5 review-fork
discount is applied on top): use ~1.0 for an explicit user STATEMENT of
preference/decision, ~0.5 for a trait you only INFER from behavior.

RULES:
- PREFER support-existing over new claims. Dedup hard against the current model.
- MERGE near-duplicate observations into ONE claim.
- contradict / supersede ONLY on a clear conflict - don't thrash the model.
- LIMIT %(max_new)d NEW claims this pass; if more seem warranted, pick the
  strongest and leave the rest.
- domain in style / workflow / values / context / skills / other.

Finish with a one-paragraph summary: "Processed N observations: K supports,
C contradicts, S supersedes, M new claims, X skipped."

CURRENT MODEL
=============
%(model)s

PENDING OBSERVATIONS
====================
%(inventory)s
"""


def _last_validate_ts(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT target FROM events WHERE kind='dialectic_validate_pass' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row or not row["target"]:
        return 0
    try:
        return int(row["target"])
    except (ValueError, TypeError):
        return 0


def _record_pass(conn: sqlite3.Connection, ts: int, outcome: str) -> None:
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, target, summary, created_at) "
            "VALUES (?, 'dialectic_validate_pass', ?, ?, ?)",
            (identity._session_id or "", str(ts), outcome[:300], int(time.time())),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("dialectic_validator: record_pass failed", exc_info=True)


def _collect_pending(conn: sqlite3.Connection) -> tuple[str, int]:
    """Inventory of pending observations within the last 30 days."""
    now = int(time.time())
    stale_cutoff = now - 30 * 86400
    try:
        rows = conn.execute(
            "SELECT id, user_quote, context, source_cid, created_at "
            "FROM dialectic_observations WHERE status='pending' AND created_at > ? "
            "ORDER BY created_at ASC",
            (stale_cutoff,),
        ).fetchall()
    except sqlite3.OperationalError:
        return ("", 0)
    if not rows:
        return ("", 0)
    parts = [f"PENDING OBSERVATIONS (n={len(rows)})\n"]
    for r in rows:
        quote = (r["user_quote"] or "")[:400].replace("\n", " ")
        ctx = (r["context"] or "")[:200].replace("\n", " ")
        parts.append(
            f"  #{r['id']} cid={(r['source_cid'] or '-')[:8]}\n"
            f"    context: {ctx}\n"
            f"    user: {quote}"
        )
    return ("\n".join(parts), len(rows))


def _current_model_dump(conn: sqlite3.Connection) -> str:
    """The full active model the child must dedup against."""
    from .tools.dialectic import dialectic_review
    out = dialectic_review(min_confidence="low", k=200)
    return out if out and not out.startswith("no_claims") else "(model is empty)"


def run_validate_pass(force: bool = False) -> str:
    if DIALECTIC_VALIDATE_INTERVAL_S <= 0 and not force:
        return "disabled"
    conn = get_db()
    _ensure_session(conn)
    now = int(time.time())
    inventory, n_pending = _collect_pending(conn)
    if n_pending < DIALECTIC_VALIDATE_MIN:
        _record_pass(conn, now,
                     f"below_threshold pending={n_pending} "
                     f"min={DIALECTIC_VALIDATE_MIN}")
        return f"below_threshold n={n_pending}"

    prompt = DIALECTIC_VALIDATOR_PROMPT % {
        "max_new": DIALECTIC_MAX_NEW_CLAIMS,
        "model": _current_model_dump(conn),
        "inventory": inventory,
    }

    from .tools.spawn import spawn  # type: ignore
    try:
        result = spawn(
            prompt=prompt,
            visible=False,
            capture_output=True,
            permission_mode="auto",
            role="dialectic_validator",
            write_origin="background_review",
            slim=True,
            extra_allowed_tools=(
                "mcp__thread-keeper__dialectic_claim,"
                "mcp__thread-keeper__dialectic_evidence,"
                "mcp__thread-keeper__dialectic_supersede,"
                "mcp__thread-keeper__dialectic_review,"
                "mcp__thread-keeper__dialectic_observation_resolve"
            ),
        )
    except Exception as e:
        _record_pass(conn, now, f"spawn_error: {e}")
        return f"spawn_error: {e}"

    _record_pass(conn, now, f"spawned pending={n_pending} :: {str(result)[:140]}")
    return str(result)


def _serve_loop() -> None:
    while True:
        try:
            run_validate_pass()
        except Exception:
            logger.debug("dialectic_validator tick failed", exc_info=True)
        time.sleep(DIALECTIC_VALIDATE_INTERVAL_S)


def start_dialectic_validator_daemon() -> None:
    """Idempotent. Spawns children -> same cascade-prevention as
    candidate_reviewer (BACKGROUND_DAEMONS_ALLOWED + SEMANTIC_AVAILABLE)."""
    global _started
    if _started:
        return
    if DIALECTIC_VALIDATE_INTERVAL_S <= 0:
        return
    from .config import BACKGROUND_DAEMONS_ALLOWED, SEMANTIC_AVAILABLE
    if not BACKGROUND_DAEMONS_ALLOWED:
        return
    if not SEMANTIC_AVAILABLE:
        return
    t = threading.Thread(target=_serve_loop, name="dialectic_validator", daemon=True)
    t.start()
    _started = True
