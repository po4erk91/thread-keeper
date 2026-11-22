# Check child MCP environment forwarding safely

Use this check when a child cannot see its MCP identity. It reports whether a
nonsecret marker reaches a disposable dummy MCP command. It never reads your
identity, pass, database path, provider settings, or access-token values.

## Which names are forwarded

`CHILD_MCP_ENV_KEYS` in `threadkeeper/adapters/base.py` is the shared list of
per-child environment variable names. Claude children receive their values in
the private slim MCP configuration. Codex starts MCP servers with a scrubbed
environment; `threadkeeper/adapters/codex.py` supplies the names through
`-c mcp_servers.thread-keeper.env_vars=[...]`. Values stay out of those arguments.
The list includes `THREADKEEPER_MEMORY_TOKEN`, so capturing a complete process
environment can disclose private memory access as well as unrelated credentials.

For a needed per-child variable, add its exact name to `CHILD_MCP_ENV_KEYS`,
ensure the parent sets it for the intended child, and extend the adapter
forwarding tests with synthetic values. Do not forward every variable or put a
real value in a command, test fixture, report, or issue. A user's MCP entry has
a separate filter, `child_mcp_entry_env()`, for package paths and
`THREADKEEPER_*` settings; it excludes per-child keys so stale settings cannot
replace the current child's identity. Editing a user's Codex `env_vars` alone
does not change the list generated for autonomous children.

## Offline presence check

From the source checkout root, run the block below with `.venv/bin/python`
(paste it into a Python file, or use a quoted
Python heredoc). It uses the shipped forwarding list to build a synthetic child
environment, then launches only Python with a disposable dummy MCP command.
It makes no LLM or GitHub request, starts no real memory server, and reads no
host configuration. This checks the shared allowlist across a local process
boundary; it does not verify a particular CLI build or a live child's authority.

`THREADKEEPER_TZ` serves only as a marker; the dummy does not interpret its
value. An unrelated synthetic secret is deliberately present in both dummy
environments to check that the output stays limited even if other variables
reach the command. Keep all inputs synthetic; do not substitute real values.

<!-- child-mcp-presence-probe:start -->
```python
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile

CHILD_MCP_ENV_KEYS = runpy.run_path("threadkeeper/adapters/base.py")["CHILD_MCP_ENV_KEYS"]

DUMMY_MCP = '''import json, os
present = "THREADKEEPER_TZ" in os.environ
code = 0 if present else 1
print(json.dumps({"marker_present": present, "result_code": code}))
raise SystemExit(code)
'''


def check_marker(command, child_env):
    error = {"marker_present": False, "result_code": 2}
    try:
        child = subprocess.run(
            command, env=child_env, capture_output=True, text=True,
            timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return error
    for code in (0, 1):
        result = {"marker_present": code == 0, "result_code": code}
        if (child.returncode == code and not child.stderr
                and child.stdout == json.dumps(result) + "\n"):
            return result
    return error


def main():
    synthetic_parent = {"THREADKEEPER_TZ": "synthetic-marker-only"}
    forwarded = {name: synthetic_parent[name] for name in CHILD_MCP_ENV_KEYS
                 if name in synthetic_parent}
    unrelated = {"UNRELATED_SYNTHETIC_SECRET": "synthetic-secret-only"}
    with tempfile.TemporaryDirectory(prefix="tk-child-mcp-probe-") as directory:
        probe = Path(directory) / "dummy_mcp.py"
        probe.write_text(DUMMY_MCP, encoding="utf-8")
        command = [sys.executable, "-I", "-B", str(probe)]
        results = [check_marker(command, unrelated | env)
                   for env in (forwarded, {})]
    for result in results:
        print(json.dumps(result))
    return 0 if [result["result_code"] for result in results] == [0, 1] else 2


if __name__ == "__main__":
    raise SystemExit(main())
```
<!-- child-mcp-presence-probe:end -->

Expected output, also the complete safe issue-report example:

```json
{"marker_present": true, "result_code": 0}
{"marker_present": false, "result_code": 1}
```

The first result uses the forwarded marker. The second intentionally omits it
and must fail the presence check. Result codes are `0` (present), `1` (absent),
and `2` (probe error, including unexpected output or timeout). The recipe exits
`0` only when both controls behave as expected; otherwise it exits `2`. It
never echoes unexpected child stdout or stderr.

The temporary directory and dummy command are removed automatically, including
after a failed check. If you saved the recipe as a file, delete that file after
use. After a forced kill, remove the leftover `tk-child-mcp-probe-*` directory
from your temporary directory. The dummy contains only fixed source code and
writes no report file. Share only the two result objects; avoid complete process
environment captures, raw configuration, and dummy commands that save the
environment. Guidance installed by another project must be fixed by its owner.
