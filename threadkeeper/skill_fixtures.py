"""Dry-run-first migration for explicitly selected hidden skill fixtures."""
from __future__ import annotations

import argparse
import json
import sys

from .tools.skills import apply_fixture_migration, fixture_migration_plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tk-skill-fixtures",
        description=(
            "Classify explicitly named managed skills as hidden fixtures. "
            "The default dry-run never changes a skill."
        ),
    )
    parser.add_argument(
        "--name",
        action="append",
        default=[],
        help="managed skill name to review (repeatable; never inferred from a name pattern)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="move reviewed candidate skills into hidden fixture storage",
    )
    args = parser.parse_args(argv)
    if args.apply and not args.name:
        parser.error("--apply requires at least one explicit --name")

    plan = fixture_migration_plan(args.name)
    if not args.apply:
        print(json.dumps({"dry_run": True, "candidates": plan}, indent=2, sort_keys=True))
        return 0

    outcomes = apply_fixture_migration(args.name)
    print(json.dumps({"dry_run": False, "candidates": outcomes}, indent=2, sort_keys=True))
    succeeded = all(
        item["status"] == "already_fixture"
        or item.get("result", "").startswith("ok")
        for item in outcomes
    )
    return 0 if succeeded else 2


if __name__ == "__main__":  # pragma: no cover - console entry point
    sys.exit(main())
