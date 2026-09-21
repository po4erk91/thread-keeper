"""Keep README and architecture inventory claims independent of mutable totals."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
INVENTORY_DOCS = (ROOT / "README.md", ROOT / "docs" / "ARCHITECTURE.md")
MANUAL_TOTAL_PATTERNS = (
    re.compile(r"\b\d[\d,]*\s+tests?\b", re.IGNORECASE),
    re.compile(r"\b(?:all|currently)\s+\d[\d,]*\s+(?:MCP\s+)?tools?\b", re.IGNORECASE),
    re.compile(r"\bMCP\s+tools?\s*\(\d[\d,]*\s+total\)", re.IGNORECASE),
    re.compile(r"\b(?:entries|tools)\s*(?:—|-)\s*\d[\d,]*\s+of\s+them\b", re.IGNORECASE),
    re.compile(r"^\|\s*Module\s*\|\s*N\s*\|\s*Tools\s*\|$", re.MULTILINE),
)


def test_inventory_docs_do_not_reintroduce_hand_maintained_totals():
    """New tests and tools must not require manually changing documentation."""
    violations = [
        f"{path.relative_to(ROOT)}: {pattern.pattern}"
        for path in INVENTORY_DOCS
        for pattern in MANUAL_TOTAL_PATTERNS
        if pattern.search(path.read_text())
    ]

    assert not violations, (
        "Do not add hand-maintained test or MCP tool totals to README or "
        "docs/ARCHITECTURE.md. Use suite or registry wording instead:\n"
        + "\n".join(violations)
    )
