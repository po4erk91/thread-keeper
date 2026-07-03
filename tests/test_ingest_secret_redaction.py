from __future__ import annotations

from pathlib import Path

from threadkeeper.adapters.base import NormalizedMessage


class _FakeAdapter:
    name = "fake-cli"

    def __init__(self, messages: list[NormalizedMessage]) -> None:
        self._messages = messages

    def iter_messages(self, _fp: Path):
        yield from self._messages

    def project_label(self, _fp: Path) -> str:
        return "fake-project"


def _touch_transcript(tmp_path: Path) -> Path:
    fp = tmp_path / "session.jsonl"
    fp.write_text("{}\n", encoding="utf-8")
    return fp


def _secret_values() -> dict[str, str]:
    return {
        "auth": "bearer" * 5,
        "generic_bearer": "oauth" * 6,
        "aws": "AKIA" + ("A" * 16),
        "npm": "npm_" + ("b" * 24),
        "github": "ghp_" + ("c" * 24),
        "openai": "sk-" + ("d" * 24),
        "anthropic": "sk-ant-" + ("e" * 24),
        "slack": "xoxb-" + ("f" * 24),
        "service": "svc" * 10,
        "netrc_login": "ci-user",
        "netrc_password": "netrc" * 6,
    }


def _dialog_with_secrets() -> tuple[str, dict[str, str]]:
    values = _secret_values()
    text = "\n".join(
        [
            f"Authorization: Bearer {values['auth']}",
            f"OAuth {values['generic_bearer']}",
            f"SERVICE_TOKEN=\"{values['service']}\"",
            f"//registry.npmjs.org/:_authToken={values['npm']}",
            (
                "machine api.example.test "
                f"login {values['netrc_login']} "
                f"password {values['netrc_password']}"
            ),
            f"bare aws key {values['aws']}",
            f"known tokens {values['github']} {values['openai']}",
            f"more known tokens {values['anthropic']} {values['slack']}",
        ]
    )
    return text, values


def _ingest_one(pkg: dict, tmp_path: Path, content: str) -> str:
    from threadkeeper import ingest

    ingest.SEMANTIC_AVAILABLE = False
    conn = pkg["db"].get_db()
    fp = _touch_transcript(tmp_path)
    adapter = _FakeAdapter(
        [
            NormalizedMessage(
                uuid="msg-secret-1",
                session_id="sess-redact",
                role="user",
                content=content,
                model="",
                created_at=1_800_000_000,
                raw={"role": "user", "content": content},
            )
        ]
    )
    added = ingest._ingest_file(conn, fp, max_msgs=100, adapter=adapter)
    conn.commit()
    assert added == 1
    row = conn.execute(
        "SELECT content FROM dialog_messages WHERE uuid='msg-secret-1'"
    ).fetchone()
    fts = conn.execute(
        "SELECT content FROM dialog_fts WHERE uuid='msg-secret-1'"
    ).fetchone()
    assert row is not None
    assert fts is not None
    assert row["content"] == fts["content"]
    return row["content"]


def test_ingest_scrubs_secrets_before_dialog_and_fts_persistence(
    fresh_mp, tmp_path
):
    content, values = _dialog_with_secrets()
    persisted = _ingest_one(fresh_mp, tmp_path, content)

    for raw in values.values():
        assert raw not in persisted
    assert "Authorization: Bearer [REDACTED:AUTHORIZATION]" in persisted
    assert "OAuth [REDACTED:BEARER_TOKEN]" in persisted
    assert 'SERVICE_TOKEN="[REDACTED:SECRET]"' in persisted
    assert "_authToken=[REDACTED:NPMRC_CREDENTIAL]" in persisted
    assert "login [REDACTED:NETRC_LOGIN]" in persisted
    assert "password [REDACTED:NETRC_PASSWORD]" in persisted
    assert "[REDACTED:AWS_ACCESS_KEY_ID]" in persisted
    assert "[REDACTED:GITHUB_TOKEN]" in persisted
    assert "[REDACTED:OPENAI_API_KEY]" in persisted
    assert "[REDACTED:ANTHROPIC_API_KEY]" in persisted
    assert "[REDACTED:SLACK_TOKEN]" in persisted


def test_ingest_secret_redaction_can_be_disabled(fresh_mp, tmp_path, monkeypatch):
    content, values = _dialog_with_secrets()
    monkeypatch.setenv("THREADKEEPER_REDACT_DIALOG_SECRETS", "0")
    fresh_mp["config"].reload_settings()

    persisted = _ingest_one(fresh_mp, tmp_path, content)

    for raw in values.values():
        assert raw in persisted


def test_fts_backfill_scrubs_legacy_dialog_rows(fresh_mp):
    from threadkeeper import ingest

    conn = fresh_mp["db"].get_db()
    content, values = _dialog_with_secrets()
    for idx in range(6):
        body = content if idx == 0 else f"ordinary message {idx}"
        conn.execute(
            "INSERT INTO dialog_messages "
            "(uuid, source, project, session_id, role, content, model, created_at) "
            "VALUES (?, 'fake-cli', 'fake-project', 'sess', 'user', ?, '', ?)",
            (f"legacy-{idx}", body, 1_800_000_000 + idx),
        )
    conn.commit()

    ingest._backfill_dialog_fts_if_empty(conn)

    fts = conn.execute(
        "SELECT content FROM dialog_fts WHERE uuid='legacy-0'"
    ).fetchone()["content"]
    for raw in values.values():
        assert raw not in fts
    assert "Authorization: Bearer [REDACTED:AUTHORIZATION]" in fts
    assert "_authToken=[REDACTED:NPMRC_CREDENTIAL]" in fts
