"""Antigravity CLI transcript ingestion (#20).

agy keeps each conversation in one SQLite file whose `steps` rows carry
protobuf payloads. These tests build that layout by hand so the reader is
pinned to the few fields it decodes, without a real agy install.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

CASCADE = "0f0f0f0f-1111-2222-3333-444444444444"
FORCED = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
DONE, GENERATING = 3, 8


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | 0x80 if n else byte)
        if not n:
            return bytes(out)


def _field(number: int, value) -> bytes:
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    if isinstance(value, str):
        value = value.encode()
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def _metadata(ts: int, turn: str) -> bytes:
    return _field(1, _field(1, ts) + _field(2, 5)) + _field(12, turn)


def _user(text: str) -> bytes:
    return _field(1, 14) + _field(19, _field(2, text) + _field(3, _field(1, text)))


def _model(text: str = "", tool_call: str = "") -> bytes:
    body = _field(3, "private reasoning")
    if text:
        body += _field(1, text) + _field(8, text)
    if tool_call:
        body += _field(7, tool_call)
    return _field(1, 15) + _field(20, body)


def _create(fp: Path, steps, workspace: str = "file:///Users/me/My%20Proj"):
    conn = sqlite3.connect(fp)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE steps (idx integer PRIMARY KEY, step_type integer NOT NULL "
        "DEFAULT 0, status integer NOT NULL DEFAULT 0, metadata blob, "
        "step_payload blob)"
    )
    conn.execute(
        "CREATE TABLE trajectory_metadata_blob (id text PRIMARY KEY, data blob)"
    )
    if workspace:
        conn.execute(
            "INSERT INTO trajectory_metadata_blob VALUES ('main', ?)",
            (_field(1, _field(1, workspace)) + _field(7, workspace),),
        )
    conn.executemany("INSERT INTO steps VALUES (?, ?, ?, ?, ?)", steps)
    conn.commit()
    return conn


def _conversation(tmp_path: Path, steps, **kw) -> Path:
    root = tmp_path / "conversations"
    root.mkdir(exist_ok=True)
    fp = root / f"{CASCADE}.db"
    _create(fp, steps, **kw).close()
    return fp


def _adapter(tmp_path: Path):
    from threadkeeper.adapters.antigravity import AntigravityAdapter

    adapter = AntigravityAdapter()
    adapter.conversations_root = tmp_path / "conversations"
    return adapter


def test_reads_user_prompts_and_final_model_answers(tmp_path):
    turn = "turn-1"
    fp = _conversation(tmp_path, [
        (0, 14, DONE, _metadata(1_790_000_000, turn), _user("fix the conflicts")),
        (1, 15, DONE, _metadata(1_790_000_005, turn), _model(tool_call="list_dir")),
        (2, 21, DONE, _metadata(1_790_000_006, turn), b"tool output step"),
        (3, 15, DONE, b"\xff\xff", b"\x0a\xff"),  # malformed: skipped
        (4, 15, DONE, _metadata(1_790_000_009, turn), _model("All conflicts fixed.")),
        (5, 15, GENERATING, _metadata(1_790_000_010, turn), _model("Half an ans")),
    ])
    adapter = _adapter(tmp_path)

    assert adapter.transcript_files() == [fp]
    msgs = list(adapter.iter_messages(fp))

    assert [(m.role, m.content) for m in msgs] == [
        ("user", "fix the conflicts"),
        ("assistant", "All conflicts fixed."),
    ]
    assert [m.uuid for m in msgs] == [
        f"antigravity:{CASCADE}:0:{turn}", f"antigravity:{CASCADE}:4:{turn}",
    ]
    assert [m.created_at for m in msgs] == [1_790_000_000, 1_790_000_009]
    assert {m.session_id for m in msgs} == {CASCADE}
    assert {m.origin_path for m in msgs} == {"/Users/me/My Proj"}
    assert adapter.project_label(fp) == "antigravity"


def test_spawned_child_adopts_the_forced_cid(tmp_path):
    preamble = (
        "You were spawned in the background by parent conversation p-1. "
        f"Your own cid is {FORCED} (forced via --session-id and "
        "THREADKEEPER_FORCE_CID env).\n\n---\n\nreview the lessons"
    )
    fp = _conversation(tmp_path, [
        (0, 14, DONE, _metadata(1_790_000_000, "t"), _user(preamble)),
        (1, 15, DONE, _metadata(1_790_000_001, "t"), _model("Reviewed.")),
    ], workspace="")

    msgs = list(_adapter(tmp_path).iter_messages(fp))

    assert {m.session_id for m in msgs} == {FORCED}
    assert {m.origin_path for m in msgs} == {""}


def test_unknown_schema_is_skipped_and_nothing_is_written_next_to_it(tmp_path):
    root = tmp_path / "conversations"
    root.mkdir()
    other = root / "not-agy.db"
    conn = sqlite3.connect(other)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE unrelated (x)")
    conn.commit()
    conn.close()
    fp = _conversation(tmp_path, [
        (0, 14, DONE, _metadata(1_790_000_000, "t"), _user("hello there agy")),
    ])
    adapter = _adapter(tmp_path)
    before = sorted(p.name for p in root.iterdir())

    assert list(adapter.iter_messages(other)) == []
    assert len(list(adapter.iter_messages(fp))) == 1
    assert sorted(p.name for p in root.iterdir()) == before


def test_live_wal_steps_are_read_and_move_the_change_signature(tmp_path):
    fp = _conversation(tmp_path, [
        (0, 14, DONE, _metadata(1_790_000_000, "t"), _user("first question")),
    ])
    adapter = _adapter(tmp_path)
    at_rest = adapter.transcript_stat(fp)

    writer = sqlite3.connect(fp)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    try:
        writer.execute(
            "INSERT INTO steps VALUES (?, ?, ?, ?, ?)",
            (1, 15, DONE, _metadata(1_790_000_001, "t"), _model("Live answer.")),
        )
        writer.commit()

        assert fp.stat().st_size == at_rest[1]  # main file untouched
        assert adapter.transcript_stat(fp)[1] > at_rest[1]
        assert [m.content for m in adapter.iter_messages(fp)] == [
            "first question", "Live answer.",
        ]
    finally:
        writer.close()


def test_ingest_stores_agy_dialog_once_and_retries_transient_errors(
    fresh_mp, tmp_path, monkeypatch,
):
    from threadkeeper import ingest
    from threadkeeper.adapters import antigravity

    ingest.SEMANTIC_AVAILABLE = False
    conn = fresh_mp["db"].get_db()
    fp = _conversation(tmp_path, [
        (0, 14, DONE, _metadata(1_790_000_000, "t"), _user("which branch is live?")),
        (1, 15, DONE, _metadata(1_790_000_001, "t"), _model("The main branch is live.")),
    ])
    adapter = _adapter(tmp_path)

    def locked(_fp):
        raise sqlite3.OperationalError("database is locked")

    real_open = antigravity._open_readonly
    monkeypatch.setattr(antigravity, "_open_readonly", locked)
    assert ingest._ingest_file(conn, fp, max_msgs=100, adapter=adapter) == 0
    conn.commit()
    state = conn.execute(
        "SELECT last_size, last_mtime FROM ingest_state WHERE file_path=?",
        (str(fp),),
    ).fetchone()
    assert (state["last_size"], state["last_mtime"]) == (0, 0)

    monkeypatch.setattr(antigravity, "_open_readonly", real_open)
    assert ingest._ingest_file(conn, fp, max_msgs=100, adapter=adapter) == 2
    conn.commit()
    rows = conn.execute(
        "SELECT source, project, session_id, role, content FROM dialog_messages "
        "ORDER BY created_at"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("antigravity", "antigravity", CASCADE, "user", "which branch is live?"),
        ("antigravity", "antigravity", CASCADE, "assistant",
         "The main branch is live."),
    ]
    assert ingest._ingest_file(conn, fp, max_msgs=100, adapter=adapter) == 0
