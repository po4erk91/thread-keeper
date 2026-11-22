"""Execute the maintained diagnostic recipe with synthetic environments."""
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "CHILD_MCP_DIAGNOSTICS.md"


def _recipe():
    section = GUIDE.read_text(encoding="utf-8").split(
        "<!-- child-mcp-presence-probe:start -->", 1,
    )[1].split("<!-- child-mcp-presence-probe:end -->", 1)[0]
    return section.split("```python\n", 1)[1].split("```", 1)[0]


def test_documented_probe_outputs_only_presence_and_cleans_up(monkeypatch, capsys):
    inherited_secret = "unrelated-inherited-secret-must-stay-private"
    monkeypatch.setenv("UNRELATED_INHERITED_SECRET", inherited_secret)
    real_run = subprocess.run
    probe_paths = []

    def run_dummy(command, **kwargs):
        assert command[:3] == [sys.executable, "-I", "-B"]
        probe = Path(command[3])
        probe_paths.append(probe)
        assert set(kwargs["env"]) <= {"THREADKEEPER_TZ", "UNRELATED_SYNTHETIC_SECRET"}
        assert "UNRELATED_INHERITED_SECRET" not in kwargs["env"]
        source = probe.read_text(encoding="utf-8")
        for value in (*kwargs["env"].values(), inherited_secret):
            assert value not in source
        child = real_run(command, **kwargs)
        for value in (*kwargs["env"].values(), inherited_secret):
            assert value not in child.stdout + child.stderr
        assert child.stderr == ""
        return child

    def refuse_network(*args, **kwargs):
        pytest.fail("The offline recipe must not open a network connection")

    monkeypatch.setattr(subprocess, "run", run_dummy)
    monkeypatch.setattr(socket, "socket", refuse_network)
    namespace = {"__name__": "diagnostic_recipe"}
    exec(compile(_recipe(), str(GUIDE), "exec"), namespace)

    assert namespace["main"]() == 0

    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == (
        '{"marker_present": true, "result_code": 0}\n'
        '{"marker_present": false, "result_code": 1}\n'
    )
    assert len(probe_paths) == 2
    assert all(not path.parent.exists() for path in probe_paths)
    report_example = GUIDE.read_text(encoding="utf-8").split("```json\n", 1)[1].split("```", 1)[0]
    assert output.out == report_example


@pytest.mark.parametrize("failure", ["output", "stderr", "exit", "timeout"])
def test_documented_probe_errors_do_not_echo_child_output(monkeypatch, capsys, failure):
    secret = "synthetic-error-secret-must-stay-private"

    def failed_child(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("dummy", 5, output=secret, stderr=secret)
        return subprocess.CompletedProcess(
            args=[], returncode=17 if failure == "exit" else 0,
            stdout=secret if failure == "output" else json.dumps({
                "marker_present": True, "result_code": 0,
            }) + "\n",
            stderr=secret if failure == "stderr" else "",
        )

    monkeypatch.setattr(subprocess, "run", failed_child)
    namespace = {"__name__": "diagnostic_recipe"}
    exec(compile(_recipe(), str(GUIDE), "exec"), namespace)

    assert namespace["check_marker"](["dummy"], {}) == {
        "marker_present": False, "result_code": 2,
    }
    assert namespace["main"]() == 2
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == (
        '{"marker_present": false, "result_code": 2}\n' * 2
    )
