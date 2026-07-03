from __future__ import annotations


def test_primary_rate_limit_headers_set_reset_cooldown(fresh_mp):
    import threadkeeper.github_budget as gb

    obs = gb.observe_rate_headers(
        {
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": "1120",
            "x-ratelimit-limit": "5000",
        },
        status_code=403,
        body='{"message":"API rate limit exceeded"}',
        now_t=1000,
    )

    assert obs.remaining == 0
    assert obs.limit == 5000
    assert obs.cooldown_until == 1120
    assert obs.reason == "primary_rate_limit"


def test_secondary_rate_limit_uses_exponential_backoff(fresh_mp):
    import threadkeeper.github_budget as gb

    first = gb.observe_rate_headers(
        {"x-ratelimit-remaining": "4999"},
        status_code=403,
        body='{"message":"You have exceeded a secondary rate limit"}',
        now_t=1000,
        previous_attempts=0,
    )
    second = gb.observe_rate_headers(
        {"x-ratelimit-remaining": "4998"},
        status_code=403,
        body='{"message":"You have exceeded a secondary rate limit"}',
        now_t=1000,
        previous_attempts=1,
    )

    assert first.reason == "secondary_rate_limit"
    assert first.backoff_attempts == 1
    assert first.cooldown_until == 1060
    assert second.backoff_attempts == 2
    assert second.cooldown_until == 1120
    assert gb.exponential_backoff_s(10, base_s=60, cap_s=300) == 300


def test_retry_after_header_bounds_cooldown(fresh_mp):
    import threadkeeper.github_budget as gb

    obs = gb.observe_rate_headers(
        {"retry-after": "999999"},
        status_code=429,
        body="too many requests",
        now_t=1000,
    )

    assert obs.reason == "retry_after"
    assert obs.cooldown_until == 1000 + gb.GITHUB_RATE_BACKOFF_CAP_S


def test_split_gh_api_output_extracts_headers_and_page_bodies(fresh_mp):
    import json
    import threadkeeper.github_budget as gb

    output = (
        "HTTP/2.0 200 OK\n"
        "X-RateLimit-Remaining: 42\n"
        "X-RateLimit-Reset: 1200\n"
        "\n"
        '[{"number":1}]\n'
        "HTTP/2.0 200 OK\n"
        "X-RateLimit-Remaining: 41\n"
        "X-RateLimit-Reset: 1200\n"
        "\n"
        '[{"number":2}]'
    )

    responses, bodies = gb.split_gh_api_output(output)

    assert [status for status, _headers in responses] == [200, 200]
    assert responses[-1][1]["x-ratelimit-remaining"] == "41"
    assert [json.loads(body)[0]["number"] for body in bodies] == [1, 2]


def test_shared_cooldown_preflight_blocks_runner(fresh_mp, monkeypatch):
    import threadkeeper.github_budget as gb

    monkeypatch.setattr(gb.time, "time", lambda: 1000)
    gb.record_github_response(
        status_code=403,
        headers={"x-ratelimit-remaining": "10"},
        body='{"message":"secondary rate limit"}',
    )
    state = gb.github_budget_state(now_t=1000)
    assert state["cooldown_active"] is True
    assert state["cooldown_left_s"] == 60

    called = False

    def _runner(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("runner should not be invoked during cooldown")

    proc = gb.run_gh(["gh", "issue", "view", "38"], runner=_runner)

    assert called is False
    assert proc.returncode == gb.GITHUB_RATE_COOLDOWN_EXIT
    assert "cooldown active" in proc.stderr
