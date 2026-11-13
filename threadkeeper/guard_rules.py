"""Exact, bounded rule contracts; no shell execution or arbitrary regex."""
from __future__ import annotations

from fnmatch import fnmatchcase
import inspect
import json
import posixpath
from pathlib import PurePosixPath
import re
import shlex


CONTROL_TOOLS = frozenset({"lesson_guard_manage", "lesson_guard_status"})


def tool_arguments(tool_name: str, payload: dict) -> dict:
    from ._mcp import mcp, WRITE_TOOL_NAMES
    if tool_name not in WRITE_TOOL_NAMES or tool_name in CONTROL_TOOLS:
        raise ValueError("unsupported_tool")
    tool = mcp._tool_manager._tools.get(tool_name)
    if tool is None:
        raise ValueError("unsupported_tool")
    try:
        bound = inspect.signature(tool.fn).bind(**payload)
        bound.apply_defaults()
        return {key: value for key, value in bound.arguments.items() if key != "ctx"}
    except TypeError as exc:
        raise ValueError("invalid_payload") from exc


def normalize(trigger: dict) -> dict[str, str]:
    if not isinstance(trigger, dict) or set(trigger) != {"tool", "field", "operator", "value"}:
        raise ValueError("invalid_trigger")
    if not all(isinstance(value, str) for value in trigger.values()):
        raise ValueError("invalid_trigger")
    rule = {key: value.strip() for key, value in trigger.items()}
    rule["value"] = trigger["value"]
    if not re.fullmatch(r"[a-z_]+(?:\.[a-z_]+){0,3}", rule["field"]):
        raise ValueError("invalid_field")
    if rule["field"].split(".")[0] == "ctx":
        raise ValueError("invalid_field")
    from ._mcp import mcp, WRITE_TOOL_NAMES
    tool = mcp._tool_manager._tools.get(rule["tool"])
    if rule["tool"] not in WRITE_TOOL_NAMES or rule["tool"] in CONTROL_TOOLS or tool is None:
        raise ValueError("unsupported_tool")
    if rule["field"].split(".")[0] not in inspect.signature(tool.fn).parameters:
        raise ValueError("invalid_field")
    if rule["operator"] not in {"equals", "command_prefix", "path_glob"}:
        raise ValueError("invalid_operator")
    value = rule["value"]
    if not value.strip() or len(value) > 160 or any(ord(char) < 32 for char in value):
        raise ValueError("invalid_pattern")
    if re.search(r"(?:^|[\s'\"=])(?:/|~/|[A-Za-z]:[\\/])", value):
        raise ValueError("private_pattern")
    if re.search(r"(?i)\b(?:password|secret|token|api[_-]?key|authorization)\s*[:=]", value):
        raise ValueError("private_pattern")
    from .github_safety import sanitize_public_github_body
    if sanitize_public_github_body(value) != value:
        raise ValueError("private_pattern")
    if rule["operator"] == "path_glob":
        path = PurePosixPath(value.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or value.startswith(("~", "$")):
            raise ValueError("private_pattern")
        rule["value"] = str(path)
    if rule["operator"] == "command_prefix":
        try:
            tokens = shlex.split(value)
        except ValueError as exc:
            raise ValueError("invalid_pattern") from exc
        if not tokens or any(token in {";", "|", "&&", "||"} for token in tokens):
            raise ValueError("invalid_pattern")
        rule["value"] = shlex.join(tokens)
    return rule


def matches(rule: dict, payload: dict) -> bool:
    value = payload
    for key in rule["field"].split("."):
        value = value.get(key) if isinstance(value, dict) else None
    if not isinstance(value, str):
        return False
    expected = rule["value"]
    if rule["operator"] == "equals":
        return value == expected
    if rule["operator"] == "path_glob":
        path = posixpath.normpath(value.replace("\\", "/"))
        return fnmatchcase(path, expected) or (expected.startswith("**/") and fnmatchcase(path, expected[3:]))
    try:
        tokens, prefix = shlex.split(value), shlex.split(expected)
        return tokens[:len(prefix)] == prefix
    except ValueError:
        return False


def decision(rule: dict, payload: dict, *, exempt: bool = False) -> str:
    return "deny" if matches(rule, payload) and not exempt else "allow"


def canonical(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_payload") from exc
