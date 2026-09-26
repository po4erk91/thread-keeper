from pathlib import Path


def test_ci_uses_the_parallel_xdist_runner():
    workflow = Path(__file__).parents[1] / ".github" / "workflows" / "test.yml"
    pytest_commands = [
        line.strip()
        for line in workflow.read_text().splitlines()
        if line.strip().startswith("python -m pytest")
    ]

    assert pytest_commands == [
        "python -m pytest -q -n auto --dist loadscope --tb=short",
    ]
