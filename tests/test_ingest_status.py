from __future__ import annotations

import threading


_FAKE_CID = "77778888-9999-aaaa-bbbb-ccccdddd0000"


def test_ingest_pass_event_recorded_for_status_ui(mp_with_cid):
    pkg = mp_with_cid(_FAKE_CID)
    conn = pkg["db"].get_db()
    pkg["identity"]._ensure_session(conn)

    from threadkeeper import ingest

    ingest._last_ingest_event_at = 0
    ingest._record_ingest_pass(
        conn,
        mode="recent",
        new_msgs=2,
        files_seen=5,
    )

    row = conn.execute(
        "SELECT kind, target, summary FROM events "
        "WHERE kind='ingest_pass' ORDER BY id DESC LIMIT 1"
    ).fetchone()

    assert row["kind"] == "ingest_pass"
    assert row["target"]
    assert row["summary"] == "ok mode=recent new=2 files=5"


def test_start_background_ingester_respects_disable_bg_daemons(mp_with_cid, monkeypatch):
    """BACKGROUND_DAEMONS_ALLOWED=False must block the daemon thread even
    when the ingest interval is positive."""
    mp_with_cid(_FAKE_CID)
    from threadkeeper import ingest
    monkeypatch.setattr(ingest, "_ingest_interval_s", 3.0)

    before = {t.name for t in threading.enumerate()}
    ingest._start_background_ingester()
    after = {t.name for t in threading.enumerate()}
    assert "thread-keeper-live-ingest" not in (after - before)
