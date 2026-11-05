"""Run isolated Caliper experiments and bind decisions to the tested bytes."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import uuid

import yaml

from .adapters.base import find_cli_executable
from .config import (
    CURATOR_CALIPER_ARM_TIMEOUT_S,
    CURATOR_CALIPER_CLEANUP_TIMEOUT_S,
    CURATOR_CALIPER_DIR,
    CURATOR_CALIPER_MODEL,
)
from .curator_snapshots import VERIFY_BATCH_ENV, current_snapshot_dir, snapshots_root
from .helpers import single_flight_lock
from .permissions import open_private_binary_write
from .skill_audit import _tree_hash


IMAGE = "threadkeeper-caliper"
PRIVATE_ROOT = "/run/caliper-private"
ATTEMPTS = 3
MAX_CASES = 4
MAX_RUNS_PER_PASS = 2
MAX_RESULT_BYTES = 8 * 1024 * 1024


def suite_path(name: str) -> Path | None:
    if CURATOR_CALIPER_DIR is None:
        return None
    return CURATOR_CALIPER_DIR / "suites" / f"{name}.eval.yaml"


def suite_revision(name: str) -> str | None:
    suite = suite_path(name)
    if suite is None or not suite.is_file():
        return None
    try:
        return _digest(suite.read_bytes())
    except OSError:
        return None


def scheduled_suites(names: list[str]) -> set[str]:
    eligible = dict.fromkeys(names, 0.0)
    if not eligible:
        return set()
    for marker in snapshots_root().glob("*/caliper/batch-*/*.started"):
        name = marker.stem.split(":", 1)[0]
        if name in eligible:
            try:
                eligible[name] = max(eligible[name], marker.stat().st_mtime)
            except OSError:
                continue
    return set(sorted(eligible, key=lambda name: (eligible[name], name))[:MAX_RUNS_PER_PASS])


def read_suite(source: bytes) -> list[dict]:
    if len(source) > 32_000:
        raise ValueError("Caliper suite too large")
    data = yaml.safe_load(source)
    if not isinstance(data, dict) or set(data) != {"tasks"}:
        raise ValueError("Caliper suite must contain only a tasks mapping")
    tasks = data["tasks"]
    if not isinstance(tasks, list) or any(
        not isinstance(task, dict)
        or set(task) - {"name", "prompt", "setup", "assert"}
        or not all(isinstance(value, str) and value.strip() for value in task.values())
        or not {"name", "prompt", "assert"} <= task.keys()
        for task in tasks
    ):
        raise ValueError("Caliper tasks need a name, prompt and deterministic inline assertion")
    if not 2 <= len(tasks) <= MAX_CASES or len({task["name"] for task in tasks}) != len(tasks):
        raise ValueError("Caliper suite needs two to four uniquely named tasks")
    for task in tasks:
        try:
            assertion = ast.parse(task["assert"])
        except SyntaxError as exc:
            raise ValueError("Caliper suite requires inline Python assertions") from exc
        if not any(isinstance(node, ast.Assert) for node in ast.walk(assertion)):
            raise ValueError("Caliper task must assert an observable result")
    return tasks


def _read_result(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise ValueError("invalid Caliper result file")
        content = file.read(MAX_RESULT_BYTES + 1)
    if len(content) > MAX_RESULT_BYTES:
        raise ValueError("Caliper result exceeds the size limit")
    return json.loads(content)


def read_arm(
    path: Path, cases: tuple[str, ...], name: str, *, control: bool = False,
) -> tuple[tuple[int, ...], tuple[int, ...], tuple]:
    try:
        result = _read_result(path)
        run = result["run"]
        tasks = {task["task_name"]: task["attempts"] for task in result["task_results"]}
        if (
            run["backend"] != "codex" or run["k"] != ATTEMPTS
            or run.get("interrupted") or run.get("hook_failures")
            or run.get("user_customizations") is not False
            or len(result["task_results"]) != len(cases) or set(tasks) != set(cases)
            or run["ablated"] != ([name] if control else [])
        ):
            raise ValueError("incomplete Caliper run")
        outcomes = tuple(tuple(attempt["outcome"] for attempt in tasks[case]) for case in cases)
        if any(
            len(attempts) != ATTEMPTS or set(attempts) - {"pass", "task_fail"}
            for attempts in outcomes
        ):
            raise ValueError("incomplete Caliper attempts")
        activations = tuple(
            sum(name in (attempt["activated"] or ()) for attempt in tasks[case])
            for case in cases
        )
        if control and any(activations):
            raise ValueError("Caliper control unexpectedly loaded the skill")
        identity = (run["model"], run["cli_version"])
        if not all(identity):
            raise ValueError("incomplete Caliper runtime identity")
    except (KeyError, TypeError) as exc:
        raise ValueError("incomplete Caliper result") from exc
    return tuple(attempts.count("pass") for attempts in outcomes), activations, identity


def decide(
    control: tuple[int, ...], current: tuple[int, ...], candidate: tuple[int, ...] | None,
) -> str:
    perfect = (ATTEMPTS,) * len(current)
    failed = (0,) * len(current)
    if current == perfect:
        return "keep"
    if candidate == perfect:
        return "repair"
    if control == perfect and current == failed and candidate in (None, failed):
        return "remove"
    return "inconclusive"


def evidence_dir() -> Path | None:
    snapshot = current_snapshot_dir()
    batch = os.environ.get(VERIFY_BATCH_ENV, "")
    if snapshot is None or not batch.isascii() or not batch.isdecimal():
        return None
    return snapshot / "caliper" / f"batch-{int(batch):03d}"


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def save_evidence(
    directory: Path, name: str, skill_digest: str, suite_digest: str,
    candidate: str, result: dict,
) -> None:
    record = {
        **result,
        "skill_digest": skill_digest,
        "suite_digest": suite_digest,
        "candidate_digest": _digest(candidate.encode()) if candidate else None,
    }
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(json.dumps(record, sort_keys=True).encode())
            file.flush()
            temporary.replace(directory / f"{name}.json")
        finally:
            temporary.unlink(missing_ok=True)


def mutation_refusal(
    action: str, name: str, skill: Path, candidate: str = "", *, replacement: str = "",
) -> str:
    suite = suite_path(name)
    if suite is None or action not in {"edit", "delete"}:
        return ""
    if action == "delete" and replacement:
        return ""
    if action == "edit" and not suite.is_file():
        return ""
    directory = evidence_dir()
    if directory is not None:
        try:
            evidence = _read_result(directory / f"{name}.json")
            expected = "repair" if action == "edit" else "remove"
            current_digest = _tree_hash(skill)
            if (
                evidence["decision"] == expected
                and current_digest is not None and evidence["skill_digest"] == current_digest
                and evidence["suite_digest"] == _digest(suite.read_bytes())
                and (action != "edit" or evidence["candidate_digest"] == _digest(candidate.encode()))
            ):
                return ""
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return f"ERR caliper_evidence_required action={action} name={name}"


def _docker_run(
    auth: Path, inputs: Path, outputs: Path, arm: str, name: str, model: str,
) -> None:
    docker = find_cli_executable("docker")
    if not docker:
        raise FileNotFoundError("Docker CLI unavailable")
    endpoint = subprocess.run(
        [docker, "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
        check=True, capture_output=True, text=True, timeout=5,
    ).stdout.strip()
    if not endpoint.startswith("unix:///"):
        raise ValueError("Caliper requires a local Docker socket")
    docker_command = [docker, "--host", endpoint]
    docker_env = {key: value for key, value in os.environ.items()
                  if key not in {"DOCKER_CONTEXT", "DOCKER_HOST"}}
    container = f"tk-caliper-{uuid.uuid4().hex}"
    command = [
        *docker_command, "run", "--rm", "--name", container, "--pull=never", "--init",
        "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,exec,size=512m",
        "--memory=1g", "--pids-limit=128", "--security-opt=no-new-privileges",
        "--env", "CALIPER_TELEMETRY_DISABLED=1", "--env", f"XDG_CONFIG_HOME={PRIVATE_ROOT}",
        "--mount", f"type=bind,src={auth},dst={PRIVATE_ROOT}/caliper",
        "--mount", f"type=bind,src={inputs},dst={PRIVATE_ROOT}/input,readonly",
        "--mount", f"type=bind,src={outputs},dst={PRIVATE_ROOT}/output",
        IMAGE, "run", f"{PRIVATE_ROOT}/input/{arm}.eval.yaml", "--model", f"codex:{model}",
        "--no-user-customizations", "--k", str(ATTEMPTS), "--workers", "3",
        "--timeout", "35", "--fail-fast", "1", "--output", f"{PRIVATE_ROOT}/output/run.json",
    ]
    if arm == "control":
        command.extend(("--ablate", name))
    try:
        subprocess.run(
            command, check=True, timeout=CURATOR_CALIPER_ARM_TIMEOUT_S,
            env=docker_env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    finally:
        try:
            subprocess.run(
                [*docker_command, "rm", "-f", container],
                timeout=CURATOR_CALIPER_CLEANUP_TIMEOUT_S, env=docker_env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


def _copy_skill(source: Path, destination: Path) -> None:
    if source.is_symlink() or not (source / "SKILL.md").is_file():
        raise ValueError("skill has no local source")
    size = 0
    for path in source.rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError("skill contains an unsupported file")
        if path.is_file():
            size += path.stat().st_size
        if size > 2_000_000:
            raise ValueError("skill bundle too large")
    shutil.copytree(source, destination)


def _stage_spec(inputs: Path, arm: str, bundle: str, name: str, tasks: list[dict]) -> None:
    spec = {"skills": [f"./{bundle}/{name}/SKILL.md"], "tasks": tasks}
    (inputs / f"{arm}.eval.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")


def evaluate(name: str, skill: Path, candidate: str = "") -> dict:
    suite = suite_path(name)
    directory = evidence_dir()
    if suite is None or directory is None or not suite.is_file():
        return {"decision": "defer", "reason": "no_configured_suite"}
    if not CURATOR_CALIPER_MODEL:
        return {"decision": "defer", "reason": "caliper_model_required"}
    auth = CURATOR_CALIPER_DIR / "auth" / "caliper"
    try:
        if (
            auth.is_symlink() or not auth.is_dir()
            or auth.stat().st_mode & 0o077
            or not (auth / "chatgpt.json").is_file()
        ):
            return {"decision": "defer", "reason": "private_caliper_subscription_login_required"}
        return _evaluate(name, skill, suite, auth, directory, candidate, CURATOR_CALIPER_MODEL)
    except subprocess.CalledProcessError as exc:
        return {"decision": "defer", "reason": "caliper_failed", "exit_code": exc.returncode}
    except (OSError, ValueError, yaml.YAMLError, subprocess.SubprocessError) as exc:
        return {"decision": "defer", "reason": type(exc).__name__}


def _evaluate(
    name: str, skill: Path, suite: Path, auth: Path, directory: Path, candidate: str, model: str,
) -> dict:
    with single_flight_lock("curator-caliper") as locked:
        if not locked:
            return {"decision": "defer", "reason": "evaluation_running"}
        marker = directory / f"{name}.started"
        if candidate and marker.exists():
            marker = directory / f"{name}:candidate.started"
        if marker.exists() or len(list(directory.parent.glob("batch-*/*.started"))) >= MAX_RUNS_PER_PASS:
            return {"decision": "defer", "reason": "evaluation_budget_exhausted"}
        suite_bytes = suite.read_bytes()
        tasks = read_suite(suite_bytes)
        cases = tuple(task["name"] for task in tasks)
        suite_digest = _digest(suite_bytes)
        with tempfile.TemporaryDirectory(prefix="tk-caliper-") as scratch:
            root = Path(scratch)
            inputs = root / "input"
            outputs = root / "output"
            inputs.mkdir(mode=0o700)
            original = inputs / "original" / name
            _copy_skill(skill, original)
            original_digest = _tree_hash(original)
            if not original_digest or original_digest != _tree_hash(skill):
                return {"decision": "defer", "reason": "source_changed"}
            _stage_spec(inputs, "control", "original", name, tasks)
            _stage_spec(inputs, "current", "original", name, tasks)
            if candidate:
                repaired = inputs / "repaired" / name
                shutil.copytree(original, repaired)
                (repaired / "SKILL.md").write_text(candidate, encoding="utf-8")
                _stage_spec(inputs, "candidate", "repaired", name, tasks)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open_private_binary_write(marker):
                pass
            (directory / f"{name}.json").unlink(missing_ok=True)
            results = {}
            activation = {}
            identities = set()
            for arm in ("control", "current", "candidate") if candidate else ("control", "current"):
                result_dir = outputs / arm
                result_dir.mkdir(mode=0o700, parents=True)
                _docker_run(auth, inputs, result_dir, arm, name, model)
                results[arm], activation[arm], identity = read_arm(
                    result_dir / "run.json", cases, name, control=arm == "control",
                )
                identities.add(identity)
            if len(identities) != 1 or next(iter(identities))[0] != model:
                return {"decision": "defer", "reason": "runtime_changed"}
            if original_digest != _tree_hash(skill) or suite_digest != _digest(suite.read_bytes()):
                return {"decision": "defer", "reason": "source_changed"}
            decision = decide(results["control"], results["current"], results.get("candidate"))
            tested_arm = {"repair": "candidate", "remove": "current"}.get(decision)
            if tested_arm and activation[tested_arm] != (ATTEMPTS,) * len(cases):
                decision = "inconclusive"
            result = {
                "decision": decision,
                "passes_by_case": results,
                "activations_by_case": activation,
                "cases": cases,
                "attempts_per_case": ATTEMPTS,
                "runtime": identities.pop(),
            }
            save_evidence(directory, name, original_digest, suite_digest, candidate, result)
            return result
