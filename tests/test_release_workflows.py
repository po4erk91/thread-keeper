from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _workflow_text(name: str) -> str:
    return (WORKFLOWS / name).read_text()


def test_release_readiness_is_read_only_and_does_not_publish():
    data = _workflow("release-tag.yml")
    text = _workflow_text("release-tag.yml")

    assert data["permissions"] == {"contents": "read"}
    assert "actions: write" not in text
    assert "contents: write" not in text
    assert "gh workflow run publish.yml" not in text
    assert 'git push origin "refs/tags/$TAG"' not in text
    assert "Report maintainer release action" in text


def test_publish_requires_signed_tag_and_pypi_environment():
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
    assert "Verify signed annotated tag" in text
    assert ".verification.verified" in text
    assert "must be an annotated tag" in text
    assert "Validate release metadata" in text


def test_releasing_docs_describe_the_approval_flow():
    docs = (ROOT / "docs" / "RELEASING.md").read_text()

    assert "does **not** create a tag" in docs
    assert "Add at least one **Required reviewer**" in docs
    assert "git tag -s v0.4.1 origin/main" in docs
    assert "The output must include a `required_reviewers` rule" in docs
