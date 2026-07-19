from __future__ import annotations

import time


def _tool(pkg, name: str):
    return pkg["mcp"]._tool_manager._tools[name].fn


def _count(conn, table: str, where: str = "1=1", params: tuple = ()) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()
    return int(row[0] or 0)


def _match_uuids(conn, term: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT d.uuid FROM dialog_fts f "
            "JOIN dialog_messages d ON d.rowid = f.rowid "
            "WHERE dialog_fts MATCH ? ORDER BY rank",
            (term,),
        ).fetchall()
    ]


def _ensure_vec_tables(conn, db) -> None:
    if db.vec_available():
        return
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dialog_vec_map ("
        "rowid INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL UNIQUE)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dialog_vec ("
        "rowid INTEGER PRIMARY KEY, embedding BLOB)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS notes_vec ("
        "id INTEGER PRIMARY KEY, embedding BLOB)"
    )


def _vec_blob(config) -> bytes:
    try:
        import sqlite_vec  # type: ignore

        return sqlite_vec.serialize_float32([0.01] * config.EMBED_DIM)
    except Exception:
        return b"vec0"


def _upsert_dialog_vec(conn, uuid: str, config) -> None:
    row = conn.execute(
        "SELECT rowid FROM dialog_vec_map WHERE uuid=?",
        (uuid,),
    ).fetchone()
    if row is None:
        cur = conn.execute("INSERT INTO dialog_vec_map(uuid) VALUES (?)", (uuid,))
        rowid = cur.lastrowid
    else:
        rowid = row[0]
    conn.execute(
        "INSERT OR REPLACE INTO dialog_vec(rowid, embedding) VALUES (?, ?)",
        (rowid, _vec_blob(config)),
    )


def test_forget_dry_run_then_apply_cascades_session(fresh_mp, tmp_path):
    from threadkeeper.lessons import append_lesson, iter_lessons

    db = fresh_mp["db"]
    config = fresh_mp["config"]
    conn = db.get_db()
    _ensure_vec_tables(conn, db)
    now = int(time.time())
    cid = "cid-forget-1"
    keep_cid = "cid-keep-1"

    for uuid, session_id, content in [
        ("forget-a", cid, "eraseable alpha marker"),
        ("forget-b", cid, "eraseable beta marker"),
        ("keep-a", keep_cid, "durable gamma marker"),
    ]:
        conn.execute(
            "INSERT INTO dialog_messages "
            "(uuid, source, project, session_id, role, content, model, created_at) "
            "VALUES (?, 'pytest', 'proj', ?, 'user', ?, NULL, ?)",
            (uuid, session_id, content, now),
        )
        _upsert_dialog_vec(conn, uuid, config)

    conn.execute(
        "INSERT INTO threads (id, question, state, opened_at, last_touched_at) "
        "VALUES ('Tkeep', 'keep topic', 'active', ?, ?)",
        (now, now),
    )
    cur = conn.execute(
        "INSERT INTO notes (thread_id, content, kind, created_at, session_id) "
        "VALUES ('Tkeep', 'note to erase', 'move', ?, ?)",
        (now, cid),
    )
    forget_note_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO notes_vec(id, embedding) VALUES (?, ?)",
        (forget_note_id, _vec_blob(config)),
    )
    cur = conn.execute(
        "INSERT INTO notes (thread_id, content, kind, created_at, session_id) "
        "VALUES ('Tkeep', 'note to keep', 'move', ?, ?)",
        (now, keep_cid),
    )
    keep_note_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO notes_vec(id, embedding) VALUES (?, ?)",
        (keep_note_id, _vec_blob(config)),
    )

    cur = conn.execute(
        "INSERT INTO verbatim (speaker, content, thread_id, created_at, session_id) "
        "VALUES ('user', 'quote to erase', 'Tkeep', ?, ?)",
        (now, cid),
    )
    verbatim_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO dialectic_observations "
        "(dialog_uuid, user_quote, context, source_cid, status, created_at) "
        "VALUES ('forget-a', 'quote', 'context', ?, 'pending', ?)",
        (cid, now),
    )
    conn.execute(
        "INSERT INTO user_dialectic "
        "(id, claim, domain, created_by_cid, created_at, valid_from) "
        "VALUES ('UCerase', 'erase-only claim', 'workflow', 'other', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO dialectic_evidence "
        "(claim_id, kind, source, quote, weight, created_at) "
        "VALUES ('UCerase', 'support', 'dialog:forget-a', 'quote', 1.0, ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO user_dialectic "
        "(id, claim, domain, support_count, created_by_cid, created_at, valid_from) "
        "VALUES ('UCmixed', 'mixed claim', 'workflow', 2, 'other', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO dialectic_evidence "
        "(claim_id, kind, source, quote, weight, created_at) "
        "VALUES ('UCmixed', 'support', 'dialog:forget-b', 'quote', 1.0, ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO dialectic_evidence "
        "(claim_id, kind, source, quote, weight, created_at) "
        "VALUES ('UCmixed', 'support', 'manual', 'keep quote', 1.0, ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO dialectic_evidence "
        "(claim_id, kind, source, quote, weight, created_at) "
        "VALUES ('UCmixed', 'support', ?, 'quote', 1.0, ?)",
        (f"verbatim:{verbatim_id}", now),
    )
    conn.execute(
        "INSERT INTO extract_candidates "
        "(kind, source_uuid, source_cid, content, status, created_at) "
        "VALUES ('note', 'forget-a', ?, 'candidate', 'pending', ?)",
        (cid, now),
    )
    conn.execute(
        "INSERT INTO tasks "
        "(id, pid, parent_cid, spawned_cid, cwd, prompt, started_at, ended_at) "
        "VALUES ('tk_forget_demo', 0, ?, NULL, '/tmp', 'secret prompt', ?, ?)",
        (cid, now, now),
    )
    conn.execute(
        "INSERT INTO signals (from_cid, to_cid, kind, content, created_at) "
        "VALUES (?, 'peer', 'whisper', 'secret signal', ?)",
        (cid, now),
    )
    conn.execute(
        "INSERT INTO events (session_id, kind, target, summary, created_at) "
        "VALUES (?, 'note:move', 'Tkeep', 'secret summary', ?)",
        (cid, now),
    )
    conn.execute(
        "INSERT INTO sessions (id, started_at, client) VALUES (?, ?, 'pytest')",
        (cid, now),
    )
    conn.execute(
        "INSERT INTO cursors (session_id, last_event_id, updated_at) "
        "VALUES (?, 1, ?)",
        (cid, now),
    )
    conn.execute(
        "INSERT INTO presence (session_id, started_at, heartbeat_at) "
        "VALUES (?, ?, ?)",
        (cid, now, now),
    )
    conn.commit()

    task_dir = config.TASK_LOG_DIR
    task_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "tk_forget_demo.log",
        "tk_forget_demo.stdin.txt",
        "tk_forget_demo.command",
        "slim-mcp-tk_forget_demo.json",
    ):
        (task_dir / name).write_text("secret")
    safe_dir = task_dir / "gh-safe-tk_forget_demo"
    safe_dir.mkdir()
    (safe_dir / "gh").write_text("secret")

    append_lesson(
        "Forget cites dialog",
        "A derived lesson cites dialog:forget-a.",
        "cites source",
        source=cid,
    )
    skill_dir = config.CLAUDE_SKILLS_DIR / "forget-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: forget-skill\ndescription: cites erased source\n---\n"
        "This skill cites dialog:forget-a.\n"
    )

    dry = _tool(fresh_mp, "forget")(cid)

    assert "mode=dry_run" in dry
    assert "dialog_messages=2" in dry
    assert "dialog_fts=2" in dry
    assert "dialog_vec_map=2" in dry
    assert "notes=1" in dry
    assert "verbatim=1" in dry
    assert "dialectic_observations=1" in dry
    assert "review_required lessons=1 skills=1" in dry
    assert _count(conn, "dialog_messages", "session_id=?", (cid,)) == 2
    assert _match_uuids(conn, "eraseable") == ["forget-a", "forget-b"]

    applied = _tool(fresh_mp, "forget")(cid, dry_run=False)

    assert "mode=applied" in applied
    assert "dialog_messages=2" in applied
    assert "residuals" in applied
    assert _count(conn, "dialog_messages", "session_id=?", (cid,)) == 0
    assert _count(conn, "dialog_messages", "session_id=?", (keep_cid,)) == 1
    assert _match_uuids(conn, "eraseable") == []
    assert _match_uuids(conn, "durable") == ["keep-a"]
    assert _count(conn, "dialog_fts_docsize") == 1
    assert _count(conn, "dialog_vec_map", "uuid IN ('forget-a','forget-b')") == 0
    assert _count(conn, "dialog_vec_map", "uuid='keep-a'") == 1
    assert _count(conn, "notes", "session_id=?", (cid,)) == 0
    assert _count(conn, "notes", "session_id=?", (keep_cid,)) == 1
    assert _count(conn, "notes_vec", "id=?", (forget_note_id,)) == 0
    assert _count(conn, "notes_vec", "id=?", (keep_note_id,)) == 1
    assert _count(conn, "verbatim", "session_id=?", (cid,)) == 0
    assert _count(conn, "dialectic_observations") == 0
    assert _count(conn, "user_dialectic", "id='UCerase'") == 0
    mixed = conn.execute(
        "SELECT support_count, contradict_count, confidence "
        "FROM user_dialectic WHERE id='UCmixed'"
    ).fetchone()
    assert mixed["support_count"] == 1
    assert mixed["contradict_count"] == 0
    assert mixed["confidence"] == "medium"
    assert _count(conn, "dialectic_evidence", "claim_id='UCmixed'") == 1
    assert _count(conn, "extract_candidates") == 0
    assert _count(conn, "tasks", "id='tk_forget_demo'") == 0
    assert _count(conn, "signals", "from_cid=?", (cid,)) == 0
    assert _count(conn, "events", "session_id=?", (cid,)) == 0
    assert _count(conn, "sessions", "id=?", (cid,)) == 0
    assert _count(conn, "cursors", "session_id=?", (cid,)) == 0
    assert _count(conn, "presence", "session_id=?", (cid,)) == 0
    assert not (task_dir / "tk_forget_demo.log").exists()
    assert not (task_dir / "tk_forget_demo.stdin.txt").exists()
    assert not (task_dir / "tk_forget_demo.command").exists()
    assert not (task_dir / "slim-mcp-tk_forget_demo.json").exists()
    assert not safe_dir.exists()
    assert list(iter_lessons())
    assert (skill_dir / "SKILL.md").exists()

    assert _count(
        conn,
        "dialog_vec_map m LEFT JOIN dialog_messages d ON d.uuid=m.uuid",
        "d.uuid IS NULL",
    ) == 0
    assert _count(
        conn,
        "dialog_vec v LEFT JOIN dialog_vec_map m ON m.rowid=v.rowid",
        "m.rowid IS NULL",
    ) == 0
    assert _count(
        conn,
        "notes_vec v LEFT JOIN notes n ON n.id=v.id",
        "n.id IS NULL",
    ) == 0


def test_forget_after_reid_migration(fresh_mp):
    """R3-B2 regression: after the sync re-id migration, notes/verbatim/
    dialectic ids are TEXT ULIDs. build_forget_plan must not raise int()-cast
    errors, and apply must remove the note plus its FTS/vec/map sidecars with no
    false orphans in the integrity report."""
    from threadkeeper.sync import migrate
    from threadkeeper import forget
    from threadkeeper.embeddings import _vec_upsert_note, _notes_mapped
    db = fresh_mp["db"]
    config = fresh_mp["config"]

    conn = db.get_db()
    _ensure_vec_tables(conn, db)
    conn.execute("INSERT INTO threads(id,question,state,opened_at,last_touched_at)"
                 " VALUES('Tfg','q','active',1,1)")
    conn.commit()
    conn.close()

    assert migrate.apply(db.DB_PATH, do_apply=True) == 0
    assert _notes_mapped(db.get_db())  # sanity: migrated

    conn = db.get_db()
    try:
        # post-migration inserts get TEXT ULID ids via the column DEFAULT
        conn.execute("INSERT INTO notes(thread_id,content,kind,created_at,session_id)"
                     " VALUES('Tfg','forget me capybara','move',1,'cid-fg')")
        note_id = conn.execute(
            "SELECT id FROM notes WHERE content LIKE 'forget me%'").fetchone()[0]
        _vec_upsert_note(conn, note_id, _vec_blob(config))  # creates notes_vec_map row
        conn.execute("INSERT INTO verbatim(speaker,content,thread_id,created_at)"
                     " VALUES('user','verbatim capybara','Tfg',1)")
        conn.commit()
        assert not note_id.isdigit()  # TEXT ULID, the case that broke int() cast

        # build a plan by thread — the previously-crashing path (_notes cast=int)
        plan = forget.build_forget_plan(conn, "Tfg", "thread_id")
        assert note_id in plan.note_ids
        assert plan.verbatim_ids and all(not str(v).isdigit() for v in plan.verbatim_ids)
        assert plan.counts["notes_vec"] == 1 and plan.counts["notes_vec_map"] == 1

        deleted = forget._apply_forget(conn, plan)
        conn.commit()
        assert deleted["notes"] == 1
        assert deleted["notes_vec"] == 1 and deleted["notes_vec_map"] == 1

        # base row + all sidecars gone
        assert conn.execute("SELECT COUNT(*) FROM notes WHERE id=?",
                            (note_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM notes_vec_map WHERE gid=?",
                            (note_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM verbatim").fetchone()[0] == 0
        # integrity report reports no false orphans
        orphans = forget._orphan_counts(conn)
        assert orphans["notes_vec_orphans"] == 0
        assert orphans["notes_vec_map_orphans"] == 0
        assert orphans["notes_fts_orphans"] == 0
    finally:
        conn.close()
