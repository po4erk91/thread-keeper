"""Fixed synthetic subjects expose the missing context in the raw baseline."""
import json


def test_context_report_measures_real_retrieval(fresh_mp, tmp_path, monkeypatch):
    monkeypatch.setenv("THREADKEEPER_PROJECT_NAMESPACE", "prj_aaaaaaaaaaaaaaaaaaaa")
    from threadkeeper import embeddings, memory_compat
    from threadkeeper.eval.contextual_retrieval import measure

    tools = fresh_mp["mcp"]._tool_manager._tools
    cases = []
    conn = fresh_mp["db"].get_db()
    for topic in ("Aurora deployment rollback", "Cobalt invoice reconciliation", "Orchid warehouse inventory"):
        tid = tools["open_thread"].fn(question=topic)
        tools["note"].fn(thread_id=tid, content="Fixed by switching it off.")
        row = conn.execute("SELECT id,content FROM notes WHERE thread_id=?", (tid,)).fetchone()
        cases.append((topic, str(row[0])))
        blob = embeddings._embed(row[1])
        assert blob is not None
        memory_compat.stage_vectors(conn, embeddings.embedding_fingerprint(contextual=False), "note", [(row[0], row[1])], [blob])
        conn.execute(
            "INSERT INTO notes(content,kind,created_at,project_id) VALUES(?,'move',1,'foreign-project')", (topic,),
        )
        conn.commit()
    conn.commit()
    report = measure(conn, cases)
    artifact = tmp_path / "contextual-retrieval.json"
    artifact.write_text(json.dumps(report, indent=2) + "\n")
    assert json.loads(artifact.read_text()) == report
    assert report["body_fts"]["recall_at_3"] == 0
    for mode in ("fts", "hybrid"):
        assert report[f"context_{mode}"]["recall_at_3"] == 1
        assert report[f"context_{mode}"]["mrr"] == 1
    assert all(value["wrong_scope_hits"] == 0 for key, value in report.items() if key != "index")
    assert report["index"]["context_text_bytes"] > report["index"]["body_text_bytes"]
    print(json.dumps(report, sort_keys=True))
    conn.close()
