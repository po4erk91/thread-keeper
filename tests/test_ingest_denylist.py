from __future__ import annotations

import time
from pathlib import Path

from threadkeeper.adapters.base import NormalizedMessage


class _FakeAdapter:
    name = "fake-cli"

    def __init__(self, messages: list[NormalizedMessage]) -> None:
        self._messages = messages
        self.iter_calls = 0
        self.files: list[Path] = []

    def transcript_files(self) -> list[Path]:
        return self.files

    def iter_messages(self, _fp: Path):
        self.iter_calls += 1
        yield from self._messages

    def project_label(self, _fp: Path) -> str:
        return "fake-project"


def _message(uuid: str, content: str, origin_path: str) -> NormalizedMessage:
    return NormalizedMessage(
        uuid=uuid,
        session_id="denylist-session",
        role="user",
        content=content,
        model="",
        created_at=int(time.time()),
        raw={"role": "user", "content": content},
        origin_path=origin_path,
    )


def test_denylisted_messages_never_persist_or_reach_shadow_window(
    fresh_mp, tmp_path, monkeypatch
):
    from threadkeeper import ingest, shadow_review

    sensitive_root = tmp_path / "sensitive-client"
    allowed_root = tmp_path / "ordinary-project"
    monkeypatch.setenv("THREADKEEPER_INGEST_DENY_GLOBS", str(sensitive_root))
    fresh_mp["config"].reload_settings()
    monkeypatch.setattr(ingest, "SEMANTIC_AVAILABLE", False)

    transcript = tmp_path / "session.jsonl"
    transcript.write_text("fixture\n", encoding="utf-8")
    adapter = _FakeAdapter([
        _message("denied-1", "client secret must not persist", str(sensitive_root)),
        _message("allowed-1", "ordinary project can persist", str(allowed_root)),
    ])
    conn = fresh_mp["db"].get_db()
    skipped = [0]

    assert ingest._ingest_file(
        conn, transcript, max_msgs=100, adapter=adapter, skipped_counter=skipped
    ) == 1
    conn.commit()

    assert skipped == [1]
    assert conn.execute(
        "SELECT 1 FROM dialog_messages WHERE uuid='denied-1'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM dialog_fts WHERE dialog_fts MATCH 'client secret'"
    ).fetchone() is None
    dump, _, _, _ = shadow_review._collect_window(conn, 0, 3600)
    assert "ordinary project can persist" in dump
    assert "client secret must not persist" not in dump

    state = conn.execute(
        "SELECT last_size, last_mtime FROM ingest_state WHERE file_path=?",
        (str(transcript),),
    ).fetchone()
    assert state["last_size"] == transcript.stat().st_size
    assert state["last_mtime"] == int(transcript.stat().st_mtime)
    assert ingest._ingest_file(conn, transcript, max_msgs=100, adapter=adapter) == 0
    assert adapter.iter_calls == 1


def test_denylist_file_and_status_report_skipped_messages(
    fresh_mp, tmp_path, monkeypatch
):
    from threadkeeper import ingest

    denylist = tmp_path / "ingest_denylist.txt"
    sensitive_root = tmp_path / "regulated"
    denylist.write_text(
        "# do not index this client\n" + str(sensitive_root) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("THREADKEEPER_INGEST_DENYLIST_FILE", str(denylist))
    fresh_mp["config"].reload_settings()
    monkeypatch.setattr(ingest, "SEMANTIC_AVAILABLE", False)

    transcript = tmp_path / "session.jsonl"
    transcript.write_text("fixture\n", encoding="utf-8")
    adapter = _FakeAdapter([
        _message("denied-file-1", "regulated transcript text", str(sensitive_root)),
    ])
    adapter.files = [transcript]
    conn = fresh_mp["db"].get_db()
    from threadkeeper import adapters
    monkeypatch.setattr(adapters, "installed_adapters", lambda: [adapter])
    assert ingest._ingest_all(conn) == (0, 1)

    out = fresh_mp["mcp"]._tool_manager._tools["mp_dashboard"].fn()
    assert f"denylist={sensitive_root}" in out
    assert "ingest_denied_messages=1" in out
