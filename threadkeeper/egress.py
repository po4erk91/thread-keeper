"""Cross-provider memory egress policy (issue #74).

thread-keeper is "one user model … shared across CLIs" by design, and that
sharing is the point. But the most sensitive memory it holds — `verbatim_user`
quotes and the `dialectic` user-model (claims *about* the user) — is rendered
into every `brief()`, and `brief()` is consumed by whatever LLM vendor backs
the active or spawned CLI. A quote said in confidence to Claude, or a dialectic
claim inferred about the user, can therefore be transmitted to OpenAI / Google /
Microsoft on the next spawn or session-start under a third-party CLI.

This module is the cheap, static control layer over that flow:

  * a category → sensitivity-class map (`personal` / `work` / `shared`),
  * a CLI → backing-vendor map, and
  * a policy resolver that answers "may a `personal`-class section render into a
    brief consumed by this CLI's vendor, under the active policy?"

The policy is the `THREADKEEPER_MEMORY_EGRESS` knob (read live from
`config.settings`, so hot-reload applies):

  all          (default, current behavior) — no gating; everything egresses
               everywhere, byte-identical to pre-#74.
  same-vendor  — `personal`-class memory renders only for the native vendor
               (Anthropic / Claude — thread-keeper's brief is Claude-native and
               personal memory is authored in Claude sessions). Third-party
               vendors (OpenAI / Google / Microsoft) get those sections omitted.
  work-only    — `personal`-class memory never renders, for any vendor
               (including Claude). Maximum-privacy mode: only `work`/`shared`
               memory leaves the machine.

`work` and `shared` classes always egress; only `personal` is gated. The gate
is intentionally fail-open: an unknown policy string or an undetected consumer
falls back to allowing personal, so a typo or a ppid-detection miss never
silently strips the user's own brief. Spawn passes the resolved target CLI to
the child via `THREADKEEPER_EGRESS_CONSUMER` so the gate is deterministic on the
spawn path rather than relying on the child's own ppid walk.
"""
from __future__ import annotations

from typing import Optional

from . import config

# ── Sensitivity classes ──────────────────────────────────────────────────────
PERSONAL = "personal"   # verbatim quotes, dialectic user-model — about the user
WORK = "work"           # threads, notes, tasks — what the user is working on
SHARED = "shared"       # skills, lessons, concepts — reusable, subject-agnostic

# Static memory-category → sensitivity-class map. Cheap (no per-row column);
# the brief renderer keys section gating off these classes.
CATEGORY_CLASS: dict[str, str] = {
    "verbatim": PERSONAL,
    "verbatim_user": PERSONAL,
    "dialectic": PERSONAL,
    "user_model": PERSONAL,
    "currently_testing": PERSONAL,
    "threads": WORK,
    "notes": WORK,
    "tasks": WORK,
    "skills": SHARED,
    "lessons": SHARED,
    "concepts": SHARED,
}

# ── CLI → backing LLM vendor ──────────────────────────────────────────────────
# Antigravity is Google's; copilot is Microsoft/GitHub. Aliases (agy) are
# normalized before lookup.
CLI_VENDOR: dict[str, str] = {
    "claude": "anthropic",
    "codex": "openai",
    # Historical transcripts retain this source tag after the legacy adapter
    # was removed. Keep classification so old rows remain policy-gated.
    "gemini": "google",
    "antigravity": "google",
    "copilot": "microsoft",
}
_CLI_ALIASES = {"agy": "antigravity"}

# The vendor thread-keeper's brief is native to. Personal memory is authored in
# Claude sessions and the brief format is Claude-native, so Anthropic is the
# first-party vendor; every other vendor is "third-party" for egress purposes.
NATIVE_VENDOR = "anthropic"

POLICIES = ("all", "same-vendor", "work-only")
DEFAULT_POLICY = "all"

# Accept a few friendly spellings of the restricted modes; anything else
# normalizes to the permissive default (fail-open — don't regress the product).
_POLICY_ALIASES = {
    "same_vendor": "same-vendor",
    "samevendor": "same-vendor",
    "work_only": "work-only",
    "workonly": "work-only",
}


def normalize_policy(value: Optional[str]) -> str:
    """Canonicalize a raw policy string to one of POLICIES.

    Unknown / empty values fall back to the permissive default so a typo can
    never silently break the brief; restricted modes must be spelled correctly
    (or via a known alias) to take effect.
    """
    p = (value or "").strip().lower()
    p = _POLICY_ALIASES.get(p, p)
    return p if p in POLICIES else DEFAULT_POLICY


def current_policy() -> str:
    """The active egress policy, read live from config (hot-reload aware)."""
    return normalize_policy(getattr(config.settings, "memory_egress", DEFAULT_POLICY))


def vendor_for(cli: Optional[str]) -> Optional[str]:
    """Backing LLM vendor for a CLI short-name (alias-aware), or None."""
    c = (cli or "").strip().lower()
    c = _CLI_ALIASES.get(c, c)
    return CLI_VENDOR.get(c)


def class_for(category: str) -> str:
    """Sensitivity class for a memory category. Unknown → WORK (the neutral,
    always-egressed middle class — never silently treated as SHARED, never
    silently leaked as if it were nothing)."""
    return CATEGORY_CLASS.get((category or "").strip().lower(), WORK)


def personal_allowed(consumer_cli: Optional[str], policy: Optional[str] = None) -> bool:
    """May `personal`-class brief sections render for this consuming CLI?

    Resolution:
      all          → always True (default; no gating).
      work-only    → always False (personal never egresses, any vendor).
      same-vendor  → True only when the consumer's vendor is the native vendor
                     (Anthropic / Claude); third-party vendors → False.

    Fail-open: an undetected consumer (vendor_for → None) is treated as native
    under same-vendor, so a ppid-detection miss never strips the foreground
    user's own brief. Spawned third-party children always carry an explicit
    target CLI, so they gate correctly regardless.
    """
    pol = normalize_policy(policy) if policy is not None else current_policy()
    if pol == "all":
        return True
    if pol == "work-only":
        return False
    # same-vendor
    vendor = vendor_for(consumer_cli)
    if vendor is None:
        return True  # unknown consumer → don't strip (fail-open)
    return vendor == NATIVE_VENDOR
