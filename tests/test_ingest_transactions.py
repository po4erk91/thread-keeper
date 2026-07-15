"""Ingest prepares expensive payloads before taking SQLite's writer slot."""
from __future__ import annotations

from pathlib import Path

from threadkeeper.adapters.base import NormalizedMessage


class _Adapter:
    name = "txn-test"

    def __init__(self, messages):
        self.messages = messages

    def iter_messages(self, _path: Path):
        yield from self.messages

    def project_label(self, _path: Path) -> str:
        return "txn-project"


def _message(idx: int) -> NormalizedMessage:
    return NormalizedMessage(
        uuid=f"txn-{idx}",
        session_id="txn-session",
        role="user",
        content=f"expensive embedding payload number {idx}",
        model="test",
        created_at=1_900_000_000 + idx,
        raw={"role": "user"},
    )


def test_embeddings_finish_before_first_ingest_write(fresh_mp, tmp_path,
                                                     monkeypatch):
    from threadkeeper import ingest

    conn = fresh_mp["db"].get_db()
    fp = tmp_path / "conversation.jsonl"
    fp.write_text("fixture\n", encoding="utf-8")
    seen_transaction_state: list[bool] = []

    monkeypatch.setattr(ingest, "SEMANTIC_AVAILABLE", True)

    def _fake_embed(_text: str):
        seen_transaction_state.append(conn.in_transaction)
        return None

    monkeypatch.setattr(ingest, "_embed", _fake_embed)
    added = ingest._ingest_file(
        conn, fp, max_msgs=10, adapter=_Adapter([_message(1), _message(2)])
    )
    try:
        assert added == 2
        assert seen_transaction_state == [False, False]
        assert conn.in_transaction is True  # writes are pending only afterward
    finally:
        conn.rollback()
        conn.close()


def test_initial_ingest_is_disabled_in_test_runtime(fresh_mp):
    from threadkeeper import ingest

    ingest._start_initial_ingest()
    assert ingest._initial_ingest_thread is None
