"""Keep README and architecture inventory claims in step with the code (#278).

Exact test and tool totals drifted every release, so the docs no longer carry
them. The tool table in docs/ARCHITECTURE.md stays, but it is checked against
the live registry, so a new tool fails here with the row to update instead of
leaving the table silently stale.
"""

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


def _architecture_tool_table() -> set[str]:
    text = (ROOT / "docs" / "ARCHITECTURE.md").read_text()
    section = text[text.index("## MCP tools\n"):]
    table = section[section.index("| Module | Tools |"):]
    table = table[:table.index("\n\n")]
    listed: set[str] = set()
    for line in table.splitlines()[2:]:
        _module, tools = [c.strip() for c in line.strip().strip("|").split("|")]
        listed |= {t.strip() for t in tools.split(",") if t.strip()}
    return listed


def test_architecture_tool_table_matches_the_registry(fresh_mp):
    registered = {t.name for t in fresh_mp["mcp"]._tool_manager.list_tools()}
    listed = _architecture_tool_table()

    assert registered == listed, (
        "Update the MCP tools table in docs/ARCHITECTURE.md: "
        f"missing={sorted(registered - listed)} "
        f"not_registered={sorted(listed - registered)}"
    )
