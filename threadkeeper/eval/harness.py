"""Decision-quality eval harness for the learning loop (issue #72).

The quality-control daemons each make a binary call on every item they see:

  * ``shadow_review``      — MATERIALIZE (write a durable skill) vs SKIP, over a
                            window of recent dialog.
  * ``candidate_reviewer`` — ACCEPT (genuine note/skill/verbatim) vs REJECT
                            (false-positive noise), over a harvested candidate.
  * ``curator``            — KEEP vs PRUNE, i.e. the open-ended judgment "is this
                            an entry worth keeping in the library?".

There was no way to tell whether those calls were *good*: the codebase has
decision telemetry but no labeled set and no precision/recall (issue #72). This
harness closes that gap with three pieces, modeled on the
``verify_ingest.py`` PASS/PARTIAL/FAIL surface and the evidently.ai
LLM-as-a-judge guidance (build a labeled set; measure judge↔human agreement;
calibrate before trusting a judge's scores):

(a) Fixtures — ``fixtures/shadow.json`` (dialog windows + expected
    materialize/skip), ``fixtures/candidates.json`` (candidate snippets +
    expected accept/reject), and ``fixtures/skill_quality.json`` (candidate
    skill bodies + a human high/low quality label). All hand-written and
    anonymized; ``test_eval_harness.py`` asserts they carry no secrets/paths.

(b) Two judges, mirroring the memory-recall harness (#71):

    * ``rubric`` (default, offline, deterministic, no API key) — a *signal-vote*
      classifier that is coupled to the LIVE daemon prompt. Each fixture item
      carries human ``signals`` (e.g. ``stated_policy``, ``false_positive``);
      each signal maps to an ``anchor`` phrase that the daemon's rubric ships.
      A signal only votes for its decision if its anchor is **still present in
      the current prompt** — so editing the rubric (dropping a signal class)
      deactivates those signals and *moves the metric* (acceptance criterion
      #2), deterministically and offline. This is a faithful but cheap stand-in
      for the LLM; on the bundled golden fixtures it agrees with the human
      labels, so a rubric edit that drops a criterion shows up as a metric
      regression in CI.

    * ``llm`` (opt-in, needs ``ANTHROPIC_API_KEY``) — replays the *actual*
      daemon prompt (``SHADOW_REVIEW_PROMPT`` / ``CANDIDATE_REVIEW_PROMPT``)
      over each item and parses the daemon's own verdict contract. This is the
      high-fidelity decision-quality measurement; a prompt edit obviously moves
      it because the model reads the edited prompt.

(c) Calibration — the skill-quality axis reports judge↔human **agreement**
    (raw accuracy + Cohen's kappa). A drifting judge (the offline heuristic or
    the LLM) shows up as falling agreement against the fixed human labels.

The pure functions (metrics, kappa, the rubric classifier, the quality
heuristic) take their inputs as plain data and import nothing from
``threadkeeper`` at module load, so they are unit-testable in-process; only the
live-prompt getters and the LLM backend touch the rest of the package, and they
defer those imports.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Iterable, Optional

HERE = Path(__file__).resolve().parent
FIXTURES_DIR = HERE / "fixtures"

# A decision axis is "ready" (its precision/recall are meaningfully defined)
# only with enough labels AND both classes present. Below this the harness
# reports the axis as thin, not as a quality failure.
MIN_LABELS = 6


# ──────────────────────────────────────────────────────────────────────────
# Signal registries — couple a fixture's human signal tags to the LIVE rubric.
#
# Each entry: signal_key -> {"label": <decision>, "anchor": <phrase the daemon
# prompt ships>}. The rubric classifier honors a signal's vote only when its
# anchor is still present in the current prompt text, so a rubric edit that
# removes a criterion deactivates the signals that depend on it.
# ──────────────────────────────────────────────────────────────────────────

# Phrases below are exact substrings of SHADOW_REVIEW_PROMPT's
# "CLASS-LEVEL signals (materialize)" / "NOT class-level (skip)" sections.
SHADOW_SIGNALS: dict[str, dict] = {
    "debugging_insight":   {"label": "materialize", "anchor": "debugging insight"},
    "stated_policy":       {"label": "materialize", "anchor": "workflow rule"},
    "corrected_skill":     {"label": "materialize", "anchor": "corrected misunderstanding"},
    "recovery_procedure":  {"label": "materialize", "anchor": "recovery / cleanup procedure"},
    "one_off_task":        {"label": "skip", "anchor": "one-off task"},
    "transient_confusion": {"label": "skip", "anchor": "session-transient confusion"},
    "what_is_question":    {"label": "skip", "anchor": "asking what something is"},
    "self_summary":        {"label": "skip", "anchor": "summarizing what just happened"},
    "transient_env_error": {"label": "skip", "anchor": "transient env errors"},
}

# The rubric SECTIONS each decision's anchors must live in. A signal votes only
# if its anchor is present in its decision's section (so an edit to the
# materialize/skip rubric block moves the metric even when the same phrase
# happens to appear elsewhere in the prompt, e.g. in POSITIVE_EXAMPLES). When a
# section marker isn't found (e.g. someone reworded a header), coupling degrades
# gracefully to whole-prompt presence rather than cratering every signal.
SHADOW_SECTIONS: dict[str, tuple[str, str]] = {
    "materialize": ("CLASS-LEVEL signals (materialize)", "NOT class-level (skip)"),
    "skip": ("NOT class-level (skip)", "PROCEDURE"),
}

# Phrases below are exact substrings of CANDIDATE_REVIEW_PROMPT's action menu.
CANDIDATE_SIGNALS: dict[str, dict] = {
    "class_level_rule": {"label": "accept", "anchor": "class-level rule"},
    "refines_skill":    {"label": "accept", "anchor": "refines a skill"},
    "incident_note":    {"label": "accept", "anchor": "per-incident decision"},
    "user_verbatim":    {"label": "accept", "anchor": "worth preserving verbatim"},
    "false_positive":   {"label": "reject", "anchor": "false positive"},
    "system_fragment":  {"label": "reject", "anchor": "system prompt fragment"},
    "log_dump":         {"label": "reject", "anchor": "log dump"},
}

CANDIDATE_SECTIONS: dict[str, tuple[str, str]] = {
    "accept": ("PROCEDURE", "6. REJECT"),
    "reject": ("6. REJECT", "DECISION RULES"),
}


# ──────────────────────────────────────────────────────────────────────────
# Pure functions: metrics
# ──────────────────────────────────────────────────────────────────────────

def binary_metrics(pairs: Iterable[tuple[str, str]], positive: str) -> dict:
    """Precision / recall / F1 for a binary decision.

    ``pairs`` is an iterable of ``(predicted, gold)`` labels; ``positive`` is
    the class precision/recall are computed against. Returns a dict with the
    confusion counts and the three metrics (``None`` when undefined — e.g. no
    predicted/actual positives — so a thin fixture never fakes a 0.0).
    """
    tp = fp = fn = tn = 0
    for pred, gold in pairs:
        if gold == positive and pred == positive:
            tp += 1
        elif gold != positive and pred == positive:
            fp += 1
        elif gold == positive and pred != positive:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    if precision is None or recall is None:
        f1 = None
    elif precision == 0 and recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "positive": positive,
        "n": tp + fp + fn + tn,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(precision, 4) if precision is not None else None,
        "recall": round(recall, 4) if recall is not None else None,
        "f1": round(f1, 4) if f1 is not None else None,
        "accuracy": round((tp + tn) / (tp + fp + fn + tn), 4)
        if (tp + fp + fn + tn) else None,
    }


def cohen_kappa(pairs: Iterable[tuple[str, str]]) -> Optional[float]:
    """Cohen's kappa between two labelers over ``(a, b)`` label pairs.

    1.0 = perfect agreement, 0.0 = chance-level, <0 = worse than chance.
    Returns ``None`` for an empty set. When both labelers are perfectly
    constant and identical (no variance), agreement is trivially perfect → 1.0.
    """
    pairs = list(pairs)
    n = len(pairs)
    if not n:
        return None
    po = sum(1 for a, b in pairs if a == b) / n
    labels = {a for a, _ in pairs} | {b for _, b in pairs}
    pe = 0.0
    for lab in labels:
        pa = sum(1 for a, _ in pairs if a == lab) / n
        pb = sum(1 for _, b in pairs if b == lab) / n
        pe += pa * pb
    if pe >= 1.0:
        return 1.0
    return (po - pe) / (1 - pe)


def agreement(pairs: Iterable[tuple[str, str]]) -> dict:
    """Judge↔human agreement: raw accuracy + Cohen's kappa over ``(judge,
    human)`` pairs."""
    pairs = list(pairs)
    n = len(pairs)
    acc = sum(1 for a, b in pairs if a == b) / n if n else None
    kappa = cohen_kappa(pairs)
    return {
        "n": n,
        "accuracy": round(acc, 4) if acc is not None else None,
        "kappa": round(kappa, 4) if kappa is not None else None,
    }


# ──────────────────────────────────────────────────────────────────────────
# Pure functions: the deterministic rubric classifier + quality heuristic
# ──────────────────────────────────────────────────────────────────────────

def _section_text(prompt: str, start: str, end: str) -> Optional[str]:
    """Return the slice of ``prompt`` between markers ``start`` and ``end``
    (start inclusive, end exclusive), or ``None`` if ``start`` is absent."""
    lo = prompt.find(start)
    if lo < 0:
        return None
    hi = prompt.find(end, lo + len(start))
    return prompt[lo: hi if hi >= 0 else len(prompt)]


def rubric_predict(prompt: str, signals: Iterable[str], registry: dict,
                   default: str,
                   sections: Optional[dict] = None) -> tuple[str, str]:
    """Predict a decision from a fixture item's human ``signals``, coupled to
    the live ``prompt``.

    Each signal votes for its registry label ONLY if its anchor phrase is still
    present in the rubric. With ``sections`` (label → (start, end) markers) the
    anchor must appear inside that decision's section, so editing the rubric
    block moves the metric even when the same phrase recurs elsewhere; if the
    markers aren't found, coupling falls back to whole-prompt presence. The
    majority label wins; a tie or no active votes falls back to ``default`` (the
    conservative call). Returns ``(label, reason)``. Because votes depend on the
    live prompt, removing a rubric criterion drops the signals anchored to it
    and changes the prediction — which is how a rubric edit moves the metric.
    """
    low = prompt.lower()
    region_cache: dict[str, Optional[str]] = {}

    def haystack_for(label: str) -> str:
        if not sections or label not in sections:
            return low
        if label not in region_cache:
            seg = _section_text(prompt, *sections[label])
            region_cache[label] = seg.lower() if seg is not None else None
        region = region_cache[label]
        return region if region is not None else low

    tally: dict[str, int] = {}
    active: list[str] = []
    dropped: list[str] = []
    for sig in signals:
        spec = registry.get(sig)
        if not spec:
            continue
        if spec["anchor"].lower() in haystack_for(spec["label"]):
            tally[spec["label"]] = tally.get(spec["label"], 0) + 1
            active.append(sig)
        else:
            dropped.append(sig)
    if not tally:
        reason = "no active rubric signal"
        if dropped:
            reason += f" (dropped: {', '.join(dropped)})"
        return default, reason
    best = max(tally.values())
    winners = sorted(k for k, v in tally.items() if v == best)
    if len(winners) > 1:
        return default, f"tie {winners} → default {default}"
    return winners[0], f"{winners[0]} via {', '.join(active)}"


# Negative-claim constraints ANTI_CAPTURE warns against ("this tool is broken")
# — a hallmark of a low-quality, self-limiting skill.
_NEG_CLAIM = (
    "do not work", "does not work", "doesn't work", "do not function",
    "is broken", "are broken", "cannot use", "can't use", "not working",
    "never works",
)
# Incident-scoped naming: a skill keyed to one PR/issue/day is not class-level.
_INCIDENT_RE = re.compile(r"\b(pr[-\s]?\d+|#\d+|issue\s+\d+|today|yesterday)\b")


def quality_heuristic(name: str, description: str, body: str) -> tuple[str, str]:
    """Deterministic stand-in for the "is this a high-quality skill?" judge.

    Returns ``("high"|"low", reason)``. Encodes the same shape the curator /
    skill rubric optimizes for: a class-level trigger description plus a
    durable, rule-shaped body, and NOT a negative-claim constraint or an
    incident-scoped one-off. Calibrated to the human labels on the bundled
    fixtures; the LLM judge is the higher-fidelity alternative.
    """
    desc = (description or "").strip()
    text = f"{desc} {body or ''}".lower()
    if any(n in text for n in _NEG_CLAIM):
        return "low", "negative-claim constraint (ANTI_CAPTURE)"
    if _INCIDENT_RE.search(f"{name} {desc}".lower()):
        return "low", "incident-scoped name, not class-level"
    if not desc:
        return "low", "missing frontmatter trigger description"
    words = len((body or "").split())
    if words < 8:
        return "low", "body too thin to be durable"
    if words > 600:
        return "low", "body bloated beyond a focused rule"
    return "high", "class-level trigger + durable rule body"


# ──────────────────────────────────────────────────────────────────────────
# Live daemon prompts (deferred imports — keep pure functions import-light)
# ──────────────────────────────────────────────────────────────────────────

def get_shadow_prompt() -> str:
    from ..shadow_review import SHADOW_REVIEW_PROMPT
    return SHADOW_REVIEW_PROMPT


def get_candidate_prompt() -> str:
    from ..candidate_reviewer import CANDIDATE_REVIEW_PROMPT
    return CANDIDATE_REVIEW_PROMPT


def _fence(content: str, label: str) -> str:
    """Wrap an item as observed data for the LLM judge (same fence the daemons
    use, so the replay matches production framing)."""
    from ..review_prompts import fence_observed
    return fence_observed(content, label)


# ──────────────────────────────────────────────────────────────────────────
# LLM judge — replay the real prompt, parse the daemon's own verdict contract
# ──────────────────────────────────────────────────────────────────────────

class LLMJudgeUnavailable(RuntimeError):
    pass


def _anthropic_text(prompt: str, *, model: str, api_key: str,
                    timeout: float = 60.0) -> str:
    """POST a single-turn Messages request over urllib (no SDK). Raises
    :class:`LLMJudgeUnavailable` on any transport/API error."""
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
    return "".join(
        b.get("text", "") for b in payload.get("content", [])
        if b.get("type") == "text"
    )


_SHADOW_EVAL_TAIL = (
    "\n\nEVAL MODE — do NOT call any tools and do NOT write anything. Using "
    "ONLY the rubric above, classify the single dialog window below. Output "
    "exactly one line, either `MATERIALIZED: <would-be-slug>` (class-level "
    "learning worth a durable skill) or `SKIP: <reason>` (nothing "
    "class-level).\n\n"
)
_CANDIDATE_EVAL_TAIL = (
    "\n\nEVAL MODE — do NOT call any tools. Using ONLY the rubric above, "
    "classify the single candidate below. Output exactly one line, either "
    "`DECISION: ACCEPT` (a genuine note/verbatim/skill) or `DECISION: REJECT` "
    "(a false-positive that slipped past extract's noise filters).\n\n"
)
_QUALITY_PROMPT = (
    "You grade thread-keeper SKILL.md quality. A HIGH-quality skill is "
    "class-level (applies to a whole class of tasks, not one incident), has a "
    "trigger description, and a durable rule-shaped body. A LOW-quality skill "
    "is an incident-scoped one-off, a negative claim that a tool 'does not "
    "work', or too thin/bloated to be durable. Reply with ONLY a JSON object "
    '{"quality": "high"|"low", "reason": "<=12 words"}.\n\n'
    "SKILL name: {name}\nSKILL description: {description}\n\nSKILL body:\n{body}\n"
)


def _parse_shadow_verdict(text: str) -> str:
    """Last MATERIALIZED/SKIP line wins (tool prose can precede it)."""
    verdict = "skip"
    for line in text.splitlines():
        st = line.lstrip().upper()
        if st.startswith("MATERIALIZED:"):
            verdict = "materialize"
        elif st.startswith("SKIP:"):
            verdict = "skip"
    return verdict


def _parse_candidate_verdict(text: str) -> str:
    verdict = "reject"
    for line in text.splitlines():
        st = line.lstrip().upper()
        if st.startswith("DECISION: ACCEPT") or st.startswith("ACCEPT"):
            verdict = "accept"
        elif st.startswith("DECISION: REJECT") or st.startswith("REJECT"):
            verdict = "reject"
    return verdict


def _llm_shadow(item: dict, *, model: str, api_key: str) -> tuple[str, str]:
    prompt = (get_shadow_prompt() + _SHADOW_EVAL_TAIL
              + _fence(item.get("dialog", ""), "recent dialog"))
    text = _anthropic_text(prompt, model=model, api_key=api_key)
    return _parse_shadow_verdict(text), text.strip().replace("\n", " ")[:80]


def _llm_candidate(item: dict, *, model: str, api_key: str) -> tuple[str, str]:
    prompt = (get_candidate_prompt() + _CANDIDATE_EVAL_TAIL
              + _fence(item.get("content", ""), "pending candidate"))
    text = _anthropic_text(prompt, model=model, api_key=api_key)
    return _parse_candidate_verdict(text), text.strip().replace("\n", " ")[:80]


def _llm_quality(item: dict, *, model: str, api_key: str) -> tuple[str, str]:
    prompt = _QUALITY_PROMPT.format(
        name=item.get("name", ""),
        description=item.get("description", ""),
        body=(item.get("body", ""))[:4000],
    )
    text = _anthropic_text(prompt, model=model, api_key=api_key)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return "low", f"unparseable: {text[:50]!r}"
    try:
        verdict = json.loads(m.group(0))
    except json.JSONDecodeError:
        return "low", f"unparseable: {text[:50]!r}"
    q = str(verdict.get("quality", "low")).lower()
    q = "high" if q == "high" else "low"
    return q, str(verdict.get("reason", ""))[:60]


# ──────────────────────────────────────────────────────────────────────────
# Fixture loading + axis evaluation
# ──────────────────────────────────────────────────────────────────────────

def load_fixtures(fixtures_dir: Path) -> dict:
    """Load the three fixture files. Missing files yield empty lists so the
    verdict can report FAIL rather than crash."""
    out: dict[str, list] = {}
    for key, fname in (("shadow", "shadow.json"),
                       ("candidate", "candidates.json"),
                       ("quality", "skill_quality.json")):
        path = fixtures_dir / fname
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            out[key] = []
            continue
        out[key] = data.get("items", data) if isinstance(data, dict) else data
    return out


def _eval_decision_axis(items: list[dict], positive: str, default: str,
                        signals_registry: dict, sections: dict, get_prompt,
                        llm_fn, *, judge: str, model: str, api_key: str) -> dict:
    """Score one binary decision axis (shadow or candidate)."""
    rows: list[dict] = []
    prompt = get_prompt() if (judge == "rubric" and items) else ""
    for it in items:
        gold = it["label"]
        if judge == "llm":
            pred, reason = llm_fn(it, model=model, api_key=api_key)
        else:
            pred, reason = rubric_predict(
                prompt, it.get("signals", []), signals_registry, default,
                sections)
        rows.append({"id": it.get("id", "?"), "gold": gold, "pred": pred,
                     "correct": pred == gold, "reason": reason})
    metrics = binary_metrics(((r["pred"], r["gold"]) for r in rows), positive)
    classes = {r["gold"] for r in rows}
    metrics["ready"] = len(rows) >= MIN_LABELS and len(classes) >= 2
    metrics["rows"] = rows
    return metrics


def _eval_quality_axis(items: list[dict], *, judge: str, model: str,
                       api_key: str) -> dict:
    """Score the open-ended skill-quality axis as judge↔human agreement."""
    rows: list[dict] = []
    for it in items:
        human = it["label"]
        if judge == "llm":
            pred, reason = _llm_quality(it, model=model, api_key=api_key)
        else:
            pred, reason = quality_heuristic(
                it.get("name", ""), it.get("description", ""),
                it.get("body", ""))
        rows.append({"id": it.get("id", "?"), "human": human, "judge": pred,
                     "agree": pred == human, "reason": reason})
    agr = agreement((r["judge"], r["human"]) for r in rows)
    classes = {r["human"] for r in rows}
    agr["ready"] = len(rows) >= MIN_LABELS and len(classes) >= 2
    agr["rows"] = rows
    return agr


def run_eval(fixtures_dir: Path = FIXTURES_DIR, *, judge: str = "rubric",
             llm_model: str = "claude-haiku-4-5-20251001") -> dict:
    """Run all three axes and assemble the report with a PASS/PARTIAL/FAIL
    verdict on harness readiness (NOT a fixed quality threshold)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if judge == "llm" and not api_key:
        raise LLMJudgeUnavailable(
            "ANTHROPIC_API_KEY not set; rerun with --judge rubric (default).")

    fx = load_fixtures(fixtures_dir)
    shadow = _eval_decision_axis(
        fx["shadow"], "materialize", "skip", SHADOW_SIGNALS, SHADOW_SECTIONS,
        get_shadow_prompt, _llm_shadow,
        judge=judge, model=llm_model, api_key=api_key)
    candidate = _eval_decision_axis(
        fx["candidate"], "accept", "reject", CANDIDATE_SIGNALS,
        CANDIDATE_SECTIONS, get_candidate_prompt, _llm_candidate,
        judge=judge, model=llm_model, api_key=api_key)
    quality = _eval_quality_axis(
        fx["quality"], judge=judge, model=llm_model, api_key=api_key)

    ready = [shadow["ready"], candidate["ready"], quality["ready"]]
    if all(ready):
        verdict = "PASS"
    elif not any(ready):
        verdict = "FAIL"
    else:
        verdict = "PARTIAL"

    return {
        "judge": judge,
        "shadow": shadow,
        "candidate": candidate,
        "quality": quality,
        "verdict": verdict,
        "summary": _summarize(verdict, shadow, candidate, quality),
    }


def _summarize(verdict: str, shadow: dict, candidate: dict,
               quality: dict) -> str:
    def f1(m):
        return f"{m['f1']:.2f}" if m.get("f1") is not None else "n/a"

    def agr(m):
        return f"{m['accuracy']:.2f}" if m.get("accuracy") is not None else "n/a"

    return (
        f"{verdict}: shadow_F1={f1(shadow)} candidate_F1={f1(candidate)} "
        f"quality_agreement={agr(quality)} (n={quality.get('n', 0)})"
    )


# ──────────────────────────────────────────────────────────────────────────
# Reporting
# ──────────────────────────────────────────────────────────────────────────

def _fmt_pct(x) -> str:
    return f"{x:.1%}" if isinstance(x, (int, float)) else "n/a"


def _decision_block(title: str, m: dict) -> list[str]:
    out = [f"  {title}  (positive='{m['positive']}', n={m['n']}"
           f"{'' if m['ready'] else ', THIN'})"]
    out.append(
        f"    precision={_fmt_pct(m['precision'])}  "
        f"recall={_fmt_pct(m['recall'])}  f1={_fmt_pct(m['f1'])}  "
        f"acc={_fmt_pct(m['accuracy'])}"
    )
    out.append(
        f"    confusion: tp={m['tp']} fp={m['fp']} fn={m['fn']} tn={m['tn']}")
    wrong = [r for r in m["rows"] if not r["correct"]]
    if wrong:
        out.append(f"    misclassified ({len(wrong)}):")
        for r in wrong:
            out.append(
                f"      ✗ {r['id']:<22} gold={r['gold']} pred={r['pred']}"
                f"  {r['reason']}")
    return out


def format_report(report: dict) -> str:
    out: list[str] = []
    out.append("── learning-loop decision-quality eval ───────────────────")
    out.append(f"judge={report['judge']}")
    out.append("")
    out.append("shadow-review decisions (materialize vs skip):")
    out.extend(_decision_block("shadow", report["shadow"]))
    out.append("")
    out.append("candidate-reviewer decisions (accept vs reject):")
    out.extend(_decision_block("candidate", report["candidate"]))
    out.append("")
    q = report["quality"]
    out.append("skill-quality judge ↔ human agreement (calibration):")
    out.append(
        f"  agreement={_fmt_pct(q['accuracy'])}  kappa="
        f"{q['kappa'] if q['kappa'] is not None else 'n/a'}  n={q['n']}"
        f"{'' if q['ready'] else '  (THIN)'}")
    disagree = [r for r in q["rows"] if not r["agree"]]
    if disagree:
        out.append(f"  disagreements ({len(disagree)}):")
        for r in disagree:
            out.append(
                f"    ✗ {r['id']:<22} human={r['human']} judge={r['judge']}"
                f"  {r['reason']}")
    out.append("")
    out.append(f"  VERDICT: {report['verdict']}")
    out.append(f"  {report['summary']}")
    out.append("──────────────────────────────────────────────────────────")
    return "\n".join(out)


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    # Defensive: this is a read-only offline measurement, never a live session.
    # Setting the kill-switch before any threadkeeper import means importing the
    # daemon prompt modules can never start background work.
    os.environ.setdefault("THREADKEEPER_DISABLE_BG_DAEMONS", "1")

    ap = argparse.ArgumentParser(
        prog="python -m threadkeeper.eval",
        description="Offline eval harness for learning-loop decision quality "
                    "(issue #72): precision/recall/F1 for shadow-review and "
                    "candidate decisions, plus calibrated judge↔human "
                    "agreement on skill quality.")
    ap.add_argument("--judge", choices=("rubric", "llm"), default="rubric",
                    help="rubric (default, offline, deterministic) or llm "
                         "(replays the real prompt; needs ANTHROPIC_API_KEY).")
    ap.add_argument("--llm-model", default="claude-haiku-4-5-20251001",
                    help="model id for --judge llm.")
    ap.add_argument("--fixtures-dir", type=Path, default=FIXTURES_DIR,
                    help="fixture directory (default: bundled golden set).")
    ap.add_argument("--json", action="store_true",
                    help="emit the full report as JSON instead of a table.")
    args = ap.parse_args(argv)

    try:
        report = run_eval(args.fixtures_dir, judge=args.judge,
                          llm_model=args.llm_model)
    except LLMJudgeUnavailable as e:
        print(f"ERR: {e}", file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report(report))
    # Exit non-zero only when the harness itself is broken (no fixtures /
    # nothing computable), never on model quality — quality is a number to
    # track, not a gate.
    return 0 if report["verdict"] in ("PASS", "PARTIAL") else 1


if __name__ == "__main__":
    raise SystemExit(main())
