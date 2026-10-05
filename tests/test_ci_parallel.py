"""CI parallelism: deterministic --forked shards instead of xdist workers."""
import importlib.util
from pathlib import Path

import yaml

_spec = importlib.util.spec_from_file_location(
    "_tk_conftest", Path(__file__).with_name("conftest.py"),
)
_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_conftest)
shard_of = _conftest.shard_of


def test_shards_partition_every_test_exactly_once():
    ids = [f"tests/test_mod_{i % 37}.py::test_case_{i}" for i in range(2000)]
    counts = {1: 0, 2: 0, 3: 0}
    for nodeid in ids:
        shard = shard_of(nodeid, 3)
        assert shard in counts
        assert shard == shard_of(nodeid, 3)  # stable
        counts[shard] += 1
    assert sum(counts.values()) == len(ids)
    assert min(counts.values()) > len(ids) / 3 * 0.85  # roughly balanced


def test_ci_runs_forked_shards_and_keeps_required_check_names():
    workflow = yaml.safe_load(
        (Path(__file__).parents[1] / ".github" / "workflows" / "test.yml").read_text()
    )
    jobs = workflow["jobs"]
    shard_job = jobs["pytest-shard"]
    assert shard_job["strategy"]["matrix"]["shard"] == [1, 2, 3]
    run_step = next(
        step for step in shard_job["steps"] if step.get("name") == "Run pytest"
    )
    assert "python -m pytest -q --forked --tb=short" in run_step["run"]
    assert run_step["env"]["THREADKEEPER_TEST_SHARD"] == "${{ matrix.shard }}/3"
    command = [
        line.strip() for line in run_step["run"].splitlines()
        if line.strip().startswith("python -m pytest")
    ]
    assert command == ["python -m pytest -q --forked --tb=short"]

    gate = jobs["pytest"]
    assert gate["name"] == "pytest (py${{ matrix.python }})"
    assert gate["needs"] == "pytest-shard"
    assert gate["strategy"]["matrix"]["python"] == ["3.11", "3.12", "3.13"]
