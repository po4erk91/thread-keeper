from pathlib import Path
import tomllib

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
DOCKERFILE = ROOT / "Dockerfile"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
PIP_AUDIT_IGNORES = ROOT / ".github" / "pip-audit-ignores.txt"

BOT_TAGGER_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _workflow_text(name: str) -> str:
    return (WORKFLOWS / name).read_text()


def test_release_tag_escalates_only_inside_the_tag_job():
    data = _workflow("release-tag.yml")
    text = _workflow_text("release-tag.yml")

    # Workflow-level permissions stay read-only; only the tag job may
    # create the tag ref and dispatch publish.yml.
    assert data["permissions"] == {"contents": "read"}
    tag_job = data["jobs"]["tag"]
    assert tag_job["permissions"] == {"contents": "write", "actions": "write"}

    # Fires only after a successful tests run for a push to main.
    assert 'workflows: ["tests"]' in text
    assert "branches: [main]" in text
    assert "conclusion == 'success'" in tag_job["if"]
    assert "event == 'push'" in tag_job["if"]


def test_release_tag_gates_on_version_and_changelog_before_dispatch():
    text = _workflow_text("release-tag.yml")

    # Never re-tags an existing version, never tags without release
    # notes, creates an annotated bot tag via the API, and hands off to
    # publish.yml explicitly (GITHUB_TOKEN tag pushes don't start
    # push-triggered runs).
    assert "git ls-remote --exit-code --tags origin" in text
    assert "^## v${VERSION} " in text
    assert "git/tags" in text
    assert "refs/tags/$TAG" in text
    assert BOT_TAGGER_EMAIL in text
    assert "gh workflow run publish.yml" in text


def test_publish_requires_signed_or_bot_main_tag_and_pypi_environment():
    data = _workflow("publish.yml")
    text = _workflow_text("publish.yml")
    jobs = data["jobs"]

    assert data["permissions"] == {"contents": "read"}
    assert jobs["build"]["needs"] == "authorize"
    assert jobs["publish-pypi"]["needs"] == "build"
    assert jobs["publish-pypi"]["environment"]["name"] == "pypi"
    assert jobs["publish-pypi"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
    }

    assert "Require v* tag ref" in text
    assert "Verify release tag" in text
    assert ".verification.verified" in text
    assert "must be an annotated tag" in text
    assert "Validate release metadata" in text

    # The unsigned path is narrow: explicit dispatch + github-actions[bot]
    # tagger + tag commit already merged to main. Pushed unsigned tags
    # must keep failing authorization.
    assert '"$EVENT" != "workflow_dispatch"' in text
    assert BOT_TAGGER_EMAIL in text
    assert "compare/main..." in text


def test_releasing_docs_describe_the_approval_flow():
    docs = (ROOT / "docs" / "RELEASING.md").read_text()

    assert "release-tag.yml" in docs
    assert "Add at least one **Required reviewer**" in docs
    assert "The output must include a `required_reviewers` rule" in docs
    # The manual signed-tag path stays documented as backfill/override.
    assert "git tag -s" in docs
    assert "Dockerfile Glama-eval pin" in docs


def test_glama_dockerfile_pin_matches_the_project_release():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]

    assert f"threadkeeper=={version}" in DOCKERFILE.read_text()


def test_mcp_requirement_is_capped_while_the_1x_import_path_is_used():
    # mcp 2.0.0 renamed `mcp.server.fastmcp` -> `mcp.server.mcpserver`
    # (FastMCP -> MCPServer) with no shim, so an unbounded `mcp>=1.10.0`
    # made every fresh resolve die at import. Keep the cap and the import
    # path in lockstep: whoever migrates to the 2.x API drops both.
    importers = [
        ROOT / "threadkeeper" / "_mcp.py",
        ROOT / "threadkeeper" / "elicitation.py",
        ROOT / "threadkeeper" / "tools" / "dialectic.py",
    ]
    if not any("mcp.server.fastmcp" in p.read_text() for p in importers):
        return  # migrated to the 2.x API — the cap is free to go.

    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    mcp_req = next(d for d in deps if d.split(">")[0].split("<")[0].strip() == "mcp")

    assert "<2" in mcp_req, f"mcp requirement must exclude 2.x, got {mcp_req!r}"


def test_ci_security_scanning_covers_code_and_resolved_dependencies():
    codeql = _workflow("codeql.yml")
    codeql_text = _workflow_text("codeql.yml")
    pip_audit_job = _workflow("test.yml")["jobs"]["pip-audit"]
    dependabot = yaml.safe_load(DEPENDABOT.read_text())

    assert codeql["permissions"] == {
        "contents": "read",
        "security-events": "write",
    }
    assert "branches: [main]" in codeql_text
    assert "cron:" in codeql_text
    assert "github/codeql-action/init@v4" in codeql_text
    assert "github/codeql-action/analyze@v4" in codeql_text
    assert "languages: python" in codeql_text
    assert "build-mode: none" in codeql_text
    assert "queries:" not in codeql_text  # Keep the default suite for now.

    audit_text = _workflow_text("test.yml")
    assert pip_audit_job["name"] == "pip-audit (resolved dependencies)"
    assert "python -m pip install -e '.[semantic,dev]'" in audit_text
    assert "pip-audit --local --strict" in audit_text
    assert "--ignore-vuln" in audit_text
    assert "Malformed .github/pip-audit-ignores.txt entry" in audit_text
    assert "There are no active suppressions at present." in PIP_AUDIT_IGNORES.read_text()

    ecosystems = {entry["package-ecosystem"] for entry in dependabot["updates"]}
    assert "docker" in ecosystems
