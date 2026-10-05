"""Regression coverage for the internal spawn-result contract."""

from threadkeeper.spawn_result import parse_spawn_result


def test_parse_spawn_result_requires_a_task_identifier():
    launched = parse_spawn_result("ok task=tk_123 pid=42")
    legacy = parse_spawn_result("spawn task_id=old-test-task pid=0")
    returned_error = parse_spawn_result("ERR spawn_reservation_failed=locked")
    malformed = parse_spawn_result("ok accepted")

    assert launched.ok and launched.task_id == "tk_123"
    assert legacy.ok and legacy.task_id == "old-test-task"
    assert not returned_error.ok
    assert returned_error.reason == "spawn_reservation_failed=locked"
    assert not malformed.ok
    assert malformed.reason == "invalid_spawn_result: missing task id"
