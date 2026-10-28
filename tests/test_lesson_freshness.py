"""Protected content and factual freshness have independent lifecycles."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

import pytest


SOURCE = "https://docs.example.org/reference"
SOURCE_REVISION = hashlib.sha256(b"official source revision one").hexdigest()
NEXT_SOURCE_REVISION = hashlib.sha256(b"official source revision two").hexdigest()
CLAIM = "The service allows ten requests per minute."


@pytest.fixture()
def audit(fresh_mp, monkeypatch):
    from threadkeeper import lessons
    from threadkeeper.tools import lessons as lesson_tools

    monkeypatch.setattr(lessons, "WRITE_ORIGIN", "foreground")
    monkeypatch.setattr(lesson_tools, "WRITE_ORIGIN", "curator")
    lessons.append_lesson("service limits", CLAIM)
    return fresh_mp


def tool(pkg, name):
    return pkg["mcp"]._tool_manager._tools[name].fn


def read_checks(pkg, slug="service-limits"):
    return json.loads(tool(pkg, "lesson_verifications")(slug))


def verify(pkg, status="contradicted", **overrides):
    values = {
        "slug": "service-limits",
        "lesson_revision": read_checks(pkg)["lesson_revision"],
        "claim": CLAIM,
        "status": status,
        "source_url": SOURCE,
        "source_revision": SOURCE_REVISION,
    }
    values.update(overrides)
    return tool(pkg, "curator_verify_lesson")(**values)


@pytest.mark.parametrize("status", ["verified", "contradicted", "unsubstantiated"])
def test_checks_preserve_content_and_warn_at_task_time(audit, status):
    from threadkeeper import curator, lessons

    path = lessons.get_path()
    before = path.read_bytes()
    check_started = int(time.time())
    assert verify(audit, status).startswith("ok ")
    assert path.read_bytes() == before
    data = read_checks(audit)
    result, = data["verifications"]
    assert result["lesson_revision"] == data["lesson_revision"]
    assert result["claim_fingerprint"] == hashlib.sha256(CLAIM.encode()).hexdigest()
    assert result["status"] == status
    assert result["source_url"] == SOURCE
    assert result["source_revision"] == SOURCE_REVISION
    assert check_started <= result["checked_at"] <= int(time.time())
    assert result["invalidated"] == ""

    body = tool(audit, "lesson_get")("service-limits")
    listing = tool(audit, "lesson_list")(query="service limits")
    inventory = curator._collect_inventory(audit["db"].get_db())[0]
    for text in (body, listing, inventory):
        assert ("WARNING factual freshness" in text) == (status != "verified")
        if status != "verified":
            assert status in text
            assert SOURCE in text
            assert CLAIM in text
    assert CLAIM in body
    refusal = tool(audit, "lesson_patch")("service-limits", CLAIM, "New limits.")
    assert refusal.startswith("ERR ")
    assert path.read_bytes() == before


def test_reaudits_update_one_record_and_clear_warning(audit, monkeypatch):
    from threadkeeper import lesson_freshness

    assert verify(audit).startswith("ok ")
    first = read_checks(audit)["verifications"][0]
    monkeypatch.setattr(lesson_freshness.time, "time", lambda: first["checked_at"] + 10)
    assert verify(audit, "unsubstantiated").startswith("ok ")
    checks = read_checks(audit)["verifications"]
    assert len(checks) == 1
    assert checks[0]["checked_at"] == first["checked_at"] + 10
    listing = tool(audit, "lesson_list")()
    assert listing.count("WARNING factual freshness") == 1
    assert verify(audit, "verified").startswith("ok ")
    assert len(read_checks(audit)["verifications"]) == 1
    assert "WARNING factual freshness" not in tool(audit, "lesson_get")("service-limits")


@pytest.mark.parametrize("status", ["verified", "contradicted", "unsubstantiated"])
def test_lesson_edits_invalidate_every_verdict_and_reject_stale_audits(audit, status):
    from threadkeeper import lessons

    assert verify(audit, status).startswith("ok ")
    revision = read_checks(audit)["lesson_revision"]
    assert lessons.patch_lesson("service-limits", CLAIM, CLAIM + " Use a queue.")
    checks = read_checks(audit)
    assert checks["lesson_revision"] != revision
    assert checks["verifications"][0]["invalidated"] == "lesson_revision_changed"
    assert "needs_recheck (lesson_revision_changed" in tool(audit, "lesson_get")("service-limits")
    assert "needs_recheck" in tool(audit, "lesson_list")(query="service limits")
    assert verify(audit, lesson_revision=revision).startswith("ERR lesson_revision_changed")
    assert verify(audit, "verified").startswith("ok ")
    assert read_checks(audit)["verifications"][0]["invalidated"] == ""
    assert "WARNING factual freshness" not in tool(audit, "lesson_get")("service-limits")


@pytest.mark.parametrize("status", ["verified", "contradicted", "unsubstantiated"])
def test_removed_claims_leave_history_without_warning_about_current_text(audit, status):
    from threadkeeper import lessons

    assert verify(audit, status).startswith("ok ")
    replacement = "The service supports a request queue."
    assert lessons.patch_lesson("service-limits", CLAIM, replacement)
    assert read_checks(audit)["verifications"][0]["invalidated"] == "lesson_revision_changed"
    assert "WARNING factual freshness" not in tool(audit, "lesson_get")("service-limits")
    assert "WARNING factual freshness" not in tool(audit, "lesson_list")(query="service limits")
    assert verify(audit, "contradicted", claim=replacement).startswith("ok ")
    assert tool(audit, "lesson_get")("service-limits").count("WARNING factual freshness") == 1


@pytest.mark.parametrize("status", ["verified", "contradicted", "unsubstantiated"])
def test_source_change_invalidates_other_claims_without_reviving_old_checks(audit, status):
    from threadkeeper import lessons

    lessons.append_lesson("other limits", "The service accepts uploads.")
    assert verify(audit, status).startswith("ok ")
    second_revision = read_checks(audit, "other-limits")["lesson_revision"]
    assert verify(
        audit, "verified", slug="other-limits", lesson_revision=second_revision,
        claim="The service accepts uploads.", source_revision=NEXT_SOURCE_REVISION,
    ).startswith("ok ")
    assert read_checks(audit)["verifications"][0]["invalidated"] == "source_revision_changed"
    assert "needs_recheck (source_revision_changed" in tool(audit, "lesson_get")("service-limits")
    assert verify(
        audit, "verified", slug="other-limits", lesson_revision=second_revision,
        claim="The service accepts uploads.",
    ).startswith("ok ")
    assert read_checks(audit)["verifications"][0]["invalidated"] == "source_revision_changed"
    assert verify(audit, "verified").startswith("ok ")
    assert read_checks(audit)["verifications"][0]["invalidated"] == ""


@pytest.mark.parametrize("overrides, error", [
    ({"status": "stale"}, "status must be"),
    ({"claim": "Invented claim"}, "exact nonempty excerpt"),
    ({"claim": " "}, "exact nonempty excerpt"),
    ({"source_url": "http://docs.example.org"}, "official HTTPS"),
    ({"source_url": "https://user:pass@docs.example.org"}, "official HTTPS"),
    ({"source_url": "https://docs.example.org/#part"}, "official HTTPS"),
    ({"source_revision": ""}, "exactly one"),
    ({"source_text": "ambiguous evidence"}, "exactly one"),
])
def test_invalid_evidence_cannot_create_or_replace_results(audit, overrides, error):
    assert verify(audit).startswith("ok ")
    before = read_checks(audit)
    assert error in verify(audit, **overrides)
    assert read_checks(audit) == before


def test_unknown_and_unprotected_lessons_are_refused(audit, monkeypatch):
    from threadkeeper import lessons

    assert "not_found" in verify(audit, slug="missing")
    monkeypatch.setattr(lessons, "WRITE_ORIGIN", "shadow_review")
    lessons.append_lesson("autonomous rule", CLAIM)
    revision = read_checks(audit, "autonomous-rule")["lesson_revision"]
    assert "lesson_not_protected" in verify(audit, slug="autonomous-rule", lesson_revision=revision)
    assert read_checks(audit)["verifications"] == []


def test_source_text_is_hashed_without_retaining_it(audit):
    source_text = "Official example documentation: twenty requests per minute."
    assert verify(audit, source_revision="", source_text=source_text).startswith("ok ")
    result, = read_checks(audit)["verifications"]
    assert result["source_revision"] == hashlib.sha256(source_text.encode()).hexdigest()
    assert source_text not in json.dumps(result)
    assert verify(audit, "verified", source_revision='"official-etag-v2"').startswith("ok ")
    assert read_checks(audit)["verifications"][0]["source_revision"] == '"official-etag-v2"'


def test_pinned_lesson_can_be_checked_but_cannot_be_edited(audit):
    conn = audit["db"].get_db()
    conn.execute("INSERT INTO lesson_usage (slug, created_at, pinned) VALUES (?, ?, 1)",
                 ("service-limits", int(time.time())))
    conn.commit()
    assert verify(audit).startswith("ok ")
    assert "pinned_lesson" in tool(audit, "lesson_patch")("service-limits", CLAIM, "Changed.")
    assert "ERR" in tool(audit, "lesson_remove")("service-limits", force=True)


def test_curator_can_record_freshness_in_both_modes(audit):
    from threadkeeper import curator

    for mode in ("advisory", "destructive"):
        _, allowed = curator._evaluation_contract(mode, None)
        assert "mcp__thread-keeper__lesson_verifications" in allowed
        assert "mcp__thread-keeper__curator_verify_lesson" in allowed
    assert "unsubstantiated" in curator.CURATOR_PROMPT
    assert "DEFER instead" in curator.CURATOR_PROMPT


def test_verification_reads_are_pure_with_existing_checks(audit):
    assert verify(audit).startswith("ok ")
    conn = audit["db"].get_db()

    def snapshot():
        return [conn.execute(f"SELECT * FROM {table}").fetchall() for table in (
            "events", "lesson_usage", "lesson_claim_verifications", "lesson_verification_sources",
        )]

    before = snapshot()
    assert len(read_checks(audit)["verifications"]) == 1
    assert snapshot() == before


@pytest.mark.parametrize("database_version", [0, 5])
@pytest.mark.parametrize("status", ["verified", "contradicted", "unsubstantiated"])
def test_freshness_tables_reach_existing_databases_and_survive_rebootstrap(
    audit, database_version, status,
):
    db = audit["db"]
    conn = db.get_db()
    conn.execute("DROP TABLE lesson_claim_verifications")
    conn.execute("DROP TABLE lesson_verification_sources")
    conn.execute(f"PRAGMA user_version={database_version}")
    conn.commit()
    conn.close()
    db.bootstrap_db(force=True)
    conn = db.get_db()
    assert conn.execute("SELECT count(*) FROM lesson_claim_verifications").fetchone()[0] == 0
    conn.close()
    assert verify(audit, status).startswith("ok ")
    before = read_checks(audit)
    db.bootstrap_db(force=True)
    conn = db.get_db()
    assert read_checks(audit) == before
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.CURRENT_SCHEMA_VERSION
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lesson_claim_verifications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("slug", "revision", "fingerprint", "claim", "unknown", SOURCE, SOURCE_REVISION, 1, 1),
        )
