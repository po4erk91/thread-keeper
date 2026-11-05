"""Run isolated Caliper experiments and bind decisions to the tested bytes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
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
from .ingest import _scrub_dialog_secrets
from .skill_audit import _frontmatter, _tree_hash


IMAGE = "threadkeeper-caliper"
PRIVATE_ROOT = "/run/caliper-private"
ATTEMPTS = 3
MIN_CASES = 5
MAX_CASES = 6
MIN_EXCLUSIVE_CASES = 3
MAX_CONTEXT_CHARS = 2_000
MAX_SKILL_CONTEXT_CHARS = 20_000
MAX_INFERRED_USE_CASE_CHARS = 500
INFERRED_CONTEXT_SOURCE = "skill_inferred"
CONTEXT_KEY = "threadkeeper-eval-context"
MAX_RUNS_PER_PASS = 2
MAX_RESULT_BYTES = 8 * 1024 * 1024


def _read_skill_text(skill: Path) -> str:
    try:
        with (skill / "SKILL.md").open(encoding="utf-8") as file:
            return file.read(64_000)
    except (OSError, UnicodeError):
        return ""


def _explicit_context(content: str) -> str:
    fields, _, _ = _frontmatter(content)
    metadata = fields.get("metadata")
    text = metadata.get(CONTEXT_KEY) if isinstance(metadata, dict) else None
    return text.strip() if isinstance(text, str) else ""


def _creation_context(conn: sqlite3.Connection, name: str) -> tuple[str, str]:
    parents = conn.execute(
        "SELECT d.parent_kind, d.parent_id FROM memory_derivations d "
        "JOIN memory_authority a ON a.artifact_kind=d.parent_kind AND a.artifact_id=d.parent_id "
        "WHERE d.child_kind='skill' AND d.child_id=? AND a.invalidated_at IS NULL "
        "ORDER BY d.created_at, d.parent_kind, d.parent_id",
        (name,),
    )
    queries = {
        "note": "SELECT content FROM notes WHERE id=?",
        "verbatim": "SELECT content FROM verbatim WHERE id=? AND speaker='user'",
        "dialog": "SELECT content FROM dialog_messages WHERE uuid=? AND role='user'",
    }
    for kind, artifact_id in parents:
        query = queries.get(kind)
        if query and (row := conn.execute(query, (artifact_id,)).fetchone()) and row[0].strip():
            return f"{kind}:{artifact_id}", row[0]
    return "", ""


def skill_context(conn: sqlite3.Connection, name: str, skill: Path) -> dict[str, str] | None:
    """Prefer a recorded use case and fall back to the skill as evidence."""
    content = _read_skill_text(skill)
    explicit = _explicit_context(content)
    source, text = ("metadata", explicit) if explicit else _creation_context(conn, name)
    excerpt_limit = MAX_CONTEXT_CHARS
    if not text:
        if not content.strip():
            return None
        source, text = INFERRED_CONTEXT_SOURCE, content
        excerpt_limit = MAX_SKILL_CONTEXT_CHARS
    excerpt = _scrub_dialog_secrets(text)[:excerpt_limit].strip()
    revision = _digest(json.dumps([source, text], ensure_ascii=False).encode())
    return {"source": source, "text": excerpt, "revision": revision}


def context_revision(conn: sqlite3.Connection, name: str, skill: Path) -> str | None:
    context = skill_context(conn, name, skill) if CURATOR_CALIPER_DIR else None
    return context["revision"] if context else None


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


def _scenario_task(case: dict, axis: str) -> dict:
    condition = {axis: case["exclusive_value"]} if "exclusive_value" in case else {}
    setup = (
        f"printf '%s' {shlex.quote(case['input'])} > input.txt\n"
        f"printf '%s' {shlex.quote(json.dumps(condition))} > condition.json"
    )
    expected = case["expected"].encode()
    assertion = (
        "import os, stat\n"
        "fd = os.open('result.txt', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)\n"
        "with os.fdopen(fd, 'rb') as result:\n"
        "    assert stat.S_ISREG(os.fstat(result.fileno()).st_mode)\n"
        f"    assert result.read({len(expected) + 2}).removesuffix(b'\\n') == {expected!r}\n"
    )
    return {
        "name": case["name"],
        "prompt": (
            f"{case['prompt']}\n\n"
            "Use input.txt and the scenario condition in condition.json. "
            "Write the requested outcome to result.txt."
        ),
        "setup": setup,
        "assert": assertion,
    }


def compile_scenarios(raw: str, context: str, *, inferred: bool = False) -> tuple[list[dict], str]:
    if len(raw.encode()) > 32_000:
        raise ValueError("scenario set too large")
    suite = json.loads(raw)
    required_keys = {"exclusive_axis", "cases"}
    if (
        not isinstance(suite, dict) or not required_keys <= suite.keys()
        or suite.keys() - required_keys - {"inferred_use_case"}
    ):
        raise ValueError("provide an exclusive_axis and cases")
    inference = suite.get("inferred_use_case", "")
    if inferred:
        if (
            not isinstance(inference, str) or not inference.strip()
            or len(inference) > MAX_INFERRED_USE_CASE_CHARS or "\x00" in inference
        ):
            raise ValueError("skill-derived scenarios need a short inferred_use_case")
        inference = _scrub_dialog_secrets(inference.strip())
    elif "inferred_use_case" in suite:
        raise ValueError("inferred_use_case is only allowed for skill-derived scenarios")
    axis = suite["exclusive_axis"]
    if not isinstance(axis, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", axis):
        raise ValueError("exclusive_axis must name one condition shared by the alternatives")
    cases = suite["cases"]
    if not isinstance(cases, list) or not MIN_CASES <= len(cases) <= MAX_CASES:
        raise ValueError("provide five to six scenarios")
    required = {"name", "prompt", "input", "expected", "context_excerpt"}
    for case in cases:
        if (
            not isinstance(case, dict) or not required <= case.keys()
            or case.keys() - required - {"exclusive_value"}
            or any(not isinstance(value, str) or "\x00" in value for value in case.values())
            or any(not case[field].strip() for field in case.keys() - {"input", "expected"})
        ):
            raise ValueError("invalid scenario fields")
        excerpt = case["context_excerpt"].strip()
        if len(excerpt) < 12 or excerpt not in context:
            raise ValueError("each scenario must cite its source context")
    if len({case["name"].strip().casefold() for case in cases}) != len(cases):
        raise ValueError("scenario names must be unique")
    if len({case["prompt"].strip().casefold() for case in cases}) != len(cases):
        raise ValueError("scenario prompts must be distinct")
    alternatives = [
        case["exclusive_value"].strip().casefold()
        for case in cases if "exclusive_value" in case
    ]
    if len(alternatives) < MIN_EXCLUSIVE_CASES or len(set(alternatives)) != len(alternatives):
        raise ValueError("at least three cases need mutually exclusive values on the same axis")
    return [_scenario_task(case, axis) for case in cases], inference


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
    context_digest: str, candidate: str, result: dict,
) -> None:
    record = {
        **result,
        "skill_digest": skill_digest,
        "suite_digest": suite_digest,
        "context_digest": context_digest,
        "candidate_digest": _digest(candidate.encode()) if candidate else None,
    }
    _save_private_bytes(directory / f"{name}.json", json.dumps(record, sort_keys=True).encode())


def _save_private_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(data)
            file.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _redundancy_proved(evidence: dict) -> bool:
    cases = evidence["cases"]
    if not isinstance(cases, (list, tuple)) or not MIN_CASES <= len(cases) <= MAX_CASES:
        return False
    perfect = (ATTEMPTS,) * len(cases)
    return (
        tuple(evidence["passes_by_case"]["control"]) == perfect
        and tuple(evidence["passes_by_case"]["current"]) == perfect
        and tuple(evidence["activations_by_case"]["current"]) == perfect
    )


def _decision_matches(evidence: dict, action: str, candidate: str) -> bool:
    decision = evidence["decision"]
    if action == "edit":
        return decision == "repair" and evidence["candidate_digest"] == _digest(candidate.encode())
    return decision == "remove" or (
        action == "retire_rule" and decision == "keep" and _redundancy_proved(evidence)
    )


def mutation_refusal(
    conn: sqlite3.Connection, action: str, name: str, skill: Path,
    candidate: str = "", *, replacement: str = "",
) -> str:
    if CURATOR_CALIPER_DIR is None or action not in {"edit", "delete", "retire_rule"}:
        return ""
    if action == "delete" and replacement:
        return ""
    revision = context_revision(conn, name, skill)
    if action == "edit" and revision is None:
        return ""
    directory = evidence_dir()
    if directory is not None and revision is not None:
        try:
            evidence = _read_result(directory / f"{name}.json")
            current_digest = _tree_hash(skill)
            if (
                current_digest is not None
                and evidence["skill_digest"] == current_digest
                and evidence["context_digest"] == revision
                and evidence["suite_digest"] == _digest((directory / f"{name}.suite.json").read_bytes())
                and _decision_matches(evidence, action, candidate)
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
        "--no-user-customizations", "--k", str(ATTEMPTS), "--workers", "6",
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


def evaluate(
    conn: sqlite3.Connection, name: str, skill: Path, candidate: str = "", scenarios: str = "",
) -> dict:
    directory = evidence_dir()
    if CURATOR_CALIPER_DIR is None or directory is None:
        return {"decision": "defer", "reason": "caliper_not_configured"}
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
        context = skill_context(conn, name, skill)
        if context is None:
            return {"decision": "defer", "reason": "missing_skill_context_and_source"}
        suite = directory / f"{name}.suite.json"
        if not scenarios and not (candidate and suite.is_file()):
            return {
                "status": "needs_scenarios",
                "context": context["text"],
                "context_source": context["source"],
            }
        return _evaluate(
            conn, name, skill, suite, auth, directory, context, candidate, scenarios,
        )
    except subprocess.CalledProcessError as exc:
        return {"decision": "defer", "reason": "caliper_failed", "exit_code": exc.returncode}
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, subprocess.SubprocessError) as exc:
        return {"decision": "defer", "reason": type(exc).__name__}


def _evaluate(
    conn: sqlite3.Connection, name: str, skill: Path, suite: Path, auth: Path,
    directory: Path, context: dict, candidate: str, scenarios: str,
) -> dict:
    with single_flight_lock("curator-caliper") as locked:
        if not locked:
            return {"decision": "defer", "reason": "evaluation_running"}
        marker = directory / f"{name}.started"
        if candidate and marker.exists():
            marker = directory / f"{name}:candidate.started"
        if marker.exists() or len(list(directory.parent.glob("batch-*/*.started"))) >= MAX_RUNS_PER_PASS:
            return {"decision": "defer", "reason": "evaluation_budget_exhausted"}
        context_digest = context["revision"]
        if suite.is_file():
            if scenarios:
                return {"decision": "defer", "reason": "reuse_original_scenarios"}
            suite_bytes = suite.read_bytes()
            recorded = json.loads(suite_bytes)
            if recorded["context_digest"] != context_digest:
                return {"decision": "defer", "reason": "source_changed"}
            tasks = recorded["tasks"]
            inference = recorded.get("inferred_use_case", "")
        else:
            try:
                tasks, inference = compile_scenarios(
                    scenarios, context["text"], inferred=context["source"] == INFERRED_CONTEXT_SOURCE,
                )
            except ValueError as exc:
                return {"decision": "defer", "reason": "invalid_scenarios", "detail": str(exc)}
            recorded = {"context_digest": context_digest, "tasks": tasks}
            if inference:
                recorded["inferred_use_case"] = inference
            suite_bytes = json.dumps(recorded).encode()
        cases = tuple(task["name"] for task in tasks)
        suite_digest = _digest(suite_bytes)
        model = CURATOR_CALIPER_MODEL
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
            if not suite.is_file():
                _save_private_bytes(suite, suite_bytes)
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
            if (
                original_digest != _tree_hash(skill) or suite_digest != _digest(suite.read_bytes())
                or context_digest != context_revision(conn, name, skill)
            ):
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
                "context_source": context["source"],
            }
            if inference:
                result["inferred_use_case"] = inference
            result["no_measured_benefit"] = _redundancy_proved(result)
            save_evidence(
                directory, name, original_digest, suite_digest, context_digest, candidate, result,
            )
            return result
