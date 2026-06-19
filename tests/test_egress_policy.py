"""Cross-provider memory egress policy (issue #74).

thread-keeper renders the most sensitive memory it holds — verbatim user quotes
and the dialectic user-model — into every brief(), and brief() is consumed by
whatever LLM vendor backs the active/spawned CLI. These tests pin the control
layer that scopes that egress:

  * the static policy resolver (`egress`): class map, CLI→vendor map, and the
    `personal_allowed(consumer, policy)` decision per policy × consumer;
  * `render_brief`'s personal-section gating under each policy value × consumer,
    including default-mode (`all`) byte-parity;
  * spawn-target → policy resolution (a spawn to a third-party CLI gates the
    same way that CLI's brief would);
  * the env override being honored live (hot-reload).
"""
from __future__ import annotations

import os

import pytest

from threadkeeper import egress
from threadkeeper.brief import render_brief


@pytest.fixture(autouse=True)
def _clean_egress_env():
    """reload_settings() mutates os.environ in place, so a policy set by one
    test leaks into the next (and into the pure resolver tests). Scrub the
    egress knobs before and after every test in this module."""
    keys = ("THREADKEEPER_MEMORY_EGRESS", "THREADKEEPER_EGRESS_CONSUMER")
    for k in keys:
        os.environ.pop(k, None)
    yield
    for k in keys:
        os.environ.pop(k, None)


# ── helpers ───────────────────────────────────────────────────────────────────

def _tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _seed_personal(pkg):
    """Seed one verbatim quote + one validated dialectic claim so both
    personal-class brief sections (`verbatim`, `user_model`) render."""
    verbatim_user = _tool(pkg, "verbatim_user")
    claim = _tool(pkg, "dialectic_claim")
    evidence = _tool(pkg, "dialectic_evidence")

    verbatim_user(content="this is a confidential quote to claude")
    res = claim(claim="prefers terse, source-grounded answers", domain="style")
    claim_id = next(p[3:] for p in res.split() if p.startswith("id="))
    for _ in range(5):
        evidence(claim_id=claim_id, kind="support", quote="terse")


def _set_policy(pkg, value: str):
    pkg["config"].reload_settings({"THREADKEEPER_MEMORY_EGRESS": value})


# ── static resolver ───────────────────────────────────────────────────────────

def test_vendor_map_covers_all_supported_clis():
    assert egress.vendor_for("claude") == "anthropic"
    assert egress.vendor_for("codex") == "openai"
    assert egress.vendor_for("gemini") == "google"
    assert egress.vendor_for("antigravity") == "google"
    assert egress.vendor_for("agy") == "google"          # alias
    assert egress.vendor_for("copilot") == "microsoft"
    assert egress.vendor_for("unknown-cli") is None


def test_normalize_policy_aliases_and_fallback():
    assert egress.normalize_policy("same_vendor") == "same-vendor"
    assert egress.normalize_policy("WORK-ONLY") == "work-only"
    assert egress.normalize_policy("  all ") == "all"
    # Unknown / empty → permissive default (fail-open, don't regress product).
    assert egress.normalize_policy("nonsense") == "all"
    assert egress.normalize_policy("") == "all"
    assert egress.normalize_policy(None) == "all"


def test_category_class_map():
    assert egress.class_for("verbatim_user") == egress.PERSONAL
    assert egress.class_for("dialectic") == egress.PERSONAL
    assert egress.class_for("threads") == egress.WORK
    assert egress.class_for("lessons") == egress.SHARED
    # Unknown category → neutral WORK (never silently SHARED).
    assert egress.class_for("something-new") == egress.WORK


@pytest.mark.parametrize(
    "policy,consumer,expected",
    [
        # all → everything egresses everywhere
        ("all", "claude", True),
        ("all", "codex", True),
        ("all", "gemini", True),
        ("all", "copilot", True),
        # same-vendor → personal only to the native vendor (Anthropic/Claude)
        ("same-vendor", "claude", True),
        ("same-vendor", "codex", False),
        ("same-vendor", "gemini", False),
        ("same-vendor", "antigravity", False),
        ("same-vendor", "copilot", False),
        ("same-vendor", None, True),     # undetected consumer → fail-open
        # work-only → personal never egresses, any vendor incl. Claude
        ("work-only", "claude", False),
        ("work-only", "codex", False),
        ("work-only", None, False),
    ],
)
def test_personal_allowed_matrix(policy, consumer, expected):
    assert egress.personal_allowed(consumer, policy) is expected


# ── render_brief gating ───────────────────────────────────────────────────────

def test_default_all_renders_personal_for_every_consumer(mp_with_cid):
    """Default policy = no gating: personal sections present regardless of the
    consuming CLI, and output is identical across consumers (byte-parity)."""
    pkg = mp_with_cid("aaaaaaaa-0000-0000-0000-000000000001")
    _seed_personal(pkg)
    conn = pkg["db"].get_db()

    base = render_brief(conn)  # no consumer override
    for consumer in ("claude", "codex", "gemini", "copilot"):
        txt = render_brief(conn, consumer_cli=consumer)
        assert "verbatim" in txt
        assert "user_model" in txt
        assert "withheld" not in txt
        # default 'all' ignores the consumer entirely → byte-identical
        assert txt == base


def test_same_vendor_omits_personal_for_third_party(mp_with_cid):
    pkg = mp_with_cid("aaaaaaaa-0000-0000-0000-000000000002")
    _seed_personal(pkg)
    conn = pkg["db"].get_db()
    _set_policy(pkg, "same-vendor")

    claude_txt = render_brief(conn, consumer_cli="claude")
    assert "verbatim" in claude_txt
    assert "user_model" in claude_txt

    for third_party in ("codex", "gemini", "antigravity", "copilot"):
        txt = render_brief(conn, consumer_cli=third_party)
        assert "verbatim (reactivated first)" not in txt
        assert "user_model (dialectic)" not in txt
        # disclosure: tell the consumer personal memory exists but was withheld
        assert "withheld" in txt
        assert f"policy=same-vendor" in txt


def test_work_only_omits_personal_for_every_consumer(mp_with_cid):
    pkg = mp_with_cid("aaaaaaaa-0000-0000-0000-000000000003")
    _seed_personal(pkg)
    conn = pkg["db"].get_db()
    _set_policy(pkg, "work-only")

    for consumer in ("claude", "codex", "gemini", "copilot"):
        txt = render_brief(conn, consumer_cli=consumer)
        assert "verbatim (reactivated first)" not in txt
        assert "user_model (dialectic)" not in txt
        assert "withheld" in txt


def test_consumer_resolved_from_env_when_unset(mp_with_cid, monkeypatch):
    """When render_brief gets no explicit consumer, it resolves the spawn-set
    THREADKEEPER_EGRESS_CONSUMER env (the deterministic spawn path)."""
    pkg = mp_with_cid("aaaaaaaa-0000-0000-0000-000000000004")
    _seed_personal(pkg)
    conn = pkg["db"].get_db()
    _set_policy(pkg, "same-vendor")

    monkeypatch.setenv("THREADKEEPER_EGRESS_CONSUMER", "codex")
    txt = render_brief(conn)  # consumer_cli=None → env
    assert "user_model (dialectic)" not in txt
    assert "withheld" in txt


# ── spawn-target → policy resolution ──────────────────────────────────────────

def test_spawn_target_resolution_matches_brief_gate(fresh_mp):
    """A spawn to a third-party-CLI role resolves to that CLI; the egress gate
    for that resolved target matches what its brief would render. Pinning a
    role to codex under same-vendor must drop personal for that child."""
    # Re-import against the fixture's fresh package so current_policy() reads
    # the reloaded config (the top-level `egress` binding is stale post-wipe).
    from threadkeeper import spawn_config
    from threadkeeper import egress as egr

    fresh_mp["config"].reload_settings({
        "THREADKEEPER_SPAWN__LOOP__SHADOW_OBSERVER": "codex",
        "THREADKEEPER_MEMORY_EGRESS": "same-vendor",
    })
    target = spawn_config.resolve_agent("shadow_observer", active_cli="claude")
    assert target == "codex"
    # The child consuming under this target gets personal withheld.
    assert egr.personal_allowed(target, egr.current_policy()) is False

    # A claude-resolved role keeps personal under same-vendor.
    claude_target = spawn_config.resolve_agent("archivist", active_cli="claude")
    assert claude_target == "claude"
    assert egr.personal_allowed(claude_target, egr.current_policy()) is True


def test_env_override_beats_dotenv_default(fresh_mp):
    """A real THREADKEEPER_MEMORY_EGRESS override is honored over the default."""
    from threadkeeper import egress as egr

    assert egr.current_policy() == "all"
    fresh_mp["config"].reload_settings({"THREADKEEPER_MEMORY_EGRESS": "work-only"})
    assert egr.current_policy() == "work-only"
    fresh_mp["config"].reload_settings(remove=["THREADKEEPER_MEMORY_EGRESS"])
    assert egr.current_policy() == "all"
