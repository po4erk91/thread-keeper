from __future__ import annotations

import os
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


def _message(idx: int) -> NormalizedMessage:
    return NormalizedMessage(
        uuid=f"cursor-msg-{idx}",
        session_id="cursor-session",
        role="user",
        content=f"cursor regression message {idx}",
        model="",
        created_at=1_800_000_000 + idx,
        raw={"role": "user", "content": f"cursor regression message {idx}"},
    )


def _row_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) c FROM dialog_messages").fetchone()["c"]


def test_ingest_cap_preserves_cursor_until_file_is_fully_drained(
    fresh_mp, tmp_path
):
    from threadkeeper import ingest

    ingest.SEMANTIC_AVAILABLE = False
    conn = fresh_mp["db"].get_db()
    fp = tmp_path / "session.jsonl"
    fp.write_text("synthetic transcript marker\n", encoding="utf-8")
    adapter = _FakeAdapter([_message(i) for i in range(5)])

    assert ingest._ingest_file(conn, fp, max_msgs=2, adapter=adapter) == 2
    conn.commit()
    state = conn.execute(
        "SELECT last_size, last_mtime, msg_count FROM ingest_state "
        "WHERE file_path=?",
        (str(fp),),
    ).fetchone()
    assert state["last_size"] == 0
    assert state["last_mtime"] == 0
    assert state["msg_count"] == 2

    assert ingest._ingest_file(conn, fp, max_msgs=2, adapter=adapter) == 2
    conn.commit()
    assert _row_count(conn) == 4

    assert ingest._ingest_file(conn, fp, max_msgs=2, adapter=adapter) == 1
    conn.commit()
    assert _row_count(conn) == 5
    state = conn.execute(
        "SELECT last_size, last_mtime, msg_count FROM ingest_state "
        "WHERE file_path=?",
        (str(fp),),
    ).fetchone()
    assert state["last_size"] == fp.stat().st_size
    assert state["last_mtime"] == int(fp.stat().st_mtime)
    assert state["msg_count"] == 5

    assert ingest._ingest_file(conn, fp, max_msgs=2, adapter=adapter) == 0
    conn.commit()
    assert _row_count(conn) == 5


def test_ingest_same_second_append_uses_size_to_avoid_mtime_skip(
    fresh_mp, tmp_path
):
    from threadkeeper import ingest

    ingest.SEMANTIC_AVAILABLE = False
    conn = fresh_mp["db"].get_db()
    fp = tmp_path / "session.jsonl"
    fixed_mtime = 1_900_000_000
    fp.write_text("first\n", encoding="utf-8")
    os.utime(fp, (fixed_mtime, fixed_mtime))

    assert ingest._ingest_file(
        conn, fp, max_msgs=100, adapter=_FakeAdapter([_message(0)])
    ) == 1
    conn.commit()
    first_state = conn.execute(
        "SELECT last_size, last_mtime FROM ingest_state WHERE file_path=?",
        (str(fp),),
    ).fetchone()
    assert first_state["last_mtime"] == fixed_mtime

    fp.write_text("first\nsecond\n", encoding="utf-8")
    os.utime(fp, (fixed_mtime, fixed_mtime))
    assert fp.stat().st_size > first_state["last_size"]
    assert int(fp.stat().st_mtime) == fixed_mtime

    assert ingest._ingest_file(
        conn, fp, max_msgs=100, adapter=_FakeAdapter([_message(0), _message(1)])
    ) == 1
    conn.commit()
    assert _row_count(conn) == 2

    assert ingest._ingest_file(
        conn, fp, max_msgs=100, adapter=_FakeAdapter([_message(0), _message(1)])
    ) == 0
    conn.commit()
    assert _row_count(conn) == 2
