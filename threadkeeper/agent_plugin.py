"""Generate and check the portable Agent Plugins bundle without host writes."""
from __future__ import annotations

import argparse
from importlib.metadata import version as package_version
import json
from pathlib import Path

from .instructions import BOOTSTRAP_SKILL
from .setup_metadata import SERVER_NAME, portable_server

SCHEMA_ROOT = "https://agent-plugins.org/schemas/1.0.0"
BUNDLE_ROOT = Path(__file__).resolve().parent / "assets" / "agent-plugin"


def bundle_files(version: str) -> dict[str, str]:
    manifests = {
        "plugin.json": {
            "$schema": f"{SCHEMA_ROOT}/plugin.schema.json",
            "name": SERVER_NAME,
            "version": version,
            "description": "Persistent working memory across agent conversations.",
            "repository": "https://github.com/po4erk91/thread-keeper",
            "license": "MIT",
        },
        "mcp.json": {
            "$schema": f"{SCHEMA_ROOT}/mcp.schema.json",
            "mcpServers": {SERVER_NAME: portable_server(version)},
        },
    }
    return {
        **{name: json.dumps(value, indent=2) + "\n" for name, value in manifests.items()},
        f"skills/{SERVER_NAME}/SKILL.md": BOOTSTRAP_SKILL,
    }


def check_bundle(root: Path, version: str) -> list[str]:
    expected = bundle_files(version)
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    errors = [f"unexpected file: {name}" for name in sorted(actual - expected.keys())]
    for name, content in expected.items():
        path = root / name
        if not path.is_file():
            errors.append(f"missing file: {name}")
        elif path.read_text(encoding="utf-8") != content:
            errors.append(f"stale file: {name}")
    return errors


def write_bundle(root: Path, version: str, *, dry_run: bool = False) -> list[str]:
    from .config_io import atomic_write_text

    names = []
    for name, content in bundle_files(version).items():
        if not dry_run:
            atomic_write_text(root / name, content)
        names.append(name)
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=BUNDLE_ROOT)
    parser.add_argument("--check", action="store_true", help="Fail on missing, stale or extra files.")
    args = parser.parse_args(argv)
    version = package_version("threadkeeper")
    if args.check:
        errors = check_bundle(args.output, version)
        print("\n".join(errors) if errors else "Agent Plugins bundle is current.")
        return int(bool(errors))
    for name in write_bundle(args.output, version):
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
