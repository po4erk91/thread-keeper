"""Step 2: the opt-in re-id migration (INTEGER PK -> global TEXT id).

Seeds the pre-migration schema shape (integer notes/verbatim/edges + short-hex
threads/concepts + cross references), runs the migration on the temp DB, and
asserts every id became a global ULID with all references rewritten in
lockstep. See docs/sync.md."""
from __future__ import annotations

import sqlite3


def _seed(conn):
    """Insert rows with the OLD id shapes + the reference edges between them."""
    now = 1_700_000_000
    # short-hex TEXT ids (as _gen_short_id would produce pre-migration)
    conn.execute("INSERT INTO threads(id,question,state,opened_at,last_touched_at)"
                 " VALUES('T001','q','active',?,?)", (now, now))
    conn.execute("INSERT INTO threads(id,question,state,parent_id,opened_at,last_touched_at)"
                 " VALUES('T002','q2','active','T001',?,?)", (now, now))
    conn.execute("INSERT INTO concepts(id,description,registered_at)"
                 " VALUES('C001','desc',?)", (now,))
    conn.execute("INSERT INTO distill(id,content,created_at) VALUES('D001','x',?)", (now,))
    conn.execute("INSERT INTO distill_votes(distill_id,voter_cid,weight,voted_at)"
                 " VALUES('D001','cidA',1.0,?)", (now,))
    conn.execute("INSERT INTO user_dialectic(id,claim,created_at) VALUES('UC01','c',?)", (now,))
    conn.execute("INSERT INTO user_dialectic(id,claim,superseded_by,state,created_at)"
                 " VALUES('UC02','c2','UC01','superseded',?)", (now,))
    conn.execute("INSERT INTO probes(id,category,prompt,created_at)"
                 " VALUES('P001','cat','p',?)", (now,))
    # INTEGER autoincrement rows
    conn.execute("INSERT INTO notes(id,thread_id,content,kind,created_at)"
                 " VALUES(1,'T001','n1','move',?)", (now,))
    conn.execute("INSERT INTO notes(id,thread_id,content,kind,created_at)"
                 " VALUES(2,'T002','n2','insight',?)", (now,))
    conn.execute("INSERT INTO verbatim(id,speaker,content,thread_id,created_at)"
                 " VALUES(1,'user','v','T001',?)", (now,))
    conn.execute("INSERT INTO probe_results(id,probe_id,category,success,created_at)"
                 " VALUES(1,'P001','cat',1,?)", (now,))
    conn.execute("INSERT INTO dialectic_evidence(id,claim_id,kind,created_at)"
                 " VALUES(1,'UC01','support',?)", (now,))
    conn.execute("INSERT INTO evolve(id,suggestion,created_at) VALUES(1,'do x',?)", (now,))
    # edges: polymorphic refs to a note (int id) and a concept (short-hex)
    conn.execute("INSERT INTO edges(from_kind,from_id,to_kind,to_id,relation,created_at)"
                 " VALUES('note','1','concept','C001','mentions',?)", (now,))
    conn.execute("INSERT INTO edges(from_kind,from_id,to_kind,to_id,relation,created_at)"
                 " VALUES('thread','T001','distill','D001','elaborates',?)", (now,))
    conn.commit()


def test_migration_reids_and_fixes_refs(fresh_mp):
    from threadkeeper.sync import migrate, SYNC_SCHEMA_VERSION
    db = fresh_mp["db"]
    conn = db.get_db()
    _seed(conn)
    conn.close()

    db_path = db.DB_PATH
    # dry-run writes nothing
    assert migrate.apply(db_path, do_apply=False) == 0
    c = sqlite3.connect(str(db_path))
    assert migrate._sync_version(c) == 0
    assert c.execute("SELECT id FROM notes WHERE id='1'").fetchone() is not None
    c.close()

    # apply
    assert migrate.apply(db_path, do_apply=True) == 0

    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    try:
        assert migrate._sync_version(c) == SYNC_SCHEMA_VERSION
        # notes.id is now TEXT ULID, not the old integer
        notes = {r["content"]: r["id"] for r in c.execute("SELECT id,content FROM notes")}
        assert set(notes) == {"n1", "n2"}
        for nid in notes.values():
            assert not nid.isdigit() and len(nid) == 26  # ULID, no prefix

        # thread self-ref parent_id remapped
        t = {r["question"]: r for r in c.execute("SELECT id,question,parent_id FROM threads")}
        assert t["q2"]["parent_id"] == t["q"]["id"]
        assert t["q"]["id"].startswith("T") and len(t["q"]["id"]) == 27

        # notes.thread_id remapped to the new thread ids
        for r in c.execute("SELECT content,thread_id FROM notes"):
            assert r["thread_id"] in {t["q"]["id"], t["q2"]["id"]}

        # declared FK fixups
        assert c.execute("SELECT distill_id FROM distill_votes").fetchone()[0] == \
            c.execute("SELECT id FROM distill").fetchone()[0]
        assert c.execute("SELECT claim_id FROM dialectic_evidence").fetchone()[0] == \
            c.execute("SELECT id FROM user_dialectic WHERE claim='c'").fetchone()[0]
        assert c.execute("SELECT superseded_by FROM user_dialectic WHERE claim='c2'").fetchone()[0] == \
            c.execute("SELECT id FROM user_dialectic WHERE claim='c'").fetchone()[0]
        assert c.execute("SELECT probe_id FROM probe_results").fetchone()[0] == \
            c.execute("SELECT id FROM probes").fetchone()[0]

        # polymorphic edges remapped for note + concept + thread + distill kinds
        note_id = notes["n1"]
        concept_id = c.execute("SELECT id FROM concepts").fetchone()[0]
        e1 = c.execute("SELECT from_id,to_id FROM edges WHERE relation='mentions'").fetchone()
        assert e1["from_id"] == note_id and e1["to_id"] == concept_id
        e2 = c.execute("SELECT from_id,to_id FROM edges WHERE relation='elaborates'").fetchone()
        assert e2["from_id"] == t["q"]["id"]
        assert e2["to_id"] == c.execute("SELECT id FROM distill").fetchone()[0]

        # baseline HLC + origin stamped on replicated rows
        r = c.execute("SELECT hlc,origin_node FROM notes LIMIT 1").fetchone()
        assert r["hlc"] and r["origin_node"] and r["origin_node"].startswith("N")
    finally:
        c.close()


def test_migration_idempotent(fresh_mp):
    from threadkeeper.sync import migrate
    db = fresh_mp["db"]
    conn = db.get_db()
    _seed(conn)
    conn.close()
    assert migrate.apply(db.DB_PATH, do_apply=True) == 0
    # second apply is a clean no-op (already at target sync_schema_version)
    assert migrate.apply(db.DB_PATH, do_apply=True) == 0


def test_migration_rebuilds_notes_fts_and_indexes(fresh_mp):
    """Blocker #2 regression: the re-id migration drops notes' indexes +
    notes_fts triggers and leaves notes_fts keyed on the (now-TEXT) id. Both
    pre-existing and newly-inserted notes must stay FTS-searchable, and the
    indexes/triggers must be restored."""
    from threadkeeper.sync import migrate
    db = fresh_mp["db"]
    conn = db.get_db()
    conn.execute("INSERT INTO threads(id,question,state,opened_at,last_touched_at)"
                 " VALUES('T1','q','active',1,1)")
    conn.execute("INSERT INTO notes(thread_id,content,kind,created_at)"
                 " VALUES('T1','platypus pre-migration',?,1)", ("move",))
    conn.commit()
    conn.close()

    assert migrate.apply(db.DB_PATH, do_apply=True) == 0

    conn = db.get_db()
    try:
        def _fts(term):
            return [r[0] for r in conn.execute(
                "SELECT n.content FROM notes_fts f JOIN notes n ON n.rowid=f.rowid "
                "WHERE notes_fts MATCH ?", (term,)).fetchall()]

        # pre-existing note is still searchable after migration
        assert any("platypus" in c for c in _fts("platypus")), _fts("platypus")
        # FTS triggers were recreated
        trigs = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name IN ('notes_fts_ai','notes_fts_ad')")}
        assert trigs == {"notes_fts_ai", "notes_fts_ad"}, trigs
        # ordinary indexes were recreated
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name IN ('idx_notes_thread','idx_notes_created')")}
        assert idx == {"idx_notes_thread", "idx_notes_created"}, idx
        # notes_fts is keyed on the integer rowid, not the TEXT id
        fts_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='notes_fts'").fetchone()[0]
        assert "content_rowid='rowid'" in fts_sql, fts_sql
        # a NEW note inserted post-migration is searchable via the recreated trigger
        tid = conn.execute("SELECT id FROM threads LIMIT 1").fetchone()[0]
        conn.execute("INSERT INTO notes(thread_id,content,kind,created_at)"
                     " VALUES(?,?,?,2)", (tid, "narwhal post-migration", "move"))
        conn.commit()
        assert any("narwhal" in c for c in _fts("narwhal")), _fts("narwhal")
    finally:
        conn.close()
