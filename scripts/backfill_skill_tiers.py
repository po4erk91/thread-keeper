#!/usr/bin/env python3
"""One-shot backfill: recompute foreground_use_count + tier for every skill.

Before the passive-tier fix, the ingest skill scanner only ever bumped
`use_count`, never `foreground_use_count`, so every skill stayed stuck at
tier='hypothesis'. This script repairs the historical record:

  1. Re-scan ALL installed-CLI transcripts for Skill tool_use invocations.
  2. Tally, per skill, the count of GENUINE foreground uses — excluding
     spawned review-fork child sessions (tasks.spawned_cid), exactly as the
     live path now does. Dedup by message uuid so re-runs are stable.
  3. Overwrite skill_usage.foreground_use_count with the tally and recompute
     each skill's tier via the canonical state machine.

Usage:
    python -m scripts.backfill_skill_tiers            # dry-run (prints plan)
    python -m scripts.backfill_skill_tiers --apply    # write changes

Safe to re-run; idempotent (recomputes from scratch each time).
"""
from __future__ import annotations

import sys

from threadkeeper.db import get_db
from threadkeeper.ingest import (
    _is_spawned_child_session,
    _scan_message_for_skill_use,
)
from threadkeeper.tools.skills import _recompute_skill_tier


def _tally_foreground_uses(conn) -> dict[str, int]:
    """Return {skill_name: foreground_use_count} from a full transcript scan."""
    from threadkeeper.adapters import installed_adapters

    seen_uuids: set[str] = set()
    counts: dict[str, int] = {}
    for adapter in installed_adapters():
        for fp in adapter.transcript_files():
            try:
                for nm in adapter.iter_messages(fp):
                    if nm.role != "assistant" or not nm.uuid:
                        continue
                    if nm.uuid in seen_uuids:
                        continue
                    skills = _scan_message_for_skill_use(nm.raw)
                    if not skills:
                        continue
                    seen_uuids.add(nm.uuid)
                    if _is_spawned_child_session(conn, nm.session_id):
                        continue  # review-fork use does not promote
                    for name in skills:
                        counts[name] = counts.get(name, 0) + 1
            except OSError:
                continue
    return counts


def main(apply: bool) -> int:
    conn = get_db()
    counts = _tally_foreground_uses(conn)
    existing = {
        r["name"]: (r["tier"], r["foreground_use_count"])
        for r in conn.execute(
            "SELECT name, tier, foreground_use_count FROM skill_usage"
        ).fetchall()
    }

    print(f"scanned skills with foreground uses: {len(counts)}")
    print(f"skills currently in skill_usage:     {len(existing)}")
    print()

    import time
    now = int(time.time())
    changes = []
    for name, fg in sorted(counts.items(), key=lambda kv: -kv[1]):
        old_tier, old_fg = existing.get(name, ("(absent)", 0))
        if apply:
            conn.execute(
                "INSERT INTO skill_usage (name, created_at, foreground_use_count) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET foreground_use_count=excluded.foreground_use_count",
                (name, now, fg),
            )
            # _recompute_skill_tier advances ONE tier step per call
            # (hypothesis→observed→validated). The incremental live path
            # converges naturally over many uses; a one-shot backfill must
            # iterate to a fixpoint so e.g. fg=17 reaches validated, not just
            # observed. Loop until a call reports no change (bounded).
            new = old_tier
            for _ in range(5):
                old_t, new_t = _recompute_skill_tier(conn, name, now)
                new = new_t
                if new_t == old_t:
                    break
        else:
            # predict tier without writing
            new = "(dry-run)"
            old = old_tier
        changes.append((name, old_fg, fg, old_tier, new))

    if apply:
        conn.commit()

    print(f"{'skill':<52} fg:old→new  tier:old→new")
    print("-" * 88)
    for name, old_fg, fg, old_tier, new in changes:
        print(f"{name:<52} {old_fg:>3}→{fg:<3}    {old_tier} → {new}")

    if not apply:
        print("\n(dry-run — re-run with --apply to write)")
    else:
        promoted = sum(
            1 for _, _, _, ot, nt in changes
            if nt not in (ot, "(dry-run)") and nt != "hypothesis"
        )
        print(f"\napplied. skills promoted above hypothesis: {promoted}")
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))
