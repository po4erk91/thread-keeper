"""Validate the generated bundle against pinned or published Agent Plugins schemas."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tomllib
from urllib.request import urlopen

from jsonschema import Draft202012Validator

from threadkeeper.agent_plugin import BUNDLE_ROOT, SCHEMA_ROOT, check_bundle

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "tests" / "schemas" / "agent-plugins" / "1.0.0"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--published", action="store_true", help="Also check the published schema copies.")
    args = parser.parse_args()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    errors = check_bundle(BUNDLE_ROOT, version)
    if errors:
        raise SystemExit("\n".join(errors))
    for name in ("plugin", "mcp"):
        schema = json.loads((SCHEMAS / f"{name}.schema.json").read_text())
        if args.published:
            with urlopen(f"{SCHEMA_ROOT}/{name}.schema.json", timeout=30) as response:
                published = json.load(response)
            if schema != published:
                raise SystemExit(f"Published {name} schema differs from the pinned copy.")
            schema = published
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(json.loads((BUNDLE_ROOT / f"{name}.json").read_text()))
    print("Agent Plugins manifests and installer metadata agree.")


if __name__ == "__main__":
    main()
