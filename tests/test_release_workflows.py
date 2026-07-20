from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

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
